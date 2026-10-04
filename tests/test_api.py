"""HTTP/JSON 接口端到端测试（真实起服、真实 socket）。"""
from __future__ import annotations

import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from points_ledger.api import make_server
from points_ledger.service import LedgerService


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LedgerService()
        self.httpd: ThreadingHTTPServer = make_server(
            "127.0.0.1", _free_port(), self.svc)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self) -> None:
        # 健康检查
        status, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

        # 建场次 + 举办
        self.assertEqual(self.call("POST", "/sessions", {
            "session_id": "S1", "activity_id": "ACT", "service_date": "2025-03-01",
            "hours": 2})[0], 200)
        self.assertEqual(self.call("POST", "/sessions/S1/held", {})[0], 200)

        # 两渠道重复报送
        self.call("POST", "/checkins", {
            "checkin_id": "K1", "volunteer_id": "V1", "session_id": "S1",
            "role": "讲解员", "source": "school"})
        status, body = self.call("POST", "/checkins", {
            "checkin_id": "K2", "volunteer_id": "V1", "session_id": "S1",
            "role": "讲解员", "source": "venue"})
        self.assertEqual(body["result"], "pending")

        # 待确认队列
        status, body = self.call("GET", "/pending")
        self.assertEqual(len(body["items"]), 1)
        pid = body["items"][0]["pending_id"]

        # 挂起期间余额为 0
        status, body = self.call("GET", "/balance?volunteer_id=V1&year=2025")
        self.assertEqual(body["balance"], 0)

        # 核对确认
        self.call("POST", f"/pending/{pid}/resolve", {
            "decision": "confirm", "chosen_checkin_id": "K1", "reviewer": "审计"})
        status, body = self.call("GET", "/balance?volunteer_id=V1&year=2025")
        self.assertEqual(body["balance"], 24)

        # 封账
        self.call("POST", "/periods/2025/close", {"closed_on": "2026-01-05"})

        # 封账后补报被 409
        status, body = self.call("POST", "/checkins", {
            "checkin_id": "K9", "volunteer_id": "V9", "session_id": "S1",
            "role": "协助员", "source": "school"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "period_closed")

        # 逐笔解释：至少有一笔原始服务分录携带完整计算规则证据
        status, ledger = self.call("GET", "/ledger?volunteer_id=V1&year=2025")
        rule_entry = next(
            (e for e in ledger["entries"]
             if any(ev["key"] == "规则" for ev in e["evidence"])), None)
        self.assertIsNotNone(rule_entry, ledger)
        status, explanation = self.call(
            "GET", f"/entries/{rule_entry['entry_id']}")
        self.assertEqual(status, 200)
        self.assertTrue(any(ev["key"] == "规则" for ev in explanation["evidence"]))
        # 核对结论生成的结算分录也必须可解释（带待确认单号）
        settled = next(
            (e for e in ledger["entries"]
             if e.get("pending_id") and e["amount"] > 0), None)
        if settled is not None:
            status, detail = self.call("GET", f"/entries/{settled['entry_id']}")
            self.assertEqual(status, 200)
            self.assertTrue(any(ev["key"] == "待确认单" for ev in detail["evidence"]))

        # 审计复算
        status, rec = self.call("GET", "/periods/2025/recompute")
        self.assertEqual(status, 200)
        self.assertTrue(rec["matches"], rec["checks"])
        self.assertTrue(rec["conservation"]["ok"])

        # 排名基于快照
        status, rank = self.call("GET", "/rankings?year=2025")
        self.assertEqual(rank["basis"], "settled_snapshot")
        self.assertEqual(rank["rankings"][0]["points"], 24)

        # 守恒
        status, cons = self.call("GET", "/conservation")
        self.assertTrue(cons["ok"])

        # 错误路由 404、坏 JSON 400
        self.assertEqual(self.call("GET", "/nope")[0], 404)

    def test_validation_error_maps_to_400(self) -> None:
        status, body = self.call("POST", "/sessions", {
            "session_id": "S1", "activity_id": "ACT",
            "service_date": "bad-date", "hours": 2})
        self.assertEqual(status, 400, body)

    def test_not_found_maps_to_404(self) -> None:
        status, body = self.call("GET", "/entries/NOPE")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
