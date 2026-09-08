"""事件状态机纯函数测试。"""

import unittest

from app.events import compute_events

FLOOD = 10.0
RECOVERY = 8.0


def ev(readings, flood=FLOOD, recovery=RECOVERY):
    return compute_events(readings, flood, recovery)


class EventStateMachineTests(unittest.TestCase):
    def test_full_cycle(self):
        events = ev([
            ("t1", 7.0), ("t2", 10.0), ("t3", 12.0),
            ("t4", 9.0), ("t5", 8.0),
        ])
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual((e["start_at"], e["end_at"], e["peak_at"]),
                         ("t2", "t5", "t3"))
        self.assertEqual(e["peak_level"], 12.0)
        self.assertEqual(e["sample_count"], 4)  # t2,t3,t4,t5；t1 不计入
        self.assertEqual(e["status"], "closed")

    def test_dip_between_thresholds_does_not_split(self):
        # 回落到洪水线以下但未到恢复线：事件继续，不拆分
        events = ev([("t1", 10.0), ("t2", 9.5), ("t3", 9.0), ("t4", 8.0)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["sample_count"], 4)
        self.assertEqual(events[0]["end_at"], "t4")

    def test_two_separate_events(self):
        events = ev([
            ("t1", 10.0), ("t2", 8.0),          # 事件 1
            ("t3", 7.0), ("t4", 9.0),           # 阈值之间，不开事件
            ("t5", 11.0), ("t6", 8.0),          # 事件 2
        ])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["sample_count"], 2)
        self.assertEqual(events[1]["sample_count"], 2)
        self.assertEqual(events[1]["peak_level"], 11.0)

    def test_open_event(self):
        events = ev([("t1", 7.0), ("t2", 10.0), ("t3", 12.0)])
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["end_at"])
        self.assertEqual(events[0]["status"], "open")
        self.assertEqual(events[0]["sample_count"], 2)

    def test_recovery_then_immediate_flood(self):
        events = ev([("t1", 10.0), ("t2", 8.0), ("t3", 10.0), ("t4", 8.0)])
        self.assertEqual(len(events), 2)
        self.assertEqual([e["status"] for e in events], ["closed", "closed"])

    def test_peak_tie_keeps_earliest(self):
        events = ev([
            ("t1", 12.0), ("t2", 9.0), ("t3", 12.0), ("t4", 8.0),
        ])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["peak_at"], "t1")  # 峰值相同取最早
        self.assertEqual(events[0]["peak_level"], 12.0)

    def test_no_event_below_flood(self):
        self.assertEqual(ev([("t1", 7.0), ("t2", 8.0), ("t3", 9.99)]), [])

    def test_threshold_boundaries(self):
        # 恰好等于 flood_level 开启；恰好等于 recovery_level 结束
        events = ev([("t1", 10.0), ("t2", 8.0)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["start_at"], "t1")
        self.assertEqual(events[0]["end_at"], "t2")

    def test_between_thresholds_after_close_no_event(self):
        events = ev([("t1", 10.0), ("t2", 8.0), ("t3", 8.5), ("t4", 9.9)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "closed")


if __name__ == "__main__":
    unittest.main()
