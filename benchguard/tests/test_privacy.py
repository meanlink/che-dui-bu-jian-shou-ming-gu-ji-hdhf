"""隐私分组纯逻辑单元测试（不触碰数据库）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchguard.constants import K_ANONYMITY
from benchguard.privacy import (
    build_vectors,
    describe_vector,
    evaluate_groups,
    group_parts,
    rejection_message,
)

CODES = ["R-0001", "R-0002"]


class VectorTests(unittest.TestCase):
    def test_vector_includes_withdrawn_dimension(self):
        # 两个发布（第二个可能已撤下），撤下不影响向量构造
        hist = {1: {"R-0001"}, 2: {"R-0001", "R-0002"}}
        vecs = build_vectors([1, 2, 3], CODES, hist, candidate={3})
        self.assertEqual(vecs[1], (1, 0, 0))
        self.assertEqual(vecs[2], (1, 1, 0))
        self.assertEqual(vecs[3], (0, 0, 1))

    def test_grouping_by_full_combination(self):
        vecs = {1: (1, 0, 0), 2: (1, 0, 0), 3: (0, 0, 1)}
        groups = group_parts(vecs)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[(1, 0, 0)], [1, 2])

    def test_zero_vector_group_is_exempt(self):
        # 10 个从未入选件（全零组）+ 一个 4 件的候选组
        vecs = {pid: (0, 0, 0) for pid in range(1, 11)}
        for pid in range(11, 15):
            vecs[pid] = (0, 0, 1)
        violations = evaluate_groups(vecs, CODES, K_ANONYMITY)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["group_size"], 4)
        self.assertIn("本次申请", violations[0]["combination"])

    def test_group_of_five_passes(self):
        vecs = {pid: (1, 0, 1) for pid in range(1, 6)}
        vecs.update({pid: (0, 0, 0) for pid in range(6, 16)})
        self.assertEqual(evaluate_groups(vecs, CODES, K_ANONYMITY), [])

    def test_multiple_small_groups_all_reported(self):
        vecs = {1: (1, 0, 1), 2: (1, 0, 1)}          # 2 件
        vecs.update({3: (0, 1, 1), 4: (0, 1, 1)})    # 2 件
        vecs.update({pid: (0, 0, 0) for pid in range(5, 15)})
        violations = evaluate_groups(vecs, CODES, K_ANONYMITY)
        self.assertEqual(len(violations), 2)

    def test_describe_vector_never_identifies_parts(self):
        text = describe_vector((1, 0, 1), CODES)
        self.assertEqual(text, "R-0001 + 本次申请")
        self.assertNotIn("AC-", text)

    def test_rejection_message_contains_gap_and_withdrawn_note(self):
        violations = [
            {"vector": [0, 0, 1], "group_size": 3, "combination": "本次申请"}
        ]
        msg = rejection_message(violations, K_ANONYMITY)
        self.assertIn("至少 5 件", msg)
        self.assertIn("差额 2 件", msg)
        self.assertIn("已撤下", msg)


if __name__ == "__main__":
    unittest.main()
