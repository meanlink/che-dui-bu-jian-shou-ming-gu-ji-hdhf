"""数据访问与工作流服务。

裁决在单事务内完成：批准时 release + inclusion 与申请状态同事务提交，
保证"放行登记与公开数字同时生效"。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .constants import (
    AUDIT_APPROVE,
    AUDIT_REJECT,
    K_ANONYMITY,
    RELEASE_LISTED,
    RELEASE_WITHDRAWN,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_REJECTED,
)
from .db import Database
from .privacy import build_vectors, evaluate_groups, rejection_message
from .security import new_token, verify_password


class ServiceError(Exception):
    """业务校验错误，携带 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------- 元信息与冻结批次 ----------

def get_frozen_batch(db: Database, conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT b.* FROM meta m JOIN batches b ON b.id = m.frozen_batch_id WHERE m.id = 1"
    ).fetchone()
    if row is None:
        raise ServiceError("系统尚未初始化冻结批次", 503)
    return row


def list_batch_parts(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, serial, model FROM parts WHERE batch_id=? ORDER BY serial",
        (batch_id,),
    ).fetchall()


# ---------- 历史发布口径（含已撤下） ----------

def list_release_codes(conn: sqlite3.Connection) -> list[str]:
    """全部历次发布编码，按发布顺序；撤下的发布同样参与核对。"""
    rows = conn.execute("SELECT code FROM releases ORDER BY id").fetchall()
    return [r["code"] for r in rows]


def historical_map(conn: sqlite3.Connection) -> dict[int, set[str]]:
    rows = conn.execute(
        """
        SELECT i.part_id AS pid, r.code AS code
        FROM inclusions i JOIN releases r ON r.id = i.release_id
        """
    ).fetchall()
    hist: dict[int, set[str]] = {}
    for r in rows:
        hist.setdefault(r["pid"], set()).add(r["code"])
    return hist


# ---------- 认证 ----------

def login(db: Database, username: str, password: str) -> tuple[sqlite3.Row, str]:
    with db.adjudicating:
        conn = db.connect()
        try:
            user = conn.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
            if user is None or not verify_password(
                password, user["pwd_salt"], user["pwd_hash"]
            ):
                raise ServiceError("用户名或口令错误", 401)
            token = new_token()
            expires = (datetime.now(timezone.utc) + timedelta(hours=12)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conn.execute(
                "INSERT INTO sessions(token, user_id, expires_at) VALUES(?,?,?)",
                (token, user["id"], expires),
            )
            audit(conn, user["id"], "login", f"用户 {username} 登录")
            conn.commit()
            return user, token
        finally:
            conn.close()


def user_for_token(db: Database, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    conn = db.connect()
    try:
        return conn.execute(
            """
            SELECT u.* FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token=? AND s.expires_at > datetime('now')
            """,
            (token,),
        ).fetchone()
    finally:
        conn.close()


def logout(db: Database, token: str) -> None:
    with db.adjudicating:
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT user_id FROM sessions WHERE token=?", (token,)
            ).fetchone()
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            if row:
                audit(conn, row["user_id"], "logout", "用户登出")
            conn.commit()
        finally:
            conn.close()


# ---------- 审计 ----------

def audit(
    conn: sqlite3.Connection, actor_id: int | None, action: str, detail: str
) -> None:
    conn.execute(
        "INSERT INTO audit_logs(actor_id, action, detail) VALUES(?,?,?)",
        (actor_id, action, detail[:500]),
    )


# ---------- 申请提交（幂等） ----------

def create_application(
    db: Database,
    applicant: sqlite3.Row,
    idempotency_key: str,
    title: str,
    items: list[dict],
) -> sqlite3.Row:
    """登记申请为 pending；同一申请人重复幂等键返回原申请，不重复发布。"""
    if not idempotency_key or len(idempotency_key) > 80:
        raise ServiceError("idempotency_key 必填且不超过 80 字符")
    if not title or not title.strip():
        raise ServiceError("申请标题必填")
    if not items:
        raise ServiceError("申请至少包含一个部件")

    seen: set[int] = set()
    clean: list[tuple[int, int]] = []
    for it in items:
        pid = it.get("part_id")
        failed = it.get("failed")
        if not isinstance(pid, int) or failed not in (0, 1):
            raise ServiceError("选件条目非法：需要整数 part_id 与 0/1 的 failed")
        if pid in seen:
            raise ServiceError(f"部件 {pid} 在同一申请中重复")
        seen.add(pid)
        clean.append((pid, int(failed)))

    with db.txn() as conn:
        batch = get_frozen_batch(db, conn)
        # 全部选件必须来自同一冻结批次
        placeholders = ",".join("?" for _ in clean)
        rows = conn.execute(
            f"SELECT id FROM parts WHERE batch_id=? AND id IN ({placeholders})",
            (batch["id"], *[p for p, _ in clean]),
        ).fetchall()
        if len(rows) != len(clean):
            raise ServiceError("存在不属于当前冻结批次的部件，拒绝选件")

        existing = conn.execute(
            "SELECT * FROM applications WHERE applicant_id=? AND idempotency_key=?",
            (applicant["id"], idempotency_key),
        ).fetchone()
        if existing is not None:
            # 重试：原样返回，绝不重复登记
            return existing

        cur = conn.execute(
            "INSERT INTO applications(applicant_id, idempotency_key, title, status)"
            " VALUES(?,?,?,?)",
            (applicant["id"], idempotency_key, title.strip()[:200], STATUS_PENDING),
        )
        app_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO application_items(application_id, part_id, failed) VALUES(?,?,?)",
            [(app_id, pid, failed) for pid, failed in clean],
        )
        audit(
            conn,
            applicant["id"],
            "apply",
            f"登记申请 #{app_id}《{title.strip()[:80]}》，{len(clean)} 件，待裁决",
        )
        return conn.execute(
            "SELECT * FROM applications WHERE id=?", (app_id,)
        ).fetchone()


