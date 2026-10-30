"""组织输入加载、责任链执行、扩展规则和最终文件输出。"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

from ..common.lab_account import LAB_PASSWORD, LAB_USERNAME
from .factory import parse_document
from .input_normalization import normalize_input
from .models import ConversionContext, DeviceContext, TopologyWorkbook, Vendor
from .pipeline import build_default_pipeline
from .profiles import load_profiles
from .topology import load_topology, write_adapted_topology
from ..common.policies import WashingPolicy


CONFIG_EXTENSIONS = (".cfg", ".conf", ".txt")
_OUTPUT_MANIFEST = ".generated-artifacts.json"


def _output_path(output_dir: Path, name: str) -> Path:
    """拒绝越界父目录；叶子链接由原子替换处理，绝不跟随写入。"""
    relative = Path(name)
    path = output_dir / relative
    if relative.is_absolute() or ".." in relative.parts or not path.parent.resolve().is_relative_to(output_dir.resolve()):
        raise ValueError(f"输出产物路径不安全: {name}")
    return path


def _write_text(output_dir: Path, name: str, text: str) -> None:
    """在同目录暂存并替换文件，避免覆盖符号链接指向的外部内容。"""
    path = _output_path(output_dir, name)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _managed_path(output_dir: Path, name: str) -> Path:
    """清单只允许本程序发布的固定文件和 configs 下的单层 cfg。"""
    relative = Path(name)
    allowed = name in {"topology-adapted.xlsx", "README.txt"} or (
        len(relative.parts) == 2 and relative.parts[0] == "configs"
        and relative.suffix == ".cfg" and relative.name not in {".cfg", ".."}
    )
    if not allowed:
        raise ValueError(f"输出产物路径不安全: {name}")
    return _output_path(output_dir, name)


def _published_files(output_dir: Path) -> set[str]:
    """读取明确发布过的文件；兼容没有清单的旧版导出，不扫描用户 cfg。"""
    manifest = output_dir / _OUTPUT_MANIFEST
    if manifest.is_file() and not manifest.is_symlink():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("generator") != "gns3-config-adaptor":
            raise ValueError("输出目录中的产物清单不是本程序生成的有效清单")
        files = payload.get("files")
        if not isinstance(files, list) or not all(isinstance(name, str) for name in files):
            raise ValueError("输出产物清单的 files 必须是文件名列表")
        for name in files:
            _managed_path(output_dir, name)
        return set(files)

    # 老版本的适配拓扑记录了本程序发布的配置名，是最可靠的兼容来源。
    files: set[str] = set()
    topology_path = output_dir / "topology-adapted.xlsx"
    readme = output_dir / "README.txt"
    generated_readme = readme.is_file() and not readme.is_symlink() and readme.read_text(encoding="utf-8").startswith("GNS3 配置自适应输出")
    if topology_path.is_file() and not topology_path.is_symlink() and generated_readme:
        previous = load_topology(topology_path)
        files.update({"topology-adapted.xlsx", "README.txt"})
        for device in previous.devices:
            if device.vendor in {Vendor.CISCO_IOSXR, Vendor.JUNIPER_JUNOS}:
                name = f"configs/{_safe_output_name(device.name)}.cfg"
                _managed_path(output_dir, name)
                files.add(name)
    return files


def _archive_outputs(context: ConversionContext, output_dir: Path, files: set[str]) -> None:
    """撤下旧的可导入成果，保存在隔离目录中；用户额外文件保持原位。"""
    existing = [(name, _managed_path(output_dir, name)) for name in sorted(files)]
    existing = [(name, path) for name, path in existing if path.exists() or path.is_symlink()]
    if not existing:
        return
    archive_root = output_dir / ".previous-outputs"
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
        "previous-output-archived", "上次或未完成运行的导出产物已撤下并隔离保存",
        archive=str(archive.relative_to(output_dir)), files=[name for name, _ in existing],
    )


def _safe_config_path(config_dir: Path, configured: str | None, device_name: str) -> Path:
    """在配置目录内定位设备文件，并阻止 ``..`` 等目录穿越。"""
    root = config_dir.resolve()
    candidates: list[Path] = []
    if configured:
        candidates.append(config_dir / configured)
    else:
        candidates.extend(config_dir / f"{device_name}{suffix}" for suffix in CONFIG_EXTENSIONS)
        lowered = device_name.lower()
        if config_dir.exists():
            candidates.extend(
                item
                for item in config_dir.iterdir()
                if item.is_file() and item.suffix.lower() in CONFIG_EXTENSIONS and item.stem.lower() == lowered
            )
    # 对每个候选都先 resolve，再确认仍位于用户指定的配置目录中。
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"设备 {device_name} 的配置文件超出配置目录: {configured}")
        if resolved.is_file():
            return resolved
    wanted = configured or f"{device_name}{{{','.join(CONFIG_EXTENSIONS)}}}"
    raise FileNotFoundError(f"设备 {device_name} 找不到配置文件: {wanted}")


def _safe_output_name(device_name: str) -> str:
    """把设备名转换成安全的本地文件名。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]", "_", device_name).strip("._")
    return cleaned or "device"


