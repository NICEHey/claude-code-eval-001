"""HTTP 路由层（仅用标准库 http.server）。

路由：
- GET  /health
- POST /stations
- POST /observations
- GET  /stations/{id}/events

业务异常（ServiceError）转换为 4xx JSON；其余异常返回 500 JSON，
不会把内部堆栈直接当成功响应。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from .database import Database
from .errors import ServiceError
from .service import Service
from .validation import parse_json

_MAX_BODY = 10 * 1024 * 1024  # 10 MiB

_EVENTS_PATH = re.compile(r"^/stations/([^/]+)/events/?$")


class _Handler(BaseHTTPRequestHandler):
    server_version = "HydroMonitor/1.0"

    # --- 由 server 注入 ---
    service: Service

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        import sys

        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- 响应工具 ----------

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > _MAX_BODY:
            from .errors import ValidationError

            raise ValidationError("请求体超过 10MiB 上限", code="body_too_large")
        return self.rfile.read(length)

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 约定)
        path = self.path.split("?", 1)[0]
        try:
            if path == "/health":
                self._send_json(200, self.service.health())
                return
            m = _EVENTS_PATH.match(path)
            if m:
                station_id = unquote(m.group(1))
                self._send_json(200, {
                    "station_id": station_id,
                    "events": self.service.list_events(station_id),
                })
                return
            self._send_json(404, {"error": "not_found", "message": f"路径不存在：{path}"})
        except ServiceError as exc:
            self._send_json(exc.status_code, exc.to_dict())
        except Exception as exc:  # noqa: BLE001 — 兜底，不泄露内部异常
            self._send_json(500, {"error": "internal_error",
                                  "message": "服务器内部错误"})
            self.log_error("internal error: %r", exc)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            raw = self._read_body()
            payload = parse_json(raw)
            if path == "/stations":
                self._send_json(201, self.service.create_station(payload))
            elif path == "/observations":
                self._send_json(200, self.service.submit_batch(payload))
            else:
                self._send_json(404, {"error": "not_found",
                                      "message": f"路径不存在：{path}"})
        except ServiceError as exc:
            self._send_json(exc.status_code, exc.to_dict())
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": "internal_error",
                                  "message": "服务器内部错误"})
            self.log_error("internal error: %r", exc)


def build_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    db = Database(db_path)
    service = Service(db)

    handler = type("Handler", (_Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.db = db  # type: ignore[attr-defined]
    return httpd


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="水文监测与洪峰事件识别服务")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "data/hydro.db"))
    args = parser.parse_args(argv)

    httpd = build_server(args.host, args.port, args.db)
    print(f"水文监测服务已启动：http://{args.host}:{args.port} （数据库 {args.db}）",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务...", flush=True)
    finally:
        httpd.server_close()
        httpd.db.close()  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
