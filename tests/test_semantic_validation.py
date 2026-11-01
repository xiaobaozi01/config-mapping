"""验证源语义检查与最终转换完整性检查的职责分层。"""

import unittest
from pathlib import Path

from adaptation.interfaces import (
    SourceSemanticValidationHandler,
    _source_semantic_parents,
)
from adaptation.models import (
    ConversionContext,
    Device,
    DeviceContext,
    InterfaceMapping,
    Link,
    MappingAction,
    MappingRole,
    TopologyWorkbook,
    Vendor,
)
from adaptation.profiles import load_profiles
from impl.cisco.document import CiscoDocument


def _context_for_cisco(config, links, *, target=None):
    """构造单设备 Cisco 验证上下文。

    可选 NNI 自映射让源语义和最终校验都访问同一端口。
    """
    name = "R1"
    device = Device(name, Vendor.CISCO_IOSXR, "R1.cfg", 2)
    mappings = []
    if target:
        mappings.append(
            InterfaceMapping(
                device=name,
                source_interface=target,
                role=MappingRole.NNI,
                action=MappingAction.MAP,
                target_interface=target,
            )
        )
    device_context = DeviceContext(
        device,
        Path("R1.cfg"),
        CiscoDocument(config),
        load_profiles(None)[Vendor.CISCO_IOSXR],
        mappings=mappings,
    )
    context = ConversionContext(
        TopologyWorkbook(
            Path("topology.xlsx"),
            None,
            "devices",
            "links",
            {},
            {},
            [device],
            links,
        ),
        {name: device_context},
    )
    return context, device_context


class SourceSemanticValidationTest(unittest.TestCase):
    """验证候选范围、默认告警和严格阻断。

    测试只覆盖映射前源语义阶段，不依赖最终完整性规则。
    """

    def test_source_scope_covers_active_bundle_and_skips_inactive_link(self):
        """源范围覆盖活动 Bundle 全部成员并忽略 inactive 链路。

        未直接出现在拓扑表中的同 Bundle 成员也必须参与源语义检查。
        """
        context, device_context = _context_for_cisco(
            "interface GigabitEthernet0/0/0/0\n bundle id 10 mode on\n!\n"
            "interface GigabitEthernet0/0/0/1\n bundle id 10 mode on\n!\n"
            "interface GigabitEthernet0/0/0/2\n bundle id 10 mode on\n!\n"
            "end\n",
            [
                Link(
                    2,
                    "active",
                    "R1",
                    "GigabitEthernet0/0/0/0",
                    "PEER",
                    "GigabitEthernet0/0/0/0",
                ),
                Link(
                    3,
                    "inactive",
                    "R1",
                    "GigabitEthernet0/0/0/9",
                    "PEER",
                    "GigabitEthernet0/0/0/9",
                    active=False,
                ),
            ],
        )

        parents = _source_semantic_parents(context, "R1", device_context)

        self.assertTrue({
            "GigabitEthernet0/0/0/0",
            "GigabitEthernet0/0/0/1",
            "GigabitEthernet0/0/0/2",
            "Bundle-Ether10",
        } <= parents)
        self.assertNotIn("GigabitEthernet0/0/0/9", parents)

    def test_source_semantic_problem_warns_by_default(self):
        """默认模式报告源配置语义问题但不阻断。

        后续映射可以继续处理此前已经告警的输入。
        """
        context, device_context = _context_for_cisco(
            "interface GigabitEthernet0/0/0/0\n"
            " mtu 1500\n mtu 9000\n!\nend\n",
            [
                Link(
                    2,
                    "link",
                    "R1",
                    "GigabitEthernet0/0/0/0",
                    "PEER",
                    "GigabitEthernet0/0/0/0",
                )
            ],
        )

        SourceSemanticValidationHandler().process(context)

        self.assertFalse(context.has_errors)
        self.assertTrue(any(
            "mtu 存在冲突" in warning for warning in device_context.warnings
        ))

    def test_source_semantic_problem_blocks_in_strict_mode(self):
        """严格模式在映射前阻断源配置语义问题。

        错误必须发生在任何接口映射或配置改写之前。
        """
        context, device_context = _context_for_cisco(
            "interface GigabitEthernet0/0/0/0\n"
            " mtu 1500\n mtu 9000\n!\nend\n",
            [
                Link(
                    2,
                    "link",
                    "R1",
                    "GigabitEthernet0/0/0/0",
                    "PEER",
                    "GigabitEthernet0/0/0/0",
                )
            ],
        )
        context.strict_input_validation = True

        SourceSemanticValidationHandler().process(context)

        self.assertTrue(context.has_errors)
        self.assertTrue(any(
            "mtu 存在冲突" in error for error in device_context.errors
        ))


class FinalIntegrityValidationTest(unittest.TestCase):
    """验证最终阶段只检查转换完整性和目标镜像约束。

    源配置语义不在这里重复执行。
    """

    def test_final_integrity_omits_source_semantics_but_keeps_target_limits(self):
        """最终校验不重复源语义，但目标镜像不兼容仍阻断。

        源问题已经在前置阶段完成告警或严格阻断。
        """
        document = CiscoDocument(
            "interface GigabitEthernet0/0/0/0\n"
            " mtu 1500\n"
            " mtu 9000\n"
            " speed 100\n"
            "!\n"
            "end\n"
        )

        outcome = document.validate_final_integrity(
            {"GigabitEthernet0/0/0/0"},
            set(),
        )

        rendered = "\n".join(outcome.errors)
        self.assertNotIn("mtu 存在冲突", rendered)
        self.assertIn("残留不兼容属性", rendered)


if __name__ == "__main__":
    unittest.main()
