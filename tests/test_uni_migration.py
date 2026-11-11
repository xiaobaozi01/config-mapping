"""共享 UNI 的业务保留、封装及类型兼容回归。"""

from dataclasses import replace
from pathlib import Path

import pytest

from gns3_config_adaptor.adaptation.factory import parse_document
from gns3_config_adaptor.adaptation.models import (
    MappingRole,
)
from gns3_config_adaptor.adaptation.pipeline import build_default_pipeline
from gns3_config_adaptor.adaptation.service import convert, prepare_context
from gns3_config_adaptor.adaptation.uni import allocate_uni_vlans
from gns3_config_adaptor.impl.common.domain.interface import (
    InterfaceKind,
    InterfaceSpec,
)
from gns3_config_adaptor.impl.common.domain.policies import (
    SimulationAdaptationPolicy,
)

FIXTURES = Path(__file__).parent / "fixtures"


def uni_context(vendor, source=None):
    """以现有双设备拓扑加载覆盖各类 UNI 的独立配置。

    只替换本端配置，对端及 NNI 链路仍沿用端到端样例。
    """
    bundle = "iosxr_bundle" if vendor == "cisco_iosxr" else "junos_bundle"
    filename = (
        "iosxr_uni_migration.cfg"
        if vendor == "cisco_iosxr"
        else "junos_uni_migration.cfg"
    )
    context = prepare_context(
        FIXTURES / bundle / "topology.xlsx", FIXTURES / bundle / "configs"
    )
    device = context.devices["R1" if vendor == "cisco_iosxr" else "J1"]
    device.document = parse_document(
        vendor, source or (FIXTURES / "interfaces" / filename).read_text()
    )
    return context, device


def test_cisco_business_recognizes_unnumbered_and_l2transport():
    document = parse_document(
        "cisco_iosxr",
        """interface GigabitEthernet0/0/0/1
 ipv4 unnumbered Loopback0
!
interface GigabitEthernet0/0/0/2.100 l2transport
 encapsulation dot1q 100
!
interface GigabitEthernet0/0/0/3.200
 encapsulation dot1q 200
!
end
""",
    )
    assert document.interfaces.analysis.business_interface_names() == {
        "GigabitEthernet0/0/0/1",
        "GigabitEthernet0/0/0/2.100",
    }


@pytest.mark.parametrize("vendor", ["cisco_iosxr", "juniper_junos"])
def test_uni_preserves_business_and_native_gateway(vendor):
    context, device = uni_context(vendor)
    build_default_pipeline().execute(context)
    assert not context.has_errors, [d.errors for d in context.devices.values()]
    output = device.document.render()
    for address in (
        "192.0.2.1",
        "198.51.100.1",
        "203.0.113.1",
        "172.16.0.1",
        "172.16.1.1",
        "172.20.0.1",
    ):
        assert address in output
    assert "ADAPT-UNI" not in output
    assert "Bundle-Ether20" not in output and "ae20" not in output
    assert "speed " not in output and "sonet-options" not in output
    mappings = [
        m
        for m in device.mappings
        if m.role == MappingRole.UNI and m.target_vlan is not None
    ]
    assert len(mappings) == 6
    assert all(
        mapping.target_interface
        == f"{device.profile.interfaces[-1]}.{mapping.target_vlan}"
        for mapping in mappings
    )
    assert len({m.target_vlan for m in mappings}) == 6
    qinq = next(m for m in mappings if m.inner_vlan == 200)
    assert qinq.source_vlan == 100
    assert qinq.target_vlan != 100
    if vendor == "cisco_iosxr":
        assert "routed interface BVI900" in output
        assert "interface BVI900" in output
        assert "vrf TENANT-A" in output and "vrf TENANT-GATEWAY" in output
        assert any(
            e.get("rule_id") == "cisco.remove.interface.service.policy"
            for e in context.events
        )
        assert (
            f"encapsulation dot1q {qinq.target_vlan} second-dot1q 200" in output
        )
        assert "bundle id 10 mode active" in output
    else:
        assert "routing-interface irb.300" in output
        assert "irb {" in output and "TENANT-A" in output
        assert "input KEEP-FILTER" in output and "mtu 9192" in output
        assert f"vlan-tags outer {qinq.target_vlan} inner 200" in output
        assert "802.3ad ae0" in output
    removed = [
        e
        for e in context.events
        if e.get("kind") == "uni-migration" and e.get("action") == "removed"
    ]
    assert removed
    assert all(e.get("source_interface") and e.get("path") for e in removed)
    assert all("ADAPT-UNI" not in str(e) for e in removed)