# ---------- 裁决 ----------

def recover_interrupted(db: Database) -> int:
    """中断恢复：上次进程在裁决中崩溃的申请回到 pending 重新排队。"""
    with db.txn() as conn:
        cur = conn.execute(
            "UPDATE applications SET status=? WHERE status=?",
            (STATUS_PENDING, STATUS_PROCESSING),
        )
        return cur.rowcount


def _next_release_code(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT code FROM releases ORDER BY id DESC LIMIT 1"
    ).fetchone()
    nxt = 1 if row is None else int(row["code"].split("-")[1]) + 1
    return f"R-{nxt:04d}"


def adjudicate_next(db: Database) -> bool:
    """串行裁决队首一份申请。返回是否处理了一份。

    并发申请逐份裁决：本函数由单守护线程持有 db.adjudicating 锁调用；
    HTTP 层不直接裁决。批准时 release/inclusion 与申请状态同事务提交。
    """
    with db.txn() as conn:
        app = conn.execute(
            "SELECT * FROM applications WHERE status=? ORDER BY id LIMIT 1",
            (STATUS_PENDING,),
        ).fetchone()
        if app is None:
            return False
        conn.execute(
            "UPDATE applications SET status=? WHERE id=?",
            (STATUS_PROCESSING, app["id"]),
        )

        batch = get_frozen_batch(db, conn)
        parts = list_batch_parts(conn, batch["id"])
        all_ids = [p["id"] for p in parts]
        items = conn.execute(
            "SELECT part_id, failed FROM application_items WHERE application_id=?",
            (app["id"],),
        ).fetchall()
        candidate = {r["part_id"] for r in items}
        codes = list_release_codes(conn)
        hist = historical_map(conn)

        vectors = build_vectors(all_ids, codes, hist, candidate)
        violations = evaluate_groups(vectors, codes, K_ANONYMITY)

        if violations:
            reason = rejection_message(violations, K_ANONYMITY)
            conn.execute(
                "UPDATE applications SET status=?, reject_reason=?, decided_at=datetime('now')"
                " WHERE id=?",
                (STATUS_REJECTED, reason, app["id"]),
            )
            audit(
                conn,
                app["applicant_id"],
                AUDIT_REJECT,
                f"申请 #{app['id']} 拒发：{len(violations)} 个入选组合组不足 {K_ANONYMITY} 件",
            )
            return True

        code = _next_release_code(conn)
        cur = conn.execute(
            "INSERT INTO releases(code, application_id, applicant_id, status)"
            " VALUES(?,?,?,?)",
            (code, app["id"], app["applicant_id"], RELEASE_LISTED),
        )
        release_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO inclusions(release_id, part_id, failed) VALUES(?,?,?)",
            [(release_id, r["part_id"], r["failed"]) for r in items],
        )
        conn.execute(
            "UPDATE applications SET status=?, decided_at=datetime('now') WHERE id=?",
            (STATUS_APPROVED, app["id"]),
        )
        audit(
            conn,
            app["applicant_id"],
            AUDIT_APPROVE,
            f"申请 #{app['id']} 通过，登记发布 {code}，样本 {len(items)} 件",
        )
        return True


