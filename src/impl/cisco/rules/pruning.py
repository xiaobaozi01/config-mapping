"""按原始接口身份裁剪非 NNI 业务，并检查其活动依赖。"""

from __future__ import annotations

import ipaddress
import re

from impl.common.interface import InterfaceKind, interface_parent
from impl.common.outcomes import InterfacePruningOutcome
from ..document import CiscoDocument, CiscoContext, canonical_cisco_interface, _CISCO_INTERFACE_TOKEN
from ..interfaces import interface_nodes
from .support import CiscoRuleVisitor, CiscoVisitorRule, document_scope


# 整个接口 token 一次规范化，避免 Gi/BE 缩写和主口/子口前缀误匹配。
_ADDRESS = re.compile(r"^(?:ipv4|ipv6)\s+address\s+(\S+)", re.I)


def interface_references(document: CiscoDocument, text: str, names: set[str]) -> set[str]:
    """只返回完整、规范化后属于给定集合的接口 token。"""
    # 桥组/桥域的标识符可以恰好等于 BVI 名称，它们是定义名称而非引用。
    if re.match(r"^(?:bridge\s+group|bridge-domain|description|remark)\b", text, re.I):
        return set()
    indexed = {name.lower(): name for name in names}
    return {
        indexed[canonical_cisco_interface(match.group()).lower()]
        for match in _CISCO_INTERFACE_TOKEN.finditer(text)
        if canonical_cisco_interface(match.group()).lower() in indexed
    }


def _addresses(block: CiscoContext) -> set[str]:
    result: set[str] = set()
    for child in block.walk():
        match = _ADDRESS.match(child.header)
        if match:
            try:
                result.add(str(ipaddress.ip_interface(match.group(1)).ip))
            except ValueError:
                pass  # negotiated/unnumbered 不代表已配置本地地址。
    return result


def _walk(nodes: list[CiscoContext], path: tuple[str, ...] = ()):
    for node in nodes:
        if not node.active or node.is_formatting:
            continue
        current = (*path, node.header)
        yield node, current
        yield from _walk(node.nodes, current)


