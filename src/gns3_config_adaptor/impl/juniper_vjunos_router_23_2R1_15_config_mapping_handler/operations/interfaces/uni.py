"""Junos UNI unit 迁移及父口专属属性适配。"""

from __future__ import annotations

import re

from ....common.domain.interface import interface_parent, interface_unit
from ....common.domain.outcomes import InterfaceAdaptationOutcome
from ...support.interface_commands import is_uni_type_attribute
from ...syntax.ast import JuniperNode
from .access import find_interface_node, unit_nodes, unit_number
from .transform import ensure_parent_interface, remove_interface

_TAGS = re.compile(
    r"^(?:vlan-id|vlan-tags|vlan-id-list|native-vlan-id|vlan-tagging|flexible-vlan-tagging|stacked-vlan-tagging)\b"
)
_KNOWN = re.compile(
    r"^(?:description|family|encapsulation|vlan-id|vlan-tags|input-vlan-map|output-vlan-map|mtu|bandwidth|filter|policer|disable|enable|accounting|rpf-check|no-redirects)\b"
)


def _record(outcome, source, command, reason, action="removed"):
    """记录 Junos 配置从原层级删除或迁移的原因。

    源路径与规则 ID 一起写入报告，避免兼容清理成为无法解释的配置缺失。
    """
    outcome.changes.append(
        {
            "interface": source,
            "command": command,
            "path": [
                "interfaces",
                interface_parent(source),
                *(
                    [f"unit {interface_unit(source)}"]
                    if interface_unit(source) is not None
                    else []
                ),
                command,
            ],
            "action": action,
            "reason": reason,
            "rule_id": "juniper.uni.interface_type_compatibility",
        }
    )


def _effective(document, node):
    """读取去除状态前缀的有效语句文本。

    所有层级使用一致的分号规范化，避免叶子与块配置走不同匹配分支。
    """
    return document._parser.base_header(node.header).rstrip(";")


def _validate(document, source, payload, vlan, inner, outcome):
    """验证整个标签匹配和重写表达式均可按单项业务迁移。

    范围、通配匹配不能被简化为解析器提取到的第一个 VLAN 数字。
    """
    original = None
    l2 = False
    maps = []
    for node in payload.nodes:
        command = _effective(document, node)
        if command.startswith(("vlan-id", "vlan-tags")):
            match = re.fullmatch(
                r"(?:vlan-id|vlan-tags outer) (\d+)(?: inner \d+)?", command
            )
            if match:
                original = int(match.group(1))
            else:
                outcome.errors.append(f"{source}: 无法安全迁移封装 {command}")
        elif command.startswith("encapsulation "):
            if command.split()[1] not in {
                "ethernet-ccc",
                "vlan-ccc",
                "ethernet-bridge",
                "vlan-bridge",
            }:
                outcome.errors.append(f"{source}: 不支持源 unit 封装 {command}")
        elif command.startswith(("input-vlan-map", "output-vlan-map")):
            maps.append(node)
        l2 = l2 or bool(
            re.match(r"family\s+(?:bridge|ethernet-switching|ccc)\b", command)
        )
    normalized = _validate_maps(document, source, maps, outcome)
    if l2 and original is not None and original != vlan and not normalized:
        outcome.errors.append(f"{source}: 二层 VLAN 重编号缺少安全的标签归一化")
    if inner is not None and not 1 <= inner <= 4094:
        outcome.errors.append(f"{source}: QinQ 内层 VLAN 非法")


def _validate_maps(document, source, maps, outcome):
    """只允许已建模的简单标签剥离和对称回加操作。

    带显式标签值的替换或混合操作需要单独建模，不能沿用可能失效的源语义。
    """
    operations = {}
    for node in maps:
        header = _effective(document, node).split()
        commands = (
            header[1:]
            if len(header) > 1
            else [_effective(document, child) for child in node.nodes]
        )
        if len(commands) != 1 or commands[0] not in {
            "pop",
            "pop-pop",
            "push",
            "push-push",
        }:
            outcome.errors.append(
                f"{source}: 无法安全转换 {_effective(document, node)}"
            )
        else:
            operations[header[0]] = commands[0]
    ingress = operations.get("input-vlan-map")
    return (ingress, operations.get("output-vlan-map")) in {
        ("pop", "push"),
        ("pop-pop", "push-push"),
    }


