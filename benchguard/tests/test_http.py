"""HTTP 接口测试：鉴权、角色强校验、公开页、撤下、并发不重复。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchguard.db import Database
from benchguard.seed import seed
from benchguard.server import build_server


class HttpClient:
    def __init__(self, base: str):
        self.base = base
        self.token: str | None = None

    def call(self, method: str, path: str, body=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def login(self, username: str, password: str):
        status, data = self.call("POST", "/api/auth/login",
                                 {"username": username, "password": password})
        if status == 200:
            # 从响应头取不到 cookie（urllib 不回传 Set-Cookie 到这里），改用登录返回后
            # 直接查库拿 token 的替代方案：再调用一次带 cookie 处理
            pass
        return status, data


class HttpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "http.db"))
        seed(self.db)
        self.server = build_server(self.db, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.worker.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _client(self) -> HttpClient:
        return HttpClient(self.base)

    def _login(self, client: HttpClient, username: str, password: str):
        # urllib 需要手动保存 Set-Cookie
        data = json.dumps({"username": username, "password": password}).encode()
        req = urllib.request.Request(self.base + "/api/auth/login", data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as res:
            cookie = res.headers["Set-Cookie"]
            payload = json.loads(res.read().decode())
        client.token = cookie.split("session=")[1].split(";")[0]
        return payload["user"]

    def _wait_status(self, client: HttpClient, app_id: int, expected: str, timeout: float = 5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, data = client.call("GET", f"/api/applications/{app_id}")
            if data["application"]["status"] == expected:
                return data
            if data["application"]["status"] in ("approved", "rejected"):
                return data
            time.sleep(0.1)
        raise AssertionError("裁决超时")

    def test_health_and_public_anonymous(self):
        client = self._client()
        status, data = client.call("GET", "/api/health")
        self.assertEqual(status, 200)
        status, data = client.call("GET", "/api/public")
        self.assertEqual(status, 200)
        codes = [r["code"] for r in data["listed_releases"]]
        self.assertIn("R-0001", codes)
        self.assertNotIn("R-0003", codes)  # 撤下发布不公开数字
        self.assertTrue(any(r["code"] == "R-0003" for r in data["withdrawn_releases"]))

    def test_roster_requires_maintainer(self):
        client = self._client()
        # 匿名
        status, _ = client.call("GET", "/api/parts")
        self.assertEqual(status, 401)
        # 申请单位角色被服务端拒绝（按钮显隐不替代校验）
        self._login(client, "applicant", "Apply123!")
        status, data = client.call("GET", "/api/parts")
        self.assertEqual(status, 403)
        self.assertIn("机务", data["error"])
        # 机务可查
        mc = self._client()
        self._login(mc, "maintainer", "Mech123!")
        status, data = mc.call("GET", "/api/parts")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 40)

    def test_apply_flow_rejection_reason_and_withdraw(self):
        client = self._client()
        self._login(client, "applicant", "Apply123!")
        # 单件申请 → 拒发，页面可取原因
        status, data = client.call("POST", "/api/applications", {
            "idempotency_key": "http-single",
            "title": "HTTP 单件",
            "items": [{"part_id": 31, "failed": 1}],
        })
        self.assertEqual(status, 201)
        app_id = data["application"]["id"]
        detail = self._wait_status(client, app_id, "rejected")
        self.assertEqual(detail["application"]["status"], "rejected")
        self.assertIn("至少 5 件", detail["application"]["reject_reason"])

        # 合规申请（未入选组 5 件）→ 通过 → 公开页出现新数字
        status, data = client.call("POST", "/api/applications", {
            "idempotency_key": "http-ok",
            "title": "HTTP 合规",
            "items": [{"part_id": pid, "failed": 1} for pid in range(31, 36)],
        })
        app_id = data["application"]["id"]
        detail = self._wait_status(client, app_id, "approved")
        self.assertEqual(detail["release"]["code"], "R-0004")
        _, pub = client.call("GET", "/api/public")
        r4 = next(r for r in pub["listed_releases"] if r["code"] == "R-0004")
        self.assertEqual((r4["sample_size"], r4["failure_count"]), (5, 5))

        # 申请单位撤下自己的发布
        status, data = client.call("POST", f"/api/releases/{detail['release']['id']}/withdraw")
        self.assertEqual(status, 200)
        _, pub = client.call("GET", "/api/public")
        self.assertFalse(any(r["code"] == "R-0004" for r in pub["listed_releases"]))
        # 重复撤下报错
        status, data = client.call("POST", f"/api/releases/{detail['release']['id']}/withdraw")
        self.assertEqual(status, 400)

    def test_concurrent_submissions_each_decided_once(self):
        client = self._client()
        self._login(client, "applicant", "Apply123!")
        results = []

        def submit(idx):
            c = HttpClient(self.base)
            self._login(c, "applicant", "Apply123!")
            status, data = c.call("POST", "/api/applications", {
                "idempotency_key": f"conc-{idx}",
                "title": f"并发 {idx}",
                "items": [{"part_id": 31 + (idx % 10), "failed": 0}],
            })
            results.append((status, data["application"]["id"]))

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 6)
        self.assertEqual(len({app_id for _, app_id in results}), 6)
        # 等待裁决排空
        deadline = time.time() + 5
        while time.time() < deadline:
            _, data = client.call("GET", "/api/applications")
            if all(a["status"] in ("approved", "rejected") for a in data["applications"]
                   if a["idempotency_key"].startswith("conc-")):
                break
            time.sleep(0.1)
        _, data = client.call("GET", "/api/applications")
        conc = [a for a in data["applications"] if a["idempotency_key"].startswith("conc-")]
        self.assertEqual(len(conc), 6)
        self.assertTrue(all(a["status"] == "rejected" for a in conc))

    def test_audit_only_maintainer(self):
        client = self._client()
        self._login(client, "applicant", "Apply123!")
        status, _ = client.call("GET", "/api/audit")
        self.assertEqual(status, 403)
        mc = self._client()
        self._login(mc, "maintainer", "Mech123!")
        status, data = mc.call("GET", "/api/audit")
        self.assertEqual(status, 200)
        self.assertGreater(len(data["audit_logs"]), 0)


if __name__ == "__main__":
    unittest.main()
