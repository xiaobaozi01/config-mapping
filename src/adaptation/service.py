"""组织输入加载、责任链执行、扩展规则和最终文件输出。"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

from impl.common.lab_account import LAB_PASSWORD, LAB_USERNAME
from impl.common.policies import WashingPolicy

from .factory import parse_document
from .input_normalization import normalize_input
from .models import (
    ConversionContext,
    Device,
    DeviceContext,
    EventKind,
    ImageProfile,
    TopologyWorkbook,
    Vendor,
)
from .pipeline import build_default_pipeline
from .profiles import load_profiles
from .topology import load_topology, write_adapted_topology

CONFIG_EXTENSIONS = (".cfg", ".conf", ".txt")
_OUTPUT_MANIFEST = ".generated-artifacts.json"
GENERATOR_NAME = "gns3-config-adaptor"
OUTPUT_CONFIGS_DIR = "configs"
OUTPUT_TOPOLOGY = "topology-adapted.xlsx"
OUTPUT_README = "README.txt"
OUTPUT_MAPPING = "interface-mapping.json"
OUTPUT_REPORT = "report.json"
PREVIOUS_OUTPUTS_DIR = ".previous-outputs"


def _output_path(output_dir: Path, name: str) -> Path:
    """构造输出目录内的安全目标路径。

    拒绝绝对路径和父目录跳转可阻止产物逃逸；叶子符号链接由后续原子替换处理。
    """
    relative = Path(name)
    path = output_dir / relative
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not path.parent.resolve().is_relative_to(output_dir.resolve())
    ):
        raise ValueError(f"输出产物路径不安全: {name}")
    return path


def _write_text(output_dir: Path, name: str, text: str) -> None:
    """使用同目录临时文件原子写入文本产物。

    目标文件不会被直接跟随写入，因此失败不会留下半写文件或覆盖链接外部内容。
    """
    path = _output_path(output_dir, name)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _managed_path(output_dir: Path, name: str) -> Path:
    """校验并返回本程序允许管理的输出文件路径。

    白名单阻止清单误删用户文件，并限制配置产物只能是 ``configs`` 下的单层文件。
    """
    relative = Path(name)
    allowed = name in {OUTPUT_TOPOLOGY, OUTPUT_README} or (
        len(relative.parts) == 2
        and relative.parts[0] == OUTPUT_CONFIGS_DIR
        and relative.suffix == ".cfg"
        and relative.name not in {".cfg", ".."}
    )
    if not allowed:
        raise ValueError(f"输出产物路径不安全: {name}")
    return _output_path(output_dir, name)


def _published_files(output_dir: Path) -> set[str]:
    """读取程序清单中明确记录的上次发布文件。

    只信任当前生成器格式的清单，避免根据目录内容推断并误处理用户拥有的文件。
    """
    manifest = output_dir / _OUTPUT_MANIFEST
    if manifest.is_file() and not manifest.is_symlink():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("generator") != GENERATOR_NAME
        ):
            raise ValueError("输出目录中的产物清单不是本程序生成的有效清单")
        files = payload.get("files")
        if not isinstance(files, list) or not all(
            isinstance(name, str) for name in files
        ):
            raise ValueError("输出产物清单的 files 必须是文件名列表")
        for name in files:
            _managed_path(output_dir, name)
        return set(files)
    return set()


def _archive_outputs(
    context: ConversionContext, output_dir: Path, files: set[str]
) -> None:
    """把上次发布的输出移动到隔离归档目录。

    归档只处理清单中的文件，使失败重跑不会覆盖旧成果，也不会移动用户额外文件。
    """
    existing = [
        (name, _managed_path(output_dir, name)) for name in sorted(files)
    ]
    existing = [
        (name, path)
        for name, path in existing
        if path.exists() or path.is_symlink()
    ]
    if not existing:
        return
    archive_root = output_dir / PREVIOUS_OUTPUTS_DIR
    if archive_root.is_symlink():
        raise ValueError("输出归档目录不能是符号链接")
    archive_root.mkdir(exist_ok=True)
    archive = Path(tempfile.mkdtemp(prefix="run-", dir=archive_root))
    for name, path in existing:
        destination = archive / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.rename(destination)
    manifest = output_dir / _OUTPUT_MANIFEST
    if manifest.is_file():
        shutil.copyfile(manifest, archive / _OUTPUT_MANIFEST)
    context.add_event(
        EventKind.PREVIOUS_OUTPUT_ARCHIVED,
        "上次或未完成运行的导出产物已撤下并隔离保存",
        archive=str(archive.relative_to(output_dir)),
        files=[name for name, _ in existing],
    )


def _safe_config_path(
    config_dir: Path, configured: str | None, device_name: str
) -> Path:
    """在配置目录内定位设备配置文件。

    先解析真实路径再校验目录边界，可阻止路径穿越和符号链接逃逸到外部文件。
    """
    root = config_dir.resolve()
    candidates: list[Path] = []
    if configured:
        candidates.append(config_dir / configured)
    else:
        candidates.extend(
            config_dir / f"{device_name}{suffix}"
            for suffix in CONFIG_EXTENSIONS
        )
        lowered = device_name.lower()
        if config_dir.exists():
            candidates.extend(
                item
                for item in config_dir.iterdir()
                if item.is_file()
                and item.suffix.lower() in CONFIG_EXTENSIONS
                and item.stem.lower() == lowered
            )
    # 对每个候选都先 resolve，再确认仍位于用户指定的配置目录中。
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(
                f"设备 {device_name} 的配置文件超出配置目录: {configured}"
            )
        if resolved.is_file():
            return resolved
    wanted = configured or f"{device_name}{{{','.join(CONFIG_EXTENSIONS)}}}"
    raise FileNotFoundError(f"设备 {device_name} 找不到配置文件: {wanted}")


def _safe_output_name(device_name: str) -> str:
    """把设备名转换为跨平台安全的配置文件名。

    保留 ASCII 和中文名称并替换其他字符，使文件名稳定可读且不会形成非法路径。
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]", "_", device_name).strip(
        "._"
    )
    return cleaned or "device"