def test_original_vlan_reservations_are_not_stolen():
    specs = [
        InterfaceSpec("A", "A", None, None, InterfaceKind.PHYSICAL),
        InterfaceSpec("B.2", "B", "2", 2, InterfaceKind.PHYSICAL),
        InterfaceSpec("C.2", "C", "2", 2, InterfaceKind.PHYSICAL),
    ]
    assert allocate_uni_vlans(specs) == {"A": 3, "B.2": 2, "C.2": 4}


def test_vlan_exhaustion_is_explicit():
    specs = [
        InterfaceSpec(str(i), str(i), None, None, InterfaceKind.PHYSICAL)
        for i in range(4094)
    ]
    with pytest.raises(ValueError, match="VLAN"):
        allocate_uni_vlans(specs)


def test_nni_target_does_not_merge_old_uni_business():
    context, device = uni_context("cisco_iosxr")
    ports = device.profile.interfaces.copy()
    ports[0], ports[3] = ports[3], ports[0]
    device.profile = replace(device.profile, interfaces=ports)
    build_default_pipeline().execute(context)
    assert not context.has_errors, device.errors
    output = device.document.render()
    from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.operations.interfaces.access import (
        find_interface_node,
    )

    target = find_interface_node(device.document, "GigabitEthernet0/0/0/3")
    assert "bundle id 10" in device.document.writer.subtree(target)
    assert "192.0.2.1" not in device.document.writer.subtree(target)
    mapping = next(
        m
        for m in device.mappings
        if m.source_interface == "GigabitEthernet0/0/0/3"
        and m.role == MappingRole.UNI
    )
    assert f"interface {mapping.target_interface}" in output
    assert f"interface {mapping.target_interface}\n" in output


@pytest.mark.parametrize("vendor", ["cisco_iosxr", "juniper_junos"])
def test_known_type_attributes_are_removed_with_simulation_off(vendor):
    context, device = uni_context(vendor)
    device.profile = replace(
        device.profile,
        simulation_adaptation=SimulationAdaptationPolicy(mode="off"),
    )
    build_default_pipeline().execute(context)
    assert not context.has_errors, device.errors
    output = device.document.render()
    for command in (
        "speed ",
        "clock source",
        "pos crc",
        "sonet-options",
        "minimum-links",
    ):
        assert command not in output
    assert "192.0.2.1" in output


@pytest.mark.parametrize("vendor", ["cisco_iosxr", "juniper_junos"])
def test_complex_encapsulation_is_rejected(vendor):
    context, device = uni_context(vendor)
    source = device.document.render()
    source = (
        source.replace(
            "encapsulation dot1q 100\n", "encapsulation dot1q 100-200\n"
        )
        if vendor == "cisco_iosxr"
        else source.replace("vlan-id 100;", "vlan-id-list [ 100 200 ];")
    )
    device.document = parse_document(vendor, source)
    build_default_pipeline().execute(context)
    assert context.has_errors
    assert any("无法安全迁移封装" in error for error in device.errors)


