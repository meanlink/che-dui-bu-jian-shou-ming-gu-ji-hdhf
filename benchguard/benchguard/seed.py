"""幂等种子：一个冻结批次、40 件部件、三份历史发布（含一份已撤下）。

历史发布的分组结果满足 k=5：
  组 A（R-0001+R-0002+R-0003）：p01..p10，10 件
  组 B（R-0001+R-0003）       ：p11..p20，10 件
  组 C（R-0002+R-0003）       ：p21..p30，10 件
  组 D（从未入选，豁免）       ：p31..p40，10 件
R-0003 演示"已撤下仍参与核对"。重复执行不覆盖既有数据。
"""
from __future__ import annotations

import logging

from .constants import RELEASE_WITHDRAWN
from .db import Database
from .security import hash_password

log = logging.getLogger("benchguard.seed")

BATCH_CODE = "FB-2026-Q4"
PART_COUNT = 40

USERS = [
    ("maintainer", "机务核验员", "maintainer", "Mech123!"),
    ("applicant", "对标申请单位", "applicant", "Apply123!"),
]

# (发布编码, 入选部件序号区间, 失效件序号, 状态)
HISTORICAL_RELEASES = [
    ("R-0001", range(1, 21), {2, 7, 13, 18}, "listed"),
    ("R-0002", list(range(1, 11)) + list(range(21, 31)), {4, 9, 24, 29}, "listed"),
    ("R-0003", range(1, 31), {3, 8, 14, 19, 25}, RELEASE_WITHDRAWN),
]


def seed(db: Database) -> str:
    with db.txn() as conn:
        if conn.execute("SELECT 1 FROM meta WHERE id=1").fetchone():
            return "already-seeded"

        cur = conn.execute(
            "INSERT INTO batches(code, part_count) VALUES(?,?)",
            (BATCH_CODE, PART_COUNT),
        )
        batch_id = cur.lastrowid
        for n in range(1, PART_COUNT + 1):
            serial = f"AC-{n:03d}"
            model = "AC-8" if n <= 20 else "AC-12"
            conn.execute(
                "INSERT INTO parts(batch_id, serial, model) VALUES(?,?,?)",
                (batch_id, serial, model),
            )
        conn.execute(
            "INSERT INTO meta(id, frozen_batch_id, k_anonymity) VALUES(1,?,5)",
            (batch_id,),
        )

        for username, display, role, password in USERS:
            salt, pwd_hash = hash_password(password)
            conn.execute(
                "INSERT INTO users(username, display_name, role, pwd_salt, pwd_hash)"
                " VALUES(?,?,?,?,?)",
                (username, display, role, salt, pwd_hash),
            )

        applicant = conn.execute(
            "SELECT id FROM users WHERE username='applicant'"
        ).fetchone()
        parts = {
            r["serial"]: r["id"]
            for r in conn.execute(
                "SELECT id, serial FROM parts WHERE batch_id=?", (batch_id,)
            ).fetchall()
        }

        for code, seq_range, fail_set, status in HISTORICAL_RELEASES:
            app_cur = conn.execute(
                "INSERT INTO applications(applicant_id, idempotency_key, title, status,"
                " decided_at) VALUES(?,?,?,?, datetime('now'))",
                (applicant["id"], f"seed-{code}", f"历史发布 {code}", "approved"),
            )
            app_id = app_cur.lastrowid
            rel_cur = conn.execute(
                "INSERT INTO releases(code, application_id, applicant_id, status,"
                " withdrawn_at) VALUES(?,?,?,?, CASE WHEN ?='withdrawn' THEN datetime('now') END)",
                (code, app_id, applicant["id"], status, status),
            )
            release_id = rel_cur.lastrowid
            for n in seq_range:
                serial = f"AC-{n:03d}"
                conn.execute(
                    "INSERT INTO inclusions(release_id, part_id, failed) VALUES(?,?,?)",
                    (release_id, parts[serial], 1 if n in fail_set else 0),
                )
                conn.execute(
                    "INSERT INTO application_items(application_id, part_id, failed)"
                    " VALUES(?,?,?)",
                    (app_id, parts[serial], 1 if n in fail_set else 0),
                )

        from .repo import audit

        audit(conn, None, "apply", f"种子初始化：批次 {BATCH_CODE}，{PART_COUNT} 件，3 份历史发布")
        return "seeded"