class _BgpSourcePruner:
    """解析 BGP 源继承并按存活会话清理失效配置。"""

    def __init__(self, document, removed_interfaces, removed_addresses,
                 retained_addresses, outcome, record):
        self.document = document
        self.removed_interfaces = removed_interfaces
        self.all_removed_addresses = set().union(*removed_addresses.values()) if removed_addresses else set()
        self.retained_addresses = retained_addresses
        self.outcome = outcome
        self.record = record

    def vrf_for(self, path: tuple[str, ...]) -> str:
        return next((header.split(maxsplit=1)[1] for header in reversed(path)
                     if re.match(r"^vrf\s+", header, re.I)), "")

    def source_key(self, value: str) -> tuple[str, str]:
        try:
            return "address", str(ipaddress.ip_address(value))
        except ValueError:
            return "interface", self.document.resolve_interface(value)

    def source_lost(self, source: tuple[str, str], vrf: str) -> bool:
        kind, value = source
        if kind == "interface":
            return bool(interface_references(self.document, value, self.removed_interfaces))
        # 全局模板的 IP 源也必须在实际邻居的 VRF 内存在，不能借用别的 VRF。
        return value in self.all_removed_addresses and value not in self.retained_addresses.get(vrf, set())

    def source_interfaces(self, source: tuple[str, str]) -> set[str]:
        if source[0] == "interface":
            return interface_references(self.document, source[1], self.removed_interfaces)
        return set()

    def index(self, root):
        self.sessions: dict[int, tuple[CiscoContext, tuple[str, ...], bool]] = {}
        self.templates: dict[tuple[tuple[str, ...], str, str], list[int]] = {}
        self.statements: dict[int, tuple[CiscoContext, tuple[str, ...], tuple[str, str]]] = {}
        self.direct: dict[int, dict[tuple[str, str], set[int]]] = {}
        self.uses: dict[int, list[tuple[str, str]]] = {}
        for node, path in _walk([root]):
            match = re.fullmatch(r"(neighbor|neighbor-group|session-group)\s+(.+)", node.header, re.I)
            if not match:
                continue
            identity = id(node)
            kind, name = match.group(1).lower(), match.group(2)
            self.sessions[identity] = node, path, kind != "neighbor"
            if kind != "neighbor":
                self.templates.setdefault((path[:-1], kind, name), []).append(identity)
            self.direct[identity] = {}
            self.uses[identity] = []
            for child in node.nodes:
                if not child.active or child.is_formatting:
                    continue
                source = re.fullmatch(r"(?:update-source|local-address)\s+(.+)", child.header, re.I)
                if source:
                    key = self.source_key(source.group(1).strip())
                    self.direct[identity].setdefault(key, set()).add(id(child))
                    self.statements[id(child)] = child, (*path, child.header), key
                use = re.fullmatch(r"use\s+(neighbor-group|session-group)\s+(\S+)", child.header, re.I)
                if use:
                    self.uses[identity].append((use.group(1).lower(), use.group(2)))

    def link_templates(self):
        self.edges: dict[int, list[int]] = {identity: [] for identity in self.sessions}
        self.errors: set[str] = set()
        for identity, references in self.uses.items():
            _, path, _ = self.sessions[identity]
            for kind, name in references:
                candidates = [
                    (scope, ids) for (scope, template_kind, template_name), ids in self.templates.items()
                    if template_kind == kind and template_name == name and path[:len(scope)] == scope
                ]
                if not candidates:
                    self.errors.add(f"BGP 本地源继承无法解析: {' / '.join(path)} 引用缺失的 {kind} {name}")
                    continue
                _, ids = max(candidates, key=lambda item: len(item[0]))
                if len(ids) != 1:
                    self.errors.add(f"BGP 本地源继承无法确定: {' / '.join(path)} 引用重复的 {kind} {name}")
                    continue
                self.edges[identity].append(ids[0])

    def effective(self, identity: int, visiting: set[int]) -> dict[tuple[str, str], set[int]]:
        if identity in self.resolved:
            return self.resolved[identity]
        _, path, _ = self.sessions[identity]
        if identity in visiting:
            self.errors.add(f"BGP 本地源继承存在循环: {' / '.join(path)}")
            return {}
        # 本层显式值优先于模板；不能因模板失效而删除显式使用健康源的邻居。
        values = {key: set(ids) for key, ids in self.direct[identity].items()}
        if not values:
            for target in self.edges[identity]:
                for key, ids in self.effective(target, visiting | {identity}).items():
                    values.setdefault(key, set()).update(ids)
        if len(values) > 1:
            self.errors.add(f"BGP 有效本地源冲突，无法安全裁剪: {' / '.join(path)}")
        self.resolved[identity] = values
        return values

    def prune_sessions(self):
        required: set[int] = set()
        used_statements: set[int] = set()
        affected_templates: set[int] = set()
        for identity, (node, path, template) in self.sessions.items():
            values = self.resolved[identity]
            lost = any(self.source_lost(source, self.vrf_for(path)) for source in values)
            if not template and lost:
                refs = set().union(*(self.source_interfaces(source) for source in values))
                self.record(node, path, "BGP 有效本地源依赖已裁剪接口或地址，删除整个邻居", refs)
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
            for target in self.edges[identity]:
                if target not in required:
                    required.add(target)
                    pending.append(target)
        for identity in affected_templates - required:
            node, path, _ = self.sessions[identity]
            refs = set().union(*(self.source_interfaces(source) for source in self.resolved[identity]))
            self.record(node, path, "BGP 失效本地源模板不再被保留会话依赖", refs)
        for identity in required:
            _, _, template = self.sessions[identity]
            if not template:
                continue
            for source, ids in self.direct[identity].items():
                for statement_id in ids:
                    child, path, _ = self.statements[statement_id]
                    if statement_id not in used_statements and self.source_lost(source, self.vrf_for(path)):
                        self.record(child, path, "保留 BGP 模板公共参数，移除未被有效会话使用的失效默认源", self.source_interfaces(source))

    def run(self):
        for root in self.document.root.nodes:
            if not root.active or not re.match(r"^router\s+bgp\b", root.header, re.I):
                continue
            self.index(root)
            self.link_templates()
            self.resolved = {}
            for identity in self.sessions:
                self.effective(identity, set())
            if self.errors:
                self.outcome.errors.extend(sorted(self.errors))
                continue
            self.prune_sessions()


