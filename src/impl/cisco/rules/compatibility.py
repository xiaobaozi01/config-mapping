"""接口类型适配、兼容校验与最终只读检查。"""

import re
from impl.common.interface import InterfaceKind, interface_parent, interface_unit
from impl.common.semantic_identity.models import MergeKind
from ..identity import resolve_cisco_identity
from ..interfaces import interface_nodes
from .support import (
    CiscoVisitorRule, CiscoRuleVisitor, InterfaceScope, append_top_level,
    compatibility_interfaces_scope, format_fragment,
)

_AGGREGATION = re.compile(r"^(?:no\s+)?(?:bundle\b|lacp\b|aggregated-)", re.I)
_ETHERTYPE = re.compile(r"^(?:ethernet\s+)?(?:dot1q\s+)?tunneling\s+ethertype\b", re.I)
_TARGETS = InterfaceScope("target_interfaces", True, frozenset({InterfaceKind.PHYSICAL, InterfaceKind.GATEWAY}))
_PHYSICAL = InterfaceScope("target_interfaces", True, frozenset({InterfaceKind.PHYSICAL}))
_HARDWARE = re.compile(
    r"^(?:no\s+)?(?:speed|duplex|negotiation|fec|transceiver|carrier-delay|dampening)\b",
    re.IGNORECASE,
)
_ADDRESSING = re.compile(
    r"^(?:ipv[46]\s+(?:address|unnumbered|enable)\b|vrf\b)", re.IGNORECASE
)
# This list suppresses noise for familiar command families; it is not an allowlist
# certifying all their options on every XRv9000 image.
_FAMILIAR = re.compile(
    r"^(?:no\s+)?(?:description|shutdown|mtu|bandwidth|ipv4|ipv6|vrf|arp|"
    r"proxy-arp|local-proxy-arp|mac-address|encapsulation|rewrite|l2transport|"
    r"load-interval|flow-control|keepalive|service-policy|bfd|lldp|cdp|"
    r"xconnect|mpls|rsvp|ethernet|tunneling)\b",
    re.IGNORECASE,
)


def _normal(command):
    return " ".join(command.lower().split())


class AggregationAttributesVisitor(CiscoRuleVisitor):
    def __init__(self, session):
        super().__init__(session)
        self.members = session.document.bundle_members()
        self.interface = None

    def visitNode(self, node):
        if node.parentCtx is self.session.document.root:
            self.interface = node.interface_name
            if self.interface in self.members:
                return None
        elif _AGGREGATION.match(node.header):
            self.session.edits.delete(node, "目标非聚合接口不保留聚合属性", interface=self.interface)
            return None
        return self.visitChildren(node)


class EthertypeVisitor(CiscoRuleVisitor):
    def __init__(self, session):
        super().__init__(session)
        self.parents = {}
        for block in interface_nodes(session.document):
            self.parents.setdefault(block.interface_name, block)
        self.additions = {}
        self.moves = []

    def visitNode(self, block):
        name = block.interface_name
        if interface_unit(name) is None:
            return None
        parent_name = interface_parent(name)
        parent = self.parents.get(parent_name)
        existing = [] if parent is None else [node for node in parent.nodes if _ETHERTYPE.match(node.header)]
        pending = self.additions.setdefault(parent_name, [])
        for node in block.nodes:
            if not _ETHERTYPE.match(node.header):
                continue
            if any(_normal(other.header) != _normal(node.header) for other in (*existing, *pending)):
                self.session.result.errors.append(
                    f"接口 {name} 的 {node.header} 与父接口 {parent_name} 的 tunneling ethertype 冲突"
                )
                continue
            if not existing and not pending:
                pending.append(node)
            self.session.edits.delete(node, "tunneling ethertype 只能配置在物理主口")
            self.moves.append({
                "action": "moved", "interface": name, "target_interface": parent_name,
                "command": node.header, "reason": "tunneling ethertype 只能配置在物理主口",
            })
        return None

    def finish(self):
        if self.session.result.errors:
            return
        for name, nodes in self.additions.items():
            if not nodes:
                continue
            text = "".join(node.text for node in nodes)
            parent = self.parents.get(name)
            if parent is not None:
                self.session.edits.append_child(parent, text, "迁入物理主口 ethertype")
            else:
                fragment = f"interface {name}\n no shutdown\n" + format_fragment(text, 1) + "!\n"
                append_top_level(self.session.edits, self.session.document, fragment, "创建承载 ethertype 的物理主口")


def ethertype_result(session, visitor, edits):
    session.result.changes[:] = visitor.moves if edits.total else []


