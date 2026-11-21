"""服务层端到端测试：冻结批次、k=5 裁决、撤下、幂等、中断恢复。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchguard.constants import (
    RELEASE_WITHDRAWN,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_REJECTED,
)
from benchguard.db import Database
from benchguard.repo import (
    ServiceError,
    adjudicate_next,
    application_detail,
    create_application,
    historical_map,
    list_applications,
    list_release_codes,
    public_releases,
    recover_interrupted,
    withdraw_release,
)
from benchguard.seed import PART_COUNT, seed


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "test.db"))
        self.assertEqual(seed(self.db), "seeded")
        conn = self.db.connect()
        try:
            self.applicant = conn.execute(
                "SELECT * FROM users WHERE username='applicant'"
            ).fetchone()
            self.maintainer = conn.execute(
                "SELECT * FROM users WHERE username='maintainer'"
            ).fetchone()
            parts = conn.execute("SELECT id, serial FROM parts ORDER BY id").fetchall()
            self.pid = {r["serial"]: r["id"] for r in parts}
        finally:
            conn.close()
        # 种子历史发布应全部满足门槛（无遗留 processing）
        self.assertEqual(list_release_codes(self.db.connect()), ["R-0001", "R-0002", "R-0003"])

    def tearDown(self):
        self.tmp.cleanup()

    def _ids(self, *serials):
        return [self.pid[s] for s in serials]

    def _drain(self):
        while adjudicate_next(self.db):
            pass

    # ---- 场景 1：同构组 10 件选 5 件 → 通过，公开数字同事务生效 ----
    def test_approved_release_publishes_atomically(self):
        chosen = self._ids("AC-001", "AC-002", "AC-003", "AC-004", "AC-005")
        app = create_application(
            self.db, self.applicant, "key-ok", "五件同组申请",
            [{"part_id": pid, "failed": i % 2} for i, pid in enumerate(chosen)],
        )
        self.assertEqual(app["status"], STATUS_PENDING)
        self.assertTrue(adjudicate_next(self.db))
        conn = self.db.connect()
        try:
            decided = conn.execute(
                "SELECT status FROM applications WHERE id=?", (app["id"],)
            ).fetchone()
            self.assertEqual(decided["status"], STATUS_APPROVED)
            rel = conn.execute("SELECT * FROM releases ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(rel["code"], "R-0004")
            # 公开数字直接由 inclusion 派生，与登记同源同时刻
            pub = {r["code"]: r for r in public_releases(conn)}
            self.assertEqual(pub["R-0004"]["sample_size"], 5)
            self.assertEqual(pub["R-0004"]["failure_count"], 2)
        finally:
            conn.close()

    # ---- 场景 2：单件候选 → 独自成组 1 件 → 拒发，原因可查且不含部件身份 ----
    def test_single_part_application_rejected(self):
        app = create_application(
            self.db, self.applicant, "key-single", "单件试探",
            [{"part_id": self.pid["AC-031"], "failed": 1}],
        )
        adjudicate_next(self.db)
        conn = self.db.connect()
        try:
            detail = application_detail(conn, app["id"], self.applicant)
        finally:
            conn.close()
        self.assertEqual(detail["application"]["status"], STATUS_REJECTED)
        reason = detail["application"]["reject_reason"]
        self.assertIn("仅 1 件", reason)
        self.assertNotIn("AC-031", reason)  # 拒发原因不泄露部件身份
        self.assertIsNone(detail["release"])

    # ---- 场景 3：从 10 件历史组挑 2 件 → 拆出 2 件小组 → 拒发 ----
    def test_splitting_historical_group_below_k_rejected(self):
        chosen = self._ids("AC-001", "AC-002")
        app = create_application(
            self.db, self.applicant, "key-split", "拆组试探",
            [{"part_id": pid, "failed": 0} for pid in chosen],
        )
        adjudicate_next(self.db)
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT status, reject_reason FROM applications WHERE id=?",
                (app["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], STATUS_REJECTED)
        # 旧组剩余 8 件仍 ≥5，违规的是被拆出的 2 件新组合
        self.assertIn("仅 2 件", row["reject_reason"])

    # ---- 场景 4：幂等重试返回原申请，绝不重复发布 ----
    def test_idempotent_retry_no_duplicate_release(self):
        payload = [
            {"part_id": pid, "failed": 0}
            for pid in self._ids("AC-031", "AC-032", "AC-033", "AC-034", "AC-035")
        ]
        first = create_application(self.db, self.applicant, "idem-1", "幂等测试", payload)
        again = create_application(self.db, self.applicant, "idem-1", "幂等测试", payload)
        self.assertEqual(first["id"], again["id"])
        adjudicate_next(self.db)
        # 再提交同键：原申请已批准，仍返回同一条，不新增发布
        third = create_application(self.db, self.applicant, "idem-1", "幂等测试", payload)
        self.assertEqual(third["id"], first["id"])
        conn = self.db.connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"], 4)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"], 4
            )  # 3 个种子申请 + 1
        finally:
            conn.close()

    # ---- 场景 5：撤下后公开页撤数字，但历史向量仍含该发布 ----
    def test_withdrawn_release_kept_in_privacy_check(self):
        conn = self.db.connect()
        try:
            r3 = conn.execute("SELECT * FROM releases WHERE code='R-0003'").fetchone()
            self.assertEqual(r3["status"], RELEASE_WITHDRAWN)
            # 撤下发布仍在历史口径中
            self.assertIn("R-0003", list_release_codes(conn))
            self.assertIn("R-0003", historical_map(conn)[self.pid["AC-001"]])
        finally:
            conn.close()

        # 机务撤下一份在架发布
        conn = self.db.connect()
        try:
            r1 = conn.execute("SELECT * FROM releases WHERE code='R-0001'").fetchone()
        finally:
            conn.close()
        withdraw_release(self.db, self.maintainer, r1["id"])
        conn = self.db.connect()
        try:
            codes = {r["code"] for r in public_releases(conn)}
            self.assertNotIn("R-0001", codes)  # 公开数字撤下
            self.assertIn("R-0001", list_release_codes(conn))  # 核对仍在
        finally:
            conn.close()

    # ---- 场景 6：中断恢复 processing → pending，重审不重复 ----
    def test_recovery_after_interruption(self):
        app = create_application(
            self.db, self.applicant, "key-crash", "中断测试",
            [{"part_id": self.pid["AC-031"], "failed": 0}],
        )
        conn = self.db.connect()
        try:
            conn.execute(
                "UPDATE applications SET status=? WHERE id=?",
                (STATUS_PROCESSING, app["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        recovered = recover_interrupted(self.db)
        self.assertEqual(recovered, 1)
        adjudicate_next(self.db)
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT status FROM applications WHERE id=?", (app["id"],)
            ).fetchone()
            self.assertEqual(row["status"], STATUS_REJECTED)
            # 没有产生任何发布
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM releases WHERE code='R-0004'").fetchone()["c"], 0
            )
        finally:
            conn.close()

    # ---- 场景 7：非冻结批次件（不存在的部件）拒收 ----
    def test_foreign_batch_part_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            create_application(
                self.db, self.applicant, "key-foreign", "批次外选件",
                [{"part_id": 999_999, "failed": 0}],
            )
        self.assertEqual(ctx.exception.status, 400)

    # ---- 场景 8：重复件、空申请、非法失效标记 ----
    def test_input_validation(self):
        with self.assertRaises(ServiceError):
            create_application(self.db, self.applicant, "k1", "空", [])
        with self.assertRaises(ServiceError):
            create_application(
                self.db, self.applicant, "k2", "重复",
                [
                    {"part_id": self.pid["AC-001"], "failed": 0},
                    {"part_id": self.pid["AC-001"], "failed": 1},
                ],
            )
        with self.assertRaises(ServiceError):
            create_application(
                self.db, self.applicant, "k3", "非法标记",
                [{"part_id": self.pid["AC-001"], "failed": 2}],
            )

    # ---- 场景 9：串行裁决多份申请，按 FIFO 逐份定案 ----
    def test_fifo_serial_adjudication(self):
        for n, serial in enumerate(("AC-031", "AC-032", "AC-033")):
            create_application(
                self.db, self.applicant, f"fifo-{n}", f"队列 {n}",
                [{"part_id": self.pid[serial], "failed": 0}],
            )
        self._drain()
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT id, status FROM applications WHERE idempotency_key LIKE 'fifo-%' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([r["status"] for r in rows], [STATUS_REJECTED] * 3)

    # ---- 场景 10：从未入选的 10 件中取 5 件 → 5+5 两组均达标，通过 ----
    def test_never_selected_group_split_into_two_groups_of_five(self):
        chosen = self._ids("AC-031", "AC-032", "AC-033", "AC-034", "AC-035")
        app = create_application(
            self.db, self.applicant, "key-never", "未入选组取五件",
            [{"part_id": pid, "failed": 1} for pid in chosen],
        )
        adjudicate_next(self.db)
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT status FROM applications WHERE id=?", (app["id"],)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], STATUS_APPROVED)

    def test_seed_is_idempotent_and_part_count(self):
        self.assertEqual(seed(self.db), "already-seeded")
        conn = self.db.connect()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) c FROM parts").fetchone()["c"], PART_COUNT
            )
            apps = list_applications(conn, self.applicant)
            self.assertEqual(len(apps), 3)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
