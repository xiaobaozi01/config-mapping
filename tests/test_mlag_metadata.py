"""跨对端聚合不能在缺少MC-LAG支持时伪造克隆关系。"""

from pathlib import Path
import unittest

from adaptation.service import prepare_context
from adaptation.pipeline import build_default_pipeline

FIXTURES = Path(__file__).parent / "fixtures"


class MlagMetadataTest(unittest.TestCase):
    def test_cross_peer_aggregate_has_no_fabricated_clone_metadata(self):
        for fixture in ("iosxr_mlag", "junos_mlag"):
            with self.subTest(fixture=fixture):
                folder = FIXTURES / fixture
                context = prepare_context(folder / "topology.xlsx", folder / "configs")
                build_default_pipeline().execute(context)
                self.assertTrue(context.has_errors)
                self.assertTrue(
                    all("is_mlag_clone" not in mapping.to_dict() for mapping in context.mappings)
                )

    def test_single_peer_bundle_has_identity_logical_mapping(self):
        folder = FIXTURES / "iosxr_bundle"
        context = prepare_context(folder / "topology.xlsx", folder / "configs")
        build_default_pipeline().execute(context)
        self.assertFalse(context.has_errors)
        mapping = next(item for item in context.mappings if item.source_interface == "Bundle-Ether10")
        self.assertEqual(mapping.target_interface, "Bundle-Ether10")
        self.assertEqual(mapping.action, "preserve")
        serialized = mapping.to_dict()
        self.assertNotIn("is_mlag_clone", serialized)
        self.assertNotIn("mlag_group_id", serialized)
        self.assertNotIn("mlag_peers", serialized)


if __name__ == "__main__":
    unittest.main()
