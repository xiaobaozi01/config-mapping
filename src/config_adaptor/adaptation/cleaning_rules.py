"""统一加载和执行 IOS XR/Junos 配置清洗规则。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import ConversionContext, Vendor


# 指向 config_adaptor 包根目录（本文件位于其下两层的 adaptation/ 内）。
_PACKAGE_ROOT = Path(__file__).parent.parent

# 默认规则来源：路径与所属厂商配对存放，避免靠元组下标隐式对应。
_RULE_SOURCES = (
    (_PACKAGE_ROOT / "cisco" / "rules" / "xrv9000" / "cleaning.yaml", Vendor.CISCO_IOSXR),
    (_PACKAGE_ROOT / "juniper" / "rules" / "vmx" / "cleaning.yaml", Vendor.JUNIPER_JUNOS),
)

# 厂商值 -> 规则 id 前缀；键集合同时充当"受支持厂商"的校验来源。
_VENDOR_PREFIX = {
    Vendor.CISCO_IOSXR.value: "cisco",
    Vendor.JUNIPER_JUNOS.value: "juniper",
}

_ACTIONS = {"delete", "replace", "mask", "warn"}
_LOCAL_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)*$")


@dataclass(slots=True, frozen=True)
class CleaningRule:
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


def _required_string(raw: dict[str, object], field: str, rule_id: str) -> str:
    """读取并校验一个必填字符串字段，缺失或为空时立即报错。

    规则文件由人手工维护，字段类型错误要在加载阶段尽早暴露，避免把坏规则带进
    转换期才失败。
    """
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"清洗规则 {rule_id} 的 {field} 必须是非空字符串")
    return value


def _parse_rule(raw: object, seen: set[str]) -> CleaningRule:
    """把一条 YAML 规则字典解析并校验成 ``CleaningRule``。

    除字段类型和必填项外，还校验厂商、id 格式、动作枚举、正则合法性、``replace``
    必须带 ``value`` 以及未知字段；在加载阶段集中拒绝无效规则，可让转换期只执行
    可靠输入，而不是运行到一半才失败。
    """
    if not isinstance(raw, dict):
        raise ValueError("清洗规则必须是键值映射")
    local_id = _required_string(raw, "id", "<unknown>")
    vendor = _required_string(raw, "vendor", local_id)
    if vendor not in _VENDOR_PREFIX:
        raise ValueError(f"清洗规则 {local_id} 使用不支持的厂商: {vendor}")
    if not _LOCAL_ID_PATTERN.fullmatch(local_id):
        raise ValueError(f"清洗规则 {local_id} 的 id 必须由小写字母、数字和点号组成")
    rule_id = f"{_VENDOR_PREFIX[vendor]}.{local_id}"
    if rule_id in seen:
        raise ValueError(f"重复的清洗规则 id: {rule_id}")
    seen.add(rule_id)

    enable = raw.get("enable")
    if not isinstance(enable, bool):
        raise ValueError(f"清洗规则 {rule_id} 的 enable 必须是布尔值")
    category = _required_string(raw, "category", rule_id)
    match = _required_string(raw, "match", rule_id)
    action = _required_string(raw, "action", rule_id)
    if action not in _ACTIONS:
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
    reason = _required_string(raw, "reason", rule_id)

    allowed = {
        "id", "vendor", "enable", "category", "path", "match",
        "action", "reason", "value",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"清洗规则 {rule_id} 包含未知字段: {', '.join(unknown)}")
    return CleaningRule(
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


def load_rules() -> tuple[CleaningRule, ...]:
    """读取并完整校验随程序发布的两份厂商清洗规则。

    默认规则按厂商分文件发布；逐文件校验 vendor 一致性、跨文件查重 id，保证最终
    规则集完整、唯一且与目标镜像对应，供清洗处理器直接复用。
    """
    rules: list[CleaningRule] = []
    seen: set[str] = set()
    for source, vendor in _RULE_SOURCES:
        expected_vendor = vendor.value
        with source.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        if not isinstance(payload, dict) or set(payload) != {"rules"}:
            raise ValueError(f"{source}: 顶层必须且只能包含 rules")
        raw_rules = payload["rules"]
        if not isinstance(raw_rules, list):
            raise ValueError(f"{source}: rules 必须是列表")
        for raw in raw_rules:
            rule = _parse_rule(raw, seen)
            if rule.vendor != expected_vendor:
                raise ValueError(
                    f"{source}: 默认厂商规则只能包含 vendor: {expected_vendor}"
                )
            rules.append(rule)
    return tuple(rules)


class CleaningRulesHandler:
    """在接口及引用迁移完成后统一执行声明式配置清洗规则。

    清洗放在迁移与引用改写之后，可避免误删迁移前仍在引用的配置；独立处理器让
    声明式规则与厂商内置的必删清洗解耦，并统一输出命中统计供报告审计。
    """

    def __init__(self, rules: tuple[CleaningRule, ...]):
        """创建持有已校验规则元组的处理器实例。

        规则在传入前已由 ``load_rules`` 校验，这里只保存引用，保持构造无副作用。
        """
        self._rules = rules

    def process(self, context: ConversionContext) -> None:
        """按设备执行所有已启用规则，并记录每条命中的分类和数量。

        每条规则先按 ``enable`` 和厂商过滤，避免跨厂商误用或把未启用的规则套到
        设备上；命中才记录事件，未命中不产生噪音，命中的分类与数量供报告审计实际
        清洗范围。
        """
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            for rule in self._rules:
                if not rule.enable or rule.vendor != device.device.vendor.value:
                    continue
                hits = device.document.apply_cleaning_rule(
                    rule.path,
                    rule.match,
                    rule.action,
                    rule.value,
                )
                if not hits:
                    continue
                context.add_event(
                    "cleaning-rule",
                    f"设备 {device_name} 命中规则 {rule.rule_id}",
                    device=device_name,
                    rule_id=rule.rule_id,
                    category=rule.category,
                    action=rule.action,
                    hits=hits,
                    reason=rule.reason,
                )
