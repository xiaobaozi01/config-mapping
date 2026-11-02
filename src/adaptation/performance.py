"""低开销的转换阶段计时和进程峰值内存指标。

只保存阶段名、耗时、状态和数值，不采样配置内容，也不启用 profiler 或内存分配追踪。
"""

from __future__ import annotations

import ctypes
import sys
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from time import perf_counter


class _ProcessMemoryCounters(ctypes.Structure):
    """声明 Windows GetProcessMemoryInfo 使用的原生结构。

    SIZE_T 字段跟随进程位数，PeakWorkingSetSize 表示进程生命周期的驻留内存高水位。
    """

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_peak_rss() -> int:
    """读取当前 Windows 进程的峰值工作集字节数。

    显式声明句柄和指针类型，避免在 64 位服务中截断系统调用参数。
    """
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    current = kernel.GetCurrentProcess
    current.argtypes, current.restype = [], ctypes.c_void_p
    query = psapi.GetProcessMemoryInfo
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.c_uint32,
    ]
    query.restype = ctypes.c_int
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not query(current(), ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return counters.PeakWorkingSetSize


def process_peak_rss() -> int | None:
    """读取进程生命周期驻留内存峰值，不可用时返回空值。

    macOS 的 ru_maxrss 已以字节计量，Linux 需要从 KiB 转换；Windows 使用原生工作集指标。
    """
    try:
        if sys.platform == "win32":
            return _windows_peak_rss()
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except (ImportError, OSError, AttributeError, ValueError):
        return None


def _monitoring_active(slot: str) -> bool:
    """检查 Python 3.12 及以上的指定监控槽是否实际启用。

    新版 cProfile 可通过 sys.monitoring 工作，不一定出现在 sys.getprofile 中。
    """
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is None:
        return False
    identifier = getattr(monitoring, slot)
    return monitoring.get_tool(identifier) is not None and bool(
        monitoring.get_events(identifier)
    )


def profiler_enabled() -> bool:
    """检测解释器内的传统 profile hook 和新监控接口。

    该标记只提示可检测的插桩，外部进程采样器仍需由操作者自行关闭。
    """
    return sys.getprofile() is not None or _monitoring_active("PROFILER_ID")


def tracing_enabled() -> bool:
    """检测可能改变正常耗时的调试或覆盖率追踪。

    同时兼容传统 trace hook 和 Python 新版监控槽，不主动开启任何追踪工具。
    """
    return (
        sys.gettrace() is not None
        or _monitoring_active("DEBUGGER_ID")
        or _monitoring_active("COVERAGE_ID")
    )


@dataclass
class ConversionPerformance:
    """保存单次转换的聚合计时，不持有设备或配置对象。

    每个上下文拥有独立实例，内存峰值另标注进程口径，避免把历史高水位误认为单任务峰值。
    """

    stages: list[dict[str, object]] = field(default_factory=list)
    cache: dict[str, object] = field(default_factory=dict)
    _started: float = field(default_factory=perf_counter)
    _finished: float | None = None
    _status: str = "running"
    _peak_start: int | None = field(default_factory=process_peak_rss)
    _peak_end: int | None = None
    _profiled: bool = field(default_factory=profiler_enabled)
    _traced: bool = field(default_factory=tracing_enabled)

    @contextmanager
    def measure(self, name: str):
        """记录一个阶段的实际耗时，包括异常退出前的已执行时间。

        调用方可将返回记录标记为业务失败，异常失败则自动标记；异常继续向上传递。
        """
        started = perf_counter()
        record = {"stage": name, "status": "success"}
        try:
            yield record
        except BaseException:
            record["status"] = "failed"
            raise
        finally:
            record["seconds"] = perf_counter() - started
            self.stages.append(record)

    def finish(self, failed: bool) -> None:
        """冻结完整转换耗时和结束时的进程内存高水位。

        性能文件本身的写入排除在总耗时之外，重复读取不会继续增加已完成任务的时间。
        """
        if self._finished is None:
            self._finished = perf_counter()
            self._status = "failed" if failed else "success"
            self._peak_end = process_peak_rss()

    def snapshot(self) -> dict[str, object]:
        """返回只含聚合指标的独立可序列化快照。

        不包含文件名、设备名、配置命令、错误原文或逐条审计内容，可单独用于性能复测。
        """
        end = self._finished if self._finished is not None else perf_counter()
        return {
            "cache": deepcopy(self.cache),
            "schema_version": 1,
            "status": self._status,
            "total_seconds": end - self._started,
            "stages": [dict(record) for record in self.stages],
            "source_semantic_audit": "removed",
            "profiler_enabled": self._profiled,
            "tracing_enabled": self._traced,
            "memory": {
                "scope": "process_lifetime",
                "unit": "bytes",
                "peak_rss_at_start": self._peak_start,
                "peak_rss_at_end": self._peak_end,
            },
        }
