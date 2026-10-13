"""基于 ``telnetlib`` 的 GNS3 串口 Console 连接。

``telnetlib`` 在 Python 3.13 中已移出标准库，该版本下由 ``standard-telnetlib`` 包
提供同名模块。
"""

from __future__ import annotations

import re
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import telnetlib


# 用户名提示符，出现在行尾。
_LOGIN_PROMPT = re.compile(rb"(?:login|username):\s*$", re.I | re.M)
# XRv9000 首次启动会额外询问 root-system 的用户名和密钥。
_PASSWORD_PROMPT = re.compile(rb"(?:password|secret(?: again)?):\s*$", re.I | re.M)
# 最坏情况是 XRv9000 首次启动：先走 root-system 对话（用户名、密钥、再次密钥），
# 再走常规登录（用户名、密码），最后才出现提示符。
_LOGIN_ROUNDS = 3 + 2 + 1


class TelnetConsole:
    """一个 GNS3 节点 Console 的 Telnet 实现。

    只用纯 NVT 流、不做 Telnet 选项协商，正好匹配 GNS3 暴露的串口 Console 端口，
    也避免不同设备的协商差异干扰提示符匹配。
    """

    def __init__(self, host: str, port: int, connect_timeout: float = 10.0):
        """建立 TCP 连接。

        超时只作用于连接阶段；建连之后的读写时限由各方法自己的 ``timeout`` 参数控制，
        因为登录和命令下发的等待时间差别很大。
        """
        # telnetlib 会拒绝 Console 提出的所有选项，从而保持纯 NVT 流。
        self._telnet = telnetlib.Telnet(host, port, connect_timeout)

    def _expect(self, patterns: list[re.Pattern[bytes]], timeout: float) -> tuple[int, str]:
        """等待任一模式匹配，返回命中的下标和已解码的输出。

        把超时统一转成 ``TimeoutError``，上层就能区分"提示符迟迟没出现"和"连接本身
        失败"，并给出不同的错误说明。
        """
        index, _, data = self._telnet.expect(patterns, timeout)
        if index < 0:
            raise TimeoutError("Console prompt timeout")
        return index, data.decode("utf-8", errors="replace")

    def _sendline(self, value: str) -> None:
        """发送一行文本，以裸 CR 结尾。

        发送前先丢弃未读输出，避免上一次遗留的提示符立刻满足下一次 expect，导致读到
        错位的输出；串口 Console 会把 CR LF 当成两次回车，所以只发 CR。
        """
        # 丢弃未读输出，避免旧提示符满足下一次 expect。
        self._telnet.read_very_eager()
        # 只发裸 CR：串口 Console 可能把 CR LF 当成两次回车。
        self._telnet.write(value.encode("utf-8") + b"\r")

    def login(self, username: str, password: str, ready_pattern: str, timeout: float) -> None:
        """按设备实际提出的提示回答用户名和密钥，直到出现就绪提示符。

        设备可能完全不询问、也可能先问一轮 root-system 凭据再问常规登录，所以这里按
        最大轮数循环、碰到哪个提示就回答哪个；一旦提前出现就绪提示符立即返回。
        """
        patterns = [_LOGIN_PROMPT, _PASSWORD_PROMPT, _compile(ready_pattern)]
        self._sendline("")
        for _ in range(_LOGIN_ROUNDS):
            match, _ = self._expect(patterns, timeout)
            if match == 2:
                return
            if match == 0:
                if not username:
                    raise ValueError("Console requested a username")
                self._sendline(username)
            else:
                self._sendline(password)
        raise TimeoutError("Console login did not reach a ready prompt")

    def command(self, text: str, prompt_pattern: str, timeout: float) -> str:
        """发送一条命令并读到 ``prompt_pattern``，返回这段原始输出。

        返回的内容包含命令回显，交给驱动做错误和成功文本判定；提示符匹配失败会抛出
        ``TimeoutError``，由上层转成对应的导入阶段错误。
        """
        self._sendline(text)
        _, output = self._expect([_compile(prompt_pattern)], timeout)
        return output

    def close(self) -> None:
        """关闭 Telnet 连接。"""
        self._telnet.close()


class TelnetConsoleFactory:
    """按主机和端口创建 Telnet Console，供导入器注入使用。"""

    def __init__(self, connect_timeout: float = 10.0):
        """保存连接超时，应用于此后创建的每一个 Console。"""
        self.connect_timeout = connect_timeout

    def connect(self, host: str, port: int) -> TelnetConsole:
        """建立一条尚未登录的新连接。"""
        return TelnetConsole(host, port, self.connect_timeout)


def _compile(pattern: str) -> re.Pattern[bytes]:
    """把字符串模式编译成字节模式，供 telnetlib 的 expect 使用。

    Console 输出按字节匹配，这里统一加上忽略大小写和多行模式，与驱动里书写提示符
    的方式保持一致。
    """
    return re.compile(pattern.encode("utf-8"), re.I | re.M)
