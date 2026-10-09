"""定义、加载并按固定流水线执行点运行 IOS XR/Junos 清洗规则。

简单的节点匹配由随镜像发布的 YAML 描述，跨节点或复合变更由代码规则实现；两类
规则共享同一协议和审计结果，避免 Pipeline 理解厂商语法或维护多套清洗入口。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Protocol

import yaml

from ..cisco.document import CiscoDocument
from ..common.contracts import VendorConfiguration
from .models import ConversionContext, Vendor


class CleaningPoint(StrEnum):
    """限制规则只能进入两个稳定的流水线执行点。

    固定执行点既允许 banner 在分析前移除、认证在引用改写后替换，也防止规则依赖
    任意 Stage 下标而使流水线顺序难以审计。
    """

    # 解析已完成，但拓扑、Group 和接口业务分析尚未开始。
    PRE_ANALYSIS = "pre_analysis"
    # 接口迁移及引用改写已完成；依次执行 YAML 清洗和认证替换。
    POST_REWRITE = "post_rewrite"


@dataclass(slots=True)
class CleaningResult:
    """承载一条规则产生的结构化变更统计。

    Handler 只依赖这个统一结果生成报告，因此 YAML 规则和代码规则不需要各自维护
    事件格式；分类字典只记录类别和数量，避免把被删除的秘密值写入报告。
    """

    # 本规则完成的总变更数，用于判断是否需要记录命中事件。
    changed: int = 0
    # 按删除对象类型统计数量，例如 username、snmp 或 banner。
    removed_by_type: dict[str, int] = field(default_factory=dict)
    # 按新增对象类型统计数量，例如统一实验账号。
    added_by_type: dict[str, int] = field(default_factory=dict)


class CleaningRuleError(ValueError):
    """表示输入配置命中了规则，但缺少安全完成变更所需的结构。

    单独的异常类型让 Handler 把未闭合 banner 等业务输入问题写入转换报告，同时让
    TypeError 等编程错误继续向外暴露，避免把实现缺陷误报成普通配置错误。
    """


class ExecutableCleaningRule(Protocol):
    """声明式规则和代码规则共同遵循的最小执行协议。

    Handler 通过结构化类型只关心规则元数据、启用状态和 ``apply``，因此新增复杂
    规则无需修改调度流程，也不要求厂商文档继承某个规则基类。
    """

    @property
    def rule_id(self) -> str:
        """返回用于报告和定位规则的全局唯一标识。"""
        raise NotImplementedError

    @property
    def vendor(self) -> str:
        """返回规则适用的厂商配置类型。"""
        raise NotImplementedError

    @property
    def category(self) -> str:
        """返回规则变更对象的审计分类。"""
        raise NotImplementedError

    @property
    def action(self) -> str:
        """返回规则执行的动作类型。"""
        raise NotImplementedError

    @property
    def reason(self) -> str:
        """返回执行该规则的业务原因。"""
        raise NotImplementedError

    @property
    def point(self) -> CleaningPoint:
        """返回规则所属的固定流水线执行点。"""
        raise NotImplementedError

    @property
    def enabled(self) -> bool:
        """返回规则是否参与执行，统一 YAML 开关和内置规则常量。"""
        raise NotImplementedError

    def apply(self, document: VendorConfiguration) -> CleaningResult:
        """修改一个厂商文档并返回不含敏感值的统计结果。"""
        ...


@dataclass(slots=True, frozen=True)
class ConfiguredCleaningRule:
    """一条从 YAML 加载的声明式清洗规则。

    清洗动作由数据描述而非散落在厂商代码里，规则因此可随镜像打包、在不改代码的
    情况下增删；``frozen`` 使加载后的规则只读，供多设备共享而不被意外修改。
    """

    rule_id: str
    vendor: str
    enable: bool
    category: str
    path: tuple[str, ...]
    match: str
    action: str
    reason: str
    value: str | None = None

    @property
    def point(self) -> CleaningPoint:
        """把 YAML 规则固定到引用改写后的安全执行点。

        YAML 不开放阶段配置，可防止简单删除规则提前改变接口分类、NNI 或 UNI 决策。
        """
        return CleaningPoint.POST_REWRITE

    @property
    def enabled(self) -> bool:
        """把 YAML 的 ``enable`` 字段适配为统一规则协议。"""
        return self.enable

    def apply(self, document: VendorConfiguration) -> CleaningResult:
        """委托厂商文档按路径匹配并执行声明式动作。

        匹配和 AST 修改留在厂商实现中，规则对象只负责携带数据并把命中数转换为统一
        结果，避免流程层理解 Cisco 与 Junos 的树结构差异。
        """
        hits = document.apply_cleaning_rule(
            self.path,
            self.match,
            self.action,
            self.value,
        )
        return CleaningResult(changed=hits)


@dataclass(slots=True, frozen=True)
class CiscoBannerCleaningRule:
    """在业务分析前停用完整的 exec/login banner 节点区间。

    当前 Cisco AST 会把未缩进的 banner 正文解析成相邻顶层节点，单个 YAML 正则只能
    删除起始行；代码规则按分隔符停用完整区间，避免正文中的 ``interface`` 等文本
    干扰后续配置分析。
    """

    rule_id: str = "cisco.remove.banner"
    vendor: str = Vendor.CISCO_IOSXR.value
    category: str = "banner"
    action: str = "delete"
    reason: str = "删除生产环境登录提示和设备信息"
    point: CleaningPoint = CleaningPoint.PRE_ANALYSIS
    _START_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^banner\s+(?:exec|login)\s+(\S+)\s*$",
        re.IGNORECASE,
    )

    @property
    def enabled(self) -> bool:
        """始终启用内置 banner 清洗，避免生产提示和站点信息进入实验配置。"""
        return True

    def apply(self, document: VendorConfiguration) -> CleaningResult:
        """扫描顶层节点并停用每个 banner 起止分隔符之间的完整区间。

        缺少结束分隔符时拒绝继续，因为猜测区间终点可能误删 banner 后的真实配置；
        所有删除均通过 ``active=False`` 完成，保持 AST 的逻辑删除约定。
        """
        if not isinstance(document, CiscoDocument):
            raise TypeError("Cisco banner 规则只能应用于 CiscoDocument")

        nodes = document.root.children
        removed = 0
        index = 0
        while index < len(nodes):
            node = nodes[index]
            if not node.active:
                index += 1
                continue
            match = self._START_PATTERN.match(node.header.strip())
            if not match:
                index += 1
                continue

            delimiter = match.group(1)
            end = next(
                (
                    candidate_index
                    for candidate_index in range(index + 1, len(nodes))
                    if nodes[candidate_index].active
                    and nodes[candidate_index].header.strip() == delimiter
                ),
                None,
            )
            if end is None:
                raise CleaningRuleError(
                    f"{node.header} 缺少结束分隔符 {delimiter}"
                )

            for candidate in nodes[index : end + 1]:
                candidate.active = False
            removed += 1
            index = end + 1

        return CleaningResult(
            changed=removed,
            removed_by_type={"banner": removed} if removed else {},
        )


@dataclass(slots=True, frozen=True)
class AuthenticationCleaningRule:
    """原子地删除生产认证配置并创建厂商对应的实验账号。

    认证替换包含删除和新增两种动作，无法用单条节点正则安全表达；把两步保留在同一
    代码规则中，可避免输出只删除旧账号却没有建立实验登录入口的中间状态。
    """

    rule_id: str
    vendor: str
    category: str = "authentication"
    action: str = "transform"
    reason: str = "替换生产认证并创建统一实验账号"
    point: CleaningPoint = CleaningPoint.POST_REWRITE

    @property
    def enabled(self) -> bool:
        """始终执行认证替换，这是实验配置可接管性的固定要求。"""
        return True

    def apply(self, document: VendorConfiguration) -> CleaningResult:
        """调用厂商能力清除管理认证，并立即写入统一实验账号。

        返回值只包含删除类别和新增账号数量，用于审计范围而不泄露原密码或密钥。
        """
        cleanup = document.clean_management_access()
        document.add_lab_account()
        return CleaningResult(
            changed=cleanup.total + 1,
            removed_by_type=dict(cleanup.removed),
            added_by_type={"lab-account": 1},
        )


class CleaningRulesHandler:
    """在一个固定执行点统一选择、执行并审计内置清洗规则。

    Handler 自己拥有固定规则注册表，Pipeline 只传执行点而不注入规则集合；这样规则
    来源保持唯一，每个执行点持有独立列表，也避免调用方改变生产清洗行为。
    """

    # YAML 路径与期望厂商成对声明，用于加载时阻止规则放错镜像目录。
    _PACKAGE_ROOT = Path(__file__).parent.parent
    _RULE_SOURCES = (
        (_PACKAGE_ROOT / "cisco" / "rules" / "xrv9000" / "cleaning.yaml", Vendor.CISCO_IOSXR),
        (_PACKAGE_ROOT / "juniper" / "rules" / "vmx" / "cleaning.yaml", Vendor.JUNIPER_JUNOS),
    )
    # YAML 使用厂商内局部 id，加载时统一生成稳定、全局唯一的报告 id。
    _VENDOR_PREFIX = {
        Vendor.CISCO_IOSXR.value: "cisco",
        Vendor.JUNIPER_JUNOS.value: "juniper",
    }
    # 声明式规则仅开放厂商文档已经实现且可统一审计的动作。
    _ACTIONS = {"delete", "replace", "mask", "warn"}
    _LOCAL_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)*$")

    def __init__(self, point: CleaningPoint):
        """绑定一个固定执行点，并创建该处理器使用的默认规则列表。

        构造参数不接受规则集合，因为规则随程序发布且不会由一次转换动态替换；每个
        Handler 持有自己的列表，避免不同执行点之间共享可变容器。
        """
        self._rules = self._default_rules()
        self.point = point

    def process(self, context: ConversionContext) -> None:
        """按设备执行属于当前点和厂商的已启用规则。

        本方法只负责编排筛选、执行和失败短路；成功事件的字段构造交给
        ``_record_result``，使主流程保持清晰。规则报告安全输入错误后立即返回，Pipeline
        会看到 ``context.errors`` 并停止后续阶段。
        """
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            for rule in self._rules:
                if (
                    not rule.enabled
                    or rule.point != self.point
                    or rule.vendor != device.device.vendor.value
                ):
                    continue
                try:
                    result = rule.apply(device.document)
                except CleaningRuleError as exc:
                    message = f"设备 {device_name} 执行清洗规则 {rule.rule_id} 失败: {exc}"
                    context.errors.append(message)
                    context.add_event(
                        "cleaning-rule-error",
                        message,
                        device=device_name,
                        rule_id=rule.rule_id,
                        category=rule.category,
                        point=self.point.value,
                    )
                    return
                if not result.changed:
                    continue
                self._record_result(context, device_name, rule, result)

    def _record_result(
        self,
        context: ConversionContext,
        device_name: str,
        rule: ExecutableCleaningRule,
        result: CleaningResult,
    ) -> None:
        """把一条成功规则的统一结果写成结构化清洗事件。

        集中生成事件可以保证 YAML 和代码规则使用相同字段，并只在分类统计非空时写入
        删除或新增明细，减少报告噪音且避免各规则自行记录敏感内容。
        """
        details: dict[str, object] = {
            "device": device_name,
            "rule_id": rule.rule_id,
            "category": rule.category,
            "action": rule.action,
            "point": self.point.value,
            "hits": result.changed,
            "reason": rule.reason,
        }
        if result.removed_by_type:
            details["removed_by_type"] = result.removed_by_type
        if result.added_by_type:
            details["added_by_type"] = result.added_by_type
        context.add_event(
            "cleaning-rule",
            f"设备 {device_name} 命中规则 {rule.rule_id}",
            **details,
        )

    @staticmethod
    def _required_string(raw: dict[str, object], field: str, rule_id: str) -> str:
        """读取一个必填非空字符串，并在加载阶段报告具体规则和字段。

        统一这个基础校验可保持错误格式一致，避免主解析方法重复类型判断。
        """
        value = raw.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"清洗规则 {rule_id} 的 {field} 必须是非空字符串")
        return value

    @classmethod
    def _parse_rule(
        cls,
        raw: object,
        seen: set[str],
    ) -> ConfiguredCleaningRule:
        """把一条原始 YAML 映射校验并构造成只读声明式规则。

        单条规则的字段、id、动作和正则都在这里一次性验证，转换阶段因此只处理可信的
        强类型对象；``seen`` 在构造前查重，保证跨厂商文件的最终 id 全局唯一。
        """
        if not isinstance(raw, dict):
            raise ValueError("清洗规则必须是键值映射")
        local_id = cls._required_string(raw, "id", "<unknown>")
        vendor = cls._required_string(raw, "vendor", local_id)
        if vendor not in cls._VENDOR_PREFIX:
            raise ValueError(f"清洗规则 {local_id} 使用不支持的厂商: {vendor}")
        if not cls._LOCAL_ID_PATTERN.fullmatch(local_id):
            raise ValueError(
                f"清洗规则 {local_id} 的 id 必须由小写字母、数字和点号组成"
            )
        rule_id = f"{cls._VENDOR_PREFIX[vendor]}.{local_id}"
        if rule_id in seen:
            raise ValueError(f"重复的清洗规则 id: {rule_id}")
        seen.add(rule_id)

        enable = raw.get("enable")
        if not isinstance(enable, bool):
            raise ValueError(f"清洗规则 {rule_id} 的 enable 必须是布尔值")
        category = cls._required_string(raw, "category", rule_id)
        match = cls._required_string(raw, "match", rule_id)
        action = cls._required_string(raw, "action", rule_id)
        if action not in cls._ACTIONS:
            raise ValueError(f"清洗规则 {rule_id} 使用不支持的动作: {action}")

        path_value = raw.get("path")
        if not isinstance(path_value, list) or not all(
            isinstance(component, str) and component for component in path_value
        ):
            raise ValueError(f"清洗规则 {rule_id} 的 path 必须是字符串列表")
        path = tuple(path_value)
        try:
            re.compile(match, re.IGNORECASE)
            for component in path:
                if component not in {"*", "**"}:
                    re.compile(component, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"清洗规则 {rule_id} 包含无效正则: {exc}") from exc

        value = raw.get("value")
        if value is not None and not isinstance(value, str):
            raise ValueError(f"清洗规则 {rule_id} 的 value 必须是字符串")
        if action == "replace" and value is None:
            raise ValueError(f"清洗规则 {rule_id} 的 replace 动作必须提供 value")
        reason = cls._required_string(raw, "reason", rule_id)

        allowed = {
            "id", "vendor", "enable", "category", "path", "match",
            "action", "reason", "value",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"清洗规则 {rule_id} 包含未知字段: {', '.join(unknown)}")
        return ConfiguredCleaningRule(
            rule_id=rule_id,
            vendor=vendor,
            enable=enable,
            category=category,
            path=path,
            match=match,
            action=action,
            reason=reason,
            value=value,
        )

    @classmethod
    def _configured_rules(cls) -> list[ConfiguredCleaningRule]:
        """加载两份内置 YAML，并校验文件结构、厂商归属和跨文件 id。

        文件级校验与单条字段解析分开，使本方法只负责规则包边界，同时确保 Cisco 与
        Juniper 规则不会误放到对方的镜像目录。
        """
        rules: list[ConfiguredCleaningRule] = []
        seen: set[str] = set()
        for source, vendor in cls._RULE_SOURCES:
            expected_vendor = vendor.value
            with source.open("r", encoding="utf-8") as handle:
                payload = yaml.safe_load(handle) or {}
            if not isinstance(payload, dict) or set(payload) != {"rules"}:
                raise ValueError(f"{source}: 顶层必须且只能包含 rules")
            raw_rules = payload["rules"]
            if not isinstance(raw_rules, list):
                raise ValueError(f"{source}: rules 必须是列表")
            for raw in raw_rules:
                rule = cls._parse_rule(raw, seen)
                if rule.vendor != expected_vendor:
                    raise ValueError(
                        f"{source}: 默认厂商规则只能包含 vendor: {expected_vendor}"
                    )
                rules.append(rule)
        return rules

    @classmethod
    def _default_rules(cls) -> list[ExecutableCleaningRule]:
        """按固定顺序合并 YAML 和代码规则，返回独立的规则列表。

        显式的协议列表让静态检查器逐项验证每种规则实现，也让各执行点拥有独立容器；
        注册顺序仍然是唯一、可审计的执行顺序。
        """
        rules: list[ExecutableCleaningRule] = []
        rules.extend(cls._configured_rules())
        rules.append(CiscoBannerCleaningRule())
        rules.append(
            AuthenticationCleaningRule(
                rule_id="cisco.replace.authentication",
                vendor=Vendor.CISCO_IOSXR.value,
            )
        )
        rules.append(
            AuthenticationCleaningRule(
                rule_id="juniper.replace.authentication",
                vendor=Vendor.JUNIPER_JUNOS.value,
            )
        )
        return rules
