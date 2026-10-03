"""HTTP API 端到端测试（真实端口 + 标准库 urllib）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from points_ledger.server import build_server


class ApiClient:
    def __init__(self, base: str):
        self.base = base

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        return self.call("POST", path, payload)

    def get(self, path: str) -> tuple[int, dict]:
        return self.call("GET", path)


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = build_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _full_scenario_2026(self):
        self.assertEqual(self.api.post("/periods/open", {"period": "2026"})[0], 200)
        self.api.post("/volunteers", {"volunteer_id": "v1", "name": "张三"})
        self.api.post("/sessions", {"session_id": "s1", "service_date": "2026-05-01",
                                    "name": "展厅引导"})
        for rid, source in (("r-school", "school"), ("r-venue", "venue")):
            self.api.post("/reports", {"report_id": rid, "session_id": "s1",
                                       "volunteer_id": "v1", "source": source,
                                       "role": "引导", "hours": 2})
            self.api.post("/checkins", {"checkin_id": f"ci-{rid}", "session_id": "s1",
                                        "volunteer_id": "v1", "source": source,
                                        "at": "2026-05-01T09:00:00Z"})
            self.api.post("/reviews", {"report_id": rid, "decision": "approved",
                                       "reviewer": "li"})

    def test_duplicate_pending_resolve_then_statement_and_ranking(self):
        self._full_scenario_2026()
        code, body = self.api.get("/duplicates/pending")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["data"]), 1)
        gid = body["data"][0]["group_id"]
        # 待确认期间余额为 0
        _, body = self.api.get("/volunteers/v1/balance")
        self.assertEqual(body["data"]["balance"], 0)
        code, body = self.api.post("/duplicates/resolve",
                                   {"group_id": gid, "decision": "duplicate",
                                    "winner_report_id": "r-school", "arbiter": "wang"})
        self.assertEqual(code, 200, body)
        self.api.post("/sessions/s1/complete", {})
        _, body = self.api.get("/ranking?period=2026")
        self.assertEqual(body["data"]["rows"][0]["points"], 20)
        # 逐笔来源解释
        _, body = self.api.get("/volunteers/v1/statement")
        entries = body["data"]["entries"]
        self.assertTrue(entries[0]["source_trace"])
        self.assertIn("hash", entries[0])

    def test_cancel_and_conservation(self):
        self.api.post("/periods/open", {"period": "2025"})
        self.api.post("/volunteers", {"volunteer_id": "v2", "name": "李四"})
        self.api.post("/sessions", {"session_id": "s2", "service_date": "2025-07-01",
                                    "name": "讲解场"})
        self.api.post("/reports", {"report_id": "r2", "session_id": "s2",
                                   "volunteer_id": "v2", "source": "school",
                                   "role": "讲解", "hours": 2})
        self.api.post("/checkins", {"checkin_id": "ci2", "session_id": "s2",
                                    "volunteer_id": "v2", "source": "school",
                                    "at": "2025-07-01T09:00:00Z"})
        self.api.post("/reviews", {"report_id": "r2", "decision": "approved",
                                   "reviewer": "li"})
        self.api.post("/sessions/s2/complete", {})
        _, body = self.api.get("/volunteers/v2/balance")
        self.assertEqual(body["data"]["balance"], 24)
        self.api.post("/sessions/s2/cancel", {})
        _, body = self.api.get("/volunteers/v2/balance")
        self.assertEqual(body["data"]["balance"], 0)
        _, body = self.api.get("/conservation")
        self.assertEqual(body["data"]["all_accounts_total"], 0)
        self.assertTrue(body["data"]["hash_chain_ok"])

    def test_audit_recompute_and_bad_request(self):
        self.api.post("/periods/open", {"period": "2024"})
        code, body = self.api.get("/audit/recompute?period=2024")
        self.assertEqual(code, 200, body)
        rec = body["data"]
        self.assertTrue(rec["global_conservation"])
        self.assertIn("entries", rec)
        # 错误请求返回 4xx 且不泄漏为 500
        code, body = self.api.post("/reports", {"report_id": "x"})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])
        code, _ = self.api.get("/ranking?period=xx")
        self.assertEqual(code, 400)
        code, _ = self.api.get("/nope")
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
