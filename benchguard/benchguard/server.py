"""零依赖 HTTP 服务（标准库 http.server）。

路由：
  POST /api/auth/login|logout   GET /api/auth/me
  GET  /api/public              公开数字（在架发布的样本数/失效数；撤下只列编码）
  GET  /api/parts               部件名单（仅机务，服务端校验 + 审计）
  POST /api/applications        提交申请（幂等）
  GET  /api/applications        我的申请列表
  GET  /api/applications/<id>   申请详情含拒发原因
  POST /api/releases/<id>/withdraw  撤下（机务或原申请单位）
  GET  /api/audit               审计轨迹（仅机务）
"""
from __future__ import annotations

import json
import logging
import os
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .constants import ROLE_MAINTAINER
from .db import Database
from .repo import (
    ServiceError,
    application_detail,
    create_application,
    list_applications,
    login,
    logout,
    public_releases,
    roster,
    user_for_token,
    withdraw_release,
    withdrawn_releases,
)
from .worker import AdjudicationWorker

log = logging.getLogger("benchguard.server")
WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def row_to_dict(row) -> dict | None:
    return dict(row) if row is not None else None


class Handler(BaseHTTPRequestHandler):
    server_version = "benchguard/1.0"

    # ---- 基础设施 ----
    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, payload, status: int = 200, extra_headers: dict | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ServiceError("请求体过大", 413)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode())
        except json.JSONDecodeError as exc:
            raise ServiceError(f"JSON 解析失败: {exc}", 400) from exc
        if not isinstance(data, dict):
            raise ServiceError("请求体必须是 JSON 对象", 400)
        return data

    def _token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "session" in cookie:
            return cookie["session"].value
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    def _current_user(self):
        return user_for_token(self.server.db, self._token())

    def _require_user(self):
        user = self._current_user()
        if user is None:
            raise ServiceError("未登录或会话已过期", 401)
        return user

    def _require_role(self, role: str):
        user = self._require_user()
        if user["role"] != role:
            raise ServiceError("角色权限不足", 403)
        return user

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/"):
                self._route_get(path)
            else:
                self._serve_static(path)
        except ServiceError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except Exception:
            log.exception("GET %s 失败", path)
            self._send_json({"error": "服务器内部错误"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            self._route_post(path)
        except ServiceError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except Exception:
            log.exception("POST %s 失败", path)
            self._send_json({"error": "服务器内部错误"}, 500)

    def _route_get(self, path: str) -> None:
        if path == "/api/health":
            self._send_json({"status": "ok"})
            return
        if path == "/api/auth/me":
            user = self._current_user()
            self._send_json({"user": self._public_user(user)})
            return
        if path == "/api/public":
            conn = self.server.db.connect()
            try:
                listed = [dict(r) for r in public_releases(conn)]
                gone = [dict(r) for r in withdrawn_releases(conn)]
            finally:
                conn.close()
            self._send_json(
                {
                    "listed_releases": listed,
                    "withdrawn_releases": [
                        {"code": r["code"], "withdrawn_at": r["withdrawn_at"]}
                        for r in gone
                    ],
                    "note": "撤下发布的历史数字不再公开展示，但其入选记录仍参与匿名核对。",
                }
            )
            return
        if path == "/api/parts":
            user = self._require_user()
            parts = [dict(r) for r in roster(self.server.db, user)]
            self._send_json({"parts": parts, "total": len(parts)})
            return
        if path == "/api/applications":
            user = self._require_user()
            conn = self.server.db.connect()
            try:
                apps = [dict(r) for r in list_applications(conn, user)]
            finally:
                conn.close()
            self._send_json({"applications": apps})
            return
        if path.startswith("/api/applications/"):
            app_id = self._int_id(path.rsplit("/", 1)[-1])
            user = self._require_user()
            conn = self.server.db.connect()
            try:
                detail = application_detail(conn, app_id, user)
            finally:
                conn.close()
            self._send_json(
                {
                    "application": dict(detail["application"]),
                    "items": [dict(r) for r in detail["items"]],
                    "release": row_to_dict(detail["release"]),
                }
            )
            return
        if path == "/api/audit":
            self._require_role(ROLE_MAINTAINER)
            conn = self.server.db.connect()
            try:
                logs = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM audit_logs ORDER BY id DESC LIMIT 200"
                    ).fetchall()
                ]
            finally:
                conn.close()
            self._send_json({"audit_logs": logs})
            return
        raise ServiceError("接口不存在", 404)

    def _route_post(self, path: str) -> None:
        if path == "/api/auth/login":
            data = self._read_json()
            user, token = login(
                self.server.db, str(data.get("username", "")), str(data.get("password", ""))
            )
            self._send_json(
                {"user": self._public_user(user)},
                extra_headers={"Set-Cookie": f"session={token}; HttpOnly; Path=/; SameSite=Lax"},
            )
            return
        if path == "/api/auth/logout":
            token = self._token()
            if token:
                logout(self.server.db, token)
            self._send_json({"ok": True}, extra_headers={"Set-Cookie": "session=; Max-Age=0; Path=/"})
            return
        if path == "/api/applications":
            user = self._require_user()
            data = self._read_json()
            app = create_application(
                self.server.db,
                user,
                str(data.get("idempotency_key", "")),
                str(data.get("title", "")),
                list(data.get("items", [])),
            )
            self._send_json({"application": dict(app)}, 201)
            return
        if path.startswith("/api/releases/") and path.endswith("/withdraw"):
            user = self._require_user()
            release_id = self._int_id(path.split("/")[3])
            rel = withdraw_release(self.server.db, user, release_id)
            self._send_json({"release": dict(rel)})
            return
        raise ServiceError("接口不存在", 404)

    @staticmethod
    def _int_id(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ServiceError("ID 必须是整数", 400) from exc
        if value <= 0:
            raise ServiceError("ID 非法", 400)
        return value

    @staticmethod
    def _public_user(user) -> dict | None:
        if user is None:
            return None
        return {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
        }

    # ---- 静态资源 ----
    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = os.path.normpath(os.path.join(WEB_ROOT, rel))
        if not target.startswith(WEB_ROOT) or not os.path.isfile(target):
            target = os.path.join(WEB_ROOT, "index.html")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(target)[1], "application/octet-stream")
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_server(db: Database, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.db = db  # type: ignore[attr-defined]
    worker = AdjudicationWorker(db)
    server.worker = worker  # type: ignore[attr-defined]
    worker.start()
    return server
