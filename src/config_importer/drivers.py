"""Device-specific CLI dialogs for one import session.

A driver knows the prompts and commands of one network OS; it never touches
GNS3 or the FTP node. Add a platform by subclassing ``Driver`` and listing it in
``DRIVERS``.
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod

from .models import FtpTransfer, ImportStageError
from .ports import Console


# Junos reports load problems as "<file>:<line>:(<col>) syntax error" followed by
# "load complete (N errors)"; IOS XR as "Syntax/Authorization errors ...". Both
# still print their normal success text, so errors must be checked anywhere.
ERROR = re.compile(
    r"(?im)\berror:|syntax error|syntax/authorization errors|\(\d+ errors?\)"
    r"|^\s*%\s*(?:error|invalid|incomplete|ambiguous|failed)"
    r"|commit failed|load failed|couldn't open"
)
PING_RETRY_INTERVAL = 2.0


class Driver(ABC):
    """One CLI session on a target device, from login prompt back to exec mode."""

    name: str
    ready_prompt: str  # any prompt the device may show right after login
    config_prompt: str
    ping_command: str  # formatted with ``server``; run from configuration mode
    ping_success: str

    def __init__(self, console: Console, command_timeout: float):
        self._console = console
        self._timeout = command_timeout

    @abstractmethod
    def enter_config(self) -> None:
        """Reach configuration mode with no leftover uncommitted changes."""

    @abstractmethod
    def configure_transfer(self, interface: str, transfer: FtpTransfer) -> None:
        """Address and commit the reserved interface for the FTP link."""

    @abstractmethod
    def load_and_commit(self, transfer: FtpTransfer) -> None:
        """Replace the whole configuration with the cfg served over FTP."""

    @abstractmethod
    def leave_config(self) -> None:
        """Discard anything uncommitted and return to exec mode."""

    def wait_for_ftp(self, transfer: FtpTransfer, timeout: float) -> None:
        # A started FTP node or a fresh link is not yet a forwarding path.
        deadline = time.monotonic() + timeout
        command = self.ping_command.format(server=transfer.server_ip)
        while True:
            if re.search(self.ping_success, self._run(command, self.config_prompt, "ftp-reachability"), re.I):
                return
            if time.monotonic() >= deadline:
                raise ImportStageError(f"{self.name}-ftp-reachability", "设备无法连通 FTP 服务器")
            time.sleep(min(PING_RETRY_INTERVAL, max(0.0, deadline - time.monotonic())))

    def _run(self, command: str, prompt: str, step: str, success: str | None = None) -> str:
        """Send one command; fail on any error text or a missing success marker."""
        stage = f"{self.name}-{step}"
        try:
            output = self._console.command(command, prompt, self._timeout)
        except Exception as exc:
            raise ImportStageError(stage, f"命令未完成: {type(exc).__name__}") from exc
        if ERROR.search(output) or (success is not None and not re.search(success, output, re.I)):
            raise ImportStageError(stage, "设备未确认命令成功")
        return output

    def _probe(self) -> str:
        """Return a fresh prompt so callers can tell which CLI mode is active."""
        return self._run("", self.ready_prompt, "probe")


class JunosDriver(Driver):
    name = "junos"
    # Login may land in the FreeBSD shell (``%`` or ``root@:~ #``) or the CLI.
    ready_prompt = r"(?:>|#|%)\s*$"
    operational_prompt = r">\s*$"
    config_prompt = r"#\s*$"
    ping_command = "run ping {server} count 3 rapid"
    ping_success = r"\b[1-9]\d* packets received"

    def enter_config(self) -> None:
        prompt = self._probe()
        if "[edit" not in prompt:
            if not re.search(self.operational_prompt, prompt):
                self._run("cli", self.operational_prompt, "cli")
            self._run("configure", self.config_prompt, "configure")
        # Discard any candidate left behind by an earlier interrupted session.
        self._run("rollback 0", self.config_prompt, "rollback")

    def configure_transfer(self, interface: str, transfer: FtpTransfer) -> None:
        self._run(f"set interfaces {interface} unit 0 family inet address "
                  f"{transfer.device_ip}/{transfer.prefix_length}", self.config_prompt, "ftp-ip")
        self._run("commit", self.config_prompt, "ftp-commit", r"commit complete")

    def load_and_commit(self, transfer: FtpTransfer) -> None:
        self._run(f"load override {transfer.url}", self.config_prompt, "load", r"load complete")
        self._run("commit", self.config_prompt, "commit", r"commit complete")

    def leave_config(self) -> None:
        self._run("rollback 0", self.config_prompt, "rollback")
        self._run("exit configuration-mode", self.operational_prompt, "exit")


class IOSXRDriver(Driver):
    name = "iosxr"
    ready_prompt = r"#\s*$"
    exec_prompt = r"(?<!\))#\s*$"
    config_prompt = r"\(config[^)]*\)#\s*$"
    interface_prompt = r"\(config-if\)#\s*$"
    replace_confirm = r"\[no\]:\s*$"
    ping_command = "do ping {server} count 3"
    ping_success = r"success rate is [1-9]\d* percent"

    def enter_config(self) -> None:
        if re.search(self.config_prompt, self._probe()):
            # ``abort`` drops uncommitted changes of an earlier session.
            self._run("abort", self.exec_prompt, "abort")
        self._run("configure", self.config_prompt, "configure")

    def configure_transfer(self, interface: str, transfer: FtpTransfer) -> None:
        self._run(f"interface {interface}", self.interface_prompt, "interface")
        self._run(f"ipv4 address {transfer.device_ip} {transfer.netmask}", self.interface_prompt, "ftp-ip")
        self._run("no shutdown", self.interface_prompt, "ftp-enable")
        self._run("root", self.config_prompt, "root")
        # XR prints nothing on a successful commit; failures are caught by ERROR.
        self._run("commit", self.config_prompt, "ftp-commit")

    def load_and_commit(self, transfer: FtpTransfer) -> None:
        self._run(f"load {transfer.url}", self.config_prompt, "load", r"(?:bytes|lines?) parsed")
        output = self._run("commit replace", f"(?:{self.replace_confirm}|{self.config_prompt})", "commit")
        if re.search(self.replace_confirm, output):
            self._run("yes", self.config_prompt, "commit")

    def leave_config(self) -> None:
        self._run("abort", self.exec_prompt, "abort")


DRIVERS: dict[str, type[Driver]] = {"vmx": JunosDriver, "xrv9000": IOSXRDriver}
