"""Cisco IOS XR 语法模型及面向应用层的兼容门面。"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable

from ..common.cleaning import matches_cleaning_path
from ..common.contracts import VendorConfiguration
from ..common.interface import InterfaceKind, InterfaceSpec, interface_parent
from ..common.outcomes import (
    CleanupOutcome,
    GroupExpansionOutcome,
    SimulationAdaptationOutcome,
)
from ..common.policies import SimulationAdaptationPolicy, WashingPolicy
from ..common.interface import normalized_command as _normalized_command


@dataclass
class CiscoNode:
    """表示 IOS XR 配置树中的顶层或嵌套节点。

    ``header`` 保存去除缩进后的命令或 ``!``/空行格式节点，``children``
    保存由缩进确定的直接子命令。顶层配置和嵌套配置使用同一类型，
    与 JunosNode 的整棵树模式一致；``origin`` 和 ``rank`` 供 group 继承合并使用。
    """
    header: str
    children: list["CiscoNode"] = field(default_factory=list)
    active: bool = True
    is_block: bool = False
    origin: str = "explicit"
    rank: tuple[int, int] = (1_000_000, 0)

    @property
    def is_formatting(self) -> bool:
        """返回节点是否仅表示 ``!`` 分隔符或空行。

        格式节点需要保留以便未改写文档可稳定渲染，但不应参与接口识别、group
        语义合并或清洗规则匹配，因此由统一属性供各模块过滤。
        """
        return self.header in {"", "!"}

    def clone(self) -> "CiscoNode":
        """深拷贝当前节点及其完整子树。

        Group 继承、M-LAG 展开和一对多引用替换都可能从同一源节点生成多个目标；
        深拷贝可防止修改某个目标时连带改变原树或其他副本。
        """
        return copy.deepcopy(self)

    def walk(
        self,
        *,
        include_self: bool = False,
        include_formatting: bool = False,
    ) -> Iterable["CiscoNode"]:
        """按深度优先顺序遍历当前节点的活动子树。

        默认不包含节点自身，使顶层节点可直接取得内部命令；通过
        ``include_self`` 可覆盖整棵子树。``!`` 和空行默认被跳过，停用节点则
        连同后代一起忽略，避免业务分析读取不会渲染的配置。
        """
        if not self.active:
            return
        if include_self and (include_formatting or not self.is_formatting):
            yield self
        for child in self.children:
            yield from child.walk(
                include_self=True,
                include_formatting=include_formatting,
            )

    @property
    def interface_name(self) -> str | None:
        """返回接口节点的规范化名称，非接口节点返回 ``None``。

        该属性放在统一节点上，但只有顶层 ``interface`` 节点会被接口模块使用；
        去掉 ``l2transport`` 模式后缀才能与拓扑中的纯接口名匹配。
        """
        if not self.header.lower().startswith("interface"):
            return None
        return _interface_name_from_header(self.header)

    @property
    def is_preconfigured_interface(self) -> bool:
        """区分 IOS XR 的预配置模式与真实接口名。"""
        return bool(
            re.match(r"^interface\s+preconfigure(?:\s|$)", self.header, re.IGNORECASE)
        )

    @property
    def l2transport(self) -> bool:
        """判断接口节点头是否显式启用 ``l2transport`` 模式。

        接口克隆、改名或 UNI 映射会重建 header，单独暴露模式标志可避免
        名称变更时丢失二层属性。
        """
        return bool(re.match(r"interface\s+.+\s+l2transport\s*$", self.header, re.IGNORECASE))


@lru_cache(maxsize=32_768)
def _interface_name_from_header(header: str) -> str | None:
    """按不可变 header 缓存接口名；节点改名后会自动使用新键。"""
    match = re.match(r"^interface\s+(.+?)\s*$", header, re.IGNORECASE)
    if not match:
        return None
    value = match.group(1)
    if re.match(r"^interface\s+preconfigure(?:\s|$)", header, re.IGNORECASE):
        preconfigured = re.match(
            r"^interface\s+preconfigure(?:\s+(.+?))?\s*$",
            header,
            re.IGNORECASE,
        )
        value = preconfigured.group(1) if preconfigured else None
        if not value:
            return None
    value = re.sub(r"\s+l2transport\s*$", "", value, flags=re.IGNORECASE)
    return canonical_cisco_interface(value)


def _clone_cisco_nodes(
    nodes: Iterable[CiscoNode],
    *,
    include_formatting: bool = False,
    origin: str | None = None,
) -> list[CiscoNode]:
    """深拷贝节点列表，并可过滤格式节点或统一重设来源。

    文档保真渲染需要 ``!`` 和空行，但 group 语义合并只需要命令节点；集中克隆可以
    在两个场景间安全转换，同时确保对工作树的修改不会污染持久化文档 AST。
    """
    result: list[CiscoNode] = []
    for node in nodes:
        if node.is_formatting and not include_formatting:
            continue
        clone = copy.copy(node)
        if origin is not None:
            clone.origin = origin
        clone.children = _clone_cisco_nodes(
            node.children,
            include_formatting=include_formatting,
            origin=origin,
        )
        result.append(clone)
    return result


def _render_cisco_nodes(nodes: Iterable[CiscoNode], depth: int = 1) -> list[str]:
    """按 AST 深度递归渲染节点，并保留格式节点的位置。

    语义节点使用每层一个空格的 IOS XR 规范缩进；``!`` 同样按所属父层输出，空行则
    输出为空字符串。由树结构统一生成缩进，避免节点移动后沿用旧文本的错误层级。
    """
    lines: list[str] = []
    for node in nodes:
        if not node.active:
            continue
        if not node.header:
            lines.append("")
            continue
        lines.append(" " * depth + node.header)
        lines.extend(_render_cisco_nodes(node.children, depth + 1))
    return lines


def strip_matching_nodes(
    nodes: list[CiscoNode],
    pattern: re.Pattern[str],
    *,
    match_anywhere: bool = False,
) -> tuple[list[CiscoNode], int]:
    """递归删除命中模式的命令节点及其完整子树。

    ``match_anywhere=False`` 时用 ``pattern.match`` 从行首锚定；置为 ``True`` 时改用
    ``pattern.search``，允许命中行内任意位置。删除父命令时连同其语义后代一并移除，
    返回保留节点列表与删除的语义节点数量。
    """
    retained: list[CiscoNode] = []
    removed = 0
    test = pattern.search if match_anywhere else pattern.match
    for node in nodes:
        if not node.is_formatting and test(node.header):
            removed += sum(1 for _ in node.walk(include_self=True))
            continue
        node.children, child_removed = strip_matching_nodes(
            node.children,
            pattern,
            match_anywhere=match_anywhere,
        )
        removed += child_removed
        retained.append(node)
    return retained, removed


def _cisco_command_identity(
    command: str,
    has_children: bool = False,
    path: list[str] | None = None,
) -> str:
    """把旧式命令参数转换成 Cisco group 语义标识键。

    方法委托统一 identity 解析器，并根据 ``has_children`` 区分块与叶子节点；保留
    该包装层是为了兼容旧内部调用，同时确保新旧入口采用相同的冲突判定规则。
    """
    from .identity import resolve_cisco_identity

    return resolve_cisco_identity(
        command,
        path=path or [],
        node_kind="block" if has_children else "leaf",
    ).key


# 拓扑中经常使用接口缩写，统一展开后才能与配置块可靠匹配。
_CISCO_PREFIXES = {
    "gi": "GigabitEthernet",
    "gigabitethernet": "GigabitEthernet",
    "te": "TenGigE",
    "tengige": "TenGigE",
    "hu": "HundredGigE",
    "hundredgige": "HundredGigE",
    "fo": "FortyGigE",
    "fortygige": "FortyGigE",
    "tf": "TwentyFiveGigE",
    "twentyfivegige": "TwentyFiveGigE",
    "be": "Bundle-Ether",
    "bundle-ether": "Bundle-Ether",
    "lo": "Loopback",
    "loopback": "Loopback",
    "mgmteth": "MgmtEth",
}


def canonical_cisco_interface(value: str) -> str:
    """清理接口名空白、移除头部模式后缀并展开常见 IOS XR 缩写。

    拓扑表可能使用 ``Gi``、``Te`` 或 ``BE``，配置中则通常使用完整名称；统一成
    同一种表示后，端点校验、聚合关系查询和接口树改名才能可靠匹配同一接口。
    """
    value = re.sub(r"\s+l2transport\s*$", "", value.strip(), flags=re.IGNORECASE)
    compact = re.sub(r"\s+", "", value.strip())
    match = re.match(r"([A-Za-z-]+)(.*)", compact)
    if not match:
        return compact
    prefix, suffix = match.groups()
    expanded = _CISCO_PREFIXES.get(prefix.lower(), prefix)
    return f"{expanded}{suffix}"


_CISCO_PHYSICAL_INTERFACE = re.compile(
    r"^(?:Ethernet|FastEthernet|GigabitEthernet|TenGigE|TwentyFiveGigE|"
    r"FortyGigE|FiftyGigE|HundredGigE|FourHundredGigE|POS|Serial)\d",
    re.IGNORECASE,
)
_CISCO_VIRTUAL_INTERFACE = re.compile(
    r"^(?:Tunnel(?:-ip|-te)?|Null|PW-Ether|VASI(?:Left|Right)?|NVE|Multilink)\d",
    re.IGNORECASE,
)
_CISCO_BLOCK_HEADER = re.compile(
    r"^(?:interface|router|vrf|l2vpn|mpls|username|line|"
    r"segment-routing|telemetry)\b",
    re.IGNORECASE,
)


def _is_cisco_block_header(header: str) -> bool:
    """判断顶层命令是否天然表示可进入的配置块。

    空接口、空路由进程等节点没有 children，但仍需保留块语义，以便
    group 展开可以进入该路径并物化继承配置。
    """
    return bool(_CISCO_BLOCK_HEADER.match(header.strip()))


def _cisco_interface_kind(name: str) -> InterfaceKind:
    """按 IOS XR 父接口名前缀返回厂商无关的接口类别。

    方法先去掉子接口后缀，再区分环回、管理、Bundle、BVI、物理和虚拟接口；无法
    识别时返回 ``UNKNOWN``，而不乐观地当作物理口，以免迁移新型控制接口或删除
    解析器尚不了解的配置。
    """
    parent = interface_parent(name).lower()
    if re.fullmatch(r"loopback\d+", parent):
        return InterfaceKind.LOOPBACK
    if re.match(r"^mgmteth\d", parent):
        return InterfaceKind.MANAGEMENT
    if re.fullmatch(r"bundle-ether\d+", parent):
        return InterfaceKind.BUNDLE
    if re.fullmatch(r"bvi\d+", parent):
        return InterfaceKind.GATEWAY
    if re.match(r"^ptp(?:\d|$)", parent, re.IGNORECASE):
        return InterfaceKind.VIRTUAL
    if _CISCO_PHYSICAL_INTERFACE.match(parent):
        return InterfaceKind.PHYSICAL
    if _CISCO_VIRTUAL_INTERFACE.match(parent):
        return InterfaceKind.VIRTUAL
    return InterfaceKind.UNKNOWN


class CiscoDocument(VendorConfiguration):
    """提供可修改 IOS XR 配置树及应用层所需的统一厂商门面。

    类本身负责文本解析、渲染、声明式清洗规则和引用替换；接口迁移、group、清洗及模拟
    适配委托给独立厂商模块。虚拟根节点承载所有顶层命令，使 Cisco 和 Juniper
    都以“Document + root + 单一节点类型”表达完整文档。
    """
    vendor = "cisco_iosxr"

    def __init__(self, text: str):
        """记录输入换行信息，并把原始 IOS XR 文本解析到虚拟根节点下。

        构造时立即建立可修改树，使后续处理器共享同一文档状态；保留节点顺序是为了
        在删除或插入配置后仍尽量维持原配置的组织方式和确定性输出。
        """
        self.trailing_newline = text.endswith("\n")
        self.root = CiscoNode("<root>", is_block=True)
        self.root.children = self._parse(text)

    def hostname(self) -> str | None:
        """返回配置中最后一条有效的顶层 ``hostname`` 命令值，未配置时返回 ``None``。

        IOS XR 用无缩进的顶层 ``hostname`` 单行命令声明设备名；按文档顺序取最后一条
        活动命令以反映加载后的最终值。返回前去除可能包裹的外层引号，便于与 Excel 中
        的设备名直接比较。
        """
        value: str | None = None
        for node in self.root.children:
            if not node.active:
                continue
            match = re.match(r"hostname\s+(.+)$", node.header.strip(), re.IGNORECASE)
            if match:
                value = match.group(1).strip().strip('"').strip("'")
        return value

    @staticmethod
    def _is_group_node(node: CiscoNode) -> bool:
        """判断当前顶层节点是否为 IOS XR group 定义。

        解析器由 ``current`` 同时表示当前顶层块和 group 模式，避免再维护
        一个与它指向同一对象的 ``group_node`` 状态。
        """
        return bool(re.match(r"group\s+\S+", node.header, re.IGNORECASE))

    @staticmethod
    def _parse(text: str) -> list[CiscoNode]:
        """把 IOS XR 文本解析成有序顶层块及持久化缩进 AST。

        普通配置由无缩进命令开始、无缩进 ``!`` 结束；group 是例外，其中看起来像
        顶层命令的内容仍属于 group，只有 ``end-group`` 才结束定义。块内命令使用
        缩进栈构造父子关系：更深缩进成为当前节点的 child，相同或更浅缩进先退栈；
        缩进 ``!`` 按自身深度关闭配置模式并作为格式节点保留。
        """
        nodes: list[CiscoNode] = []
        current: CiscoNode | None = None
        stack: list[tuple[int, CiscoNode]] = []

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            is_indented = bool(line) and line[0].isspace()

            # group 的内部命令可能看起来像普通顶层 header，因此进入 group 后
            # 必须优先消费所有行，只允许 end-group 结束这个状态。
            if current is not None and CiscoDocument._is_group_node(current):
                if stripped.lower() == "end-group":
                    current = None
                    stack = []
                    nodes.append(CiscoNode(header="end-group"))
                else:
                    CiscoDocument._append_child(current, stack, line)
                continue

            # 普通状态下，无缩进命令开启一个新的顶层块。感叹号单独处理，
            # 因为它是否缩进决定了是顶层分隔符还是块内子模式结束符。
            if stripped and not is_indented and stripped != "!":
                current = CiscoNode(
                    header=line,
                    is_block=_is_cisco_block_header(line),
                )
                nodes.append(current)
                stack = []
                continue

            if stripped == "!" and not is_indented:
                current = None
                stack = []
                nodes.append(CiscoNode(header="!"))
                continue

            if current is not None:
                CiscoDocument._append_child(current, stack, line)
            else:
                nodes.append(CiscoNode(header=line))
        return nodes

    @staticmethod
    def _append_child(
        parent_node: CiscoNode,
        stack: list[tuple[int, CiscoNode]],
        line: str,
        *,
        origin: str = "explicit",
    ) -> None:
        """按行缩进把一个块内命令追加到正确父节点。

        普通命令缩进更深时成为栈顶节点的 child，相同或更浅时先退出已结束层级；
        ``!`` 使用同样的退栈规则但自身不入栈，因此既能关闭对应配置模式，又不会
        接管后续命令。空行作为根级格式节点保留，但不改变当前语义层级。
        """
        stripped = line.strip()
        if not stripped:
            parent_node.children.append(CiscoNode("", origin=origin))
            return

        expanded = line.expandtabs(8)
        indent = len(expanded) - len(expanded.lstrip())
        while stack and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1] if stack else None
        node = CiscoNode(stripped, origin=origin)
        if parent is None:
            parent_node.children.append(node)
            parent_node.is_block = True
        else:
            parent.children.append(node)
            parent.is_block = True

        if stripped != "!":
            stack.append((indent, node))

    def _interface_nodes(self) -> list[CiscoNode]:
        """返回仍有效且已识别接口名的 IOS XR 顶层节点。

        具体筛选委托接口模块，保证兼容旧内部入口的同时，让所有调用方复用同一套
        活动状态和接口识别规则，避免操作已逻辑删除的块。
        """
        return self._interfaces().interface_nodes(self)

    @staticmethod
    def _interfaces():
        """延迟返回 Cisco 接口操作模块。

        接口模块需要引用 ``CiscoDocument`` 和 ``CiscoNode``；在方法调用时再导入
        可打破模块初始化阶段的循环依赖，同时保持文档类对外提供稳定门面。
        """
        from . import interfaces

        return interfaces

    def interface_specs(self) -> list[InterfaceSpec]:
        """提取接口父子关系、类型及 VLAN/QinQ 信息。

        返回厂商无关的 ``InterfaceSpec``，使 NNI/UNI 规划无需理解 IOS XR 配置行；
        统一规格也是业务口筛选、VLAN 分配和审计映射共享同一事实来源的基础。
        """
        return self._interfaces().interface_specs(self)

    def interface_kind(self, name: str) -> InterfaceKind:
        """按 IOS XR 规则返回指定接口的厂商无关类别。

        拓扑预检和配置扫描都通过同一入口分类，可避免一处把接口视为物理口、另一处
        又视为虚拟口，从而防止不合法端点参与 NNI 映射。
        """
        return self._interfaces().interface_kind(self, name)

    def bundle_members(self) -> dict[str, str]:
        """返回物理成员接口到 ``Bundle-Ether`` 聚合父口的映射。

        NNI/UNI 扁平化需要从拓扑物理端点反查承载业务的逻辑聚合口，并在迁移完成后
        清理真实成员，因此统一暴露该方向的映射。
        """
        return self._interfaces().bundle_members(self)

    def resolve_interface(self, value: str) -> str:
        """把拓扑中的 IOS XR 接口写法转换成配置解析器的规范形式。

        拓扑数据和设备配置可能使用不同缩写或空白格式；在所有查找前统一名称可避免
        同一接口被误认为两个端点，或因匹配失败而遗漏聚合关系。
        """
        return self._interfaces().resolve_interface(self, value)

    def logical_names_under(self, parent: str) -> list[str]:
        """列出指定父接口自身及其所有已配置子接口名称。

        NNI 克隆和映射审计必须同时覆盖父口与每个子接口；集中枚举可保留子接口后缀，
        并避免各处理器自行扫描块模型产生不一致结果。
        """
        return self._interfaces().logical_names_under(self, parent)

    def business_interface_names(self) -> set[str]:
        """识别真正承载三层或二层业务的 IOS XR 接口。

        直接业务包括 IPv4/IPv6、l2transport、xconnect 等；此外，
        被 L2VPN、bridge-domain、路由协议等全局配置引用的接口也视为活跃业务口。
        BVI 不因自身有 IP 或编号类似 VLAN 就自动迁移，只有显式关联它的
        bridge-domain 仍包含活跃 attachment circuit 时才作为网关迁移。
        """
        return self._interfaces().business_interface_names(self)

    def _find_interface_node(self, name: str) -> CiscoNode | None:
        """按规范化名称查找第一个有效 IOS XR 接口块。

        这是保留给旧内部调用的块级入口；统一委托接口模块可确保查找忽略已停用块，
        避免后续修改落到不会被渲染的旧定义上。
        """
        return self._interfaces().find_interface_node(self, name)

    @staticmethod
    def _parse_cisco_nodes(lines: Iterable[str], origin: str = "explicit") -> list[CiscoNode]:
        """使用文档同款缩进规则把独立文本行解析成语义节点树。

        该兼容入口以临时块复用持久 AST 构造器，随后过滤 ``!`` 和空行；Group 展开
        等语义算法因此与主文档共享唯一解析规则，而不会重新实现一套临时解析器。
        """
        root = CiscoNode("<root>", is_block=True)
        stack: list[tuple[int, CiscoNode]] = []
        for raw in lines:
            CiscoDocument._append_child(root, stack, raw.rstrip(), origin=origin)
        return _clone_cisco_nodes(root.children, origin=origin)

    @staticmethod
    def _render_cisco_nodes(nodes: list[CiscoNode], depth: int = 1) -> list[str]:
        """按指定起始深度渲染 Cisco 节点树。

        兼容入口直接调用文档 AST 的统一渲染器，使 Group 合并结果、接口树和普通配置
        块使用完全相同的缩进及格式节点规则。
        """
        return _render_cisco_nodes(nodes, depth)

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """展开全部已应用 IOS XR group，并返回事件、冲突及成败信息。

        group 继承必须在接口分类前物化，但其语义远比基础块解析复杂；委托专用展开器
        可以隔离冲突处理，并让调用方通过结果对象决定是否安全继续转换。
        """
        from .groups import CiscoGroupExpander

        return CiscoGroupExpander(self).expand_groups(known_interfaces, policy)

    def remove_interface(self, name: str, include_children: bool = False) -> None:
        """逻辑停用指定接口，并可连同全部点号子接口一起停用。

        接口模块通过活动标记保留原块而非直接删除，便于维持文档顺序；父口迁移结束
        时使用 ``include_children`` 可一次清理整棵旧接口树。
        """
        self._interfaces().remove_interface(self, name, include_children)

    def rename_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """重命名源接口树，并可移除不再适用的聚合成员属性。

        NNI/UNI 暂存与最终落位都需要父口和子接口保持相同后缀；统一树级改名还能在
        目标重名时复用接口模块的合并规则，避免重复定义。
        """
        self._interfaces().rename_interface_tree(self, source, target, strip_bundle)

    def clone_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """把一棵 IOS XR 接口配置深拷贝到新目标接口。

        M-LAG 可能要求同一逻辑聚合按多个对端拆成多份，不能直接改名并消耗唯一源树；
        克隆保留源配置供后续目标继续复制，``strip_bundle`` 则防止旧成员属性泄漏。
        """
        self._interfaces().clone_interface_tree(self, source, target, strip_bundle)

    def _merge_duplicate_interface(self, preferred: CiscoNode) -> None:
        """把同名接口块的非重复配置子树合并到首选块。

        接口改名或克隆可能撞上已有目标定义；合并而非覆盖可以保留双方有效配置，
        同时停用多余块，确保最终 IOS XR 输出只有一个活动接口定义。
        """
        self._interfaces()._merge_duplicate_interface(self, preferred)

    def map_uni(self, source: str, target_parent: str, vlan: int, inner_vlan: int) -> str:
        """把源 UNI 业务改写到目标父口的 QinQ 子接口并返回目标名。

        外层 VLAN 用作实验网络运输标签，内层 VLAN 保留原业务语义；由厂商接口模块
        统一移除冲突的旧封装，可确保生成的 IOS XR 终结配置只有一套标签规则。
        """
        return self._interfaces().map_uni(self, source, target_parent, vlan, inner_vlan)

    def finalize_uni_source(self, source_parent: str) -> None:
        """在所有 UNI 业务复制完成后停用源父接口及其子接口。

        清理必须延后到全部子接口映射完成，否则较早迁移的 unit 可能删除后续 unit
        仍需读取的共同父树；集中收尾也可避免临时占位接口进入最终输出。
        """
        self.remove_interface(source_parent, include_children=True)

    def ensure_parent_interface(self, name: str) -> None:
        """确保 UNI 目标父接口存在，并在新建时默认启用。

        迁移结果可能只生成 QinQ 子接口，但 IOS XR 仍需要明确的承载父口；已有父口
        保持原配置，新建父口则以可工作的最小状态出现。
        """
        self._interfaces().ensure_parent_interface(self, name)

    def adapt_to_simulation(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """按镜像策略适配已迁移数据口及 IOS XR 模拟运行参数。

        适配逻辑与基础语法解析分离，可针对不同镜像处理接口启停、物理特性或稳定性
        参数；返回分类统计则让上层报告完整说明自动修改的范围。
        """
        from .simulation import adapt_to_simulation

        return adapt_to_simulation(self, policy, data_interfaces)

    def adjust_simulation_parameters(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """通过旧方法名调用当前的模拟参数适配入口。

        保留该别名可避免既有调用方立即失效，同时只委托新方法，不复制实现，确保
        两个入口始终使用同一策略和统计行为。
        """
        return self.adapt_to_simulation(policy, data_interfaces)

    def clean_management_access(self) -> CleanupOutcome:
        """清除 IOS XR 原有管理访问和认证配置，并返回分类统计。

        生产账号、AAA 和远程管理凭据不应进入实验环境；委托独立清洗模块可以集中
        维护敏感命令模式，同时让文档门面保持统一调用接口。
        """
        from .cleaning import clean_management_access

        return clean_management_access(self)

    def add_lab_account(self) -> None:
        """向 IOS XR 文档添加统一实验账号。

        清除生产认证后必须建立已知的本地登录入口，防止输出设备无法接管；账号语法
        由厂商清洗模块生成，使凭据替换与旧认证删除使用同一实现边界。
        """
        from .cleaning import add_lab_account

        add_lab_account(self)

    def apply_cleaning_rule(
        self,
        path: tuple[str, ...],
        match: str,
        action: str,
        value: str | None,
    ) -> int:
        """按完整父级路径遍历 IOS XR 配置树并执行一条清洗规则。"""
        pattern = re.compile(match, re.IGNORECASE)
        hits = 0

        def walk(node: CiscoNode, ancestors: tuple[str, ...]) -> None:
            nonlocal hits
            if not node.active or node.is_formatting:
                return
            header = node.header.strip()
            if matches_cleaning_path(ancestors, path) and pattern.search(header):
                hits += 1
                if action == "delete":
                    node.active = False
                    return
                if action == "replace":
                    node.header = pattern.sub(value or "", node.header)
                elif action == "mask":
                    node.header = pattern.sub("<masked>", node.header)
                header = node.header.strip()
            for child in node.children:
                walk(child, (*ancestors, header))

        for block in self.root.children:
            walk(block, ())
        return hits

    def replace_references(self, replacements: dict[str, list[str]]) -> None:
        """更新 IOS XR 配置中的旧接口引用，并展开一对多 M-LAG 目标。

        方法先规范化并去重替换表，再基于原 AST 一次性生成命令变体；接口
        header 由迁移阶段负责，这里递归更新其内部节点，其他顶层块则按目标数克隆。
        节点及子树整体复制可避免层级丢失，也防止新目标名再次命中旧源规则。
        """
        if not replacements:
            return
        canonical = {
            canonical_cisco_interface(source): list(dict.fromkeys(targets))
            for source, targets in replacements.items()
            if targets and targets != [source]
        }
        if not canonical:
            return
        names = sorted(canonical, key=len, reverse=True)
        pattern = re.compile(
            r"(?<![A-Za-z0-9_.-])(" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9_.-])"
        )

        def expand(value: str) -> list[str]:
            """基于原始文本的所有命中位置生成去重后的替换变体。

            先收集匹配再组合目标可支持一个源接口映射到多个 M-LAG 端口，并避免已
            替换出的目标名称再次作为源名称参与级联替换。
            """
            matches = list(pattern.finditer(value))
            if not matches:
                return [value]
            variants = [""]
            cursor = 0
            for match in matches:
                prefix = value[cursor : match.start()]
                variants = [
                    current + prefix + target
                    for current in variants
                    for target in canonical[match.group(1)]
                ]
                cursor = match.end()
            return list(dict.fromkeys(current + value[cursor:] for current in variants))

        def expand_nodes(nodes: list[CiscoNode]) -> list[CiscoNode]:
            """递归展开节点命令中的接口引用并保留子树结构。

            每个命令变体获得独立深拷贝，随后递归处理其 children；因此一对多 M-LAG
            引用可以在任意深度展开，而不会把子命令留在被替换节点之外。
            """
            expanded: list[CiscoNode] = []
            for node in nodes:
                if node.is_formatting:
                    expanded.append(node.clone())
                    continue
                for header in expand(node.header):
                    clone = node.clone()
                    clone.header = header
                    clone.children = expand_nodes(clone.children)
                    expanded.append(clone)
            return expanded

        rebuilt: list[CiscoNode] = []
        for block in self.root.children:
            if not block.active or block.interface_name:
                if block.active:
                    block.children = expand_nodes(block.children)
                rebuilt.append(block)
                continue
            header_variants = expand(block.header)
            for header in header_variants:
                clone = copy.deepcopy(block)
                clone.header = header
                clone.children = expand_nodes(clone.children)
                rebuilt.append(clone)
        self.root.children = rebuilt

    def render(self) -> str:
        """按虚拟根下的文档顺序渲染所有活动 IOS XR 节点。

        逻辑停用的块被跳过，未知命令和格式节点按现有树保留；语义节点的缩进由
        AST 深度统一生成。稳定顺序和统一末尾换行便于设备加载和生成确定性差异。
        """
        lines = _render_cisco_nodes(self.root.children, depth=0)
        text = "\n".join(lines).rstrip() + "\n"
        return text
