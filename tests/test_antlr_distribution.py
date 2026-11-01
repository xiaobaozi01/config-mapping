"""运行包自带生成文件与语法；普通解析与规则执行不启动工具进程。"""

import unittest
from importlib import metadata, resources
from unittest.mock import patch

from impl.cisco import antlr as cisco_antlr
from impl.cisco.document import CiscoDocument
from impl.juniper import antlr as juniper_antlr
from impl.juniper.document import JunosDocument


class AntlrDistributionTest(unittest.TestCase):

    def test_pinned_runtime_and_generated_assets_are_available(self):
        self.assertEqual(metadata.version("antlr4-python3-runtime"), "4.13.2")
        packages = (
            (resources.files(cisco_antlr), "Cisco"),
            (resources.files(juniper_antlr), "Junos"),
        )
        for root, prefix in packages:
            for name in (f"{prefix}Lexer.g4", f"{prefix}Parser.g4"):
                self.assertTrue(root.joinpath(name).read_text())
            for suffix in (
                "Lexer.py",
                "Parser.py",
                "ParserVisitor.py",
                "ParserListener.py",
            ):
                text = root.joinpath(
                    "generated", f"{prefix}{suffix}"
                ).read_text()
                self.assertIn("ANTLR 4.13.2", text)

    def test_parsing_group_expansion_and_rule_execution_never_spawn_java(self):
        source = "group G\n interface 'GigabitEthernet.*'\n  mtu 9000\nend-group\napply-group G\ninterface Gi0/0/0/0\n"
        with patch(
            "subprocess.Popen",
            side_effect=AssertionError("runtime must not launch Java"),
        ):
            document = CiscoDocument(source)
            self.assertTrue(
                document.expand_groups(["GigabitEthernet0/0/0/0"]).success
            )
            document.rename_interface_tree("Gi0/0/0/0", "Gi0/0/0/1")
            document.adapt_interfaces({"Gi0/0/0/1"})
            self.assertIn("interface GigabitEthernet0/0/0/1", document.render())
            junos = JunosDocument("system { host-name lab; }\n")
            self.assertEqual(junos.render(), "system { host-name lab; }\n")


if __name__ == "__main__":
    unittest.main()
