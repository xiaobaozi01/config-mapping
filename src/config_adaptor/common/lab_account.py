"""隔离实验环境使用的统一账号默认值。"""

LAB_USERNAME = "labadmin"
LAB_PASSWORD = "Gns3Lab@2026"

# Junos 保存哈希而非明文；该值与上面的实验密码对应。
JUNOS_LAB_PASSWORD_HASH = (
    "$6$Gns3Lab2026$9Er/3JwyekNoiNfTNyetLUTFy7QCUOSOj8yWjBCzKYHPeWVr5Dqkya8eGkkMNiW2u106aCysiC62jiv059ulr1"
)