AGGREGATION_ATTRIBUTES_RULE = CiscoVisitorRule(
    "cisco.adapt.aggregation_attributes", AggregationAttributesVisitor, selector=_TARGETS,
)
ETHERTYPE_RULE = CiscoVisitorRule(
    "cisco.adapt.ethertype", EthertypeVisitor, selector=_PHYSICAL, result_mapper=ethertype_result,
)


def remove_aggregation_attributes(document, context):
    return AGGREGATION_ATTRIBUTES_RULE.apply(document, context)


def migrate_ethertype(document, context):
    return ETHERTYPE_RULE.apply(document, context)


def _is_l2(block):
    return block.l2transport or any(_normal(node.header) == "l2transport" for node in block.nodes)


def _scope(document, targets):
    return {interface_parent(document.resolve_interface(name)) for name in targets}


def _in_scope(name, parents):
    return interface_parent(name) in parents


def l2vpn_scope(document, context):
    return tuple(node for node in document.root.nodes if re.match(r"^l2vpn\b", node.header, re.I))


def _validate_command_types(name, command, kind, l2, unit, outcome):
    if l2 and _ADDRESSING.match(command):
        outcome.errors.append(f"L2 接口 {name} 不能包含三层地址或 VRF 命令: {command}")
    if l2 and re.match(r"^mac(?:-|\s+)address\b", command, re.IGNORECASE):
        outcome.errors.append(f"L2 接口 {name} 不能包含三层 MAC 地址命令: {command}")
    if not l2 and re.match(r"^rewrite\b", command, re.IGNORECASE):
        outcome.errors.append(f"三层接口 {name} 不能包含 L2 标签改写命令: {command}")
    if kind == InterfaceKind.GATEWAY and re.match(
        r"^(?:encapsulation|rewrite)\b", command, re.IGNORECASE
    ):
        outcome.errors.append(f"BVI 接口 {name} 不能包含以太网子接口命令: {command}")
    if kind in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE} and unit is None and re.match(
        r"^encapsulation\b", command, re.IGNORECASE
    ):
        outcome.errors.append(f"物理主口 {name} 不能包含子接口封装命令: {command}")
    if kind == InterfaceKind.PHYSICAL and unit is not None and _ETHERTYPE.match(command):
        outcome.errors.append(f"子接口 {name} 残留主口专属命令: {command}")


class EndVisitor(CiscoRuleVisitor):
    def __init__(self, session):
        super().__init__(session)
        self.ended = False

    def visitNode(self, node):
        if node.is_formatting:
            return None
        if node.header.lower() == "end":
            self.ended = True
        elif self.ended:
            self.session.result.errors.append(f"配置终止标记 end 后存在活动配置: {node.header.split()[0]}")
        return None


class ScalarValuesVisitor(CiscoRuleVisitor):
    def visitNode(self, block):
        values = {}
        for node in block.nodes:
            if node.is_formatting:
                continue
            decision = resolve_cisco_identity(
                node.header, path=(block.header,), node_kind="block" if node.is_block else "leaf",
            )
            if decision.matched and decision.merge_kind == MergeKind.REPLACE:
                previous = values.setdefault(decision.identity, decision.normalized)
                if previous != decision.normalized:
                    self.session.result.errors.append(f"接口 {block.interface_name} 的单值配置 {' '.join(decision.identity)} 存在冲突")
        return None


class InterfaceTypesVisitor(CiscoRuleVisitor):
    def __init__(self, session):
        super().__init__(session)
        self.members = session.document.bundle_members()
        self.blocks = {block.interface_name: block for block in interface_nodes(session.document)}

    def visitNode(self, block):
        document, outcome = self.session.document, self.session.result
        name = block.interface_name
        kind = document.interface_kind(name)
        if name in self.members and document.bundle_mode(name) == "unknown":
            outcome.errors.append(f"聚合成员 {name} 含非法业务或冲突绑定")
        l2, unit = _is_l2(block), interface_unit(name)
        if kind == InterfaceKind.GATEWAY and (l2 or unit is not None):
            outcome.errors.append(f"网关接口 {name} 必须保留 BVI 三层接口类型")
        for node in block.nodes:
            if not node.is_formatting:
                _validate_command_types(name, node.header, kind, l2, unit, outcome)
        if kind == InterfaceKind.PHYSICAL and unit is not None:
            parent = self.blocks.get(interface_parent(name))
            if parent is not None and _is_l2(parent):
                outcome.errors.append(f"子接口 {name} 的物理父口不能配置为 l2transport")
        return None


