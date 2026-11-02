"""接口类型适配、兼容校验与最终只读检查。"""

import re

from impl.common.domain.interface import (
    BundleMode,
    InterfaceKind,
    interface_parent,
    interface_unit,
)
from impl.common.semantic_identity.models import MergeKind

from ..operations.interfaces.access import interface_nodes
from ..support.identity import resolve_cisco_identity
from .support import (
    CiscoRuleVisitor,
    CiscoVisitorRule,
    InterfaceScope,
    append_top_level,
    compatibility_interfaces_scope,
    format_fragment,
)

_AGGREGATION = re.compile(r"^(?:no\s+)?(?:bundle\b|lacp\b|aggregated-)", re.I)
_ETHERTYPE = re.compile(
    r"^(?:ethernet\s+)?(?:dot1q\s+)?tunneling\s+ethertype\b", re.I
)
_TARGETS = InterfaceScope(
    "target_interfaces",
    True,
    frozenset({InterfaceKind.PHYSICAL, InterfaceKind.GATEWAY}),
)
_PHYSICAL = InterfaceScope(
    "target_interfaces", True, frozenset({InterfaceKind.PHYSICAL})
)
_HARDWARE = re.compile(
    r"^(?:no\s+)?(?:speed|duplex|negotiation|fec|transceiver|"
    r"carrier-delay|dampening)\b",
    re.IGNORECASE,
)
_ADDRESSING = re.compile(
    r"^(?:ipv[46]\s+(?:address|unnumbered|enable)\b|vrf\b)", re.IGNORECASE
)
# This list suppresses noise for familiar command families; it is not an
# allowlist certifying all their options on every XRv9000 image.
_FAMILIAR = re.compile(
    r"^(?:no\s+)?(?:description|shutdown|mtu|bandwidth|ipv4|ipv6|vrf|arp|"
    r"proxy-arp|local-proxy-arp|mac-address|encapsulation|rewrite|l2transport|"
    r"load-interval|flow-control|keepalive|service-policy|bfd|lldp|cdp|"
    r"xconnect|mpls|rsvp|ethernet|tunneling)\b",
    re.IGNORECASE,
)


def _normal(command):
    """把命令压缩为小写空白分隔形式。

    兼容性检查比较语法槽位而非原始排版，因此统一空白和大小写可避免重复报告。
    """
    return " ".join(command.lower().split())


def _record_input_semantic(session, outcome, message):
    """按运行策略记录源配置语义问题，严格模式保留错误级阻断。

    目标镜像兼容与映射完整性错误不经过此入口。
    """
    destination = (
        outcome.errors
        if session.context.strict_input_validation
        else outcome.warnings
    )
    destination.append(message)


class AggregationAttributesVisitor(CiscoRuleVisitor):
    """把目标接口上的聚合属性迁移到物理父口。

    子接口和 Bundle 主口上的绑定属性不能被 XRv9000 直接导入，迁移可保留成员关系。
    """

    def __init__(self, session):
        """建立成员索引并记录当前遍历的接口。

        当前接口名用于把子接口属性归属到同一物理父口。
        """
        super().__init__(session)
        self.members = session.document.interfaces.analysis.bundle_members()
        self.interface = None

    def visit_node(self, node):
        """检查一个节点并登记聚合属性的删除或迁移。

        只处理目标接口作用域，所有编辑通过托管会话收集后统一提交。
        """
        if node.parent is self.session.document.root:
            self.interface = node.interface_name
            if self.interface in self.members:
                return None
        elif _AGGREGATION.match(node.header):
            self.session.edits.delete(
                node, "目标非聚合接口不保留聚合属性", interface=self.interface
            )
            return None
        return self.visit_children(node)


