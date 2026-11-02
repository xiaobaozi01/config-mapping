"""Aggregate timing must include failures and never include configuration data."""

import ctypes
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from adaptation import performance, service
from adaptation.performance import ConversionPerformance
from adaptation.pipeline import ConversionPipeline

FIXTURES = Path(__file__).parent / "fixtures" / "iosxr_bundle"


def convert(output):
    return service.convert(
        FIXTURES / "topology.xlsx", FIXTURES / "configs", output
    )


def load(output):
    return json.loads((output / "performance.json").read_text())


def test_timer_freezes_and_copies_aggregate_snapshot():
    metrics = ConversionPerformance(_started=0, _peak_start=10)
    with (
        patch.object(performance, "perf_counter", side_effect=[2, 5, 8]),
        patch.object(performance, "process_peak_rss", return_value=20),
    ):
        with metrics.measure("stage"):
            pass
        metrics.finish(False)
    first = metrics.snapshot()
    assert first["total_seconds"] == 8
    assert first["stages"] == [
        {"stage": "stage", "status": "success", "seconds": 3}
    ]
    assert first["memory"] == {
        "scope": "process_lifetime",
        "unit": "bytes",
        "peak_rss_at_start": 10,
        "peak_rss_at_end": 20,
    }
    first["stages"][0]["seconds"] = 999
    assert metrics.snapshot()["stages"][0]["seconds"] == 3
    metrics.finish(True)
    assert metrics.snapshot()["status"] == "success"


def test_failed_stage_is_recorded_and_exception_propagates():
    metrics = ConversionPerformance()
    with pytest.raises(RuntimeError, match="private error"):
        with metrics.measure("final_validation"):
            raise RuntimeError("private error")
    assert metrics.stages[0]["status"] == "failed"
    assert "private error" not in json.dumps(metrics.snapshot())


def test_output_measurement_includes_the_whole_write():
    metrics = ConversionPerformance(_started=0)
    context = SimpleNamespace(performance=metrics, has_errors=False)

    def write(*args):
        performance.perf_counter()
        performance.perf_counter()

    with (
        patch.object(performance, "perf_counter", side_effect=[2, 3, 4, 5]),
        patch.object(service, "write_outputs", side_effect=write),
    ):
        service._write_timed_outputs(context, Path("unused"))
    assert metrics.stages == [
        {"stage": "output", "status": "success", "seconds": 3}
    ]


def test_success_records_all_stages_without_private_data(tmp_path):
    context = convert(tmp_path)
    assert not context.has_errors
    data = load(tmp_path)
    stages = data["stages"]
    assert [r["stage"] for r in stages] == [
        "input_preparation",
        "pipeline_setup",
        "cleaning.pre_analysis",
        "TopologyPreflightHandler",
        "GroupExpansionHandler",
        "PreconfiguredInterfaceCleanupHandler",
        "InterfaceClassificationHandler",
        "NonNNICleanupHandler",
        "NNIHandler",
        "ReferenceRewriteHandler",
        "cleaning.post_rewrite",
        "InterfaceCompatibilityHandler",
        "SimulationAdaptationHandler",
        "FinalInterfaceValidationHandler",
        "output",
    ]
    assert all(
        record["status"] == "success" and record["seconds"] >= 0
        for record in stages
    )
    assert data["total_seconds"] >= sum(r["seconds"] for r in stages)
    assert data["status"] == "success"
    assert not data["profiler_enabled"]
    assert data["source_semantic_audit"] == "removed"
    assert data == context.performance.snapshot()
    assert (
        data["memory"]["peak_rss_at_end"] is None
        or data["memory"]["peak_rss_at_end"] > 0
    )
    text = json.dumps(data)
    for forbidden in (
        str(tmp_path),
        "GigabitEthernet",
        "R1.cfg",
        "192.0.2",
        "LAB_PASSWORD",
        "mtu",
    ):
        assert forbidden not in text


