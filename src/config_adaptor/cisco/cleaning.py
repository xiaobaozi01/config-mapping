"""Cisco IOS XR 配置清洗函数。"""

from __future__ import annotations

import re

from ..common.lab_account import LAB_PASSWORD, LAB_USERNAME
from ..common.outcomes import CleanupOutcome
from .document import CiscoDocument, CiscoNode, strip_matching_nodes


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
    for block in document.root.children:
        if not block.active:
            continue
        header = block.header.strip()
        matched = next(
            (category for category, pattern in _MANAGEMENT_TOP_LEVEL if pattern.match(header)),
            None,
        )
        if matched:
            block.active = False
            outcome.record(matched)
            continue
        if re.match(r"^line\b", header, re.IGNORECASE):
            block.children, removed = strip_matching_nodes(
                block.children,
                _LINE_AUTH_PATTERN,
                match_anywhere=True,
            )
            outcome.record("line-auth-reference", removed)
    return outcome


def add_lab_account(document: CiscoDocument) -> None:
    """在 ``end``/``commit`` 结束标记前插入统一的实验账号。

    清除生产认证后必须建立已知的本地登录入口，否则输出设备无法接管；插在结束
    标记之前可确保账号落在配置主体内，又不破坏结束标记之后的任何内容。
    """
    account = CiscoNode(
        header=f"username {LAB_USERNAME}",
        children=[
            CiscoNode(f"secret 0 {LAB_PASSWORD}"),
            CiscoNode("group root-system"),
        ],
    )
    insert_at = next(
        (
            index
            for index, block in enumerate(document.root.children)
            if block.active and block.header.strip().lower() in {"end", "commit"}
        ),
        len(document.root.children),
    )
    document.root.children[insert_at:insert_at] = [
        account,
        CiscoNode(header="!"),
    ]