class EthertypeVisitor(CiscoRuleVisitor):
    """把 tunneling ethertype 从子接口迁移到物理主口。

    XRv9000 只接受该命令位于物理主口；集中移动并检查冲突可避免导入失败。
    """

    def __init__(self, session):
        """建立物理父口索引和待迁移命令集合。

        预索引使子接口能在遍历过程中快速定位对应父口并检查现有值。
        """
        super().__init__(session)
        self.parents = {}
        for block in interface_nodes(session.document):
            self.parents.setdefault(block.interface_name, block)
        self.additions = {}
        self.moves = []

    def visit_node(self, block):
        """处理一个接口块中的 ethertype 命令。

        冲突值只报告错误；无冲突时删除子接口命令并记录一次主口添加。
        """
        name = block.interface_name
        if interface_unit(name) is None:
            return None
        parent_name = interface_parent(name)
        parent = self.parents.get(parent_name)
        existing = (
            []
            if parent is None
            else [
                node for node in parent.nodes if _ETHERTYPE.match(node.header)
            ]
        )
        pending = self.additions.setdefault(parent_name, [])
        for node in block.nodes:
            if not _ETHERTYPE.match(node.header):
                continue
            if any(
                _normal(other.header) != _normal(node.header)
                for other in (*existing, *pending)
            ):
                self.session.result.errors.append(
                    f"接口 {name} 的 {node.header} 与父接口 "
                    f"{parent_name} 的 tunneling ethertype 冲突"
                )
                continue
            if not existing and not pending:
                pending.append(node)
            self.session.edits.delete(
                node, "tunneling ethertype 只能配置在物理主口"
            )
            self.moves.append(
                {
                    "action": "moved",
                    "interface": name,
                    "target_interface": parent_name,
                    "command": node.header,
                    "reason": "tunneling ethertype 只能配置在物理主口",
                }
            )
        return None

    def finish(self):
        """提交收集到的物理主口添加操作。

        若遍历发现冲突则整体跳过添加，确保失败规则不留下半套迁移。
        """
        if self.session.result.errors:
            return
        for name, nodes in self.additions.items():
            if not nodes:
                continue
            text = "".join(
                self.session.document.writer.subtree(node) for node in nodes
            )
            parent = self.parents.get(name)
            if parent is not None:
                self.session.edits.append_child(
                    parent, text, "迁入物理主口 ethertype"
                )
            else:
                fragment = (
                    f"interface {name}\n no shutdown\n"
                    + format_fragment(text, 1)
                    + "!\n"
                )
                append_top_level(
                    self.session.edits,
                    self.session.document,
                    fragment,
                    "创建承载 ethertype 的物理主口",
                )


def ethertype_result(session, visitor, edits):
    """把 ethertype 移动记录映射到最终结果。

    只有实际提交编辑时才公开移动事件，避免无变更执行污染报告。
    """
    session.result.changes[:] = visitor.moves if edits.total else []


AGGREGATION_ATTRIBUTES_RULE = CiscoVisitorRule(
    "cisco.adapt.aggregation_attributes",
    AggregationAttributesVisitor,
    selector=_TARGETS,
)
ETHERTYPE_RULE = CiscoVisitorRule(
    "cisco.adapt.ethertype",
    EthertypeVisitor,
    selector=_PHYSICAL,
    result_mapper=ethertype_result,
)


def _is_l2(block):
    """判断接口块是否启用二层传输。

    同时识别 header 后缀和直接 l2transport 子命令，兼容两种合法 Cisco 写法。
    """
    return block.l2transport or any(
        _normal(node.header) == "l2transport" for node in block.nodes
    )


def _scope(document, targets):
    """把目标接口规范化为物理父口集合。

    规则以父口覆盖完整接口树，避免遗漏与目标共享父口的子接口。
    """
    return {
        interface_parent(document.interfaces.analysis.resolve_interface(name))
        for name in targets
    }


def _in_scope(name, parents):
    """判断接口父口是否位于目标作用域。

    使用规范化父口比较可避免接口缩写或子接口后缀造成范围误判。
    """
    return interface_parent(name) in parents


def l2vpn_scope(document, context):
    """选择全部顶层 L2VPN 容器。

    接入和 routed 引用均位于这些容器中，限定范围可避免扫描无关业务块。
    """
    return tuple(
        node
        for node in document.root.nodes
        if re.match(r"^l2vpn\b", node.header, re.I)
    )


