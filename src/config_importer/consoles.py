"""在 GNS3 节点上打开并登录 Console，不依赖具体传输方式。"""

from __future__ import annotations

import time

from .models import ImportStageError, Node
from .ports import Console, ConsoleFactory


# 节点状态为 started 时 Console 端口可能还没开始监听，因此按固定间隔轮询；
# 总时长由调用方的超时决定，这里只要一个足够小的间隔即可。
CONNECT_RETRY_INTERVAL = 1.0


class ConsoleOpener:
    """在节点启动过程中重试连接，并完成 Console 登录。

    设备节点和临时 FTP 节点都存在"已启动但 Console 还不可用"的窗口，把等待、连接
    和登录收敛到一处，可以让各个阶段拿到一致的超时语义和错误信息。
    """

    def __init__(self, factory: ConsoleFactory, timeout: float):
        """保存 Console 工厂和统一超时，供连接重试与登录共用。"""
        self._factory = factory
        self._timeout = timeout

    def open(self, node: Node, username: str, password: str, ready_prompt: str, stage: str) -> Console:
        """连到 ``node`` 的 Console 并登录，返回可以执行命令的连接。

        登录失败时先关闭连接再抛错，避免把半开的 Console 会话留在 GNS3 里；``stage``
        用于把失败归到具体导入阶段，方便业务侧定位。
        """
        console = self._connect(node.console_host, node.console_port, stage)
        try:
            console.login(username, password, ready_prompt, self._timeout)
        except Exception as exc:
            console.close()
            raise ImportStageError(stage, f"Console 登录失败: {type(exc).__name__}") from exc
        return console

    def _connect(self, host: str, port: int, stage: str) -> Console:
        """在超时时间内反复尝试建立连接，成功即返回。

        只对连接类异常重试：这些异常通常意味着节点仍在启动；超过截止时间后抛出该
        阶段的 ``ImportStageError``，让调用方知道是连接问题而不是登录问题。
        """
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                return self._factory.connect(host, port)
            except (OSError, TimeoutError) as exc:
                if time.monotonic() >= deadline:
                    raise ImportStageError(stage, "Console 连接超时") from exc
                time.sleep(min(CONNECT_RETRY_INTERVAL, max(0.0, deadline - time.monotonic())))
