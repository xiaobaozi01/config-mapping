"""Orchestrates one cfg import: preflight, FTP staging, device session, cleanup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .consoles import ConsoleOpener
from .drivers import DRIVERS, Driver
from .ftp_server import FtpServer
from .models import ImportResult, ImportSettings, ImportStageError, Node, describe_error
from .ports import ConsoleFactory, GNS3Client


@dataclass(slots=True)
class _ImportRun:
    """Progress of one call plus the undo steps for everything it created."""

    ftp: FtpServer
    link_id: str | None = None
    transfer_configured: bool = False
    applied: bool = False
    _undo: list[tuple[str, Callable[[], None]]] = field(default_factory=list)

    def on_cleanup(self, label: str, undo: Callable[[], None]) -> None:
        self._undo.append((label, undo))

    def cleanup(self) -> list[str]:
        """Run undo steps newest first; report, but do not stop at, failures."""
        errors: list[str] = []
        while self._undo:
            label, undo = self._undo.pop()
            try:
                undo()
            except Exception as exc:
                errors.append(describe_error(exc, f"{label}失败"))
        return errors


class ConfigImporter:
    """Import one cfg into one already started GNS3 node per call."""

    def __init__(self, gns3: GNS3Client, consoles: ConsoleFactory, settings: ImportSettings):
        driver = DRIVERS.get(settings.image.lower())
        if driver is None:
            raise ValueError(f"不支持的目标镜像: {settings.image}")
        self._gns3 = gns3
        self._consoles = ConsoleOpener(consoles, settings.command_timeout)
        self._settings = settings
        self._driver = driver

    def import_cfg(self, local_cfg_path: str | Path) -> ImportResult:
        """Apply one local .cfg; always attempt cleanup of resources created here."""
        path = Path(local_cfg_path)
        run = _ImportRun(FtpServer(self._gns3, self._consoles, self._settings))
        error: str | None = None
        try:
            content = _read_cfg(path)
            node = self._preflight()
            run.on_cleanup("FTP 节点删除", run.ftp.delete)
            run.ftp.prepare(node.compute_id, content)
            self._link_ftp(run)
            self._apply_on_device(run, node)
        except Exception as exc:
            error = describe_error(exc, "导入失败")
        finally:
            cleanup_errors = run.cleanup()

        return ImportResult(
            config_path=path, applied=run.applied, cleanup_ok=not cleanup_errors,
            error=error, cleanup_errors=tuple(cleanup_errors),
            remaining_link_id=run.link_id, remaining_ftp_node_id=run.ftp.node_id,
            temporary_device_address_may_remain=run.transfer_configured and not run.applied,
        )

    def _preflight(self) -> Node:
        s = self._settings
        node = self._gns3.get_node(s.project_id, s.node_id)
        if node.node_id != s.node_id or node.status != "started":
            raise ImportStageError("preflight", "目标节点不存在或未启动")
        if not node.compute_id or not node.console_host or not node.console_port:
            raise ImportStageError("preflight", "目标节点缺少 compute 或 Console 信息")
        if node.image and s.image.lower() not in node.image.lower():
            raise ImportStageError("preflight", "目标节点镜像与导入器配置不匹配")
        return node

    def _link_ftp(self, run: _ImportRun) -> None:
        s = self._settings
        port = s.reserved_port
        run.link_id = self._gns3.create_link(s.project_id, run.ftp.node_id, 0, 0, s.node_id,
                                             port.adapter_number, port.port_number)

        def delete_link() -> None:
            self._gns3.delete_link(s.project_id, run.link_id)
            run.link_id = None

        run.on_cleanup("临时链路删除", delete_link)

    def _apply_on_device(self, run: _ImportRun, node: Node) -> None:
        s = self._settings
        console = self._consoles.open(node, s.console_username, s.console_password,
                                      self._driver.ready_prompt, "device-login")
        run.on_cleanup("设备 Console 关闭", console.close)
        driver: Driver = self._driver(console, s.command_timeout)
        driver.enter_config()
        # Leaves no candidate or open config session for the next import.
        run.on_cleanup("设备退出配置模式", driver.leave_config)
        transfer = s.transfer
        run.transfer_configured = True
        driver.configure_transfer(s.reserved_port.interface_name, transfer)
        driver.wait_for_ftp(transfer, s.reachability_timeout)
        driver.load_and_commit(transfer)
        run.applied = True


def _read_cfg(path: Path) -> bytes:
    if path.suffix.lower() != ".cfg" or not path.is_file():
        raise ImportStageError("input", "需要一个存在的 .cfg 文件")
    content = path.read_bytes()
    if not content.strip():
        raise ImportStageError("input", "配置文件为空")
    return content
