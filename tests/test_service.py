"""业务层测试：批次原子性、去重/冲突、乱序、时区等价、重启持久化、并发。"""

import json
import os
import tempfile
import threading
import unittest

from app.database import Database
from app.errors import ConflictError, NotFoundError, ValidationError
from app.service import Service

STATION = {"id": "s1", "name": "站一",
           "flood_level": 10.0, "recovery_level": 8.0}


def batch(bid, readings):
    return {"batch_id": bid, "readings": readings}


def rd(sid, ts, level):
    return {"station_id": sid, "observed_at": ts, "level": level}


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.db = Database(self.db_path)
        self.svc = Service(self.db)
        self.svc.create_station(dict(STATION))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def events(self, sid="s1"):
        return self.svc.list_events(sid)


class FullChainTests(ServiceTestBase):
    def test_fixture_batch_produces_closed_event(self):
        fixture_path = os.path.join(
            os.path.dirname(__file__), "..", "fixtures", "observations.json")
        with open(fixture_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        # fixture 中的站点是 river-a，按其阈值建站
        st = payload["station"]
        self.svc.create_station(dict(st))
        result = self.svc.submit_batch(payload["batch"])
        self.assertEqual(result["inserted"], 5)
        ev = self.svc.list_events("river-a")
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual(e["start_at"], "2026-09-01T00:10:00+00:00")
        self.assertEqual(e["end_at"], "2026-09-01T00:40:00+00:00")
        self.assertEqual(e["peak_at"], "2026-09-01T00:20:00+00:00")
        self.assertEqual(e["peak_level"], 12)
        self.assertEqual(e["sample_count"], 4)
        self.assertEqual(e["status"], "closed")

    def test_open_event_when_never_recovers(self):
        self.svc.submit_batch(batch("b1", [
            rd("s1", "2026-09-01T00:00:00Z", 7.0),
            rd("s1", "2026-09-01T01:00:00Z", 11.0),
            rd("s1", "2026-09-01T02:00:00Z", 13.0),
        ]))
        ev = self.events()
        self.assertEqual(len(ev), 1)
        self.assertIsNone(ev[0]["end_at"])
        self.assertEqual(ev[0]["status"], "open")
        self.assertEqual(ev[0]["peak_level"], 13)

    def test_open_event_closes_after_late_recovery(self):
        self.svc.submit_batch(batch("b1", [
            rd("s1", "2026-09-01T01:00:00Z", 11.0),
        ]))
        self.assertEqual(self.events()[0]["status"], "open")
        # 迟到的回落观测
        self.svc.submit_batch(batch("b2", [
            rd("s1", "2026-09-01T03:00:00Z", 8.0),
        ]))
        ev = self.events()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["status"], "closed")
        self.assertEqual(ev[0]["end_at"], "2026-09-01T03:00:00+00:00")
        self.assertEqual(ev[0]["sample_count"], 2)


class OutOfOrderTests(ServiceTestBase):
    def test_late_arrivals_match_bulk_import(self):
        # 乱序、分批、迟到导入
        self.svc.submit_batch(batch("b1", [
            rd("s1", "2026-09-01T04:00:00Z", 8.0),    # 回落
            rd("s1", "2026-09-01T02:00:00Z", 12.0),   # 峰值
        ]))
        self.svc.submit_batch(batch("b2", [
            rd("s1", "2026-09-01T01:00:00Z", 10.0),   # 起涨（迟到）
            rd("s1", "2026-09-01T03:00:00Z", 9.0),    # 阈值间（迟到）
        ]))
        self.svc.submit_batch(batch("b3", [
            rd("s1", "2026-09-01T05:00:00Z", 7.0),
            rd("s1", "2026-09-01T06:00:00Z", 11.0),
            rd("s1", "2026-09-01T07:00:00Z", 8.0),
        ]))
        got = self.events()

        # 对照：一次性按时间顺序导入
        ref_db = Database(":memory:")
        ref = Service(ref_db)
        ref.create_station(dict(STATION))
        ref.submit_batch(batch("ref", [
            rd("s1", "2026-09-01T01:00:00Z", 10.0),
            rd("s1", "2026-09-01T02:00:00Z", 12.0),
            rd("s1", "2026-09-01T03:00:00Z", 9.0),
            rd("s1", "2026-09-01T04:00:00Z", 8.0),
            rd("s1", "2026-09-01T05:00:00Z", 7.0),
            rd("s1", "2026-09-01T06:00:00Z", 11.0),
            rd("s1", "2026-09-01T07:00:00Z", 8.0),
        ]))
        self.assertEqual(got, ref.list_events("s1"))
        self.assertEqual(len(got), 2)

    def test_timezone_equivalence(self):
        # 同一时刻的不同时区表示 + 相同水位 = 重复观测
        result = self.svc.submit_batch(batch("tz1", [
            rd("s1", "2026-09-01T08:00:00+08:00", 10.0),
        ]))
        self.assertEqual(result["inserted"], 1)
        result2 = self.svc.submit_batch(batch("tz2", [
            rd("s1", "2026-09-01T00:00:00Z", 10.0),          # 同一时刻
            rd("s1", "2026-09-01T08:00:00+08:00", 10.0),     # 再次重复
        ]))
        self.assertEqual(result2["inserted"], 0)
        self.assertEqual(result2["duplicates"], 2)
        self.assertEqual(len(self.events()), 1)

    def test_timezone_equivalent_conflicting_level(self):
        self.svc.submit_batch(batch("tz1", [
            rd("s1", "2026-09-01T08:00:00+08:00", 10.0),
        ]))
        with self.assertRaises(ConflictError):
            self.svc.submit_batch(batch("tz2", [
                rd("s1", "2026-09-01T00:00:00Z", 11.5),  # 同一时刻不同水位
            ]))


