"""ANTLR 驱动的 IOS XR 文档门面；当前源码与解析快照是唯一配置状态。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache

from impl.common.cleaning import (
    CleaningAction,
    CleaningOperation,
    compile_cleaning_rules,
)
from impl.common.contracts import VendorConfiguration
from impl.common.empty_blocks import EmptyBlockAction
from impl.common.interface import (
    BundleMode,
    InterfaceKind,
    InterfaceSpec,
    interface_parent,
)
from impl.common.interface_cache import (
    PARSER_HEADER_CACHE_SIZE,
    copy_interface_specs,
)
from impl.common.outcomes import (
    CleanupOutcome,
    DeferredInterfaceReference,
    GroupExpansionOutcome,
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from impl.common.policies import SimulationAdaptationPolicy, WashingPolicy

from .antlr.context import CiscoContext
from .antlr.parsing import CiscoSnapshot
from .antlr.rewriting import RewriteTransaction
from .empty_blocks import xrv9000_empty_block_policy


def _is_preconfigured_interface_header(header: str) -> bool:
    """判断接口头是否声明为 ``preconfigure`` 候选。

    该模式表示硬件尚未实例化，必须在接口分类前识别并排除。
    """
    return bool(
        re.match(r"^interface\s+preconfigure(?:\s|$)", header, re.IGNORECASE)
    )


def _is_l2transport_header(header: str) -> bool:
    """判断接口头是否以 ``l2transport`` 结尾。

    只匹配行内模式可快速区分二层接口，同时不把子命令中的同名文本视为接口模式。
    """
    return bool(
        re.match(r"interface\s+.+\s+l2transport\s*$", header, re.IGNORECASE)
    )


def _interface_name_from_header(header: str) -> str | None:
    """从不可变接口头提取规范化接口名。

    统一处理预配置和 l2transport 后缀，使缓存键不受等价书写差异影响。
    """
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
    value = re.sub(
        r"\s+l2transport\s*$", "", value.strip(), flags=re.IGNORECASE
    )
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
    r"^(?:Tunnel(?:-ip|-te)?|Null|PW-Ether|VASI(?:Left|Right)?|"
    r"NVE|Multilink)\d",
    re.IGNORECASE,
)


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
    """先把外部接口名规范化再解析类别。

    该函数作为文档级缓存目标，不能依赖仍会变化的文档实例或节点状态。
    """
    return _cisco_interface_kind(canonical_cisco_interface(name))


class _CiscoHeaderParser:
    """保存单个 Cisco 文档的字符串解析缓存。

    缓存只依赖不可变 header，不持有文档或节点，避免复制和快照发布时泄漏旧状态。
    """

    def __init__(self) -> None:
        """建立接口名、类型和二层模式的有界缓存。

        每个文档独立持有缓存，使不同配置之间不会互相影响命中结果。
        """
        self.canonical_interface = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            canonical_cisco_interface
        )
        self.interface_kind = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _resolved_interface_kind
        )
        self.interface_name = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _interface_name_from_header
        )
        self.l2transport = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _is_l2transport_header
        )

    def __deepcopy__(self, _memo: dict[int, object]) -> _CiscoHeaderParser:
        """为复制后的文档创建空解析缓存。

        functools 包装器无法安全共享，重新构造可避免旧文档的缓存键进入新修订。
        """
        return type(self)()


class CiscoDocument(VendorConfiguration):
    """提供 IOS XR 配置解析、查询、迁移和渲染的统一门面。

    该类只维护当前快照和按需缓存，具体接口、group 和规则算法委托专用模块实现。
    """

    vendor = "cisco_iosxr"

    def __init__(self, text: str):
        """从 Cisco 配置文本创建初始解析快照。

        构造阶段只建立文本和解析上下文，接口分析等昂贵结果延迟到首次查询。
        """
        self._parser = _CiscoHeaderParser()
        self._interface_analysis_cache = _CiscoAnalysisCache()
        self._snapshot = CiscoSnapshot(text)

    @property
    def snapshot(self):
        """返回当前不可变解析快照。

        事务通过替换快照而非原地修改提供一致性，调用方可据此识别修订变化。
        """
        return self._snapshot

    @property
    def source(self):
        """返回当前快照的完整配置源码。

        源码与解析 token 来自同一快照，保证文本查询和结构化操作看到同一修订。
        """
        return self.snapshot.text

    @property
    def revision(self):
        """返回当前快照的单调修订号。

        修订号用于诊断和缓存失效，不作为持久化标识。
        """
        return self.snapshot.revision

    @property
    def root(self):
        """返回当前快照的 Cisco 虚拟根上下文。

        所有规则访问都从该根开始，以确保绑定同一快照并避免跨修订引用。
        """
        return self.snapshot.root

    def _publish(self, snapshot):
        """原子替换当前快照并清空接口分析缓存。

        只有事务成功重解析后才调用，使失败操作不会暴露部分配置。
        """
        self._snapshot = snapshot
        self._interface_analysis_cache = _CiscoAnalysisCache()

    def rewrite(self):
        """创建基于当前快照的新重写事务。

        事务捕获固定快照坐标，提交后重新解析并发布新修订。
        """
        return RewriteTransaction(self)

    def __deepcopy__(self, _memo):
        """复制配置内容并保留原有空块语义。

        重新解析源码后传播块标记，使副本可独立修改且不共享正式 AST 或缓存。
        """
        from .antlr.metadata import retain_block_modes

        cloned = type(self)(self.source)
        retain_block_modes(self.snapshot, cloned.snapshot, (), set())
        return cloned

    def hostname(self):
        """返回 IOS XR ``hostname`` 命令的最终值。

        按文档顺序取最后一条命令，与设备加载配置后的最终主机名一致。
        """
        values = [
            match.group(1).strip().strip("\"'")
            for node in self.root.nodes
            if (match := re.match(r"hostname\s+(.+)$", node.header, re.I))
        ]
        return values[-1] if values else None

    def _interface_nodes(self) -> list[CiscoContext]:
        """返回仍有效且已识别接口名的 IOS XR 顶层节点。

        具体筛选委托接口模块，让所有调用方复用同一套接口识别规则，每次返回
        当前修订的 Context。
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
            "bundle_members",
            lambda: self._interfaces().bundle_members(self),
            dict,
        )

    def interface_has_business(self, name: str) -> bool:
        """判断物理聚合成员是否含地址、VRF、L2 业务或子接口。

        该结果用于让成员退出压缩，而不是判断源配置是否正确。
        """
        return self._interfaces().interface_has_business(self, name)

    def bundle_binding_is_ambiguous(self, name: str) -> bool:
        """判断物理成员是否声明了多个不同 Bundle 编号。

        多个绑定无法唯一映射，仍作为转换错误处理。
        """
        return self._interfaces().bundle_binding_is_ambiguous(self, name)

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
            "business_interface_names",
            lambda: self._interfaces().business_interface_names(self),
            set,
        )

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

    def remove_interface(
        self, name: str, include_children: bool = False
    ) -> None:
        """逻辑停用指定接口，并可连同全部点号子接口一起停用。

        接口模块通过活动标记保留原块而非直接删除，便于维持文档顺序；父口迁移结束
        时使用 ``include_children`` 可一次清理整棵旧接口树。
        """
        self._interfaces().remove_interface(self, name, include_children)

    def rename_interface_tree(
        self, source: str, target: str, strip_bundle: bool = False
    ) -> None:
        """重命名源接口树，并可移除不再适用的聚合成员属性。

        NNI 暂存与最终落位都需要父口和子接口保持相同后缀；统一树级改名还能在
        目标重名时复用接口模块的合并规则，避免重复定义。
        """
        self._interfaces().rename_interface_tree(
            self, source, target, strip_bundle
        )

    def clone_interface_tree(
        self, source: str, target: str, strip_bundle: bool = False
    ) -> None:
        """把一棵 IOS XR 接口配置深拷贝到新目标接口。

        M-LAG 可能要求同一逻辑聚合按多个对端拆成多份，不能直接改名并消耗唯一源树；
        克隆保留源配置供后续目标继续复制，``strip_bundle`` 则防止旧成员属性泄漏。
        """
        self._interfaces().clone_interface_tree(
            self, source, target, strip_bundle
        )

    def _merge_duplicate_interface(self, preferred: CiscoContext) -> None:
        """把同名接口块的非重复配置子树合并到首选块。

        接口改名或克隆可能撞上已有目标定义；合并而非覆盖可以保留双方有效配置，
        同时停用多余块，确保最终 IOS XR 输出只有一个活动接口定义。
        """
        self._interfaces()._merge_duplicate_interface(self, preferred)

    def bundle_mode(self, member: str) -> BundleMode:
        """返回聚合成员的协商模式或未知标记。

        模式与其他单值业务冲突时无法安全复制，因此不能默认成静态聚合。
        """
        from .bundles import bundle_mode

        return bundle_mode(self, member)

    def adapt_bundle(self, name: str) -> InterfaceAdaptationOutcome:
        """调整聚合主口的模拟成员门限。

        只修改已有聚合属性并保留逻辑接口，使成员缩减后业务仍可稳定运行。
        """
        from .bundles import adapt_bundle

        return adapt_bundle(self, name)

    def validate_nni_clone(
        self,
        source: str,
        target_interfaces: set[str],
    ) -> InterfaceAdaptationOutcome:
        """检查 NNI 源接口是否可安全复制到多个目标。

        地址和单值协议源不能共享，因此校验先于克隆执行。
        """
        from .clone_constraints import validate_nni_clone

        return validate_nni_clone(self, source, target_interfaces)

    def ensure_parent_interface(self, name: str) -> None:
        """确保 NNI 目标父接口存在。

        缺少定义时创建最小启用接口，但不会改动已有接口的封装或业务配置。
        """
        self._interfaces().ensure_parent_interface(self, name)

    def prune_non_nni(
        self,
        protected_parents: set[str],
        *,
        strict_input_validation: bool = True,
    ) -> InterfacePruningOutcome:
        """裁剪不属于 NNI 保留闭包的接口和业务。

        使用原始接口身份而非迁移后名称，避免映射过程改变保留边界。
        """
        from .rules.pruning import prune_non_nni

        del strict_input_validation
        return prune_non_nni(self, protected_parents)

    def prune_interface_dependencies(
        self,
        removed_interfaces: set[str],
        *,
        strict_input_validation: bool = True,
    ) -> InterfacePruningOutcome:
        """清理聚合缩减后成员接口留下的可识别依赖。

        只传播已删除成员引用，不启动完整非 NNI 清理。
        """
        from .rules.pruning import prune_interface_dependencies

        del strict_input_validation
        return prune_interface_dependencies(self, removed_interfaces)

    def adapt_interfaces(
        self, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """对最终目标接口执行类型兼容适配。

        该阶段在映射后运行，使规则看到实际目标名称和最终接口结构。
        """
        from .rules import adapt_interfaces

        return adapt_interfaces(self, target_interfaces)

    def validate_source_semantics(
        self, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """在映射前执行源配置语义校验。

        目标兼容和映射完整性由最终校验单独负责。
        """
        from .rules import validate_source_semantics

        return validate_source_semantics(self, target_interfaces)

    def validate_final_integrity(
        self,
        target_interfaces: set[str],
        removed_interfaces: set[str],
        deferred_interface_references: Iterable[
            DeferredInterfaceReference
        ] = (),
    ) -> InterfaceAdaptationOutcome:
        """校验适配产物的目标兼容性和映射完整性。

        源配置语义已在前置阶段处理，本入口不重复执行。
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
        deferred_interface_references: Iterable[
            DeferredInterfaceReference
        ] = (),
        *,
        strict_input_validation: bool = True,
    ) -> InterfaceAdaptationOutcome:
        """执行最终接口约束和删除引用校验。

        校验规则只读运行，任何错误都以结构化结果返回，不发布部分修复。
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
        """按镜像策略适配已迁移数据口及 IOS XR 模拟运行参数。

        适配逻辑与基础语法解析分离，可针对不同镜像处理接口启停、物理特性或稳定性
        参数；返回分类统计则让上层报告完整说明自动修改的范围。
        """
        from .rules.simulation import adapt_to_simulation

        return adapt_to_simulation(self, policy, data_interfaces)

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

    def apply_cleaning_rule(
        self,
        path: tuple[str, ...],
        match: str,
        action: CleaningAction,
        value: str | None,
    ) -> int:
        """执行一条声明式清洗操作并返回命中数。

        单条调用复用批量编译器，确保作用域、动作顺序和错误处理一致。
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
        """按删除和替换轮次执行批量清洗操作。

        每轮在独立事务中提交，使后续解析错误不会保留前一轮的半套输出。
        """
        hits = [0] * len(operations)
        for phase in compile_cleaning_rules(operations):
            transaction = self.rewrite()
            for block in self.root.nodes:
                _clean_phase(
                    transaction, block, (), block.interface_name, phase, hits
                )
            transaction.commit()
        return hits

    def replace_references(self, replacements: dict[str, list[str]]) -> None:
        """改写配置中指向迁移源接口的引用。

        展开基于原始快照并跳过接口定义 header，避免新目标再次作为源产生级联替换。
        """
        canonical = {
            self.resolve_interface(source): list(dict.fromkeys(targets))
            for source, targets in replacements.items()
            if targets and targets != [source]
        }
        if not canonical:
            return
        source_keys = {name.lower(): name for name in canonical}

        def expand(value):
            """把一个命令文本展开为所有合法目标组合。

            多源同时出现时笛卡尔展开并去重，可保留一对多映射的完整语义。
            """
            matches = [
                match
                for match in _CISCO_INTERFACE_TOKEN.finditer(value)
                if self.resolve_interface(match.group()).lower() in source_keys
            ]
            variants, position = [""], 0
            for match in matches:
                targets = canonical[
                    source_keys[self.resolve_interface(match.group()).lower()]
                ]
                variants = [
                    current + value[position : match.start()] + target
                    for current in variants
                    for target in targets
                ]
                position = match.end()
            return list(
                dict.fromkeys(
                    current + value[position:] for current in variants
                )
            )

        transaction = self.rewrite()
        for node in self.root.nodes:
            _rewrite_reference_node(
                transaction,
                node,
                expand,
                skip_header=node.interface_name is not None,
            )
        transaction.commit()

    def render(self) -> str:
        """生成应用镜像空块策略后的最终配置文本。

        仅在事务预览中计算输出，不修改当前源码或已发布解析快照。
        """
        transaction = self.rewrite()
        _plan_rendering(
            transaction, self.root.nodes, (), xrv9000_empty_block_policy()
        )
        return transaction.preview()


def _expanded_reference_text(node, expand, header):
    """递归渲染使用指定 header 变体的节点子树。

    子节点也逐层应用同一映射，使父命令展开后内部引用保持对应目标。
    """
    lines = [" " * node.indent + header]
    for child in node.nodes:
        if child.is_formatting:
            lines.extend(child.text.rstrip("\r\n").splitlines())
        else:
            for variant in expand(child.header):
                lines.extend(
                    _expanded_reference_text(child, expand, variant)
                    .rstrip("\r\n")
                    .splitlines()
                )
    return "\n".join(lines) + "\n"


def _rewrite_reference_node(transaction, node, expand, *, skip_header=False):
    """递归登记节点及其子树的引用替换操作。

    多目标替换改为整棵子树展开，单目标只改 header，确保编辑区间不重叠。
    """
    if node.is_formatting:
        return
    headers = [node.header] if skip_header else expand(node.header)
    if len(headers) > 1:
        text = "".join(
            _expanded_reference_text(node, expand, header) for header in headers
        )
        transaction.replace_subtree(node, text, "展开一对多接口引用")
        return
    if headers[0] != node.header:
        transaction.replace_header(node, headers[0], "改写接口引用")
    for child in node.nodes:
        _rewrite_reference_node(transaction, child, expand)


def _omit_empty_block(node, ancestors, policy):
    """判断节点是否应按镜像策略省略整个空块。

    只有块自身和全部后代都为空时才省略，避免删除仍含注释或业务内容的容器。
    """
    if node.is_formatting:
        return True
    path = (*ancestors, node.header.strip())
    children_empty = all(
        _omit_empty_block(child, path, policy) for child in node.nodes
    )
    return (
        node.is_block
        and children_empty
        and policy.action_for(path) == EmptyBlockAction.OMIT
    )


def _plan_rendering(transaction, nodes, ancestors, policy):
    """递归登记最终渲染中的空块删除操作。

    与预检查使用同一策略路径，保证删除判断和实际事务操作一致。
    """
    for node in nodes:
        if node.is_formatting:
            continue
        if _omit_empty_block(node, ancestors, policy):
            transaction.delete(node, "按镜像策略省略空块")
        else:
            _plan_rendering(
                transaction, node.nodes, (*ancestors, node.header), policy
            )


def _clean_phase(transaction, node, ancestors, interface, phase, hits):
    """在一个清洗轮次中递归应用首条匹配操作。

    节点命中替换后继续使用新 header 遍历子节点，删除则停止该子树以避免悬空编辑。
    """
    if node.is_formatting:
        return
    header = node.header
    for operation in phase.candidates(ancestors):
        if (
            operation.interface_names is not None
            and interface not in operation.interface_names
        ):
            continue
        if not operation.pattern.search(header):
            continue
        hits[operation.index] += 1
        if phase.action == CleaningAction.DELETE:
            transaction.delete(node, "声明式清洗删除")
            return
        header = operation.pattern.sub(operation.value or "", header)
        transaction.replace_header(node, header, "声明式清洗替换")
        break
    for child in node.nodes:
        _clean_phase(
            transaction, child, (*ancestors, header), interface, phase, hits
        )


class _CiscoAnalysisCache:
    """保存一份 Cisco 文档当前快照的接口分析结果。

    缓存由文档快照发布时整体替换，因此条目天然绑定单一修订，不会返回旧树结果。
    """

    def __init__(self):
        """创建空的查询结果映射。

        查询按需计算并按名称缓存，未访问的分析不会产生启动成本。
        """
        self.results = {}

    def read(self, query, collect, copy_result):
        """读取或计算一个查询，并返回调用方独立副本。

        复制结果可防止调用方修改列表或集合时污染同一快照的后续查询。
        """
        if query not in self.results:
            self.results[query] = collect()
        return copy_result(self.results[query])
