"""GNS3 serial consoles over Telnet, built on ``telnetlib``.

``telnetlib`` left the standard library in Python 3.13; there the
``standard-telnetlib`` package provides the same module.
"""

from __future__ import annotations

import re
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import telnetlib


_LOGIN_PROMPT = re.compile(rb"(?:login|username):\s*$", re.I | re.M)
# XRv9000 first boot asks for a root-system username and secret.
_PASSWORD_PROMPT = re.compile(rb"(?:password|secret(?: again)?):\s*$", re.I | re.M)
# Worst case is an XRv9000 first boot: the root-system dialog (username, secret,
# secret again), then the regular login (username, password), then the prompt.
_LOGIN_ROUNDS = 3 + 2 + 1


class TelnetConsole:
    def __init__(self, host: str, port: int, connect_timeout: float = 10.0):
        # telnetlib refuses every option the console offers, keeping a plain NVT stream.
        self._telnet = telnetlib.Telnet(host, port, connect_timeout)

    def _expect(self, patterns: list[re.Pattern[bytes]], timeout: float) -> tuple[int, str]:
        index, _, data = self._telnet.expect(patterns, timeout)
        if index < 0:
            raise TimeoutError("Console prompt timeout")
        return index, data.decode("utf-8", errors="replace")

    def _sendline(self, value: str) -> None:
        # Drop unread output so a stale prompt cannot satisfy the next expect.
        self._telnet.read_very_eager()
        # A bare CR: serial consoles may treat CR LF as two Enter presses.
        self._telnet.write(value.encode("utf-8") + b"\r")

    def login(self, username: str, password: str, ready_pattern: str, timeout: float) -> None:
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
        self._sendline(text)
        _, output = self._expect([_compile(prompt_pattern)], timeout)
        return output

    def close(self) -> None:
        self._telnet.close()


class TelnetConsoleFactory:
    def __init__(self, connect_timeout: float = 10.0):
        self.connect_timeout = connect_timeout

    def connect(self, host: str, port: int) -> TelnetConsole:
        return TelnetConsole(host, port, self.connect_timeout)


def _compile(pattern: str) -> re.Pattern[bytes]:
    return re.compile(pattern.encode("utf-8"), re.I | re.M)
