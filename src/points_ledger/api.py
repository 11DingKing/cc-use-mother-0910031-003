"""HTTP/JSON 接口（仅依赖标准库 ``http.server``）。

所有路由围绕 :class:`points_ledger.service.LedgerService` 暴露；
业务异常统一映射为 4xx，未知错误为 500 且不吞栈。

启动::

    python -m points_ledger.api [--host 127.0.0.1] [--port 8080]
"""
from __future__ import annotations

import enum
import json
import re
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .errors import (
    LedgerError, NotFound, PreconditionFailed, ValidationError,
)
from .models import ReviewDecision, Role, SessionStatus
from .service import LedgerService


def _default(obj: Any) -> Any:
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"不可序列化的类型：{type(obj)!r}")


def dumps(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=_default,
                      sort_keys=False).encode("utf-8")


def parse_date(value: Optional[str], field: str) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def parse_role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError as exc:
        raise ValidationError(
            f"角色必须是：{'/'.join(r.value for r in Role)}") from exc


def parse_status(value: Optional[str]) -> SessionStatus:
    try:
        return SessionStatus(value or SessionStatus.PLANNED.value)
    except ValueError as exc:
        raise ValidationError(f"场次状态必须是：{SessionStatus.HELD.value}/"
                              f"{SessionStatus.PLANNED.value}/{SessionStatus.CANCELLED.value}"
                              ) from exc


def parse_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是整数") from exc


