"""Document 核心与组合能力协议。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .bundles import BundleOperations
from .cleaning import CleaningOperations
from .compatibility import CompatibilityOperations
from .groups import GroupOperations
from .interfaces import InterfaceCapabilities
from .pruning import PruningOperations
from .references import ReferenceOperations
from .simulation import SimulationOperations
from .validation import ValidationOperations


@runtime_checkable
class DocumentCore(Protocol):
    """定义所有厂商文档共享的核心能力。

    该协议只包含输入身份和最终输出，不暴露配置树编辑细节。
    """

    vendor: str

    def hostname(self) -> str | None:
        """返回最终生效的设备主机名。

        未配置主机名时返回空值。
        """
        ...

    def render(self) -> str:
        """生成目标镜像使用的最终配置文本。

        渲染不得修改当前配置树或修订号。
        """
        ...


@runtime_checkable
class VendorDocument(DocumentCore, Protocol):
    """组合 Document 核心与全部窄业务能力。

    流水线阶段应优先依赖实际使用的子能力，而不是该组合接口。
    """

    @property
    def interfaces(self) -> InterfaceCapabilities:
        """返回接口能力组合。

        接口能力继续区分只读分析与修改操作。
        """
        ...

    @property
    def bundles(self) -> BundleOperations:
        """返回聚合接口能力。

        聚合查询和门限适配共享同一业务边界。
        """
        ...

    @property
    def groups(self) -> GroupOperations:
        """返回 Group 展开能力。

        每次访问返回轻量对象，不缓存配置树状态。
        """
        ...

    @property
    def cleaning(self) -> CleaningOperations:
        """返回配置清洗能力。

        管理认证与声明式清洗共享同一审计边界。
        """
        ...

    @property
    def pruning(self) -> PruningOperations:
        """返回接口裁剪能力。

        完整裁剪和成员依赖清理保持为同一职责。
        """
        ...

    @property
    def compatibility(self) -> CompatibilityOperations:
        """返回目标接口兼容能力。

        兼容适配独立于模拟参数策略。
        """
        ...

    @property
    def validation(self) -> ValidationOperations:
        """返回只读校验能力。

        该入口不得修改配置树。
        """
        ...

    @property
    def references(self) -> ReferenceOperations:
        """返回接口引用改写能力。

        引用更新必须使用最终接口映射。
        """
        ...

    @property
    def simulation(self) -> SimulationOperations:
        """返回模拟参数适配能力。

        适配边界由镜像策略和已映射接口共同确定。
        """
        ...
