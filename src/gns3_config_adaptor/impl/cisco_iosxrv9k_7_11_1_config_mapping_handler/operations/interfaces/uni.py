"""IOS XR UNI 子接口封装重建与类型专属属性清理。"""

from __future__ import annotations

import re

from ....common.config_ast.editor import format_fragment
from ....common.domain.interface import interface_parent
from ....common.domain.outcomes import InterfaceAdaptationOutcome
from ...support.interface_commands import is_uni_type_attribute
from .access import find_interface_node
from .transform import ensure_parent_interface, remove_interface


def _record(outcome, source, command, reason, action="removed"):
    """记录命令级清理原因和原路径。

    每项变更带稳定规则 ID，便于追溯接口换型导致的配置删除。
    """
    outcome.changes.append(
        {
            "interface": source,
            "command": command,
            "path": [f"interface {source}", command],
            "action": action,
            "reason": reason,
            "rule_id": "cisco.uni.interface_type_compatibility",
        }
    )


def _validate(block, vlan, inner, outcome):
    """拒绝范围、通配封装及无法安全重建的二层标签操作。

    解析器提取到数字不代表完整封装已覆盖，必须校验整条命令。
    """
    encapsulations = [
        node.header
        for node in block.nodes
        if re.match(r"^encapsulation\b", node.header, re.I)
    ]
    old_vlan = None
    for command in encapsulations:
        match = re.fullmatch(
            r"encapsulation dot1q (\d+)(?: second-dot1q (\d+))?", command, re.I
        )
        if not match:
            outcome.errors.append(
                f"{block.interface_name}: 无法安全迁移封装 {command}"
            )
        else:
            old_vlan = int(match.group(1))
    if len(encapsulations) > 1:
        outcome.errors.append(f"{block.interface_name}: 存在多个封装定义")
    rewrites = [
        node.header
        for node in block.nodes
        if re.match(r"^rewrite\b", node.header, re.I)
    ]
    for command in rewrites:
        if not re.fullmatch(
            r"rewrite ingress tag pop [12] symmetric", command, re.I
        ):
            outcome.errors.append(
                f"{block.interface_name}: 无法安全迁移标签重写 {command}"
            )
    if (
        block.l2transport
        and old_vlan is not None
        and old_vlan != vlan
        and not rewrites
    ):
        outcome.errors.append(
            f"{block.interface_name}: 二层业务重编号会改变客户标签，缺少安全的标签归一化"
        )
    if inner is not None and not 1 <= inner <= 4094:
        outcome.errors.append(f"{block.interface_name}: QinQ 内层 VLAN 非法")


def _parent_payload(document, block, parent, outcome):
    """保留共享父口所需 MTU，将可用的 ethertype 留给兼容阶段迁移。

    未知父口属性留在源暂存树，待正常清洗后再检查，避免提前拦截可清洗配置。
    """
    source_parent = find_interface_node(
        document, interface_parent(block.interface_name)
    )
    if source_parent is None or source_parent is block:
        return []
    payload = []
    for node in source_parent.nodes:
        command = node.header
        if node.is_formatting:
            continue
        if is_uni_type_attribute(command):
            _record(
                outcome,
                source_parent.interface_name,
                command,
                "原父口类型专属属性不适用于共享 UNI",
            )
        elif re.match(
            r"^(?:description|(?:no\s+)?shutdown)\b", command, re.I
        ) or re.match(
            r"^(?:ipv[46]\s+(?:address|unnumbered|enable)|vrf)\b", command, re.I
        ):
            continue
        elif re.fullmatch(r"mtu\s+\d+", command, re.I):
            _merge_mtu(
                document, parent, command, outcome, source_parent.interface_name
            )
        elif re.search(r"tunneling\s+ethertype\b", command, re.I):
            payload.append(node)
        # 其他父口配置留在暂存树，等待后续 YAML 清洗后再检查。
    return payload


def _merge_mtu(document, parent, command, outcome, source):
    """将物理介质 MTU 的最大值配置到共享父口。

    同一共享链路必须能承载各逻辑业务，不把物理 MTU 丢进不合适的层级。
    """
    block = find_interface_node(document, parent)
    existing = [
        node
        for node in block.nodes
        if re.fullmatch(r"mtu\s+\d+", node.header, re.I)
    ]
    size = max(
        [
            int(command.split()[1]),
            *(int(node.header.split()[1]) for node in existing),
        ]
    )
    transaction = document.edit()
    for node in existing:
        transaction.delete(node, "合并共享父口介质 MTU")
    transaction.append_child(block, f" mtu {size}\n", "保留最大介质 MTU")
    transaction.apply()
    _record(outcome, source, command, "介质 MTU 合并到共享父口", "moved")


