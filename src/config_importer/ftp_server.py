"""向目标设备提供单个 cfg 文件的临时 FTP 节点。"""

from __future__ import annotations

import io
import re
import shlex
import zipfile

from .consoles import ConsoleOpener
from .models import ImportSettings, ImportStageError
from .ports import Console, GNS3Client


# FTP 节点是普通 Linux 来宾，登录后得到的是 shell 提示符。
SHELL_PROMPT = r"[#$]\s*$"
# 命令回显里也含 "$?"，只有真实的状态行带数字，所以用带前缀的标记匹配。
_EXIT_STATUS = re.compile(r"__rc=(\d+)")
# GNS3 在节点启动时把节点目录中的这个文件写入 QEMU 配置盘。
CONFIG_ZIP = "config.zip"
# 配置盘在来宾内的挂载点。
CONFIG_MOUNT = "/mnt/gns3-config"


class FtpServer:
    """创建 QEMU FTP 节点、经配置盘送入 cfg，并在最后删除该节点。

    GNS3 无法向运行中的 QEMU 来宾写文件，因此 cfg 在节点首次启动前就写入节点目录的
    ``config.zip``；模板开启 ``create_config_disk`` 时，GNS3 会把它写到挂在来宾上的
    FAT 配置盘，再由导入器登录来宾挂载并复制到 FTP 目录。
    """

    def __init__(self, gns3: GNS3Client, consoles: ConsoleOpener, settings: ImportSettings):
        """保存 GNS3 客户端、Console 打开器和导入设置；节点在 ``prepare`` 时才创建。"""
        self._gns3 = gns3
        self._consoles = consoles
        self._settings = settings
        self.node_id: str | None = None

    def prepare(self, compute_id: str, content: bytes) -> None:
        """在 ``compute_id`` 上带着 cfg 启动节点，配好地址并发布配置文件。

        顺序不能调换：配置盘只在节点启动时生成一次，先写 ``config.zip`` 才能让 cfg
        进入来宾；随后才登录来宾配地址、把文件放进 FTP 目录。
        """
        s = self._settings
        transfer = s.transfer
        self.node_id = self._gns3.create_node_from_template(s.project_id, s.ftp_template_id, compute_id).node_id
        self._gns3.write_node_file(s.project_id, self.node_id, CONFIG_ZIP, _zip(transfer.filename, content))
        self._gns3.start_node(s.project_id, self.node_id)
        node = self._gns3.get_node(s.project_id, self.node_id)
        if node.status != "started":
            raise ImportStageError("ftp-start", "FTP 节点未启动")
        console = self._consoles.open(node, s.ftp_console_username, s.ftp_console_password,
                                      SHELL_PROMPT, "ftp-login")
        try:
            self._shell(console, f"ifconfig eth0 {transfer.server_ip} netmask {transfer.netmask}",
                        "ftp-address")
            self._publish(console, transfer.filename)
        finally:
            console.close()

    def delete(self) -> None:
        """删除本次创建的 FTP 节点。

        清理阶段可能在没有创建过节点的情况下调用（例如前置校验就失败），因此这里
        必须幂等；删除成功或从未创建都把 ``node_id`` 清空，导入结果才能反映真实残留。
        """
        if self.node_id is not None:
            self._gns3.delete_node(self._settings.project_id, self.node_id)
            self.node_id = None

    def _publish(self, console: Console, filename: str) -> None:
        """把 cfg 从配置盘复制到 FTP 目录，并设为只有 FTP 账号可读。

        配置盘是只读 FAT，而设备是通过 FTP 账号取文件的，所以需要复制一份并改属主、
        收紧权限；挂载期间用 ``finally`` 卸载，即使复制失败也不留下挂载点。
        """
        s = self._settings
        source = shlex.quote(f"{CONFIG_MOUNT}/{filename}")
        target = shlex.quote(f"{s.ftp_directory.rstrip('/')}/{filename}")
        mount = shlex.quote(CONFIG_MOUNT)
        self._shell(console, f"mkdir -p {mount}", "ftp-config-disk")
        self._shell(console, f"mount -t vfat -o ro {shlex.quote(s.ftp_config_disk)} {mount}",
                    "ftp-config-disk")
        try:
            self._shell(console, f"cp {source} {target}", "ftp-copy")
        finally:
            self._shell(console, f"umount {mount}", "ftp-config-disk")
        self._shell(console, f"chown {s.ftp_username} {target}", "ftp-owner")
        self._shell(console, f"chmod 600 {target}", "ftp-permission")

    def _shell(self, console: Console, command: str, stage: str) -> None:
        """执行一条 shell 命令，并用退出码判断成败。

        来宾的 shell 回显不像设备 CLI 那样有可靠的提示符语义，所以在命令后追加
        ``echo __rc=$?``，以退出码作为唯一判据；失败时抛出该阶段的错误，方便定位是
        挂载、复制还是改权限出了问题。
        """
        try:
            output = console.command(f"{command}; echo __rc=$?", SHELL_PROMPT, self._settings.command_timeout)
        except Exception as exc:
            raise ImportStageError(stage, f"FTP 命令未完成: {type(exc).__name__}") from exc
        status = _EXIT_STATUS.search(output)
        if status is None or status.group(1) != "0":
            raise ImportStageError(stage, "FTP 节点未确认命令成功")


def _zip(filename: str, content: bytes) -> bytes:
    """把 cfg 打包成 GNS3 配置盘所需的单文件 zip。

    GNS3 只接受 zip 形式的 ``config.zip`` 并把它展开到配置盘上，因此文件名要在包内
    就写成目标文件名，设备侧才能按预期路径取到。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, content)
    return buffer.getvalue()
