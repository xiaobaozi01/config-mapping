"""跨模块共享的固定值、工作表别名和列名别名。"""

# 实验账号只用于隔离的 GNS3 环境，不应复用于生产设备。
LAB_USERNAME = "labadmin"
LAB_PASSWORD = "Gns3Lab@2026"

# Junos 不能直接保存明文密码，因此预生成与上方密码对应的 SHA-512 crypt。
JUNOS_LAB_PASSWORD_HASH = (
    "$6$Gns3Lab2026$9Er/3JwyekNoiNfTNyetLUTFy7QCUOSOj8yWjBCzKYHPeWVr5Dqkya8eGkkMNiW2u106aCysiC62jiv059ulr1"
)

# 接受常见中英文名称，降低不同拓扑模板之间的耦合。
DEVICE_SHEET_ALIASES = ("设备列表", "devices", "device list", "device_list")
LINK_SHEET_ALIASES = ("链接表", "链路表", "links", "link list", "link_list")

DEVICE_COLUMN_ALIASES = {
    "name": ("设备名称", "设备名", "名称", "device", "device_name", "name", "hostname"),
    "vendor": ("厂商", "厂家", "vendor", "manufacturer"),
    "config_file": ("配置文件", "配置文件名", "config", "config_file", "configuration"),
}

LINK_COLUMN_ALIASES = {
    "a_device": ("a端设备", "源设备", "本端设备", "a_device", "device_a", "source_device"),
    "a_interface": ("a端接口", "源接口", "本端接口", "a_interface", "interface_a", "source_interface"),
    "z_device": ("z端设备", "对端设备", "目的设备", "z_device", "device_z", "target_device"),
    "z_interface": ("z端接口", "对端接口", "目的接口", "z_interface", "interface_z", "target_interface"),
}
