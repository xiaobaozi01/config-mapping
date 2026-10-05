"""显式、可组合的转换流水线。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .groups import GroupExpansionHandler
from .interfaces import InterfaceClassificationHandler, ReferenceRewriteHandler
from .models import ConversionContext
from .nni import NNIHandler
from .simulation import SimulationAdaptationHandler
from .topology import TopologyPreflightHandler
from .uni import UNIHandler
from .washing import AuthWashingHandler, OptionalFeatureWashingHandler


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

    顺序体现数据依赖：先预检并展开继承配置，再分类和迁移 NNI/UNI，随后改写外部
    引用，最后执行可选清洗、认证替换和模拟适配。流水线会在首次错误后停止，因此
    集中定义顺序可以防止调用方漏掉阶段，或在前置校验失败后继续产生部分输出。
    """
    return ConversionPipeline(
        [
            TopologyPreflightHandler(),
            GroupExpansionHandler(),
            InterfaceClassificationHandler(),
            NNIHandler(),
            UNIHandler(),
            ReferenceRewriteHandler(),
            OptionalFeatureWashingHandler(),
            AuthWashingHandler(),
            SimulationAdaptationHandler(),
        ]
    )


def build_default_chain() -> ConversionPipeline:
    """通过旧的 chain 构建入口返回当前默认转换流水线。

    该函数不维护另一套阶段列表，而是直接委托 ``build_default_pipeline``，从而兼容
    既有调用方的同时保证新旧入口拥有完全相同的处理顺序，避免两套流程逐渐分叉。
    """
    return build_default_pipeline()
