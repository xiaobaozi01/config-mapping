"""Cisco IOS XR 接口分析与修改函数。"""

from __future__ import annotations

import re

from impl.common.config_ast.editor import format_fragment
from impl.common.errors import require_invariant
from impl.common.interface import (
    InterfaceKind,
    InterfaceSpec,
    interface_parent,
    interface_unit,
    referenced_interface_names,
)

from .document import (
    CiscoDocument,
    CiscoNode,
)
from .rewrite_helpers import append_top_level, delete_matching
from .writer import CiscoWriter

_MEMBER_BUSINESS = re.compile(
    r"^(?:ipv[46]\s+(?:address|unnumbered|enable)|vrf|l2transport|xconnect)\b",
    re.IGNORECASE,
)


def interface_nodes(document: CiscoDocument) -> list[CiscoNode]:
    """返回文档中仍有效且已识别出接口名的 IOS XR 配置块。

    后续接口分析和修改统一从这里取数，以免把已逻辑删除的块或普通顶层配置
    误当成接口处理。
    """
    parser = document._parser
    return [
        block
        for block in document.root.nodes
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
            for child in node.nodes
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
    for block in document.root.nodes:
        if not block.active or not re.match(
            r"^l2vpn\b",
            block.header.strip(),
            re.IGNORECASE,
        ):
            continue
        for node in block.nodes:
            if node.is_formatting:
                continue
            stack = [node]
            while stack:
                current = stack.pop()
                stack.extend(current.nodes)
                if not re.match(
                    r"^bridge-domain\b", current.header, re.IGNORECASE
                ):
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
                        gateways.add(
                            document.resolve_interface(gateway_match.group(1))
                        )
                        continue
                    interface_match = re.match(
                        r"^interface\s+(.+)$",
                        child.header,
                        re.IGNORECASE,
                    )
                    if interface_match:
                        attachments.add(
                            document.resolve_interface(interface_match.group(1))
                        )
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
            if (
                attachment_spec is None
                or attachment_spec.kind not in mappable_kinds
            ):
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
        if (
            document.interface_kind(name) != InterfaceKind.PHYSICAL
            or interface_unit(name) is not None
        ):
            continue
        for node in block.walk():
            match = re.match(
                r"bundle\s+id\s+(\d+)\b", node.header, re.IGNORECASE
            )
            if match:
                result[name] = f"Bundle-Ether{match.group(1)}"
                break
    return result


def interface_has_business(document: CiscoDocument, name: str) -> bool:
    """判断物理聚合成员是否承载地址、VRF、L2 业务或子接口。

    识别结果用于阻止压缩删除独立业务；普通描述、启停和 MTU 不在此范围内。
    """
    parent = interface_parent(document.resolve_interface(name))
    parser = document._parser
    for block in interface_nodes(document):
        block_name = parser.interface_name(block.header)
        if block_name is None:
            continue
        if block_name == parent:
            if block.l2transport or any(
                _MEMBER_BUSINESS.match(node.header) for node in block.walk()
            ):
                return True
        elif interface_parent(block_name) == parent:
            return True
    return False


def bundle_binding_is_ambiguous(document: CiscoDocument, name: str) -> bool:
    """判断物理成员是否声明了多个不同 Bundle 编号。

    多个绑定无法选择唯一目标关系，因此与可保留的成员业务分开处理。
    """
    parent = interface_parent(document.resolve_interface(name))
    bindings = set()
    for block in interface_nodes(document):
        if document._parser.interface_name(block.header) != parent:
            continue
        bindings.update(
            match.group(1)
            for node in block.walk()
            if (
                match := re.match(
                    r"^bundle\s+id\s+(\d+)\b",
                    node.header,
                    re.IGNORECASE,
                )
            )
        )
    return len(bindings) > 1


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
        {
            spec.name
            for spec in document.interface_specs()
            if spec.parent == parent
        },
        key=lambda value: (interface_unit(value) is not None, value),
    )


def _external_configuration_text(document):
    """提取接口定义之外的全部有效语义命令。

    协议、VRF 和 L2VPN 对接口的引用不在接口块内，合并这些文本才能识别外部依赖。
    """
    return "\n".join(
        text
        for block in document.root.nodes
        if not block.interface_name
        for text in [block.header, *(node.header for node in block.walk())]
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

    external_text = _external_configuration_text(document)
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
    document: CiscoDocument, name: str, include_children: bool = False
) -> None:
    """从文档中逻辑删除指定接口及其可选子接口。

    通过事务统一删除可保持格式分隔符和原始顺序稳定；子接口开关用于区分普通端点
    清理与完整接口树迁移。
    """
    canonical = document.resolve_interface(name)
    transaction = document.edit()
    for block in interface_nodes(document):
        current = block.interface_name
        if current == canonical or (
            include_children and current and current.startswith(canonical + ".")
        ):
            transaction.delete(block, "删除接口及其业务子树")
    transaction.apply()


