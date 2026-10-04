"""Juniper Junos 接口分析与修改函数。"""

from __future__ import annotations

import re

from ...parsers.common import InterfaceKind, InterfaceSpec, interface_parent, interface_unit
from ...parsers.juniper_junos import (
    JunosDocument,
    JunosNode,
    _junos_interface_kind,
    canonical_junos_interface,
)


def interface_specs(document: JunosDocument) -> list[InterfaceSpec]:
    """提取 Junos 接口或 unit 的父子关系、类型及 VLAN 规划信息。

    有 unit 时每个 unit 是独立业务对象，无 unit 时才把父口本身作为对象。IRB
    unit 若没有显式 VLAN 且编号合法，则按 Junos 常见的一一对应方式把 unit 号
    作为 VLAN，供广播域网关关联和 UNI 标签保留使用。
    """
    result: list[InterfaceSpec] = []
    for node in document._interface_nodes():
        parent = document._interface_name(node)
        units = document._unit_nodes(node)
        if not units:
            result.append(
                InterfaceSpec(parent, parent, None, None, _junos_interface_kind(parent))
            )
            continue
        for unit in units:
            number = document._unit_number(unit)
            vlan = document._find_vlan(unit)
            if (
                vlan is None
                and parent.lower() == "irb"
                and number.isdigit()
                and 1 <= int(number) <= 4094
            ):
                vlan = int(number)
            result.append(
                InterfaceSpec(
                    name=f"{parent}.{number}",
                    parent=parent,
                    unit=number,
                    vlan=vlan,
                    kind=_junos_interface_kind(parent),
                    inner_vlan=document._find_inner_vlan(unit),
                )
            )
    return result


def interface_kind(document: JunosDocument, name: str) -> InterfaceKind:
    """按 Junos 命名规则返回规范化接口的业务类别。

    ``document`` 保留在签名中是为了实现统一的厂商接口协议；分类本身只依赖
    接口名，供拓扑校验及物理口、ae 聚合口、IRB 网关口分流使用。
    """
    return _junos_interface_kind(canonical_junos_interface(name))


def bundle_members(document: JunosDocument) -> dict[str, str]:
    """返回物理接口到其 ``ae`` 聚合接口的映射。

    方法只检查物理接口节点中的 ``802.3ad`` 绑定，供迁移流程定位并清理真实
    聚合成员；其他接口类型即使包含相似文本也不会被误判。
    """
    result: dict[str, str] = {}
    for node in document._interface_nodes():
        parent = document._interface_name(node)
        if _junos_interface_kind(parent) != InterfaceKind.PHYSICAL:
            continue
        rendered = document._render_effective_node(node, 0)
        match = re.search(r"\b802\.3ad\s+(ae\d+)\s*;", rendered)
        if match:
            result[parent] = match.group(1)
    return result


def resolve_interface(document: JunosDocument, value: str) -> str:
    """把外部输入的 Junos 接口名转换为解析器使用的规范形式。

    ``document`` 仅用于与其他厂商实现保持统一调用形式；名称统一后，拓扑数据
    才能与配置树中的接口节点可靠匹配。
    """
    return canonical_junos_interface(value)


def logical_names_under(document: JunosDocument, parent: str) -> list[str]:
    """列出指定父接口对应的全部已配置业务名称，并把父口排在 unit 前。

    对含 unit 的 Junos 接口，接口规格只包含各 unit；无 unit 时则返回父口本身。
    集合去重和稳定排序便于 NNI 克隆以及生成可重复的审计映射。
    """
    parent = canonical_junos_interface(parent)
    return sorted(
        {spec.name for spec in interface_specs(document) if spec.parent == parent},
        key=lambda value: (interface_unit(value) is not None, value),
    )


