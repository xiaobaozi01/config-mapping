"""手写 AST 驱动的 IOS XR 文档聚合根。"""

from __future__ import annotations

import re

from ..common.config_ast.document import AstDocument
from ..common.config_ast.tracking import cache_token
from ..common.domain.contracts import VendorConfiguration
from .support.empty_blocks import xrv9000_empty_block_policy
from .syntax.ast import CiscoNode
from .syntax.header_parser import _CiscoHeaderParser
from .syntax.parser import CiscoParser
from .syntax.writer import CiscoWriter


class CiscoDocument(AstDocument, VendorConfiguration):
    """持有 IOS XR 配置树并组合窄业务能力。

    语法解析、编辑事务、修订、分析缓存和渲染由 Document 管理，具体业务动作
    通过按需创建的轻量能力对象提供。
    """

    vendor = "cisco_iosxr"

    @property
    def interfaces(self):
        """返回按需创建的 IOS XR 接口能力。

        每次访问生成轻量对象，Document 不保存长期服务集合。
        """
        from .operations.interfaces import CiscoInterfaceCapabilities

        return CiscoInterfaceCapabilities(self)

    @property
    def bundles(self):
        """返回按需创建的 IOS XR 聚合能力。

        聚合对象只持有当前文档引用。
        """
        from .operations.bundles import CiscoBundleOperations

        return CiscoBundleOperations(self)

    @property
    def groups(self):
        """返回按需创建的 IOS XR Group 能力。

        Group 展开不缓存跨调用状态。
        """
        from .operations.groups import CiscoGroupOperations

        return CiscoGroupOperations(self)

    @property
    def cleaning(self):
        """返回按需创建的 IOS XR 清洗能力。

        清洗能力沿用现有编辑和审计事务。
        """
        from .operations.cleaning import CiscoCleaningOperations

        return CiscoCleaningOperations(self)

    @property
    def pruning(self):
        """返回按需创建的 IOS XR 裁剪能力。

        裁剪规则仍由现有注册表执行。
        """
        from .operations.pruning import CiscoPruningOperations

        return CiscoPruningOperations(self)

    @property
    def compatibility(self):
        """返回按需创建的 IOS XR 兼容能力。

        目标接口适配不受模拟模式控制。
        """
        from .operations.compatibility import CiscoCompatibilityOperations

        return CiscoCompatibilityOperations(self)

    @property
    def validation(self):
        """返回按需创建的 IOS XR 校验能力。

        校验能力保持只读。
        """
        from .operations.validation import CiscoValidationOperations

        return CiscoValidationOperations(self)

    @property
    def references(self):
        """返回按需创建的 IOS XR 引用能力。

        引用改写使用最终接口映射。
        """
        from .operations.references import CiscoReferenceOperations

        return CiscoReferenceOperations(self)

    @property
    def simulation(self):
        """返回按需创建的 IOS XR 模拟能力。

        适配边界由镜像策略确定。
        """
        from .operations.simulation import CiscoSimulationOperations

        return CiscoSimulationOperations(self)

    def __init__(self, text: str):
        """把配置解析为唯一的可变文档树。

        构造阶段建立解析器、写入器、接口缓存和增量归属索引。
        """
        self._parser = _CiscoHeaderParser()
        self._interface_analysis_cache = _CiscoAnalysisCache()
        self._revision = 0
        self.writer = CiscoWriter()
        self.root = CiscoParser().parse(text)

    def parse_fragment(self, text):
        """在挂接到配置树前解析独立片段。

        片段校验先于 Editor 修改活动树执行。
        """
        return CiscoParser().parse(text)

    def validate_header(self, node, header):
        """把结构命令校验委托给厂商 Parser。

        Document 门面只选择语法服务，不自行实现语法检查。
        """
        return CiscoParser().validate_header(node, header)

    def hostname(self):
        """返回 IOS XR ``hostname`` 命令的最终值。

        按文档顺序取最后一条有效命令，与设备加载后的最终状态一致。
        """
        values = [
            match.group(1).strip().strip("\"'")
            for node in self.root.nodes
            if (match := re.match(r"hostname\s+(.+)$", node.header, re.I))
        ]
        return values[-1] if values else None

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
        """生成应用镜像空块策略后的最终配置文本。

        Writer 直接过滤输出中的空块，不创建编辑器或修改当前配置树。
        """
        return self.writer.render(self.root, xrv9000_empty_block_policy())


class _CiscoAnalysisCache:
    """保存一份 Cisco 文档当前配置树的接口分析结果。

    缓存由文档修改时整体替换，因此条目天然绑定单一修订。
    """

    def __init__(self):
        """创建空的查询结果映射。

        查询按需计算并按名称缓存，未访问的分析不会产生启动成本。
        """
        self.results = {}
        self.signature = None

    def read(self, root, query, collect, copy_result):
        """读取或计算一个查询，并返回调用方独立副本。

        复制结果可防止调用方修改列表或集合时污染后续查询。
        """
        signature = cache_token(root)
        if signature != self.signature:
            self.results.clear()
            self.signature = signature
        if query not in self.results:
            self.results[query] = collect()
        return copy_result(self.results[query])


__all__ = ["CiscoDocument", "CiscoNode"]
