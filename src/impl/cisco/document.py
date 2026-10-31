"""ANTLR 驱动的 IOS XR 文档门面；当前源码与解析快照是唯一配置状态。"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable, Sequence

from .antlr.context import CiscoContext
from .antlr.parsing import CiscoSnapshot
from .antlr.rewriting import RewriteTransaction
from .empty_blocks import xrv9000_empty_block_policy
from impl.common.cleaning import CleaningOperation, compile_cleaning_rules
from impl.common.contracts import VendorConfiguration
from impl.common.interface import InterfaceKind, InterfaceSpec, interface_parent
from impl.common.interface_cache import copy_interface_specs
from impl.common.outcomes import CleanupOutcome, GroupExpansionOutcome, InterfaceAdaptationOutcome, InterfacePruningOutcome, SimulationAdaptationOutcome
from impl.common.policies import SimulationAdaptationPolicy, WashingPolicy

def _is_preconfigured_interface_header(header: str) -> bool:
    """按 header 判断接口是否处于预配置模式。"""
    return bool(re.match(r"^interface\s+preconfigure(?:\s|$)", header, re.IGNORECASE))


def _is_l2transport_header(header: str) -> bool:
    """按 header 判断接口是否显式启用二层模式。"""
    return bool(re.match(r"interface\s+.+\s+l2transport\s*$", header, re.IGNORECASE))


def _is_group_header(header: str) -> bool:
    """按 header 判断顶层命令是否为 group 定义。"""
    return bool(re.match(r"group\s+\S+", header, re.IGNORECASE))


def _interface_name_from_header(header: str) -> str | None:
    """从不可变 header 解析接口名，缓存由 Document 实例持有。"""
    if not header.lower().startswith("interface"):
        return None
    match = re.match(r"^interface\s+(.+?)\s*$", header, re.IGNORECASE)
    if not match:
        return None
    value = match.group(1)
    if _is_preconfigured_interface_header(header):
        preconfigured = re.match(
            r"^interface\s+preconfigure(?:\s+(.+?))?\s*$",
            header,
            re.IGNORECASE,
        )
        value = preconfigured.group(1) if preconfigured else None
        if not value:
            return None
    value = re.sub(r"\s+l2transport\s*$", "", value, flags=re.IGNORECASE)
    return canonical_cisco_interface(value)


def _cisco_command_identity(
    command: str,
    has_children: bool = False,
    path: list[str] | None = None,
) -> str:
    """把旧式命令参数转换成 Cisco group 语义标识键。

    方法委托统一 identity 解析器，并根据 ``has_children`` 区分块与叶子节点；保留
    该包装层是为了兼容旧内部调用，同时确保新旧入口采用相同的冲突判定规则。
    """
    from .identity import resolve_cisco_identity

    return resolve_cisco_identity(
        command,
        path=path or [],
        node_kind="block" if has_children else "leaf",
    ).key


# 拓扑中经常使用接口缩写，统一展开后才能与配置块可靠匹配。
_CISCO_PREFIXES = {
    "gi": "GigabitEthernet",
    "gigabitethernet": "GigabitEthernet",
    "te": "TenGigE",
    "tengige": "TenGigE",
    "hu": "HundredGigE",
    "hundredgige": "HundredGigE",
    "fo": "FortyGigE",
    "fortygige": "FortyGigE",
    "tf": "TwentyFiveGigE",
    "twentyfivegige": "TwentyFiveGigE",
    "be": "Bundle-Ether",
    "bundle-ether": "Bundle-Ether",
    "lo": "Loopback",
    "loopback": "Loopback",
    "mgmteth": "MgmtEth",
}

_CISCO_INTERFACE_TOKEN = re.compile(
    r"(?<![\w./-])(?:[A-Za-z][A-Za-z-]*\s*\d[\d/]*(?:\.\d+)?)(?![\w./-])"
)


def canonical_cisco_interface(value: str) -> str:
    """清理接口名空白、移除头部模式后缀并展开常见 IOS XR 缩写。

    拓扑表可能使用 ``Gi``、``Te`` 或 ``BE``，配置中则通常使用完整名称；统一成
    同一种表示后，端点校验、聚合关系查询和接口树改名才能可靠匹配同一接口。
    """
    value = re.sub(r"\s+l2transport\s*$", "", value.strip(), flags=re.IGNORECASE)
    compact = re.sub(r"\s+", "", value.strip())
    match = re.match(r"([A-Za-z-]+)(.*)", compact)
    if not match:
        return compact
    prefix, suffix = match.groups()
    expanded = _CISCO_PREFIXES.get(prefix.lower(), prefix)
    return f"{expanded}{suffix}"


_CISCO_PHYSICAL_INTERFACE = re.compile(
    r"^(?:Ethernet|FastEthernet|GigabitEthernet|TenGigE|TwentyFiveGigE|"
    r"FortyGigE|FiftyGigE|HundredGigE|FourHundredGigE|POS|Serial)\d",
    re.IGNORECASE,
)
_CISCO_VIRTUAL_INTERFACE = re.compile(
    r"^(?:Tunnel(?:-ip|-te)?|Null|PW-Ether|VASI(?:Left|Right)?|NVE|Multilink)\d",
    re.IGNORECASE,
)
_CISCO_BLOCK_HEADER = re.compile(
    r"^(?:interface|router|vrf|l2vpn|mpls|username|line|"
    r"segment-routing|telemetry)\b",
    re.IGNORECASE,
)


def _is_cisco_block_header(header: str) -> bool:
    """判断顶层命令是否天然表示可进入的配置块。

    空接口、空路由进程等节点没有 children，但仍需保留块语义，以便
    group 展开可以进入该路径并物化继承配置。
    """
    return bool(_CISCO_BLOCK_HEADER.match(header.strip()))


def _cisco_interface_kind(name: str) -> InterfaceKind:
    """按 IOS XR 父接口名前缀返回厂商无关的接口类别。

    方法先去掉子接口后缀，再区分环回、管理、Bundle、BVI、物理和虚拟接口；无法
    识别时返回 ``UNKNOWN``，而不乐观地当作物理口，以免迁移新型控制接口或删除
    解析器尚不了解的配置。
    """
    parent = interface_parent(name).lower()
    if re.fullmatch(r"loopback\d+", parent):
        return InterfaceKind.LOOPBACK
    if re.match(r"^mgmteth\d", parent):
        return InterfaceKind.MANAGEMENT
    if re.fullmatch(r"bundle-ether\d+", parent):
        return InterfaceKind.BUNDLE
    if re.fullmatch(r"bvi\d+", parent):
        return InterfaceKind.GATEWAY
    if re.match(r"^ptp(?:\d|$)", parent, re.IGNORECASE):
        return InterfaceKind.VIRTUAL
    if _CISCO_PHYSICAL_INTERFACE.match(parent):
        return InterfaceKind.PHYSICAL
    if _CISCO_VIRTUAL_INTERFACE.match(parent):
        return InterfaceKind.VIRTUAL
    return InterfaceKind.UNKNOWN


def _resolved_interface_kind(name: str) -> InterfaceKind:
    """从外部接口写法解析类别，不在模块级保存配置数据。"""
    return _cisco_interface_kind(canonical_cisco_interface(name))


class _CiscoHeaderParser:
    """单个文档持有的字符串解析器，缓存不引用文档或可修改节点。"""

    def __init__(self) -> None:
        self.canonical_interface = lru_cache(maxsize=32_768)(canonical_cisco_interface)
        self.interface_kind = lru_cache(maxsize=32_768)(_resolved_interface_kind)
        self.interface_name = lru_cache(maxsize=32_768)(_interface_name_from_header)
        self.l2transport = lru_cache(maxsize=32_768)(_is_l2transport_header)
        self.is_group = lru_cache(maxsize=32_768)(_is_group_header)

    def __deepcopy__(self, memo: dict[int, object]) -> _CiscoHeaderParser:
        """复制文档时创建空缓存，避免 deepcopy 复用 functools 的包装器。"""
        return type(self)()


class CiscoDocument(VendorConfiguration):
    vendor = "cisco_iosxr"

    def __init__(self, text: str):
        self._parser = _CiscoHeaderParser()
        self._interface_analysis_cache = _CiscoAnalysisCache()
        self._snapshot = CiscoSnapshot(text)

    @property
    def snapshot(self):
        return self._snapshot

    @property
    def source(self):
        return self.snapshot.text

    @property
    def revision(self):
        return self.snapshot.revision

    @property
    def root(self):
        return self.snapshot.root

    def _publish(self, snapshot):
        self._snapshot = snapshot
        self._interface_analysis_cache = _CiscoAnalysisCache()

    def rewrite(self):
        return RewriteTransaction(self)

    def __deepcopy__(self, memo):
        from .antlr.metadata import retain_block_modes
        cloned = type(self)(self.source)
        retain_block_modes(self.snapshot, cloned.snapshot, (), set())
        return cloned

    def hostname(self):
        values = [match.group(1).strip().strip('"\'') for node in self.root.nodes
                  if (match := re.match(r"hostname\s+(.+)$", node.header, re.I))]
        return values[-1] if values else None

    def _interface_nodes(self) -> list[CiscoContext]:
        """返回仍有效且已识别接口名的 IOS XR 顶层节点。

        具体筛选委托接口模块，保证兼容旧内部入口的同时，让所有调用方复用同一套
        接口识别规则，每次返回当前修订的 Context。
        """
        return self._interfaces().interface_nodes(self)

    @staticmethod
    def _interfaces():
        """延迟返回 Cisco 接口操作模块。

        接口模块需要引用 ``CiscoDocument`` 和 ``CiscoContext``；在方法调用时再导入
        可打破模块初始化阶段的循环依赖，同时保持文档类对外提供稳定门面。
        """
        from . import interfaces

        return interfaces

    def interface_specs(self) -> list[InterfaceSpec]:
        """提取接口父子关系、类型及 VLAN/QinQ 信息。

        返回厂商无关的 ``InterfaceSpec``，使 NNI 规划无需理解 IOS XR 配置行；
        统一规格也是业务口筛选、VLAN 分配和审计映射共享同一事实来源的基础。
        """
        return self._interface_analysis_cache.read(
            self.snapshot,
            "interface_specs",
            lambda: self._interfaces().interface_specs(self),
            copy_interface_specs,
        )

    def interface_kind(self, name: str) -> InterfaceKind:
        """按 IOS XR 规则返回指定接口的厂商无关类别。

        拓扑预检和配置扫描都通过同一入口分类，可避免一处把接口视为物理口、另一处
        又视为虚拟口，从而防止不合法端点参与 NNI 映射。
        """
        return self._parser.interface_kind(name)

    def bundle_members(self) -> dict[str, str]:
        """返回物理成员接口到 ``Bundle-Ether`` 聚合父口的映射。

        NNI 扁平化需要从拓扑物理端点反查承载业务的逻辑聚合口，并在迁移完成后
        清理真实成员，因此统一暴露该方向的映射。
        """
        return self._interface_analysis_cache.read(
            self.snapshot,
            "bundle_members",
            lambda: self._interfaces().bundle_members(self),
            dict,
        )

    def resolve_interface(self, value: str) -> str:
        """把拓扑中的 IOS XR 接口写法转换成配置解析器的规范形式。

        拓扑数据和设备配置可能使用不同缩写或空白格式；在所有查找前统一名称可避免
        同一接口被误认为两个端点，或因匹配失败而遗漏聚合关系。
        """
        return self._parser.canonical_interface(value)

    def logical_names_under(self, parent: str) -> list[str]:
        """列出指定父接口自身及其所有已配置子接口名称。

        NNI 克隆和映射审计必须同时覆盖父口与每个子接口；集中枚举可保留子接口后缀，
        并避免各处理器自行扫描块模型产生不一致结果。
        """
        return self._interfaces().logical_names_under(self, parent)

    def business_interface_names(self) -> set[str]:
        """识别真正承载三层或二层业务的 IOS XR 接口。

        直接业务包括 IPv4/IPv6、l2transport、xconnect 等；此外，
        被 L2VPN、bridge-domain、路由协议等全局配置引用的接口也视为活跃业务口。
        BVI 不因自身有 IP 或编号类似 VLAN 就被视为活跃，只有显式关联它的
        bridge-domain 仍包含活跃 attachment circuit 时才作为活跃网关。
        """
        return self._interface_analysis_cache.read(
            self.snapshot,
            "business_interface_names",
            lambda: self._interfaces().business_interface_names(self),
            set,
        )

    def _find_interface_node(self, name: str) -> CiscoContext | None:
        """按规范化名称查找第一个有效 IOS XR 接口块。

        这是保留给旧内部调用的块级入口；统一委托接口模块可确保查找忽略已停用块，
        避免后续修改落到不会被渲染的旧定义上。
        """
        return self._interfaces().find_interface_node(self, name)

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """展开全部已应用 IOS XR group，并返回事件、冲突及成败信息。

        group 继承必须在接口分类前物化，但其语义远比基础块解析复杂；委托专用展开器
        可以隔离冲突处理，并让调用方通过结果对象决定是否安全继续转换。
        """
        from .groups import CiscoGroupExpander

        return CiscoGroupExpander(self).expand_groups(known_interfaces, policy)

    def remove_interface(self, name: str, include_children: bool = False) -> None:
        """逻辑停用指定接口，并可连同全部点号子接口一起停用。

        接口模块通过活动标记保留原块而非直接删除，便于维持文档顺序；父口迁移结束
        时使用 ``include_children`` 可一次清理整棵旧接口树。
        """
        self._interfaces().remove_interface(self, name, include_children)

    def rename_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """重命名源接口树，并可移除不再适用的聚合成员属性。

        NNI 暂存与最终落位都需要父口和子接口保持相同后缀；统一树级改名还能在
        目标重名时复用接口模块的合并规则，避免重复定义。
        """
        self._interfaces().rename_interface_tree(self, source, target, strip_bundle)

    def clone_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """把一棵 IOS XR 接口配置深拷贝到新目标接口。

        M-LAG 可能要求同一逻辑聚合按多个对端拆成多份，不能直接改名并消耗唯一源树；
        克隆保留源配置供后续目标继续复制，``strip_bundle`` 则防止旧成员属性泄漏。
        """
        self._interfaces().clone_interface_tree(self, source, target, strip_bundle)

    def _merge_duplicate_interface(self, preferred: CiscoContext) -> None:
        """把同名接口块的非重复配置子树合并到首选块。

        接口改名或克隆可能撞上已有目标定义；合并而非覆盖可以保留双方有效配置，
        同时停用多余块，确保最终 IOS XR 输出只有一个活动接口定义。
        """
        self._interfaces()._merge_duplicate_interface(self, preferred)

    def bundle_mode(self, member: str) -> str:
        """读取成员原协商方式，unknown 表示无法安全判定。"""
        from .bundles import bundle_mode
        return bundle_mode(self, member)

    def adapt_bundle(self, name: str) -> InterfaceAdaptationOutcome:
        """保留逻辑聚合，按单成员实验调整已有门限。"""
        from .bundles import adapt_bundle
        return adapt_bundle(self, name)

    def validate_nni_clone(
        self, source: str, target_interfaces: set[str],
    ) -> InterfaceAdaptationOutcome:
        """在源树复制前检查三层地址及单值协议源引用。"""
        from .clone_constraints import validate_nni_clone

        return validate_nni_clone(self, source, target_interfaces)

    def ensure_parent_interface(self, name: str) -> None:
        """确保 NNI 承载父接口存在，不改变现有封装。"""
        self._interfaces().ensure_parent_interface(self, name)

    def prune_non_nni(self, protected_parents: set[str]) -> InterfacePruningOutcome:
        """基于原始接口名裁剪接入接口及其可识别依赖。"""
        from .rules.pruning import prune_non_nni

        return prune_non_nni(self, protected_parents)

    def prune_interface_dependencies(self, removed_interfaces: set[str]) -> InterfacePruningOutcome:
        """清理 NNI 聚合缩减后已删除成员口的可识别引用。"""
        from .rules.pruning import prune_interface_dependencies

        return prune_interface_dependencies(self, removed_interfaces)

    def adapt_interfaces(self, target_interfaces: set[str]) -> InterfaceAdaptationOutcome:
        """按目标接口类型规范化迁移后的配置。"""
        from .rules.compatibility import adapt_interfaces

        return adapt_interfaces(self, target_interfaces)

    def validate_interfaces(
        self, target_interfaces: set[str], removed_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """执行全部接口约束及删除引用校验规则，不修改文档。"""
        from .rules import validate_interfaces

        return validate_interfaces(self, target_interfaces, removed_interfaces)

    def adapt_to_simulation(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """按镜像策略适配已迁移数据口及 IOS XR 模拟运行参数。

        适配逻辑与基础语法解析分离，可针对不同镜像处理接口启停、物理特性或稳定性
        参数；返回分类统计则让上层报告完整说明自动修改的范围。
        """
        from .rules.simulation import adapt_to_simulation

        return adapt_to_simulation(self, policy, data_interfaces)

    def adjust_simulation_parameters(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """通过旧方法名调用当前的模拟参数适配入口。

        保留该别名可避免既有调用方立即失效，同时只委托新方法，不复制实现，确保
        两个入口始终使用同一策略和统计行为。
        """
        return self.adapt_to_simulation(policy, data_interfaces)

    def clean_management_access(self) -> CleanupOutcome:
        """清除 IOS XR 原有管理访问和认证配置，并返回分类统计。

        生产账号、AAA 和远程管理凭据不应进入实验环境；委托独立清洗模块可以集中
        维护敏感命令模式，同时让文档门面保持统一调用接口。
        """
        from .cleaning import clean_management_access

        return clean_management_access(self)

    def add_lab_account(self) -> None:
        """向 IOS XR 文档添加统一实验账号。

        清除生产认证后必须建立已知的本地登录入口，防止输出设备无法接管；账号语法
        由厂商清洗模块生成，使凭据替换与旧认证删除使用同一实现边界。
        """
        from .cleaning import add_lab_account

        add_lab_account(self)

    def apply_cleaning_rule(self, path, match, action, value):
        if action == "replace" and value is None:
            value = ""
        return self.apply_cleaning_rules([CleaningOperation(path, match, action, value)])[0]

    def apply_cleaning_rules(self, operations: Sequence[CleaningOperation]) -> list[int]:
        hits = [0] * len(operations)
        for phase in compile_cleaning_rules(operations):
            transaction = self.rewrite()
            for block in self.root.nodes:
                _clean_phase(transaction, block, (), block.interface_name, phase, hits)
            transaction.commit()
        return hits

    def replace_references(self, replacements: dict[str, list[str]]) -> None:
        """从当前快照展开接口引用，避免把新目标再次当作源名称改写。"""
        canonical = {
            self.resolve_interface(source): list(dict.fromkeys(targets))
            for source, targets in replacements.items() if targets and targets != [source]
        }
        if not canonical:
            return
        source_keys = {name.lower(): name for name in canonical}

        def expand(value):
            matches = [match for match in _CISCO_INTERFACE_TOKEN.finditer(value)
                       if self.resolve_interface(match.group()).lower() in source_keys]
            variants, position = [""], 0
            for match in matches:
                targets = canonical[source_keys[self.resolve_interface(match.group()).lower()]]
                variants = [current + value[position:match.start()] + target for current in variants for target in targets]
                position = match.end()
            return list(dict.fromkeys(current + value[position:] for current in variants))

        transaction = self.rewrite()
        for node in self.root.nodes:
            _rewrite_reference_node(transaction, node, expand, skip_header=node.interface_name is not None)
        transaction.commit()

    def render(self) -> str:
        """按镜像策略整理最终输出，不修改当前源码或解析快照。"""
        transaction = self.rewrite()
        _plan_rendering(transaction, self.root.nodes, (), xrv9000_empty_block_policy())
        return transaction.preview()


def _expanded_reference_text(node, expand, header):
    lines = [" " * node.indent + header]
    for child in node.nodes:
        if child.is_formatting:
            lines.extend(child.text.rstrip("\r\n").splitlines())
        else:
            for variant in expand(child.header):
                lines.extend(_expanded_reference_text(child, expand, variant).rstrip("\r\n").splitlines())
    return "\n".join(lines) + "\n"


def _rewrite_reference_node(transaction, node, expand, *, skip_header=False):
    if node.is_formatting:
        return
    headers = [node.header] if skip_header else expand(node.header)
    if len(headers) > 1:
        text = "".join(_expanded_reference_text(node, expand, header) for header in headers)
        transaction.replace_subtree(node, text, "展开一对多接口引用")
        return
    if headers[0] != node.header:
        transaction.replace_header(node, headers[0], "改写接口引用")
    for child in node.nodes:
        _rewrite_reference_node(transaction, child, expand)


def _omit_empty_block(node, ancestors, policy):
    if node.is_formatting:
        return True
    path = (*ancestors, node.header.strip())
    children_empty = all(_omit_empty_block(child, path, policy) for child in node.nodes)
    return node.is_block and children_empty and policy.action_for(path) == "omit"


def _plan_rendering(transaction, nodes, ancestors, policy):
    for node in nodes:
        if node.is_formatting:
            continue
        if _omit_empty_block(node, ancestors, policy):
            transaction.delete(node, "按镜像策略省略空块")
        else:
            _plan_rendering(transaction, node.nodes, (*ancestors, node.header), policy)


def _clean_phase(transaction, node, ancestors, interface, phase, hits):
    if node.is_formatting:
        return
    header = node.header
    for operation in phase.candidates(ancestors):
        if operation.interface_names is not None and interface not in operation.interface_names:
            continue
        if not operation.pattern.search(header):
            continue
        hits[operation.index] += 1
        if phase.action == "delete":
            transaction.delete(node, "声明式清洗删除")
            return
        header = operation.pattern.sub(operation.value or "", header)
        transaction.replace_header(node, header, "声明式清洗替换")
        break
    for child in node.nodes:
        _clean_phase(transaction, child, (*ancestors, header), interface, phase, hits)


class _CiscoAnalysisCache:
    def __init__(self):
        self.results = {}

    def read(self, snapshot, query, collect, copy_result):
        if query not in self.results:
            self.results[query] = collect()
        return copy_result(self.results[query])