def business_interface_names(document: JunosDocument) -> set[str]:
    """识别实际承载三层、二层或活跃广播域网关业务的 Junos 接口。

    物理口和 ae 口若自身含业务 family、CCC、桥接或 VLAN 映射语句，或被接口树
    之外的有效配置显式引用，就视为活跃；父口引用会激活其所有 unit。随后从活跃
    二层接口收集 VLAN ID/名称，并沿 bridge-domain 或 vlans 配置激活对应 IRB。
    这样可迁移真正关联的网关，同时避免仅因 IRB 配有地址就误判为当前 UNI 业务。
    """
    specs = interface_specs(document)
    known = {spec.name for spec in specs}
    kinds = {spec.name: spec.kind for spec in specs}
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    active: set[str] = set()
    business_pattern = re.compile(
        r"\b(?:family\s+(?:inet6?|ccc|bridge|ethernet-switching)|"
        r"encapsulation\s+(?:ethernet-ccc|vlan-ccc)|input-vlan-map|output-vlan-map)\b"
    )
    for node in document._interface_nodes():
        parent = document._interface_name(node)
        units = document._unit_nodes(node)
        if not units:
            rendered = document._render_effective_node(node, 0)
            if _junos_interface_kind(parent) in mappable_kinds and business_pattern.search(rendered):
                active.add(parent)
            continue
        for unit in units:
            rendered = document._render_effective_node(unit, 0)
            name = f"{parent}.{document._unit_number(unit)}"
            if kinds.get(name) in mappable_kinds and business_pattern.search(rendered):
                active.add(name)

    external = "\n".join(
        document._render_effective_node(node, 0)
        for node in (document.root.children or [])
        if node.effective
        and document._base_header(node.header) not in {"interfaces", "groups"}
    )
    for name in known:
        if kinds.get(name) in mappable_kinds and re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_.-])",
            external,
        ):
            active.add(name)
    for parent in {spec.parent for spec in specs}:
        parent_specs = [spec for spec in specs if spec.parent == parent]
        if parent_specs and parent_specs[0].kind in mappable_kinds and re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(parent)}(?![A-Za-z0-9_.-])",
            external,
        ):
            active.update(spec.name for spec in parent_specs)

    spec_by_name = {spec.name: spec for spec in specs}
    active_vlans: set[int] = set()
    active_vlan_names: set[str] = set()
    for name in list(active):
        spec = spec_by_name.get(name)
        if not spec or spec.kind == InterfaceKind.GATEWAY:
            continue
        node = find_interface_node(document, spec.parent)
        if not node:
            continue
        units = document._unit_nodes(node)
        unit = next(
            (
                item
                for item in units
                if spec.unit is not None
                and document._unit_number(item) == spec.unit
            ),
            node if spec.unit is None else None,
        )
        if unit is None:
            continue
        rendered = document._render_effective_node(unit, 0)
        is_l2 = bool(
            re.search(
                r"\b(?:family\s+(?:ccc|bridge|ethernet-switching)|"
                r"encapsulation\s+(?:ethernet-ccc|vlan-ccc)|"
                r"vlan-id-list|vlan\s+members|input-vlan-map|output-vlan-map)\b",
                rendered,
            )
        )
        if is_l2 and spec.vlan is not None:
            active_vlans.add(spec.vlan)
        for match in re.finditer(r"vlan-id-list\s+\[([^\]]+)\]", rendered):
            active_vlans.update(_expand_vlan_tokens(match.group(1)))
        for match in re.finditer(
            r"vlan\s+members\s+(?:\[([^\]]+)\]|([^;\s]+))\s*;",
            rendered,
        ):
            payload = match.group(1) or match.group(2) or ""
            active_vlans.update(_expand_vlan_tokens(payload))
            active_vlan_names.update(
                token
                for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", payload)
                if not token.isdigit()
            )

    _activate_domain_gateways(
        document,
        document.root,
        active,
        active_vlans,
        active_vlan_names,
        known,
    )
    for spec in specs:
        if spec.kind == InterfaceKind.GATEWAY and spec.vlan in active_vlans:
            active.add(spec.name)
    return active