def prepare_context(
    topology_path: Path,
    config_dir: Path,
    profiles_path: Path | None = None,
) -> ConversionContext:
    """加载拓扑、镜像规格和厂商文档，构造一次转换的共享上下文。"""
    topology = load_topology(topology_path)
    profiles = load_profiles(profiles_path)
    washing_policy = WashingPolicy()
    device_contexts: dict[str, DeviceContext] = {}
    warnings: list[str] = []
    errors: list[str] = []
    input_events: list[dict[str, object]] = []
    external_devices: set[str] = set()

    # 华为设备不参与配置转换，只作为拓扑对端；未知厂商属于输入错误，不能静默忽略。
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
            config_path = _safe_config_path(config_dir, device.config_file, device.name)
            text = config_path.read_text(encoding="utf-8-sig")
            normalized = normalize_input(device.vendor, text)
            document = parse_document(device.vendor.value, normalized.text)
            for removal in normalized.removed:
                input_events.append({
                    "kind": "input-normalization",
                    "message": f"设备 {device.name} 命中输入整理规则 {removal.rule_id}",
                    "device": device.name,
                    "rule_id": removal.rule_id,
                    "start_line": removal.start_line,
                    "end_line": removal.end_line,
                    "removed_lines": removal.removed_lines,
                })
            device_contexts[device.name] = DeviceContext(
                device=device,
                config_path=config_path,
                document=document,
                profile=profiles[device.vendor],
            )
        except (OSError, ValueError) as exc:
            errors.append(str(exc))

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
        topology=TopologyWorkbook(topology_path, None, "", "", {}, {}, [], []), devices={},
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
        message = (
            f"{phase}发生内部异常 "
            f"({type(exc).__name__}): {detail}"
        )
        context.errors.append(message)
        context.add_event(
            "internal-error",
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


def write_outputs(context: ConversionContext, output_dir: Path) -> None:
    """写出报告、接口映射、设备配置和修改后的拓扑。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    previous_files = _published_files(output_dir)
    mapping_payload = [mapping.to_dict() for mapping in context.mappings]

    combined_errors = context.errors + [
        message for device in context.devices.values() for message in device.errors
    ]
    combined_warnings = context.warnings + [
        message for device in context.devices.values() for message in device.warnings
    ]
    # 冲突既保留在完整事件流中，也单独汇总，方便自动化审计。
    group_conflicts = [
        {key: value for key, value in event.items() if key not in {"kind", "message"}}
        for event in context.events
        if event.get("kind") == "group-conflict"
    ]
    simulation_adaptation_count = sum(
        int(event.get("change_count", 0))
        for event in context.events
        if event.get("kind") == "simulation-adaptation"
    )
    group_identity_ambiguity_count = sum(
        event.get("kind") == "group-identity-ambiguous"
        for event in context.events
    )
    group_identity_rule_hits = sum(
        int(event.get("matched", 0))
        for event in context.events
        if event.get("kind") == "group-identity-coverage"
    )
    group_identity_fallbacks = sum(
        int(event.get("fallback", 0))
        for event in context.events
        if event.get("kind") == "group-identity-coverage"
    )
    group_identity_total = group_identity_rule_hits + group_identity_fallbacks
    cleaning_rule_hits_by_category: dict[str, int] = {}
    for event in context.events:
        if event.get("kind") != "cleaning-rule":
            continue
        category = str(event.get("category", "uncategorized"))
        cleaning_rule_hits_by_category[category] = (
            cleaning_rule_hits_by_category.get(category, 0)
            + int(event.get("hits", 0))
        )
    report = {
        "status": "failed" if context.has_errors else "success",
        "errors": list(dict.fromkeys(combined_errors)),
        "warnings": list(dict.fromkeys(combined_warnings)),
        "events": context.events,
        "group_conflicts": group_conflicts,
        "summary": {
            "converted_devices": len(context.devices) if not context.has_errors else 0,
            "active_links": sum(1 for link in context.topology.links if link.active),
            "skipped_links": sum(1 for link in context.topology.links if not link.active),
            "mapping_count": len(mapping_payload),
            "group_conflict_count": len(group_conflicts),
            "group_identity_ambiguity_count": group_identity_ambiguity_count,
            "group_identity_rule_hits": group_identity_rule_hits,
            "group_identity_fallbacks": group_identity_fallbacks,
            "group_identity_coverage": (
                round(group_identity_rule_hits / group_identity_total, 4)
                if group_identity_total
                else None
            ),
            "simulation_adaptation_count": simulation_adaptation_count,
            "cleaning_rule_hits_by_category": cleaning_rule_hits_by_category,
        },
    }
    def write_diagnostics() -> None:
        for filename, payload in (("interface-mapping.json", mapping_payload), ("report.json", report)):
            _write_text(output_dir, filename, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    if context.has_errors:
        _archive_outputs(context, output_dir, previous_files)
        write_diagnostics()
        return

    # 先在内存中渲染全部配置；任一文档违反内部不变量时，不创建
    # 本次运行的部分 configs 输出。
    rendered_configs = {
        device_name: device.document.render()
        for device_name, device in context.devices.items()
    }
    published = {"topology-adapted.xlsx", "README.txt"} | {
        f"configs/{_safe_output_name(name)}.cfg" for name in rendered_configs
    }
    for name in published:
        _managed_path(output_dir, name)
    _archive_outputs(context, output_dir, previous_files)
    # 先记录将要发布的路径：即使随后写盘失败，失败分支仍可撤下部分产物。
    _write_text(
        output_dir, _OUTPUT_MANIFEST,
        json.dumps({"generator": "gns3-config-adaptor", "files": sorted(published)}, ensure_ascii=False) + "\n",
    )
    configs_dir = output_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    for device_name, rendered in rendered_configs.items():
        _write_text(output_dir, f"configs/{_safe_output_name(device_name)}.cfg", rendered)

    adapted_config_files = {
        device_name: f"configs/{_safe_output_name(device_name)}.cfg"
        for device_name in context.devices
    }
    topology_temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=output_dir, delete=False) as handle:
            topology_temporary = Path(handle.name)
        write_adapted_topology(context.topology, topology_temporary, config_files=adapted_config_files)
        topology_temporary.replace(_output_path(output_dir, "topology-adapted.xlsx"))
    finally:
        if topology_temporary is not None:
            topology_temporary.unlink(missing_ok=True)
    readme = f"""GNS3 配置自适应输出

实验登录凭据（仅限隔离实验环境）：
  IOS XR 用户名：{LAB_USERNAME}
  Junos 用户名：root
  密码：{LAB_PASSWORD}

文件说明：
  topology-adapted.xlsx     已更新接口并移除冗余聚合成员的拓扑
  configs/                  XRv9000 和 vMX 配置
  interface-mapping.json    全局接口映射
  report.json               转换报告（含模拟参数适配明细）
"""
    _write_text(output_dir, "README.txt", readme)
    write_diagnostics()