def _validate_command_types(name, command, kind, l2, unit, outcome, session):
    """校验单条命令与接口类型及层级的匹配关系。

    所有非法组合追加到同一结果，使一次转换可以完整报告需要人工处理的冲突。
    """
    if l2 and _ADDRESSING.match(command):
        _record_input_semantic(
            session,
            outcome,
            f"L2 接口 {name} 不能包含三层地址或 VRF 命令: {command}",
        )
    if l2 and re.match(r"^mac(?:-|\s+)address\b", command, re.IGNORECASE):
        _record_input_semantic(
            session,
            outcome,
            f"L2 接口 {name} 不能包含三层 MAC 地址命令: {command}",
        )
    if not l2 and re.match(r"^rewrite\b", command, re.IGNORECASE):
        _record_input_semantic(
            session,
            outcome,
            f"三层接口 {name} 不能包含 L2 标签改写命令: {command}",
        )
    if kind == InterfaceKind.GATEWAY and re.match(
        r"^(?:encapsulation|rewrite)\b", command, re.IGNORECASE
    ):
        _record_input_semantic(
            session,
            outcome,
            f"BVI 接口 {name} 不能包含以太网子接口命令: {command}",
        )
    if (
        kind in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
        and unit is None
        and re.match(r"^encapsulation\b", command, re.IGNORECASE)
    ):
        _record_input_semantic(
            session,
            outcome,
            f"物理主口 {name} 不能包含子接口封装命令: {command}",
        )
    if (
        kind == InterfaceKind.PHYSICAL
        and unit is not None
        and _ETHERTYPE.match(command)
    ):
        _record_input_semantic(
            session,
            outcome,
            f"子接口 {name} 残留主口专属命令: {command}",
        )


class EndVisitor(CiscoRuleVisitor):
    """检查 Cisco ``end`` 之后不存在活动配置。

    终端标记之后的内容不会被设备加载；默认告警，严格模式恢复为错误。
    """

    def __init__(self, session):
        """初始化尚未遇到终止标记的状态。

        状态从首个节点开始累计，直到遍历结束或发现尾随配置。
        """
        super().__init__(session)
        self.ended = False

    def visit_node(self, node):
        """检查当前节点相对于终止标记的位置。

        格式节点被忽略，使空行和叹号不会改变活动配置判断。
        """
        if node.is_formatting:
            return None
        if node.header.lower() == "end":
            self.ended = True
        elif self.ended:
            _record_input_semantic(
                self.session,
                self.session.result,
                f"配置终止标记 end 后存在活动配置: {node.header.split()[0]}",
            )
        return None


class ScalarValuesVisitor(CiscoRuleVisitor):
    """检测接口中层叠的单值配置。

    同一语义槽位出现不同值时设备结果依赖顺序，规则必须报错而不能任意选择。
    """

    def visit_node(self, block):
        """检查一个接口块的直接单值命令。

        每个接口独立维护已见值，避免不同接口的相同命令被误判为冲突。
        """
        values = {}
        for node in block.nodes:
            if node.is_formatting:
                continue
            decision = resolve_cisco_identity(
                node.header,
                path=(block.header,),
                node_kind="block" if node.is_block else "leaf",
            )
            if decision.matched and decision.merge_kind == MergeKind.REPLACE:
                previous = values.setdefault(
                    decision.identity, decision.normalized
                )
                if previous != decision.normalized:
                    _record_input_semantic(
                        self.session,
                        self.session.result,
                        f"接口 {block.interface_name} 的单值配置 "
                        f"{' '.join(decision.identity)} 存在冲突",
                    )
        return None


