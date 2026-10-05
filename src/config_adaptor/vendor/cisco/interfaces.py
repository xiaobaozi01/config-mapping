"""Cisco IOS XR 接口分析与修改函数。"""

from __future__ import annotations

import copy
import re

from ...errors import require_invariant
from ...parsers.cisco_iosxr import (
    CiscoDocument,
    CiscoNode,
    _cisco_interface_kind,
    canonical_cisco_interface,
    strip_matching_nodes,
)
from ...parsers.common import InterfaceKind, InterfaceSpec, interface_parent, interface_unit


def interface_nodes(document: CiscoDocument) -> list[CiscoNode]:
    """返回文档中仍有效且已识别出接口名的 IOS XR 配置块。

    后续接口分析和修改统一从这里取数，以免把已逻辑删除的块或普通顶层配置
    误当成接口处理。
    """
    return [
        block
        for block in document.root.children
        if block.active and block.interface_name
    ]


def interface_specs(document: CiscoDocument) -> list[InterfaceSpec]:
    """提取接口名、父子关系、类型及 dot1q/QinQ 标签等规划信息。

    BVI 的编号只是接口标识，不能直接视为业务 VLAN；因此网关的
    ``inner_vlan`` 只从唯一、无歧义的 bridge-domain 绑定中推导，避免 UNI
    迁移时把错误的 VLAN 当作需要保留的内层标签。
    """
    result: list[InterfaceSpec] = []
    for block in interface_nodes(document):
        name = block.interface_name
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
                kind=_cisco_interface_kind(name),
                inner_vlan=inner_vlan,
            )
        )

    # BVI 编号只是接口标识，不能当作 VLAN。仅当同一 bridge-domain 的
    # attachment circuit 给出唯一明确的标签时，才把它作为网关的内层业务 VLAN。
    gateway_hints = _gateway_inner_vlan_hints(document, result)
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
                        gateways.add(canonical_cisco_interface(gateway_match.group(1)))
                        continue
                    interface_match = re.match(
                        r"^interface\s+(.+)$",
                        child.header,
                        re.IGNORECASE,
                    )
                    if interface_match:
                        attachments.add(canonical_cisco_interface(interface_match.group(1)))
                result.append((attachments, gateways))
    return result


def _gateway_inner_vlan_hints(
    document: CiscoDocument,
    specs: list[InterfaceSpec],
) -> dict[str, int]:
    """从 bridge-domain 关系推导确定无歧义的 BVI 内层业务 VLAN。

    只有一个广播域内的物理口或 Bundle 口指向同一个业务标签，且同一 BVI 在
    所有关联广播域中也只得到一个候选值时才返回提示；歧义映射会被丢弃，以免
    生成错误的 QinQ 配置。
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

    ``document`` 保留在签名中是为了实现统一的厂商接口协议；分类本身只依赖
    接口名，供拓扑校验及物理口、聚合口、网关口分流使用。
    """
    return _cisco_interface_kind(canonical_cisco_interface(name))


def bundle_members(document: CiscoDocument) -> dict[str, str]:
    """返回物理父接口到其 ``Bundle-Ether`` 聚合接口的映射。

    仅采集物理父口上的 ``bundle id``，不把子接口当成独立成员，供迁移流程在
    展平聚合配置时找到并清理真实成员口。
    """
    result: dict[str, str] = {}
    for block in interface_nodes(document):
        name = block.interface_name
        require_invariant(
            name is not None,
            "Cisco 接口节点集合中的节点必须包含接口名",
        )
        if _cisco_interface_kind(name) != InterfaceKind.PHYSICAL or interface_unit(name) is not None:
            continue
        for node in block.walk():
            match = re.match(r"bundle\s+id\s+(\d+)\b", node.header, re.IGNORECASE)
            if match:
                result[name] = f"Bundle-Ether{match.group(1)}"
                break
    return result


def resolve_interface(document: CiscoDocument, value: str) -> str:
    """把外部输入的 IOS XR 接口别名转换为解析器使用的规范名称。

    ``document`` 仅用于与其他厂商实现保持统一调用形式；名称统一后，拓扑数据
    才能与配置中的接口块可靠匹配。
    """
    return canonical_cisco_interface(value)


def logical_names_under(document: CiscoDocument, parent: str) -> list[str]:
    """列出指定父接口自身及其已配置子接口，并保证父接口排在前面。

    返回完整接口树名称是为了在 NNI 克隆、重命名及审计映射时保留每个子接口
    的后缀，而集合去重可避免重复配置块产生重复记录。
    """
    parent = canonical_cisco_interface(parent)
    return sorted(
        {spec.name for spec in interface_specs(document) if spec.parent == parent},
        key=lambda value: (interface_unit(value) is not None, value),
    )


