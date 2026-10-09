"""显式、可组合的转换流水线。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .cleaning_rules import CleaningPoint, CleaningRulesHandler
from .groups import GroupExpansionHandler
from .interfaces import (
    InterfaceClassificationHandler,
    PreconfiguredInterfaceCleanupHandler,
    ReferenceRewriteHandler,
)
from .models import ConversionContext
from .nni import NNIHandler
from .simulation import SimulationAdaptationHandler
from .topology import TopologyPreflightHandler
from .uni import UNIHandler


class ConversionStage(Protocol):
    """一个原子转换阶段；阶段只负责自身业务，不持有下游阶段。"""

    def process(self, context: ConversionContext) -> None: ...


class ConversionPipeline:
    """按注册顺序执行阶段，并在首次错误后停止。"""

    def __init__(self, stages: Iterable[ConversionStage]):
        self._stages = tuple(stages)

    @property
    def stages(self) -> tuple[ConversionStage, ...]:
        """暴露只读阶段列表，便于测试和定制流程。"""
        return self._stages

    def handle(self, context: ConversionContext) -> ConversionContext:
        """兼容原责任链入口。"""
        for stage in self._stages:
            stage.process(context)
            if context.has_errors:
                break
        return context

    def execute(self, context: ConversionContext) -> ConversionContext:
        """应用层语义更明确的新入口。"""
        return self.handle(context)


def build_default_pipeline() -> ConversionPipeline:
    """按依赖顺序组装一次完整配置转换所需的默认处理器流水线。

    顺序体现数据依赖：先执行分析前代码规则，再预检、展开继承配置并忽略未实例化
    接口，然后分类和迁移 NNI/UNI、改写外部引用；普通 YAML 清洗和认证替换在同一
    清洗阶段依次执行，最后进行模拟适配。流水线会在首次错误后停止。
    """
    return ConversionPipeline(
        [
            CleaningRulesHandler(CleaningPoint.PRE_ANALYSIS),
            TopologyPreflightHandler(),
            GroupExpansionHandler(),
            PreconfiguredInterfaceCleanupHandler(),
            InterfaceClassificationHandler(),
            NNIHandler(),
            UNIHandler(),
            ReferenceRewriteHandler(),
            CleaningRulesHandler(CleaningPoint.POST_REWRITE),
            SimulationAdaptationHandler(),
        ]
    )