def _expand_vlan_tokens(value: str) -> set[int]:
    """把 VLAN 列表文本中的单值和闭区间展开为合法 VLAN ID 集合。

    仅接受 1 到 4094 且起点不大于终点的值，避免畸形配置扩大匹配范围；结果用于
    将 ``vlan-id-list`` 和 ``vlan members`` 与广播域网关关联。
    """
    result: set[int] = set()
    for start, end, single in re.findall(r"(?:(\d+)\s*-\s*(\d+))|(\d+)", value):
        if single:
            number = int(single)
            if 1 <= number <= 4094:
                result.add(number)
            continue
        lower, upper = int(start), int(end)
        if 1 <= lower <= upper <= 4094:
            result.update(range(lower, upper + 1))
    return result


def _activate_domain_gateways(
    document: JunosDocument,
    node: JunosNode,
    active: set[str],
    active_vlans: set[int],
    active_vlan_names: set[str],
    known: set[str],
) -> None:
    """递归查找活跃广播域，并把其已知三层网关加入业务接口集合。

    bridge-domain/vlan 只要显式接口、VLAN ID 或 VLAN 名命中已识别的二层业务，
    其中的 ``routing-interface``/``l3-interface`` 才会被激活；该约束防止迁移与
    当前接入业务无关的 IRB。递归遍历用于兼容这些配置位于不同 Junos 层级的情况。
    """
    if node.children is None:
        return
    base = document._base_header(node.header).rstrip(";")
    if base in {"bridge-domains", "vlans"}:
        for domain in node.children:
            if not domain.effective or domain.children is None:
                continue
            rendered = document._render_effective_node(domain, 0)
            interfaces = {
                canonical_junos_interface(value)
                for value in re.findall(
                    r"(?<![-\w])interface\s+([^;\s]+)\s*;",
                    rendered,
                )
            }
            gateways = {
                canonical_junos_interface(value)
                for value in re.findall(
                    r"\b(?:routing-interface|l3-interface)\s+([^;\s]+)\s*;",
                    rendered,
                )
            }
            vlan_ids = {
                int(value)
                for value in re.findall(r"\bvlan-id\s+(\d+)\s*;", rendered)
                if 1 <= int(value) <= 4094
            }
            domain_name = document._base_header(domain.header).split()[0].rstrip(";")
            if (
                interfaces & active
                or vlan_ids & active_vlans
                or domain_name in active_vlan_names
            ):
                active.update(gateway for gateway in gateways if gateway in known)
    for child in node.children:
        if child.effective:
            _activate_domain_gateways(
                document,
                child,
                active,
                active_vlans,
                active_vlan_names,
                known,
            )


def find_interface_node(document: JunosDocument, name: str) -> JunosNode | None:
    """按规范化父接口名查找第一个有效的 Junos 接口节点。

    调用方可以传父接口或 unit 名；先去掉 unit 是因为 Junos 把 unit 存在父接口
    节点内部，而不是独立的顶层接口节点。
    """
    canonical = canonical_junos_interface(interface_parent(name))
    return next(
        (
            node
            for node in document._interface_nodes()
            if document._interface_name(node) == canonical
        ),
        None,
    )


def remove_interface(document: JunosDocument, name: str, include_children: bool = False) -> None:
    """逻辑停用指定接口所在的整个 Junos 父接口节点。

    Junos 的 unit 是父节点的子树，因此停用父节点会自然包含全部 unit；
    ``include_children`` 仅为兼容统一厂商协议而保留，在此实现中不会改变行为。
    使用活动标记而非物理删除可保留原始树结构和编辑上下文。
    """
    node = find_interface_node(document, name)
    if node:
        node.active = False