class _CiscoPruner:
    """跨对象裁剪分析；通过回调声明删除，不持有或提交事务。"""

    def __init__(self, document, protected_parents, outcome, record):
        self.document = document
        self.outcome = outcome
        self.record = record
        self.protected = {interface_parent(self.document.resolve_interface(name)) for name in protected_parents}
        members = self.document.bundle_members()
        self.protected.update(bundle for member, bundle in members.items() if member in self.protected)
        self.protected.update(member for member, bundle in members.items() if bundle in self.protected)
        self.blocks = interface_nodes(self.document)
        self.names = {block.interface_name for block in self.blocks if block.interface_name}
        self.removed = {
            name for name in self.names
            if self.document.interface_kind(name) in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
            and interface_parent(name) not in self.protected
        }
        # 即使父口没有单独的 interface 块，外部也可能引用父口。
        self.removed.update(interface_parent(name) for name in list(self.removed))

    def prune_domains(self):
        self.retained_gateways: set[str] = set()
        domains = [
            (node, path) for node, path in _walk(self.document.root.nodes)
            if path[0].lower() == "l2vpn" and re.match(r"^bridge-domain\s", node.header, re.I)
        ]
        for domain, path in domains:
            attachments = {
                self.document.resolve_interface(child.header.split(maxsplit=1)[1])
                for child in domain.nodes
                if child.active and re.match(r"^interface\s", child.header, re.I)
            }
            gateways = {
                self.document.resolve_interface(re.sub(r"^routed\s+interface\s+", "", child.header, flags=re.I))
                for child in domain.nodes
                if child.active and re.match(r"^routed\s+interface\s", child.header, re.I)
            }
            if attachments and not (attachments - self.removed):
                self.record(domain, path, "广播域只关联已裁剪的接入接口", attachments & self.removed)
            elif attachments - self.removed:
                self.retained_gateways.update(gateways)

    def prune_p2p(self):
        for service, path in list(_walk(self.document.root.nodes)):
            if (
                path[0].lower() != "l2vpn"
                or not re.match(r"^p2p\s+", service.header, re.I)
                or not any(re.match(r"^xconnect\s+group\s+", ancestor, re.I) for ancestor in path[:-1])
            ):
                continue
            attachments = {
                self.document.resolve_interface(child.header.split(maxsplit=1)[1])
                for child in service.nodes
                if child.active and re.match(r"^interface\s+", child.header, re.I)
            }
            if attachments and attachments <= self.removed:
                self.record(service, path, "点到点业务的全部本地接口已裁剪，删除远端 PW 绑定", attachments)

    def prune_interfaces(self):
        self.removed.update(
            name for name in self.names
            if self.document.interface_kind(name) == InterfaceKind.GATEWAY and name not in self.retained_gateways
        )
        self.removed_addresses: set[str] = set()
        self.retained_addresses: set[str] = set()
        self.removed_sources: dict[str, set[str]] = {}
        self.retained_sources: dict[str, set[str]] = {}
        for block in self.blocks:
            name = block.interface_name
            vrf = next((child.header.split(maxsplit=1)[1] for child in block.nodes
                        if child.active and re.match(r"^vrf\s+", child.header, re.I)), "")
            addresses = _addresses(block)
            if name in self.removed:
                self.removed_addresses.update(addresses)
                self.removed_sources.setdefault(vrf, set()).update(addresses)
                self.record(block, (block.header,), "非 NNI 接口不参与核心路由实验", {name})
            else:
                self.retained_addresses.update(addresses)
                self.retained_sources.setdefault(vrf, set()).update(addresses)
        self.removed_addresses.difference_update(self.retained_addresses)
        self.outcome.removed_interfaces = self.removed
        self.outcome.removed_addresses = self.removed_addresses

    def clean(self, nodes: list[CiscoContext], path: tuple[str, ...] = ()) -> None:
        for node in nodes:
            if not node.active or node.is_formatting:
                continue
            current = (*path, node.header)
            if re.match(r"^(?:description|remark)\b", node.header, re.I):
                continue
            refs = interface_references(self.document, node.header, self.removed)
            if refs:
                # 这些语法中整个节点就是接口绑定或以接口为下一跳的路由。
                top = current[0].lower()
                interface_binding = bool(re.match(r"^(?:interface|routed\s+interface)\s", node.header, re.I))
                known_binding = interface_binding and (
                    re.match(r"^(?:router\s+(?:ospf\S*|isis|rip|vrrp)|mpls|rsvp|l2vpn|vrf|evpn|dhcp\s+ipv[46])\b", top)
                    is not None
                    or (top == "control-plane" and "management-plane" in current and "inband" in current)
                )
                static_route = top.startswith("router static") and bool(re.match(r"^[0-9a-fA-F:.]+/\d+\s", node.header))
                if known_binding or static_route:
                    self.record(node, current, "删除依赖非 NNI 接口的绑定或静态路由", refs)
                    continue
                self.outcome.errors.append(
                    f"无法安全裁剪活动接口依赖: {' / '.join(current)}; 接口 {', '.join(sorted(refs))}"
                )
            self.clean(node.nodes, current)

    def prune_monitor_sessions(self):
        """镜像目的接口消失时删除对应会话；不影响其他镜像会话。"""
        for node in self.document.root.nodes:
            if not node.active or not re.match(r"^monitor-session\b", node.header, re.I):
                continue
            references = set()
            for child in node.walk():
                if re.match(r"^destination\s+interface\s+", child.header, re.I):
                    references.update(interface_references(self.document, child.header, self.removed))
            if references:
                self.record(node, (node.header,), "镜像会话的目的接口已删除", references)

    def check_bundle_ownership(self):
        members = self.document.bundle_members()
        business = self.document.business_interface_names()
        orphans = {
            spec.parent for spec in self.document.interface_specs()
            if spec.kind == InterfaceKind.BUNDLE and spec.name in business
            and spec.parent not in set(members.values())
        }
        if orphans:
            self.outcome.errors.append(
                f"业务聚合没有可解析的物理成员，归属不明: {', '.join(sorted(orphans))}"
            )
        return not orphans

    def analyze(self):
        if not self.check_bundle_ownership():
            return
        self.prune_domains()
        self.prune_p2p()
        self.prune_interfaces()
        _BgpSourcePruner(
            self.document, self.removed, self.removed_sources, self.retained_sources,
            self.outcome, self.record,
        ).run()
        self.prune_monitor_sessions()


