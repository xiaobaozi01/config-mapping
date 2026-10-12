"""隔离实验环境使用的登录凭据。"""

LAB_USERNAME = "root"
LAB_PASSWORD = "admin123"

# Junos 保存 SHA-512 crypt 哈希；密码和固定 salt 均为 admin123。
JUNOS_LAB_PASSWORD_HASH = (
    "$6$admin123$qkQUo1OlslMMLHPyx8kLQnBJKIZaPQsFkb7XxVevTdwU3L40gq0yjvrD2XbU1uKkZN38GonwTiTZnZ1euaZYB."
)
