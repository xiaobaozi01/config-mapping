"""验证配置解析前的采集回显整理及端到端报告。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config_adaptor.adaptation import input_normalization
from config_adaptor.adaptation.input_normalization import normalize_input
from config_adaptor.adaptation.models import Vendor
from config_adaptor.adaptation.service import convert
from config_adaptor.juniper.document import JunosDocument


FIXTURES = Path(__file__).parent / "fixtures"


class InputNormalizationTest(unittest.TestCase):
    def test_junos_capture_removes_leading_and_multiline_trailing_noise(self):
        source = (
            "<AG-DPKLD-01>show configuration\n"
            "## Last commit: 2025-06-18 09:45:25 WIB by m2m_msp\n"
            "version 24.4R1-S2.9;\n"
            "system { host-name AG-DPKLD-01; }\n"
            "{\n    master\n}\n"
            "ca_usr@AG-DPKLD-01>\n"
            "========= ######HUAWEI#####=========\n"
            "<AG-DPKLD-01>\n"
        )

        result = normalize_input(Vendor.JUNIPER_JUNOS, source)

        self.assertEqual(
            result.text,
            "version 24.4R1-S2.9;\nsystem { host-name AG-DPKLD-01; }\n",
        )
        self.assertEqual(
            [(item.rule_id, item.start_line, item.end_line) for item in result.removed],
            [
                ("juniper.command_echo", 1, 1),
                ("juniper.last_commit", 2, 2),
                ("juniper.angle_prompt", 10, 10),
                ("juniper.capture_separator", 9, 9),
                ("juniper.cli_prompt", 8, 8),
                ("juniper.master_tail", 5, 7),
            ],
        )
        self.assertEqual(JunosDocument(result.text).hostname(), "AG-DPKLD-01")
        self.assertEqual(normalize_input(Vendor.JUNIPER_JUNOS, result.text).text, result.text)

    def test_normal_configurations_are_unchanged(self):
        for vendor, source in (
            (Vendor.JUNIPER_JUNOS, "\nsystem { host-name J1; }\n\n"),
            (Vendor.CISCO_IOSXR, "hostname R1\n!\nend\n"),
        ):
            with self.subTest(vendor=vendor):
                result = normalize_input(vendor, source)
                self.assertEqual(result.text, source)
                self.assertEqual(result.removed, ())

    def test_known_pattern_inside_configuration_is_left_untouched(self):
        source = "system { host-name J1; }\n{\n master\n}\nprotocols { ospf; }\n"
        result = normalize_input(Vendor.JUNIPER_JUNOS, source)
        self.assertEqual(result.text, source)
        self.assertEqual(result.removed, ())

    def test_normalization_may_return_empty_or_whitespace_only_text(self):
        self.assertEqual(
            normalize_input(Vendor.JUNIPER_JUNOS, "<J1>show configuration\n").text,
            "",
        )
        self.assertEqual(
            normalize_input(Vendor.JUNIPER_JUNOS, "<J1>show configuration\n\n").text,
            "\n",
        )

    def test_vendor_yaml_can_define_a_multiline_capture_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input_normalization.yaml"
            path.write_text(
                "version: 1\n"
                "vendor: cisco_iosxr\n"
                "rules:\n"
                "  - id: capture_tail\n"
                "    region: trailing\n"
                "    match:\n"
                "      - '^CAPTURE START$'\n"
                "      - '^CAPTURE END$'\n"
                "  - id: cisco.capture_header\n"
                "    region: leading\n"
                "    match:\n"
                "      - '^CAPTURE HEADER$'\n",
                encoding="utf-8",
            )
            with patch.dict(input_normalization._RULE_PATHS, {Vendor.CISCO_IOSXR: path}):
                result = normalize_input(
                    Vendor.CISCO_IOSXR,
                    "CAPTURE HEADER\nhostname R1\nend\nCAPTURE START\nCAPTURE END\n",
                )
            self.assertEqual(result.text, "hostname R1\nend\n")
            self.assertEqual(
                [(item.rule_id, item.removed_lines) for item in result.removed],
                [("cisco.capture_header", 1), ("cisco.capture_tail", 2)],
            )

    def test_conversion_records_removed_lines_without_raw_content(self):
        root = FIXTURES / "junos_mlag"
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config_dir = workspace / "configs"
            shutil.copytree(root / "configs", config_dir)
            source_path = config_dir / "JA.cfg"
            source = source_path.read_text(encoding="utf-8")
            source_path.write_text(
                "<JA>show configuration\n## Last commit: secret operator\n"
                + source
                + "{\n master\n}\nuser@JA>\n",
                encoding="utf-8",
            )

            output = workspace / "output"
            context = convert(root / "topology.xlsx", config_dir, output)

            self.assertFalse(context.has_errors, context.errors)
            rendered = (output / "configs" / "JA.cfg").read_text(encoding="utf-8")
            self.assertNotIn("master", rendered)
            self.assertNotIn("show configuration", rendered)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            events = [item for item in report["events"] if item["kind"] == "input-normalization"]
            self.assertEqual({item["rule_id"] for item in events}, {
                "juniper.command_echo", "juniper.last_commit", "juniper.master_tail", "juniper.cli_prompt"
            })
            master_event = next(item for item in events if item["rule_id"] == "juniper.master_tail")
            self.assertEqual(master_event["removed_lines"], 3)
            self.assertNotIn("secret operator", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
