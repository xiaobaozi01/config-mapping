"""Cisco IOS XR 配置清洗函数。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..common.lab_account import LAB_PASSWORD, LAB_USERNAME
from ..common.outcomes import CleanupOutcome
from ..common.policies import WashingPolicy
from .document import CiscoDocument, CiscoNode, strip_matching_nodes


# 模块级预编译，避免每次调用重复编译正则，并把清洗数据与遍历逻辑分离。
_MANAGEMENT_TOP_LEVEL: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("username", re.compile(r"^username\b", re.IGNORECASE)),
    ("aaa", re.compile(r"^aaa\b", re.IGNORECASE)),
    ("tacacs", re.compile(r"^(?:tacacs-server|tacacs)\b", re.IGNORECASE)),
    ("radius", re.compile(r"^(?:radius-server|radius)\b", re.IGNORECASE)),
    ("taskgroup", re.compile(r"^task-?group\b", re.IGNORECASE)),
    ("usergroup", re.compile(r"^user-?group\b", re.IGNORECASE)),
    ("snmp", re.compile(r"^snmp-server\b", re.IGNORECASE)),
    ("ssh", re.compile(r"^ssh\b", re.IGNORECASE)),
    ("telnet", re.compile(r"^telnet\b", re.IGNORECASE)),
)

# line 块内需剥除的认证子命令；line 块本身要保留以承载登录终端。
_LINE_AUTH_PATTERN = re.compile(
    r"^(?:password|secret)\b"
    r"|^login\s+authentication\b"
    r"|^authorization\b"
    r"|^accounting\b"
    r"|^users\s+group\b",
    re.IGNORECASE,
)


def clean_management_access(document: CiscoDocument) -> CleanupOutcome:
    """删除 IOS XR 顶层管理面块，并剥除 ``line`` 块内的认证子命令。

    删除 username/aaa/tacacs/radius/taskgroup/usergroup/snmp/ssh/telnet 等顶层块，
    避免生产账号与远程管理凭据进入实验环境；``line`` 块承载 console/vty 等登录终端
    需要保留，因此只递归删除其中的认证子命令而非整块。
    """
    outcome = CleanupOutcome()
    for block in document.root.children:
        if not block.active:
            continue
        header = block.header.strip()
        matched = next(
            (category for category, pattern in _MANAGEMENT_TOP_LEVEL if pattern.match(header)),
            None,
        )
        if matched:
            block.active = False
            outcome.record(matched)
            continue
        if re.match(r"^line\b", header, re.IGNORECASE):
            block.children, removed = strip_matching_nodes(
                block.children,
                _LINE_AUTH_PATTERN,
                match_anywhere=True,
            )
            outcome.record("line-auth-reference", removed)
    return outcome


@dataclass(frozen=True, slots=True)
class _OptionalRule:
    """一条由策略开关控制的可选能力清洗规则。

    用命名字段而非裸元组表达 ``enabled``/``category``/``pattern``，调用处无需
    逐位猜测每个位置的含义。
    """

    enabled: bool
    category: str
    pattern: str


def clean_optional_features(document: CiscoDocument, policy: WashingPolicy) -> CleanupOutcome:
    """按策略开关删除可选能力顶层块，并清理协议认证引用。

    PKI、硬件、NAT 和流量统计会影响业务语义，需显式开启才删除；协议认证则拆成
    “顶层 key-chain 定义”与“协议/接口内引用”两层处理，避免只删定义却遗留失效引用。
    """
    outcome = CleanupOutcome()
    optional_rules = [
        _OptionalRule(
            policy.pki,
            "pki",
            r"^(?:crypto\s+(?:pki|ca|key)\b|certificate\b|trustpoint\b)",
        ),
        _OptionalRule(
            policy.hardware,
            "hardware",
            r"^(?:hw-module|platform|service-location|slot)\b",
        ),
        _OptionalRule(
            policy.nat,
            "nat",
            r"^(?:nat|cgn|service\s+cgn)\b",
        ),
        _OptionalRule(
            policy.flow_statistics,
            "flow-statistics",
            r"^(?:flow(?:-exporter|-monitor)?|sampler|monitor-session)\b",
        ),
    ]
    top_level = [
        (rule.category, re.compile(rule.pattern, re.IGNORECASE))
        for rule in optional_rules
        if rule.enabled
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
    """剥除路由进程、MPLS/RSVP 和接口块内的协议认证子命令。

    认证关键字既可作顶层 key-chain 定义，也会以子命令形式出现在 bgp/ospf 等进程
    与接口内部；两者锚点不同，需单独一层清理，避免漏删散落在子层级里的引用。
    """
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
            block.children, removed = strip_matching_nodes(
                block.children,
                pattern,
                match_anywhere=True,
            )
            outcome.record("protocol-auth-reference", removed)


def add_lab_account(document: CiscoDocument) -> None:
    """在 ``end``/``commit`` 结束标记前插入统一的实验账号。

    清除生产认证后必须建立已知的本地登录入口，否则输出设备无法接管；插在结束
    标记之前可确保账号落在配置主体内，又不破坏结束标记之后的任何内容。
    """
    account = CiscoNode(
        header=f"username {LAB_USERNAME}",
        children=[
            CiscoNode(f"secret 0 {LAB_PASSWORD}"),
            CiscoNode("group root-system"),
        ],
    )
    insert_at = next(
        (
            index
            for index, block in enumerate(document.root.children)
            if block.active and block.header.strip().lower() in {"end", "commit"}
        ),
        len(document.root.children),
    )
    document.root.children[insert_at:insert_at] = [
        account,
        CiscoNode(header="!"),
    ]
