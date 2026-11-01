"""跨厂商配置文档共同实现的能力协议。

协议只描述转换流程真正需要的能力。Cisco/Junos 的 AST、节点和语法辅助函数
均属于实现细节，不应泄漏到责任链和声明式清洗规则中。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ..rules.cleaning import CleaningAction, CleaningOperation
from .interface import BundleMode, InterfaceKind, InterfaceSpec
from .outcomes import (
    CleanupOutcome,
    GroupExpansionOutcome,
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from .policies import SimulationAdaptationPolicy, WashingPolicy


@runtime_checkable
class VendorConfiguration(Protocol):
    """单台设备配置的厂商无关能力集合。

    这是有意保留的 Facade 边界：应用层只认识业务动作，不认识 Cisco block
    或 Junos node。具体实现内部再按 group、接口、清洗和模拟适配模块组合。
    """

    vendor: str

    def hostname(self) -> str | None:
        """返回配置中最终生效的设备主机名。

        统一主机名入口让拓扑与配置的匹配不依赖厂商 AST，同时允许未配置时返回空值。
        """
        ...

    def expand_groups(
        self,
        known_interfaces: set[str],
        *,
        policy: WashingPolicy,
    ) -> GroupExpansionOutcome:
        """展开活动配置引用的 group，并返回继承、冲突和诊断结果。

        group 必须在接口分析前物化，否则后续阶段只能看到不完整的地址和绑定配置。
        """
        ...

    def interface_specs(self) -> list[InterfaceSpec]:
        """返回当前修订中的接口及逻辑单元规格。

        规格数据屏蔽厂商节点差异，使分类、映射和清理阶段共享同一接口视图。
        """
        ...

    def interface_kind(self, name: str) -> InterfaceKind:
        """返回指定接口的厂商无关类别。

        分类结果直接决定接口是否可参与迁移和删除，因此未知名称必须保守归为未知类型。
        """
        ...

    def bundle_members(self) -> dict[str, str]:
        """返回物理成员接口到聚合父接口的映射。

        该方向与拓扑成员查询相反，集中提供可避免各处理器重复解析厂商绑定语法。
        """
        ...

    def interface_has_business(self, name: str) -> bool:
        """判断物理成员是否承载不能随聚合压缩丢弃的业务。

        该判断与 Bundle 绑定解析分离，源配置业务不会被误判为未知模式。
        """
        ...

    def bundle_binding_is_ambiguous(self, name: str) -> bool:
        """判断成员是否声明了多个聚合绑定。

        多个绑定无法唯一映射，调用方必须阻断而不是猜测目标关系。
        """
        ...

    def resolve_interface(self, value: str) -> str:
        """把外部接口写法转换为配置解析器使用的规范名称。

        拓扑缩写和配置全文必须落到同一名称，后续端点与引用匹配才不会产生歧义。
        """
        ...

    def logical_names_under(self, parent: str) -> list[str]:
        """列出父接口及其已配置逻辑单元名称。

        迁移和审计需要稳定的父子顺序，因此结果由厂商实现统一去重并排序。
        """
        ...

    def business_interface_names(self) -> set[str]:
        """识别承载业务或仍被有效业务配置引用的接口。

        仅看接口是否有地址会误判网关和虚拟口，集中分析可保证清理边界一致。
        """
        ...

    def remove_interface(
        self, name: str, include_children: bool = False
    ) -> None:
        """从逻辑上停用指定接口及其允许包含的子对象。

        保留原配置树而非物理删除，使编辑顺序、注释和审计位置在重写期间保持稳定。
        """
        ...

    def rename_interface_tree(
        self,
        source: str,
        target: str,
        strip_bundle: bool = False,
    ) -> None:
        """把接口定义及子对象改名到目标接口。

        树级操作确保父接口与 unit 的后缀同步变化，并在目标已存在时复用厂商的合并规则。
        """
        ...

    def clone_interface_tree(
        self,
        source: str,
        target: str,
        strip_bundle: bool = False,
    ) -> None:
        """把源接口子树复制到目标接口。

        克隆用于一对多迁移，可避免继续复用同一源节点而污染后续目标或残留绑定属性。
        """
        ...

    def bundle_mode(self, member: str) -> BundleMode:
        """返回聚合成员绑定的协商模式或未知标记。

        模式影响跨设备聚合能否安全迁移，无法确认时必须返回未知而不能默认兼容。
        """
        ...

    def adapt_bundle(self, name: str) -> InterfaceAdaptationOutcome:
        """按目标镜像约束调整指定聚合接口。

        聚合主口和成员语义差异较大，集中处理可避免迁移阶段直接修改门限和绑定。
        """
        ...

    def validate_nni_clone(
        self,
        source: str,
        target_interfaces: set[str],
    ) -> InterfaceAdaptationOutcome:
        """校验源接口是否可安全复制到全部目标接口。

        校验先于任何克隆执行，防止单值地址或引用被展开后才发现无法等价表达。
        """
        ...

    def ensure_parent_interface(self, name: str) -> None:
        """确保接口父节点存在，但不创建不需要的业务子树。

        迁移后的目标可能尚未在配置中出现，先建立父口可让后续重命名和引用写入成功。
        """
        ...

    def prune_non_nni(
        self,
        protected_parents: set[str],
        *,
        strict_input_validation: bool = True,
    ) -> InterfacePruningOutcome:
        """删除 NNI 保留闭包之外的接口及其依赖业务。

        保护集合由原始拓扑计算，避免接口迁移改变 NNI 身份后误删真正的端点。
        """
        ...

    def prune_interface_dependencies(
        self,
        removed_interfaces: set[str],
        *,
        strict_input_validation: bool = True,
    ) -> InterfacePruningOutcome:
        """清理已删除接口在协议、策略和路由中的可识别依赖。

        依赖删除在接口定义处理前汇总，使最终配置不会保留悬空引用。
        """
        ...

    def adapt_interfaces(
        self, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """规范化目标接口类型并返回兼容适配结果。

        该阶段独立于模拟策略，确保虚拟镜像要求的基础语法约束始终执行。
        """
        ...

    def validate_source_semantics(
        self, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """在映射前校验源配置自身的语义问题。

        该入口不包含目标镜像兼容、删除引用或映射完整性校验。
        """
        ...

    def validate_final_integrity(
        self,
        target_interfaces: set[str],
        removed_interfaces: set[str],
        deferred_interface_references=(),
    ) -> InterfaceAdaptationOutcome:
        """校验适配产物，不重复源配置语义规则。

        该入口用于最终阶段检查目标兼容、聚合绑定和删除引用完整性。
        """
        ...

    def validate_interfaces(
        self,
        target_interfaces: set[str],
        removed_interfaces: set[str],
        deferred_interface_references=(),
        *,
        strict_input_validation: bool = True,
    ) -> InterfaceAdaptationOutcome:
        """校验目标接口、已删除引用和厂商类型约束。

        最终校验只报告问题而不修改配置，避免失败结果混入未审计的自动修复。
        """
        ...

    def replace_references(self, replacements: dict[str, list[str]]) -> None:
        """按映射表改写接口定义之外的配置引用。

        引用改写晚于映射，使一对多关系可以使用最终目标集合而不发生级联替换。
        """
        ...

    def clean_management_access(self) -> CleanupOutcome:
        """删除生产管理凭据和远程访问配置，并返回分类统计。

        管理面数据不得进入隔离实验环境，分类统计让输出能够说明清理范围而不泄露内容。
        """
        ...

    def add_lab_account(self) -> None:
        """写入隔离实验环境统一使用的本地登录账号。

        管理认证被清除后必须建立可接管入口，否则生成的设备配置可能无法登录。
        """
        ...

    def adapt_to_simulation(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """按镜像策略调整目标接口和模拟环境参数。

        策略只作用于已映射接口和已有参数，避免改变未参与拓扑的业务能力。
        """
        ...

    def apply_cleaning_rule(
        self,
        path: tuple[str, ...],
        match: str,
        action: CleaningAction,
        value: str | None,
    ) -> int:
        """执行一条清洗规则并返回命中数量。

        单条入口转换为批量执行流程，可复用同一编译、作用域和审计逻辑。
        """
        ...

    def apply_cleaning_rules(
        self, operations: Sequence[CleaningOperation]
    ) -> list[int]:
        """按输入顺序执行批量清洗规则并返回各自命中数量。

        批量执行保证删除先于替换，同时让所有规则共享一次路径和语法分析。
        """
        ...

    def render(self) -> str:
        """生成当前修订的最终厂商配置文本。

        输出只包含逻辑上的活动节点，并保留解析器承诺维护的原始格式语义。
        """
        ...
