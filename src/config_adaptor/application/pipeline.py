"""显式、可组合的转换流水线。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..models import ConversionContext


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