class DedupConflictTests(ServiceTestBase):
    def test_same_batch_id_retry_returns_original_result(self):
        b = batch("dup", [rd("s1", "2026-09-01T01:00:00Z", 10.0),
                          rd("s1", "2026-09-01T02:00:00Z", 8.0)])
        r1 = self.svc.submit_batch(b)
        r2 = self.svc.submit_batch(json.loads(json.dumps(b)))  # 等价重试
        self.assertEqual(r1, r2)

    def test_same_batch_id_different_content_returns_409(self):
        self.svc.submit_batch(batch("x", [
            rd("s1", "2026-09-01T01:00:00Z", 10.0)]))
        with self.assertRaises(ConflictError):
            self.svc.submit_batch(batch("x", [
                rd("s1", "2026-09-01T01:00:00Z", 11.0)]))

    def test_intra_batch_duplicate_counted_once(self):
        result = self.svc.submit_batch(batch("intra", [
            rd("s1", "2026-09-01T01:00:00Z", 10.0),
            rd("s1", "2026-09-01T09:00:00+08:00", 10.0),  # 同刻同时区等价
        ]))
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["duplicates"], 1)

    def test_intra_batch_conflict_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.submit_batch(batch("bad", [
                rd("s1", "2026-09-01T01:00:00Z", 10.0),
                rd("s1", "2026-09-01T01:00:00Z", 10.5),
            ]))


class RollbackTests(ServiceTestBase):
    def _count(self, table, **where):
        clauses = " AND ".join(f"{k} = ?" for k in where)
        params = tuple(where.values())
        with self.db.lock:
            row = self.db.connect_read().execute(
                f"SELECT COUNT(*) c FROM {table}" +
                (f" WHERE {clauses}" if where else ""), params).fetchone()
        return row["c"]

    def test_unknown_station_rolls_back_entire_batch(self):
        with self.assertRaises(NotFoundError):
            self.svc.submit_batch(batch("rb1", [
                rd("s1", "2026-09-01T01:00:00Z", 10.0),       # 合法
                rd("ghost", "2026-09-01T02:00:00Z", 11.0),    # 站点不存在
            ]))
        self.assertEqual(self._count("observations"), 0)
        self.assertEqual(self._count("batches"), 0)
        self.assertEqual(self._count("events"), 0)

    def test_conflicting_level_rolls_back_entire_batch(self):
        self.svc.submit_batch(batch("ok", [
            rd("s1", "2026-09-01T01:00:00Z", 10.0)]))
        with self.assertRaises(ConflictError):
            self.svc.submit_batch(batch("rb2", [
                rd("s1", "2026-09-01T02:00:00Z", 10.0),       # 新数据
                rd("s1", "2026-09-01T01:00:00Z", 99.0),       # 冲突
            ]))
        # 第二条新观测不能留下
        rows = self.db.connect_read().execute(
            "SELECT observed_at_utc FROM observations WHERE station_id='s1'"
        ).fetchall()
        self.assertEqual([r["observed_at_utc"] for r in rows],
                         ["2026-09-01T01:00:00+00:00"])
        self.assertEqual(self._count("batches", batch_id="rb2"), 0)

    def test_invalid_payload_rolls_back(self):
        cases = [
            batch("e1", [rd("s1", "2026-09-01T01:00:00", 10.0)]),       # 无时区
            batch("e2", [rd("s1", "2026-09-01T01:00:00Z", True)]),     # bool
            batch("e3", [rd("s1", "2026-09-01T01:00:00Z", float("nan"))]),
            batch("e4", [rd("s1", "2026-09-01T01:00:00Z", float("inf"))]),
            batch("e5", [rd("s1", "not-a-time", 10.0)]),
            batch("e6", []),
        ]
        for b in cases:
            with self.subTest(batch=b["batch_id"]):
                with self.assertRaises(ValidationError):
                    self.svc.submit_batch(b)
        self.assertEqual(self._count("observations"), 0)
        self.assertEqual(self._count("batches"), 0)