def _prepare_target(document, parent):
    """创建共享父口并补齐灵活 VLAN 和以太网业务封装。

    该最小配置同时承载普通单层标签和原有双层标签 unit。
    """
    ensure_parent_interface(document, parent)
    target = find_interface_node(document, parent)
    for statement in (
        "flexible-vlan-tagging;",
        "encapsulation flexible-ethernet-services;",
    ):
        if statement not in {child.header for child in target.nodes}:
            target.children.append(JuniperNode(statement))
    return target


def _parent_config(document, source, node, target, outcome):
    """处理物理父口属性，只把明确需要的属性带到共享父口。

    聚合结构和 MTU 在迁移时处理，其余父口属性留在暂存树等待正常清洗后检查。
    """
    parser = document._parser
    source_parent = interface_parent(source)
    inherited = []
    encapsulation = None
    for child in node.nodes:
        if not parser.is_effective(child) or parser.is_unit(child.header):
            continue
        command = _effective(document, child)
        if is_uni_type_attribute(command):
            _record(
                outcome,
                source_parent,
                command,
                "原端口介质或聚合属性不适用于共享 UNI",
            )
        elif _TAGS.match(command) or command in {"enable", "disable"}:
            _record(
                outcome,
                source_parent,
                command,
                "源父口终结和启停配置由共享口重建",
            )
        elif re.fullmatch(r"mtu\s+\d+", command):
            _merge_mtu(document, target, int(command.split()[1]))
            _record(
                outcome,
                source_parent,
                command,
                "介质 MTU 合并到共享父口",
                "moved",
            )
        elif command.startswith("encapsulation "):
            encapsulation = command.split()[1]
            if encapsulation not in {
                "ethernet-ccc",
                "vlan-ccc",
                "ethernet-bridge",
                "vlan-bridge",
                "flexible-ethernet-services",
            }:
                outcome.errors.append(f"{source}: 不支持源父口封装 {command}")
        elif command.startswith(("description ", "family ")):
            inherited.append(child.clone())
        # 其他父口配置留在暂存树，等待后续 YAML 清洗后再检查。
    return inherited, encapsulation


def _merge_mtu(document, parent, size):
    """按最大值合并源端口的物理介质 MTU。

    共享父口需要容纳所有业务，逻辑 unit 的协议 MTU 仍保留原配置。
    """
    nodes = [
        node
        for node in parent.nodes
        if re.fullmatch(r"mtu\s+\d+", _effective(document, node))
    ]
    size = max(
        [size, *(int(_effective(document, node).split()[1]) for node in nodes)]
    )
    parent.children = [node for node in parent.children if node not in nodes]
    parent.children.append(JuniperNode(f"mtu {size};"))


def _payload(document, source, payload, outcome):
    """复制 unit 业务并重建标签，常规配置删除留给正常 YAML 清洗。

    不删除 family 中的业务 VLAN 成员关系，未知 unit 命令保留并告警。
    """
    retained = []
    for child in payload.nodes:
        if not document._parser.is_effective(child):
            continue
        command = _effective(document, child)
        if _TAGS.match(command) or is_uni_type_attribute(command):
            _record(
                outcome, source, command, "逻辑接口重建标签并清理类型专属属性"
            )
        else:
            retained.append(child.clone())
            if not _KNOWN.match(command):
                outcome.warnings.append(
                    f"{source}: 未覆盖逻辑命令族 {command.split()[0]}，保留待镜像核验"
                )
    return retained


