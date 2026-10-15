"""组织输入加载、责任链执行、扩展规则和最终文件输出。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from ..common.lab_account import LAB_PASSWORD, LAB_USERNAME
from .factory import parse_document
from .input_normalization import normalize_input
from .interfaces import summarize_physical_ports
from .models import (
    ConversionContext,
    Device,
    DeviceAdaptationResult,
    DeviceContext,
    DeviceLink,
    ImageProfile,
    Link,
    TopologyWorkbook,
    Vendor,
)
from .pipeline import build_default_pipeline
from .profiles import load_profiles
from .topology import load_topology, write_adapted_topology
from ..common.policies import WashingPolicy


CONFIG_EXTENSIONS = (".cfg", ".conf", ".txt")


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


def _load_device(
    device: Device,
    config_path: Path,
    text: str,
    profiles: dict[Vendor, ImageProfile],
    input_events: list[dict[str, object]],
) -> DeviceContext:
    """整理原始采集文本并解析成厂商文档；命中的输入整理规则写入 input_events。"""
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
    return DeviceContext(
        device=device,
        config_path=config_path,
        document=document,
        profile=profiles[device.vendor],
    )


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

    # 华为首版跳过；未知厂商属于输入错误，不能静默忽略。
    for device in topology.devices:
        if device.vendor == Vendor.HUAWEI:
            warnings.append(f"设备 {device.name} 为华为设备，首版跳过")
            continue
        if device.vendor == Vendor.UNKNOWN:
            errors.append(f"设备 {device.name} 的厂商无法识别")
            continue
        try:
            config_path = _safe_config_path(config_dir, device.config_file, device.name)
            text = config_path.read_text(encoding="utf-8-sig")
            device_contexts[device.name] = _load_device(
                device, config_path, text, profiles, input_events
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
    context = prepare_context(topology_path, config_dir, profiles_path)
    phase = "核心转换流水线"
    try:
        if not context.errors:
            phase = "清洗规则加载"
            pipeline = build_default_pipeline()
            phase = "核心转换流水线"
            # 清洗规则在接口迁移和引用改写之后执行，不改变 NNI/UNI 分类依据。
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


def _single_device_context(
    device_name: str,
    vendor: str,
    config_text: str,
    links: Sequence[DeviceLink],
    profiles_path: Path | None,
) -> ConversionContext:
    """把单台设备及其链路构造成与全量转换等价的共享上下文。

    链路按输入顺序编号（从 2 开始，对应 Excel 首个数据行），NNI 目标口按此顺序
    分配。对端只作为拓扑端点存在：受支持厂商放入 ``external_devices``，华为/未知
    厂商不放入，由预检阶段按全量规则跳过。
    """
    local = Device(name=device_name, vendor=Vendor.parse(vendor), config_file=None, row=2)
    devices: dict[str, Device] = {device_name: local}
    topology_links: list[Link] = []
    errors: list[str] = []
    for index, item in enumerate(links):
        if item.peer_device == device_name:
            errors.append(f"链路 {index + 1} 的对端设备不能是本设备: {device_name}")
            continue
        peer_vendor = Vendor.parse(item.peer_vendor)
        known = devices.get(item.peer_device)
        if known is None:
            devices[item.peer_device] = Device(
                name=item.peer_device,
                vendor=peer_vendor,
                config_file=None,
                row=len(devices) + 2,
            )
        elif known.vendor != peer_vendor:
            errors.append(f"对端设备 {item.peer_device} 的厂商在不同链路中不一致")
        topology_links.append(
            Link(
                row=index + 2,
                a_device=device_name,
                a_interface=item.local_interface,
                z_device=item.peer_device,
                z_interface=item.peer_interface,
            )
        )
    topology = TopologyWorkbook(
        path=Path(f"<{device_name}>"),
        workbook=None,
        device_sheet="",
        link_sheet="",
        device_headers={},
        link_headers={},
        devices=list(devices.values()),
        links=topology_links,
    )
    context = ConversionContext(
        topology=topology,
        devices={},
        errors=errors,
        external_devices={
            name
            for name, device in devices.items()
            if name != device_name
            and device.vendor in {Vendor.CISCO_IOSXR, Vendor.JUNIPER_JUNOS}
        },
    )
    if local.vendor not in {Vendor.CISCO_IOSXR, Vendor.JUNIPER_JUNOS}:
        context.errors.append(f"设备 {device_name} 的厂商不在支持范围: {vendor}")
        return context
    try:
        profiles = load_profiles(profiles_path)
        context.devices[device_name] = _load_device(
            local,
            Path(f"<{device_name}>"),
            config_text.removeprefix("\ufeff"),
            profiles,
            context.events,
        )
    except (OSError, ValueError) as exc:
        context.errors.append(str(exc))
    return context


def adapt_device(
    device_name: str,
    vendor: str,
    config_text: str,
    links: Sequence[DeviceLink] = (),
    profiles_path: Path | None = None,
) -> DeviceAdaptationResult:
    """对单台设备执行完整自适应，结果只在内存中返回，不写任何文件。

    ``links`` 是该设备参与的全部 NNI，顺序决定目标口分配顺序；链路两端各自
    调用本函数即可得到各自的目标接口，与全量 ``convert`` 的结果一致。可预期的
    业务失败写入 ``errors``；内部异常记录 ``internal-error`` 事件后继续抛出。
    """
    context = _single_device_context(device_name, vendor, config_text, links, profiles_path)
    phase = "核心转换流水线"
    config: str | None = None
    try:
        if not context.has_errors:
            phase = "清洗规则加载"
            pipeline = build_default_pipeline()
            phase = "核心转换流水线"
            pipeline.execute(context)
        if not context.has_errors:
            phase = "输出结果"
            config = context.devices[device_name].document.render()
    except Exception as exc:
        detail = str(exc).strip() or "异常未提供详细信息"
        message = f"{phase}发生内部异常 ({type(exc).__name__}): {detail}"
        context.errors.append(message)
        context.add_event(
            "internal-error",
            message,
            phase=phase,
            exception_type=type(exc).__name__,
        )
        raise
    report = build_report(context)
    physical, unused_sources, unused_targets = (
        summarize_physical_ports(context.devices[device_name])
        if config is not None
        else ([], [], [])
    )
    return DeviceAdaptationResult(
        status=str(report["status"]),
        config=config,
        mappings=context.mappings,
        links=[
            {
                "source_interface": links[link.row - 2].local_interface,
                "target_interface": link.a_interface if link.active else None,
                "peer_device": link.z_device,
                "peer_interface": link.z_interface,
                "active": link.active,
                "skip_reason": link.skip_reason,
            }
            for link in context.topology.links
        ],
        warnings=list(report["warnings"]),
        errors=list(report["errors"]),
        events=context.events,
        summary=dict(report["summary"]),
        physical_mappings=physical,
        unused_source_interfaces=unused_sources,
        unused_target_interfaces=unused_targets,
    )


def build_report(context: ConversionContext) -> dict[str, object]:
    """汇总错误、警告、事件和统计信息，生成 report.json 的内容。"""
    mapping_count = len(context.mappings)
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
            "mapping_count": mapping_count,
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
    return report


def write_outputs(context: ConversionContext, output_dir: Path) -> None:
    """写出报告、接口映射、设备配置和修改后的拓扑。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    mapping_payload = [mapping.to_dict() for mapping in context.mappings]
    (output_dir / "interface-mapping.json").write_text(
        json.dumps(mapping_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = build_report(context)
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 失败时只输出诊断文件，避免用户误用不完整配置。
    if context.has_errors:
        return

    # 先在内存中渲染全部配置；任一文档违反内部不变量时，不创建
    # 本次运行的部分 configs 输出。
    rendered_configs = {
        device_name: device.document.render()
        for device_name, device in context.devices.items()
    }
    configs_dir = output_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    for device_name, rendered in rendered_configs.items():
        output = configs_dir / f"{_safe_output_name(device_name)}.cfg"
        output.write_text(rendered, encoding="utf-8")

    adapted_config_files = {
        device_name: f"configs/{_safe_output_name(device_name)}.cfg"
        for device_name in context.devices
    }
    write_adapted_topology(
        context.topology,
        output_dir / "topology-adapted.xlsx",
        config_files=adapted_config_files,
    )
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
    (output_dir / "README.txt").write_text(readme, encoding="utf-8")