class StationValidationTests(ServiceTestBase):
    def test_duplicate_station_conflict(self):
        with self.assertRaises(ConflictError):
            self.svc.create_station(dict(STATION))

    def test_threshold_validation(self):
        for payload in [
            {"id": "x1", "name": "x", "flood_level": 8.0, "recovery_level": 10.0},
            {"id": "x2", "name": "x", "flood_level": 10.0, "recovery_level": 10.0},
            {"id": "x3", "name": "x", "flood_level": "x", "recovery_level": 1.0},
            {"id": "x4", "name": "x", "flood_level": True, "recovery_level": 1.0},
            {"id": "x5", "name": "x", "flood_level": float("inf"), "recovery_level": 1.0},
            {"id": "", "name": "x", "flood_level": 10.0, "recovery_level": 8.0},
        ]:
            with self.subTest(id=payload.get("id")):
                with self.assertRaises(ValidationError):
                    self.svc.create_station(payload)

    def test_unknown_station_events_404(self):
        with self.assertRaises(NotFoundError):
            self.svc.list_events("nope")


class RestartPersistenceTests(unittest.TestCase):
    def test_data_survives_restart(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "persist.db")
        try:
            db = Database(path)
            svc = Service(db)
            svc.create_station(dict(STATION))
            svc.submit_batch(batch("p1", [
                rd("s1", "2026-09-01T01:00:00Z", 10.0),
                rd("s1", "2026-09-01T02:00:00Z", 12.0),
            ]))
            db.close()

            # 重启：新连接、新 Service
            db2 = Database(path)
            svc2 = Service(db2)
            ev = svc2.list_events("s1")
            self.assertEqual(len(ev), 1)
            self.assertEqual(ev[0]["status"], "open")
            # 批次幂等信息仍在：重试原样返回首次成功结果，改内容返回 409
            r = svc2.submit_batch(batch("p1", [
                rd("s1", "2026-09-01T01:00:00Z", 10.0),
                rd("s1", "2026-09-01T02:00:00Z", 12.0),
            ]))
            self.assertEqual(r["inserted"], 2)  # 首次成功的原始结果
            self.assertEqual(svc2.list_events("s1")[0]["status"], "open")
            with self.assertRaises(ConflictError):
                svc2.submit_batch(batch("p1", [
                    rd("s1", "2026-09-01T01:00:00Z", 10.1)]))
            # 重启后可继续接收数据并把开放事件关闭
            svc2.submit_batch(batch("p2", [
                rd("s1", "2026-09-01T03:00:00Z", 8.0)]))
            ev2 = svc2.list_events("s1")
            self.assertEqual(ev2[0]["status"], "closed")
            self.assertEqual(ev2[0]["sample_count"], 3)
            db2.close()
        finally:
            tmp.cleanup()


class ConcurrencyTests(ServiceTestBase):
    def test_concurrent_batches_serialize(self):
        errors = []

        def submit(i):
            try:
                self.svc.submit_batch(batch(f"c{i}", [
                    rd("s1", f"2026-09-01T{i:02d}:00:00Z",
                       10.0 if i % 2 == 0 else 8.0),
                ]))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # 10 条观测全部入库；事件状态自洽（偶数点 10.0 起事件、奇数点 8.0 结束）
        count = self.db.connect_read().execute(
            "SELECT COUNT(*) c FROM observations").fetchone()["c"]
        self.assertEqual(count, 10)
        for e in self.events():
            self.assertIn(e["status"], ("open", "closed"))


if __name__ == "__main__":
    unittest.main()