class InterfaceTypesVisitor(CiscoRuleVisitor):
    """校验接口类型和业务命令的兼容关系。

    物理主口、子接口、聚合和网关允许的命令集合不同；默认保留并告警，
    严格输入校验时恢复为错误。
    """

    def __init__(self, session):
        """缓存聚合成员和接口块索引。

        索引使父口和成员判断无需反复遍历完整文档。
        """
        super().__init__(session)
        self.members = session.document.interfaces.analysis.bundle_members()
        self.blocks = {
            block.interface_name: block
            for block in interface_nodes(session.document)
        }

    def visit_node(self, block):
        """检查接口块的角色、层级和直接命令。

        先验证接口整体角色，再逐条验证命令，能够给出更具体的错误位置。
        """
        document, outcome = self.session.document, self.session.result
        name = block.interface_name
        kind = document.interfaces.analysis.interface_kind(name)
        l2, unit = _is_l2(block), interface_unit(name)
        if kind == InterfaceKind.GATEWAY and (l2 or unit is not None):
            _record_input_semantic(
                self.session,
                outcome,
                f"网关接口 {name} 必须保留 BVI 三层接口类型",
            )
        for node in block.nodes:
            if not node.is_formatting:
                _validate_command_types(
                    name,
                    node.header,
                    kind,
                    l2,
                    unit,
                    outcome,
                    self.session,
                )
        if kind == InterfaceKind.PHYSICAL and unit is not None:
            parent = self.blocks.get(interface_parent(name))
            if parent is not None and _is_l2(parent):
                _record_input_semantic(
                    self.session,
                    outcome,
                    f"子接口 {name} 的物理父口不能配置为 l2transport",
                )
        return None


class BundleBindingsVisitor(CiscoRuleVisitor):
    """检查聚合成员是否声明了无法唯一解释的绑定。

    该完整性检查不依赖源语义告警策略，始终阻断冲突绑定。
    """

    def __init__(self, session):
        """缓存物理成员集合，供每个接口块快速判断。

        索引只在当前只读校验会话内使用。
        """
        super().__init__(session)
        self.members = session.document.interfaces.analysis.bundle_members()

    def visit_node(self, block):
        """把成员绑定歧义作为始终阻断的完整性错误。

        无法唯一确定 Bundle 时不能猜测目标聚合关系。
        """
        name = block.interface_name
        if (
            name in self.members
            and self.session.document.bundles.mode(name) == BundleMode.UNKNOWN
        ):
            self.session.result.errors.append(f"聚合成员 {name} 包含冲突绑定")
        return None


class CompatibleCommandsVisitor(CiscoRuleVisitor):
    """检查 XRv9000 不支持的残留硬件与聚合命令。

    明确不支持的命令报错，未知命令族则保留并告警，避免将不确定项静默删除。
    """

    def __init__(self, session):
        """缓存聚合成员集合。

        成员接口允许 bundle 绑定命令，而 Bundle 主口和独立接口不允许，因此需要区分。
        """
        super().__init__(session)
        self.members = session.document.interfaces.analysis.bundle_members()

    def visit_node(self, block):
        """扫描接口块的直接命令并生成兼容性诊断。

        已覆盖命令族只做基本检查，未覆盖命令族保留并提示镜像人工核验。
        """
        name = block.interface_name
        kind = self.session.document.interfaces.analysis.interface_kind(name)
        outcome = self.session.result
        for node in block.nodes:
            if node.is_formatting:
                continue
            command = node.header
            if kind != InterfaceKind.BUNDLE and (
                (_AGGREGATION.match(command) and name not in self.members)
                or _HARDWARE.match(command)
            ):
                outcome.errors.append(f"接口 {name} 残留不兼容属性: {command}")
            if not _FAMILIAR.match(command) and not _ETHERTYPE.match(command):
                outcome.warnings.append(
                    f"接口 {name} 命令族 {command.split()[0]} 未覆盖兼容规则，已保留，需镜像核验"
                )
        return None