class CompatibleCommandsVisitor(CiscoRuleVisitor):
    def __init__(self, session):
        super().__init__(session)
        self.members = session.document.bundle_members()

    def visitNode(self, block):
        name = block.interface_name
        kind = self.session.document.interface_kind(name)
        outcome = self.session.result
        for node in block.nodes:
            if node.is_formatting:
                continue
            command = node.header
            if kind != InterfaceKind.BUNDLE and ((_AGGREGATION.match(command) and name not in self.members) or _HARDWARE.match(command)):
                outcome.errors.append(f"接口 {name} 残留不兼容属性: {command}")
            if not _FAMILIAR.match(command) and not _ETHERTYPE.match(command):
                outcome.warnings.append(f"接口 {name} 命令族 {command.split()[0]} 未覆盖兼容规则，已保留，需镜像核验")
        return None


class L2vpnVisitor(CiscoRuleVisitor):
    def __init__(self, session):
        super().__init__(session)
        self.parents = _scope(session.document, session.context.target_interfaces)
        self.blocks = {block.interface_name: block for block in interface_nodes(session.document)}

    def visitNode(self, node):
        document, outcome = self.session.document, self.session.result
        routed = re.match(r"^routed\s+interface\s+(.+)$", node.header, re.I)
        attachment = re.match(r"^interface\s+(.+)$", node.header, re.I)
        if routed:
            name = document.resolve_interface(routed.group(1))
            if document.interface_kind(name) != InterfaceKind.GATEWAY or interface_unit(name) is not None:
                outcome.errors.append(f"L2VPN routed interface 必须引用 BVI，不能引用 {name}")
            elif name not in self.blocks:
                outcome.errors.append(f"L2VPN routed interface 引用不存在的 BVI: {name}")
        elif attachment:
            name = document.resolve_interface(attachment.group(1))
            target = self.blocks.get(name)
            if target is not None and not _is_l2(target):
                outcome.errors.append(f"L2VPN 接入接口 {name} 必须配置 l2transport")
            elif target is None and _in_scope(name, self.parents):
                outcome.errors.append(f"L2VPN 引用不存在的目标接入接口: {name}")
        return self.visitChildren(node)


class RemovedReferencesVisitor(CiscoRuleVisitor):
    def __init__(self, session):
        super().__init__(session)
        defined = {block.interface_name for block in interface_nodes(session.document)}
        defined.update(interface_parent(name) for name in list(defined) if name)
        self.deleted = session.context.removed_interfaces - defined

    def visitNode(self, node):
        from .pruning import interface_references
        if not node.is_formatting and not re.match(r"^(?:description|remark)\b", node.header, re.I):
            refs = interface_references(self.session.document, node.header, self.deleted)
            if refs:
                path = (*node.path, node.header)
                self.session.result.errors.append(f"残留已删除接口引用: {' / '.join(path)}; 接口 {', '.join(sorted(refs))}")
        return self.visitChildren(node)


END_RULE = CiscoVisitorRule("cisco.validate.end", EndVisitor, readonly=True)
SCALAR_VALUES_RULE = CiscoVisitorRule(
    "cisco.validate.scalar_values", ScalarValuesVisitor, selector=compatibility_interfaces_scope, readonly=True,
)
INTERFACE_TYPES_RULE = CiscoVisitorRule(
    "cisco.validate.interface_types", InterfaceTypesVisitor, selector=compatibility_interfaces_scope, readonly=True,
)
COMPATIBLE_COMMANDS_RULE = CiscoVisitorRule(
    "cisco.validate.compatible_commands", CompatibleCommandsVisitor, selector=compatibility_interfaces_scope, readonly=True,
)
L2VPN_RULE = CiscoVisitorRule("cisco.validate.l2vpn", L2vpnVisitor, selector=l2vpn_scope, readonly=True)
REMOVED_REFERENCES_RULE = CiscoVisitorRule("cisco.validate.removed_references", RemovedReferencesVisitor, readonly=True)


def validate_end(document, context):
    return END_RULE.apply(document, context)


def validate_scalar_values(document, context):
    return SCALAR_VALUES_RULE.apply(document, context)


def validate_interface_types(document, context):
    return INTERFACE_TYPES_RULE.apply(document, context)


def validate_compatible_commands(document, context):
    return COMPATIBLE_COMMANDS_RULE.apply(document, context)


def validate_l2vpn(document, context):
    return L2VPN_RULE.apply(document, context)


def adapt_interfaces(document, target_interfaces):
    from . import adapt_interfaces as execute
    return execute(document, target_interfaces)


def validate_interfaces(document, target_interfaces):
    from . import validate_compatibility
    return validate_compatibility(document, target_interfaces)
