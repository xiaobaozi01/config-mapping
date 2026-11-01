"""Junos 配置树聚合根及厂商能力组合入口。"""

from __future__ import annotations

import re
from functools import lru_cache

from impl.common.config_ast.document import AstDocument
from impl.common.config_ast.model import tree_signature
from impl.common.domain.contracts import VendorConfiguration
from impl.common.domain.interface import InterfaceKind, interface_parent
from impl.common.support.interface_cache import (
    PARSER_HEADER_CACHE_SIZE,
    InterfaceAnalysisCache,
)

from .support.empty_blocks import vmx_empty_block_policy
from .syntax.ast import JuniperNode, top_block
from .syntax.parser import JuniperParser
from .syntax.writer import JuniperWriter


def canonical_junos_interface(value: str) -> str:
    """去除 Junos 接口名中的无意义空白。

    规范化结果用于连接拓扑名称、配置节点和接口引用。
    """
    return re.sub(r"\s+", "", value.strip())


_JUNOS_PHYSICAL_INTERFACE = re.compile(
    r"^(?:fe|ge|xe|et|mge|so|se|t1|e1|ct|coc|sat|xle)-\d",
    re.IGNORECASE,
)
_JUNOS_VIRTUAL_INTERFACE = re.compile(
    r"^(?:(?:gr|ip|lt|mt|pd|pe|sp|st|vtep|demux|reth)(?:-|\d)|"
    r"(?:dsc|lsi|pimd|pime|tap)$)",
    re.IGNORECASE,
)


def _junos_interface_kind(name: str) -> InterfaceKind:
    """按 Junos 父接口名前缀返回接口类别。

    无法识别的名称返回未知类型，不乐观地当作物理接口。
    """
    parent = interface_parent(name).lower()
    if parent == "lo0":
        return InterfaceKind.LOOPBACK
    if re.fullmatch(r"(?:(?:fxp|em|me)\d+|vme\d*)", parent):
        return InterfaceKind.MANAGEMENT
    if re.fullmatch(r"ae\d+", parent):
        return InterfaceKind.BUNDLE
    if parent == "irb":
        return InterfaceKind.GATEWAY
    if _JUNOS_PHYSICAL_INTERFACE.match(parent):
        return InterfaceKind.PHYSICAL
    if _JUNOS_VIRTUAL_INTERFACE.match(parent):
        return InterfaceKind.VIRTUAL
    return InterfaceKind.UNKNOWN


def _resolved_interface_kind(name: str) -> InterfaceKind:
    """先规范化外部接口名，再解析接口类别。

    该函数不依赖文档实例，可安全进入文档级缓存。
    """
    return _junos_interface_kind(canonical_junos_interface(name))


def _parse_header_prefixes(header: str) -> tuple[str, bool]:
    """去除 Junos 编辑状态前缀并返回基础语句。

    同时报告节点是否带 inactive 状态，供生效判断和输出复用。
    """
    result = header.strip()
    inactive = False
    while True:
        for prefix in ("inactive:", "protect:"):
            if result.startswith(prefix):
                inactive = inactive or prefix == "inactive:"
                result = result[len(prefix) :].strip()
                break
        else:
            return result, inactive


def _is_unit_header(header: str) -> bool:
    """判断 header 是否表示完整 unit 语句。

    同时接受块和分号结尾的叶子形式。
    """
    return bool(
        re.fullmatch(r"unit\s+\d+;?", _parse_header_prefixes(header)[0])
    )


def _unit_number_from_header(header: str) -> str:
    """提取 unit 语句中的原始编号。

    保留字符串形式，使前导零等写法不会丢失。
    """
    return _parse_header_prefixes(header)[0].rstrip(";").split(maxsplit=1)[1]


def _vlan_from_header(header: str) -> int | None:
    """解析单层 VLAN 或 QinQ 外层 VLAN。

    非 VLAN 语句返回空值。
    """
    statement = _parse_header_prefixes(header)[0]
    match = re.match(r"vlan-id\s+(\d+)\s*;?$", statement)
    if match:
        return int(match.group(1))
    tags = re.match(r"vlan-tags\s+outer\s+(\d+)\s+inner\s+\d+\s*;?$", statement)
    return int(tags.group(1)) if tags else None


def _inner_vlan_from_header(header: str) -> int | None:
    """解析 QinQ 语句的内层 VLAN。

    只有完整 ``vlan-tags`` 结构才返回编号。
    """
    match = re.match(
        r"vlan-tags\s+outer\s+\d+\s+inner\s+(\d+)\s*;?$",
        _parse_header_prefixes(header)[0],
    )
    return int(match.group(1)) if match else None


class _JunosHeaderParser:
    """保存单文档使用的可失效字符串解析缓存。

    缓存只依赖不可变 header，不持有节点或文档。
    """

    def __init__(self) -> None:
        """为 header 分类操作建立有界缓存。

        每份文档拥有独立缓存，避免修订之间复用旧结果。
        """
        self.canonical_interface = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            canonical_junos_interface
        )
        self.interface_kind = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _resolved_interface_kind
        )
        self.header_prefixes = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _parse_header_prefixes
        )
        self.is_unit = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _is_unit_header
        )
        self.unit_number = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _unit_number_from_header
        )
        self.vlan = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _vlan_from_header
        )
        self.inner_vlan = lru_cache(maxsize=PARSER_HEADER_CACHE_SIZE)(
            _inner_vlan_from_header
        )

    def __deepcopy__(self, _memo: dict[int, object]) -> _JunosHeaderParser:
        """为复制后的文档创建空 header 缓存。

        不共享旧包装器可避免缓存键与当前树不一致。
        """
        return type(self)()

    def base_header(self, header: str) -> str:
        """返回用于语义匹配的基础 header。

        状态前缀通过共享缓存统一移除。
        """
        return self.header_prefixes(header)[0]

    def output_header(self, header: str) -> str:
        """返回移除 protect 并保留 inactive 的输出 header。

        实验配置不需要编辑保护，但必须保留停用状态。
        """
        result, inactive = self.header_prefixes(header)
        return f"inactive: {result}" if inactive else result

    def is_effective(self, node: JuniperNode) -> bool:
        """判断节点是否参与当前生效配置。

        header 文本可以缓存，节点活动和注释状态始终实时读取。
        """
        return (
            node.active
            and not self.header_prefixes(node.header)[1]
            and not node.is_comment_only
        )


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

        构造阶段建立解析器、写入器、接口缓存和初始树签名。
        """
        self._parser = _JunosHeaderParser()
        self._interface_analysis_cache = InterfaceAnalysisCache()
        self._revision = 0
        self.writer = JuniperWriter()
        self.root = JuniperParser().parse(text)
        self._last_signature = tree_signature(self.root)

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

        能力对象通过该入口复用统一树签名和失效规则。
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