def test_failed_uni_conversion_publishes_no_deployment_files(tmp_path):
    context, device = uni_context("cisco_iosxr")
    config_dir = tmp_path / "input"
    config_dir.mkdir()
    for name, item in context.devices.items():
        source = item.document.render()
        if name == device.device.name:
            source = source.replace(
                "encapsulation dot1q 100\n", "encapsulation dot1q any\n"
            )
        (config_dir / f"{name}.cfg").write_text(source)
    output = tmp_path / "output"
    result = convert(
        FIXTURES / "iosxr_bundle" / "topology.xlsx", config_dir, output
    )
    assert result.has_errors
    assert (output / "report.json").exists()
    assert not (output / "configs").exists()
    assert not (output / "topology-adapted.xlsx").exists()


@pytest.mark.parametrize("vendor", ["cisco_iosxr", "juniper_junos"])
@pytest.mark.parametrize("reuse_as", [MappingRole.NNI, MappingRole.UNI])
def test_uni_removes_member_dependencies_before_name_reuse(vendor, reuse_as):
    context, device = uni_context(vendor)
    cisco = vendor == "cisco_iosxr"
    original_member = "GigabitEthernet0/0/0/40" if cisco else "ge-0/0/40"
    service = "Bundle-Ether20.200" if cisco else "ae20.200"
    source = device.document.render()
    if reuse_as == MappingRole.UNI:
        member = device.profile.uni_parent
        source = source.replace(original_member, member)
    else:
        member = original_member
        device.profile = replace(
            device.profile, interfaces=[member, *device.profile.interfaces[1:]]
        )
    if cisco:
        source = source.replace(
            "\nend\n",
            f"\nrouter ospf 90\n area 0\n  interface {member}\n"
            f"  interface {service}\n!\nend\n",
        )
    else:
        source += (
            f"protocols {{ ospf {{ area 0 {{ interface {member}; "
            f"interface {service}; }} }} }}\n"
            f"routing-options {{ static {{ route 10.20.0.0/24 {{ "
            f"next-hop [ {member} 172.16.1.2 ]; "
            f"qualified-next-hop {member} {{ preference 10; }} "
            f"qualified-next-hop {service} {{ preference 20; }} }} }} }}\n"
        )
    device.document = parse_document(vendor, source)

    build_default_pipeline().execute(context)

    assert not context.has_errors, device.errors
    output = device.document.render()
    assert member in {
        spec.parent
        for spec in device.document.interfaces.analysis.interface_specs()
    }
    assert f"  interface {member}" + ("\n" if cisco else ";") not in output
    target = next(
        mapping.target_interface
        for mapping in device.mappings
        if mapping.source_interface == service and mapping.action == "map"
    )
    assert f"  interface {target}" + ("\n" if cisco else ";") in output
    assert "172.16.1.1" in output
    if not cisco:
        assert "route 10.20.0.0/24 {" in output
        assert "next-hop 172.16.1.2;" in output
        assert f"qualified-next-hop {member} {{" not in output
        assert (
            f"qualified-next-hop {target} {{\n                preference 20;"
            in output
        )
        assert "preference 10;" not in output
    assert any(
        event["kind"] == "uni-migration"
        and event.get("action") == "remove"
        and "ospf" in str(event["path"])
        and member
        in event.get("source_interfaces", event.get("interfaces", []))
        for event in context.events
    )


@pytest.mark.parametrize(
    ("vendor", "fixture"),
    [("cisco_iosxr", "iosxr_bundle"), ("juniper_junos", "junos_bundle")],
)
def test_unknown_uni_member_reference_blocks_deployment(
    vendor, fixture, tmp_path
):
    context, device = uni_context(vendor)
    cisco = vendor == "cisco_iosxr"
    member = device.profile.uni_parent
    original_member = "GigabitEthernet0/0/0/40" if cisco else "ge-0/0/40"
    source = device.document.render().replace(original_member, member)
    if cisco:
        source = source.replace(
            "\nend\n",
            f"\ncustom-service LOST\n bind-interface {member}\n!\nend\n",
        )
    else:
        source += f"custom-service LOST {{ bind-interface {member}; }}\n"
    config_dir = tmp_path / "input"
    config_dir.mkdir()
    for name, item in context.devices.items():
        config = source if item is device else item.document.render()
        (config_dir / f"{name}.cfg").write_text(config)

    output = tmp_path / "output"
    result = convert(FIXTURES / fixture / "topology.xlsx", config_dir, output)

    assert result.has_errors
    assert any(
        event["kind"] == "uni-migration-error"
        and f"custom-service LOST / bind-interface {member}" in event["message"]
        for event in result.events
    )
    assert (output / "report.json").exists()
    assert not (output / "configs").exists()
    assert not (output / "topology-adapted.xlsx").exists()