def _load_device_context(
    device: Device,
    config_dir: Path,
    profile: ImageProfile,
) -> tuple[DeviceContext, list[dict[str, object]]]:
    """加载单台设备配置并生成输入整理审计事件。

    先整理原始采集文本再解析，使被删除的命令回显不会影响厂商文档内容。
    """
    config_path = _safe_config_path(config_dir, device.config_file, device.name)
    text = config_path.read_text(encoding="utf-8-sig")
    normalized = normalize_input(device.vendor, text)
    document = parse_document(device.vendor.value, normalized.text)
    events = [
        {
            "kind": EventKind.INPUT_NORMALIZATION.value,
            "message": f"设备 {device.name} 命中输入整理规则 {removal.rule_id}",
            "device": device.name,
            "rule_id": removal.rule_id,
            "start_line": removal.start_line,
            "end_line": removal.end_line,
            "removed_lines": removal.removed_lines,
        }
        for removal in normalized.removed
    ]
    return (
        DeviceContext(
            device=device,
            config_path=config_path,
            document=document,
            profile=profile,
        ),
        events,
    )


def _collect_devices(
    topology: TopologyWorkbook,
    config_dir: Path,
    profiles: dict[Vendor, ImageProfile],
) -> tuple[
    dict[str, DeviceContext],
    list[str],
    list[str],
    list[dict[str, object]],
    set[str],
]:
    """收集所有支持设备的上下文和全局输入诊断。

    Cisco/Juniper 设备加载失败会保留为可报告错误，华为设备只登记为外置端点。
    """
    device_contexts: dict[str, DeviceContext] = {}
    warnings: list[str] = []
    errors: list[str] = []
    input_events: list[dict[str, object]] = []
    external_devices: set[str] = set()
    for device in topology.devices:
        if device.vendor == Vendor.HUAWEI:
            warnings.append(
                f"设备 {device.name} 为华为设备，不参与配置自适应（无需配置文件），"
                "仅作为拓扑对端保留"
            )
            external_devices.add(device.name)
            continue
        if device.vendor == Vendor.UNKNOWN:
            errors.append(f"设备 {device.name} 的厂商无法识别")
            continue
        try:
            context, events = _load_device_context(
                device, config_dir, profiles[device.vendor]
            )
            device_contexts[device.name] = context
            input_events.extend(events)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    return device_contexts, warnings, errors, input_events, external_devices


def prepare_context(
    topology_path: Path,
    config_dir: Path,
    profiles_path: Path | None = None,
) -> ConversionContext:
    """加载转换输入并创建所有阶段共享的初始上下文。

    拓扑、profile 和配置在此一次性读取，使后续阶段基于同一快照执行并统一报告输入错误。
    """
    topology = load_topology(topology_path)
    profiles = load_profiles(profiles_path)
    washing_policy = WashingPolicy()
    (
        device_contexts,
        warnings,
        errors,
        input_events,
        external_devices,
    ) = _collect_devices(topology, config_dir, profiles)

    return ConversionContext(
        topology=topology,
        devices=device_contexts,
        washing_policy=washing_policy,
        warnings=warnings,
        errors=errors,
        events=input_events,
        external_devices=external_devices,
    )


