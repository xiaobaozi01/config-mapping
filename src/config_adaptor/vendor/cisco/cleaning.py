"""Cisco IOS XR 配置清洗函数。"""

from __future__ import annotations

import re

from ...constants import LAB_PASSWORD, LAB_USERNAME
from ...models import WashingPolicy
from ...parsers.cisco_iosxr import CiscoDocument, CiscoNode
from ...parsers.common import CleanupOutcome


def clean_management_access(document: CiscoDocument) -> CleanupOutcome:
    outcome = CleanupOutcome()
    top_level: list[tuple[str, re.Pattern[str]]] = [
        ("username", re.compile(r"^username\b", re.IGNORECASE)),
        ("aaa", re.compile(r"^aaa\b", re.IGNORECASE)),
        ("tacacs", re.compile(r"^(?:tacacs-server|tacacs)\b", re.IGNORECASE)),
        ("radius", re.compile(r"^(?:radius-server|radius)\b", re.IGNORECASE)),
        ("taskgroup", re.compile(r"^task-?group\b", re.IGNORECASE)),
        ("usergroup", re.compile(r"^user-?group\b", re.IGNORECASE)),
        ("snmp", re.compile(r"^snmp-server\b", re.IGNORECASE)),
        ("ssh", re.compile(r"^ssh\b", re.IGNORECASE)),
        ("telnet", re.compile(r"^telnet\b", re.IGNORECASE)),
    ]
    line_auth = re.compile(
        r"^(?:password|secret)\b"
        r"|^login\s+authentication\b"
        r"|^authorization\b"
        r"|^accounting\b"
        r"|^users\s+group\b",
        re.IGNORECASE,
    )
    for block in document.root.children:
        if not block.active:
            continue
        header = block.header.strip()
        matched = next(
            (category for category, pattern in top_level if pattern.match(header)),
            None,
        )
        if matched:
            block.active = False
            outcome.record(matched)
            continue
        if re.match(r"^line\b", header, re.IGNORECASE):
            block.children, removed = _strip_sections(block.children, line_auth)
            outcome.record("line-auth-reference", removed)
    return outcome


def clean_optional_features(document: CiscoDocument, policy: WashingPolicy) -> CleanupOutcome:
    outcome = CleanupOutcome()
    optional_top_level: list[tuple[bool, str, str]] = [
        (policy.pki, "pki", r"^(?:crypto\s+(?:pki|ca|key)\b|certificate\b|trustpoint\b)"),
        (policy.hardware, "hardware", r"^(?:hw-module|platform|service-location|slot)\b"),
        (policy.nat, "nat", r"^(?:nat|cgn|service\s+cgn)\b"),
        (
            policy.flow_statistics,
            "flow-statistics",
            r"^(?:flow(?:-exporter|-monitor)?|sampler|monitor-session)\b",
        ),
    ]
    top_level = [
        (category, re.compile(pattern, re.IGNORECASE))
        for enabled, category, pattern in optional_top_level
        if enabled
    ]
    if policy.protocol_authentication:
        top_level.append(
            (
                "protocol-auth-definition",
                re.compile(r"^(?:key\s+chain|key-?chain)\b", re.IGNORECASE),
            )
        )
    for block in document.root.children:
        if not block.active:
            continue
        header = block.header.strip()
        matched = next(
            (category for category, pattern in top_level if pattern.match(header)),
            None,
        )
        if matched:
            block.active = False
            outcome.record(matched)

    if policy.protocol_authentication:
        _clean_protocol_authentication(document, outcome)
    return outcome


def _clean_protocol_authentication(document: CiscoDocument, outcome: CleanupOutcome) -> None:
    protocol_header = re.compile(
        r"^(?:router\s+(?:bgp|isis|ospf|ospfv3|rip)|mpls\s+ldp|rsvp)\b",
        re.IGNORECASE,
    )
    protocol_auth = re.compile(
        r"(?:^|\s)(?:authentication(?:-key(?:-chain)?|-algorithm|-type)?|"
        r"password|key-?chain)(?:\s|$)",
        re.IGNORECASE,
    )
    interface_auth = re.compile(
        r"^(?:authentication(?:-key(?:-chain)?|-algorithm|-type)?|key-?chain)\b",
        re.IGNORECASE,
    )
    for block in document.root.children:
        if not block.active:
            continue
        pattern = None
        if protocol_header.match(block.header.strip()):
            pattern = protocol_auth
        elif block.interface_name:
            pattern = interface_auth
        if pattern:
            block.children, removed = _strip_sections(block.children, pattern)
            outcome.record("protocol-auth-reference", removed)


def _strip_sections(
    nodes: list[CiscoNode],
    pattern: re.Pattern[str],
) -> tuple[list[CiscoNode], int]:
    """递归删除命中认证模式的节点及其全部语义后代。

    AST 使父配置模式和子命令形成完整子树；删除认证父节点时同步删除其 children，
    可避免扁平文本算法遗留无父级的秘密或引用。返回值同时提供保留节点和删除计数。
    """
    retained: list[CiscoNode] = []
    removed = 0
    for node in nodes:
        if not node.is_formatting and pattern.search(node.header):
            removed += sum(1 for _ in node.walk(include_self=True))
            continue
        node.children, child_removed = _strip_sections(node.children, pattern)
        removed += child_removed
        retained.append(node)
    return retained, removed


def add_lab_account(document: CiscoDocument) -> None:
    account = CiscoNode(
        header=f"username {LAB_USERNAME}",
        children=[
            CiscoNode(f"secret 0 {LAB_PASSWORD}"),
            CiscoNode("group root-system"),
        ],
    )
    terminal = next(
        (
            index
            for index, block in enumerate(document.root.children)
            if block.active and block.header.strip().lower() in {"end", "commit"}
        ),
        len(document.root.children),
    )
    document.root.children[terminal:terminal] = [
        account,
        CiscoNode(header="!"),
    ]
