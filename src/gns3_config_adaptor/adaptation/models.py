"""转换流程使用的领域模型和共享上下文。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..impl.common.domain.interface import UniInterfacePlan
from ..impl.common.domain.outcomes import DeferredInterfaceReference
from ..impl.common.domain.policies import (
    SimulationAdaptationPolicy,
    WashingPolicy,
)
from .performance import ConversionPerformance

if TYPE_CHECKING:
    from ..impl.common.domain.contracts import VendorConfiguration
    from .external_aggregation import ExternalAggregation

RESERVED_NNI_INTERFACE_COUNT = 3
MIN_PROFILE_INTERFACE_COUNT = RESERVED_NNI_INTERFACE_COUNT + 1


class Vendor(StrEnum):
    """定义转换流程识别的设备厂商及其稳定序列化值。

    枚举值同时作为解析器和 profile 键，避免不同模块使用不一致的厂商字符串。
    """

    CISCO_IOSXR = "cisco_iosxr"
    JUNIPER_JUNOS = "juniper_junos"
    HUAWEI = "huawei"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str) -> Vendor:
        """把 Excel 中的中英文厂商名称归一化为枚举值。

        接受常见缩写可容忍人工表格差异，未知名称返回 ``UNKNOWN`` 以保留诊断机会。
        """
        normalized = "".join(value.lower().split()).replace("-", "_")
        if normalized in {
            "cisco",
            "思科",
            "iosxr",
            "ios_xr",
            "ciscoiosxr",
            "cisco_iosxr",
        }:
            return cls.CISCO_IOSXR
        if normalized in {
            "juniper",
            "瞻博",
            "junos",
            "vmx",
            "juniperjunos",
            "juniper_junos",
        }:
            return cls.JUNIPER_JUNOS
        if normalized in {"huawei", "华为"}:
            return cls.HUAWEI
        return cls.UNKNOWN


class MappingRole(StrEnum):
    """定义接口映射在拓扑中的业务角色。

    该领域值用于阶段输入或报告输出，保持统一定义可避免调用方各自解释相同状态。
    """

    NNI = "NNI"
    UNI = "UNI"
    NON_NNI = "NON_NNI"


class MappingAction(StrEnum):
    """定义接口映射允许的稳定动作及其序列化值。

    该领域值用于阶段输入或报告输出，保持统一定义可避免调用方各自解释相同状态。
    """

    MAP = "map"
    MAP_MEMBER = "map-member"
    PRESERVE = "preserve"
    SKIP = "skip"
    REMOVE = "remove"
    REMOVE_BARE = "remove-bare"


class EventKind(StrEnum):
    """定义转换报告中的稳定事件分类。

    该领域值用于阶段输入或报告输出，保持统一定义可避免调用方各自解释相同状态。
    """

    BUNDLE_ADAPTATION = "bundle-adaptation"
    BUNDLE_ADAPTATION_WARNING = "bundle-adaptation-warning"
    CLEANING_RULE = "cleaning-rule"
    CLEANING_RULE_ERROR = "cleaning-rule-error"
    GROUP_CONFLICT = "group-conflict"
    GROUP_EXPANSION = "group-expansion"
    GROUP_IDENTITY_AMBIGUOUS = "group-identity-ambiguous"
    GROUP_IDENTITY_COVERAGE = "group-identity-coverage"
    INPUT_NORMALIZATION = "input-normalization"
    INTERFACE_CLASSIFICATION_WARNING = "interface-classification-warning"
    INTERFACE_COMPATIBILITY = "interface-compatibility"
    INTERFACE_COMPATIBILITY_ERROR = "interface-compatibility-error"
    INTERFACE_COMPATIBILITY_WARNING = "interface-compatibility-warning"
    INTERFACE_VALIDATION = "interface-validation"
    INTERFACE_VALIDATION_ERROR = "interface-validation-error"
    INTERFACE_VALIDATION_WARNING = "interface-validation-warning"
    INTERNAL_ERROR = "internal-error"
    NON_NNI_CLEANUP = "non-nni-cleanup"
    NON_NNI_CLEANUP_ERROR = "non-nni-cleanup-error"
    NON_NNI_CLEANUP_WARNING = "non-nni-cleanup-warning"
    PRECONFIGURED_INTERFACE_CLEANUP = "preconfigured-interface-cleanup"
    PREVIOUS_OUTPUT_ARCHIVED = "previous-output-archived"
    REDUCE_MEMBER_LINK = "reduce-member-link"
    REDUNDANT_MEMBER_CLEANUP = "redundant-member-cleanup"
    REDUNDANT_MEMBER_CLEANUP_ERROR = "redundant-member-cleanup-error"
    REDUNDANT_MEMBER_CLEANUP_WARNING = "redundant-member-cleanup-warning"
    SIMULATION_ADAPTATION = "simulation-adaptation"
    SKIP_LINK = "skip-link"
    TOPOLOGY_PREFLIGHT = "topology-preflight"
    UNI_MIGRATION = "uni-migration"
    UNI_MIGRATION_ERROR = "uni-migration-error"
    UNI_MIGRATION_WARNING = "uni-migration-warning"


@dataclass(slots=True)
class Device:
    """保存设备列表中的设备身份和配置来源。

    设备名作为后续映射主键，配置文件名与行号则用于定位输入和报告问题。
    """

    name: str
    vendor: Vendor
    config_file: str | None
    row: int


@dataclass(slots=True)
class Link:
    """保存链接表中的一条物理 NNI 及两端接口。

    ``active`` 允许预检和聚合缩减在不删除原始行信息的情况下停止下游消费。
    """

    row: int
    link_name: str
    a_device: str
    a_interface: str
    z_device: str
    z_interface: str
    active: bool = True
    skip_reason: str | None = None

    def endpoints(self) -> tuple[tuple[str, str], tuple[str, str]]:
        """以固定顺序返回 A、Z 两端的设备名和接口名。

        固定顺序让分配、校验和报告遍历相同链路时产生稳定结果。
        """
        return (
            (self.a_device, self.a_interface),
            (self.z_device, self.z_interface),
        )

    def set_interface_for(self, device_name: str, interface: str) -> None:
        """把指定设备端点的接口改写为目标接口。

        只修改匹配端点可让同一链路两端分别按各自镜像分配，而不影响另一端。
        """
        if self.a_device == device_name:
            self.a_interface = interface
        elif self.z_device == device_name:
            self.z_interface = interface


@dataclass(slots=True)
class InterfaceMapping:
    """保存一条源接口到目标接口的转换映射。

    设备、动作和原因共同构成审计事实，序列化后可供报告和外部工具复核。
    """

    device: str
    source_interface: str
    role: MappingRole
    action: MappingAction
    target_interface: str | None
    link_rows: list[int] = field(default_factory=list)
    reason: str = ""
    source_vlan: int | None = None
    target_vlan: int | None = None
    inner_vlan: int | None = None

    def __post_init__(self) -> None:
        """校验并规范化接口映射角色和动作。

        该领域值用于阶段输入或报告输出，保持统一定义可避免调用方各自解释相同状态。
        """
        try:
            self.role = MappingRole(self.role)
        except ValueError as exc:
            raise ValueError(f"未知接口映射 role: {self.role}") from exc
        try:
            self.action = MappingAction(self.action)
        except ValueError as exc:
            raise ValueError(f"未知接口映射 action: {self.action}") from exc

    def to_dict(self) -> dict[str, Any]:
        """把映射转换为可直接序列化为 JSON 的字典。

        统一从数据模型导出，避免报告层手工枚举字段而遗漏后续新增信息。
        """
        payload = asdict(self)
        payload["role"] = self.role.value
        payload["action"] = self.action.value
        if self.role != MappingRole.UNI:
            for key in ("source_vlan", "target_vlan", "inner_vlan"):
                payload.pop(key)
        return payload


@dataclass(slots=True)
class ImageProfile:
    """描述目标镜像顺序暴露的接口和模拟适配参数。

    接口顺序是 NNI 稳定分配的依据，策略则限定镜像上允许执行的兼容调整。
    """

    vendor: Vendor
    interfaces: list[str]
    image: str
    version: str | None = None
    simulation_adaptation: SimulationAdaptationPolicy = field(
        default_factory=SimulationAdaptationPolicy
    )

    @property
    def nni_interfaces(self) -> list[str]:
        """返回可用于 NNI 分配的接口前缀。

        最后三个接口不分配给 NNI，其中最后一个供共享 UNI，其余两个预留。
        """
        return self.interfaces[:-RESERVED_NNI_INTERFACE_COUNT]

    @property
    def uni_parent(self) -> str:
        """返回 UNI 共享物理口，固定使用最后一个数据口。

        另外两个末尾端口继续预留，NNI 的容量不因恢复 UNI 而改变。
        """
        if len(self.interfaces) < MIN_PROFILE_INTERFACE_COUNT:
            raise ValueError("镜像接口列表至少需要四个接口")
        return self.interfaces[-1]


@dataclass(slots=True)
class DeviceContext:
    """保存单台设备在转换期间的文档、映射和诊断状态。

    所有阶段共享同一上下文对象，使接口删除和映射结果能够跨阶段累积。
    """

    device: Device
    config_path: Path
    document: VendorConfiguration
    profile: ImageProfile
    mappings: list[InterfaceMapping] = field(default_factory=list)
    removed_interfaces: set[str] = field(default_factory=set)
    deferred_interface_references: set[DeferredInterfaceReference] = field(
        default_factory=set
    )
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    uni_plan: list[UniInterfacePlan] = field(default_factory=list)

    @property
    def replacement_map(self) -> dict[str, list[str]]:
        """汇总物理接口改名关系，排除保留的逻辑聚合自映射。

        普通接口与选中成员映射到目标物理口；Bundle/ae 保持原名，
        不参与全局引用替换。历史一对多记录仍按生成顺序去重。
        """
        result: dict[str, list[str]] = {}
        for item in self.mappings:
            if not item.target_interface or item.action in {
                MappingAction.REMOVE,
                MappingAction.REMOVE_BARE,
                MappingAction.SKIP,
                MappingAction.PRESERVE,
            }:
                continue
            targets = result.setdefault(item.source_interface, [])
            if item.target_interface not in targets:
                targets.append(item.target_interface)
        return result


@dataclass(slots=True)
class TopologyWorkbook:
    """保存原始 Excel 工作簿、列位置和解析后的拓扑数据。

    同时保留工作簿和字段索引，使转换结束后可以基于原文件安全写出适配副本。
    """

    path: Path
    workbook: Any
    device_sheet: str
    link_sheet: str
    device_headers: dict[str, int]
    link_headers: dict[str, int]
    devices: list[Device]
    links: list[Link]


@dataclass(slots=True)
class ConversionContext:
    """保存一次转换流水线共享的拓扑、设备状态和诊断事件。

    单一上下文让阶段按顺序累积结果，同时通过错误集合统一决定是否短路后续处理。
    """

    topology: TopologyWorkbook
    devices: dict[str, DeviceContext]
    washing_policy: WashingPolicy = field(default_factory=WashingPolicy)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    # 不参与配置自适应的拓扑端点；可以只读提取配置中的聚合归属。
    external_devices: set[str] = field(default_factory=set)
    # 保留的业务校验策略；不会启用已移除的源配置语义审计。
    strict_input_validation: bool = False
    performance: ConversionPerformance = field(
        default_factory=ConversionPerformance, repr=False, compare=False
    )
    external_aggregations: dict[str, ExternalAggregation] = field(
        default_factory=dict
    )

    def add_event(
        self, kind: EventKind | str, message: str, **details: Any
    ) -> None:
        """追加一条带类型和附加上下文的结构化事件。

        事件只保存可审计元数据，禁止传入认证秘密，以免报告泄漏生产凭据。
        """
        event = {
            "kind": kind.value if isinstance(kind, EventKind) else kind,
            "message": message,
        }
        event.update(details)
        self.events.append(event)

    @property
    def mappings(self) -> list[InterfaceMapping]:
        """按设备名排序汇总全部接口映射。

        稳定排序让相同输入生成相同报告顺序，便于比较和自动化审计。
        """
        result: list[InterfaceMapping] = []
        for name in sorted(self.devices):
            result.extend(self.devices[name].mappings)
        return result

    @property
    def has_errors(self) -> bool:
        """报告全局或任一设备是否已产生错误。

        全局失败条件集中在一个属性中，避免各阶段自行遗漏设备级错误。
        """
        return bool(
            self.errors
            or any(device.errors for device in self.devices.values())
        )