class PruningVisitor(CiscoRuleVisitor):
    """保存本次业务删除事件，在 finish 向统一会话声明不重叠的删除。"""

    def __init__(self, session):
        super().__init__(session)
        self.deletions = {}
        self.events = []

    def record(self, node, path, reason, references):
        sources = sorted(references)
        self.deletions[id(node)] = node, reason, sources
        self.events.append({
            "action": "remove", "path": list(path), "reason": reason, "source_interfaces": sources,
        })

    def finish(self):
        if self.session.result.errors:
            return
        for node, reason, sources in self.deletions.values():
            parent = node.parentCtx
            while parent is not None and id(parent) not in self.deletions:
                parent = parent.parentCtx
            if parent is None:
                self.session.edits.delete(node, reason, source_interfaces=sources)


class NonNNIVisitor(PruningVisitor):
    def visitConfiguration(self, root):
        _CiscoPruner(
            self.session.document, self.session.context.protected_parents,
            self.session.result, self.record,
        ).analyze()


class MemberSourcesVisitor(PruningVisitor):
    def visitConfiguration(self, root):
        worker = _CiscoPruner(self.session.document, set(), self.session.result, self.record)
        worker.removed = set(self.session.context.removed_interfaces)
        _BgpSourcePruner(worker.document, worker.removed, {}, {}, worker.outcome, self.record).run()
        worker.prune_monitor_sessions()


class InterfaceBindingsVisitor(PruningVisitor):
    def visitConfiguration(self, root):
        worker = _CiscoPruner(self.session.document, set(), self.session.result, self.record)
        worker.removed = set(self.session.context.removed_interfaces)
        worker.clean(root.nodes)


def pruning_result(session, visitor, edits):
    """成功提交后映射业务删除事件；祖先删除覆盖的后代事件保留既有报告语义。"""
    session.result.changes[:] = visitor.events if edits.total else []


def prune_non_nni(document: CiscoDocument, protected_parents: set[str]) -> InterfacePruningOutcome:
    from . import prune_non_nni as execute

    return execute(document, protected_parents)


def validate_pruned_references(document: CiscoDocument, removed: set[str]) -> InterfacePruningOutcome:
    """兼容函数入口，最终引用检查使用同一个只读 Visitor 规则。"""
    from .compatibility import REMOVED_REFERENCES_RULE
    from impl.common.rules import ValidationContext
    result = REMOVED_REFERENCES_RULE.apply(document, ValidationContext(frozenset(), frozenset(removed)))
    return InterfacePruningOutcome(errors=result.errors, warnings=result.warnings)


def prune_member_sources(document, context):
    return MEMBER_SOURCES_RULE.apply(document, context)


def prune_interface_bindings(document, context):
    return INTERFACE_BINDINGS_RULE.apply(document, context)


