"""The temporary FTP node that serves one cfg file to the target device."""

from __future__ import annotations

import io
import re
import shlex
import zipfile

from .consoles import ConsoleOpener
from .models import ImportSettings, ImportStageError
from .ports import Console, GNS3Client


SHELL_PROMPT = r"[#$]\s*$"
# The echoed command shows "$?", so only the real status line carries digits.
_EXIT_STATUS = re.compile(r"__rc=(\d+)")
# GNS3 packs this file of the node directory into the QEMU config disk on start.
CONFIG_ZIP = "config.zip"
CONFIG_MOUNT = "/mnt/gns3-config"


class FtpServer:
    """Create the QEMU FTP node, deliver the cfg via its config disk, and delete the node.

    GNS3 cannot write into a running QEMU guest. Instead the cfg goes into
    ``config.zip`` of the node directory before the first start; with the
    template's ``create_config_disk`` enabled, GNS3 copies it onto a FAT disk
    attached to the guest, which is mounted and copied into the FTP directory.
    """

    def __init__(self, gns3: GNS3Client, consoles: ConsoleOpener, settings: ImportSettings):
        self._gns3 = gns3
        self._consoles = consoles
        self._settings = settings
        self.node_id: str | None = None

    def prepare(self, compute_id: str, content: bytes) -> None:
        """Start the node on ``compute_id`` with the cfg attached, address it and publish the cfg."""
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
        if self.node_id is not None:
            self._gns3.delete_node(self._settings.project_id, self.node_id)
            self.node_id = None

    def _publish(self, console: Console, filename: str) -> None:
        """Copy the cfg off the config disk, readable only by the FTP account."""
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
        try:
            output = console.command(f"{command}; echo __rc=$?", SHELL_PROMPT, self._settings.command_timeout)
        except Exception as exc:
            raise ImportStageError(stage, f"FTP 命令未完成: {type(exc).__name__}") from exc
        status = _EXIT_STATUS.search(output)
        if status is None or status.group(1) != "0":
            raise ImportStageError(stage, "FTP 节点未确认命令成功")


def _zip(filename: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, content)
    return buffer.getvalue()
