"""配置转换过程中的内部错误类型与不变量检查。"""

from __future__ import annotations


class InvariantViolation(RuntimeError):
    """表示程序内部数据结构违反了已建立的不变量。"""


def require_invariant(condition: bool, message: str) -> None:
    """在优化模式下也执行的内部不变量检查。

    用户输入和可恢复业务错误不应调用此函数，而应通过领域结果或
    ``ValueError`` 等显式错误报告。
    """
    if not condition:
        raise InvariantViolation(message)