@pytest.mark.parametrize("vendor", ["cisco_iosxr", "juniper_junos"])
def test_uni_parent_can_originally_be_an_active_nni_source(vendor):
    context, device = uni_context(vendor)
    original = (
        "GigabitEthernet0/0/0/0" if vendor == "cisco_iosxr" else "ge-0/0/0"
    )
    target = device.profile.uni_parent
    device.document = parse_document(
        vendor, device.document.render().replace(original, target)
    )
    for link in context.topology.links:
        if (
            link.a_device == device.device.name
            and device.document.interfaces.analysis.resolve_interface(
                link.a_interface
            )
            == original
        ):
            link.a_interface = target
    build_default_pipeline().execute(context)
    assert not context.has_errors, device.errors
    assert "192.0.2.1" in device.document.render()
    members = device.document.interfaces.analysis.bundle_members()
    assert target not in members
    assert "ADAPT-UNI" not in device.document.render()


@pytest.mark.parametrize("vendor", ["cisco_iosxr", "juniper_junos"])
def test_aggregate_member_independent_business_is_not_deleted(vendor):
    context, device = uni_context(vendor)
    source = device.document.render()
    if vendor == "cisco_iosxr":
        source = source.replace(
            " bundle id 20 mode active",
            " ipv4 address 10.99.0.1 255.255.255.0\n bundle id 20 mode active",
            1,
        )
    else:
        source = source.replace(
            "    ge-0/0/40 {",
            "    ge-0/0/40 {\n        unit 0 { family inet { address 10.99.0.1/24; } }",
            1,
        )
    device.document = parse_document(vendor, source)
    build_default_pipeline().execute(context)
    assert context.has_errors
    assert any("独立业务" in error for error in device.errors)
    assert "10.99.0.1" in device.document.render()


def test_conflicting_parent_ethertypes_stop_conversion():
    context, device = uni_context("cisco_iosxr")
    source = (
        device.document.render()
        .replace(
            " description PHYSICAL-UNI",
            " ethernet dot1q tunneling ethertype 0x9100\n description PHYSICAL-UNI",
        )
        .replace(
            " service-policy input KEEP-QOS",
            " ethernet dot1q tunneling ethertype 0x88a8\n service-policy input KEEP-QOS",
        )
    )
    device.document = parse_document("cisco_iosxr", source)
    build_default_pipeline().execute(context)
    assert context.has_errors
    assert any("ethertype 冲突" in error for error in device.errors)


@pytest.mark.parametrize("vendor", ["cisco_iosxr", "juniper_junos"])
def test_unknown_unit_commands_are_retained_with_warning(vendor):
    context, device = uni_context(vendor)
    source = device.document.render()
    if vendor == "cisco_iosxr":
        source = source.replace(
            " description PHYSICAL-UNI",
            " custom-vendor-knob 7\n description PHYSICAL-UNI",
        )
    else:
        source = source.replace(
            "vlan-id 100;", "vlan-id 100;\n custom-vendor-knob 7;"
        )
    device.document = parse_document(vendor, source)
    build_default_pipeline().execute(context)
    assert not context.has_errors, device.errors
    assert "custom-vendor-knob 7" in device.document.render()
    assert (
        sum("custom-vendor-knob" in message for message in device.warnings) == 1
    )
    diagnostics = [
        event
        for event in context.events
        if "custom-vendor-knob" in event["message"]
    ]
    assert len(diagnostics) == 1
    if vendor == "cisco_iosxr":
        assert diagnostics[0]["kind"] == "interface-validation-warning"
        assert "GigabitEthernet0/0/0/3" in diagnostics[0]["source_interfaces"]
        assert diagnostics[0]["target_interface"].startswith(
            device.profile.uni_parent + "."
        )
    else:
        assert diagnostics[0]["kind"] == "uni-migration-warning"