def business_interface_names(document: CiscoDocument) -> set[str]:
    """识别实际承载三层、二层或广播域网关业务的 IOS XR 接口。

    物理口和 Bundle 口若自身包含 IP、L2VPN 等业务命令，或被其他有效配置块
    显式引用，就视为活跃；父口的外部引用还会激活其整棵接口树。BVI 仅在其
    bridge-domain 含有已激活接入电路时加入结果，防止仅因 BVI 自身有地址或
    编号而迁移与当前 UNI 无关的网关。
    """
    specs = interface_specs(document)
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
        name = block.interface_name
        if (
            name
            and kinds.get(name) in mappable_kinds
            and (
                block.l2transport
                or any(direct.match(node.header) for node in block.walk())
            )
        ):
            active.add(name)

    external_text = "\n".join(
        text
        for block in document.root.children
        if block.active and not block.interface_name
        for text in [block.header, *(node.header for node in block.walk())]
    )
    for name in known:
        if kinds.get(name) in mappable_kinds and re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_.-])",
            external_text,
        ):
            active.add(name)
    for parent in {spec.parent for spec in specs}:
        parent_specs = [spec for spec in specs if spec.parent == parent]
        if parent_specs and parent_specs[0].kind in mappable_kinds and re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(parent)}(?![A-Za-z0-9_.-])",
            external_text,
        ):
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
    canonical = canonical_cisco_interface(name)
    return next(
        (
            block
            for block in interface_nodes(document)
            if block.interface_name == canonical
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
    canonical = canonical_cisco_interface(name)
    for block in interface_nodes(document):
        current = block.interface_name
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
    source = canonical_cisco_interface(source)
    target = canonical_cisco_interface(target)
    for block in list(interface_nodes(document)):
        current = block.interface_name
        if current != source and not (current and current.startswith(source + ".")):
            continue
        suffix = current[len(source) :] if current else ""
        new_name = target + suffix
        block.header = f"interface {new_name}" + (
            " l2transport" if block.l2transport else ""
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
    source = canonical_cisco_interface(source)
    target = canonical_cisco_interface(target)
    originals = [
        block
        for block in interface_nodes(document)
        if block.interface_name == source
        or (block.interface_name and block.interface_name.startswith(source + "."))
    ]
    if not originals:
        document.root.children.append(
            CiscoNode(
                header=f"interface {target}",
                children=[CiscoNode("no shutdown")],
            )
        )
        document.root.children.append(CiscoNode(header="!"))
        return
    insert_at = max(document.root.children.index(block) for block in originals) + 1
    clones: list[CiscoNode] = []
    for original in originals:
        clone = copy.deepcopy(original)
        current = original.interface_name or source
        new_name = target + current[len(source) :]
        clone.header = f"interface {new_name}" + (
            " l2transport" if original.l2transport else ""
        )
        if strip_bundle:
            clone.children, _ = strip_matching_nodes(
                clone.children,
                re.compile(r"^(?:bundle\b|lacp\b|aggregated-)", re.IGNORECASE),
            )
        clones.extend([clone, CiscoNode(header="!")])
    document.root.children[insert_at:insert_at] = clones
    for clone in clones:
        if clone.interface_name:
            _merge_duplicate_interface(document, clone)


def _merge_duplicate_interface(
    document: CiscoDocument,
    preferred: CiscoNode,
) -> None:
    """把同名接口块的非重复配置子树合并到首选块，并停用其余块。

    接口改名或克隆可能与已有目标接口碰撞；集中合并既保留双方配置，也保证最终
    输出只有一个活动定义。子树按原文档中各块的出现顺序收集。
    """
    name = preferred.interface_name
    duplicates = [
        block for block in interface_nodes(document) if block.interface_name == name
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


def map_uni(
    document: CiscoDocument,
    source: str,
    target_parent: str,
    vlan: int,
    inner_vlan: int,
) -> str:
    """把一个源业务接口改写成目标 UNI 父口下的 QinQ 子接口。

    外层 ``vlan`` 同时作为目标子接口号，``inner_vlan`` 保留原业务标签。方法会
    删除旧封装、rewrite 和聚合命令，再把新的双层标签放在 description 之后，
    以避免新旧终结语义冲突；目标重名时合并配置。找不到源块时仅返回预期目标名，
    让上层流程仍可记录确定性的映射结果。
    """
    source = canonical_cisco_interface(source)
    target = f"{canonical_cisco_interface(target_parent)}.{vlan}"
    block = find_interface_node(document, source)
    if not block:
        return target
    block.header = f"interface {target}" + (
        " l2transport" if block.l2transport else ""
    )
    filtered, _ = strip_matching_nodes(
        block.children,
        re.compile(r"^(?:encapsulation\b|rewrite\b|bundle\b|lacp\b)", re.IGNORECASE),
    )
    insert_at = (
        1
        if filtered and re.match(r"description\b", filtered[0].header, re.IGNORECASE)
        else 0
    )
    filtered.insert(
        insert_at,
        CiscoNode(f"encapsulation dot1q {vlan} second-dot1q {inner_vlan}"),
    )
    block.children = filtered
    _merge_duplicate_interface(document, block)
    return target


def ensure_parent_interface(document: CiscoDocument, name: str) -> None:
    """确保目标 IOS XR 父接口存在，不存在时创建并配置 ``no shutdown``。

    UNI 业务通常只生成子接口，但设备配置仍需要显式、已启用的承载父口；已有
    接口保持原样，避免覆盖用户配置。
    """
    canonical = canonical_cisco_interface(name)
    if find_interface_node(document, canonical):
        return
    document.root.children.append(
        CiscoNode(
            header=f"interface {canonical}",
            children=[CiscoNode("no shutdown")],
        )
    )
    document.root.children.append(CiscoNode(header="!"))