class L2vpnVisitor(CiscoRuleVisitor):
    """校验 L2VPN 接入和三层网关引用。

    attachment 必须为二层接口，routed interface 必须是有效 BVI，否则导入会失败。
    """

    def __init__(self, session):
        """建立目标父口和接口定义索引。

        预先解析目标范围可区分“作用域缺失”与“无关外部接口”。
        """
        super().__init__(session)
        self.parents = _scope(
            session.document, session.context.target_interfaces
        )
        self.blocks = {
            block.interface_name: block
            for block in interface_nodes(session.document)
        }

    def visit_node(self, node):
        """检查一条 L2VPN 接口引用并继续遍历子树。

        routed 和 attachment 语法分别验证，以产生准确且互不混淆的错误。
        """
        document, outcome = self.session.document, self.session.result
        routed = re.match(r"^routed\s+interface\s+(.+)$", node.header, re.I)
        attachment = re.match(r"^interface\s+(.+)$", node.header, re.I)
        if routed:
            name = document.interfaces.analysis.resolve_interface(
                routed.group(1)
            )
            if (
                document.interfaces.analysis.interface_kind(name)
                != InterfaceKind.GATEWAY
                or interface_unit(name) is not None
            ):
                _record_input_semantic(
                    self.session,
                    outcome,
                    f"L2VPN routed interface 必须引用 BVI，不能引用 {name}",
                )
            elif name not in self.blocks:
                _record_input_semantic(
                    self.session,
                    outcome,
                    f"L2VPN routed interface 引用不存在的 BVI: {name}",
                )
        elif attachment:
            name = document.interfaces.analysis.resolve_interface(
                attachment.group(1)
            )
            target = self.blocks.get(name)
            if target is not None and not _is_l2(target):
                _record_input_semantic(
                    self.session,
                    outcome,
                    f"L2VPN 接入接口 {name} 必须配置 l2transport",
                )
            elif target is None and _in_scope(name, self.parents):
                _record_input_semantic(
                    self.session,
                    outcome,
                    f"L2VPN 引用不存在的目标接入接口: {name}",
                )
        return self.visit_children(node)


class RemovedReferencesVisitor(CiscoRuleVisitor):
    """检查被删除接口在活动配置中的残留引用。

    当前目标复用旧名称时引用仍然合法，因此先扣除现有接口定义再检查。
    """

    def __init__(self, session):
        """计算已删除且未被现有定义复用的接口集合。

        该差集可避免接口迁移到同名目标时产生错误诊断。
        """
        super().__init__(session)
        defined = {
            block.interface_name for block in interface_nodes(session.document)
        }
        defined.update(interface_parent(name) for name in list(defined) if name)
        from .pruning import reference_matcher

        self.deleted = session.context.removed_interfaces - defined
        self.references = reference_matcher(session.document, self.deleted)
        self.deferred = {
            (reference.path, reference.interface)
            for reference in session.context.deferred_interface_references
        }

    def visit_node(self, node):
        """检查当前命令中的残留接口引用。

        描述和备注属于说明文本，不参与接口依赖判断。
        """
        if not node.is_formatting and not re.match(
            r"^(?:description|remark)\b", node.header, re.I
        ):
            refs = self.references(node.header)
            if refs:
                path = (*node.path, node.header)
                unexpected = {
                    name for name in refs if (path, name) not in self.deferred
                }
                if unexpected:
                    self.session.result.errors.append(
                        f"残留已删除接口引用: {' / '.join(path)}; "
                        f"接口 {', '.join(sorted(unexpected))}"
                    )
        return self.visit_children(node)


END_RULE = CiscoVisitorRule("cisco.validate.end", EndVisitor, readonly=True)
SCALAR_VALUES_RULE = CiscoVisitorRule(
    "cisco.validate.scalar_values",
    ScalarValuesVisitor,
    selector=compatibility_interfaces_scope,
    readonly=True,
)
INTERFACE_TYPES_RULE = CiscoVisitorRule(
    "cisco.validate.interface_types",
    InterfaceTypesVisitor,
    selector=compatibility_interfaces_scope,
    readonly=True,
)
BUNDLE_BINDINGS_RULE = CiscoVisitorRule(
    "cisco.validate.bundle_bindings",
    BundleBindingsVisitor,
    selector=compatibility_interfaces_scope,
    readonly=True,
)
COMPATIBLE_COMMANDS_RULE = CiscoVisitorRule(
    "cisco.validate.compatible_commands",
    CompatibleCommandsVisitor,
    selector=compatibility_interfaces_scope,
    readonly=True,
)
L2VPN_RULE = CiscoVisitorRule(
    "cisco.validate.l2vpn", L2vpnVisitor, selector=l2vpn_scope, readonly=True
)
REMOVED_REFERENCES_RULE = CiscoVisitorRule(
    "cisco.validate.removed_references", RemovedReferencesVisitor, readonly=True
)
