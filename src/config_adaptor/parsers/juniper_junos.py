"""Juniper Junos 语法模型及面向应用层的兼容门面。"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Iterable

from ..models import SimulationAdaptationPolicy, WashingPolicy
from .common import (
    CleanupOutcome,
    GroupExpansionOutcome,
    InterfaceKind,
    InterfaceSpec,
    SimulationAdaptationOutcome,
    interface_parent,
    normalized_command as _normalized_command,
)


def _junos_statement_identity(command: str, path: list[str] | None = None) -> str:
    """把旧式 Junos 命令与路径参数转换成 group 语义标识键。

    方法委托统一 identity 解析器，保留该包装层是为了兼容旧内部调用，同时让新旧
    group 展开入口共享同一条语义冲突判定路径，避免出现不同的合并结果。
    """
    from ..vendor.juniper.identity import resolve_junos_identity

    return resolve_junos_identity(command, path=path or []).key


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


@dataclass
class JunosNode:
    """表示 Junos 大括号配置树中的块节点或叶子语句。

    ``children=None`` 表示叶子，列表表示块；``active`` 仅表示转换过程中的逻辑
    删除，Junos 文本里的 ``inactive:`` 由 ``effective`` 单独识别。``origin`` 和
    ``rank`` 为 group 继承保留来源及优先级。统一节点模型可递归修改任意层级，
    同时保留解析器暂时不理解的语句文本。
    """
    header: str
    children: list["JunosNode"] | None = None
    active: bool = True
    origin: str = "explicit"
    rank: tuple[int, ...] = (1_000_000, 0)

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
        remaining = self.header.strip()
        inactive = False
        while True:
            matched = False
            for prefix in ("inactive:", "protect:"):
                if remaining.startswith(prefix):
                    inactive = inactive or prefix == "inactive:"
                    remaining = remaining[len(prefix) :].strip()
                    matched = True
                    break
            if not matched:
                return inactive

    @property
    def effective(self) -> bool:
        """返回节点是否应参与当前生效配置的语义处理。"""
        return self.active and not self.configured_inactive

    def clone(self) -> "JunosNode":
        """深拷贝当前节点及其完整子树。

        M-LAG 拆分、引用一对多展开和 UNI unit 复制都需要从同一源配置生成多个目标；
        深拷贝可防止修改某个目标时连带改变原节点或其他副本。
        """
        return copy.deepcopy(self)

class JunosDocument:
    """提供可修改 Junos 配置树及应用层所需的统一厂商门面。

    类本身负责大括号解析、渲染、外部规则和引用替换；接口迁移、group、清洗及模拟
    适配委托给独立厂商模块。这样应用层不需要了解 ``JunosNode``，厂商语法操作也
    能按职责拆分，而仍由一个文档对象维护完整转换状态。
    """
    vendor = "juniper_junos"

    def __init__(self, text: str):
        """创建不参与渲染的虚拟根节点，并解析完整 Junos 文本。

        虚拟根统一承载多个顶层块，使所有遍历都可从单一节点开始；构造时立即解析并
        校验括号，可在任何配置改写发生前拒绝结构损坏的输入。
        """
        self.root = JunosNode("<root>", [])
        self._parse(text)

    @staticmethod
    def _expand_inline_blocks(text: str) -> str:
        """把单行块预展开为原逐行解析器可识别的标准多行文本。

        只在引号和注释之外按 ``{``、``}``、``;`` 切行；本方法不建立语法树，
        后续结构识别、括号校验和节点创建仍全部由原有 ``_parse`` 栈逻辑完成。
        """
        expanded: list[str] = []
        quote: str | None = None
        escaped = False
        block_comment = False

        for raw in text.splitlines():
            fragments: list[str] = []
            buffer: list[str] = []
            index = 0

            def flush() -> None:
                value = "".join(buffer).strip()
                buffer.clear()
                if value:
                    fragments.append(value)

            while index < len(raw):
                character = raw[index]

                if quote is not None:
                    buffer.append(character)
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == quote:
                        quote = None
                    index += 1
                    continue

                if block_comment:
                    buffer.append(character)
                    if character == "*" and index + 1 < len(raw) and raw[index + 1] == "/":
                        buffer.append("/")
                        block_comment = False
                        index += 2
                    else:
                        index += 1
                    continue

                if character in {'"', "'"}:
                    quote = character
                    buffer.append(character)
                    index += 1
                    continue

                if character == "#":
                    buffer.append(raw[index:])
                    break

                if character == "/" and index + 1 < len(raw) and raw[index + 1] == "*":
                    block_comment = True
                    buffer.extend(("/", "*"))
                    index += 2
                    continue

                if character == "{":
                    buffer.append("{")
                    flush()
                    index += 1
                    continue

                if character == ";":
                    buffer.append(";")
                    flush()
                    index += 1
                    continue

                if character == "}":
                    flush()
                    closing = "}"
                    if index + 1 < len(raw) and raw[index + 1] == ";":
                        closing = "};"
                        index += 1
                    fragments.append(closing)
                    index += 1
                    continue

                buffer.append(character)
                index += 1

            flush()
            expanded.extend(fragments or [""])

        return "\n".join(expanded)

    def _parse(self, text: str) -> None:
        """使用栈把 Junos 大括号文本解析成节点树，并校验括号平衡。

        普通块和叶子被结构化，空行及暂不支持的行内大括号语法则原样作为叶子保留；
        这种保守降级避免不完整解析器擅自重写未知语法。多余或缺失右括号会立即报错，
        因为在不可靠树上继续迁移可能把配置写入错误层级。
        """
        stack = [self.root]
        expanded = self._expand_inline_blocks(text)
        for number, raw in enumerate(expanded.splitlines(), start=1):
            stripped = raw.strip()
            if not stripped:
                stack[-1].children.append(JunosNode(""))
                continue
            if stripped in {"}", "};"}:
                if len(stack) == 1:
                    raise ValueError(f"Junos 配置第 {number} 行出现多余右大括号")
                stack.pop()
                continue
            if stripped.endswith("{"):
                node = JunosNode(stripped[:-1].strip(), [])
                stack[-1].children.append(node)
                stack.append(node)
                continue
            if "{" in stripped or "}" in stripped:
                # 预处理未拆分的未知写法继续按原策略作为叶子保留。
                stack[-1].children.append(JunosNode(stripped))
                continue
            stack[-1].children.append(JunosNode(stripped))
        if len(stack) != 1:
            raise ValueError("Junos 配置大括号不平衡")

    @staticmethod
    def _base_header(header: str) -> str:
        """去除 ``inactive:``/``protect:`` 前缀并返回基础语句文本。

        这些前缀描述 Junos 节点状态而不是命令身份；语义匹配时忽略它们，才能让接口
        名、块名和清洗规则稳定识别同一命令。原始 header 仍保留在树中，输出阶段再
        单独决定保留 ``inactive:`` 并移除 ``protect:``。
        """
        result = header.strip()
        while True:
            for prefix in ("inactive:", "protect:"):
                if result.startswith(prefix):
                    result = result[len(prefix) :].strip()
                    break
            else:
                return result

    @staticmethod
    def _output_header(header: str) -> str:
        """移除输出中无运行意义的 ``protect:``，并保留 ``inactive:``。

        GNS3 配置不需要继承生产设备上的编辑保护；组合前缀会统一重建为
        ``inactive:`` 加基础语句，确保去保护不会意外激活原本停用的节点。
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
                return f"inactive: {result}" if inactive else result

    def _top_block(self, name: str, create: bool = False) -> JunosNode | None:
        """查找指定活动顶层块，并在请求时创建缺失块。

        清洗和账号注入等操作经常需要访问 ``system``、``security`` 或 ``interfaces``；
        统一入口可避免各模块重复遍历根节点，并保证新块只在明确 ``create=True`` 时
        追加，从而不因只读查询意外改变配置。
        """
        assert self.root.children is not None
        for node in self.root.children:
            if node.effective and node.is_block and self._base_header(node.header) == name:
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
        return [node for node in block.children if node.effective and node.is_block]

    def _interface_name(self, node: JunosNode) -> str:
        """从接口节点 header 的首个字段提取规范化接口名。

        先移除状态前缀再规范化，可使 ``inactive:``、``protect:`` 或空白差异不影响
        同名节点合并和接口查找。
        """
        return canonical_junos_interface(self._base_header(node.header).split()[0])

    def _unit_nodes(self, interface: JunosNode) -> list[JunosNode]:
        """列出接口节点下所有结构化的 ``unit`` 子块。

        Junos 把逻辑接口嵌套在父口内；集中枚举 unit 可让规格提取和 UNI 复制使用
        相同结构判定，同时忽略父口层面的普通叶子配置。
        """
        if interface.children is None:
            return []
        return [
            node
            for node in interface.children
            if node.effective
            and node.is_block
            and self._base_header(node.header).startswith("unit ")
        ]

    def _unit_number(self, node: JunosNode) -> str:
        """从已识别的 ``unit`` 节点头提取逻辑单元编号。

        保留字符串形式可兼容解析阶段尚未限制的输入，并在拼接完整接口名及选择源
        unit 时保持原始编号表达。
        """
        return self._base_header(node.header).split(maxsplit=1)[1]

    def _find_vlan(self, unit: JunosNode) -> int | None:
        """读取 unit 的单层 VLAN 或 QinQ 外层 VLAN，未配置时返回 ``None``。

        UNI 规划需要一个可保留或重新分配的外层标签；同时识别 ``vlan-id`` 和
        ``vlan-tags outer``，可让单层与双层终结使用同一 ``InterfaceSpec.vlan``。
        """
        if unit.children is None:
            return None
        for child in unit.children:
            if not child.effective:
                continue
            statement = self._base_header(child.header)
            match = re.match(r"vlan-id\s+(\d+)\s*;", statement)
            if match:
                return int(match.group(1))
            tags = re.match(r"vlan-tags\s+outer\s+(\d+)\s+inner\s+\d+\s*;", statement)
            if tags:
                return int(tags.group(1))
        return None

    def _find_inner_vlan(self, unit: JunosNode) -> int | None:
        """读取 ``vlan-tags`` 语句中的 QinQ 内层 VLAN。

        内层标签代表原客户业务 VLAN，UNI 汇聚时应优先保留；单层或无标签 unit
        返回 ``None``，让上层按明确的回退规则选择内层标签。
        """
        if unit.children is None:
            return None
        for child in unit.children:
            if not child.effective:
                continue
            match = re.match(
                r"vlan-tags\s+outer\s+\d+\s+inner\s+(\d+)\s*;",
                self._base_header(child.header),
            )
            if match:
                return int(match.group(1))
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
        from ..vendor.juniper.groups import JunosGroupExpander

        return JunosGroupExpander(self).expand_groups(known_interfaces, policy)

    def interface_specs(self) -> list[InterfaceSpec]:
        """提取父接口或 unit 的关系、类型及 VLAN/QinQ 信息。

        返回厂商无关的 ``InterfaceSpec``，使 NNI/UNI 规划无需遍历 Junos 节点；统一
        规格也让业务筛选、VLAN 分配和审计映射共享同一事实来源。
        """
        return self._interfaces().interface_specs(self)

    @staticmethod
    def _interfaces():
        """延迟返回 Juniper 接口操作模块。

        接口模块需要引用 ``JunosDocument`` 和 ``JunosNode``；在方法调用时再导入可
        打破模块初始化阶段的循环依赖，同时保持文档类对外提供稳定门面。
        """
        from ..vendor.juniper import interfaces

        return interfaces

    def interface_kind(self, name: str) -> InterfaceKind:
        """按 Junos 规则返回指定接口的厂商无关类别。

        拓扑预检和配置扫描都通过同一入口分类，可避免对物理口、ae、IRB 或控制接口
        产生不同判断，从而阻止不合法端点参与 NNI 映射。
        """
        return self._interfaces().interface_kind(self, name)

    def bundle_members(self) -> dict[str, str]:
        """返回物理接口到 ``ae`` 聚合父口的映射。

        NNI/UNI 扁平化需要从拓扑物理端点反查真正承载业务的逻辑聚合口，并在迁移后
        清理原成员，因此统一暴露该方向的关系。
        """
        return self._interfaces().bundle_members(self)

    def resolve_interface(self, value: str) -> str:
        """把拓扑中的 Junos 接口写法转换为解析器的规范形式。

        所有查找先经过统一规范化，可消除空白差异，避免同一接口在拓扑和配置树之间
        匹配失败或被重复处理。
        """
        return self._interfaces().resolve_interface(self, value)

    def logical_names_under(self, parent: str) -> list[str]:
        """列出指定父接口对应的已配置业务名称或全部 unit 名称。

        NNI 克隆和映射审计需要逐个保留逻辑单元后缀；集中枚举可避免处理器自行遍历
        Junos 子树，并保证无 unit 父口与有 unit 接口使用一致入口。
        """
        return self._interfaces().logical_names_under(self, parent)

    def business_interface_names(self) -> set[str]:
        """识别承载三层、二层及活跃广播域网关的 Junos 接口。

        IRB 不因自身配置地址就自动迁移；只有 bridge-domain/vlan 中仍有
        活跃接入口，或其 unit 命中活跃业务 VLAN 时才进入 UNI 计划。
        """
        return self._interfaces().business_interface_names(self)

    def _find_interface_node(self, name: str) -> JunosNode | None:
        """按父接口名查找第一个有效 Junos 接口节点。

        unit 并非顶层节点，因此查找会归一到父口；保留该兼容入口并委托接口模块，
        可确保所有旧调用方复用相同的规范化和活动状态规则。
        """
        return self._interfaces().find_interface_node(self, name)

    def remove_interface(self, name: str, include_children: bool = False) -> None:
        """逻辑停用接口所在的整个 Junos 父节点及其 unit 子树。

        Junos unit 天然嵌套在父口中，因此删除父节点即可覆盖子接口；保留
        ``include_children`` 参数是为了实现统一厂商协议，而不需要伪造独立 unit 删除。
        """
        self._interfaces().remove_interface(self, name, include_children)

    def rename_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """重命名整个 Junos 接口节点，并可移除聚合相关 options。

        父节点改名会自然携带全部 unit，适合 NNI/UNI 暂存与最终落位；迁出 ae 成员时
        清除旧 options，可避免目标物理口继续引用已不存在的聚合关系。
        """
        self._interfaces().rename_interface_tree(self, source, target, strip_bundle)

    def clone_interface_tree(self, source: str, target: str, strip_bundle: bool = False) -> None:
        """把一棵 Junos 接口子树深拷贝到新目标接口。

        M-LAG 可能要求同一 ae 业务按多个对端拆成多份，不能直接改名并消耗唯一源树；
        克隆保留源配置供后续目标继续复制，``strip_bundle`` 防止旧成员选项泄漏。
        """
        self._interfaces().clone_interface_tree(self, source, target, strip_bundle)

    def _merge_duplicate_interface(self, preferred: JunosNode) -> None:
        """把同名接口节点的非重复子树合并到首选节点并停用副本。

        接口改名或克隆可能撞上已有目标定义；按渲染结果去重可保留双方不同配置，
        又能确保最终 ``interfaces`` 下只有一个活动的同名节点。
        """
        self._interfaces()._merge_duplicate_interface(self, preferred)

    def _strip_vlan_termination(self, nodes: list[JunosNode]) -> list[JunosNode]:
        """复制节点树并递归删除旧 VLAN 匹配、标签模式及 rewrite 配置。

        新 QinQ unit 不能与原 ``vlan-id``、``vlan-tags`` 或 map 规则并存；返回清洗后
        的副本可保留地址、family 和 CCC 等业务，同时不修改仍供其他迁移读取的源树。
        """
        return self._interfaces()._strip_vlan_termination(self, nodes)

    def _ensure_target_parent(self, target_parent: str) -> JunosNode:
        """确保 UNI 目标父口存在并配置为支持灵活 QinQ unit。

        方法保留已有 unit，清理冲突的父口 VLAN 终结，并补齐 flexible 封装；集中准备
        父口可让所有新 unit 使用一致能力，又不覆盖目标口上已存在的业务。
        """
        return self._interfaces()._ensure_target_parent(self, target_parent)

    def map_uni(self, source: str, target_parent: str, vlan: int, inner_vlan: int) -> str:
        """复制源业务到目标父口，并重写成 QinQ ``vlan-tags`` unit。

        外层 VLAN 用作实验网络运输标签，内层 VLAN 保留原业务语义；复制而不立即删除
        源父口，是因为同一父口的其他 unit 可能仍需逐个迁移，最终再统一清理更安全。
        """
        return self._interfaces().map_uni(self, source, target_parent, vlan, inner_vlan)

    def finalize_uni_source(self, source_parent: str) -> None:
        """在全部 UNI unit 复制完成后停用源父接口子树。

        延后清理可避免较早迁移的 unit 删除后续 unit 仍需读取的共同父节点，也确保
        临时占位接口不会出现在最终 Junos 配置中。
        """
        self.remove_interface(source_parent, include_children=True)

    def ensure_parent_interface(self, name: str) -> None:
        """确保 UNI 目标父接口存在并具备灵活 VLAN 封装能力。

        即使迁移结果只有 unit，Junos 仍需要正确配置的承载父节点；复用统一准备逻辑
        可避免不同入口生成不一致或无法承载 QinQ 的父口。
        """
        self._interfaces().ensure_parent_interface(self, name)

    def adapt_to_simulation(
        self,
        policy: SimulationAdaptationPolicy,
        data_interfaces: set[str],
    ) -> SimulationAdaptationOutcome:
        """按镜像策略适配已迁移数据口及 Junos 模拟运行参数。

        适配逻辑与基础 AST 分离，可针对不同 vMX 镜像处理接口启停、物理选项或稳定性
        参数；返回分类统计则让上层报告完整说明自动修改的范围。
        """
        from ..vendor.juniper.simulation import adapt_to_simulation

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
        """清除 Junos 原有管理访问和认证配置，并返回分类统计。

        生产账号、外部 AAA、SNMP 和远程管理凭据不应进入实验环境；委托独立清洗
        模块可集中维护敏感节点规则，同时让文档门面保持统一调用接口。
        """
        from ..vendor.juniper.cleaning import clean_management_access

        return clean_management_access(self)

    def clean_optional_features(
        self,
        policy: WashingPolicy,
    ) -> CleanupOutcome:
        """按显式策略清理 Junos 的可选能力配置并返回统计。

        PKI、chassis、NAT、流量统计和协议认证会影响业务语义，只有策略开启时才应
        删除；交给厂商清洗模块可按 Junos 层级递归处理，而上层无需接触节点树。
        """
        from ..vendor.juniper.cleaning import clean_optional_features

        return clean_optional_features(self, policy)

    def clean_authentication(
        self,
        policy: WashingPolicy | None = None,
    ) -> CleanupOutcome:
        """兼容旧入口，顺序组合管理面清洗与策略控制的可选清洗。

        旧调用方期望一次完成两类操作，因此该方法合并两个结果；新流程将它们拆成
        独立阶段以区分强制安全处理和可能改变业务能力的显式选择。
        """
        outcome = self.clean_management_access()
        outcome.merge(self.clean_optional_features(policy or WashingPolicy()))
        return outcome

    def add_lab_account(self) -> None:
        """向 Junos 文档添加统一实验账号及根认证信息。

        清除生产认证后必须建立已知登录入口，防止输出设备无法接管；账号节点由厂商
        清洗模块生成，使凭据替换与旧认证删除使用同一实现边界。
        """
        from ..vendor.juniper.cleaning import add_lab_account

        add_lab_account(self)

    def apply_cleaning_rule(
        self,
        match: str,
        action: str,
        value: str | None,
    ) -> int:
        """递归匹配 Junos 节点路径或 header，并执行外部清洗规则。

        方法支持删除、替换和脱敏，返回活动节点命中数；路径匹配让规则可定位嵌套
        层级，而封装遍历细节可使外部规则模块不依赖 ``JunosNode`` 的内部表示。
        """
        pattern = re.compile(match, re.IGNORECASE)
        hits = 0

        def walk(node: JunosNode, path: list[str]) -> None:
            """深度优先遍历节点，并维护供规则匹配的点分层级路径。

            每层只取 header 的首个语义字段构造路径，同时仍允许正则直接匹配完整
            header，以兼顾结构化定位和具体语句内容匹配。
            """
            nonlocal hits
            if node is not self.root and not node.effective:
                return
            if node is self.root:
                next_path = path
            else:
                component = self._base_header(node.header).split(maxsplit=1)[0].rstrip(";")
                next_path = [*path, component] if component else path
                dotted = ".".join(next_path)
                if pattern.search(dotted) or pattern.search(node.header):
                    hits += 1
                    if action == "delete":
                        node.active = False
                    elif action == "replace":
                        node.header = pattern.sub(value or "", node.header)
                    elif action == "mask":
                        node.header = "<masked>;"
            if node.children:
                for child in node.children:
                    walk(child, next_path)

        walk(self.root, [])
        return hits

    def replace_references(self, replacements: dict[str, list[str]]) -> None:
        """递归更新接口定义树之外的旧接口引用，并展开一对多 M-LAG 节点。

        方法先规范化并去重映射，再基于原 header 一次性生成节点变体；``interfaces``
        子树已由迁移阶段处理，因此这里不二次改名。这样协议、策略和业务引用能跟随
        最终接口映射，又不会因目标名命中另一条源规则而发生级联替换。
        """
        relevant = {
            canonical_junos_interface(source): list(dict.fromkeys(targets))
            for source, targets in replacements.items()
            if targets and targets != [source]
        }
        if not relevant:
            return
        names = sorted(relevant, key=len, reverse=True)
        pattern = re.compile(
            r"(?<![A-Za-z0-9_.-])(" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9_.-])"
        )

        def expand(value: str) -> list[str]:
            """基于原始节点 header 的命中位置生成去重后的替换变体。

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
                    for target in relevant[match.group(1)]
                ]
                cursor = match.end()
            return list(dict.fromkeys(current + value[cursor:] for current in variants))

        def walk(node: JunosNode, inside_interfaces: bool = False) -> None:
            """递归重建子节点列表，并在父层复制需要一对多展开的节点。

            进入 ``interfaces`` 后只继续遍历、不替换 header，因为接口定义已经完成
            专门迁移；在父节点重建列表则允许一个引用节点安全扩展成多个独立副本。
            """
            if node is not self.root and not node.effective:
                return
            if node.children is None:
                return
            current_inside = inside_interfaces or (
                node is not self.root and self._base_header(node.header) == "interfaces"
            )
            rebuilt: list[JunosNode] = []
            for child in node.children:
                if not child.effective:
                    rebuilt.append(child)
                    continue
                variants = [child.header] if current_inside else expand(child.header)
                for header in variants:
                    clone = child if len(variants) == 1 and header == child.header else child.clone()
                    clone.header = header
                    walk(clone, current_inside)
                    rebuilt.append(clone)
            node.children = rebuilt

        walk(self.root)

    def _render_node(self, node: JunosNode, depth: int) -> str:
        """按深度递归渲染一个活动 Junos 节点及其子树。

        叶子直接输出 header，块节点补齐缩进和大括号，逻辑停用节点返回空文本；
        将渲染集中在一处可保证克隆、清洗和迁移后的所有节点使用一致格式。
        """
        if not node.active:
            return ""
        indent = "    " * depth
        if node.children is None:
            return indent + self._output_header(node.header)
        lines = [indent + self._output_header(node.header) + " {"]
        for child in node.children:
            rendered = self._render_node(child, depth + 1)
            if rendered:
                lines.append(rendered)
        lines.append(indent + "}")
        return "\n".join(lines)

    def _render_effective_node(self, node: JunosNode, depth: int) -> str:
        """渲染仅包含当前生效节点的语义视图，不改变最终输出。"""
        if not node.effective:
            return ""
        indent = "    " * depth
        if node.children is None:
            return indent + self._output_header(node.header)
        lines = [indent + self._output_header(node.header) + " {"]
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
        assert self.root.children is not None
        rendered = [self._render_node(node, 0) for node in self.root.children if node.active]
        return "\n".join(item for item in rendered if item).rstrip() + "\n"
