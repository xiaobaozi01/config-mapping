"""Opening logged-in consoles on GNS3 nodes, independent of the transport."""

from __future__ import annotations

import time

from .models import ImportStageError, Node
from .ports import Console, ConsoleFactory


CONNECT_RETRY_INTERVAL = 1.0


class ConsoleOpener:
    """Connect to a node console, retrying while it boots, and log in."""

    def __init__(self, factory: ConsoleFactory, timeout: float):
        self._factory = factory
        self._timeout = timeout

    def open(self, node: Node, username: str, password: str, ready_prompt: str, stage: str) -> Console:
        console = self._connect(node.console_host, node.console_port, stage)
        try:
            console.login(username, password, ready_prompt, self._timeout)
        except Exception as exc:
            console.close()
            raise ImportStageError(stage, f"Console 登录失败: {type(exc).__name__}") from exc
        return console

    def _connect(self, host: str, port: int, stage: str) -> Console:
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                return self._factory.connect(host, port)
            except (OSError, TimeoutError) as exc:
                if time.monotonic() >= deadline:
                    raise ImportStageError(stage, "Console 连接超时") from exc
                time.sleep(min(CONNECT_RETRY_INTERVAL, max(0.0, deadline - time.monotonic())))