# ---------- 撤下（行保留，仍参与核对） ----------

def withdraw_release(
    db: Database, actor: sqlite3.Row, release_id: int
) -> sqlite3.Row:
    with db.txn() as conn:
        rel = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if rel is None:
            raise ServiceError("发布不存在", 404)
        if rel["status"] == RELEASE_WITHDRAWN:
            raise ServiceError("该发布已撤下")
        conn.execute(
            "UPDATE releases SET status=?, withdrawn_at=datetime('now') WHERE id=?",
            (RELEASE_WITHDRAWN, release_id),
        )
        audit(
            conn,
            actor["id"],
            "withdraw",
            f"撤下发布 {rel['code']}（数据保留并继续参与匿名核对）",
        )
        return conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()


# ---------- 查询 ----------

def public_releases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """公开页：仅在架发布，数字直接由 inclusion 派生（与登记同表同源）。"""
    return conn.execute(
        """
        SELECT r.id, r.code, r.status, r.created_at,
               COUNT(i.part_id) AS sample_size,
               SUM(i.failed)    AS failure_count
        FROM releases r LEFT JOIN inclusions i ON i.release_id = r.id
        WHERE r.status = 'listed'
        GROUP BY r.id ORDER BY r.id
        """
    ).fetchall()


def withdrawn_releases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, code, created_at, withdrawn_at FROM releases"
        " WHERE status='withdrawn' ORDER BY id"
    ).fetchall()


def list_applications(conn: sqlite3.Connection, user: sqlite3.Row) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM applications WHERE applicant_id=? ORDER BY id DESC",
        (user["id"],),
    ).fetchall()


def application_detail(
    conn: sqlite3.Connection, application_id: int, user: sqlite3.Row
) -> dict:
    app = conn.execute(
        "SELECT * FROM applications WHERE id=?", (application_id,)
    ).fetchone()
    if app is None:
        raise ServiceError("申请不存在", 404)
    if app["applicant_id"] != user["id"] and user["role"] != "maintainer":
        raise ServiceError("无权查看该申请", 403)
    items = conn.execute(
        """
        SELECT p.id AS part_id, p.serial, p.model, ai.failed
        FROM application_items ai JOIN parts p ON p.id = ai.part_id
        WHERE ai.application_id=? ORDER BY p.serial
        """,
        (application_id,),
    ).fetchall()
    rel = conn.execute(
        "SELECT * FROM releases WHERE application_id=?", (application_id,)
    ).fetchone()
    return {"application": app, "items": items, "release": rel}


def roster(db: Database, actor: sqlite3.Row) -> list[sqlite3.Row]:
    """部件名单：仅机务核验角色，服务端强校验并留审计。"""
    if actor["role"] != "maintainer":
        raise ServiceError("部件名单仅机务核验角色可查", 403)
    with db.txn() as conn:
        batch = get_frozen_batch(db, conn)
        audit(conn, actor["id"], "view_roster", f"机务核验查看批次 {batch['code']} 部件名单")
        return list_batch_parts(conn, batch["id"])


def roster_for_seed(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return list_batch_parts(conn, batch_id)
