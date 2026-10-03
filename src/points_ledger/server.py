"""基于标准库 ``http.server`` 的 JSON HTTP 服务（零第三方依赖）。

路由概览
========

登记/期间
    POST /volunteers                 注册志愿者
    POST /periods/open               开放结算期
    POST /periods/close              封账（幂等，并发安全）

业务事实
    POST /sessions                   登记场次
    POST /sessions/{id}/complete     标记完成
    POST /sessions/{id}/cancel       取消（已入账积分自动冲回）
    POST /reports                    学校/场馆报送（重复自动进待确认队列）
    POST /checkins                   签到
    POST /reviews                    审核记录

核对与申诉
    GET  /duplicates/pending         待确认队列
    POST /duplicates/resolve         重复来源裁决
    POST /appeals                    提交申诉
    POST /appeals/decide             申诉裁决
    POST /revocations                部分撤销
    POST /corrections                封账后更正单

查询与审计
    GET  /volunteers/{id}/balance    当前余额
    GET  /volunteers/{id}/statement  逐笔来源解释（?period= 过滤）
    GET  /ranking?period=YYYY        结算期排名
    GET  /conservation               守恒与哈希链报告
    GET  /audit/recompute?period=    审计复算任一结算期
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .service import ConflictError, LedgerService, ServiceError
from .store import Store

_JSON_BAD = object()


def _ids(payload: dict, *names: str) -> list:
    out = []
    for name in names:
        value = payload.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ServiceError(f"缺少必填字段：{name}")
        out.append(value)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "PointsLedger/1.0"

    # --- 基础收发 ----------------------------------------------------------

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(payload, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        return payload

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ok(self, result, status: int = 200) -> None:
        if isinstance(result, list) and result and hasattr(result[0], "seq"):
            result = {"committed_events": [
                {"seq": e.seq, "type": e.type, "payload": e.payload} for e in result]}
        self._send(status, {"ok": True, "data": result})

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        return

    # --- 路由 --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    @property
    def svc(self) -> LedgerService:
        return self.server.service  # type: ignore[attr-defined]

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        try:
            self._route(method, path, query)
        except ServiceError as exc:
            self._send(409 if isinstance(exc, ConflictError) else 400,
                       {"ok": False, "error": str(exc)})
        except Exception as exc:  # 防御：任何未预期错误不外泄堆栈
            self._send(500, {"ok": False, "error": f"内部错误：{exc}"})

    def _route(self, method: str, path: str, query: dict) -> None:
        s = self.svc
        # ---- GET ----
        if method == "GET":
            if path == "/health":
                return self._ok({"status": "ok"})
            if path == "/duplicates/pending":
                return self._ok(s.pending_duplicates())
            if path == "/ranking":
                period = query.get("period", "")
                if not re.fullmatch(r"\d{4}", period):
                    raise ServiceError("period 必须是四位年度，如 2026")
                return self._ok(s.ranking(period))
            if path == "/conservation":
                return self._ok(s.conservation_report())
            if path == "/audit/recompute":
                period = query.get("period", "")
                if not re.fullmatch(r"\d{4}", period):
                    raise ServiceError("period 必须是四位年度，如 2026")
                return self._ok(s.recompute_period(period))
            m = re.fullmatch(r"/volunteers/([^/]+)/(balance|statement)", path)
            if m:
                vid, what = m.group(1), m.group(2)
                if what == "balance":
                    return self._ok(s.balance(vid))
                period = query.get("period")
                return self._ok(s.account_statement(vid, period))
            raise ServiceError(f"未知接口：GET {path}")

        # ---- POST ----
        p = self._read_json()
        if path == "/volunteers":
            vid, = _ids(p, "volunteer_id")
            return self._ok(s.register_volunteer(vid, p.get("name", "")))
        if path == "/periods/open":
            period, = _ids(p, "period")
            return self._ok(s.open_period(str(period)))
        if path == "/periods/close":
            period, = _ids(p, "period")
            return self._ok(s.close_period(str(period)))
        if path == "/sessions":
            sid, date = _ids(p, "session_id", "service_date")
            return self._ok(s.register_session(sid, date, p.get("name", "")))
        m = re.fullmatch(r"/sessions/([^/]+)/(complete|cancel)", path)
        if m:
            sid, action = m.group(1), m.group(2)
            if action == "complete":
                return self._ok(s.mark_session_completed(sid))
            return self._ok(s.cancel_session(sid))
        if path == "/reports":
            rid, sid, vid, source, role = _ids(
                p, "report_id", "session_id", "volunteer_id", "source", "role")
            if "hours" not in p:
                raise ServiceError("缺少必填字段：hours")
            return self._ok(s.submit_report(
                report_id=rid, session_id=sid, volunteer_id=vid, source=source,
                role=role, hours=p["hours"], service_date=p.get("service_date")))
        if path == "/checkins":
            cid, sid, vid, source, at = _ids(
                p, "checkin_id", "session_id", "volunteer_id", "source", "at")
            return self._ok(s.record_checkin(
                checkin_id=cid, session_id=sid, volunteer_id=vid,
                source=source, at=at))
        if path == "/reviews":
            rid, decision, reviewer = _ids(p, "report_id", "decision", "reviewer")
            return self._ok(s.review_report(rid, decision, reviewer, p.get("note", "")))
        if path == "/duplicates/resolve":
            gid, decision = _ids(p, "group_id", "decision")
            return self._ok(s.resolve_duplicate(
                gid, decision, p.get("winner_report_id"),
                p.get("arbiter", ""), p.get("note", "")))
        if path == "/appeals":
            aid, kind, subject, appellant, reason = _ids(
                p, "appeal_id", "kind", "subject_id", "appellant", "reason")
            return self._ok(s.submit_appeal(
                appeal_id=aid, kind=kind, subject_id=subject,
                appellant=appellant, reason=reason))
        if path == "/appeals/decide":
            aid, decision, arbiter = _ids(p, "appeal_id", "decision", "arbiter")
            return self._ok(s.decide_appeal(aid, decision, arbiter, p.get("note", "")))
        if path == "/revocations":
            rid, = _ids(p, "target_report_id")
            if "fraction" not in p:
                raise ServiceError("缺少必填字段：fraction")
            return self._ok(s.revoke_service(
                target_report_id=rid, fraction=p["fraction"],
                reason=p.get("reason", ""), voucher_id=p.get("voucher_id")))
        if path == "/corrections":
            voucher, target, vid, approver, reason = _ids(
                p, "voucher_id", "target_entry_id", "volunteer_id", "approver", "reason")
            if "delta" not in p or not isinstance(p["delta"], int):
                raise ServiceError("缺少必填整数：delta")
            return self._ok(s.issue_correction(
                voucher_id=voucher, target_entry_id=target, volunteer_id=vid,
                delta=p["delta"], approver=approver, reason=reason))
        raise ServiceError(f"未知接口：POST {path}")


def build_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    store = Store(db_path)
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = LedgerService(store)  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="志愿服务积分结算服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="points_ledger.db")
    args = parser.parse_args(argv)
    server = build_server(args.host, args.port, args.db)
    print(f"积分结算服务监听 http://{args.host}:{args.port}（事件库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