def rename_interface_tree(document, source, target, strip_bundle=False):
    """把源接口及其子接口改名到目标父接口。

    在同一事务中同步修改父口和子口后缀，避免目标名中途出现半套定义；移出聚合时
    同时删除失效的 bundle 和 LACP 属性。
    """
    source, target = document.resolve_interface(
        source
    ), document.resolve_interface(target)
    transaction = document.edit()
    renamed = []
    for block in interface_nodes(document):
        current = block.interface_name
        if current != source and not (
            current and current.startswith(source + ".")
        ):
            continue
        new_name = target + current[len(source) :]
        renamed.append(new_name)
        transaction.replace_header(
            block,
            f"interface {new_name}"
            + (" l2transport" if block.l2transport else ""),
            "迁移接口名称",
        )
        if strip_bundle:
            delete_matching(
                transaction,
                block.nodes,
                re.compile(r"^(?:bundle\b|lacp\b|aggregated-)", re.I),
            )
    transaction.apply()
    for name in renamed:
        _merge_duplicate_interface(
            document, find_interface_node(document, name)
        )


def _clone_fragment(original, name, strip_bundle):
    """生成一份重命名且可选剥离聚合属性的接口片段。

    先复制完整子树再格式化文本，保证注释和未知子命令一并保留，同时不修改源接口。
    """
    candidate = CiscoDocument(CiscoWriter().write(original))
    root = candidate.root.nodes[0]
    transaction = candidate.edit()
    transaction.replace_header(
        root,
        f"interface {name}" + (" l2transport" if original.l2transport else ""),
        "克隆接口",
    )
    if strip_bundle:
        delete_matching(
            transaction,
            root.nodes,
            re.compile(r"^(?:bundle\b|lacp\b|aggregated-)", re.I),
        )
    transaction.apply()
    return candidate.source + "!\n"


def clone_interface_tree(document, source, target, strip_bundle=False):
    """把源接口树的全部片段复制到目标接口树。

    复制后追加而不是逐项改写，使源接口可继续服务其他目标；随后合并同名目标以维持
    每个接口只有一个活动定义。
    """
    source, target = document.resolve_interface(
        source
    ), document.resolve_interface(target)
    originals = [
        block
        for block in interface_nodes(document)
        if block.interface_name == source
        or block.interface_name.startswith(source + ".")
    ]
    if not originals:
        ensure_parent_interface(document, target)
        return
    fragments, names = [], []
    for original in originals:
        name = target + original.interface_name[len(source) :]
        names.append(name)
        fragments.append(_clone_fragment(original, name, strip_bundle))
    transaction = document.edit()
    transaction.insert_after(originals[-1], "".join(fragments), "克隆接口树")
    transaction.apply()
    for name in names:
        _merge_duplicate_interface(
            document, find_interface_node(document, name)
        )


def _merge_duplicate_interface(document, preferred):
    """把同名接口块的唯一子命令合并到首选块。

    改名或克隆可能产生重复定义，按语义身份去重可保留双方不同业务并删除冗余块。
    """
    if preferred is None:
        return
    name = preferred.interface_name
    duplicates = [
        block
        for block in interface_nodes(document)
        if block.interface_name == name
    ]
    if len(duplicates) < 2:
        return
    seen, fragments = set(), []
    for block in duplicates:
        for node in block.nodes:
            if node.is_formatting:
                continue
            identity = _node_identity(node)
            if identity not in seen:
                seen.add(identity)
                fragments.append(
                    format_fragment(document.writer.subtree(node), 1)
                )
    transaction = document.edit()
    transaction.replace_subtree(
        preferred,
        preferred.header + "\n" + "".join(fragments),
        "合并同名接口块",
    )
    for block in duplicates:
        if block is not preferred:
            transaction.delete(block, "移除已合并的重复接口")
    transaction.apply()


def _insert_top_level_before_end(document, text):
    """在配置终止标记前插入新的顶层接口块。

    终止命令之后的内容不属于活动配置，因此新增定义必须通过统一顶层插入器进入主体。
    """
    transaction = document.edit()
    append_top_level(transaction, document, text, "创建目标接口")
    transaction.apply()


def ensure_parent_interface(document: CiscoDocument, name: str) -> None:
    """确保目标接口的父定义存在。

    新迁移目标可能尚未出现；创建最小父口并启用它，可让后续子接口和引用写入有效路径。
    """
    canonical = document.resolve_interface(name)
    if find_interface_node(document, canonical) is None:
        _insert_top_level_before_end(
            document, f"interface {canonical}\n no shutdown\n!\n"
        )
