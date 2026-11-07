"""华为聚合只读解析及每个外部 M-LAG 对端保留一条成员。"""

import shutil
from pathlib import Path

import pytest
from openpyxl import load_workbook

from gns3_config_adaptor.adaptation.external_aggregation import (
    canonical_interface,
    parse_huawei_aggregation,
)
from gns3_config_adaptor.adaptation.service import convert, prepare_context

FIXTURES = Path(__file__).parent / "fixtures" / "external_aggregation"


@pytest.fixture(params=["R1", "J1"])
def case(request, tmp_path):
    name = request.param
    shutil.copytree(FIXTURES / "configs", tmp_path / "configs")
    shutil.copyfile(FIXTURES / f"{name}.xlsx", tmp_path / "topology.xlsx")
    return tmp_path, name


def run_case(root):
    return convert(root / "topology.xlsx", root / "configs", root / "output")


def single_peer(root):
    workbook = load_workbook(root / "topology.xlsx")
    workbook["链接表"].delete_rows(4, 4)
    workbook.save(root / "topology.xlsx")


def change_config(root, name, old, new):
    path = root / "configs" / f"{name}.cfg"
    path.write_text(
        path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8"
    )


def test_reader_supports_both_membership_styles_ranges_and_dfs():
    first = parse_huawei_aggregation((FIXTURES / "configs/H1.cfg").read_text())
    second = parse_huawei_aggregation((FIXTURES / "configs/H2.cfg").read_text())
    assert first.members == {
        "10ge1/0/1": "eth-trunk20",
        "10ge1/0/2": "eth-trunk20",
        "100ge1/0/1": "eth-trunk0",
        "100ge1/0/2": "eth-trunk0",
        "ge1/0/3": None,
    }
    assert second.members["10ge1/0/2"] == "eth-trunk30"
    assert first.mlags == {"eth-trunk20": ("1", "10")}
    assert second.mlags == {"eth-trunk30": ("1", "10")}
    assert first.dfs_peers == {"1": ("192.0.2.1", "192.0.2.2")}
    assert second.dfs_peers == {"1": ("192.0.2.2", "192.0.2.1")}
    assert first.peer_links == {"eth-trunk0": "1"}
    assert canonical_interface("GigabitEthernet 1/0/3") == "ge1/0/3"


@pytest.mark.parametrize(
    "declaration",
    [
        "trunkport 100GE 1/0/1 1/0/2",
        "trunkport 100GE 1/0/1 to 100GE 1/0/2",
    ],
)
def test_reader_expands_equivalent_trunkport_syntax(declaration):
    source = (FIXTURES / "configs/H1.cfg").read_text()
    source = source.replace("trunkport 100GE 1/0/1 to 1/0/2", declaration)
    info = parse_huawei_aggregation(source)
    assert info.members["100ge1/0/1"] == "eth-trunk0"
    assert info.members["100ge1/0/2"] == "eth-trunk0"


@pytest.mark.parametrize(
    "declaration",
    [
        "trunkport 100GE 1/0/2 to 1/0/1",
        "trunkport 100GE 1/0/1 to 2/0/2",
        "trunkport 100GE 1/0/1 to",
    ],
)
def test_reader_rejects_ambiguous_ranges(declaration):
    source = (FIXTURES / "configs/H1.cfg").read_text()
    with pytest.raises(ValueError, match="trunkport"):
        parse_huawei_aggregation(
            source.replace("trunkport 100GE 1/0/1 to 1/0/2", declaration)
        )


def test_mlag_preserves_one_link_per_peer_without_huawei_output(case):
    root, name = case
    originals = {
        path.name: path.read_bytes() for path in (root / "configs").iterdir()
    }
    context = run_case(root)
    assert not context.has_errors, context.errors
    assert set(context.devices) == {name}
    assert context.external_devices == {"H1", "H2"}
    assert set(context.external_aggregations) == {"H1", "H2"}
    active = [link for link in context.topology.links if link.active]
    assert [link.row for link in active] == [2, 4, 6, 7]
    assert context.topology.links[1].skip_reason == "聚合成员缩减，保留第 2 行"
    assert [(link.z_device, link.z_interface) for link in active[:2]] == [
        ("H1", "10GE1/0/1"),
        ("H2", "10GE1/0/1"),
    ]
    assert all(mapping.device == name for mapping in context.mappings)
    assert (
        sum(mapping.action == "map-member" for mapping in context.mappings) == 2
    )
    assert (
        sum(
            event["kind"] == "external-mlag-verified"
            for event in context.events
        )
        == 1
    )
    assert {
        path.name: path.read_bytes() for path in (root / "configs").iterdir()
    } == originals
    assert_local_output(root, name, context)


def assert_local_output(root, name, context):
    output = (root / "output/configs" / f"{name}.cfg").read_text()
    binding = "bundle id 10" if name == "R1" else "802.3ad ae10;"
    assert output.count(binding) == 2
    assert "10.0.0.1" in output
    reference = (
        "interface Bundle-Ether10" if name == "R1" else "interface ae10.0;"
    )
    assert (
        reference
        in output.split("router ospf" if name == "R1" else "protocols")[1]
    )
    bundle = "Bundle-Ether10" if name == "R1" else "ae10"
    assert any(
        mapping.source_interface == bundle and mapping.action == "preserve"
        for mapping in context.mappings
    )
    assert sorted(
        path.name for path in (root / "output/configs").iterdir()
    ) == [f"{name}.cfg"]
    adapted = load_workbook(root / "output/topology-adapted.xlsx")
    assert adapted["链接表"].max_row == 5
    assert list(adapted["链接表"].values)[-2:] == [
        ("H1", "100GE1/0/1", "H2", "100GE1/0/1", "peer-link-1"),
        ("H1", "100GE1/0/2", "H2", "100GE1/0/2", "peer-link-2"),
    ]