def convert(
    topology_path: Path,
    config_dir: Path,
    output_dir: Path,
    profiles_path: Path | None = None,
) -> ConversionContext:
    """执行完整转换；即使失败也写 report，便于定位输入问题。

    可预期的业务失败由各阶段写入 ``context.errors``。内部不变量或其他
    未预期异常仍会继续向调用方抛出，但在此之前先写入失败报告，使 CLI
    操作者不会只得到一条无上下文的错误。
    """
    # 输入加载本身也可能失败；空上下文只用于生成失败诊断和撤下旧成果。
    context = ConversionContext(
        topology=TopologyWorkbook(topology_path, None, "", "", {}, {}, [], []),
        devices={},
    )
    phase = "输入加载"
    try:
        context = prepare_context(topology_path, config_dir, profiles_path)
        if not context.errors:
            phase = "清洗规则加载"
            pipeline = build_default_pipeline()
            phase = "核心转换流水线"
            # 清洗规则在接口迁移和引用改写之后执行，不改变原始 NNI 分类依据。
            pipeline.execute(context)
        phase = "输出结果"
        write_outputs(context, output_dir)
    except Exception as exc:
        detail = str(exc).strip() or "异常未提供详细信息"
        message = f"{phase}发生内部异常 " f"({type(exc).__name__}): {detail}"
        context.errors.append(message)
        context.add_event(
            EventKind.INTERNAL_ERROR,
            message,
            phase=phase,
            exception_type=type(exc).__name__,
        )
        try:
            # context 已标记失败，此次调用只写诊断文件，不再渲染设备配置。
            write_outputs(context, output_dir)
        except Exception as report_exc:
            report_failure = RuntimeError(
                f"转换失败后无法写入诊断报告: {report_exc}"
            )
            report_failure.add_note(
                f"原始转换异常: {type(exc).__name__}: {detail}"
            )
            raise report_failure from report_exc
        raise
    return context


def _build_report(
    context: ConversionContext,
    mapping_payload: list[dict[str, object]],
) -> dict[str, object]:
    """构造转换报告的状态、诊断和摘要数据。

    错误与告警在全局和设备级合并去重，使输出报告完整且不重复。
    """
    errors = context.errors + [
        message
        for device in context.devices.values()
        for message in device.errors
    ]
    warnings = context.warnings + [
        message
        for device in context.devices.values()
        for message in device.warnings
    ]
    conflicts = [
        {
            key: value
            for key, value in event.items()
            if key not in {"kind", "message"}
        }
        for event in context.events
        if event.get("kind") == EventKind.GROUP_CONFLICT.value
    ]
    return {
        "status": "failed" if context.has_errors else "success",
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
        "events": context.events,
        "group_conflicts": conflicts,
        "summary": _report_summary(context, mapping_payload, conflicts),
    }


def _report_summary(
    context: ConversionContext,
    mapping_payload: list[dict[str, object]],
    conflicts: list[dict[str, object]],
) -> dict[str, object]:
    """计算报告摘要中的链路、映射和规则统计。

    所有计数从同一事件流推导，使摘要与明细报告保持一致。
    """
    events = context.events
    simulation_count = sum(
        int(event.get("change_count", 0))
        for event in events
        if event.get("kind") == EventKind.SIMULATION_ADAPTATION.value
    )
    ambiguity_count = sum(
        event.get("kind") == EventKind.GROUP_IDENTITY_AMBIGUOUS.value
        for event in events
    )
    rule_hits = sum(
        int(event.get("matched", 0))
        for event in events
        if event.get("kind") == EventKind.GROUP_IDENTITY_COVERAGE.value
    )
    fallbacks = sum(
        int(event.get("fallback", 0))
        for event in events
        if event.get("kind") == EventKind.GROUP_IDENTITY_COVERAGE.value
    )
    identity_total = rule_hits + fallbacks
    return {
        "converted_devices": len(context.devices)
        if not context.has_errors
        else 0,
        "active_links": sum(link.active for link in context.topology.links),
        "skipped_links": sum(
            not link.active for link in context.topology.links
        ),
        "mapping_count": len(mapping_payload),
        "group_conflict_count": len(conflicts),
        "group_identity_ambiguity_count": ambiguity_count,
        "group_identity_rule_hits": rule_hits,
        "group_identity_fallbacks": fallbacks,
        "group_identity_coverage": (
            round(rule_hits / identity_total, 4) if identity_total else None
        ),
        "simulation_adaptation_count": simulation_count,
        "cleaning_rule_hits_by_category": _cleaning_rule_hits(events),
    }


