"""Cisco IOS XR 接口分析与修改函数。"""

from __future__ import annotations

import copy
import re

from ..common.errors import require_invariant
from .document import (
    CiscoDocument,
    CiscoNode,
    strip_matching_nodes,
)
from ..common.interface import (
    InterfaceKind,
    InterfaceSpec,
    interface_parent,
    interface_unit,
    referenced_interface_names,
)


def interface_nodes(document: CiscoDocument) -> list[CiscoNode]:
    """返回文档中仍有效且已识别出接口名的 IOS XR 配置块。

    后续接口分析和修改统一从这里取数，以免把已逻辑删除的块或普通顶层配置
    误当成接口处理。
    """
    parser = document._parser
    return [
        block
        for block in document.root.children
        if block.active and parser.interface_name(block.header)
    ]


def interface_specs(document: CiscoDocument) -> list[InterfaceSpec]:
    """提取接口名、父子关系、类型及 dot1q/QinQ 标签等规划信息。

    BVI 的编号只是接口标识，不能直接视为业务 VLAN；因此网关的
    ``inner_vlan`` 只从唯一、无歧义的 bridge-domain 绑定中推导，避免业务分析
    把错误的 VLAN 当作内层标签。
    """
    parser = document._parser
    result: list[InterfaceSpec] = []
    for block in interface_nodes(document):
        name = parser.interface_name(block.header)
        require_invariant(
            name is not None,
            "Cisco 接口节点集合中的节点必须包含接口名",
        )
        vlan = None
        inner_vlan = None
        for node in block.walk():
            match = re.match(
                r"encapsulation\s+dot1q\s+(\d+)"
                r"(?:\s+second-dot1q\s+(\d+))?",
                node.header,
                re.IGNORECASE,
            )
            if match:
                vlan = int(match.group(1))
                inner_vlan = int(match.group(2)) if match.group(2) else None
                break
        result.append(
            InterfaceSpec(
                name=name,
                parent=interface_parent(name),
                unit=interface_unit(name),
                vlan=vlan,
                kind=document.interface_kind(name),
                inner_vlan=inner_vlan,
            )
        )

    # BVI 编号只是接口标识，不能当作 VLAN。仅当同一 bridge-domain 的
    # attachment circuit 给出唯一明确的标签时，才把它作为网关的内层业务 VLAN。
    gateway_hints = (
        _gateway_inner_vlan_hints(document, result)
        if any(spec.kind == InterfaceKind.GATEWAY for spec in result)
        else {}
    )
    for spec in result:
        if spec.kind == InterfaceKind.GATEWAY:
            spec.inner_vlan = gateway_hints.get(spec.name)
    return result


def _node_identity(node: CiscoNode) -> tuple[object, ...]:
    """返回节点及其语义子树的可哈希结构标识。

    接口块碰撞合并时需要以完整子树而非单行文本判重；忽略格式节点可避免仅因 ``!``
    位置不同而重复保留同一条业务配置。
    """
    return (
        node.header,
        node.is_block,
        tuple(
            _node_identity(child)
            for child in node.children
            if not child.is_formatting
        ),
    )


def _bridge_domain_bindings(
    document: CiscoDocument,
) -> list[tuple[set[str], set[str]]]:
    """返回每个有效 bridge-domain 的接入接口与三层网关接口集合。

    只解析活动的 ``l2vpn`` 块，并规范化接口名；这为业务口识别和 BVI VLAN
    推导提供显式配置关系，而不是依赖接口编号等不可靠的约定。
    """
    result: list[tuple[set[str], set[str]]] = []
    for block in document.root.children:
        if not block.active or not re.match(
            r"^l2vpn\b",
            block.header.strip(),
            re.IGNORECASE,
        ):
            continue
        for node in block.children:
            if node.is_formatting:
                continue
            stack = [node]
            while stack:
                current = stack.pop()
                stack.extend(current.children)
                if not re.match(r"^bridge-domain\b", current.header, re.IGNORECASE):
                    continue
                attachments: set[str] = set()
                gateways: set[str] = set()
                for child in current.walk():
                    gateway_match = re.match(
                        r"^routed\s+interface\s+(.+)$",
                        child.header,
                        re.IGNORECASE,
                    )
                    if gateway_match:
                        gateways.add(document.resolve_interface(gateway_match.group(1)))
                        continue
                    interface_match = re.match(
                        r"^interface\s+(.+)$",
                        child.header,
                        re.IGNORECASE,
                    )
                    if interface_match:
                        attachments.add(document.resolve_interface(interface_match.group(1)))
                result.append((attachments, gateways))
    return result


