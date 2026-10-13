"""一次导入会话中与设备平台相关的 CLI 交互。

驱动只认识目标设备的提示符和命令，不接触 GNS3 和 FTP 节点；新增平台时继承
``Driver`` 并登记到 ``DRIVERS`` 即可，公共流程不用改动。
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod

from .models import FtpTransfer, ImportStageError
from .ports import Console


# Junos 把加载问题报成 "<file>:<line>:(<col>) syntax error"，随后才输出
# "load complete (N errors)"；IOS XR 则报 "Syntax/Authorization errors ..."。
# 两者即使报错也会照常打印成功字样，所以错误必须在任意位置、任意阶段检查。
ERROR = re.compile(
    r"(?im)\berror:|syntax error|syntax/authorization errors|\(\d+ errors?\)"
    r"|^\s*%\s*(?:error|invalid|incomplete|ambiguous|failed)"
    r"|commit failed|load failed|couldn't open"
)
# ping 只用来探测链路是否已经开始转发，节点启动和链路生效都需要时间，
# 因此按固定间隔重试，总时长由调用方给定。
PING_RETRY_INTERVAL = 2.0


class Driver(ABC):
    """一次设备 CLI 会话，从登录后的提示符一直到退回 exec 模式。

    子类只实现四个平台相关的步骤，命令下发、错误与成功判定、FTP 连通性等待都在
    基类里统一处理，这样两台设备的失败语义和阶段命名保持一致。
    """

    name: str
    ready_prompt: str  # 登录后设备可能出现的任意提示符
    config_prompt: str
    ping_command: str  # 用 ``server`` 格式化；在配置模式下执行
    ping_success: str

    def __init__(self, console: Console, command_timeout: float):
        """绑定已登录的 Console 和单条命令的超时时间。"""
        self._console = console
        self._timeout = command_timeout

    @abstractmethod
    def enter_config(self) -> None:
        """进入配置模式，且不残留上一次会话未提交的改动。

        中断的导入会在设备上留下候选配置，直接下发新命令可能叠加到旧改动上，
        因此进入时必须先复位。
        """

    @abstractmethod
    def configure_transfer(self, interface: str, transfer: FtpTransfer) -> None:
        """给预留接口配上 FTP 链路的地址并使其生效。

        设备只有把 FTP 服务器当成直连邻居，才能通过 FTP 下载配置文件。
        """

    @abstractmethod
    def load_and_commit(self, transfer: FtpTransfer) -> None:
        """用 FTP 上提供的 cfg 整体替换当前配置。

        两个平台的最终动作都是"下载 + 提交"，但命令和确认方式不同，所以由子类实现。
        """

    @abstractmethod
    def leave_config(self) -> None:
        """丢弃未提交的改动并退回 exec 模式。

        供清理阶段调用：无论导入成败，都不给下一次导入留下候选配置或打开的配置会话。
        """

    def wait_for_ftp(self, transfer: FtpTransfer, timeout: float) -> None:
        """反复 ping FTP 地址，直到设备能连通，超时即判失败。

        刚启动的 FTP 节点或刚建好的链路还不具备转发能力，ping 通才说明可以下载配置；
        否则 load/commit 只会得到"文件打不开"之类的误导性报错。
        """
        deadline = time.monotonic() + timeout
        command = self.ping_command.format(server=transfer.server_ip)
        while True:
            if re.search(self.ping_success, self._run(command, self.config_prompt, "ftp-reachability"), re.I):
                return
            if time.monotonic() >= deadline:
                raise ImportStageError(f"{self.name}-ftp-reachability", "设备无法连通 FTP 服务器")
            time.sleep(min(PING_RETRY_INTERVAL, max(0.0, deadline - time.monotonic())))

    def _run(self, command: str, prompt: str, step: str, success: str | None = None) -> str:
        """下发一条命令，并按错误文本和成功标记判定结果。

        阶段名统一拼成 ``<平台>-<步骤>``，便于跨平台定位失败；``success`` 是设备必须
        回显的确认文本，为空时只做错误检查（例如 IOS XR 的 commit 成功时没有输出）。
        """
        stage = f"{self.name}-{step}"
        try:
            output = self._console.command(command, prompt, self._timeout)
        except Exception as exc:
            raise ImportStageError(stage, f"命令未完成: {type(exc).__name__}") from exc
        if ERROR.search(output) or (success is not None and not re.search(success, output, re.I)):
            raise ImportStageError(stage, "设备未确认命令成功")
        return output

    def _probe(self) -> str:
        """发送空命令取回一次新的提示符，用来判断当前处于哪种 CLI 模式。

        设备可能停在 shell、operational 或配置模式（甚至残留在别的模式），提示符是
        唯一可靠的判断依据，因此各子类都先用它探一次。
        """
        return self._run("", self.ready_prompt, "probe")


class JunosDriver(Driver):
    """Junos（vMX）的 CLI 会话实现。

    Junos 是候选配置 + commit 模型：改地址和加载配置都必须显式 commit 才生效，
    离开时用 ``rollback 0`` 丢弃候选改动。
    """

    name = "junos"
    # 登录后可能落在 FreeBSD shell（``%`` 或 ``root@:~ #``），也可能直接进 CLI。
    ready_prompt = r"(?:>|#|%)\s*$"
    operational_prompt = r">\s*$"
    config_prompt = r"#\s*$"
    ping_command = "run ping {server} count 3 rapid"
    ping_success = r"\b[1-9]\d* packets received"

    def enter_config(self) -> None:
        """确保进入 ``[edit]`` 配置模式，并丢弃上次遗留的候选配置。

        登录后可能停在 shell、operational 或配置模式，因此先探提示符再逐级切换；
        末尾统一 ``rollback 0``，避免上一次中断的会话影响本次导入。
        """
        prompt = self._probe()
        if "[edit" not in prompt:
            if not re.search(self.operational_prompt, prompt):
                self._run("cli", self.operational_prompt, "cli")
            self._run("configure", self.config_prompt, "configure")
        # 丢弃上一次被中断的会话留下的候选配置。
        self._run("rollback 0", self.config_prompt, "rollback")

    def configure_transfer(self, interface: str, transfer: FtpTransfer) -> None:
        """把临时地址配到预留接口的 unit 0 上并 commit。

        候选配置不 commit 就不会下发到转发面，FTP 探测也就无法通过。
        """
        self._run(f"set interfaces {interface} unit 0 family inet address "
                  f"{transfer.device_ip}/{transfer.prefix_length}", self.config_prompt, "ftp-ip")
        self._run("commit", self.config_prompt, "ftp-commit", r"commit complete")

    def load_and_commit(self, transfer: FtpTransfer) -> None:
        """用 ``load override`` 整体替换候选配置，确认加载成功后 commit。

        ``override`` 会先清空候选配置再加载，设备最终配置才与 cfg 文件一致；加载回显
        里的语法错误由基类拦下，不会走到 commit。
        """
        self._run(f"load override {transfer.url}", self.config_prompt, "load", r"load complete")
        self._run("commit", self.config_prompt, "commit", r"commit complete")

    def leave_config(self) -> None:
        """回滚候选配置并退出配置模式，把设备交还给下一次导入。"""
        self._run("rollback 0", self.config_prompt, "rollback")
        self._run("exit configuration-mode", self.operational_prompt, "exit")


class IOSXRDriver(Driver):
    """IOS XR（XRv9000）的 CLI 会话实现。

    XR 用 ``commit``/``abort`` 管理配置会话，``commit replace`` 还需要二次确认，
    因此提示符比 Junos 多，用于区分 exec、config 和 config-if 三种模式。
    """

    name = "iosxr"
    ready_prompt = r"#\s*$"
    exec_prompt = r"(?<!\))#\s*$"  # 排除 (config...)# 形式，只匹配 exec 模式
    config_prompt = r"\(config[^)]*\)#\s*$"
    interface_prompt = r"\(config-if\)#\s*$"
    replace_confirm = r"\[no\]:\s*$"  # commit replace 的确认提示
    ping_command = "do ping {server} count 3"
    ping_success = r"success rate is [1-9]\d* percent"

    def enter_config(self) -> None:
        """进入配置模式；若已残留在配置会话中则先 ``abort`` 丢弃未提交改动。"""
        if re.search(self.config_prompt, self._probe()):
            # abort 丢弃上一次会话未提交的改动。
            self._run("abort", self.exec_prompt, "abort")
        self._run("configure", self.config_prompt, "configure")

    def configure_transfer(self, interface: str, transfer: FtpTransfer) -> None:
        """给预留接口配 IPv4 地址、确保接口 up，再退出接口上下文并 commit。

        XR 的 commit 成功时不打印任何内容，所以这一步只靠错误文本判定成败。
        """
        self._run(f"interface {interface}", self.interface_prompt, "interface")
        self._run(f"ipv4 address {transfer.device_ip} {transfer.netmask}", self.interface_prompt, "ftp-ip")
        self._run("no shutdown", self.interface_prompt, "ftp-enable")
        self._run("root", self.config_prompt, "root")
        # XR 的 commit 成功时没有输出，失败则由 ERROR 捕获。
        self._run("commit", self.config_prompt, "ftp-commit")

    def load_and_commit(self, transfer: FtpTransfer) -> None:
        """通过 FTP 加载 cfg，并用 ``commit replace`` 整体替换配置。

        ``commit replace`` 会先弹确认提示，因此先按两种可能的提示符读取输出，出现确认
        提示时再补一个 ``yes``，避免确认提示把命令交互卡住。
        """
        self._run(f"load {transfer.url}", self.config_prompt, "load", r"(?:bytes|lines?) parsed")
        output = self._run("commit replace", f"(?:{self.replace_confirm}|{self.config_prompt})", "commit")
        if re.search(self.replace_confirm, output):
            self._run("yes", self.config_prompt, "commit")

    def leave_config(self) -> None:
        """abort 当前配置会话，丢弃未提交改动并退回 exec 模式。"""
        self._run("abort", self.exec_prompt, "abort")


# 镜像标识（ImportSettings.image）到驱动的映射；新增平台时在这里登记。
DRIVERS: dict[str, type[Driver]] = {"vmx": JunosDriver, "xrv9000": IOSXRDriver}
