"""Junos 配置树聚合根及厂商能力组合入口。"""

from __future__ import annotations

import re

from ..common.config_ast.document import AstDocument
from ..common.domain.contracts import VendorConfiguration
from ..common.support.interface_cache import InterfaceAnalysisCache
from .support.empty_blocks import vmx_empty_block_policy
from .syntax.ast import JuniperNode, top_block
from .syntax.header_parser import (
    _JunosHeaderParser,
    canonical_junos_interface,
)
from .syntax.parser import JuniperParser
from .syntax.writer import JuniperWriter


class JunosDocument(AstDocument, VendorConfiguration):
    """持有 Junos 配置树并组合窄业务能力。

    语法解析、编辑事务、修订、分析缓存和渲染由 Document 管理，具体业务动作
    通过按需创建的轻量能力对象提供。
    """

    vendor = "juniper_junos"

    @property
    def interfaces(self):
        """返回按需创建的 Junos 接口能力。

        每次访问生成轻量对象，Document 不保存长期服务集合。
        """
        from .operations.interfaces import JunosInterfaceCapabilities

        return JunosInterfaceCapabilities(self)

    @property
    def bundles(self):
        """返回按需创建的 Junos 聚合能力。

        聚合对象只持有当前文档引用。
        """
        from .operations.bundles import JunosBundleOperations

        return JunosBundleOperations(self)

    @property
    def groups(self):
        """返回按需创建的 Junos Group 能力。

        Group 展开不缓存跨调用状态。
        """
        from .operations.groups import JunosGroupOperations

        return JunosGroupOperations(self)

    @property
    def cleaning(self):
        """返回按需创建的 Junos 清洗能力。

        清洗能力沿用现有编辑和审计事务。
        """
        from .operations.cleaning import JunosCleaningOperations

        return JunosCleaningOperations(self)

    @property
    def pruning(self):
        """返回按需创建的 Junos 裁剪能力。

        裁剪规则仍由现有实现执行。
        """
        from .operations.pruning import JunosPruningOperations

        return JunosPruningOperations(self)

    @property
    def compatibility(self):
        """返回按需创建的 Junos 兼容能力。

        目标接口适配不受模拟模式控制。
        """
        from .operations.compatibility import JunosCompatibilityOperations

        return JunosCompatibilityOperations(self)

    @property
    def validation(self):
        """返回按需创建的 Junos 校验能力。

        校验能力保持只读。
        """
        from .operations.validation import JunosValidationOperations

        return JunosValidationOperations(self)

    @property
    def references(self):
        """返回按需创建的 Junos 引用能力。

        引用改写使用最终接口映射。
        """
        from .operations.references import JunosReferenceOperations

        return JunosReferenceOperations(self)

    @property
    def simulation(self):
        """返回按需创建的 Junos 模拟能力。

        适配边界由镜像策略确定。
        """
        from .operations.simulation import JunosSimulationOperations

        return JunosSimulationOperations(self)

    def __init__(self, text: str):
        """把配置解析为唯一的可变文档树。

        构造阶段建立解析器、写入器、接口缓存和增量归属索引。
        """
        self._parser = _JunosHeaderParser()
        self._interface_analysis_cache = InterfaceAnalysisCache()
        self._revision = 0
        self.writer = JuniperWriter()
        self.root = JuniperParser().parse(text)

    def parse_fragment(self, text):
        """在挂接到配置树前解析独立片段。

        片段校验先于 Editor 修改活动树执行。
        """
        return JuniperParser().parse(text)

    def normalize_header(self, header):
        """移除传入命令头中的语法结束符。

        结构标点不参与业务命令匹配。
        """
        return header.strip().removesuffix(";").rstrip()

    def validate_header(self, node, header):
        """把结构命令校验委托给厂商 Parser。

        Document 门面只选择语法服务，不自行实现语法检查。
        """
        return JuniperParser().validate_header(node, header)

    def hostname(self) -> str | None:
        """返回 ``system`` 块下最终生效的主机名。

        未配置主机名时返回空值。
        """
        system = top_block(self.root, "system")
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
            candidate = match.group(1).strip().removesuffix(";").rstrip()
            value = candidate.strip('"').strip("'")
        return value

    def _read_interface_analysis(self, query, collect, copy_result):
        """从文档缓存读取接口分析结果。

        能力对象通过该入口复用统一变更序号和失效规则。
        """
        return self._interface_analysis_cache.read(
            self.root,
            query,
            collect,
            copy_result,
        )

    def render(self) -> str:
        """按目标部署镜像的空块策略序列化配置树。

        部署输出不会修改 Document 当前持有的配置。
        """
        return self.writer.render(
            self.root, self._parser, vmx_empty_block_policy()
        )


__all__ = [
    "JuniperNode",
    "JunosDocument",
    "canonical_junos_interface",
]