def rename_interface_tree(
    document: JunosDocument,
    source: str,
    target: str,
    strip_bundle: bool = False,
) -> None:
    """重命名整个 Junos 接口节点，并在需要时移除聚合相关 options。

    父节点改名会连同所有 unit 一起迁移。``strip_bundle`` 用于接口脱离 ae 后删除
    aggregated/gigether/ether options，避免残留旧成员关系；若目标已存在，则合并
    两棵树以避免重复接口定义。
    """
    node = find_interface_node(document, source)
    if not node:
        return
    node.header = canonical_junos_interface(target)
    if strip_bundle and node.children is not None:
        node.children = [
            child
            for child in node.children
            if not (
                child.effective
                and child.is_block
                and document._base_header(child.header)
                in {"aggregated-ether-options", "gigether-options", "ether-options"}
            )
        ]
    _merge_duplicate_interface(document, node)


def clone_interface_tree(
    document: JunosDocument,
    source: str,
    target: str,
    strip_bundle: bool = False,
) -> None:
    """深拷贝源 Junos 接口树到目标节点，供逻辑口拆分到多个目标口。

    克隆会重新激活节点，并可剥离不应复制的聚合 options。源不存在时仍创建空
    目标节点，使后续迁移步骤有稳定落点；追加后与同名节点合并，避免重复定义。
    """
    source_node = find_interface_node(document, source)
    interfaces = document._interfaces_block(create=True)
    assert interfaces is not None and interfaces.children is not None
    if source_node is None:
        clone = JunosNode(canonical_junos_interface(target), [])
    else:
        clone = source_node.clone()
        clone.header = canonical_junos_interface(target)
        clone.active = True
        if strip_bundle and clone.children is not None:
            clone.children = [
                child
                for child in clone.children
                if not (
                    child.effective
                    and child.is_block
                    and document._base_header(child.header)
                    in {"aggregated-ether-options", "gigether-options", "ether-options"}
                )
            ]
    interfaces.children.append(clone)
    _merge_duplicate_interface(document, clone)


def _merge_duplicate_interface(document: JunosDocument, preferred: JunosNode) -> None:
    """合并同名接口节点的非重复子树，并停用首选节点之外的副本。

    接口改名或克隆可能与已有目标节点碰撞；以渲染后的子节点文本判重可保留双方
    的不同配置，同时让最终输出只剩一个活动接口定义。
    """
    name = document._interface_name(preferred)
    duplicates = [
        node
        for node in document._interface_nodes()
        if document._interface_name(node) == name
    ]
    if len(duplicates) < 2:
        return
    assert preferred.children is not None
    existing = {
        document._render_node(child, 0) for child in preferred.children
    }
    for node in duplicates:
        if node is preferred or node.children is None:
            continue
        for child in node.children:
            rendered = document._render_node(child, 0)
            if rendered not in existing:
                preferred.children.append(child)
                existing.add(rendered)
        node.active = False


def _strip_vlan_termination(document: JunosDocument, nodes: list[JunosNode]) -> list[JunosNode]:
    """递归复制节点，并删除旧 VLAN 匹配、标签模式及 rewrite 配置。

    生成新的 QinQ unit 前必须清除 ``vlan-id``、``vlan-tags``、VLAN map、
    switching mode 等旧终结语义，否则会与新标签冲突。函数返回清洗后的克隆，
    从而保留源树及其中与 VLAN 终结无关的业务 family、地址或 CCC 配置。
    """
    blocked = re.compile(
        r"^(?:vlan-id(?:-list)?|vlan-tags|native-vlan-id|"
        r"input-vlan-map|output-vlan-map|interface-mode|"
        r"flexible-vlan-tagging|stacked-vlan-tagging|vlan-tagging)\b"
    )
    retained: list[JunosNode] = []
    for node in nodes:
        if not node.effective:
            retained.append(node.clone())
            continue
        base = document._base_header(node.header).rstrip(";")
        if blocked.match(base) or re.match(r"^vlan\s+members\b", base):
            continue
        if node.is_block and base == "vlan":
            continue
        clone = node.clone()
        if clone.children is not None:
            clone.children = _strip_vlan_termination(document, clone.children)
        retained.append(clone)
    return retained