def test_input_exception_keeps_failed_input_timing(tmp_path):
    with patch.object(
        service, "prepare_context", side_effect=ValueError("private input")
    ):
        with pytest.raises(ValueError, match="private input"):
            convert(tmp_path)
    data = load(tmp_path)
    assert data["status"] == "failed"
    assert [r["stage"] for r in data["stages"]] == [
        "input_preparation",
        "failure_output",
    ]
    assert all(r["status"] == "failed" for r in data["stages"])
    assert "private input" not in json.dumps(data)
    assert "private input" in (tmp_path / "report.json").read_text()


@pytest.mark.parametrize("raises", [False, True])
def test_pipeline_failure_records_timing_and_skips_later_stages(
    tmp_path, raises
):
    class Failure:

        def process(self, context):
            if raises:
                raise RuntimeError("private pipeline")
            context.errors.append("private pipeline")

    class Never:

        def process(self, context):
            raise AssertionError("must not run")

    with patch.object(
        service,
        "build_default_pipeline",
        return_value=ConversionPipeline([Failure(), Never()]),
    ):
        if raises:
            with pytest.raises(RuntimeError, match="private pipeline"):
                convert(tmp_path)
        else:
            assert convert(tmp_path).has_errors
    data = load(tmp_path)
    assert data["status"] == "failed"
    assert (
        next(r for r in data["stages"] if r["stage"] == "Failure")["status"]
        == "failed"
    )
    assert all(r["stage"] != "Never" for r in data["stages"])
    assert not (tmp_path / "configs").exists()
    assert "private pipeline" not in json.dumps(data)


def test_output_failure_records_both_attempts_without_masking_error(tmp_path):
    with patch.object(
        service, "_publish_configs", side_effect=OSError("private output")
    ):
        with pytest.raises(OSError, match="private output"):
            convert(tmp_path)
    data = load(tmp_path)
    assert data["status"] == "failed"
    assert [r["stage"] for r in data["stages"]][-2:] == [
        "output",
        "failure_output",
    ]
    assert all(r["status"] == "failed" for r in data["stages"][-2:])
    assert "private output" not in json.dumps(data)


def test_metrics_write_failure_does_not_change_business_success(
    tmp_path, caplog
):
    original = service._write_text

    def write(directory, name, text):
        if name == service.OUTPUT_PERFORMANCE:
            raise PermissionError("private metric path")
        return original(directory, name, text)

    with patch.object(service, "_write_text", side_effect=write):
        context = convert(tmp_path)
    assert not context.has_errors
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "configs" / "R1.cfg").exists()
    assert "Unable to write aggregate performance metrics" in caplog.text
    assert "private metric path" not in caplog.text


def test_separate_tasks_and_profile_flag(tmp_path):
    import cProfile

    first = convert(tmp_path / "first")
    second = cProfile.Profile().runcall(convert, tmp_path / "second")
    assert first.performance is not second.performance
    assert not load(tmp_path / "first")["profiler_enabled"]
    assert load(tmp_path / "second")["profiler_enabled"]
    assert len(first.performance.stages) == len(second.performance.stages)


@pytest.mark.parametrize("platform,scale", [("darwin", 1), ("linux", 1024)])
def test_unix_peak_rss_units(platform, scale):
    resource = pytest.importorskip("resource")
    with (
        patch.object(performance.sys, "platform", platform),
        patch.object(
            resource, "getrusage", return_value=SimpleNamespace(ru_maxrss=42)
        ),
    ):
        assert performance.process_peak_rss() == 42 * scale


def test_windows_backend_and_unavailable_memory():
    with patch.object(performance.sys, "platform", "win32"):
        with patch.object(
            performance, "_windows_peak_rss", return_value=123456
        ):
            assert performance.process_peak_rss() == 123456
        with patch.object(
            performance, "_windows_peak_rss", side_effect=OSError
        ):
            assert performance.process_peak_rss() is None


