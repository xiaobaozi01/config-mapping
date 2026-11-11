"""UNI 迁移与正常 YAML 清洗的顺序及删除边界。"""

import pytest
import yaml
from test_uni_migration import uni_context

from gns3_config_adaptor.adaptation import cleaning_rules
from gns3_config_adaptor.adaptation.cleaning_rules import (
    CleaningPoint,
    CleaningRulesHandler,
)
from gns3_config_adaptor.adaptation.factory import parse_document
from gns3_config_adaptor.adaptation.pipeline import build_default_pipeline
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.support.interface_commands import (
    AGGREGATION_ATTRIBUTES,
    STRIPPABLE_AGGREGATION_ATTRIBUTES,
    is_uni_type_attribute,
)
from gns3_config_adaptor.impl.common.support.config_paths import (
    cleaning_rules_path,
)


def add_parent_property(vendor):
    """在有子接口业务的原父口上附加由 YAML 处理的测试属性。

    该命令不属于迁移代码已知命令，能验证迁移不会因缺少硬编码而提前报错。
    """
    context, device = uni_context(vendor)
    source = device.document.render()
    if vendor == "cisco_iosxr":
        source = source.replace(
            "interface GigabitEthernet0/0/0/4.100",
            "interface GigabitEthernet0/0/0/4\n test-media-a 7\n!\ninterface GigabitEthernet0/0/0/4.100",
            1,
        )
    else:
        source = source.replace(
            "    ge-0/0/4 {", "    ge-0/0/4 {\n        test-media-a 7;", 1
        )
    device.document = parse_document(vendor, source)
    return context, device


def install_parent_cleanup(tmp_path, monkeypatch, vendor, *, enabled=True):
    """仅向既有 YAML 文件添加普通删除规则。

    规则使用非硬件类别，确保接口迁移没有提取类别或匹配表达式的隐藏依赖。
    """
    source = tmp_path / "cleaning.yaml"
    payload = yaml.safe_load(cleaning_rules_path(vendor).read_text())
    payload["rules"].append(
        {
            "id": "test.remove.parent",
            "vendor": vendor,
            "enable": enabled,
            "category": "test-parent",
            "path": [r"^interface\b"]
            if vendor == "cisco_iosxr"
            else ["^interfaces$", "*"],
            "match": r"^test-media-a\b",
            "action": "delete",
            "reason": "测试普通父口清理",
        }
    )
    source.write_text(yaml.safe_dump(payload, allow_unicode=True))

    def locate(current_vendor):
        """只替换本测试厂商的清洗文件。

        其余厂商继续按默认资源加载，保持实际跨厂商注册流程。
        """
        return (
            source
            if current_vendor == vendor
            else cleaning_rules_path(current_vendor)
        )

    monkeypatch.setattr(cleaning_rules, "cleaning_rules_path", locate)


@pytest.mark.parametrize("vendor", ["cisco_iosxr", "juniper_junos"])
def test_parent_rules_run_at_existing_post_rewrite_stage(
    tmp_path, monkeypatch, vendor
):
    install_parent_cleanup(tmp_path, monkeypatch, vendor)
    context, device = add_parent_property(vendor)
    saw_migration = False
    saw_cleanup = False
    for stage in build_default_pipeline().stages:
        stage.process(context)
        assert not context.has_errors, device.errors
        if type(stage).__name__ == "UNIHandler":
            saw_migration = True
            assert "test-media-a 7" in device.document.render()
            assert "ADAPT-UNI" in device.document.render()
            assert not any(
                event.get("category") == "test-parent"
                for event in context.events
            )
        if (
            isinstance(stage, CleaningRulesHandler)
            and stage.point == CleaningPoint.POST_REWRITE
        ):
            saw_cleanup = True
            assert saw_migration
            assert "test-media-a" not in device.document.render()
    assert saw_migration and saw_cleanup
    assert "ADAPT-UNI" not in device.document.render()
    assert "198.51.100.1" in device.document.render()
    assert any(
        event.get("category") == "test-parent" and event["hits"] == 1
        for event in context.events
    )


@pytest.mark.parametrize("vendor", ["cisco_iosxr", "juniper_junos"])
def test_disabled_parent_rule_fails_after_cleanup_not_during_migration(
    tmp_path, monkeypatch, vendor
):
    install_parent_cleanup(tmp_path, monkeypatch, vendor, enabled=False)
    context, device = add_parent_property(vendor)
    last_stage = None
    for stage in build_default_pipeline().stages:
        stage.process(context)
        last_stage = type(stage).__name__
        if context.has_errors:
            break
    assert last_stage == "InterfaceCompatibilityHandler"
    assert any(
        "test-media-a" in error and "清洗后" in error for error in device.errors
    )
    assert all("ADAPT-UNI" not in error for error in device.errors)
    assert not any(
        event.get("category") == "test-parent" for event in context.events
    )


@pytest.mark.parametrize("vendor", ["cisco_iosxr", "juniper_junos"])
def test_hardware_cleanup_is_not_duplicated_in_uni(vendor):
    context, device = uni_context(vendor)
    for stage in build_default_pipeline().stages:
        stage.process(context)
        assert not context.has_errors, device.errors
        if type(stage).__name__ == "UNIHandler":
            assert "speed " in device.document.render()
            assert not any(
                event.get("command", "").startswith("speed ")
                for event in context.events
            )
        if (
            isinstance(stage, CleaningRulesHandler)
            and stage.point == CleaningPoint.POST_REWRITE
        ):
            assert "speed " not in device.document.render()
    assert "192.0.2.1" in device.document.render()
    assert "ADAPT-UNI" not in device.document.render()
    assert any(
        event.get("category") == "physical-interface-knob"
        for event in context.events
    )


def test_aggregation_helpers_preserve_each_callers_existing_boundary():
    assert AGGREGATION_ATTRIBUTES.match("no bundle id 10")
    assert AGGREGATION_ATTRIBUTES.match("aggregated-options")
    assert not STRIPPABLE_AGGREGATION_ATTRIBUTES.match("no bundle id 10")
    assert STRIPPABLE_AGGREGATION_ATTRIBUTES.match("bundle id 10")
    assert is_uni_type_attribute("no lacp mode active")
    assert not is_uni_type_attribute("aggregated-options")
    assert not is_uni_type_attribute("bundled-placeholder")