def _gateway_inner_vlan_hints(
    document: CiscoDocument,
    specs: list[InterfaceSpec],
) -> dict[str, int]:
    """从 bridge-domain 关系推导确定无歧义的 BVI 内层业务 VLAN。

    只有一个广播域内的物理口或 Bundle 口指向同一个业务标签，且同一 BVI 在
    所有关联广播域中也只得到一个候选值时才返回提示；歧义映射会被丢弃，以免
    推导错误的业务关联。
    """
    spec_by_name = {spec.name: spec for spec in specs}
    hints: dict[str, set[int]] = {}
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    for attachments, gateways in _bridge_domain_bindings(document):
        service_vlans: set[int] = set()
        for attachment in attachments:
            attachment_spec = spec_by_name.get(attachment)
            if attachment_spec is None or attachment_spec.kind not in mappable_kinds:
                continue
            service_vlan = attachment_spec.inner_vlan or attachment_spec.vlan
            if service_vlan is not None:
                service_vlans.add(service_vlan)
        if len(service_vlans) != 1:
            continue
        service_vlan = next(iter(service_vlans))
        for gateway in gateways:
            hints.setdefault(gateway, set()).add(service_vlan)
    return {
        gateway: next(iter(vlans))
        for gateway, vlans in hints.items()
        if len(vlans) == 1
    }


def interface_kind(document: CiscoDocument, name: str) -> InterfaceKind:
    """按 IOS XR 命名规则返回规范化接口的业务类别。

    分类本身只依赖接口名，复用当前文档的私有解析缓存，供拓扑校验及
    物理口、聚合口、网关口分流使用。
    """
    return document.interface_kind(name)


def bundle_members(document: CiscoDocument) -> dict[str, str]:
    """返回物理父接口到其 ``Bundle-Ether`` 聚合接口的映射。

    仅采集物理父口上的 ``bundle id``，不把子接口当成独立成员，供迁移流程在
    展平聚合配置时找到并清理真实成员口。
    """
    parser = document._parser
    result: dict[str, str] = {}
    for block in interface_nodes(document):
        name = parser.interface_name(block.header)
        require_invariant(
            name is not None,
            "Cisco 接口节点集合中的节点必须包含接口名",
        )
        if document.interface_kind(name) != InterfaceKind.PHYSICAL or interface_unit(name) is not None:
            continue
        for node in block.walk():
            match = re.match(r"bundle\s+id\s+(\d+)\b", node.header, re.IGNORECASE)
            if match:
                result[name] = f"Bundle-Ether{match.group(1)}"
                break
    return result


def resolve_interface(document: CiscoDocument, value: str) -> str:
    """把外部输入的 IOS XR 接口别名转换为解析器使用的规范名称。

    复用当前文档的私有解析缓存；名称统一后，拓扑数据才能与配置中的接口块
    可靠匹配。
    """
    return document.resolve_interface(value)


def logical_names_under(document: CiscoDocument, parent: str) -> list[str]:
    """列出指定父接口自身及其已配置子接口，并保证父接口排在前面。

    返回完整接口树名称是为了在 NNI 克隆、重命名及审计映射时保留每个子接口
    的后缀，而集合去重可避免重复配置块产生重复记录。
    """
    parent = document.resolve_interface(parent)
    return sorted(
        {spec.name for spec in document.interface_specs() if spec.parent == parent},
        key=lambda value: (interface_unit(value) is not None, value),
    )