def test_windows_native_types_and_peak_working_set():
    current = SimpleNamespace()
    query = SimpleNamespace()

    class Function:

        def __init__(self, function):
            self.function = function

        def __call__(self, *args):
            return self.function(*args)

    handle = 0x100001234
    current = Function(lambda: handle)

    def read(process, counters, size):
        assert process == handle
        assert size == ctypes.sizeof(performance._ProcessMemoryCounters)
        counters._obj.PeakWorkingSetSize = 777
        return 1

    query = Function(read)
    libraries = {
        "kernel32": SimpleNamespace(GetCurrentProcess=current),
        "psapi": SimpleNamespace(GetProcessMemoryInfo=query),
    }
    with patch.object(
        performance.ctypes,
        "WinDLL",
        create=True,
        side_effect=lambda name, **kwargs: libraries[name],
    ):
        assert performance._windows_peak_rss() == 777
    assert current.restype is ctypes.c_void_p
    assert query.argtypes[0] is ctypes.c_void_p


def test_monitoring_detection_handles_old_and_new_interpreters():
    with (
        patch.object(performance.sys, "getprofile", return_value=None),
        patch.object(performance.sys, "gettrace", return_value=None),
    ):
        with patch.object(performance.sys, "monitoring", None, create=True):
            assert not performance.profiler_enabled()
            assert not performance.tracing_enabled()
        events = {}
        monitor = SimpleNamespace(
            PROFILER_ID=2,
            DEBUGGER_ID=0,
            COVERAGE_ID=1,
            get_tool=lambda slot: "tool",
            get_events=lambda slot: events.get(slot, 0),
        )
        with patch.object(performance.sys, "monitoring", monitor, create=True):
            assert not performance.profiler_enabled()
            events[2] = 1
            assert performance.profiler_enabled()
            events[1] = 1
            assert performance.tracing_enabled()


def test_cancelled_task_metrics_do_not_claim_success(tmp_path):
    with patch.object(
        service, "prepare_context", side_effect=KeyboardInterrupt
    ):
        with pytest.raises(KeyboardInterrupt):
            convert(tmp_path)
    data = load(tmp_path)
    assert data["status"] == "failed"
    assert data["stages"][0]["status"] == "failed"


def test_retest_summary_requires_success_and_unprofiled_repeats():
    from benchmarks.conversion_retest import summarize

    def row(seconds, **extra):
        return dict(
            status="success",
            warmup=False,
            total_seconds=seconds,
            profiler_enabled=False,
            tracing_enabled=False,
            **extra,
        )

    rows = [dict(row(1), warmup=True), row(500), row(620), row(550)]
    summary = summarize(rows, 3, False)
    assert summary["measured_median_seconds"] == 550
    assert summary["meets_target"] is True
    assert summarize([row(500)], 1, False)["meets_target"] is None
    assert summarize(rows, 3, True)["meets_target"] is None
    assert summarize(rows[1:], 3, False)["meets_target"] is None
    rows[0]["status"] = "failed"
    assert summarize(rows, 3, False)["meets_target"] is None
    rows[0]["status"] = "success"
    rows[1]["profiler_enabled"] = True
    assert summarize(rows, 3, False)["meets_target"] is None


def test_failed_metrics_write_removes_old_metrics_and_keeps_outputs(tmp_path):
    convert(tmp_path)
    assert (tmp_path / "performance.json").exists()
    original = service._write_text

    def write(directory, name, text):
        if name == service.OUTPUT_PERFORMANCE:
            raise OSError("metrics unavailable")
        return original(directory, name, text)

    with patch.object(service, "_write_text", side_effect=write):
        assert not convert(tmp_path).has_errors
    assert not (tmp_path / "performance.json").exists()
    assert (tmp_path / "configs" / "R1.cfg").exists()


def test_metrics_write_failure_never_masks_conversion_exception(tmp_path):
    original = service._write_text

    def write(directory, name, text):
        if name == service.OUTPUT_PERFORMANCE:
            raise OSError("secondary metric error")
        return original(directory, name, text)

    with (
        patch.object(service, "_write_text", side_effect=write),
        patch.object(
            service,
            "_publish_configs",
            side_effect=ValueError("primary conversion error"),
        ),
    ):
        with pytest.raises(ValueError, match="primary conversion error"):
            convert(tmp_path)
