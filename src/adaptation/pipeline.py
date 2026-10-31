"""显式、可组合的转换流水线。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .cleaning_rules import CleaningPoint, CleaningRulesHandler
from .groups import GroupExpansionHandler
from .interfaces import (
    FinalInterfaceValidationHandler,
    InterfaceClassificationHandler,
    InterfaceCompatibilityHandler,
    NonNNICleanupHandler,
    PreconfiguredInterfaceCleanupHandler,
    ReferenceRewriteHandler,
)
from .models import ConversionContext
from .nni import NNIHandler
from .simulation import SimulationAdaptationHandler
from .topology import TopologyPreflightHandler


class ConversionStage(Protocol):
    """定义转换流水线中的一个原子阶段。

    阶段只读取和更新共享上下文而不持有下游阶段，使顺序调整和独立测试成为可能。
    """

    def process(self, context: ConversionContext) -> None:
        """处理共享转换上下文并留下本阶段结果或错误。

        阶段不得自行调用后续阶段，统一由流水线依据错误状态控制是否继续。
        """
        ...


class ConversionPipeline:
    """按注册顺序执行转换阶段。

    遇到任意阶段错误立即停止，避免后续阶段基于不完整或已失败的上下文继续写入。
    """

    def __init__(self, stages: Iterable[ConversionStage]):
        """冻结阶段注册顺序并创建流水线。

        转成元组可防止运行期间被外部列表修改，保证同一定义每次都按相同顺序执行。
        """
        self._stages = tuple(stages)

    @property
    def stages(self) -> tuple[ConversionStage, ...]:
        """返回只读阶段序列。

        暴露不可变序列既支持检查实际装配结果，也避免调用方绕过流水线修改阶段。
        """
        return self._stages

    def execute(self, context: ConversionContext) -> ConversionContext:
        """依次执行阶段并返回更新后的上下文。

        每阶段结束后检查统一错误状态，使失败处理集中在流水线而不依赖各阶段约定。
        """
        for stage in self._stages:
            stage.process(context)
            if context.has_errors:
                break
        return context


def build_default_pipeline() -> ConversionPipeline:
    """按依赖顺序组装一次完整配置转换所需的默认处理器流水线。

    顺序体现数据依赖：先执行分析前代码规则，再预检、展开继承配置并忽略未实例化
    接口，然后按原始接口身份裁剪非 NNI 业务，再迁移 NNI 和改写引用；
    清洗、接口类型兼容与模拟适配完成后执行最终校验。流水线会在首次错误后停止。
    """
    return ConversionPipeline(
        [
            CleaningRulesHandler(CleaningPoint.PRE_ANALYSIS),
            TopologyPreflightHandler(),
            GroupExpansionHandler(),
            PreconfiguredInterfaceCleanupHandler(),
            InterfaceClassificationHandler(),
            NonNNICleanupHandler(),
            NNIHandler(),
            ReferenceRewriteHandler(),
            CleaningRulesHandler(CleaningPoint.POST_REWRITE),
            InterfaceCompatibilityHandler(),
            SimulationAdaptationHandler(),
            FinalInterfaceValidationHandler(),
        ]
    )
