"""加载默认关闭的扩展配置清洗策略。"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..common.policies import WashingPolicy
from .models import ConversionContext


_OPTIONAL_KEYS = {
    "protocol_authentication",
    "pki",
    "hardware",
    "nat",
    "flow_statistics",
}


def load_washing_policy(path: Path | None) -> WashingPolicy:
    """读取 group 未知语义策略和清洗开关。"""
    if path is None:
        return WashingPolicy()
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    optional = payload.get("optional_washing", {})
    if not isinstance(optional, dict):
        raise ValueError("optional_washing 必须是键值映射")
    unknown = sorted(set(optional) - _OPTIONAL_KEYS)
    if unknown:
        raise ValueError(f"未知可选清洗开关: {', '.join(unknown)}")
    group_handling = payload.get("group_handling", {})
    if not isinstance(group_handling, dict):
        raise ValueError("group_handling 必须是键值映射")
    unknown_group_keys = sorted(set(group_handling) - {"unknown_identity"})
    if unknown_group_keys:
        raise ValueError(f"未知 group 处理配置: {', '.join(unknown_group_keys)}")
    unknown_identity = group_handling.get("unknown_identity", "warn")
    if unknown_identity not in {"preserve", "warn", "fail"}:
        raise ValueError(
            "group_handling.unknown_identity 必须是 preserve、warn 或 fail"
        )

    values: dict[str, bool | str] = {
        "group_unknown_identity": unknown_identity,
    }
    for key in _OPTIONAL_KEYS:
        value = optional.get(key, False)
        if not isinstance(value, bool):
            raise ValueError(f"清洗开关 {key} 必须是 true 或 false")
        values[key] = value
    return WashingPolicy(**values)
class OptionalFeatureWashingHandler:
    """按用户显式开启的策略清理非必需但可能影响模拟运行的配置能力。

    可选类别包括协议认证、PKI、硬件绑定、NAT 和流量统计；这些配置可能依赖真实
    设备能力或外部系统，但删除也可能改变业务语义，因此不能像管理账号清洗一样
    默认执行。独立处理器让风险较高的清理保持显式可控，并为每台设备留下分类统计。
    """

    def process(self, context: ConversionContext) -> None:
        """执行已启用的可选清洗项，并记录开关及按类别删除数量。

        方法把完整策略交给厂商实现，由其按 IOS XR 或 Junos 语法删除对应节点；
        即使没有匹配内容也记录 ``optional-washing`` 事件，便于报告证明哪些高影响
        开关被实际请求过。它与强制认证替换分离，是为了避免用户仅想重建实验账号时
        意外删除 NAT、PKI 或硬件相关业务配置。
        """
        policy = context.washing_policy
        enabled = [
            name
            for name in (
                "protocol_authentication",
                "pki",
                "hardware",
                "nat",
                "flow_statistics",
            )
            if getattr(policy, name)
        ]
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            cleanup = device.document.clean_optional_features(policy)
            context.add_event(
                "optional-washing",
                f"设备 {device_name} 已执行可选能力清洗",
                device=device_name,
                enabled=enabled,
                removed_sections=cleanup.total,
                removed_by_type=cleanup.removed,
            )

class AuthWashingHandler:
    """移除生产管理面认证信息，并为输出配置建立统一实验账号。

    原配置可能包含本地用户、AAA、TACACS/RADIUS、SNMP 或远程访问凭据，直接带入
    实验环境既有泄密风险，也可能因外部认证服务器不可达而无法登录。该处理器先由
    厂商实现清除旧管理访问配置，再添加已知实验账号，保证输出设备可安全接管。
    """

    def process(self, context: ConversionContext) -> None:
        """逐设备清理旧管理认证、添加实验账号并记录删除统计。

        清理和添加账号放在同一阶段，可避免生成既保留生产凭据又新增实验凭据的配置，
        也避免只删除认证后留下无法登录的设备。结构化事件保存总数和分类数量，用于
        审计清洗范围，但不记录任何秘密内容。
        """
        for device_name in sorted(context.devices):
            device = context.devices[device_name]
            cleanup = device.document.clean_management_access()
            device.document.add_lab_account()
            context.add_event(
                "authentication",
                f"设备 {device_name} 已替换实验认证配置",
                device=device_name,
                removed_sections=cleanup.total,
                removed_by_type=cleanup.removed,
            )
