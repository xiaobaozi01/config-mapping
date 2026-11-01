"""Juniper Junos 接口分析与修改函数。"""

from __future__ import annotations

import re

from impl.common.errors import require_invariant
from impl.common.interface import (
    InterfaceKind,
    InterfaceSpec,
    interface_parent,
    interface_unit,
    referenced_interface_names,
)

from .document import (
    JunosDocument,
    JunosNode,
)

MAX_VLAN_ID = 4094


def interface_specs(document: JunosDocument) -> list[InterfaceSpec]:
    """提取 Junos 接口或 unit 的父子关系、类型及 VLAN 规划信息。

    有 unit 时每个 unit 是独立业务对象，无 unit 时才把父口本身作为对象。IRB
    unit 若没有显式 VLAN 且编号合法，则按 Junos 常见的一一对应方式把 unit 号
    作为 VLAN，供广播域网关关联使用。
    """
    result: list[InterfaceSpec] = []
    for node in document._interface_nodes():
        parent = document._interface_name(node)
        units = document._unit_nodes(node)
        if not units:
            result.append(
                InterfaceSpec(
                    parent, parent, None, None, document.interface_kind(parent)
                )
            )
            continue
        for unit in units:
            number = document._unit_number(unit)
            vlan = document._find_vlan(unit)
            if (
                vlan is None
                and parent.lower() == "irb"
                and number.isdigit()
                and 1 <= int(number) <= MAX_VLAN_ID
            ):
                vlan = int(number)
            result.append(
                InterfaceSpec(
                    name=f"{parent}.{number}",
                    parent=parent,
                    unit=number,
                    vlan=vlan,
                    kind=document.interface_kind(parent),
                    inner_vlan=document._find_inner_vlan(unit),
                )
            )
    return result


def interface_kind(document: JunosDocument, name: str) -> InterfaceKind:
    """按 Junos 命名规则返回规范化接口的业务类别。

    分类本身只依赖接口名，复用当前文档的私有解析缓存，供拓扑校验及
    物理口、ae 聚合口、IRB 网关口分流使用。
    """
    return document.interface_kind(name)


def bundle_members(document: JunosDocument) -> dict[str, str]:
    """返回物理接口到其 ``ae`` 聚合接口的映射。

    方法只检查物理接口节点中的 ``802.3ad`` 绑定，供迁移流程定位并清理真实
    聚合成员；其他接口类型即使包含相似文本也不会被误判。
    """
    result: dict[str, str] = {}
    for node in document._interface_nodes():
        parent = document._interface_name(node)
        if document.interface_kind(parent) != InterfaceKind.PHYSICAL:
            continue
        rendered = document._render_effective_node(node, 0)
        match = re.search(r"\b802\.3ad\s+(ae\d+)\s*;", rendered)
        if match:
            result[parent] = match.group(1)
    return result


def resolve_interface(document: JunosDocument, value: str) -> str:
    """把外部输入的 Junos 接口名转换为解析器使用的规范形式。

    复用当前文档的私有解析缓存；名称统一后，拓扑数据才能与配置树中的接口
    节点可靠匹配。
    """
    return document.resolve_interface(value)


def logical_names_under(document: JunosDocument, parent: str) -> list[str]:
    """列出指定父接口对应的全部已配置业务名称，并把父口排在 unit 前。

    对含 unit 的 Junos 接口，接口规格只包含各 unit；无 unit 时则返回父口本身。
    集合去重和稳定排序便于 NNI 克隆以及生成可重复的审计映射。
    """
    parent = document.resolve_interface(parent)
    return sorted(
        {
            spec.name
            for spec in document.interface_specs()
            if spec.parent == parent
        },
        key=lambda value: (interface_unit(value) is not None, value),
    )


