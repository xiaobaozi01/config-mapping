"""M-LAG 克隆关系元数据的测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from config_adaptor.adaptation.service import prepare_context
from config_adaptor.adaptation.pipeline import build_default_pipeline


FIXTURES = Path(__file__).parent / "fixtures"


class MlagMetadataTest(unittest.TestCase):
    """验证M-LAG拆分时正确记录克隆关系元数据。"""

    def test_mlag_metadata_records_peer_interfaces(self) -> None:
        """M-LAG拆分时，记录完整的对端口列表。"""
        fixture_dir = FIXTURES / "iosxr_mlag"

        context = prepare_context(
            topology_path=fixture_dir / "topology.xlsx",
            config_dir=fixture_dir / "configs",
        )
        pipeline = build_default_pipeline()
        context = pipeline.execute(context)

        # 找到M-LAG相关的映射
        mlag_mappings = [
            m for m in context.mappings
            if m.is_mlag_clone and "Bundle" in m.source_interface
        ]

        # 应该存在M-LAG映射
        self.assertGreater(len(mlag_mappings), 0, "未找到M-LAG映射")

        # 检查元数据
        for mapping in mlag_mappings:
            # ✓ 标记为克隆
            self.assertTrue(mapping.is_mlag_clone)

            # ✓ 有克隆组ID
            self.assertIsNotNone(mapping.mlag_group_id)
            self.assertIn("mlag", mapping.mlag_group_id)

            # ✓ 有对端口列表
            self.assertGreater(len(mapping.mlag_peers), 1,
                             "M-LAG应该至少有2个对端口")

            # ✓ 目标口的父接口在对端列表中
            target_parent = mapping.target_interface.split('.')[0]
            self.assertIn(target_parent, mapping.mlag_peers,
                         f"目标口 {mapping.target_interface} 的父接口应在对端列表中")

    def test_mlag_group_id_consistent_within_group(self) -> None:
        """同一M-LAG组内的映射共享相同的group_id。"""
        fixture_dir = FIXTURES / "iosxr_mlag"

        context = prepare_context(
            topology_path=fixture_dir / "topology.xlsx",
            config_dir=fixture_dir / "configs",
        )
        pipeline = build_default_pipeline()
        context = pipeline.execute(context)

        # 按source_interface和mlag_group_id分组
        mlag_groups = {}
        for mapping in context.mappings:
            if mapping.is_mlag_clone:
                key = (mapping.source_interface, mapping.mlag_group_id)
                if key not in mlag_groups:
                    mlag_groups[key] = []
                mlag_groups[key].append(mapping)

        # 每个M-LAG组应该有多个映射（一个对应一个对端）
        for (source, group_id), mappings in mlag_groups.items():
            if len(mappings) > 1:
                # 同组内的所有映射应该有相同的peer列表
                first_peers = frozenset(mappings[0].mlag_peers)
                for mapping in mappings[1:]:
                    self.assertEqual(
                        frozenset(mapping.mlag_peers),
                        first_peers,
                        f"M-LAG组 {group_id} 的peer列表不一致"
                    )

    def test_non_mlag_interfaces_not_marked(self) -> None:
        """普通接口（非M-LAG）不应标记为克隆。"""
        fixture_dir = FIXTURES / "iosxr_bundle"

        context = prepare_context(
            topology_path=fixture_dir / "topology.xlsx",
            config_dir=fixture_dir / "configs",
        )
        pipeline = build_default_pipeline()
        context = pipeline.execute(context)

        # 检查NNI映射中没有被标记为M-LAG的
        nni_mappings = [m for m in context.mappings if m.role == "NNI"]

        # 不是所有NNI都是M-LAG
        non_mlag = [m for m in nni_mappings if not m.is_mlag_clone]
        self.assertGreater(
            len(non_mlag), 0,
            "应该存在非M-LAG的NNI映射"
        )

        # 检查非M-LAG映射没有对端口信息
        for mapping in non_mlag:
            if not mapping.is_mlag_clone:
                self.assertEqual(len(mapping.mlag_peers), 0)
                self.assertIsNone(mapping.mlag_group_id)


if __name__ == "__main__":
    unittest.main()