def prune_interface_dependencies(document, removed_interfaces):
    """成员缩减复用 PRUNE_RULES，不再直接调用具体的依赖裁剪函数。"""
    from . import prune_member_dependencies

    return prune_member_dependencies(document, removed_interfaces)


def _tracked_interface(document, node):
    """只识别单一 line-protocol 监控；列表和其他 track 类型不自动裁剪。"""
    types = [child for child in node.nodes if child.active and re.match(r"^type\b", child.header, re.I)]
    if len(types) != 1 or not re.fullmatch(r"type\s+line-protocol\s+state", types[0].header, re.I):
        return None
    interfaces = [
        match.group(1) for child in types[0].nodes if child.active
        if (match := re.fullmatch(r"interface\s+(.+)", child.header, re.I))
    ]
    return document.resolve_interface(interfaces[0]) if len(interfaces) == 1 else None


def _is_vrrp_use(header, path, name):
    match = re.fullmatch(r"track\s+object\s+(\S+)(?:\s+\d+)?", header, re.I)
    return bool(
        match and match.group(1) == name and path[0].lower() == "router vrrp"
        and any(re.fullmatch(r"address-family\s+ipv[46]", part, re.I) for part in path[1:-1])
        and re.fullmatch(r"vrrp\s+\d+", path[-2], re.I)
    )


def _track_uses(name, definitions, commands):
    """预先收集消费引用；发现未知消费者时保留整条依赖链并报错。"""
    pattern = re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])")
    known = []
    errors = []
    for node, path in commands:
        if any(node is definition for definition in definitions):
            continue
        if re.match(r"^(?:description|remark)\b", node.header, re.I) or not pattern.search(node.header):
            continue
        if _is_vrrp_use(node.header, path, name):
            known.append((node, path))
        else:
            errors.append(f"无法安全裁剪 track {name} 的活动依赖: {' / '.join(path)}")
    return known, errors


class TrackDependenciesVisitor(PruningVisitor):
    """删除失去监控接口的 track 和 VRRP 跟踪语句，保留 VRRP 组。"""

    def visitConfiguration(self, root):
        document, outcome = self.session.document, self.session.result
        removed_interfaces = self.session.context.removed_interfaces
        commands = list(_walk(root.nodes))
        definitions = {}
        for node in root.nodes:
            if node.active and (match := re.fullmatch(r"track\s+(\S+)", node.header, re.I)):
                definitions.setdefault(match.group(1), []).append(node)
        for name, nodes in definitions.items():
            targets = [_tracked_interface(document, node) for node in nodes]
            lost = {target for target in targets if target and (
                target in removed_interfaces or interface_parent(target) in removed_interfaces
            )}
            if not lost:
                continue
            if len(nodes) != 1:
                outcome.errors.append(f"track {name} 存在重复定义，不能安全级联裁剪")
                continue
            uses, unknown = _track_uses(name, nodes, commands)
            if unknown:
                outcome.errors.extend(unknown)
                continue
            self.record(nodes[0], (nodes[0].header,), "track 监控的接口已删除，移除跟踪对象", lost)
            for node, path in uses:
                self.record(node, path, f"track {name} 已删除，解除 VRRP 对象跟踪", lost)


def prune_track_dependencies(document, context):
    return TRACK_DEPENDENCIES_RULE.apply(document, context)


def full_prune_condition(context):
    return context.remove_non_nni


def member_prune_condition(context):
    return not context.remove_non_nni


def prune_non_nni_rule(document, context):
    return NON_NNI_RULE.apply(document, context)


NON_NNI_RULE = CiscoVisitorRule(
    "cisco.prune.non_nni", NonNNIVisitor, selector=document_scope, condition=full_prune_condition,
    outcome_factory=InterfacePruningOutcome, result_mapper=pruning_result,
)
MEMBER_SOURCES_RULE = CiscoVisitorRule(
    "cisco.prune.member_sources", MemberSourcesVisitor, selector=document_scope, condition=member_prune_condition,
    outcome_factory=InterfacePruningOutcome, result_mapper=pruning_result,
)
TRACK_DEPENDENCIES_RULE = CiscoVisitorRule(
    "cisco.prune.track_dependencies", TrackDependenciesVisitor, selector=document_scope,
    outcome_factory=InterfacePruningOutcome, result_mapper=pruning_result,
)
INTERFACE_BINDINGS_RULE = CiscoVisitorRule(
    "cisco.prune.interface_bindings", InterfaceBindingsVisitor, selector=document_scope,
    outcome_factory=InterfacePruningOutcome, result_mapper=pruning_result,
)
