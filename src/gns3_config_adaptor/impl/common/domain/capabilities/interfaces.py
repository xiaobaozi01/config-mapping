"""接口分析与修改能力协议。"""

from __future__ import annotations

from typing import Protocol

from ..interface import InterfaceKind, InterfaceSpec
from ..outcomes import InterfaceAdaptationOutcome


class InterfaceAnalysis(Protocol):
    """定义只读接口查询能力。

    实现只能读取当前配置树，不得修改节点或创建编辑事务。
    """

    def interface_specs(self) -> list[InterfaceSpec]:
        """返回当前配置中的接口规格。

        结果只描述接口和逻辑单元，不暴露厂商 AST。
        """
        ...

    def interface_kind(self, name: str) -> InterfaceKind:
        """返回接口的业务类别。

        无法识别的名称必须返回未知类型，而不是猜测为物理接口。
        """
        ...

    def bundle_members(self) -> dict[str, str]:
        """返回物理成员到聚合父接口的映射。

        映射只包含能够由配置唯一确定的绑定关系。
        """
        ...

    def interface_has_business(self, name: str) -> bool:
        """判断聚合成员是否承载独立业务。

        该结果用于阻止压缩删除不能共享地址或封装逻辑的成员。
        """
        ...

    def bundle_binding_is_ambiguous(self, name: str) -> bool:
        """判断成员是否存在多个聚合绑定。

        存在多个绑定时应阻断迁移，不能任选其中一个目标。
        """
        ...

    def resolve_interface(self, value: str) -> str:
        """把外部接口写法转换为规范名称。

        名称规范化必须与配置树内部查找使用同一规则。
        """
        ...

    def logical_names_under(self, parent: str) -> list[str]:
        """列出父接口及其已配置逻辑单元。

        返回顺序必须稳定，以支持可重复的映射和审计输出。
        """
        ...

    def business_interface_names(self) -> set[str]:
        """返回仍承载有效业务的接口名称。

        实现应区分普通硬件参数、业务配置和全局引用。
        """
        ...


class InterfaceTransform(Protocol):
    """定义接口结构修改能力。

    每个修改方法自行管理局部编辑事务，并遵守 Document 的回滚边界。
    """

    def remove(self, name: str, include_children: bool = False) -> None:
        """移除指定接口及其允许包含的子对象。

        删除通过逻辑停用完成，不在遍历过程中直接破坏节点关系。
        """
        ...

    def rename(
        self, source: str, target: str, strip_bundle: bool = False
    ) -> None:
        """重命名整棵源接口树。

        可同时移除不再适用于目标接口的聚合成员属性。
        """
        ...

    def clone(
        self, source: str, target: str, strip_bundle: bool = False
    ) -> None:
        """把源接口树复制到目标接口。

        源树在操作完成后必须仍然可以用于其他目标。
        """
        ...

    def ensure_parent(self, name: str) -> None:
        """确保目标父接口存在。

        只在缺失时创建最小父节点，不伪造业务子配置。
        """
        ...

    def map_uni(
        self, source: str, parent: str, vlan: int, inner_vlan: int | None
    ) -> InterfaceAdaptationOutcome:
        """把暂存业务迁到共享物理口的单层或双层标签子接口。

        无法安全转换的封装返回错误，已知失效的类型专属配置删除并审计。
        """
        ...

    def finalize_uni_source(
        self, source: str, check_parent: bool
    ) -> InterfaceAdaptationOutcome:
        """在 YAML 清洗后检查并移除已经完成业务复制的暂存源树。

        硬件删除只由常规清洗执行，未安全继承的父级业务配置仍须阻断输出。
        """
        ...


class InterfaceCapabilities(Protocol):
    """组合只读与修改型接口能力。

    Document 按需返回该对象，能力本身不缓存配置树状态。
    """

    @property
    def analysis(self) -> InterfaceAnalysis:
        """返回只读接口分析能力。

        每次访问应返回轻量对象，而不是长期保存的全局服务。
        """
        ...

    @property
    def transform(self) -> InterfaceTransform:
        """返回接口修改能力。

        修改事务由返回对象的方法自行管理。
        """
        ...
