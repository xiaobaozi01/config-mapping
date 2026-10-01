"""Juniper Junos 配置清洗函数。"""

from __future__ import annotations

from ...constants import JUNOS_LAB_PASSWORD_HASH, LAB_USERNAME
from ...models import WashingPolicy
from ...parsers.common import CleanupOutcome
from ...parsers.juniper_junos import JunosDocument, JunosNode


def _first_token(document: JunosDocument, node: JunosNode) -> str:
    base = document._base_header(node.header)
    return base.split(maxsplit=1)[0].rstrip(";") if base else ""


def _disable_matching(
    document: JunosDocument,
    node: JunosNode,
    keywords: set[str],
    category: str,
    outcome: CleanupOutcome,
    *,
    recursive: bool = False,
) -> None:
    if node.children is None:
        return
    for child in node.children:
        if not child.active:
            continue
        if _first_token(document, child) in keywords:
            child.active = False
            outcome.record(category)
            continue
        if recursive:
            _disable_matching(
                document,
                child,
                keywords,
                category,
                outcome,
                recursive=True,
    )

def clean_management_access(document: JunosDocument) -> CleanupOutcome:
    system = document._top_block("system", create=True)
    assert system is not None and system.children is not None
    outcome = CleanupOutcome()
    blocked = {
        "login",
        "root-authentication",
        "authentication-order",
        "radius-server",
        "tacplus-server",
        "radius-options",
        "tacplus-options",
        "accounting",
    }
    for child in system.children:
        first = _first_token(document, child)
        if first in blocked:
            child.active = False
            outcome.record(first)

    services = next(
        (
            child
            for child in system.children
            if child.active
            and child.is_block
            and document._base_header(child.header) == "services"
        ),
        None,
    )
    if services:
        _disable_matching(
            document,
            services,
            {"ssh", "outbound-ssh", "telnet"},
            "remote-access",
            outcome,
            recursive=True,
        )
        for child in services.children or []:
            if (
                child.active
                and _first_token(document, child) == "netconf"
                and child.children is not None
                and not any(grandchild.active for grandchild in child.children)
            ):
                child.active = False
                outcome.record("remote-access")

    snmp = document._top_block("snmp")
    if snmp:
        snmp.active = False
        outcome.record("snmp")

    security = document._top_block("security")
    if security:
        _disable_matching(document, security, {"ssh-known-hosts"}, "ssh-trust", outcome)
    return outcome


def clean_optional_features(document: JunosDocument, policy: WashingPolicy) -> CleanupOutcome:
    outcome = CleanupOutcome()
    system = document._top_block("system")
    security = document._top_block("security")

    if policy.protocol_authentication:
        if security:
            _disable_matching(
                document,
                security,
                {"authentication-key-chains"},
                "protocol-auth-definition",
                outcome,
            )
        protocol_keywords = {
            "authentication",
            "authentication-key",
            "authentication-key-chain",
            "authentication-algorithm",
            "authentication-type",
        }
        for root_name in ("protocols", "routing-instances", "logical-systems", "interfaces"):
            root = document._top_block(root_name)
            if root:
                _disable_matching(
                    document,
                    root,
                    protocol_keywords,
                    "protocol-auth-reference",
                    outcome,
                    recursive=True,
                )

    if policy.pki:
        if security:
            _disable_matching(document, security, {"pki", "certificates"}, "pki", outcome)
        if system:
            _disable_matching(document, system, {"certificates"}, "pki", outcome)

    if policy.hardware:
        chassis = document._top_block("chassis")
        if chassis:
            chassis.active = False
            outcome.record("hardware")

    if policy.nat:
        if security:
            _disable_matching(document, security, {"nat"}, "nat", outcome)
        services_top = document._top_block("services")
        if services_top:
            _disable_matching(
                document,
                services_top,
                {"nat", "nat-rules"},
                "nat",
                outcome,
                recursive=True,
            )

    if policy.flow_statistics:
        services_top = document._top_block("services")
        if services_top:
            _disable_matching(
                document,
                services_top,
                {"flow-monitoring"},
                "flow-statistics",
                outcome,
                recursive=True,
            )
        forwarding = document._top_block("forwarding-options")
        if forwarding:
            _disable_matching(
                document,
                forwarding,
                {"sampling"},
                "flow-statistics",
                outcome,
                recursive=True,
            )
        interfaces = document._top_block("interfaces")
        if interfaces:
            _disable_matching(
                document,
                interfaces,
                {"sampling"},
                "flow-statistics",
                outcome,
                recursive=True,
            )
    return outcome


def add_lab_account(document: JunosDocument) -> None:
    system = document._top_block("system", create=True)
    assert system is not None and system.children is not None
    system.children.extend(
        [
            JunosNode(f'root-authentication encrypted-password "{JUNOS_LAB_PASSWORD_HASH}";'),
            JunosNode(
                "login",
                [
                    JunosNode(
                        f"user {LAB_USERNAME}",
                        [
                            JunosNode("class super-user;"),
                            JunosNode(
                                "authentication",
                                [
                                    JunosNode(
                                        f'encrypted-password "{JUNOS_LAB_PASSWORD_HASH}";'
                                    )
                                ],
                            ),
                        ],
                    )
                ],
            ),
        ]
    )
