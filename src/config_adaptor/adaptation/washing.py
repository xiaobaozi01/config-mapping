"""加载 Group 策略并替换实验环境管理认证。"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..common.policies import WashingPolicy
from .models import ConversionContext


def load_washing_policy(path: Path | None) -> WashingPolicy:
    """读取 Group 未知语义处理策略。"""
    if path is None:
        return WashingPolicy()
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError("washing policy 必须是键值映射")
    unknown = sorted(set(payload) - {"group_handling"})
    if unknown:
        raise ValueError(f"未知 washing policy 配置: {', '.join(unknown)}")
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

    return WashingPolicy(group_unknown_identity=unknown_identity)


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
