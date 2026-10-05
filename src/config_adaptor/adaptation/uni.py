"""UNI 候选识别、VLAN 分配和 QinQ 汇聚。"""

from __future__ import annotations

from collections.abc import Iterable

from ..common.interface import InterfaceKind, InterfaceSpec, interface_parent
from .models import ConversionContext, DeviceContext, InterfaceMapping, Vendor


class VlanSpaceExhausted(ValueError):
    """设备的可用 UNI VLAN 空间已经耗尽。"""


MIN_VLAN = 2
MAX_VLAN = 4094


def allocate_uni_vlans(interfaces: Iterable[InterfaceSpec]) -> dict[str, int]:
    """优先保留合法源 VLAN，冲突时分配最小空闲 VLAN。"""
    used: set[int] = set()
    result: dict[str, int] = {}
    for interface in interfaces:
        requested = interface.vlan
        if (
            requested is not None
            and MIN_VLAN <= requested <= MAX_VLAN
            and requested not in used
        ):
            vlan = requested
        else:
            vlan = next(
                (
                    candidate
                    for candidate in range(MIN_VLAN, MAX_VLAN + 1)
                    if candidate not in used
                ),
                None,
            )
        if vlan is None:
            raise VlanSpaceExhausted("UNI 数量超过 VLAN 可用空间")
        used.add(vlan)
        result[interface.name] = vlan
    return result
