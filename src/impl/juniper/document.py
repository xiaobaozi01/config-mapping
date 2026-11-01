"""Juniper Junos 语法模型及面向应用层的兼容门面。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache

from impl.common.config_ast.document import AstDocument
from impl.common.config_ast.model import tree_signature
from impl.common.domain.contracts import VendorConfiguration
from impl.common.domain.interface import (
    BundleMode,
    InterfaceKind,
    InterfaceSpec,
    interface_parent,
)
from impl.common.domain.outcomes import (
    CleanupOutcome,
    GroupExpansionOutcome,
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from impl.common.domain.policies import (
    SimulationAdaptationPolicy,
    WashingPolicy,
)
from impl.common.rules.cleaning import (
    CleaningAction,
    CleaningOperation,
)
from impl.common.support.interface_cache import (
    PARSER_HEADER_CACHE_SIZE,
    InterfaceAnalysisCache,
    copy_interface_specs,
)

from .support.empty_blocks import vmx_empty_block_policy
from .syntax.ast import JuniperNode
from .syntax.parser import JuniperParser
from .syntax.writer import JuniperWriter


def canonical_junos_interface(value: str) -> str:
    """去除 Junos 接口名首尾及内部的无意义空白。

    拓扑表和配置文本可能采用不同空白格式；在查找接口节点、聚合成员和外部引用前
    统一名称，可确保相同接口不会因书写差异而匹配失败。
    """
    return re.sub(r"\s+", "", value.strip())


_JUNOS_PHYSICAL_INTERFACE = re.compile(
    r"^(?:fe|ge|xe|et|mge|so|se|t1|e1|ct|coc|sat|xle)-\d",
    re.IGNORECASE,
)
_JUNOS_VIRTUAL_INTERFACE = re.compile(
    r"^(?:(?:gr|ip|lt|mt|pd|pe|sp|st|vtep|demux|reth)(?:-|\d)|"
    r"(?:dsc|lsi|pimd|pime|tap)$)",
    re.IGNORECASE,
)


def _junos_interface_kind(name: str) -> InterfaceKind:
    """按 Junos 父接口名前缀返回厂商无关的接口类别。

    方法先去掉 unit 后缀，再区分环回、管理、ae、IRB、物理和虚拟接口；无法识别
    时返回 ``UNKNOWN``，而不猜测为物理口，以免迁移新型控制接口或删除解析器尚未
    覆盖的配置。
    """
    parent = interface_parent(name).lower()
    if parent == "lo0":
        return InterfaceKind.LOOPBACK
    if re.fullmatch(r"(?:(?:fxp|em|me)\d+|vme\d*)", parent):
        return InterfaceKind.MANAGEMENT
    if re.fullmatch(r"ae\d+", parent):
        return InterfaceKind.BUNDLE
    if parent == "irb":
        return InterfaceKind.GATEWAY
    if _JUNOS_PHYSICAL_INTERFACE.match(parent):
        return InterfaceKind.PHYSICAL
    if _JUNOS_VIRTUAL_INTERFACE.match(parent):
        return InterfaceKind.VIRTUAL
    return InterfaceKind.UNKNOWN


def _resolved_interface_kind(name: str) -> InterfaceKind:
    """从外部接口写法解析厂商无关类别。

    先规范化名称再分类，使拓扑缩写和配置原文共享同一判断结果且不缓存文档状态。
    """
    return _junos_interface_kind(canonical_junos_interface(name))


def _parse_header_prefixes(header: str) -> tuple[str, bool]:
    """去除 Junos 编辑状态前缀并返回基础语句。

    语义匹配忽略 inactive 和 protect，同时保留 inactive 标志供输出阶段恢复停用状态。
    """
    result = header.strip()
    inactive = False
    while True:
        for prefix in ("inactive:", "protect:"):
            if result.startswith(prefix):
                inactive = inactive or prefix == "inactive:"
                result = result[len(prefix) :].strip()
                break
        else:
            return result, inactive


def _is_unit_header(header: str) -> bool:
    """判断 header 是否表示完整 unit 语句。

    同时接受块和分号结尾的叶子，避免接口树中两种合法写法被分类成不同对象。
    """
    return bool(
        re.fullmatch(r"unit\s+\d+;?", _parse_header_prefixes(header)[0])
    )


def _unit_number_from_header(header: str) -> str:
    """提取 unit 语句中的原始编号。

    保留字符串而非整数，使前导零等原始写法在目标接口名称中保持不变。
    """
    return _parse_header_prefixes(header)[0].rstrip(";").split(maxsplit=1)[1]


def _vlan_from_header(header: str) -> int | None:
    """解析单层 VLAN 或 QinQ 外层 VLAN。

    非 VLAN 语句返回空值，使调用方可以区分未标记接口和 VLAN 0。
    """
    statement = _parse_header_prefixes(header)[0]
    match = re.match(r"vlan-id\s+(\d+)\s*;?$", statement)
    if match:
        return int(match.group(1))
    tags = re.match(r"vlan-tags\s+outer\s+(\d+)\s+inner\s+\d+\s*;?$", statement)
    return int(tags.group(1)) if tags else None


def _inner_vlan_from_header(header: str) -> int | None:
    """解析 QinQ 语句的内层 VLAN。

    只有完整 vlan-tags 结构才返回编号，避免把普通 VLAN 标签误判为双层封装。
    """
    match = re.match(
        r"vlan-tags\s+outer\s+\d+\s+inner\s+(\d+)\s*;?$",
        _parse_header_prefixes(header)[0],
    )
    return int(match.group(1)) if match else None


class _JunosHeaderParser:
    """保存单文档使用的可失效字符串解析缓存。

    缓存只依赖不可变 header 文本，不持有节点或文档，避免配置修订后复用旧解析结果。
    """

    def __init__(self) -> None:
        """为 header 分类操作建立有界缓存。

        缓存容量与接口和策略规模匹配，重复遍历时仍保持可预测内存上限。
        """
        self.canonical_interface = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            canonical_junos_interface
        )
        self.interface_kind = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _resolved_interface_kind
        )
        self.header_prefixes = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _parse_header_prefixes
        )
        self.is_unit = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _is_unit_header
        )
        self.unit_number = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _unit_number_from_header
        )
        self.vlan = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _vlan_from_header
        )
        self.inner_vlan = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _inner_vlan_from_header
        )

    def __deepcopy__(self, _memo: dict[int, object]) -> _JunosHeaderParser:
        """为复制后的文档创建空 header 缓存。

        复制树可能继续独立修改，复用原缓存包装器会返回与当前 header 不一致的结果。
        """
        return type(self)()

    def base_header(self, header: str) -> str:
        """返回用于语义匹配的基础 header。

        复用共享前缀解析缓存，使接口、group 和渲染判断采用完全一致的文本语义。
        """
        return self.header_prefixes(header)[0]

    def output_header(self, header: str) -> str:
        """返回移除 protect 并保留 inactive 的输出 header。

        实验配置不需要编辑保护，但必须继续保留原本停用的节点状态。
        """
        result, inactive = self.header_prefixes(header)
        return f"inactive: {result}" if inactive else result

    def is_effective(self, node: JuniperNode) -> bool:
        """判断节点是否参与当前生效配置。

        header 解析可以缓存，但节点活动和注释会直接变化，因此这些状态始终实时读取。
        """
        return (
            node.active
            and not self.header_prefixes(node.header)[1]
            and not node.is_comment_only
        )


class JunosDocument(AstDocument, VendorConfiguration):
    """提供可修改 Junos 配置树及应用层所需的统一厂商门面。

    类本身负责大括号解析、渲染、声明式清洗规则和引用替换；接口迁移、group、清洗及模拟
    适配委托给独立厂商模块。这样应用层不需要了解 ``JuniperNode``，厂商语法操作也
    能按职责拆分，而仍由一个文档对象维护完整转换状态。
    """

    vendor = "juniper_junos"

    def __init__(self, text: str):
        """把配置解析为唯一的可变文档树。

        该操作在当前文档的业务处理边界内运行，调用方继续负责统一的结果记录和失败处理。
        """
        self._parser = _JunosHeaderParser()
        self._interface_analysis_cache = InterfaceAnalysisCache()
        self._revision = 0
        self.writer = JuniperWriter()
        self.root = JuniperParser().parse(text)
        self._last_signature = tree_signature(self.root)

    def parse_fragment(self, text):
        """在挂接到配置树前解析独立片段。

        片段校验先于 Editor 修改活动树执行，使语法错误不会污染当前配置状态。
        """
        return JuniperParser().parse(text)

    def normalize_header(self, header):
        """移除传入命令头中的语法结束符。

        结构标点由 Parser 和 Writer 处理，不参与业务命令匹配。
        """
        return header.strip().removesuffix(";").rstrip()

    def validate_header(self, node, header):
        """把结构命令校验委托给厂商 Parser。

        Document 门面只选择语法服务，不自行实现语法检查。
        """
        return JuniperParser().validate_header(node, header)

    def hostname(self) -> str | None:
        """返回 ``system`` 块下最后一条有效的 ``host-name`` 语句值，未配置时返回 ``None``。

        Junos 把设备名放在顶层 ``system`` 容器的 ``host-name`` 叶子中，可能带引号；
        方法按文档顺序取最后一条活动语句以反映加载后的最终值，并去除外层引号与
        结尾分号，便于与 Excel 中的设备名直接比较。
        """
        system = self._top_block("system")
        if not system or not system.is_block:
            return None
        value: str | None = None
        for child in system.nodes:
            if not self._parser.is_effective(child) or child.is_block:
                continue
            statement = self._parser.base_header(child.header)
            match = re.match(r"host-name\s+(.+)$", statement, re.IGNORECASE)
            if not match:
                continue
            candidate = match.group(1).strip()
            if candidate.endswith(";"):
                candidate = candidate[:-1].rstrip()
            value = candidate.strip('"').strip("'")
        return value

    @staticmethod
    def _base_header(header: str) -> str:
        """去除 ``inactive:``/``protect:`` 前缀并返回基础语句文本。

        这些前缀描述 Junos 节点状态而不是命令身份；语义匹配时忽略它们，才能让接口
        名、块名和清洗规则稳定识别同一命令。原始 header 仍保留在树中，输出阶段再
        单独决定保留 ``inactive:`` 并移除 ``protect:``。
        """
        return _parse_header_prefixes(header)[0]

    def _top_block(self, name: str, create: bool = False) -> JuniperNode | None:
        """查找指定活动顶层块，并在请求时创建缺失块。

        清洗和账号注入等操作经常需要访问 ``system``、``security`` 或 ``interfaces``；
        统一入口可避免各模块重复遍历根节点，并保证新块只在明确 ``create=True`` 时
        追加，从而不因只读查询意外改变配置。
        """
        for node in self.root.nodes:
            if (
                self._parser.is_effective(node)
                and node.is_block
                and self._parser.base_header(node.header) == name
            ):
                return node
        if create:
            editor = self.edit()
            editor.append_child(
                self.root, f"{name} {{}}", "创建 Junos 配置容器"
            )
            editor.apply()
            return self.root.nodes[-1]
        return None

    def _interfaces_block(self, create: bool = False) -> JuniperNode | None:
        """返回顶层 ``interfaces`` 块，并可按需创建。

        该专用入口让接口操作不必重复硬编码顶层名称，同时继承 ``_top_block`` 的活动
        状态和显式创建语义，保证所有接口修改落在同一个容器中。
        """
        return self._top_block("interfaces", create=create)

    def _interface_nodes(self) -> list[JuniperNode]:
        """返回 ``interfaces`` 下仍有效且具有子树的接口节点。

        过滤内部逻辑删除节点和非块语句，可让接口规格、聚合关系及迁移操作只处理
        真正的接口定义，而不会把注释式叶子或已停用副本当作活动接口。
        """
        block = self._interfaces_block()
        if not block or not block.is_block:
            return []
        return [
            node
            for node in block.nodes
            if self._parser.is_effective(node) and node.is_block
        ]

    def _interface_name(self, node: JuniperNode) -> str:
        """从接口节点 header 的首个字段提取规范化接口名。

        先移除状态前缀再规范化，可使 ``inactive:``、``protect:`` 或空白差异不影响
        同名节点合并和接口查找。
        """
        return self.resolve_interface(
            self._parser.base_header(node.header).split()[0]
        )

    def _unit_nodes(self, interface: JuniperNode) -> list[JuniperNode]:
        """列出接口节点下有效的 ``unit`` 子节点，包括空的叶子 unit。

        Junos 把逻辑接口嵌套在父口内；集中枚举 unit 可让规格提取和 NNI 复制使用
        相同结构判定。``unit 0;`` 也是逻辑接口，不能被当作父口业务复制。
        """
        if not interface.is_block:
            return []
        return [
            node
            for node in interface.nodes
            if self._parser.is_effective(node)
            and self._parser.is_unit(node.header)
        ]

    def _unit_number(self, node: JuniperNode) -> str:
        """从已识别的 ``unit`` 节点头提取逻辑单元编号。

        保留字符串形式可兼容解析阶段尚未限制的输入，并在拼接完整接口名及选择源
        unit 时保持原始编号表达。
        """
        return self._parser.unit_number(node.header)

    def _find_vlan(self, unit: JuniperNode) -> int | None:
        """读取 unit 的单层 VLAN 或 QinQ 外层 VLAN，未配置时返回 ``None``。

        业务分析同时识别 ``vlan-id`` 和
        ``vlan-tags outer``，可让单层与双层终结使用同一 ``InterfaceSpec.vlan``。
        """
        if not unit.is_block:
            return None
        for child in unit.nodes:
            if not self._parser.is_effective(child):
                continue
            vlan = self._parser.vlan(child.header)
            if vlan is not None:
                return vlan
        return None

    def _find_inner_vlan(self, unit: JuniperNode) -> int | None:
        """读取 ``vlan-tags`` 语句中的 QinQ 内层 VLAN。

        内层标签代表原客户业务 VLAN；单层或无标签 unit
        返回 ``None``，让上层按明确的回退规则选择内层标签。
        """
        if not unit.is_block:
            return None
        for child in unit.nodes:
            if not self._parser.is_effective(child):
                continue
            vlan = self._parser.inner_vlan(child.header)
            if vlan is not None:
                return vlan
        return None

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """展开全部已应用 Junos groups，并返回事件、冲突及成败信息。

        group 继承必须在接口分类前物化，但 apply-groups、通配符及优先级处理不属于
        基础语法树职责；委托专用展开器可隔离复杂语义，并让调用方据结果决定是否继续。
        """
        from .operations.groups import JunosGroupExpander

        return JunosGroupExpander(self).expand_groups(known_interfaces, policy)

    def interface_specs(self) -> list[InterfaceSpec]:
        """提取父接口或 unit 的关系、类型及 VLAN/QinQ 信息。

        返回厂商无关的 ``InterfaceSpec``，使 NNI 规划无需遍历 Junos 节点；统一
        规格也让业务筛选、VLAN 分配和审计映射共享同一事实来源。
        """
        return self._interface_analysis_cache.read(
            self.root,
            "interface_specs",
            lambda: self._interfaces().interface_specs(self),
            copy_interface_specs,
        )

    @staticmethod
    def _interfaces():
        """延迟返回 Juniper 接口操作模块。

        接口模块需要引用 ``JunosDocument`` 和 ``JuniperNode``；在方法调用时再导入可
        打破模块初始化阶段的循环依赖，同时保持文档类对外提供稳定门面。
        """
        from .operations import interfaces

        return interfaces

    def interface_kind(self, name: str) -> InterfaceKind:
        """按 Junos 规则返回指定接口的厂商无关类别。

        拓扑预检和配置扫描都通过同一入口分类，可避免对物理口、ae、IRB 或控制接口
        产生不同判断，从而阻止不合法端点参与 NNI 映射。
        """
        return self._parser.interface_kind(name)

    def bundle_members(self) -> dict[str, str]:
        """返回物理接口到 ``ae`` 聚合父口的映射。

        NNI 扁平化需要从拓扑物理端点反查真正承载业务的逻辑聚合口，并在迁移后
        清理原成员，因此统一暴露该方向的关系。
        """
        return self._interface_analysis_cache.read(
            self.root,
            "bundle_members",
            lambda: self._interfaces().bundle_members(self),
            dict,
        )

    def interface_has_business(self, name: str) -> bool:
        """判断物理聚合成员是否含 unit、family 等独立业务。

        该结果用于让成员退出压缩，而不是判断源配置是否正确。
        """
        return self._interfaces().interface_has_business(self, name)

    def bundle_binding_is_ambiguous(self, name: str) -> bool:
        """判断物理成员是否声明了多个不同 ae 绑定。

        多个绑定无法唯一映射，仍作为转换错误处理。
        """
        return self._interfaces().bundle_binding_is_ambiguous(self, name)

    def resolve_interface(self, value: str) -> str:
        """把拓扑中的 Junos 接口写法转换为解析器的规范形式。

        所有查找先经过统一规范化，可消除空白差异，避免同一接口在拓扑和配置树之间
        匹配失败或被重复处理。
        """
        return self._parser.canonical_interface(value)

    def logical_names_under(self, parent: str) -> list[str]:
        """列出指定父接口对应的已配置业务名称或全部 unit 名称。

        NNI 克隆和映射审计需要逐个保留逻辑单元后缀；集中枚举可避免处理器自行遍历
        Junos 子树，并保证无 unit 父口与有 unit 接口使用一致入口。
        """
        return self._interfaces().logical_names_under(self, parent)

    def business_interface_names(self) -> set[str]:
        """识别承载三层、二层及活跃广播域网关的 Junos 接口。

        IRB 不因自身配置地址就自动迁移；只有 bridge-domain/vlan 中仍有
        活跃接入口，或其 unit 命中活跃业务 VLAN 时才视为关联业务。
        """
        return self._interface_analysis_cache.read(
            self.root,
            "business_interface_names",
            lambda: self._interfaces().business_interface_names(self),
            set,
        )

    def _find_interface_node(self, name: str) -> JuniperNode | None:
        """按父接口名查找第一个有效 Junos 接口节点。

        unit 并非顶层节点，因此查找会归一到父口；委托接口模块可确保调用方
        复用相同的规范化和活动状态规则。
        """
        return self._interfaces().find_interface_node(self, name)

    def remove_interface(
        self, name: str, include_children: bool = False
    ) -> None:
        """逻辑停用接口所在的整个 Junos 父节点及其 unit 子树。

        Junos unit 天然嵌套在父口中，因此删除父节点即可覆盖子接口；保留
        ``include_children`` 参数是为了实现统一厂商协议，而不需要伪造独立 unit 删除。
        """
        with self.edit().bulk():
            self._interfaces().remove_interface(self, name, include_children)

    def rename_interface_tree(
        self, source: str, target: str, strip_bundle: bool = False
    ) -> None:
        """重命名整个 Junos 接口节点，并可移除聚合相关 options。

        父节点改名会自然携带全部 unit，适合 NNI 暂存与最终落位；迁出 ae 成员时
        清除旧 options，可避免目标物理口继续引用已不存在的聚合关系。
        """
        with self.edit().bulk():
            self._interfaces().rename_interface_tree(
                self, source, target, strip_bundle
            )

    def clone_interface_tree(
        self, source: str, target: str, strip_bundle: bool = False
    ) -> None:
        """把一棵 Junos 接口子树深拷贝到新目标接口。

        M-LAG 可能要求同一 ae 业务按多个对端拆成多份，不能直接改名并消耗唯一源树；
        克隆保留源配置供后续目标继续复制，``strip_bundle`` 防止旧成员选项泄漏。
        """
        with self.edit().bulk():
            self._interfaces().clone_interface_tree(
                self, source, target, strip_bundle
            )

    def _merge_duplicate_interface(self, preferred: JuniperNode) -> None:
        """把同名接口节点的非重复子树合并到首选节点并停用副本。

        接口改名或克隆可能撞上已有目标定义；按渲染结果去重可保留双方不同配置，
        又能确保最终 ``interfaces`` 下只有一个活动的同名节点。
        """
        self._interfaces()._merge_duplicate_interface(self, preferred)

    def ensure_parent_interface(self, name: str) -> None:
        """确保 NNI 承载父接口存在。

        已有接口保持原 unit 和封装，仅有缺失时创建空父节点，避免伪造业务配置。
        """
        with self.edit().bulk():
            self._interfaces().ensure_parent_interface(self, name)

    def bundle_mode(self, member: str) -> BundleMode:
        """读取聚合成员的原协商方式。

        无法确认成员绑定或 LACP 模式时返回 unknown，使迁移流程选择保守失败。
        """
        from .operations.bundles import bundle_mode

        return bundle_mode(self, member)

    def adapt_bundle(self, name: str) -> InterfaceAdaptationOutcome:
        """保留逻辑聚合并按单成员实验调整已有门限。

        只改模拟不兼容的成员数和带宽门槛，保留主口类型、地址及引用。
        """
        from .operations.bundles import adapt_bundle

        with self.edit().bulk():
            outcome = adapt_bundle(self, name)
        return outcome

    def validate_nni_clone(
        self,
        source: str,
        target_interfaces: set[str],
    ) -> InterfaceAdaptationOutcome:
        """在源接口复制前检查三层地址和单值引用。

        一对多目标无法等价承载单值配置，因此校验必须先于任何克隆或改名。
        """
        from .support.clone_constraints import validate_nni_clone

        return validate_nni_clone(self, source, target_interfaces)

    def prune_non_nni(
        self,
        protected_parents: set[str],
        *,
        strict_input_validation: bool = True,
    ) -> InterfacePruningOutcome:
        """基于原始接口身份删除非 NNI 接入接口及明确依赖。

        保护集合来自拓扑原始名称，避免迁移后的名称变化扩大或缩小清理范围。
        """
        from .rules.pruning import prune_non_nni

        outcome = prune_non_nni(
            self,
            protected_parents,
            strict_input_validation=strict_input_validation,
        )
        return outcome

    def prune_interface_dependencies(
        self,
        removed_interfaces: set[str],
        *,
        strict_input_validation: bool = True,
    ) -> InterfacePruningOutcome:
        """清理聚合缩减后已删除成员口的可识别引用。

        只处理指定成员相关依赖，不重新执行全局非 NNI 删除，避免影响其他业务。
        """
        from .rules.pruning import prune_interface_dependencies

        with self.edit().bulk():
            outcome = prune_interface_dependencies(
                self,
                removed_interfaces,
                strict_input_validation=strict_input_validation,
            )
        return outcome

    def adapt_interfaces(
        self, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """执行已注册的 Junos 接口适配规则。

        统一入口让后续规则扩展无需修改文档门面，同时默认保留现有 unit 语义。
        """
        from .rules import adapt_interfaces

        return adapt_interfaces(self, target_interfaces)

    def validate_source_semantics(
        self, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """在映射前校验 Junos 源配置语义。

        目标引用完整性和删除引用校验由最终阶段负责。
        """
        from .rules import validate_source_semantics

        return validate_source_semantics(self, target_interfaces)

    def validate_final_integrity(
        self,
        target_interfaces: set[str],
        removed_interfaces: set[str],
        deferred_interface_references=(),
    ) -> InterfaceAdaptationOutcome:
        """校验适配产物的引用完整性。

        unit 和网关角色等源配置语义已在前置阶段处理。
        """
        from .rules import validate_final_integrity

        return validate_final_integrity(
            self,
            target_interfaces,
            removed_interfaces,
            deferred_interface_references,
        )

    def validate_interfaces(
        self,
        target_interfaces: set[str],
        removed_interfaces: set[str],
        deferred_interface_references=(),
        *,
        strict_input_validation=True,
    ) -> InterfaceAdaptationOutcome:
        """执行最终接口引用、网关和 unit 约束检查。

        校验在全部迁移和清理之后运行，只报告不允许输出的残留问题。
        """
        from .rules import validate_interfaces

        return validate_interfaces(
            self,
            target_interfaces,
            removed_interfaces,
            deferred_interface_references,
            strict_input_validation=strict_input_validation,
        )

    def adapt_to_simulation(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """按镜像策略适配已迁移数据口及 Junos 模拟运行参数。

        适配逻辑与基础 AST 分离，可针对不同 vMX 镜像处理接口启停、物理选项或稳定性
        参数；返回分类统计则让上层报告完整说明自动修改的范围。
        """
        from .rules.simulation import adapt_to_simulation

        outcome = adapt_to_simulation(self, policy, data_interfaces)
        return outcome

    def clean_management_access(self) -> CleanupOutcome:
        """清除 Junos 原有管理访问和认证配置，并返回分类统计。

        生产账号、外部 AAA、SNMP 和远程管理凭据不应进入实验环境；委托独立清洗
        模块可集中维护敏感节点规则，同时让文档门面保持统一调用接口。
        """
        from .operations.cleaning import clean_management_access

        return clean_management_access(self)

    def add_lab_account(self) -> None:
        """向 Junos 文档添加实验用 root 认证信息。

        清除生产认证后必须建立已知登录入口，防止输出设备无法接管；账号节点由厂商
        清洗模块生成，使凭据替换与旧认证删除使用同一实现边界。
        """
        from .operations.cleaning import add_lab_account

        add_lab_account(self)

    def apply_cleaning_rule(
        self,
        path: tuple[str, ...],
        match: str,
        action: CleaningAction,
        value: str | None,
    ) -> int:
        """通过批量执行器运行一条清洗操作。

        单条入口复用批量编译和审计路径，避免维护两套匹配语义。
        """
        action = CleaningAction(action)
        if action == CleaningAction.REPLACE and value is None:
            value = ""
        return self.apply_cleaning_rules(
            [CleaningOperation(path, match, action, value)]
        )[0]

    def apply_cleaning_rules(
        self, operations: Sequence[CleaningOperation]
    ) -> list[int]:
        """把有序清洗委托给厂商清洗服务。

        服务负责匹配和遍历，Document 只保留稳定入口。
        """
        from .operations.configuration_cleaning import apply_cleaning_rules

        return apply_cleaning_rules(self, operations)

    def replace_references(self, replacements: dict[str, list[str]]) -> None:
        """把接口引用转换委托给引用改写服务。

        服务保留接口定义名，只展开其他位置对这些接口的引用。
        """
        from .operations.references import replace_references

        replace_references(self, replacements)

    def render(self) -> str:
        """按目标部署镜像的空块策略序列化配置树。

        部署输出可以省略或改变空块形式，但不会修改 Document 当前持有的配置。
        """
        return self.writer.render(
            self.root, self._parser, vmx_empty_block_policy()
        )
