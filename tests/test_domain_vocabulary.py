"""领域字符串在配置和映射边界上应尽早校验并保持原序列化值。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from gns3_config_adaptor.adaptation.models import (
    ConversionContext,
    EventKind,
    InterfaceMapping,
    TopologyWorkbook,
)
from gns3_config_adaptor.impl.common.domain.policies import (
    SimulationAdaptationPolicy,
    WashingPolicy,
)


class DomainVocabularyValidationTest(unittest.TestCase):

    def test_interface_mapping_rejects_unknown_role(self):
        with self.assertRaisesRegex(ValueError, "role"):
            InterfaceMapping(
                device="R1",
                source_interface="Gi0/0/0/0",
                role="UNKNOWN",
                action="map",
                target_interface="Gi0/0/0/1",
            )

    def test_interface_mapping_rejects_unknown_action(self):
        with self.assertRaisesRegex(ValueError, "action"):
            InterfaceMapping(
                device="R1",
                source_interface="Gi0/0/0/0",
                role="NNI",
                action="invented",
                target_interface="Gi0/0/0/1",
            )

    def test_simulation_policy_rejects_unknown_mode(self):
        with self.assertRaisesRegex(ValueError, "mode"):
            SimulationAdaptationPolicy(mode="aggressive")

    def test_washing_policy_rejects_unknown_group_identity_mode(self):
        with self.assertRaisesRegex(ValueError, "group_unknown_identity"):
            WashingPolicy(group_unknown_identity="guess")


class DomainVocabularySerializationTest(unittest.TestCase):

    def test_interface_mapping_keeps_existing_string_wire_values(self):
        mapping = InterfaceMapping(
            device="R1",
            source_interface="Bundle-Ether10",
            role="NNI",
            action="preserve",
            target_interface="Bundle-Ether10",
        )

        self.assertEqual(
            mapping.to_dict(),
            {
                "device": "R1",
                "source_interface": "Bundle-Ether10",
                "role": "NNI",
                "action": "preserve",
                "target_interface": "Bundle-Ether10",
                "link_rows": [],
                "reason": "",
            },
        )
        self.assertEqual(
            json.dumps(mapping.to_dict(), ensure_ascii=False),
            (
                '{"device": "R1", "source_interface": "Bundle-Ether10", '
                '"role": "NNI", "action": "preserve", '
                '"target_interface": "Bundle-Ether10", "link_rows": [], '
                '"reason": ""}'
            ),
        )

    def test_event_kind_serializes_to_existing_report_string(self):
        context = ConversionContext(
            topology=TopologyWorkbook(
                path=Path("topology.xlsx"),
                workbook=None,
                device_sheet="",
                link_sheet="",
                device_headers={},
                link_headers={},
                devices=[],
                links=[],
            ),
            devices={},
        )

        context.add_event(EventKind.GROUP_CONFLICT, "冲突")

        self.assertEqual(
            json.dumps(context.events[0], ensure_ascii=False),
            '{"kind": "group-conflict", "message": "冲突"}',
        )


if __name__ == "__main__":
    unittest.main()
