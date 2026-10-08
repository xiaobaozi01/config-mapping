"""替换实验环境管理认证。"""

from __future__ import annotations

from .models import ConversionContext


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