def _cleaning_rule_hits(events: list[dict[str, object]]) -> dict[str, int]:
    """按审计类别累计清洗规则命中数量。

    摘要只保留类别总量，避免重复展示单条规则事件中的完整上下文。
    """
    result: dict[str, int] = {}
    for event in events:
        if event.get("kind") != EventKind.CLEANING_RULE.value:
            continue
        category = str(event.get("category", "uncategorized"))
        result[category] = result.get(category, 0) + int(event.get("hits", 0))
    return result


def _write_diagnostics(
    output_dir: Path,
    mapping_payload: list[dict[str, object]],
    report: dict[str, object],
) -> None:
    """写出接口映射和转换报告 JSON 文件。

    两个文件采用同一原子写入方式，确保失败时不会留下不完整诊断。
    """
    for filename, payload in (
        (OUTPUT_MAPPING, mapping_payload),
        (OUTPUT_REPORT, report),
    ):
        _write_text(
            output_dir,
            filename,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )


def _publish_configs(
    context: ConversionContext,
    output_dir: Path,
    previous_files: set[str],
    rendered_configs: dict[str, str],
) -> None:
    """发布设备配置、产物清单和适配拓扑。

    先归档旧产物并记录新清单，使后续任一步骤失败都能识别本次产生的输出。
    """
    published = {OUTPUT_TOPOLOGY, OUTPUT_README} | {
        f"{OUTPUT_CONFIGS_DIR}/{_safe_output_name(name)}.cfg"
        for name in rendered_configs
    }
    for name in published:
        _managed_path(output_dir, name)
    _archive_outputs(context, output_dir, previous_files)
    _write_manifest(output_dir, published)
    configs_dir = output_dir / OUTPUT_CONFIGS_DIR
    configs_dir.mkdir(parents=True, exist_ok=True)
    for device_name, rendered in rendered_configs.items():
        _write_text(
            output_dir,
            f"{OUTPUT_CONFIGS_DIR}/{_safe_output_name(device_name)}.cfg",
            rendered,
        )
    _replace_topology(context, output_dir)


def _write_manifest(output_dir: Path, published: set[str]) -> None:
    """写入本次运行计划发布的文件清单。

    在发布内容前记录清单，使中途失败时仍能准确撤下可能出现的部分产物。
    """
    payload = {
        "generator": GENERATOR_NAME,
        "files": sorted(published),
    }
    _write_text(
        output_dir,
        _OUTPUT_MANIFEST,
        json.dumps(payload, ensure_ascii=False) + "\n",
    )


def _replace_topology(context: ConversionContext, output_dir: Path) -> None:
    """生成适配拓扑并使用同目录临时文件原子替换目标。

    临时文件与目标同盘保证替换原子性，异常时清理临时产物而不破坏现有拓扑。
    """
    adapted = {
        name: f"{OUTPUT_CONFIGS_DIR}/{_safe_output_name(name)}.cfg"
        for name in context.devices
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".xlsx", dir=output_dir, delete=False
        ) as handle:
            temporary = Path(handle.name)
        write_adapted_topology(
            context.topology,
            temporary,
            config_files=adapted,
        )
        temporary.replace(_output_path(output_dir, OUTPUT_TOPOLOGY))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_output_readme(output_dir: Path) -> None:
    """写出实验环境和输出目录说明。

    README 只包含隔离实验凭据和固定文件说明，避免把转换内容重复写入说明文件。
    """
    readme = f"""GNS3 配置自适应输出

实验登录凭据（仅限隔离实验环境）：
  IOS XR 用户名：{LAB_USERNAME}
  Junos 用户名：root
  密码：{LAB_PASSWORD}

文件说明：
  {OUTPUT_TOPOLOGY}     已更新接口并移除冗余聚合成员的拓扑
  {OUTPUT_CONFIGS_DIR}/                  XRv9000 和 vMX 配置
  {OUTPUT_MAPPING}    全局接口映射
  {OUTPUT_REPORT}               转换报告（含模拟参数适配明细）
"""
    _write_text(output_dir, OUTPUT_README, readme)


def write_outputs(context: ConversionContext, output_dir: Path) -> None:
    """发布转换结果或失败诊断到输出目录。

    成功路径先完成内存渲染再发布，失败路径只撤下旧产物并写诊断，避免产生半套可导入配置。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    previous_files = _published_files(output_dir)
    mapping_payload = [mapping.to_dict() for mapping in context.mappings]
    report = _build_report(context, mapping_payload)
    if context.has_errors:
        _archive_outputs(context, output_dir, previous_files)
        _write_diagnostics(output_dir, mapping_payload, report)
        return

    rendered_configs = {
        name: device.document.render()
        for name, device in context.devices.items()
    }
    _publish_configs(
        context,
        output_dir,
        previous_files,
        rendered_configs,
    )
    _write_output_readme(output_dir)
    _write_diagnostics(output_dir, mapping_payload, report)
