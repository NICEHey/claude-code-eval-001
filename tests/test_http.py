"""HTTP 端到端测试：真实起服务，通过 urllib 走完整请求链路。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from app.server import build_server


class HttpEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "http.db")
        self.server = build_server("127.0.0.1", 0, self.db_path)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.server.db.close()  # type: ignore[attr-defined]
        self.tmp.cleanup()

    def request(self, method, path, payload=None, raw=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        if raw is not None:
            data = raw
        else:
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_chain_over_http(self):
        status, station = self.request("POST", "/stations", {
            "id": "river-a", "name": "A 站",
            "flood_level": 10.0, "recovery_level": 8.0})
        self.assertEqual(status, 201)
        self.assertEqual(station["flood_level"], 10)

        status, body = self.request("POST", "/observations", {
            "batch_id": "b1",
            "readings": [
                {"station_id": "river-a", "observed_at": "2026-09-01T08:00:00+08:00", "level": 7.0},
                {"station_id": "river-a", "observed_at": "2026-09-01T08:10:00+08:00", "level": 10.0},
                {"station_id": "river-a", "observed_at": "2026-09-01T08:20:00+08:00", "level": 12.0},
                {"station_id": "river-a", "observed_at": "2026-09-01T08:30:00+08:00", "level": 9.0},
                {"station_id": "river-a", "observed_at": "2026-09-01T08:40:00+08:00", "level": 8.0},
            ]})
        self.assertEqual(status, 200)
        self.assertEqual(body["inserted"], 5)

        status, body = self.request("GET", "/stations/river-a/events")
        self.assertEqual(status, 200)
        events = body["events"]
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["status"], "closed")
        self.assertEqual(e["start_at"], "2026-09-01T00:10:00+00:00")
        self.assertEqual(e["end_at"], "2026-09-01T00:40:00+00:00")
        self.assertEqual(e["peak_level"], 12)
        self.assertEqual(e["sample_count"], 4)

    def test_station_validation_errors(self):
        status, body = self.request("POST", "/stations", {
            "id": "bad", "name": "x",
            "flood_level": 8.0, "recovery_level": 10.0})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

        status, body = self.request("POST", "/stations", {
            "id": "ok", "name": "x",
            "flood_level": 10.0, "recovery_level": 8.0})
        self.assertEqual(status, 201)
        status, body = self.request("POST", "/stations", {
            "id": "ok", "name": "y",
            "flood_level": 10.0, "recovery_level": 8.0})
        self.assertEqual(status, 409)

    def test_observation_errors_and_rollback(self):
        self.request("POST", "/stations", {
            "id": "s1", "name": "x", "flood_level": 10.0, "recovery_level": 8.0})

        # 站点不存在 → 404
        status, body = self.request("POST", "/observations", {
            "batch_id": "x1", "readings": [
                {"station_id": "ghost", "observed_at": "2026-09-01T00:00:00Z", "level": 9.0}]})
        self.assertEqual(status, 404)

        # 无时区 → 400
        status, body = self.request("POST", "/observations", {
            "batch_id": "x2", "readings": [
                {"station_id": "s1", "observed_at": "2026-09-01T00:00:00", "level": 9.0}]})
        self.assertEqual(status, 400)

        # 布尔水位 → 400
        status, body = self.request("POST", "/observations", {
            "batch_id": "x3", "readings": [
                {"station_id": "s1", "observed_at": "2026-09-01T00:00:00Z", "level": True}]})
        self.assertEqual(status, 400)

        # 非法 JSON → 400
        status, body = self.request("POST", "/observations", raw=b"{not json")
        self.assertEqual(status, 400)

        # 失败批次均未留下事件
        status, body = self.request("GET", "/stations/s1/events")
        self.assertEqual(body["events"], [])

    def test_batch_retry_and_conflict(self):
        self.request("POST", "/stations", {
            "id": "s1", "name": "x", "flood_level": 10.0, "recovery_level": 8.0})
        payload = {"batch_id": "b", "readings": [
            {"station_id": "s1", "observed_at": "2026-09-01T00:00:00Z", "level": 11.0}]}
        s1, r1 = self.request("POST", "/observations", payload)
        s2, r2 = self.request("POST", "/observations", payload)
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(r1, r2)

        status, body = self.request("POST", "/observations", {
            "batch_id": "b", "readings": [
                {"station_id": "s1", "observed_at": "2026-09-01T00:00:00Z", "level": 12.0}]})
        self.assertEqual(status, 409)

    def test_unknown_station_events_404(self):
        status, body = self.request("GET", "/stations/nope/events")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_unknown_route(self):
        status, _ = self.request("GET", "/nonsense")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
