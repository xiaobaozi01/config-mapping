"""Juniper Junos 语法模型及面向应用层的兼容门面。"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from impl.common.cleaning import (
    CleaningAction,
    CleaningOperation,
    compile_cleaning_rules,
)
from impl.common.contracts import VendorConfiguration
from impl.common.empty_blocks import EmptyBlockAction
from impl.common.errors import require_invariant
from impl.common.interface import (
    BundleMode,
    InterfaceKind,
    InterfaceSpec,
    interface_parent,
)
from impl.common.interface_cache import (
    PARSER_HEADER_CACHE_SIZE,
    InterfaceAnalysisCache,
    copy_interface_specs,
)
from impl.common.outcomes import (
    CleanupOutcome,
    GroupExpansionOutcome,
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)
from impl.common.policies import SimulationAdaptationPolicy, WashingPolicy

from .empty_blocks import vmx_empty_block_policy


def canonical_junos_interface(value: str) -> str:
    """去除 Junos 接口名首尾及内部的无意义空白。

    拓扑表和配置文本可能采用不同空白格式；在查找接口节点、聚合成员和外部引用前
    统一名称，可确保相同接口不会因书写差异而匹配失败。
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
    """按 Junos 父接口名前缀返回厂商无关的接口类别。

    方法先去掉 unit 后缀，再区分环回、管理、ae、IRB、物理和虚拟接口；无法识别
    时返回 ``UNKNOWN``，而不猜测为物理口，以免迁移新型控制接口或删除解析器尚未
    覆盖的配置。
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
    """从外部接口写法解析厂商无关类别。

    先规范化名称再分类，使拓扑缩写和配置原文共享同一判断结果且不缓存文档状态。
    """
    return _junos_interface_kind(canonical_junos_interface(name))


def _parse_header_prefixes(header: str) -> tuple[str, bool]:
    """去除 Junos 编辑状态前缀并返回基础语句。

    语义匹配忽略 inactive 和 protect，同时保留 inactive 标志供输出阶段恢复停用状态。
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

    同时接受块和分号结尾的叶子，避免接口树中两种合法写法被分类成不同对象。
    """
    return bool(
        re.fullmatch(r"unit\s+\d+;?", _parse_header_prefixes(header)[0])
    )


def _unit_number_from_header(header: str) -> str:
    """提取 unit 语句中的原始编号。

    保留字符串而非整数，使前导零等原始写法在目标接口名称中保持不变。
    """
    return _parse_header_prefixes(header)[0].rstrip(";").split(maxsplit=1)[1]


def _vlan_from_header(header: str) -> int | None:
    """解析单层 VLAN 或 QinQ 外层 VLAN。

    非 VLAN 语句返回空值，使调用方可以区分未标记接口和 VLAN 0。
    """
    statement = _parse_header_prefixes(header)[0]
    match = re.match(r"vlan-id\s+(\d+)\s*;", statement)
    if match:
        return int(match.group(1))
    tags = re.match(r"vlan-tags\s+outer\s+(\d+)\s+inner\s+\d+\s*;", statement)
    return int(tags.group(1)) if tags else None


def _inner_vlan_from_header(header: str) -> int | None:
    """解析 QinQ 语句的内层 VLAN。

    只有完整 vlan-tags 结构才返回编号，避免把普通 VLAN 标签误判为双层封装。
    """
    match = re.match(
        r"vlan-tags\s+outer\s+\d+\s+inner\s+(\d+)\s*;",
        _parse_header_prefixes(header)[0],
    )
    return int(match.group(1)) if match else None


@dataclass
class JunosNode:
    """表示 Junos 大括号配置树中的块节点或叶子语句。

    ``children=None`` 表示叶子，列表表示块；``active`` 仅表示转换过程中的逻辑
    删除，Junos 文本里的 ``inactive:`` 由 ``effective`` 单独识别。``origin`` 和
    ``rank`` 为 group 继承保留来源及优先级；``comment`` 保存独立或行尾注释。
    ``declared_in_source`` 表示该块原本在输入配置中明确出现，或由原有叶子
    升级而来；空时按 vMX YAML 策略输出。转换中新建的空容器则不输出。
    统一节点模型可递归修改任意层级，同时保留解析器暂时不理解的语句文本。
    """

    header: str
    children: list[JunosNode] | None = None
    active: bool = True
    origin: str = "explicit"
    rank: tuple[int, ...] = (1_000_000, 0)
    comment: str | None = None
    declared_in_source: bool = False

    @property
    def is_block(self) -> bool:
        """返回节点是否表示带大括号的配置块。

        以 ``children`` 是否为列表判断，可区分空块与叶子语句；后续遍历和渲染据此
        决定是否继续递归以及是否输出大括号。
        """
        return self.children is not None

    @property
    def configured_inactive(self) -> bool:
        """返回节点是否带有 Junos ``inactive:`` 状态前缀。

        状态直接从原始 header 读取，因此渲染仍可完整保留前缀；同时支持和
        ``protect:`` 组合及不同排列顺序，避免把受保护的停用节点误判为有效。
        """
        return _parse_header_prefixes(self.header)[1]

    @property
    def is_comment_only(self) -> bool:
        """判断节点是否只承载独立注释。

        独立注释必须保留给输出，但不能进入接口、协议或 group 的业务语义遍历。
        """
        return self.comment is not None and not self.header

    @property
    def effective(self) -> bool:
        """返回节点是否参与当前生效配置的语义处理。

        活动标记、inactive 前缀和纯注释状态共同决定有效性，避免任一状态被单独忽略。
        """
        return (
            self.active
            and not self.configured_inactive
            and not self.is_comment_only
        )

    def clone(self) -> JunosNode:
        """深拷贝当前节点及其完整子树。

        M-LAG 拆分和引用一对多展开都需要从同一源配置生成多个目标；
        深拷贝可防止修改某个目标时连带改变原节点或其他副本。
        """
        return copy.deepcopy(self)


class _JunosHeaderParser:
    """保存单文档使用的可失效字符串解析缓存。

    缓存只依赖不可变 header 文本，不持有节点或文档，避免配置修订后复用旧解析结果。
    """

    def __init__(self) -> None:
        """为 header 分类操作建立有界缓存。

        缓存容量与接口和策略规模匹配，重复遍历时仍保持可预测内存上限。
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

        复制树可能继续独立修改，复用原缓存包装器会返回与当前 header 不一致的结果。
        """
        return type(self)()

    def base_header(self, header: str) -> str:
        """返回用于语义匹配的基础 header。

        复用共享前缀解析缓存，使接口、group 和渲染判断采用完全一致的文本语义。
        """
        return self.header_prefixes(header)[0]

    def output_header(self, header: str) -> str:
        """返回移除 protect 并保留 inactive 的输出 header。

        实验配置不需要编辑保护，但必须继续保留原本停用的节点状态。
        """
        result, inactive = self.header_prefixes(header)
        return f"inactive: {result}" if inactive else result

    def is_effective(self, node: JunosNode) -> bool:
        """判断节点是否参与当前生效配置。

        header 解析可以缓存，但节点活动和注释会直接变化，因此这些状态始终实时读取。
        """
        return (
            node.active
            and not self.header_prefixes(node.header)[1]
            and not node.is_comment_only
        )


class _ReferenceRewriter:
    """保存单次接口引用替换的匹配状态。

    替换模式只匹配原始名称，使一对多结果不会在同一轮继续被其他映射再次改写。
    """

    def __init__(
        self, document: JunosDocument, replacements: dict[str, list[str]]
    ):
        """编译来源名称的边界正则并保存目标映射。

        名称按长度倒序拼接，避免较短接口名抢先匹配较长名称的一部分。
        """
        self.document = document
        self.replacements = replacements
        names = sorted(replacements, key=len, reverse=True)
        self.pattern = re.compile(
            r"(?<![A-Za-z0-9_.-])("
            + "|".join(map(re.escape, names))
            + r")(?![A-Za-z0-9_.-])"
        )

    def expand(self, value: str) -> list[str]:
        """把一个 header 展开为所有目标名称组合。

        同时替换多个来源时会生成独立变体，避免先把一个名称改写后再误命中新结果。
        """
        matches = list(self.pattern.finditer(value))
        if not matches:
            return [value]
        variants = [""]
        cursor = 0
        for match in matches:
            prefix = value[cursor : match.start()]
            variants = [
                current + prefix + target
                for current in variants
                for target in self.replacements[match.group(1)]
            ]
            cursor = match.end()
        return list(
            dict.fromkeys(current + value[cursor:] for current in variants)
        )

    def walk(self, node: JunosNode, path: tuple[str, ...] = ()) -> None:
        """递归改写有效节点中的接口引用。

        根 interfaces 下的定义名称由迁移步骤负责，仅其内部引用在这里替换，避免重复改名。
        """
        if not node.active or node.children is None:
            return
        rebuilt = []
        for child in node.children:
            # inactive/protect 是配置语法前缀，仍需改写；逻辑删除和注释不参与。
            if not child.active or child.is_comment_only:
                rebuilt.append(child)
                continue
            variants = (
                [child.header]
                if path == ("interfaces",)
                else self.expand(child.header)
            )
            for header in variants:
                clone = (
                    child
                    if len(variants) == 1 and header == child.header
                    else child.clone()
                )
                clone.header = header
                base = self.document._parser.base_header(header).rstrip(";")
                self.walk(clone, (*path, base))
                rebuilt.append(clone)
        node.children = rebuilt


class JunosDocument(VendorConfiguration):
    """提供可修改 Junos 配置树及应用层所需的统一厂商门面。

    类本身负责大括号解析、渲染、声明式清洗规则和引用替换；接口迁移、group、清洗及模拟
    适配委托给独立厂商模块。这样应用层不需要了解 ``JunosNode``，厂商语法操作也
    能按职责拆分，而仍由一个文档对象维护完整转换状态。
    """

    vendor = "juniper_junos"

    def __init__(self, text: str):
        """创建不参与渲染的虚拟根节点，并解析完整 Junos 文本。

        虚拟根统一承载多个顶层块，使所有遍历都可从单一节点开始；构造时立即解析并
        校验括号，可在任何配置改写发生前拒绝结构损坏的输入。
        """
        self._parser = _JunosHeaderParser()
        self.root = JunosNode("<root>", [])
        self._parse(text)
        self._interface_analysis_cache = InterfaceAnalysisCache()

    def hostname(self) -> str | None:
        """返回 ``system`` 块下最后一条有效的 ``host-name`` 语句值，未配置时返回 ``None``。

        Junos 把设备名放在顶层 ``system`` 容器的 ``host-name`` 叶子中，可能带引号；
        方法按文档顺序取最后一条活动语句以反映加载后的最终值，并去除外层引号与
        结尾分号，便于与 Excel 中的设备名直接比较。
        """
        system = self._top_block("system")
        if not system or system.children is None:
            return None
        value: str | None = None
        for child in system.children:
            if not self._parser.is_effective(child) or child.is_block:
                continue
            statement = self._parser.base_header(child.header)
            match = re.match(r"host-name\s+(.+)$", statement, re.IGNORECASE)
            if not match:
                continue
            candidate = match.group(1).strip()
            if candidate.endswith(";"):
                candidate = candidate[:-1].rstrip()
            value = candidate.strip('"').strip("'")
        return value

    @staticmethod
    def _expand_inline_fragments(
        text: str,
    ) -> list[tuple[str, str | None, int]]:
        """拆分行内块并保留注释和原始行号。

        分号、大括号和块分隔符被拆成独立片段，使后续栈解析与审计定位保持精确。
        """
        expanded: list[tuple[str, str | None, int]] = []
        quote: str | None = None
        escaped = False
        block_comment = False

        for line_number, raw in enumerate(text.splitlines(), start=1):
            fragments, quote, escaped, block_comment = (
                JunosDocument._scan_inline_line(
                    raw,
                    quote,
                    escaped,
                    block_comment,
                )
            )
            expanded.extend(
                (header, comment, line_number)
                for header, comment in (fragments or [("", None)])
            )

        return expanded

    @staticmethod
    def _scan_inline_line(
        raw: str,
        quote: str | None,
        escaped: bool,
        block_comment: bool,
    ) -> tuple[list[tuple[str, str | None]], str | None, bool, bool]:
        """扫描一行并返回片段以及跨行词法状态。

        引号和块注释可能跨越换行，状态必须回传给下一行才能避免误拆分内容。
        """
        fragments, buffer, index = [], [], 0
        while index < len(raw):
            character = raw[index]
            if quote is not None:
                index, quote, escaped = JunosDocument._consume_quote(
                    index, character, quote, escaped, buffer
                )
                continue
            if block_comment:
                index, block_comment = JunosDocument._consume_block_comment(
                    raw, index, character, buffer
                )
                continue
            if character in {'"', "'"}:
                quote = character
                buffer.append(character)
                index += 1
                continue
            if character == "#":
                JunosDocument._attach_comment(
                    fragments, buffer, raw[index:].strip()
                )
                break
            if character == "/" and raw[index : index + 2] == "/*":
                consumed = JunosDocument._consume_inline_comment(
                    raw, index, fragments, buffer
                )
                if consumed is not None:
                    index = consumed
                    continue
                block_comment = True
                buffer.extend(("/", "*"))
                index += 2
                continue
            if character in {"{", ";", "}"}:
                index = JunosDocument._consume_delimiter(
                    raw, index, fragments, buffer
                )
                continue
            buffer.append(character)
            index += 1
        JunosDocument._flush_fragment(fragments, buffer)
        return fragments, quote, escaped, block_comment

    @staticmethod
    def _consume_delimiter(
        raw: str,
        index: int,
        fragments: list[tuple[str, str | None]],
        buffer: list[str],
    ) -> int:
        """提交语句或块分隔符并返回下一字符位置。

        大括号和分号需要进入当前语句片段，而闭块分隔符独立成段以保留树层级。
        """
        character = raw[index]
        if character in {"{", ";"}:
            buffer.append(character)
            JunosDocument._flush_fragment(fragments, buffer)
            return index + 1
        JunosDocument._flush_fragment(fragments, buffer)
        closing = "};" if raw[index : index + 2] == "};" else "}"
        fragments.append((closing, None))
        return index + len(closing)

    @staticmethod
    def _consume_quote(
        index: int,
        character: str,
        quote: str,
        escaped: bool,
        buffer: list[str],
    ) -> tuple[int, str | None, bool]:
        """消费引号内的一个字符并维护转义状态。

        只有未转义的匹配引号才结束字符串，避免反斜杠后的引号被提前关闭。
        """
        buffer.append(character)
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == quote:
            quote = None
        return index + 1, quote, escaped

    @staticmethod
    def _consume_block_comment(
        raw: str,
        index: int,
        character: str,
        buffer: list[str],
    ) -> tuple[int, bool]:
        """消费跨行块注释并识别结束标记。

        结束符的两个字符一次写入缓冲区，使注释保持原始文本而不被分词破坏。
        """
        buffer.append(character)
        if raw[index : index + 2] == "*/":
            buffer.append("/")
            return index + 2, False
        return index + 1, True

    @staticmethod
    def _consume_inline_comment(
        raw: str,
        index: int,
        fragments: list[tuple[str, str | None]],
        buffer: list[str],
    ) -> int | None:
        """消费完整行内块注释并返回结束位置。

        注释前后均有配置时保留在原语句中；独占语句位置时作为独立注释片段。
        """
        closing = raw.find("*/", index + 2)
        if closing == -1:
            return None
        comment = raw[index : closing + 2]
        before = "".join(buffer).strip()
        after = raw[closing + 2 :].strip()
        if not before or not after:
            JunosDocument._attach_comment(fragments, buffer, comment)
        else:
            buffer.append(comment)
        return closing + 2

    @staticmethod
    def _flush_fragment(
        fragments: list[tuple[str, str | None]],
        buffer: list[str],
    ) -> None:
        """把当前缓冲区提交为无注释片段。

        空缓冲区不产生节点，避免格式空白被错误解释成配置语句。
        """
        value = "".join(buffer).strip()
        buffer.clear()
        if value:
            fragments.append((value, None))

    @staticmethod
    def _attach_comment(
        fragments: list[tuple[str, str | None]],
        buffer: list[str],
        comment: str,
    ) -> None:
        """把行内注释附着到前一语句或保留为独立片段。

        紧邻闭块后的注释不附到闭块，防止输出时改变注释所属的层级和阅读位置。
        """
        JunosDocument._flush_fragment(fragments, buffer)
        if fragments and fragments[-1][0] not in {"}", "};"}:
            header, previous = fragments[-1]
            fragments[-1] = (
                header,
                f"{previous} {comment}" if previous else comment,
            )
        else:
            fragments.append(("", comment))

    def _parse(self, text: str) -> None:
        """使用栈把 Junos 大括号文本解析成节点树，并校验括号平衡。

        普通块和叶子被结构化，空行及暂不支持的行内大括号语法则原样作为叶子保留；
        这种保守降级避免不完整解析器擅自重写未知语法。多余或缺失右括号会立即报错，
        因为在不可靠树上继续迁移可能把配置写入错误层级。
        """
        stack = [self.root]
        for header, comment, number in self._expand_inline_fragments(text):
            if not header:
                stack[-1].children.append(JunosNode("", comment=comment))
                continue
            if header in {"}", "};"}:
                if len(stack) == 1:
                    raise ValueError(
                        f"Junos 配置第 {number} 行出现多余右大括号"
                    )
                stack.pop()
                if comment:
                    stack[-1].children.append(JunosNode("", comment=comment))
                continue
            if header.endswith("{"):
                node = JunosNode(
                    header[:-1].strip(),
                    [],
                    comment=comment,
                    declared_in_source=True,
                )
                stack[-1].children.append(node)
                stack.append(node)
                continue
            if "{" in header or "}" in header:
                # 预处理未拆分的未知写法继续按原策略作为叶子保留。
                stack[-1].children.append(JunosNode(header, comment=comment))
                continue
            stack[-1].children.append(JunosNode(header, comment=comment))
        if len(stack) != 1:
            raise ValueError("Junos 配置大括号不平衡")

    @staticmethod
    def _base_header(header: str) -> str:
        """去除 ``inactive:``/``protect:`` 前缀并返回基础语句文本。

        这些前缀描述 Junos 节点状态而不是命令身份；语义匹配时忽略它们，才能让接口
        名、块名和清洗规则稳定识别同一命令。原始 header 仍保留在树中，输出阶段再
        单独决定保留 ``inactive:`` 并移除 ``protect:``。
        """
        return _parse_header_prefixes(header)[0]

    def _top_block(self, name: str, create: bool = False) -> JunosNode | None:
        """查找指定活动顶层块，并在请求时创建缺失块。

        清洗和账号注入等操作经常需要访问 ``system``、``security`` 或 ``interfaces``；
        统一入口可避免各模块重复遍历根节点，并保证新块只在明确 ``create=True`` 时
        追加，从而不因只读查询意外改变配置。
        """
        require_invariant(
            self.root.children is not None,
            "Junos 文档根节点必须可包含子节点",
        )
        for node in self.root.children:
            if (
                self._parser.is_effective(node)
                and node.is_block
                and self._parser.base_header(node.header) == name
            ):
                return node
        if create:
            node = JunosNode(name, [])
            self.root.children.append(node)
            return node
        return None

    def _interfaces_block(self, create: bool = False) -> JunosNode | None:
        """返回顶层 ``interfaces`` 块，并可按需创建。

        该专用入口让接口操作不必重复硬编码顶层名称，同时继承 ``_top_block`` 的活动
        状态和显式创建语义，保证所有接口修改落在同一个容器中。
        """
        return self._top_block("interfaces", create=create)

    def _interface_nodes(self) -> list[JunosNode]:
        """返回 ``interfaces`` 下仍有效且具有子树的接口节点。

        过滤内部逻辑删除节点和非块语句，可让接口规格、聚合关系及迁移操作只处理
        真正的接口定义，而不会把注释式叶子或已停用副本当作活动接口。
        """
        block = self._interfaces_block()
        if not block or block.children is None:
            return []
        return [
            node
            for node in block.children
            if self._parser.is_effective(node) and node.is_block
        ]

    def _interface_name(self, node: JunosNode) -> str:
        """从接口节点 header 的首个字段提取规范化接口名。

        先移除状态前缀再规范化，可使 ``inactive:``、``protect:`` 或空白差异不影响
        同名节点合并和接口查找。
        """
        return self.resolve_interface(
            self._parser.base_header(node.header).split()[0]
        )

    def _unit_nodes(self, interface: JunosNode) -> list[JunosNode]:
        """列出接口节点下有效的 ``unit`` 子节点，包括空的叶子 unit。

        Junos 把逻辑接口嵌套在父口内；集中枚举 unit 可让规格提取和 NNI 复制使用
        相同结构判定。``unit 0;`` 也是逻辑接口，不能被当作父口业务复制。
        """
        if interface.children is None:
            return []
        return [
            node
            for node in interface.children
            if self._parser.is_effective(node)
            and self._parser.is_unit(node.header)
        ]

    def _unit_number(self, node: JunosNode) -> str:
        """从已识别的 ``unit`` 节点头提取逻辑单元编号。

        保留字符串形式可兼容解析阶段尚未限制的输入，并在拼接完整接口名及选择源
        unit 时保持原始编号表达。
        """
        return self._parser.unit_number(node.header)

    def _find_vlan(self, unit: JunosNode) -> int | None:
        """读取 unit 的单层 VLAN 或 QinQ 外层 VLAN，未配置时返回 ``None``。

        业务分析同时识别 ``vlan-id`` 和
        ``vlan-tags outer``，可让单层与双层终结使用同一 ``InterfaceSpec.vlan``。
        """
        if unit.children is None:
            return None
        for child in unit.children:
            if not self._parser.is_effective(child):
                continue
            vlan = self._parser.vlan(child.header)
            if vlan is not None:
                return vlan
        return None

    def _find_inner_vlan(self, unit: JunosNode) -> int | None:
        """读取 ``vlan-tags`` 语句中的 QinQ 内层 VLAN。

        内层标签代表原客户业务 VLAN；单层或无标签 unit
        返回 ``None``，让上层按明确的回退规则选择内层标签。
        """
        if unit.children is None:
            return None
        for child in unit.children:
            if not self._parser.is_effective(child):
                continue
            vlan = self._parser.inner_vlan(child.header)
            if vlan is not None:
                return vlan
        return None

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """展开全部已应用 Junos groups，并返回事件、冲突及成败信息。

        group 继承必须在接口分类前物化，但 apply-groups、通配符及优先级处理不属于
        基础语法树职责；委托专用展开器可隔离复杂语义，并让调用方据结果决定是否继续。
        """
        from .groups import JunosGroupExpander

        return JunosGroupExpander(self).expand_groups(known_interfaces, policy)

    def interface_specs(self) -> list[InterfaceSpec]:
        """提取父接口或 unit 的关系、类型及 VLAN/QinQ 信息。

        返回厂商无关的 ``InterfaceSpec``，使 NNI 规划无需遍历 Junos 节点；统一
        规格也让业务筛选、VLAN 分配和审计映射共享同一事实来源。
        """
        return self._interface_analysis_cache.read(
            self.root,
            "interface_specs",
            lambda: self._interfaces().interface_specs(self),
            copy_interface_specs,
        )

    @staticmethod
    def _interfaces():
        """延迟返回 Juniper 接口操作模块。

        接口模块需要引用 ``JunosDocument`` 和 ``JunosNode``；在方法调用时再导入可
        打破模块初始化阶段的循环依赖，同时保持文档类对外提供稳定门面。
        """
        from . import interfaces

        return interfaces

    def interface_kind(self, name: str) -> InterfaceKind:
        """按 Junos 规则返回指定接口的厂商无关类别。

        拓扑预检和配置扫描都通过同一入口分类，可避免对物理口、ae、IRB 或控制接口
        产生不同判断，从而阻止不合法端点参与 NNI 映射。
        """
        return self._parser.interface_kind(name)

    def bundle_members(self) -> dict[str, str]:
        """返回物理接口到 ``ae`` 聚合父口的映射。

        NNI 扁平化需要从拓扑物理端点反查真正承载业务的逻辑聚合口，并在迁移后
        清理原成员，因此统一暴露该方向的关系。
        """
        return self._interface_analysis_cache.read(
            self.root,
            "bundle_members",
            lambda: self._interfaces().bundle_members(self),
            dict,
        )

    def resolve_interface(self, value: str) -> str:
        """把拓扑中的 Junos 接口写法转换为解析器的规范形式。

        所有查找先经过统一规范化，可消除空白差异，避免同一接口在拓扑和配置树之间
        匹配失败或被重复处理。
        """
        return self._parser.canonical_interface(value)

    def logical_names_under(self, parent: str) -> list[str]:
        """列出指定父接口对应的已配置业务名称或全部 unit 名称。

        NNI 克隆和映射审计需要逐个保留逻辑单元后缀；集中枚举可避免处理器自行遍历
        Junos 子树，并保证无 unit 父口与有 unit 接口使用一致入口。
        """
        return self._interfaces().logical_names_under(self, parent)

    def business_interface_names(self) -> set[str]:
        """识别承载三层、二层及活跃广播域网关的 Junos 接口。

        IRB 不因自身配置地址就自动迁移；只有 bridge-domain/vlan 中仍有
        活跃接入口，或其 unit 命中活跃业务 VLAN 时才视为关联业务。
        """
        return self._interface_analysis_cache.read(
            self.root,
            "business_interface_names",
            lambda: self._interfaces().business_interface_names(self),
            set,
        )

    def _find_interface_node(self, name: str) -> JunosNode | None:
        """按父接口名查找第一个有效 Junos 接口节点。

        unit 并非顶层节点，因此查找会归一到父口；委托接口模块可确保调用方
        复用相同的规范化和活动状态规则。
        """
        return self._interfaces().find_interface_node(self, name)

    def remove_interface(
        self, name: str, include_children: bool = False
    ) -> None:
        """逻辑停用接口所在的整个 Junos 父节点及其 unit 子树。

        Junos unit 天然嵌套在父口中，因此删除父节点即可覆盖子接口；保留
        ``include_children`` 参数是为了实现统一厂商协议，而不需要伪造独立 unit 删除。
        """
        self._interfaces().remove_interface(self, name, include_children)

    def rename_interface_tree(
        self, source: str, target: str, strip_bundle: bool = False
    ) -> None:
        """重命名整个 Junos 接口节点，并可移除聚合相关 options。

        父节点改名会自然携带全部 unit，适合 NNI 暂存与最终落位；迁出 ae 成员时
        清除旧 options，可避免目标物理口继续引用已不存在的聚合关系。
        """
        self._interfaces().rename_interface_tree(
            self, source, target, strip_bundle
        )

    def clone_interface_tree(
        self, source: str, target: str, strip_bundle: bool = False
    ) -> None:
        """把一棵 Junos 接口子树深拷贝到新目标接口。

        M-LAG 可能要求同一 ae 业务按多个对端拆成多份，不能直接改名并消耗唯一源树；
        克隆保留源配置供后续目标继续复制，``strip_bundle`` 防止旧成员选项泄漏。
        """
        self._interfaces().clone_interface_tree(
            self, source, target, strip_bundle
        )

    def _merge_duplicate_interface(self, preferred: JunosNode) -> None:
        """把同名接口节点的非重复子树合并到首选节点并停用副本。

        接口改名或克隆可能撞上已有目标定义；按渲染结果去重可保留双方不同配置，
        又能确保最终 ``interfaces`` 下只有一个活动的同名节点。
        """
        self._interfaces()._merge_duplicate_interface(self, preferred)

    def ensure_parent_interface(self, name: str) -> None:
        """确保 NNI 承载父接口存在。

        已有接口保持原 unit 和封装，仅有缺失时创建空父节点，避免伪造业务配置。
        """
        self._interfaces().ensure_parent_interface(self, name)

    def bundle_mode(self, member: str) -> BundleMode:
        """读取聚合成员的原协商方式。

        无法确认成员绑定或 LACP 模式时返回 unknown，使迁移流程选择保守失败。
        """
        from .bundles import bundle_mode

        return bundle_mode(self, member)

    def adapt_bundle(self, name: str) -> InterfaceAdaptationOutcome:
        """保留逻辑聚合并按单成员实验调整已有门限。

        只改模拟不兼容的成员数和带宽门槛，保留主口类型、地址及引用。
        """
        from .bundles import adapt_bundle

        return adapt_bundle(self, name)

    def validate_nni_clone(
        self,
        source: str,
        target_interfaces: set[str],
    ) -> InterfaceAdaptationOutcome:
        """在源接口复制前检查三层地址和单值引用。

        一对多目标无法等价承载单值配置，因此校验必须先于任何克隆或改名。
        """
        from .clone_constraints import validate_nni_clone

        return validate_nni_clone(self, source, target_interfaces)

    def prune_non_nni(
        self, protected_parents: set[str]
    ) -> InterfacePruningOutcome:
        """基于原始接口身份删除非 NNI 接入接口及明确依赖。

        保护集合来自拓扑原始名称，避免迁移后的名称变化扩大或缩小清理范围。
        """
        from .rules.pruning import prune_non_nni

        return prune_non_nni(self, protected_parents)

    def prune_interface_dependencies(
        self, removed_interfaces: set[str]
    ) -> InterfacePruningOutcome:
        """清理聚合缩减后已删除成员口的可识别引用。

        只处理指定成员相关依赖，不重新执行全局非 NNI 删除，避免影响其他业务。
        """
        from .rules.pruning import prune_interface_dependencies

        return prune_interface_dependencies(self, removed_interfaces)

    def adapt_interfaces(
        self, target_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """执行已注册的 Junos 接口适配规则。

        统一入口让后续规则扩展无需修改文档门面，同时默认保留现有 unit 语义。
        """
        from .rules import adapt_interfaces

        return adapt_interfaces(self, target_interfaces)

    def validate_interfaces(
        self, target_interfaces: set[str], removed_interfaces: set[str]
    ) -> InterfaceAdaptationOutcome:
        """执行最终接口引用、网关和 unit 约束检查。

        校验在全部迁移和清理之后运行，只报告不允许输出的残留问题。
        """
        from .rules import validate_interfaces

        return validate_interfaces(self, target_interfaces, removed_interfaces)

    def adapt_to_simulation(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """按镜像策略适配已迁移数据口及 Junos 模拟运行参数。

        适配逻辑与基础 AST 分离，可针对不同 vMX 镜像处理接口启停、物理选项或稳定性
        参数；返回分类统计则让上层报告完整说明自动修改的范围。
        """
        from .rules.simulation import adapt_to_simulation

        return adapt_to_simulation(self, policy, data_interfaces)

    def clean_management_access(self) -> CleanupOutcome:
        """清除 Junos 原有管理访问和认证配置，并返回分类统计。

        生产账号、外部 AAA、SNMP 和远程管理凭据不应进入实验环境；委托独立清洗
        模块可集中维护敏感节点规则，同时让文档门面保持统一调用接口。
        """
        from .cleaning import clean_management_access

        return clean_management_access(self)

    def add_lab_account(self) -> None:
        """向 Junos 文档添加实验用 root 认证信息。

        清除生产认证后必须建立已知登录入口，防止输出设备无法接管；账号节点由厂商
        清洗模块生成，使凭据替换与旧认证删除使用同一实现边界。
        """
        from .cleaning import add_lab_account

        add_lab_account(self)

    def apply_cleaning_rule(
        self,
        path: tuple[str, ...],
        match: str,
        action: CleaningAction,
        value: str | None,
    ) -> int:
        """通过批量执行器运行一条清洗操作。

        单条入口复用批量编译和审计路径，避免维护两套匹配语义。
        """
        action = CleaningAction(action)
        if action == CleaningAction.REPLACE and value is None:
            value = ""
        return self.apply_cleaning_rules(
            [CleaningOperation(path, match, action, value)]
        )[0]

    def apply_cleaning_rules(
        self, operations: Sequence[CleaningOperation]
    ) -> list[int]:
        """按删除、替换顺序执行批量清洗并返回原输入顺序的命中数。

        每轮从最新树快照开始且每个节点只执行首条规则，避免同一轮重复改写和顺序歧义。
        """
        phases = compile_cleaning_rules(operations)
        hits = [0] * len(operations)
        parser = self._parser
        for phase in phases:
            for block in self.root.children or []:
                self._walk_cleaning_phase(
                    block,
                    (),
                    None,
                    phase,
                    hits,
                    parser,
                )
        return hits

    def _walk_cleaning_phase(
        self,
        node: JunosNode,
        ancestors: tuple[str, ...],
        interface: str | None,
        phase,
        hits: list[int],
        parser,
    ) -> None:
        """在单个清洗阶段中递归遍历节点并执行匹配操作。

        子接口上下文从 interfaces 层向下传递，使规则可以按完整接口名限制作用范围。
        """
        if not parser.is_effective(node):
            return
        header = parser.base_header(node.header).rstrip(";").strip()
        if ancestors == ("interfaces",):
            interface = header
        operations = phase.candidates(ancestors)
        if self._apply_cleaning_phase(
            node,
            phase,
            operations,
            hits,
            header,
            interface,
        ):
            return
        header = parser.base_header(node.header).rstrip(";").strip()
        for child in node.children or []:
            self._walk_cleaning_phase(
                child,
                (*ancestors, header),
                interface,
                phase,
                hits,
                parser,
            )

    @staticmethod
    def _apply_cleaning_phase(
        node: JunosNode,
        phase,
        operations,
        hits: list[int],
        header: str,
        interface: str | None,
    ) -> bool:
        """执行节点上的首条匹配清洗操作。

        删除后立即停止子树遍历，替换则继续使用更新后的 header 定位子节点。
        """
        for operation in operations:
            if (
                operation.interface_names is not None
                and interface not in operation.interface_names
            ):
                continue
            if not operation.pattern.search(header):
                continue
            hits[operation.index] += 1
            if phase.action == CleaningAction.DELETE:
                node.active = False
                return True
            node.header = operation.pattern.sub(
                operation.value or "", header
            ) + ("" if node.is_block else ";")
            return False
        return False

    def replace_references(self, replacements: dict[str, list[str]]) -> None:
        """按映射表改写 Junos 配置中的接口引用。

        接口定义名称由迁移步骤处理，本方法只更新定义之外的引用且过滤恒等映射。
        """
        relevant = {
            self.resolve_interface(source): list(dict.fromkeys(targets))
            for source, targets in replacements.items()
            if targets and targets != [source]
        }
        if relevant:
            _ReferenceRewriter(self, relevant).walk(self.root)

    def _render_node(
        self, node: JunosNode, depth: int, ancestors: tuple[str, ...] = ()
    ) -> str:
        """按深度递归渲染一个活动 Junos 节点及其子树。

        叶子直接输出 header，块节点补齐缩进和大括号；原配置中的空层级和
        由叶子升级的空块默认输出为分号语句，YAML 可指定保留大括号或省略；
        新建的空容器不输出；omit 路径上的注释不单独使空块保留。
        逻辑停用节点跳过。
        """
        if not node.active:
            return ""
        indent = "    " * depth
        if node.children is None:
            return self._render_leaf(node, indent)
        path = (*ancestors, self._parser.base_header(node.header))
        opening = indent + self._parser.output_header(node.header) + " {"
        if node.comment:
            opening += " " + node.comment
        children, has_statement, standalone_comments = self._render_children(
            node, depth, path
        )
        if not has_statement:
            return self._render_empty_block(
                node,
                indent,
                opening,
                path,
                standalone_comments,
            )
        return "\n".join([opening, *children, indent + "}"])

    def _render_leaf(self, node: JunosNode, indent: str) -> str:
        """渲染叶子节点及其行内注释。

        protect 前缀在输出时移除，inactive 状态则继续保留以维持原节点是否生效。
        """
        header = self._parser.output_header(node.header)
        if node.comment:
            header = f"{header} {node.comment}" if header else node.comment
        return indent + header

    def _render_children(
        self,
        node: JunosNode,
        depth: int,
        path: tuple[str, ...],
    ) -> tuple[list[str], bool, list[str]]:
        """渲染子节点并统计有效语句和独立注释。

        分开记录两种内容，使空块策略只由真实语句决定，而注释仍可随块输出。
        """
        print_sections: list[str] = []
        has_statement = False
        standalone_comments: list[str] = []
        for child in node.children or []:
            rendered = self._render_node(child, depth + 1, path)
            if not rendered:
                continue
            print_sections.append(rendered)
            if child.children is not None or child.header.strip():
                has_statement = True
            elif child.is_comment_only:
                standalone_comments.append(child.comment)
        return print_sections, has_statement, standalone_comments

    def _render_empty_block(
        self,
        node: JunosNode,
        indent: str,
        opening: str,
        path: tuple[str, ...],
        standalone_comments: list[str],
    ) -> str:
        """按路径策略渲染源码中声明的空块。

        只有源码中原本存在或由叶子升级的块才按镜像策略输出，避免转换过程新建的
        空容器污染结果；独立注释仍随所选语法保留，以免删除用户信息。
        """
        if not node.declared_in_source:
            return ""
        action = vmx_empty_block_policy().action_for(path)
        if action == EmptyBlockAction.OMIT:
            return ""
        if action == EmptyBlockAction.SEMICOLON:
            statement = indent + self._parser.output_header(node.header) + ";"
            if node.comment:
                statement += " " + node.comment
            return "\n".join(
                [
                    *(indent + comment for comment in standalone_comments),
                    statement,
                ]
            )
        return "\n".join(
            [
                opening,
                *(indent + "    " + comment for comment in standalone_comments),
                indent + "}",
            ]
        )

    def _render_effective_node(self, node: JunosNode, depth: int) -> str:
        """渲染仅含当前生效节点的语义视图。

        该视图供分析使用且不触发空块策略，避免分析结果改变最终配置输出。
        """
        if not self._parser.is_effective(node):
            return ""
        indent = "    " * depth
        if node.children is None:
            return indent + self._parser.output_header(node.header)
        lines = [indent + self._parser.output_header(node.header) + " {"]
        for child in node.children:
            rendered = self._render_effective_node(child, depth + 1)
            if rendered:
                lines.append(rendered)
        lines.append(indent + "}")
        return "\n".join(lines)

    def render(self) -> str:
        """把虚拟根下的全部活动节点渲染为标准 Junos 大括号配置。

        根节点本身不输出，已逻辑删除的子树被跳过，并统一添加末尾换行；稳定格式便于
        设备加载、生成可读差异，也使相同节点树得到确定性文本结果。
        """
        require_invariant(
            self.root.children is not None,
            "Junos 文档根节点必须可包含子节点",
        )
        rendered = [
            self._render_node(node, 0)
            for node in self.root.children
            if node.active
        ]
        return "\n".join(item for item in rendered if item).rstrip() + "\n"
