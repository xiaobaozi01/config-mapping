"""Juniper Junos 配置清洗函数。"""

from __future__ import annotations

from ..common.lab_account import JUNOS_LAB_PASSWORD_HASH, LAB_USERNAME
from ..common.errors import require_invariant
from ..common.outcomes import CleanupOutcome
from .document import JunosDocument, JunosNode


def _first_token(document: JunosDocument, node: JunosNode) -> str:
    """返回节点基础 header 的首个语义 token，空内容时返回空串。

    Junos 语句的身份由第一个关键字决定，清洗规则据此匹配；先去掉
    ``inactive:``/``protect:`` 状态前缀和行尾分号，才能让同名命令无论是否
    停用或被保护都识别为同一 token。
    """
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
    """把首 token 命中 ``keywords`` 的子节点标记为停用，并计入 ``category``。

    Junos 用 ``active`` 标记表达逻辑删除，命中节点连同其整棵子树一起停用，因此
    命中后不再下钻；把“匹配 + 停用 + 统计”收敛成单一原语，供管理面清洗中的
    多处递归删除复用。
    """
    if node.children is None:
        return
    for child in node.children:
        if not child.effective:
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


def _disable_empty_netconf(
    document: JunosDocument,
    services: JunosNode,
    outcome: CleanupOutcome,
) -> None:
    """停用 ``system services`` 下没有任何有效子语句的空 ``netconf`` 块。

    Junos 的 netconf 通常随 SSH 一起开启远程管理；空 netconf 块没有业务内容，
    单独识别可避免与 ssh/telnet 的递归清理混在一起。
    """
    for child in services.children or []:
        if (
            child.effective
            and _first_token(document, child) == "netconf"
            and child.children is not None
            and not any(grandchild.effective for grandchild in child.children)
        ):
            child.active = False
            outcome.record("remote-access")


def clean_management_access(document: JunosDocument) -> CleanupOutcome:
    """清除 Junos 原有管理面访问与认证配置，并返回分类统计。

    删除 ``system`` 下的本地认证语句、``system services`` 下的 ssh/telnet 及空
    netconf、顶层 ``snmp`` 和 ``security ssh-known-hosts``。这些是必删项：生产账号、
    外部 AAA、SNMP 和远程管理凭据不应进入隔离实验环境，否则既有泄密风险，也可能
    因外部认证服务器不可达而无法登录；与可改变业务能力的可选清洗分开。
    """
    system = document._top_block("system", create=True)
    require_invariant(
        system is not None and system.children is not None,
        "Junos system 块在 create=True 后必须存在且可包含子节点",
    )
    outcome = CleanupOutcome()
    auth_keywords = {
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
        if not child.effective:
            continue
        token = _first_token(document, child)
        if token in auth_keywords:
            child.active = False
            outcome.record(token)

    system_services = next(
        (
            child
            for child in system.children
            if child.effective
            and child.is_block
            and document._base_header(child.header) == "services"
        ),
        None,
    )
    if system_services:
        _disable_matching(
            document,
            system_services,
            {"ssh", "outbound-ssh", "telnet"},
            "remote-access",
            outcome,
            recursive=True,
        )
        _disable_empty_netconf(document, system_services, outcome)

    snmp = document._top_block("snmp")
    if snmp:
        snmp.active = False
        outcome.record("snmp")

    security = document._top_block("security")
    if security:
        _disable_matching(document, security, {"ssh-known-hosts"}, "ssh-trust", outcome)
    return outcome


def add_lab_account(document: JunosDocument) -> None:
    """向 ``system`` 块追加实验账号与 root 认证。

    清除生产认证后必须建立已知登录入口，否则输出设备无法接管；Junos 不接受
    明文密码，因此 root 与用户都使用预生成的 SHA-512 crypt 哈希而非明文。
    """
    system = document._top_block("system", create=True)
    require_invariant(
        system is not None and system.children is not None,
        "Junos system 块在 create=True 后必须存在且可包含子节点",
    )
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