def business_interface_names(document: CiscoDocument) -> set[str]:
    """识别实际承载三层、二层或广播域网关业务的 IOS XR 接口。

    物理口和 Bundle 口若自身包含 IP、L2VPN 等业务命令，或被其他有效配置块
    显式引用，就视为活跃；父口的外部引用还会激活其整棵接口树。BVI 仅在其
    bridge-domain 含有已激活接入电路时加入结果，防止仅因 BVI 自身有地址或
    编号而激活与当前业务无关的网关。
    """
    parser = document._parser
    specs = document.interface_specs()
    known = {spec.name for spec in specs}
    kinds = {spec.name: spec.kind for spec in specs}
    mappable_kinds = {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
    active: set[str] = set()
    direct = re.compile(
        r"^(?:ipv4\s+address|ipv6\s+address|xconnect\b|l2transport\b|"
        r"bridge-domain\b|l2vpn\b|ethernet-services\b)",
        re.IGNORECASE,
    )
    for block in interface_nodes(document):
        name = parser.interface_name(block.header)
        if (
            name
            and kinds.get(name) in mappable_kinds
            and (
                parser.l2transport(block.header)
                or any(direct.match(node.header) for node in block.walk())
            )
        ):
            active.add(name)

    external_text = "\n".join(
        text
        for block in document.root.children
        if block.active and not parser.interface_name(block.header)
        for text in [block.header, *(node.header for node in block.walk())]
    )
    referenced = referenced_interface_names(
        external_text, known | {spec.parent for spec in specs}
    )
    for name in known & referenced:
        if kinds.get(name) in mappable_kinds:
            active.add(name)
    specs_by_parent: dict[str, list[InterfaceSpec]] = {}
    for spec in specs:
        specs_by_parent.setdefault(spec.parent, []).append(spec)
    for parent, parent_specs in specs_by_parent.items():
        if parent in referenced and parent_specs[0].kind in mappable_kinds:
            active.update(spec.name for spec in parent_specs)

    for attachments, gateways in _bridge_domain_bindings(document):
        if attachments & active:
            active.update(gateway for gateway in gateways if gateway in known)
    return active


def find_interface_node(document: CiscoDocument, name: str) -> CiscoNode | None:
    """按规范化名称查找第一个仍有效的 IOS XR 接口配置块。

    统一规范化调用方输入可兼容接口别名；忽略非活动块则确保后续修改不会落到
    已被逻辑删除的旧配置上。
    """
    parser = document._parser
    canonical = document.resolve_interface(name)
    return next(
        (
            block
            for block in interface_nodes(document)
            if parser.interface_name(block.header) == canonical
        ),
        None,
    )


def remove_interface(
    document: CiscoDocument,
    name: str,
    include_children: bool = False,
) -> None:
    """逻辑停用指定接口，并可同时停用其所有点号子接口。

    方法通过设置 ``active=False`` 而非直接删除块，保留原始文档顺序和编辑上下文；
    ``include_children`` 用于父口迁移结束后一次清理整棵接口树。
    """
    parser = document._parser
    canonical = document.resolve_interface(name)
    for block in interface_nodes(document):
        current = parser.interface_name(block.header)
        if current == canonical or (
            include_children and current and current.startswith(canonical + ".")
        ):
            block.active = False


def rename_interface_tree(
    document: CiscoDocument,
    source: str,
    target: str,
    strip_bundle: bool = False,
) -> None:
    """重命名源接口及其全部子接口，并保留各自的子接口后缀。

    ``strip_bundle`` 会移除聚合成员相关命令，因为迁移到普通目标口后这些属性已
    不再成立；若新名称已存在，则合并重复接口块，避免渲染出相互冲突的定义。
    """
    parser = document._parser
    source = document.resolve_interface(source)
    target = document.resolve_interface(target)
    for block in list(interface_nodes(document)):
        current = parser.interface_name(block.header)
        if current != source and not (current and current.startswith(source + ".")):
            continue
        suffix = current[len(source) :] if current else ""
        new_name = target + suffix
        block.header = f"interface {new_name}" + (
            " l2transport" if parser.l2transport(block.header) else ""
        )
        if strip_bundle:
            block.children, _ = strip_matching_nodes(
                block.children,
                re.compile(r"^(?:bundle\b|lacp\b|aggregated-)", re.IGNORECASE),
            )
        _merge_duplicate_interface(document, block)


def clone_interface_tree(
    document: CiscoDocument,
    source: str,
    target: str,
    strip_bundle: bool = False,
) -> None:
    """深拷贝源接口树到目标名称，供一个逻辑口拆分到多个目标口。

    克隆会保留子接口后缀及 ``l2transport`` 模式，并可剥离不应带到新物理口的
    聚合属性。源不存在时仍创建一个启用的空目标口，使后续迁移步骤有稳定落点；
    目标重名时则合并配置而非产生重复接口块。
    """
    parser = document._parser
    source = document.resolve_interface(source)
    target = document.resolve_interface(target)
    originals: list[CiscoNode] = []
    for block in interface_nodes(document):
        name = parser.interface_name(block.header)
        if name == source or (name and name.startswith(source + ".")):
            originals.append(block)
    if not originals:
        _insert_top_level_before_end(document, [
            CiscoNode(header=f"interface {target}", children=[CiscoNode("no shutdown")]),
            CiscoNode(header="!"),
        ])
        return
    insert_at = max(document.root.children.index(block) for block in originals) + 1
    clones: list[CiscoNode] = []
    for original in originals:
        clone = copy.deepcopy(original)
        current = parser.interface_name(original.header) or source
        new_name = target + current[len(source) :]
        clone.header = f"interface {new_name}" + (
            " l2transport" if parser.l2transport(original.header) else ""
        )
        if strip_bundle:
            clone.children, _ = strip_matching_nodes(
                clone.children,
                re.compile(r"^(?:bundle\b|lacp\b|aggregated-)", re.IGNORECASE),
            )
        clones.extend([clone, CiscoNode(header="!")])
    document.root.children[insert_at:insert_at] = clones
    for clone in clones:
        if parser.interface_name(clone.header):
            _merge_duplicate_interface(document, clone)


def _merge_duplicate_interface(
    document: CiscoDocument,
    preferred: CiscoNode,
) -> None:
    """把同名接口块的非重复配置子树合并到首选块，并停用其余块。

    接口改名或克隆可能与已有目标接口碰撞；集中合并既保留双方配置，也保证最终
    输出只有一个活动定义。子树按原文档中各块的出现顺序收集。
    """
    parser = document._parser
    name = parser.interface_name(preferred.header)
    duplicates = [
        block for block in interface_nodes(document) if parser.interface_name(block.header) == name
    ]
    if len(duplicates) < 2:
        return
    merged: list[CiscoNode] = []
    seen: set[tuple[object, ...]] = set()
    for block in duplicates:
        for node in block.children:
            if node.is_formatting:
                continue
            identity = _node_identity(node)
            if identity not in seen:
                merged.append(node.clone())
                seen.add(identity)
        if block is not preferred:
            block.active = False
    preferred.children = merged


def _insert_top_level_before_end(document: CiscoDocument, nodes: list[CiscoNode]) -> None:
    """追加活动配置时保持 end 为最后一条配置终止命令。"""
    index = next(
        (index for index, node in enumerate(document.root.children)
         if node.active and node.header.strip().lower() == "end"),
        len(document.root.children),
    )
    document.root.children[index:index] = nodes


def ensure_parent_interface(document: CiscoDocument, name: str) -> None:
    """确保目标 IOS XR 父接口存在，不存在时创建并配置 ``no shutdown``。

    NNI 业务可能只包含子接口，但设备配置仍需要显式、已启用的承载父口；已有
    接口保持原样，避免覆盖用户配置。
    """
    canonical = document.resolve_interface(name)
    if find_interface_node(document, canonical):
        return
    _insert_top_level_before_end(document, [
        CiscoNode(header=f"interface {canonical}", children=[CiscoNode("no shutdown")]),
        CiscoNode(header="!"),
    ])
