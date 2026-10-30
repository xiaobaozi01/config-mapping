"""按原始接口身份裁剪非 NNI 业务，并检查其活动依赖。"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable

from ..common.interface import InterfaceKind, interface_parent
from ..common.outcomes import InterfacePruningOutcome
from .document import CiscoDocument, CiscoNode, canonical_cisco_interface, _CISCO_INTERFACE_TOKEN
from .interfaces import interface_nodes


# 整个接口 token 一次规范化，避免 Gi/BE 缩写和主口/子口前缀误匹配。
_ADDRESS = re.compile(r"^(?:ipv4|ipv6)\s+address\s+(\S+)", re.I)


def interface_references(document: CiscoDocument, text: str, names: set[str]) -> set[str]:
    """只返回完整、规范化后属于给定集合的接口 token。"""
    indexed = {name.lower(): name for name in names}
    return {
        indexed[canonical_cisco_interface(match.group()).lower()]
        for match in _CISCO_INTERFACE_TOKEN.finditer(text)
        if canonical_cisco_interface(match.group()).lower() in indexed
    }


def _addresses(block: CiscoNode) -> set[str]:
    result: set[str] = set()
    for child in block.walk():
        match = _ADDRESS.match(child.header)
        if match:
            try:
                result.add(str(ipaddress.ip_interface(match.group(1)).ip))
            except ValueError:
                pass  # negotiated/unnumbered 不代表已配置本地地址。
    return result


def _walk(nodes: list[CiscoNode], path: tuple[str, ...] = ()):
    for node in nodes:
        if not node.active or node.is_formatting:
            continue
        current = (*path, node.header)
        yield node, current
        yield from _walk(node.children, current)



def _prune_bgp_sources(
    document: CiscoDocument,
    removed_interfaces: set[str],
    removed_addresses: dict[str, set[str]],
    retained_addresses: dict[str, set[str]],
    outcome: InterfacePruningOutcome,
    record: Callable[[CiscoNode, tuple[str, ...], str, set[str]], None],
) -> None:
    """先按继承计算邻居的有效源，再清理失效默认值，保留显式覆盖的会话。"""
    all_removed_addresses = set().union(*removed_addresses.values()) if removed_addresses else set()

    def vrf_for(path: tuple[str, ...]) -> str:
        return next((header.split(maxsplit=1)[1] for header in reversed(path)
                     if re.match(r"^vrf\s+", header, re.I)), "")

    def source_key(value: str) -> tuple[str, str]:
        try:
            return "address", str(ipaddress.ip_address(value))
        except ValueError:
            return "interface", document.resolve_interface(value)

    def source_lost(source: tuple[str, str], vrf: str) -> bool:
        kind, value = source
        if kind == "interface":
            return bool(interface_references(document, value, removed_interfaces))
        # 全局模板的 IP 源也必须在实际邻居的 VRF 内存在，不能借用别的 VRF。
        return value in all_removed_addresses and value not in retained_addresses.get(vrf, set())

    def source_interfaces(source: tuple[str, str]) -> set[str]:
        if source[0] == "interface":
            return interface_references(document, source[1], removed_interfaces)
        return set()

    for root in document.root.children:
        if not root.active or not re.match(r"^router\s+bgp\b", root.header, re.I):
            continue
        # 用对象身份保存原始源语句，后面的停用不会影响已经算出的有效源。
        sessions: dict[int, tuple[CiscoNode, tuple[str, ...], bool]] = {}
        templates: dict[tuple[tuple[str, ...], str, str], list[int]] = {}
        statements: dict[int, tuple[CiscoNode, tuple[str, ...], tuple[str, str]]] = {}
        direct: dict[int, dict[tuple[str, str], set[int]]] = {}
        uses: dict[int, list[tuple[str, str]]] = {}
        for node, path in _walk([root]):
            match = re.fullmatch(r"(neighbor|neighbor-group|session-group)\s+(.+)", node.header, re.I)
            if not match:
                continue
            identity = id(node)
            kind, name = match.group(1).lower(), match.group(2)
            sessions[identity] = node, path, kind != "neighbor"
            if kind != "neighbor":
                templates.setdefault((path[:-1], kind, name), []).append(identity)
            direct[identity] = {}
            uses[identity] = []
            for child in node.children:
                if not child.active or child.is_formatting:
                    continue
                source = re.fullmatch(r"(?:update-source|local-address)\s+(.+)", child.header, re.I)
                if source:
                    key = source_key(source.group(1).strip())
                    direct[identity].setdefault(key, set()).add(id(child))
                    statements[id(child)] = child, (*path, child.header), key
                use = re.fullmatch(r"use\s+(neighbor-group|session-group)\s+(\S+)", child.header, re.I)
                if use:
                    uses[identity].append((use.group(1).lower(), use.group(2)))

        edges: dict[int, list[int]] = {identity: [] for identity in sessions}
        errors: set[str] = set()
        for identity, references in uses.items():
            _, path, _ = sessions[identity]
            for kind, name in references:
                candidates = [
                    (scope, ids) for (scope, template_kind, template_name), ids in templates.items()
                    if template_kind == kind and template_name == name and path[:len(scope)] == scope
                ]
                if not candidates:
                    errors.add(f"BGP 本地源继承无法解析: {' / '.join(path)} 引用缺失的 {kind} {name}")
                    continue
                _, ids = max(candidates, key=lambda item: len(item[0]))
                if len(ids) != 1:
                    errors.add(f"BGP 本地源继承无法确定: {' / '.join(path)} 引用重复的 {kind} {name}")
                    continue
                edges[identity].append(ids[0])

        resolved: dict[int, dict[tuple[str, str], set[int]]] = {}

        def effective(identity: int, visiting: set[int]) -> dict[tuple[str, str], set[int]]:
            if identity in resolved:
                return resolved[identity]
            _, path, _ = sessions[identity]
            if identity in visiting:
                errors.add(f"BGP 本地源继承存在循环: {' / '.join(path)}")
                return {}
            # 本层显式值优先于模板；不能因模板失效而删除显式使用健康源的邻居。
            values = {key: set(ids) for key, ids in direct[identity].items()}
            if not values:
                for target in edges[identity]:
                    for key, ids in effective(target, visiting | {identity}).items():
                        values.setdefault(key, set()).update(ids)
            if len(values) > 1:
                errors.add(f"BGP 有效本地源冲突，无法安全裁剪: {' / '.join(path)}")
            resolved[identity] = values
            return values

        for identity in sessions:
            effective(identity, set())
        if errors:
            outcome.errors.extend(sorted(errors))
            continue

        # liveness 同时考虑公共参数：显式覆盖源的邻居依然需要模板中的 remote-as 等配置。
        required: set[int] = set()
        used_statements: set[int] = set()
        affected_templates: set[int] = set()
        for identity, (node, path, template) in sessions.items():
            values = resolved[identity]
            lost = any(source_lost(source, vrf_for(path)) for source in values)
            if not template and lost:
                refs = set().union(*(source_interfaces(source) for source in values))
                record(node, path, "BGP 有效本地源依赖已裁剪接口或地址，删除整个邻居", refs)
            elif template and lost:
                affected_templates.add(identity)
            else:
                required.add(identity)
                if not template:
                    for ids in values.values():
                        used_statements.update(ids)

        pending = list(required)
        while pending:
            identity = pending.pop()
            for target in edges[identity]:
                if target not in required:
                    required.add(target)
                    pending.append(target)
        for identity in affected_templates - required:
            node, path, _ = sessions[identity]
            refs = set().union(*(source_interfaces(source) for source in resolved[identity]))
            record(node, path, "BGP 失效本地源模板不再被保留会话依赖", refs)
        for identity in required:
            _, _, template = sessions[identity]
            if not template:
                continue
            for source, ids in direct[identity].items():
                for statement_id in ids:
                    child, path, _ = statements[statement_id]
                    if statement_id not in used_statements and source_lost(source, vrf_for(path)):
                        record(child, path, "保留 BGP 模板公共参数，移除未被有效会话使用的失效默认源", source_interfaces(source))

def prune_non_nni(document: CiscoDocument, protected_parents: set[str]) -> InterfacePruningOutcome:
    """在 NNI 迁移前删除接入接口；不能安全裁剪的引用使转换失败。"""
    outcome = InterfacePruningOutcome()
    protected = {interface_parent(document.resolve_interface(name)) for name in protected_parents}
    members = document.bundle_members()
    protected.update(bundle for member, bundle in members.items() if member in protected)
    protected.update(member for member, bundle in members.items() if bundle in protected)
    blocks = interface_nodes(document)
    names = {block.interface_name for block in blocks if block.interface_name}
    removed = {
        name for name in names
        if document.interface_kind(name) in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
        and interface_parent(name) not in protected
    }
    # 即使父口没有单独的 interface 块，外部也可能引用父口。
    removed.update(interface_parent(name) for name in list(removed))

    def record(node: CiscoNode, path: tuple[str, ...], reason: str, refs: set[str]) -> None:
        node.active = False
        outcome.changes.append({
            "action": "remove", "path": list(path),
            "source_interfaces": sorted(refs), "reason": reason,
        })

    retained_gateways: set[str] = set()
    domains = [
        (node, path) for node, path in _walk(document.root.children)
        if path[0].lower() == "l2vpn" and re.match(r"^bridge-domain\s", node.header, re.I)
    ]
    for domain, path in domains:
        attachments = {
            document.resolve_interface(child.header.split(maxsplit=1)[1])
            for child in domain.children
            if child.active and re.match(r"^interface\s", child.header, re.I)
        }
        gateways = {
            document.resolve_interface(re.sub(r"^routed\s+interface\s+", "", child.header, flags=re.I))
            for child in domain.children
            if child.active and re.match(r"^routed\s+interface\s", child.header, re.I)
        }
        if attachments and not (attachments - removed):
            record(domain, path, "广播域只关联已裁剪的接入接口", attachments & removed)
        elif attachments - removed:
            retained_gateways.update(gateways)

    for service, path in list(_walk(document.root.children)):
        if (
            path[0].lower() != "l2vpn"
            or not re.match(r"^p2p\s+", service.header, re.I)
            or not any(re.match(r"^xconnect\s+group\s+", ancestor, re.I) for ancestor in path[:-1])
        ):
            continue
        attachments = {
            document.resolve_interface(child.header.split(maxsplit=1)[1])
            for child in service.children
            if child.active and re.match(r"^interface\s+", child.header, re.I)
        }
        if attachments and attachments <= removed:
            record(service, path, "点到点业务的全部本地接口已裁剪，删除远端 PW 绑定", attachments)

    removed.update(
        name for name in names
        if document.interface_kind(name) == InterfaceKind.GATEWAY and name not in retained_gateways
    )
    removed_addresses: set[str] = set()
    retained_addresses: set[str] = set()
    removed_sources: dict[str, set[str]] = {}
    retained_sources: dict[str, set[str]] = {}
    for block in blocks:
        name = block.interface_name
        vrf = next((child.header.split(maxsplit=1)[1] for child in block.children
                    if child.active and re.match(r"^vrf\s+", child.header, re.I)), "")
        addresses = _addresses(block)
        if name in removed:
            removed_addresses.update(addresses)
            removed_sources.setdefault(vrf, set()).update(addresses)
            record(block, (block.header,), "非 NNI 接口不参与核心路由实验", {name})
        else:
            retained_addresses.update(addresses)
            retained_sources.setdefault(vrf, set()).update(addresses)
    removed_addresses.difference_update(retained_addresses)
    outcome.removed_interfaces = removed
    outcome.removed_addresses = removed_addresses

    _prune_bgp_sources(
        document, removed, removed_sources, retained_sources, outcome, record,
    )

    def clean(nodes: list[CiscoNode], path: tuple[str, ...] = ()) -> None:
        for node in nodes:
            if not node.active or node.is_formatting:
                continue
            current = (*path, node.header)
            if re.match(r"^(?:description|remark)\b", node.header, re.I):
                continue
            refs = interface_references(document, node.header, removed)
            if refs:
                # 这些语法中整个节点就是接口绑定或以接口为下一跳的路由。
                top = current[0].lower()
                interface_binding = bool(re.match(r"^(?:interface|routed\s+interface)\s", node.header, re.I))
                known_binding = interface_binding and (
                    re.match(r"^(?:router\s+(?:ospf\S*|isis|rip)|mpls|rsvp|l2vpn|vrf)\b", top)
                    is not None
                )
                static_route = top.startswith("router static") and bool(re.match(r"^[0-9a-fA-F:.]+/\d+\s", node.header))
                if known_binding or static_route:
                    record(node, current, "删除依赖非 NNI 接口的绑定或静态路由", refs)
                    continue
                outcome.errors.append(
                    f"无法安全裁剪活动接口依赖: {' / '.join(current)}; 接口 {', '.join(sorted(refs))}"
                )
            clean(node.children, current)
    clean(document.root.children)
    return outcome


def validate_pruned_references(document: CiscoDocument, removed: set[str]) -> InterfacePruningOutcome:
    """最终检查没有重新出现的已删除接口；目标重用旧名称不误报。"""
    outcome = InterfacePruningOutcome()
    defined = {block.interface_name for block in interface_nodes(document)}
    defined.update(interface_parent(name) for name in list(defined) if name)
    deleted = removed - defined
    for node, path in _walk(document.root.children):
        if re.match(r"^(?:description|remark)\b", node.header, re.I):
            continue
        refs = interface_references(document, node.header, deleted)
        if refs:
            outcome.errors.append(f"残留已删除接口引用: {' / '.join(path)}; 接口 {', '.join(sorted(refs))}")
    return outcome