def map_uni(
    document, source: str, parent: str, vlan: int, inner_vlan: int | None
) -> InterfaceAdaptationOutcome:
    """把一个暂存接口复制为单层或双层 UNI 业务子接口。

    先完整校验封装和父级依赖，再清理类型专属属性并创建新业务入口。
    """
    outcome = InterfaceAdaptationOutcome()
    block = find_interface_node(document, source)
    if block is None:
        outcome.errors.append(f"UNI 暂存接口不存在: {source}")
        return outcome
    _validate(block, vlan, inner_vlan, outcome)
    if outcome.errors:
        return outcome
    ensure_parent_interface(document, parent)
    inherited = _parent_payload(document, block, parent, outcome)
    if outcome.errors:
        return outcome
    target = f"{parent}.{vlan}"
    commands = _payload(document, block, parent, inherited, outcome)
    suffix = f" second-dot1q {inner_vlan}" if inner_vlan is not None else ""
    commands.insert(0, f" encapsulation dot1q {vlan}{suffix}\n")
    if block.l2transport and not any(
        re.match(r"^encapsulation\b", node.header, re.I) for node in block.nodes
    ):
        commands.append(" rewrite ingress tag pop 1 symmetric\n")
    header = f"interface {target}" + (
        " l2transport" if block.l2transport else ""
    )
    transaction = document.edit()
    from ...syntax.rewrite_helpers import append_top_level

    append_top_level(
        transaction,
        document,
        header + "\n" + "".join(commands) + "!\n",
        "创建 UNI 业务子接口",
    )
    transaction.apply()
    outcome.changes.append(
        {
            "interface": target,
            "action": "mapped",
            "reason": "重建 UNI 业务封装",
            "outer_vlan": vlan,
            "inner_vlan": inner_vlan,
        }
    )
    return outcome


def _payload(document, block, parent, inherited, outcome):
    """过滤旧封装和失效聚合属性，其余配置交由正常清洗阶段处理。

    未识别命令保留，统一由最终校验告警，避免迁移和校验对同一命令重复报告。
    """
    commands = []
    for node in [*block.nodes, *inherited]:
        command = node.header
        if node.is_formatting:
            continue
        if re.match(r"^encapsulation\b", command, re.I):
            _record(
                outcome,
                block.interface_name,
                command,
                "原封装由目标 VLAN 匹配替代",
            )
        elif is_uni_type_attribute(command):
            _record(
                outcome,
                block.interface_name,
                command,
                "类型专属配置不适用于 UNI 物理子接口",
            )
        else:
            if re.fullmatch(r"mtu\s+\d+", command, re.I):
                _merge_mtu(
                    document,
                    parent,
                    command,
                    outcome,
                    block.interface_name,
                )
            commands.append(format_fragment(document.writer.subtree(node), 1))
    return commands


def finalize_uni_source(
    document, source: str, check_parent: bool
) -> InterfaceAdaptationOutcome:
    """在 YAML 清洗后检查父级剩余配置，再移除 UNI 暂存树。

    不识别硬件命令，已由清洗删除的配置不会提前触发迁移错误；仍未安全继承的
    父级业务属性必须报错，不能随暂存树静默丢弃。
    """
    outcome = InterfaceAdaptationOutcome()
    parent = find_interface_node(document, source)
    if parent is None:
        remove_interface(document, source, include_children=True)
        return outcome
    handled = re.compile(
        r"^(?:description|(?:no\s+)?shutdown|mtu|vrf|"
        r"ipv[46]\s+(?:address|unnumbered|enable))\b",
        re.I,
    )
    if check_parent:
        for node in parent.nodes:
            command = node.header
            if (
                node.is_formatting
                or handled.match(command)
                or is_uni_type_attribute(command)
            ):
                continue
            if re.search(r"tunneling\s+ethertype\b", command, re.I):
                continue
            outcome.errors.append(
                f"{source}: 清洗后原父口配置仍无法安全继承到共享子接口: {command}"
            )
    if not outcome.errors:
        remove_interface(document, source, include_children=True)
    return outcome