def business_interface_names(document: JunosDocument) -> set[str]:
    """识别实际承载三层、二层或活跃广播域网关业务的 Junos 接口。

    物理口和 ae 口若自身含业务 family、CCC、桥接或 VLAN 映射语句，或被接口树
    之外的有效配置显式引用，就视为活跃；父口引用会激活其所有 unit。随后从活跃
    二层接口收集 VLAN ID/名称，并沿 bridge-domain 或 vlans 配置激活对应 IRB。
    这样可识别真正关联的网关，避免仅因 IRB 配有地址就误判为活跃业务。
    """
    specs = document.interface_specs()
    known = {spec.name for spec in specs}
    kinds = {spec.name: spec.kind for spec in specs}
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    active = _collect_interface_business(document, kinds, mappable_kinds)
    active.update(_collect_reference_business(document, specs, known, kinds))

    active_vlans, active_vlan_names = _collect_active_vlans(
        document, active, specs
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


def _collect_interface_business(
    document: JunosDocument,
    kinds: dict[str, InterfaceKind],
    mappable_kinds: set[InterfaceKind],
) -> set[str]:
    """根据接口自身的有效业务语句识别活跃接口。

    父口业务需要传播到 unit，而独立 unit 只按自身内容判断，避免无关逻辑单元被迁移。
    """
    parser = document._parser
    active: set[str] = set()
    business_pattern = re.compile(
        r"\b(?:family\s+(?:inet6?|ccc|bridge|ethernet-switching)|"
        r"encapsulation\s+(?:ethernet-ccc|vlan-ccc)|"
        r"input-vlan-map|output-vlan-map)\b"
    )
    for node in document._interface_nodes():
        parent = document._interface_name(node)
        units = document._unit_nodes(node)
        if not units:
            rendered = document._render_effective_node(node, 0)
            if document.interface_kind(
                parent
            ) in mappable_kinds and business_pattern.search(rendered):
                active.add(parent)
            continue
        parent_business = any(
            parser.is_effective(child)
            and child not in units
            and business_pattern.search(
                document._render_effective_node(child, 0)
            )
            for child in node.children or []
        )
        for unit in units:
            rendered = document._render_effective_node(unit, 0)
            name = f"{parent}.{document._unit_number(unit)}"
            if kinds.get(name) in mappable_kinds and (
                parent_business or business_pattern.search(rendered)
            ):
                active.add(name)
    return active


def _collect_reference_business(
    document: JunosDocument,
    specs: list[InterfaceSpec],
    known: set[str],
    kinds: dict[str, InterfaceKind],
) -> set[str]:
    """找出被接口树之外的有效配置显式引用的业务接口。

    路由、VPN 等全局配置可能没有出现在接口块中，扫描这些引用才能保留仍被业务依赖的口。
    """
    parser = document._parser
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    external = "\n".join(
        document._render_effective_node(node, 0)
        for node in (document.root.children or [])
        if parser.is_effective(node)
        and parser.base_header(node.header) not in {"interfaces", "groups"}
    )
    referenced = referenced_interface_names(
        external, known | {spec.parent for spec in specs}
    )
    active = {
        name for name in known & referenced if kinds.get(name) in mappable_kinds
    }
    specs_by_parent: dict[str, list[InterfaceSpec]] = {}
    for spec in specs:
        specs_by_parent.setdefault(spec.parent, []).append(spec)
    for parent, parent_specs in specs_by_parent.items():
        if parent in referenced and parent_specs[0].kind in mappable_kinds:
            active.update(spec.name for spec in parent_specs)
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
            if 1 <= number <= MAX_VLAN_ID:
                result.add(number)
            continue
        lower, upper = int(start), int(end)
        if 1 <= lower <= upper <= MAX_VLAN_ID:
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
    parser = document._parser
    if node.children is None:
        return
    base = parser.base_header(node.header).rstrip(";")
    if base in {"bridge-domains", "vlans"}:
        for domain in node.children:
            if not parser.is_effective(domain) or domain.children is None:
                continue
            interfaces, gateways, vlan_ids, domain_name = _domain_gateways(
                document, domain
            )
            if (
                interfaces & active
                or vlan_ids & active_vlans
                or domain_name in active_vlan_names
            ):
                active.update(
                    gateway for gateway in gateways if gateway in known
                )
    for child in node.children:
        if parser.is_effective(child):
            _activate_domain_gateways(
                document,
                child,
                active,
                active_vlans,
                active_vlan_names,
                known,
            )


def _domain_gateways(
    document: JunosDocument,
    domain: JunosNode,
) -> tuple[set[str], set[str], set[int], str]:
    """提取广播域的接入接口、网关、VLAN ID 和域名。

    从渲染文本中一次收集四类信息，使后续激活判断基于同一份规范化配置视图。
    """
    parser = document._parser
    rendered = document._render_effective_node(domain, 0)
    interfaces = {
        document.resolve_interface(value)
        for value in re.findall(
            r"(?<![-\w])interface\s+([^;\s]+)\s*;",
            rendered,
        )
    }
    gateways = {
        document.resolve_interface(value)
        for value in re.findall(
            r"\b(?:routing-interface|l3-interface)\s+([^;\s]+)\s*;",
            rendered,
        )
    }
    vlan_ids = {
        int(value)
        for value in re.findall(r"\bvlan-id\s+(\d+)\s*;", rendered)
        if 1 <= int(value) <= MAX_VLAN_ID
    }
    domain_name = parser.base_header(domain.header).split()[0].rstrip(";")
    return interfaces, gateways, vlan_ids, domain_name


def _collect_active_vlans(
    document: JunosDocument,
    active: set[str],
    specs: list[InterfaceSpec],
) -> tuple[set[int], set[str]]:
    """从活跃二层接口收集业务 VLAN ID 与 VLAN 名称。

    活跃接口若带二层终结或 VLAN 映射，则贡献其自身标签；同时解析 ``vlan-id-list``
    和 ``vlan members`` 中的显式 ID/名称，供后续广播域网关激活匹配。
    """
    spec_by_name = {spec.name: spec for spec in specs}
    active_vlans: set[int] = set()
    active_vlan_names: set[str] = set()
    parents, units = _interface_index(document)
    for name in list(active):
        spec = spec_by_name.get(name)
        if not spec or spec.kind == InterfaceKind.GATEWAY:
            continue
        parent = interface_parent(document.resolve_interface(spec.parent))
        node = parents.get(parent)
        if not node:
            continue
        unit = node if spec.unit is None else units.get((parent, spec.unit))
        if unit is None:
            continue
        rendered = document._render_effective_node(unit, 0)
        _update_active_vlans(
            rendered,
            spec.vlan,
            active_vlans,
            active_vlan_names,
        )
    return active_vlans, active_vlan_names


def _interface_index(
    document: JunosDocument,
) -> tuple[dict[str, JunosNode], dict[tuple[str, str], JunosNode]]:
    """建立父接口和逻辑单元的快速查找索引。

    一次遍历接口树可避免每个活跃单元都重复线性查找父节点，同时保持首个定义的语义。
    """
    parents: dict[str, JunosNode] = {}
    units: dict[tuple[str, str], JunosNode] = {}
    for node in document._interface_nodes():
        parent = document._interface_name(node)
        if parent in parents:
            continue
        parents[parent] = node
        for unit in document._unit_nodes(node):
            units.setdefault((parent, document._unit_number(unit)), unit)
    return parents, units


def _update_active_vlans(
    rendered: str,
    own_vlan: int | None,
    active_vlans: set[int],
    active_vlan_names: set[str],
) -> None:
    """从一个活跃接口的渲染文本提取 VLAN ID 和名称。

    只解析二层终结和 VLAN 成员语句，避免把三层接口上的普通数字误当作 VLAN 依赖。
    """
    is_l2 = bool(
        re.search(
            r"\b(?:family\s+(?:ccc|bridge|ethernet-switching)|"
            r"encapsulation\s+(?:ethernet-ccc|vlan-ccc)|"
            r"vlan-id-list|vlan\s+members|input-vlan-map|output-vlan-map)\b",
            rendered,
        )
    )
    if is_l2 and own_vlan is not None:
        active_vlans.add(own_vlan)
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


def find_interface_node(document: JunosDocument, name: str) -> JunosNode | None:
    """按规范化父接口名查找第一个有效的 Junos 接口节点。

    调用方可以传父接口或 unit 名；先去掉 unit 是因为 Junos 把 unit 存在父接口
    节点内部，而不是独立的顶层接口节点。
    """
    canonical = document.resolve_interface(interface_parent(name))
    return next(
        (
            node
            for node in document._interface_nodes()
            if document._interface_name(node) == canonical
        ),
        None,
    )


def remove_interface(
    document: JunosDocument, name: str, include_children: bool = False
) -> None:
    """逻辑停用指定接口所在的整个 Junos 父接口节点。

    Junos 的 unit 是父节点的子树，因此停用父节点会自然包含全部 unit；
    ``include_children`` 仅为兼容统一厂商协议而保留，在此实现中不会改变行为。
    使用活动标记而非物理删除可保留原始树结构和编辑上下文。
    """
    node = find_interface_node(document, name)
    if node:
        node.active = False


def _strip_bundle_options(
    document: JunosDocument,
    children: list[JunosNode],
) -> list[JunosNode]:
    """移除脱离 ae 后不再成立的聚合相关 options 块。

    aggregated/gigether/ether options 只在聚合成员接口上有效；接口迁移到普通目标口
    后这些残留块会引用不存在的聚合关系，因此在改名或克隆时统一剥离。
    """
    parser = document._parser
    return [
        child
        for child in children
        if not (
            parser.is_effective(child)
            and child.is_block
            and parser.base_header(child.header)
            in {"aggregated-ether-options", "gigether-options", "ether-options"}
        )
    ]


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
    node.header = document.resolve_interface(target)
    if strip_bundle and node.children is not None:
        node.children = _strip_bundle_options(document, node.children)
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
    require_invariant(
        interfaces is not None and interfaces.children is not None,
        "Junos interfaces 块在 create=True 后必须存在且可包含子节点",
    )
    if source_node is None:
        clone = JunosNode(document.resolve_interface(target), [])
    else:
        clone = source_node.clone()
        clone.header = document.resolve_interface(target)
        clone.active = True
        if strip_bundle and clone.children is not None:
            clone.children = _strip_bundle_options(document, clone.children)
    interfaces.children.append(clone)
    _merge_duplicate_interface(document, clone)


def _merge_duplicate_interface(
    document: JunosDocument, preferred: JunosNode
) -> None:
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
    require_invariant(
        preferred.children is not None,
        "Junos 重复接口合并的首选节点必须是块节点",
    )
    existing = {document._render_node(child, 0) for child in preferred.children}
    for node in duplicates:
        if node is preferred or node.children is None:
            continue
        for child in node.children:
            rendered = document._render_node(child, 0)
            if rendered not in existing:
                preferred.children.append(child)
                existing.add(rendered)
        node.active = False


def ensure_parent_interface(document: JunosDocument, name: str) -> None:
    """确保父接口存在而不重建其业务子树。

    迁移目标可能尚无定义，只创建父节点可让后续改名和映射成功，同时不伪造封装或 unit。
    """
    if find_interface_node(document, name) is not None:
        return
    interfaces = document._interfaces_block(create=True)
    require_invariant(
        interfaces is not None and interfaces.children is not None,
        "Junos interfaces 块必须存在且可包含子节点",
    )
    interfaces.children.append(JunosNode(name, []))