class ApiHandler(BaseHTTPRequestHandler):
    service: LedgerService = None  # 由 make_server 注入到类属性

    server_version = "PointsLedger/1.0"

    # ------------------------------------------------------------ 工具
    def _send(self, status: int, payload: Any) -> None:
        body = dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError("请求体必须是合法 JSON") from exc
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _require(self, data: dict, key: str) -> Any:
        if key not in data or data[key] in ("", None):
            raise ValidationError(f"缺少必填字段：{key}")
        return data[key]

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            handler = self._match(method, path, query)
            if handler is None:
                self._send(404, {"error": "not_found", "message": f"无此路由：{method} {path}"})
                return
            handler()
        except LedgerError as exc:
            if isinstance(exc, NotFound):
                status = 404
            elif isinstance(exc, ValidationError):
                status = 400
            elif isinstance(exc, PreconditionFailed):
                status = 412
            else:
                status = 409
            self._send(status, {"error": getattr(exc, "code", "business_error"),
                                "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "internal_error", "message": str(exc)})

    def _match(self, method: str, path: str, query: dict) -> Optional[Callable[[], None]]:
        svc = self.service
        routes: list[tuple[str, str, Callable[..., Any]]] = [
            ("GET", "^/$", lambda: {"service": "志愿服务积分结算", "status": "ok"}),
            ("POST", "^/sessions$", self._create_session),
            ("GET", "^/sessions$",
             lambda: {"sessions": [
                 {"session_id": s.session_id, "activity_id": s.activity_id,
                  "service_date": s.service_date, "hours": s.hours,
                  "status": s.status, "plan_year": s.plan_year,
                  "accounting_year": s.accounting_year}
                 for s in svc.store.list_sessions()]}),
            ("POST", r"^/sessions/(?P<id>[^/]+)/held$", self._mark_held),
            ("POST", r"^/sessions/(?P<id>[^/]+)/cancel$", self._cancel_session),
            ("POST", "^/checkins$", self._ingest_checkin),
            ("GET", "^/pending$", self._list_pending),
            ("POST", r"^/pending/(?P<id>[^/]+)/resolve$", self._resolve_pending),
            ("POST", "^/appeals$", self._file_appeal),
            ("GET", "^/appeals$", lambda: {"appeals": svc.list_appeals()}),
            ("POST", r"^/appeals/(?P<id>[^/]+)/approve$", self._approve_appeal),
            ("POST", r"^/entries/(?P<id>[^/]+)/reverse$", self._reverse_partial),
            ("GET", r"^/entries/(?P<id>[^/]+)$", self._explain_entry),
            ("POST", "^/late-entries$", self._late_batch),
            ("POST", "^/periods$", self._ensure_period),
            ("GET", "^/periods$", lambda: {"periods": svc.list_periods()}),
            ("POST", r"^/periods/(?P<year>\d{4})/close$", self._close_period),
            ("POST", "^/corrections$", self._create_correction),
            ("GET", "^/balance$", self._balance),
            ("GET", "^/ledger$", self._ledger),
            ("GET", "^/rankings$", self._rankings),
            ("GET", "^/conservation$", lambda: svc.verify_conservation()),
            ("GET", r"^/periods/(?P<year>\d{4})/recompute$", self._recompute),
        ]
        for verb, pattern, fn in routes:
            if verb != method:
                continue
            m = re.fullmatch(pattern, path)
            if m:
                return lambda: self._send(200, fn(**m.groupdict(), query=query)
                                          if "query" in fn.__code__.co_varnames
                                          else fn(**m.groupdict()))
        return None

    # ------------------------------------------------------------ 端点实现
    def _create_session(self) -> dict:
        data = self._read_json()
        session = self.service.register_session(
            session_id=self._require(data, "session_id"),
            activity_id=self._require(data, "activity_id"),
            service_date=parse_date(self._require(data, "service_date"), "service_date"),
            hours=parse_int(self._require(data, "hours"), "hours"),
            status=parse_status(data.get("status", SessionStatus.PLANNED.value)),
            plan_year=(parse_int(data["plan_year"], "plan_year")
                       if data.get("plan_year") else None),
        )
        return {"session_id": session.session_id, "status": session.status.value,
                "accounting_year": session.accounting_year}

    def _mark_held(self, id: str) -> dict:
        data = self._read_json()
        return self.service.mark_session_held(
            id, on_date=parse_date(data.get("on_date"), "on_date"))

    def _cancel_session(self, id: str) -> dict:
        data = self._read_json()
        return self.service.cancel_session(
            id,
            reason=self._require(data, "reason"),
            approver=data.get("approver", "operator"),
            as_of=parse_date(data.get("as_of"), "as_of"))

    def _ingest_checkin(self) -> dict:
        data = self._read_json()
        return self.service.ingest_checkin(
            checkin_id=self._require(data, "checkin_id"),
            volunteer_id=self._require(data, "volunteer_id"),
            session_id=self._require(data, "session_id"),
            role=parse_role(self._require(data, "role")),
            source=self._require(data, "source"),
            checkin_time=data.get("checkin_time"))

    def _list_pending(self, query: dict) -> dict:
        return {"items": self.service.list_pending(
            include_resolved=query.get("include_resolved") in ("1", "true"))}

    def _resolve_pending(self, id: str) -> dict:
        data = self._read_json()
        try:
            decision = ReviewDecision(data.get("decision"))
        except ValueError as exc:
            raise ValidationError("decision 必须是 confirm/reject/keep_both") from exc
        return self.service.resolve_pending(
            id, decision,
            reviewer=self._require(data, "reviewer"),
            chosen_checkin_id=data.get("chosen_checkin_id"),
            reason=data.get("reason", ""),
            on_date=parse_date(data.get("on_date"), "on_date"))

    def _file_appeal(self) -> dict:
        data = self._read_json()
        return self.service.file_appeal(
            appeal_id=self._require(data, "appeal_id"),
            volunteer_id=self._require(data, "volunteer_id"),
            year=parse_int(self._require(data, "year"), "year"),
            amount=parse_int(self._require(data, "amount"), "amount"),
            reason=self._require(data, "reason"),
            session_id=data.get("session_id"))

    def _approve_appeal(self, id: str) -> dict:
        data = self._read_json()
        return self.service.approve_appeal(
            id,
            reviewer=self._require(data, "reviewer"),
            as_of=parse_date(data.get("as_of"), "as_of"),
            reject=bool(data.get("reject", False)))

    def _reverse_partial(self, id: str) -> dict:
        data = self._read_json()
        amount = data.get("amount")
        return self.service.reverse_partial(
            id,
            amount=parse_int(amount, "amount") if amount is not None else None,
            reason=self._require(data, "reason"),
            approver=self._require(data, "approver"),
            as_of=parse_date(data.get("as_of"), "as_of"))

    def _explain_entry(self, id: str) -> dict:
        return self.service.explain_entry(id)

    def _late_batch(self) -> dict:
        data = self._read_json()
        items = self._require(data, "items")
        for item in items:
            item["role"] = parse_role(item["role"])
        return self.service.record_late_batch(
            items, recorded_on=parse_date(data.get("recorded_on"), "recorded_on"))

    def _ensure_period(self) -> dict:
        data = self._read_json()
        return self.service.ensure_period(
            parse_int(self._require(data, "year"), "year"),
            label=data.get("label"))

    def _close_period(self, year: str) -> dict:
        data = self._read_json()
        return self.service.close_period(
            parse_int(year, "year"),
            closed_on=parse_date(data.get("closed_on"), "closed_on"))

    def _create_correction(self) -> dict:
        data = self._read_json()
        return self.service.create_correction(
            year=parse_int(self._require(data, "year"), "year"),
            volunteer_id=self._require(data, "volunteer_id"),
            reason=self._require(data, "reason"),
            approver=self._require(data, "approver"),
            entry_id=data.get("entry_id"),
            new_amount=(parse_int(data["new_amount"], "new_amount")
                        if data.get("new_amount") is not None else None),
            additive_amount=(parse_int(data["additive_amount"], "additive_amount")
                             if data.get("additive_amount") is not None else None),
            session_id=data.get("session_id"),
            as_of=parse_date(data.get("as_of"), "as_of"))

    def _balance(self, query: dict) -> dict:
        return self.service.balance(
            self._require(query, "volunteer_id"),
            year=parse_int(query["year"], "year") if query.get("year") else None)

    def _ledger(self, query: dict) -> dict:
        return {"entries": self.service.ledger(
            volunteer_id=query.get("volunteer_id"),
            year=parse_int(query["year"], "year") if query.get("year") else None)}

    def _rankings(self, query: dict) -> dict:
        return self.service.rankings(
            parse_int(self._require(query, "year"), "year"))

    def _recompute(self, year: str) -> dict:
        return self.service.recompute_period(parse_int(year, "year"))


def make_server(host: str, port: int, service: Optional[LedgerService] = None) -> ThreadingHTTPServer:
    service = service or LedgerService()

    handler = type("BoundApiHandler", (ApiHandler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.ledger_service = service  # type: ignore[attr-defined]
    return httpd


def main(argv: Optional[list[str]] = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="志愿服务积分结算服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    httpd = make_server(args.host, args.port)
    print(f"积分结算服务监听 http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
