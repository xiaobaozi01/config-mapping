"""Inputs and outcomes of a single configuration import."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class Port:
    adapter_number: int
    port_number: int
    interface_name: str


@dataclass(frozen=True, slots=True)
class Node:
    node_id: str
    status: str
    compute_id: str
    console_host: str
    console_port: int
    image: str = ""


@dataclass(frozen=True, slots=True)
class FtpTransfer:
    """The temporary point-to-point FTP path as seen by the target device."""

    server_ip: str
    device_ip: str
    prefix_length: int
    username: str
    password: str
    filename: str

    @property
    def netmask(self) -> str:
        return str(ipaddress.ip_network(f"{self.server_ip}/{self.prefix_length}", strict=False).netmask)

    @property
    def url(self) -> str:
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        return f"ftp://{user}:{password}@{self.server_ip}/{self.filename}"


@dataclass(frozen=True, slots=True)
class ImportSettings:
    project_id: str
    node_id: str
    image: str  # xrv9000 or vmx
    ftp_template_id: str
    reserved_port: Port
    console_username: str
    console_password: str
    ftp_username: str
    ftp_password: str
    ftp_directory: str
    ftp_server_ip: str = "10.0.0.1"
    device_ip: str = "10.0.0.2"
    prefix_length: int = 24
    ftp_console_username: str = ""
    ftp_console_password: str = ""
    # Partition of the GNS3 config disk inside the FTP guest; depends on the
    # template's hdd disk interface (e.g. /dev/sdb1 for SCSI/SATA, /dev/vdb1 for virtio).
    ftp_config_disk: str = "/dev/sdb1"
    command_timeout: float = 120.0
    reachability_timeout: float = 120.0

    def __post_init__(self) -> None:
        self._check_required()
        self._check_names()
        self._check_network()
        if self.command_timeout <= 0 or self.reachability_timeout < 0:
            raise ValueError("command_timeout 必须大于零，reachability_timeout 不能为负")

    @property
    def transfer(self) -> FtpTransfer:
        return FtpTransfer(self.ftp_server_ip, self.device_ip, self.prefix_length,
                           self.ftp_username, self.ftp_password, f"{self.node_id}.cfg")

    def _check_required(self) -> None:
        for label in ("project_id", "node_id", "image", "ftp_template_id", "console_username",
                      "ftp_username", "ftp_password"):
            if not getattr(self, label):
                raise ValueError(f"{label} 不能为空")
        port = self.reserved_port
        if port.adapter_number < 0 or port.port_number < 0 or not port.interface_name:
            raise ValueError("预留端口必须包含有效的 adapter、port 和接口名")

    def _check_names(self) -> None:
        # These values end up in CLI commands, shell commands and the FTP URL path.
        if not SAFE_NAME.fullmatch(self.reserved_port.interface_name.replace("/", "")):
            raise ValueError("预留接口名包含不支持的字符")
        if not self.ftp_directory.startswith("/") or ".." in Path(self.ftp_directory).parts:
            raise ValueError("FTP 目录必须是绝对路径，且不能包含 ..")
        if not re.fullmatch(r"/dev/[A-Za-z0-9_/-]+", self.ftp_config_disk):
            raise ValueError("ftp_config_disk 必须是 /dev 下的设备路径")
        if not SAFE_NAME.fullmatch(self.node_id) or not SAFE_NAME.fullmatch(self.ftp_username):
            raise ValueError("node_id 和 ftp_username 只能包含字母、数字、下划线、点和连字符")

    def _check_network(self) -> None:
        server = ipaddress.ip_address(self.ftp_server_ip)
        device = ipaddress.ip_address(self.device_ip)
        if not isinstance(server, ipaddress.IPv4Address) or not isinstance(device, ipaddress.IPv4Address):
            raise ValueError("FTP 临时链路只支持 IPv4")
        network = ipaddress.ip_network(f"{self.ftp_server_ip}/{self.prefix_length}", strict=False)
        if device == server or device not in network:
            raise ValueError("设备地址与 FTP 地址必须在同一子网")


@dataclass(frozen=True, slots=True)
class ImportResult:
    config_path: Path
    applied: bool
    cleanup_ok: bool
    error: str | None = None
    cleanup_errors: tuple[str, ...] = ()
    remaining_link_id: str | None = None
    remaining_ftp_node_id: str | None = None
    temporary_device_address_may_remain: bool = False

    @property
    def success(self) -> bool:
        return self.applied and self.cleanup_ok


class ImportStageError(RuntimeError):
    """A redacted, operator-readable failure at a known import stage."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(f"{stage}: {message}")


def describe_error(exc: Exception, fallback: str) -> str:
    """Operator message for ``exc``; CLI output and URL credentials never pass through."""
    return str(exc) if isinstance(exc, ImportStageError) else f"{fallback}: {type(exc).__name__}"
