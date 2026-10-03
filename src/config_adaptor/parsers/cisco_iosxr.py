"""Cisco IOS XR 语法模型及面向应用层的兼容门面。"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
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


@dataclass
class _CiscoNode:
    """表示 IOS XR group 展开过程中按缩进构造的临时语法节点。

    节点保存命令、子节点、块类型、来源和继承优先级，只服务于 group 的语义合并；
    主文档仍使用 ``CiscoBlock``。两种模型分离可避免为了少量层级配置而把整个
    IOS XR 文档强制转换成树，同时让 group 展开器拥有处理继承冲突所需的信息。
    """
    command: str
    children: list["_CiscoNode"] = field(default_factory=list)
    is_block: bool = False
    origin: str = "explicit"
    rank: tuple[int, int] = (1_000_000, 0)


def _cisco_command_identity(
    command: str,
    has_children: bool = False,
    path: list[str] | None = None,
) -> str:
    """把旧式命令参数转换成 Cisco group 语义标识键。

    方法委托统一 identity 解析器，并根据 ``has_children`` 区分块与叶子节点；保留
    该包装层是为了兼容旧内部调用，同时确保新旧入口采用相同的冲突判定规则。
    """
    from ..vendor.cisco.identity import resolve_cisco_identity

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
    if _CISCO_PHYSICAL_INTERFACE.match(parent):
        return InterfaceKind.PHYSICAL
    if _CISCO_VIRTUAL_INTERFACE.match(parent):
        return InterfaceKind.VIRTUAL
    return InterfaceKind.UNKNOWN


@dataclass(slots=True)
class CiscoBlock:
    """保存一个 IOS XR 顶层配置块及其原始子行。

    ``header`` 是非缩进顶层命令，``lines`` 保留其内部文本，``active`` 控制最终
    是否渲染。采用轻量块模型可以保持未知命令和原始顺序，又能通过逻辑停用实现
    可恢复的删除，而不必完整理解 IOS XR 的所有层级语法。
    """
    header: str
    lines: list[str] = field(default_factory=list)
    active: bool = True

    @property
    def interface_name(self) -> str | None:
        """返回接口块的规范化接口名，非接口块返回 ``None``。

        IOS XR 会把 ``l2transport`` 写在接口头部，但它表示接口模式而非名称；先
        去掉该后缀再规范化，才能让同一二层接口与拓扑和外部引用正确匹配。
        """
        match = re.match(r"interface\s+(.+?)\s*$", self.header, re.IGNORECASE)
        if not match:
            return None
        # IOS XR 二层子接口会把 l2transport 写在 interface 头部，
        # 它是接口模式而不是接口名的一部分。
        value = re.sub(r"\s+l2transport\s*$", "", match.group(1), flags=re.IGNORECASE)
        return canonical_cisco_interface(value)

    @property
    def l2transport(self) -> bool:
        """判断接口头是否显式启用了 IOS XR ``l2transport`` 模式。

        接口克隆、改名或 UNI 映射会重建 header，因此必须单独保留这个模式标志，
        否则二层子接口可能在名称变更后意外变成普通三层接口。
        """
        return bool(re.match(r"interface\s+.+\s+l2transport\s*$", self.header, re.IGNORECASE))

class CiscoDocument:
    """提供可修改 IOS XR 块模型及应用层所需的统一厂商门面。

    类本身负责文本解析、渲染、外部规则和引用替换；接口迁移、group、清洗及模拟
    适配委托给独立厂商模块。这样应用层只依赖统一能力，不接触 ``CiscoBlock``
    细节，同时避免把不断增长的厂商操作全部堆进语法解析器。
    """
    vendor = "cisco_iosxr"

    def __init__(self, text: str):
        """记录输入换行信息，并把原始 IOS XR 文本解析成有序顶层块。

        构造时立即建立可修改表示，使后续处理器共享同一文档状态；保留块顺序是为了
        在删除或插入配置后仍尽量维持原配置的组织方式和确定性输出。
        """
        self.trailing_newline = text.endswith("\n")
        self.blocks = self._parse(text)

    @staticmethod
    def _parse(text: str) -> list[CiscoBlock]:
        """按顶层非缩进行、group 边界和 ``!`` 分隔符切分 IOS XR 配置。

        解析器有意只建立转换所需的块级结构，未知内部命令继续作为原文保存；同时
        特判 group 的 ``end-group`` 和块内缩进 ``!``，避免把合法层级内容错误切成
        新顶层块。循环按以下优先级处理每一行：

        当前块是 group？
        ├─ 是：遇到 end-group 就结束，否则所有行都加入 group
        └─ 否：
           ├─ 无缩进普通命令 → 创建新的顶层块
           ├─ 遇到 ! → 缩进则属于当前块，无缩进则结束当前块
           └─ 其他行 → 加入当前块；没有当前块则原样独立保留

        这种轻量状态机比不完整的全语法解析更能安全保留未识别配置，也让 group
        与普通块中相同文本的不同含义得到明确处理。
        """
        blocks: list[CiscoBlock] = []
        current: CiscoBlock | None = None
        group_block: CiscoBlock | None = None

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            is_indented = bool(line) and line[0].isspace()

            # group 的内部命令可能看起来像普通顶层 header，因此进入 group 后
            # 必须优先消费所有行，只允许 end-group 结束这个状态。
            if group_block is not None:
                if stripped.lower() == "end-group":
                    group_block = None
                    current = None
                    blocks.append(CiscoBlock(header="end-group"))
                else:
                    group_block.lines.append(line)
                continue

            # 普通状态下，无缩进命令开启一个新的顶层块。感叹号单独处理，
            # 因为它是否缩进决定了是顶层分隔符还是块内子模式结束符。
            if stripped and not is_indented and stripped != "!":
                current = CiscoBlock(header=line)
                blocks.append(current)
                if re.match(r"group\s+\S+", line, re.IGNORECASE):
                    group_block = current
                continue

            if stripped == "!":
                if is_indented and current is not None:
                    current.lines.append(line)
                else:
                    current = None
                    blocks.append(CiscoBlock(header="!"))
                continue

            if current is not None:
                current.lines.append(line)
            else:
                blocks.append(CiscoBlock(header=line))
        return blocks

    def _interface_blocks(self) -> list[CiscoBlock]:
        """返回仍有效且已识别接口名的 IOS XR 配置块。

        具体筛选委托接口模块，保证兼容旧内部入口的同时，让所有调用方复用同一套
        活动状态和接口识别规则，避免操作已逻辑删除的块。
        """
        return self._interfaces().interface_blocks(self)

    @staticmethod
    def _interfaces():
        """延迟返回 Cisco 接口操作模块。

        接口模块需要引用 ``CiscoDocument`` 和 ``CiscoBlock``；在方法调用时再导入
        可打破模块初始化阶段的循环依赖，同时保持文档类对外提供稳定门面。
        """
        from ..vendor.cisco import interfaces

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

    def _find_interface_block(self, name: str) -> CiscoBlock | None:
        """按规范化名称查找第一个有效 IOS XR 接口块。

        这是保留给旧内部调用的块级入口；统一委托接口模块可确保查找忽略已停用块，
        避免后续修改落到不会被渲染的旧定义上。
        """
        return self._interfaces().find_interface_block(self, name)

    @staticmethod
    def _parse_cisco_nodes(lines: Iterable[str], origin: str = "explicit") -> list[_CiscoNode]:
        """把块内文本解析成 group 展开使用的临时 Cisco 节点树。

        方法保留旧调用路径，但把真正的缩进解析交给 ``CiscoGroupExpander``，使 group
        的解析与合并规则集中维护，避免文档门面和展开器各自实现一套层级逻辑。
        """
        from ..vendor.cisco.groups import CiscoGroupExpander

        return CiscoGroupExpander.parse_nodes(lines, origin)

    @staticmethod
    def _render_cisco_nodes(nodes: list[_CiscoNode], depth: int = 1) -> list[str]:
        """按指定缩进深度渲染 group 临时节点树。

        该兼容入口与解析入口成对存在，并委托同一个展开器输出，保证继承配置写回时
        使用与 group 解析一致的块/叶子结构和缩进规则。
        """
        from ..vendor.cisco.groups import CiscoGroupExpander

        return CiscoGroupExpander.render_nodes(nodes, depth)

    def expand_groups(
        self,
        known_interfaces: Iterable[str],
        mode: str = "relevant",
        policy: WashingPolicy | None = None,
    ) -> GroupExpansionOutcome:
        """按模式和策略展开 IOS XR group，并返回事件、冲突及成败信息。

        group 继承必须在接口分类前物化，但其语义远比基础块解析复杂；委托专用展开器
        可以隔离冲突处理，并让调用方通过结果对象决定是否安全继续转换。
        """
        from ..vendor.cisco.groups import CiscoGroupExpander

        return CiscoGroupExpander(self).expand_groups(known_interfaces, mode, policy)

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

    def _merge_duplicate_interface(self, preferred: CiscoBlock) -> None:
        """把同名接口块的非重复配置行合并到首选块。

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
        from ..vendor.cisco.simulation import adapt_to_simulation

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
        from ..vendor.cisco.cleaning import clean_management_access

        return clean_management_access(self)

    def clean_optional_features(
        self,
        policy: WashingPolicy,
    ) -> CleanupOutcome:
        """按显式策略清理 IOS XR 的可选能力配置并返回统计。

        PKI、硬件、NAT、流量统计和协议认证会影响业务语义，只有策略开启时才应删除；
        交给厂商清洗模块可按 IOS XR 语法执行，而上层无需接触块结构。
        """
        from ..vendor.cisco.cleaning import clean_optional_features

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
        """向 IOS XR 文档添加统一实验账号。

        清除生产认证后必须建立已知的本地登录入口，防止输出设备无法接管；账号语法
        由厂商清洗模块生成，使凭据替换与旧认证删除使用同一实现边界。
        """
        from ..vendor.cisco.cleaning import add_lab_account

        add_lab_account(self)

    def apply_cleaning_rule(
        self,
        match: str,
        action: str,
        value: str | None,
    ) -> int:
        """在 IOS XR 顶层配置块上应用外部规则。

        方法按正则匹配活动块 header，并执行删除、替换或脱敏，返回命中块数。规则
        模块只依赖该厂商门面而不读取 ``blocks`` 内部表示，因此外部自定义清洗无需
        与解析器数据结构耦合，也不会误改未命中的块内文本。
        """
        pattern = re.compile(match, re.IGNORECASE)
        hits = 0
        for block in self.blocks:
            if not block.active or not pattern.search(block.header.strip()):
                continue
            hits += 1
            if action == "delete":
                block.active = False
            elif action == "replace":
                block.header = pattern.sub(value or "", block.header)
            elif action == "mask":
                block.header = pattern.sub("<masked>", block.header)
        return hits

    def replace_references(self, replacements: dict[str, list[str]]) -> None:
        """更新 IOS XR 配置中的旧接口引用，并展开一对多 M-LAG 目标。

        方法先规范化并去重替换表，再基于原文一次性生成文本变体；接口 header 由迁移
        阶段负责，这里只更新其内部行，而其他顶层块可按目标数克隆。一次性展开可避免
        新目标名再次命中旧源规则，保证协议、L2VPN 和策略引用与最终接口树一致。
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

        rebuilt: list[CiscoBlock] = []
        for block in self.blocks:
            if not block.active or block.interface_name:
                if block.active:
                    block.lines = [line for raw in block.lines for line in expand(raw)]
                rebuilt.append(block)
                continue
            header_variants = expand(block.header)
            for header in header_variants:
                clone = copy.deepcopy(block)
                clone.header = header
                clone.lines = [line for raw in clone.lines for line in expand(raw)]
                rebuilt.append(clone)
        self.blocks = rebuilt

    def render(self) -> str:
        """按文档顺序渲染所有活动 IOS XR 配置块。

        逻辑停用的块被跳过，未知块及其内部原文继续保留；稳定顺序和统一末尾换行
        便于设备加载、生成可读差异，也使相同输入得到确定性输出。
        """
        lines: list[str] = []
        for block in self.blocks:
            if not block.active:
                continue
            lines.append(block.header)
            lines.extend(block.lines)
        text = "\n".join(lines).rstrip() + "\n"
        return text
