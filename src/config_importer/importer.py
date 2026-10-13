"""编排单次 cfg 导入：前置校验、FTP 暂存、设备会话与资源清理。"""

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
    """一次导入的进度，以及它创建过的每个资源的撤销步骤。

    导入是"边做边建资源"的过程，失败点可能出现在任意一步，因此把撤销动作随创建
    顺序登记下来，清理时才能把这一次真正建出来的东西完整收回。
    """

    ftp: FtpServer
    link_id: str | None = None
    transfer_configured: bool = False
    applied: bool = False
    _undo: list[tuple[str, Callable[[], None]]] = field(default_factory=list)

    def on_cleanup(self, label: str, undo: Callable[[], None]) -> None:
        """登记一个清理动作及其说明。

        按登记顺序逆序执行，因此调用方只需在创建资源后立刻登记，不必自己维护依赖关系。
        """
        self._undo.append((label, undo))

    def cleanup(self) -> list[str]:
        """从新到旧执行撤销步骤，收集失败说明但不中断。

        清理必须尽力而为：任何一步失败都不该让其余临时资源继续留在 GNS3 里，所以逐个
        执行、把错误汇总后交给调用方，而不是在第一个错误处抛出。
        """
        errors: list[str] = []
        while self._undo:
            label, undo = self._undo.pop()
            try:
                undo()
            except Exception as exc:
                errors.append(describe_error(exc, f"{label}失败"))
        return errors


class ConfigImporter:
    """每次调用把一份 cfg 导入一个已启动的 GNS3 节点。

    一个实例绑定一个目标节点和一份 ``ImportSettings``，可以反复调用 ``import_cfg``；
    设备必须已经启动，节点、预留端口和登录凭据全部由业务侧提供。
    """

    def __init__(self, gns3: GNS3Client, consoles: ConsoleFactory, settings: ImportSettings):
        """按镜像选择设备驱动并准备 Console 打开器。

        镜像到驱动的映射在构造时就查一次，遇到不支持的镜像立即报错，不必等到创建了
        FTP 节点才发现无法在这个平台上执行。
        """
        driver = DRIVERS.get(settings.image.lower())
        if driver is None:
            raise ValueError(f"不支持的目标镜像: {settings.image}")
        self._gns3 = gns3
        self._consoles = ConsoleOpener(consoles, settings.command_timeout)
        self._settings = settings
        self._driver = driver

    def import_cfg(self, local_cfg_path: str | Path) -> ImportResult:
        """导入一份本地 .cfg，并且始终尝试清理本次创建的资源。

        清理放在 ``finally`` 里、错误只记录不抛出，这样无论中途失败在哪一步，调用方
        拿到的都是一份说明"配置是否生效、还剩什么"的结果，而不是一个异常。
        """
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
        """确认目标节点可用于导入，并返回它的当前快照。

        节点必须存在、已启动、带齐 compute 与 Console 信息且镜像与驱动匹配；这些都
        在建 FTP 节点和链路之前完成，避免为一个不可用的目标白做一轮准备。
        """
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
        """把 FTP 节点的 0/0 与目标设备的预留口连起来，并登记链路的删除动作。

        FTP 节点侧固定用 0/0，设备侧用业务侧预留的端口，这样不会占用目标节点上已有的
        链路；``link_id`` 是之后清理的唯一凭据，所以探测失败时也保留在结果里。
        """
        s = self._settings
        port = s.reserved_port
        run.link_id = self._gns3.create_link(s.project_id, run.ftp.node_id, 0, 0, s.node_id,
                                             port.adapter_number, port.port_number)

        def delete_link() -> None:
            """删除链路并清空记录，使导入结果能反映链路是否真的被清理。"""
            self._gns3.delete_link(s.project_id, run.link_id)
            run.link_id = None

        run.on_cleanup("临时链路删除", delete_link)

    def _apply_on_device(self, run: _ImportRun, node: Node) -> None:
        """在设备上完成登录、进入配置模式、配临时地址、等待连通并加载配置。

        步骤按依赖顺序串行，并且每一步创建的东西都立刻登记清理：一旦中途失败，清理会
        逆序退出配置模式、关闭 Console，不给下一次导入留下候选配置或半开会话。
        """
        s = self._settings
        console = self._consoles.open(node, s.console_username, s.console_password,
                                      self._driver.ready_prompt, "device-login")
        run.on_cleanup("设备 Console 关闭", console.close)
        driver: Driver = self._driver(console, s.command_timeout)
        driver.enter_config()
        # 不给下一次导入留下候选配置或打开的配置会话。
        run.on_cleanup("设备退出配置模式", driver.leave_config)
        transfer = s.transfer
        run.transfer_configured = True
        driver.configure_transfer(s.reserved_port.interface_name, transfer)
        driver.wait_for_ftp(transfer, s.reachability_timeout)
        driver.load_and_commit(transfer)
        run.applied = True


def _read_cfg(path: Path) -> bytes:
    """读取并做最小校验的 cfg 内容。

    只接受存在且非空的 ``.cfg`` 文件：设备侧是整体替换配置，空文件或误传的别的文件
    会把设备配置清空，所以在读取阶段就拦下来。
    """
    if path.suffix.lower() != ".cfg" or not path.is_file():
        raise ImportStageError("input", "需要一个存在的 .cfg 文件")
    content = path.read_bytes()
    if not content.strip():
        raise ImportStageError("input", "配置文件为空")
    return content