class UNIHandler:
    """把剩余的有效 UNI 业务汇聚到最后一个接口的 QinQ 子接口。

    NNI 阶段完成后，未被 NNI 占用且确有业务绑定的物理口、聚合口和网关
    接口才是 UNI 源。每个业务逻辑单元获得唯一外层运输 VLAN，原业务 VLAN
    尽量作为内层标签保留，最终全部挂到 Profile 指定的 UNI 父接口下。
    """

    def process(self, context: ConversionContext) -> None:
        """按设备名依次执行 UNI 业务筛选、VLAN 分配和配置迁移。

        每台设备独立处理，使候选接口、聚合成员和镜像 Profile 始终使用同一设备
        上下文；按设备名排序则保证映射、告警和事件的输出顺序可重复，便于比较转换
        结果及排查问题。具体设备处理保持在独立方法中，以便 VLAN 耗尽等错误能够在
        修改该设备业务树之前终止。
        """
        for device_name in sorted(context.devices):
            self._process_device(context, device_name)

    def _process_device(
        self,
        context: ConversionContext,
        device_name: str,
    ) -> None:
        """处理一台设备的 UNI，并在 VLAN 耗尽时记录错误后停止迁移。

        方法会先清理范围内的无业务接口，再为有效业务分配 VLAN；只有分配
        成功才会修改这些业务接口的配置树。
        """
        device = context.devices[device_name]
        # candidates 用于找出应清理的裸口；sources 是真正需要迁移的业务口。
        # 两者必须分开，否则“没有业务”与“不属于 UNI 范围”会被混为一谈。
        candidates, sources, member_map = self._select_sources(device)
        self._remove_bare_interfaces(
            device_name,
            device,
            candidates,
            sources,
            member_map,
        )
        if not sources:
            return

        self._warn_ambiguous_gateway_vlans(context, device_name, device, sources)
        try:
            # 分配器按 sources 的稳定顺序优先保留原 VLAN，重复、缺失或越界
            # 时改用最小可用 VLAN；失败时尚未开始迁移配置树。
            vlan_plan = allocate_uni_vlans(sources)
        except VlanSpaceExhausted as exc:
            message = f"设备 {device_name} 的 {exc}"
            device.errors.append(message)
            context.errors.append(message)
            return
        self._migrate_sources(device_name, device, sources, member_map, vlan_plan)

    @staticmethod
    def _select_sources(
        device: DeviceContext,
    ) -> tuple[list[InterfaceSpec], list[InterfaceSpec], dict[str, str]]:
        """筛选 UNI 范围内接口及其中真正承载业务的迁移源。

        返回 ``(candidates, sources, member_map)``：候选接口用于裸口清理，
        业务源用于迁移，成员映射用于随聚合父口清理物理成员。
        """
        document = device.document
        specs = document.interface_specs()
        # NNI 映射中的源口和目标口都必须排除，防止再次被当作 UNI 迁移。
        # 排除目标口同样重要：NNI 阶段已将配置写到镜像接口，UNI 阶段若再次
        # 选择它，会把刚生成的 NNI 配置迁到 UNI 汇聚口。
        nni_interfaces = {
            interface_parent(value)
            for mapping in device.mappings
            if mapping.role == "NNI"
            for value in (mapping.source_interface, mapping.target_interface)
            if value
        }
        member_map = document.bundle_members()
        # business_interface_names 由厂商 AST 根据 IP、二层绑定及相关业务引用
        # 计算；不能简单地把所有配置中出现的接口都视为活跃 UNI。
        business_names = document.business_interface_names()
        # 聚合物理成员由聚合父口统一处理，不能作为独立 UNI 候选。
        candidates = [
            spec
            for spec in specs
            if spec.kind
            in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE, InterfaceKind.GATEWAY}
            and spec.parent not in nni_interfaces
            and spec.parent != device.profile.uni_parent
            and spec.parent not in member_map
        ]
        sources = [spec for spec in candidates if spec.name in business_names]
        return candidates, sources, member_map

    @staticmethod
    def _remove_bare_interfaces(
        device_name: str,
        device: DeviceContext,
        candidates: list[InterfaceSpec],
        sources: list[InterfaceSpec],
        member_map: dict[str, str],
    ) -> None:
        """删除 UNI 范围内未承载业务的接口并写入审计映射。

        对仍有活跃业务的父口只记录未使用 unit；整个父口无业务时删除父口、
        子接口及其聚合成员。未知接口和控制类虚拟接口不在候选范围内。
        """
        source_names = {spec.name for spec in sources}
        source_parents = {spec.parent for spec in sources}
        # 活跃父口下可能同时存在未被业务引用的 unit；父口会迁移，
        # 但这些 unit 不迁移，因此先单独留下删除审计记录。
        for spec in candidates:
            if spec.name in source_names or spec.parent not in source_parents:
                continue
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=spec.name,
                    role="UNI",
                    action="remove-bare",
                    target_interface=None,
                    reason="同一父接口下未关联活跃业务的逻辑单元",
                )
            )

        # 整个父口都没有业务时，父口及其聚合物理成员可一并删除。注意这里
        # 只处理 candidates，未知接口和虚拟控制接口不会被误删。
        bare_parents = sorted({spec.parent for spec in candidates} - source_parents)
        for parent in bare_parents:
            device.document.remove_interface(parent, include_children=True)
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=parent,
                    role="UNI",
                    action="remove-bare",
                    target_interface=None,
                    reason="无 IP、无 L2 绑定且无业务引用的裸口",
                )
            )
        UNIHandler._remove_bundle_members(
            device_name,
            device,
            bare_parents,
            member_map,
            inactive=True,
        )

    @staticmethod
    def _remove_bundle_members(
        device_name: str,
        device: DeviceContext,
        bundles: list[str],
        member_map: dict[str, str],
        *,
        inactive: bool,
    ) -> None:
        """删除指定 UNI 聚合口的物理成员，并记录删除原因。

        ``inactive=True`` 表示整个聚合无业务；否则表示业务已从聚合口迁移，
        物理成员因扁平化而删除。
        """
        bundle_names = set(bundles)
        for member, bundle in sorted(member_map.items()):
            if bundle not in bundle_names:
                continue
            device.document.remove_interface(member, include_children=True)
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=member,
                    role="UNI",
                    action="remove-bare" if inactive else "remove",
                    target_interface=None,
                    reason=(
                        f"无业务聚合 {bundle} 的物理成员"
                        if inactive
                        else f"UNI 聚合 {bundle} 扁平化，删除物理成员"
                    ),
                )
            )

    @staticmethod
    def _warn_ambiguous_gateway_vlans(
        context: ConversionContext,
        device_name: str,
        device: DeviceContext,
        sources: list[InterfaceSpec],
    ) -> None:
        """为无法确定接入 VLAN 的 IOS XR 网关接口生成告警。

        BVI 编号不等同于业务 VLAN，因此此处只说明后续将分配运输 VLAN，
        不修改接口配置；Junos 设备无需执行该检查。
        """
        if device.device.vendor != Vendor.CISCO_IOSXR:
            return
        for spec in sources:
            if (
                spec.kind != InterfaceKind.GATEWAY
                or spec.vlan is not None
                or spec.inner_vlan is not None
            ):
                continue
            message = (
                f"设备 {device_name} 的网关接口 {spec.name} 未找到唯一明确的"
                "接入口 VLAN；不使用 BVI 编号，将自动分配运输 VLAN"
            )
            device.warnings.append(message)
            context.add_event(
                "gateway-vlan-warning",
                message,
                device=device_name,
                interface=spec.name,
                action="allocate-transport-vlan",
            )

    @staticmethod
    def _migrate_sources(
        device_name: str,
        device: DeviceContext,
        sources: list[InterfaceSpec],
        member_map: dict[str, str],
        vlan_plan: dict[str, int],
    ) -> None:
        """把一台设备的有效 UNI 业务迁移到 Profile 指定的汇聚父口。

        先将源接口树改为占位名并移除聚合成员，再生成 QinQ 子接口、清理
        占位树，最后确保目标物理父接口存在。使用占位名可隔离源口与最终目标口，
        避免多个 unit 逐个迁移时发生名称碰撞或过早删除同一父口下尚未处理的业务；
        最后统一清理也让厂商实现可以安全复制所需配置。
        """
        source_parents = list(dict.fromkeys(spec.parent for spec in sources))
        # 先改到不会与目标 UNI 冲突的占位名，再逐个拆成 QinQ 子接口。
        # 同一父口下可能有多个 unit；父口只暂存一次，各 unit 随接口树一起
        # 移动，之后再按各自的 vlan_plan 分别生成目标子接口。
        placeholders = {
            parent: f"ADAPT-UNI-{index}"
            for index, parent in enumerate(source_parents)
        }
        UNIHandler._remove_bundle_members(
            device_name,
            device,
            source_parents,
            member_map,
            inactive=False,
        )
        for parent in source_parents:
            device.document.rename_interface_tree(
                parent,
                placeholders[parent],
                strip_bundle=True,
            )
        UNIHandler._map_sources(device_name, device, sources, placeholders, vlan_plan)
        # map_uni 已把活跃业务复制/改写到最终 UNI 子接口。此时清除占位树，
        # 并补记只有子接口参与迁移时的父口折叠映射。
        UNIHandler._finalize_sources(device_name, device, sources, placeholders)
        # 即使所有业务都落在子接口，目标配置仍需显式存在 UNI 物理父口。
        device.document.ensure_parent_interface(device.profile.uni_parent)

    @staticmethod
    def _map_sources(
        device_name: str,
        device: DeviceContext,
        sources: list[InterfaceSpec],
        placeholders: dict[str, str],
        vlan_plan: dict[str, int],
    ) -> None:
        """逐业务单元生成目标 QinQ 子接口并记录源到目标的映射。

        ``vlan_plan`` 提供唯一外层运输 VLAN；内层优先使用原 inner VLAN，
        其次使用合法原 VLAN，否则回退到新运输 VLAN。
        """
        for spec in sources:
            staged_name = placeholders[spec.parent]
            if spec.unit is not None:
                staged_name += f".{spec.unit}"
            vlan = vlan_plan[spec.name]
            # 外层 VLAN 是新的运输标签；内层优先保留原业务 VLAN。
            # 原标签缺失或非法时，使用新分配的 VLAN 保证生成配置可用。
            inner_vlan = spec.inner_vlan or (
                spec.vlan
                if spec.vlan is not None and 1 <= spec.vlan <= 4094
                else vlan
            )
            target = device.document.map_uni(
                staged_name,
                device.profile.uni_parent,
                vlan,
                inner_vlan,
            )
            action = "map" if spec.vlan == vlan else "re-vlan"
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=spec.name,
                    role="UNI",
                    action=action,
                    target_interface=target,
                    reason=(
                        "保留原 VLAN"
                        if action == "map"
                        else "无 VLAN 或 VLAN 冲突，自动分配"
                    ),
                )
            )

    @staticmethod
    def _finalize_sources(
        device_name: str,
        device: DeviceContext,
        sources: list[InterfaceSpec],
        placeholders: dict[str, str],
    ) -> None:
        """清理 UNI 临时占位接口，并补记父接口折叠关系。

        当实际迁移对象只有父口下的 unit 时，额外把原父口映射到统一 UNI
        父口，供后续非接口配置引用改写使用。
        """
        source_names = {spec.name for spec in sources}
        for parent, placeholder in placeholders.items():
            device.document.finalize_uni_source(placeholder)
            if parent in source_names:
                continue
            device.mappings.append(
                InterfaceMapping(
                    device=device_name,
                    source_interface=parent,
                    role="UNI",
                    action="collapse-parent",
                    target_interface=device.profile.uni_parent,
                    reason="UNI 父接口汇聚",
                )
            )
