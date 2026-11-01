"""Juniper Junos 配置清洗函数。"""

from __future__ import annotations

from impl.common.errors import require_invariant
from impl.common.lab_account import JUNOS_LAB_PASSWORD_HASH
from impl.common.outcomes import CleanupOutcome

from .document import JunosDocument, _WorkNode


def _disable_matching(
    node: _WorkNode,
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
        if _first_token(child) in keywords:
            child.active = False
            outcome.record(category)
            continue
        if recursive:
            _disable_matching(
                child,
                keywords,
                category,
                outcome,
                recursive=True,
            )


def _disable_empty_netconf(
    services: _WorkNode,
    outcome: CleanupOutcome,
) -> None:
    """停用 ``system services`` 下没有任何有效子语句的空 ``netconf`` 块。

    Junos 的 netconf 通常随 SSH 一起开启远程管理；空 netconf 块没有业务内容，
    单独识别可避免与 ssh/telnet 的递归清理混在一起。
    """
    for child in services.children or []:
        if (
            child.effective
            and _first_token(child) == "netconf"
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
    root = document._work_root()
    system = _top_block(root, "system", create=True)
    outcome = CleanupOutcome()
    _remove_system_auth(system, outcome)
    _clean_system_services(system, outcome)

    snmp = _top_block(root, "snmp")
    if snmp:
        snmp.active = False
        outcome.record("snmp")

    security = _top_block(root, "security")
    if security:
        _disable_matching(security, {"ssh-known-hosts"}, "ssh-trust", outcome)
    document._publish_work_root(root)
    return outcome


def _remove_system_auth(
    system: _WorkNode,
    outcome: CleanupOutcome,
) -> None:
    """停用 system 下的本地认证、外部 AAA 与 accounting 语句。

    这些生产凭据不应进入隔离实验环境，按关键字集中停用可避免认证路径遗漏。
    """
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
        token = _first_token(child)
        if token in auth_keywords:
            child.active = False
            outcome.record(token)


def _clean_system_services(
    system: _WorkNode,
    outcome: CleanupOutcome,
) -> None:
    """停用远程管理服务并清理空 netconf。

    ssh、telnet 与外部认证必须同时处理，否则设备进入实验环境后仍可能依赖不可达服务。
    """
    system_services = next(
        (
            child
            for child in system.children
            if child.effective
            and child.is_block
            and child.base_header == "services"
        ),
        None,
    )
    if system_services:
        _disable_matching(
            system_services,
            {"ssh", "outbound-ssh", "telnet"},
            "remote-access",
            outcome,
            recursive=True,
        )
        _disable_empty_netconf(system_services, outcome)


def add_lab_account(document: JunosDocument) -> None:
    """向 ``system`` 块追加实验用 root 认证。

    清除生产认证后建立已知的 root 登录入口；Junos 不接受明文密码，
    因此使用预生成的 SHA-512 crypt 哈希。
    """
    root = document._work_root()
    system = _top_block(root, "system", create=True)
    require_invariant(system is not None, "Junos system 工作块必须存在")
    system.children.append(
        _WorkNode(
            "root-authentication encrypted-password "
            f'"{JUNOS_LAB_PASSWORD_HASH}";'
        )
    )
    document._publish_work_root(root)


def _first_token(node: _WorkNode) -> str:
    """返回工作节点基础 header 的首个语义 token。

    清洗规则按命令关键字匹配，去除状态前缀和末尾分号后可保持判断一致。
    """
    base = node.base_header
    return base.split(maxsplit=1)[0].rstrip(";") if base else ""


def _top_block(
    root: _WorkNode, name: str, create: bool = False
) -> _WorkNode | None:
    """在临时工作树中查找或创建顶层配置块。

    清洗流程需要稳定的容器入口，但新建内容只能存在于待发布工作树中。
    """
    for node in root.children or []:
        if node.effective and node.is_block and node.base_header == name:
            return node
    if not create:
        return None
    node = _WorkNode(name, [])
    root.children.append(node)
    return node
