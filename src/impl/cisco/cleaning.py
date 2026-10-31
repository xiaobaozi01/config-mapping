"""Cisco IOS XR 配置清洗函数。"""

from __future__ import annotations

import re

from impl.common.lab_account import LAB_PASSWORD, LAB_USERNAME
from impl.common.outcomes import CleanupOutcome
from .document import CiscoDocument
from .rules.support import delete_matching, append_top_level


# 模块级预编译，避免每次调用重复编译正则，并把清洗数据与遍历逻辑分离。
_MANAGEMENT_TOP_LEVEL: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("username", re.compile(r"^username\b", re.IGNORECASE)),
    ("aaa", re.compile(r"^aaa\b", re.IGNORECASE)),
    ("tacacs", re.compile(r"^(?:tacacs-server|tacacs)\b", re.IGNORECASE)),
    ("radius", re.compile(r"^(?:radius-server|radius)\b", re.IGNORECASE)),
    ("taskgroup", re.compile(r"^task-?group\b", re.IGNORECASE)),
    ("usergroup", re.compile(r"^user-?group\b", re.IGNORECASE)),
    ("snmp", re.compile(r"^snmp-server\b", re.IGNORECASE)),
    ("ssh", re.compile(r"^ssh\b", re.IGNORECASE)),
    ("telnet", re.compile(r"^telnet\b", re.IGNORECASE)),
)

# line 块内需剥除的认证子命令；line 块本身要保留以承载登录终端。
_LINE_AUTH_PATTERN = re.compile(
    r"^(?:password|secret)\b"
    r"|^login\s+authentication\b"
    r"|^authorization\b"
    r"|^accounting\b"
    r"|^users\s+group\b",
    re.IGNORECASE,
)


def clean_management_access(document: CiscoDocument) -> CleanupOutcome:
    """删除 IOS XR 顶层管理面块，并剥除 ``line`` 块内的认证子命令。

    删除 username/aaa/tacacs/radius/taskgroup/usergroup/snmp/ssh/telnet 等顶层块，
    避免生产账号与远程管理凭据进入实验环境；``line`` 块承载 console/vty 等登录终端
    需要保留，因此只递归删除其中的认证子命令而非整块。
    """
    outcome = CleanupOutcome()
    transaction = document.rewrite()
    for block in document.root.nodes:
        if not block.active:
            continue
        header = block.header.strip()
        matched = next(
            (category for category, pattern in _MANAGEMENT_TOP_LEVEL if pattern.match(header)),
            None,
        )
        if matched:
            transaction.delete(block, "删除生产管理面配置")
            outcome.record(matched)
            continue
        if re.match(r"^line\b", header, re.IGNORECASE):
            removed = delete_matching(
                transaction, block.nodes,
                _LINE_AUTH_PATTERN,
                match_anywhere=True,
            )
            outcome.record("line-auth-reference", removed)
    transaction.commit()
    return outcome


def add_lab_account(document: CiscoDocument) -> None:
    """在 ``end``/``commit`` 结束标记前插入统一的实验账号。

    清除生产认证后必须建立已知的本地登录入口，否则输出设备无法接管；插在结束
    标记之前可确保账号落在配置主体内，又不破坏结束标记之后的任何内容。
    """
    transaction = document.rewrite()
    account = f"username {LAB_USERNAME}\n secret 0 {LAB_PASSWORD}\n group root-system\n!\n"
    append_top_level(transaction, document, account, "建立实验账号")
    transaction.commit()