def _ensure_target_parent(document: JunosDocument, target_parent: str) -> JunosNode:
    """确保目标父口存在并具备承载灵活 QinQ unit 的接口级配置。

    方法保留已有 unit，但清除父口层面的旧 VLAN 终结和 bridge encapsulation，
    再补齐 ``flexible-vlan-tagging`` 与 ``flexible-ethernet-services``。这样新 unit
    可以使用双层标签，同时不会破坏目标口上已经存在的业务 unit。
    """
    existing = find_interface_node(document, target_parent)
    if existing:
        node = existing
    else:
        interfaces = document._interfaces_block(create=True)
        assert interfaces is not None and interfaces.children is not None
        node = JunosNode(target_parent, [])
        interfaces.children.append(node)
    assert node.children is not None
    node.children = [
        child
        for original in node.children
        for child in (
            [original]
            if not original.effective
            or (
                original.is_block
                and document._base_header(original.header).startswith("unit ")
            )
            else _strip_vlan_termination(document, [original])
        )
    ]
    node.children = [
        child
        for child in node.children
        if not child.effective
        or not re.match(
            r"^encapsulation\s+(?:ethernet-bridge|vlan-bridge)\b",
            document._base_header(child.header),
        )
    ]
    required = ["flexible-vlan-tagging;", "encapsulation flexible-ethernet-services;"]
    existing_headers = {
        document._base_header(child.header)
        for child in node.children
        if child.effective
    }
    for statement in reversed(required):
        if statement not in existing_headers:
            node.children.insert(0, JunosNode(statement))
    return node


def map_uni(
    document: JunosDocument,
    source: str,
    target_parent: str,
    vlan: int,
    inner_vlan: int,
) -> str:
    """复制源业务到目标父口，并生成以运输 VLAN 编号的 QinQ unit。

    指定 unit 时精确复制该 unit；源父口仅含一个 unit 时可自动选中，无 unit 时
    则把父口业务包装为 unit。复制内容会移除旧 VLAN 终结语义，再前置新的
    ``vlan-tags outer ... inner ...``，以保留地址、family 等业务而避免标签冲突。
    源节点不会在此删除，因为同一父口的其他 unit 可能仍待迁移，统一清理由上层
    在全部映射完成后执行。
    """
    source_parent = interface_parent(canonical_junos_interface(source))
    unit_number = interface_unit(source)
    source_node = find_interface_node(document, source_parent)
    target_node = _ensure_target_parent(document, target_parent)
    assert target_node.children is not None
    unit_node = None
    if source_node:
        units = document._unit_nodes(source_node)
        if unit_number is not None:
            unit_node = next(
                (
                    item
                    for item in units
                    if document._unit_number(item) == unit_number
                ),
                None,
            )
        elif len(units) == 1:
            unit_node = units[0]
        elif not units:
            payload = [child.clone() for child in (source_node.children or [])]
            unit_node = JunosNode("unit 0", payload)
    if unit_node is None:
        unit_node = JunosNode("unit 0", [])
    else:
        unit_node = unit_node.clone()
    unit_node.header = f"unit {vlan}"
    assert unit_node.children is not None
    unit_node.children = _strip_vlan_termination(document, unit_node.children)
    unit_node.children.insert(
        0,
        JunosNode(f"vlan-tags outer {vlan} inner {inner_vlan};"),
    )
    target_node.children.append(unit_node)
    return f"{target_parent}.{vlan}"


def ensure_parent_interface(document: JunosDocument, name: str) -> None:
    """确保目标 Junos 父接口存在并可承载灵活 QinQ 子接口。

    该入口复用目标父口准备逻辑，因为 UNI 迁移即使只生成 unit，也需要父口具备
    与双层标签匹配的接口级封装配置。
    """
    _ensure_target_parent(document, name)