def _encapsulate(document, unit, inherited, encapsulation, vlan, inner):
    """重建单层或双层标签，并适配 CCC 与桥接封装层级。

    端口级 ethernet-ccc 不能照搬到 unit，应转换为逻辑 vlan-ccc。
    """
    present = {_effective(document, node).split()[0] for node in unit.nodes}
    unit.children.extend(
        node
        for node in inherited
        if _effective(document, node).split()[0] not in present
    )
    old = next(
        (
            _effective(document, node).split()[1]
            for node in unit.nodes
            if _effective(document, node).startswith("encapsulation ")
        ),
        encapsulation,
    )
    unit.children = [
        node
        for node in unit.children
        if not _effective(document, node).startswith("encapsulation ")
    ]
    if old in {"ethernet-ccc", "vlan-ccc"}:
        unit.children.append(JuniperNode("encapsulation vlan-ccc;"))
        if "family" not in present:
            unit.children.append(JuniperNode("family ccc;"))
    elif old in {"ethernet-bridge", "vlan-bridge"}:
        unit.children.append(JuniperNode("encapsulation vlan-bridge;"))
    tag = (
        f"vlan-id {vlan};"
        if inner is None
        else f"vlan-tags outer {vlan} inner {inner};"
    )
    unit.children.insert(0, JuniperNode(tag))


def map_uni(
    document, source: str, parent: str, vlan: int, inner_vlan: int | None
) -> InterfaceAdaptationOutcome:
    """将一个源 unit 或物理口业务迁入共享 UNI 的新 unit。

    临时源树继续保留给其他 unit，所有业务完成后由流水线统一清理。
    """
    outcome = InterfaceAdaptationOutcome()
    node = find_interface_node(document, source)
    if node is None:
        outcome.errors.append(f"UNI 暂存接口不存在: {source}")
        return outcome
    units = unit_nodes(document, node)
    payload = next(
        (
            unit
            for unit in units
            if unit_number(document, unit) == interface_unit(source)
        ),
        None,
    )
    if payload is None and interface_unit(source) is not None:
        outcome.errors.append(f"UNI 暂存 unit 不存在: {source}")
        return outcome
    payload = payload or JuniperNode("unit 0", [])
    _validate(document, source, payload, vlan, inner_vlan, outcome)
    if outcome.errors:
        return outcome
    target = _prepare_target(document, parent)
    inherited, encapsulation = _parent_config(
        document, source, node, target, outcome
    )
    if outcome.errors:
        return outcome
    unit = JuniperNode(
        f"unit {vlan}", _payload(document, source, payload, outcome)
    )
    _encapsulate(document, unit, inherited, encapsulation, vlan, inner_vlan)
    target.children.append(unit)
    outcome.changes.append(
        {
            "interface": f"{parent}.{vlan}",
            "action": "mapped",
            "reason": "重建 UNI 业务封装",
            "outer_vlan": vlan,
            "inner_vlan": inner_vlan,
        }
    )
    return outcome


def finalize_uni_source(
    document, source: str, check_parent: bool
) -> InterfaceAdaptationOutcome:
    """在正常 YAML 清洗后确认父口没有被遗漏的业务配置。

    unit 已按业务复制，未知父级属性先等待清洗；清洗后仍无法安全分配的属性
    阻断输出，网关和目标父口配置保持不变。
    """
    outcome = InterfaceAdaptationOutcome()
    parent = find_interface_node(document, source)
    if parent is None:
        return outcome
    handled = re.compile(
        r"^(?:description|mtu|family|encapsulation|enable|disable)\b"
    )
    for node in parent.nodes:
        if not document._parser.is_effective(node) or document._parser.is_unit(
            node.header
        ):
            continue
        command = _effective(document, node)
        if (
            handled.match(command)
            or _TAGS.match(command)
            or is_uni_type_attribute(command)
        ):
            continue
        outcome.errors.append(
            f"{source}: 清洗后原父口配置仍无法安全迁移: {command}"
        )
    if not outcome.errors:
        remove_interface(document, source, include_children=True)
    return outcome