def test_single_huawei_trunk_is_compressed_without_mlag(case):
    root, _ = case
    single_peer(root)
    change_config(root, "H1", " dfs-group 1 m-lag 10\n", "")
    context = run_case(root)
    assert not context.has_errors, context.errors
    assert [link.row for link in context.topology.links if link.active] == [2]
    assert not any(
        event["kind"] == "external-mlag-verified" for event in context.events
    )


def test_known_plain_huawei_ports_are_not_compressed(case):
    root, _ = case
    single_peer(root)
    change_config(root, "H1", " eth-trunk 20", " description STANDALONE")
    context = run_case(root)
    assert not context.has_errors, context.errors
    assert all(link.active for link in context.topology.links)
    assert (
        len(
            [
                mapping
                for mapping in context.mappings
                if mapping.action == "map-member"
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    "source, replacement",
    [
        (
            "source ip 192.0.2.1 peer 192.0.2.2",
            "source ip 192.0.2.1 peer 192.0.2.99",
        ),
        ("dfs-group 1 m-lag 10", "dfs-group 1 m-lag 11"),
        ("dfs-group 1 m-lag 10", "description NOT-MLAG"),
    ],
)
def test_invalid_pairing_fails_before_topology_or_config_migration(
    case, source, replacement
):
    root, name = case
    change_config(root, "H1", source, replacement)
    before = (
        prepare_context(root / "topology.xlsx", root / "configs")
        .devices[name]
        .document.render()
    )
    context = run_case(root)
    assert context.has_errors
    assert any("MC-LAG" in error for error in context.errors)
    assert all(link.active for link in context.topology.links)
    assert context.devices[name].document.render() == before
    assert context.mappings == []
    assert not (root / "output/configs").exists()


def test_source_only_dfs_requires_real_peer_link_evidence(case):
    root, _ = case
    change_config(root, "H1", " peer 192.0.2.2", "")
    change_config(root, "H2", " peer 192.0.2.1", "")
    context = run_case(root)
    assert not context.has_errors, context.errors
    workbook = load_workbook(root / "topology.xlsx")
    workbook["链接表"].delete_rows(6, 2)
    workbook.save(root / "topology.xlsx")
    context = run_case(root)
    assert context.has_errors
    assert any("MC-LAG" in error for error in context.errors)
    assert all(link.active for link in context.topology.links)


def test_missing_physical_port_is_unknown_instead_of_plain(case):
    root, _ = case
    change_config(root, "H1", "interface 10GE1/0/2", "interface 10GE1/0/99")
    context = run_case(root)
    assert context.has_errors
    assert any(
        "H1" in error and "10GE1/0/2" in error for error in context.errors
    )
    assert all(link.active for link in context.topology.links)


def test_reciprocal_dfs_addresses_do_not_need_peer_links_in_topology(case):
    root, _ = case
    workbook = load_workbook(root / "topology.xlsx")
    workbook["链接表"].delete_rows(6, 2)
    workbook.save(root / "topology.xlsx")
    context = run_case(root)
    assert not context.has_errors, context.errors
    assert [link.row for link in context.topology.links if link.active] == [
        2,
        4,
    ]


def test_reused_external_physical_port_is_rejected(case):
    root, _ = case
    workbook = load_workbook(root / "topology.xlsx")
    workbook["链接表"].cell(3, 4).value = "10GE1/0/1"
    workbook.save(root / "topology.xlsx")
    context = run_case(root)
    assert any(
        "设备 H1" in error and "重复占用" in error for error in context.errors
    )
    assert all(link.active for link in context.topology.links)


def test_optional_missing_config_keeps_external_members_unknown(case):
    root, _ = case
    single_peer(root)
    workbook = load_workbook(root / "topology.xlsx")
    workbook["设备列表"].cell(3, 3).value = None
    workbook.save(root / "topology.xlsx")
    (root / "configs/H1.cfg").unlink()
    context = run_case(root)
    assert context.has_errors
    assert any("外部对端聚合关系无法核验" in error for error in context.errors)
    assert all(link.active for link in context.topology.links)


def test_conflicting_member_bindings_are_rejected(case):
    root, _ = case
    change_config(root, "H1", " eth-trunk 20", " eth-trunk 20\n eth-trunk 21")
    context = run_case(root)
    assert context.has_errors
    assert any("冲突绑定" in error for error in context.errors)
    assert not (root / "output/configs").exists()


def test_configured_huawei_file_must_exist(case):
    root, _ = case
    (root / "configs/H1.cfg").unlink()
    context = run_case(root)
    assert any(
        "H1" in error and "找不到配置文件" in error for error in context.errors
    )


def test_huawei_config_path_cannot_escape_directory(case):
    root, _ = case
    shutil.copyfile(root / "configs/H1.cfg", root / "outside.cfg")
    workbook = load_workbook(root / "topology.xlsx")
    workbook["设备列表"].cell(3, 3).value = "../outside.cfg"
    workbook.save(root / "topology.xlsx")
    context = run_case(root)
    assert any("超出配置目录" in error for error in context.errors)