@pytest.mark.parametrize("vendor", ["cisco_iosxr", "juniper_junos"])
def test_reprocessing_keeps_tags_business_and_gateway(vendor):
    context, device = uni_context(vendor)
    build_default_pipeline().execute(context)
    assert not context.has_errors, device.errors
    first_specs = {
        (s.name, s.vlan, s.inner_vlan)
        for s in device.document.interfaces.analysis.interface_specs()
    }
    for item in context.devices.values():
        item.mappings.clear()
        item.removed_interfaces.clear()
        item.uni_plan.clear()
    build_default_pipeline().execute(context)
    assert not context.has_errors, device.errors
    assert first_specs == {
        (s.name, s.vlan, s.inner_vlan)
        for s in device.document.interfaces.analysis.interface_specs()
    }
    assert "172.20.0.1" in device.document.render()


@pytest.mark.parametrize(
    ("other_name", "other_vlan"),
    [("CUSTOMER", 200), ("OTHER", 100), ("CUSTOMER", 100)],
)
@pytest.mark.parametrize("default_scope", [False, True])
@pytest.mark.parametrize("use_vlan_name", [False, True])
def test_junos_gateway_vlan_matching_stays_in_instance(
    other_name, other_vlan, default_scope, use_vlan_name
):
    active_vlan = """vlans {
 CUSTOMER { vlan-id 100; l3-interface irb.100; }
} """
    active_scope = (
        active_vlan
        if default_scope
        else "routing-instances { TENANT-A { instance-type virtual-switch; "
        "interface ge-0/0/1.0; " + active_vlan + " } }"
    )
    member_config = (
        "family ethernet-switching { vlan { members CUSTOMER; } }"
        if use_vlan_name
        else "vlan-id 100; family bridge;"
    )
    source = "interfaces { ge-0/0/1 { unit 0 { " + member_config + " } }"
    source += """
 irb {
  unit 100 { family inet { address 192.0.2.1/24; } }
  unit 200 { family inet { address 198.51.100.1/24; } }
 }
}
""" + active_scope
    source += f"""routing-instances {{ TENANT-B {{
 instance-type virtual-switch;
 vlans {{ {other_name} {{
  vlan-id {other_vlan}; l3-interface irb.200;
 }} }}
}} }}"""
    document = parse_document("juniper_junos", source)
    assert document.interfaces.analysis.business_interface_names() == {
        "ge-0/0/1.0",
        "irb.100",
    }


def test_junos_gateway_number_fallback_stays_in_instance():
    document = parse_document(
        "juniper_junos",
        """interfaces {
 ge-0/0/1 { unit 100 { vlan-id 100; family bridge; } }
 irb {
  unit 100 { family inet { address 198.51.100.1/24; } }
  unit 200 { family inet { address 192.0.2.1/24; } }
 }
}
routing-instances {
 TENANT-A {
  instance-type virtual-switch;
  bridge-domains { BLUE {
   vlan-id 100; interface ge-0/0/1.100; routing-interface irb.200;
  } }
 }
 TENANT-B {
  instance-type virtual-switch;
  bridge-domains { RED { vlan-id 300; routing-interface irb.100; } }
 }
}
""",
    )
    assert document.interfaces.analysis.business_interface_names() == {
        "ge-0/0/1.100",
        "irb.200",
    }
