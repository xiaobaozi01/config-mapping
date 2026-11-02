"""源语义审计已移除，转换结果约束继续执行。"""

import unittest

from impl.cisco.document import CiscoDocument


class FinalIntegrityValidationTest(unittest.TestCase):
    """验证最终阶段只检查转换完整性和目标镜像约束。

    源配置语义不在这里重复执行。
    """

    def test_final_integrity_omits_source_semantics_but_keeps_target_limits(
        self,
    ):
        """最终校验不重复源语义，但目标镜像不兼容仍阻断。

        源语义问题不再单独审计，最终检查仍须拒绝目标不兼容属性。
        """
        document = CiscoDocument(
            "interface GigabitEthernet0/0/0/0\n"
            " mtu 1500\n"
            " mtu 9000\n"
            " speed 100\n"
            "!\n"
            "end\n"
        )

        outcome = document.validation.validate_final_integrity(
            {"GigabitEthernet0/0/0/0"},
            set(),
        )

        rendered = "\n".join(outcome.errors)
        self.assertNotIn("mtu 存在冲突", rendered)
        self.assertIn("残留不兼容属性", rendered)


if __name__ == "__main__":
    unittest.main()


def test_default_pipeline_has_no_source_audit_for_either_vendor(tmp_path):
    from pathlib import Path
    from unittest.mock import patch

    from adaptation.service import convert
    from impl.cisco.operations.validation import CiscoValidationOperations
    from impl.juniper.operations.validation import JunosValidationOperations

    fixtures = Path(__file__).parent / "fixtures"
    for fixture, operations in (
        ("iosxr_bundle", CiscoValidationOperations),
        ("junos_bundle", JunosValidationOperations),
    ):
        assert not hasattr(operations, "validate_source_semantics")
        for strict in (False, True):
            with patch.object(
                operations,
                "validate_source_semantics",
                create=True,
                side_effect=AssertionError("source audit executed"),
            ):
                context = convert(
                    fixtures / fixture / "topology.xlsx",
                    fixtures / fixture / "configs",
                    tmp_path / f"{fixture}-{strict}",
                    strict_input_validation=strict,
                )
            assert not context.has_errors
