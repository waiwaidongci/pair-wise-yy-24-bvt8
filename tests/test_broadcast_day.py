import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import ConflictError, DomainError, RadioDB


def weekday_of(date_str: str) -> int:
    return datetime.strptime(date_str, "%Y-%m-%d").weekday()


class BroadcastDayBoundaryTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.p = self.db.add_program("午夜节目", "music", 60, "2026-01-01", "2026-12-31", None, 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_early_morning_belongs_to_previous_broadcast_day(self):
        s1 = self.db.schedule_slot("2026-09-28", "02:00", self.p, "华东")
        s2 = self.db.schedule_slot("2026-09-28", "03:00", self.p, "华东")
        s3 = self.db.schedule_slot("2026-09-28", "06:00", self.p, "华东")
        s4 = self.db.schedule_slot("2026-09-28", "23:00", self.p, "华东")
        self.assertEqual("2026-09-27", self.db.get_slot(s1)["broadcast_day"])
        self.assertEqual("2026-09-27", self.db.get_slot(s2)["broadcast_day"])
        self.assertEqual("2026-09-28", self.db.get_slot(s3)["broadcast_day"])
        self.assertEqual("2026-09-28", self.db.get_slot(s4)["broadcast_day"])

    def test_cross_midnight_slot_overlaps_next_day_early_morning(self):
        long = self.db.add_program("深夜长节目", "talk", 120, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.db.schedule_slot("2026-09-28", "23:00", long, "华东")  # 23:00-01:00
        early = self.db.add_program("凌晨档", "music", 60, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.schedule_slot("2026-09-29", "00:30", early, "华东")
        # Exactly at the end (01:00) is fine.
        self.db.schedule_slot("2026-09-29", "01:00", early, "华东")

    def test_authorization_uses_broadcast_day(self):
        licensed = self.db.add_program("单日授权", "music", 30, "2026-09-27", "2026-09-27", None, 0, ["华东"])
        # Airing at 02:00 on the 28th belongs to broadcast day the 27th -> licensed.
        self.db.schedule_slot("2026-09-28", "02:00", licensed, "华东")
        # Airing at 06:00 on the 28th belongs to broadcast day the 28th -> not licensed.
        with self.assertRaisesRegex(DomainError, "授权"):
            self.db.schedule_slot("2026-09-28", "06:00", licensed, "华东")


class BanWindowTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.p = self.db.add_program("常规节目", "music", 60, "2026-01-01", "2026-12-31", None, 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_overnight_ban_window_splits_into_two_segments(self):
        self.db.add_blocked_window("华东", 0, "23:00", "02:00", "夜间禁播")
        rows = self.db.conn.execute(
            "SELECT * FROM blocked_windows WHERE region='华东' ORDER BY weekday, start_time"
        ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual((0, "23:00", "24:00"), (rows[0]["weekday"], rows[0]["start_time"], rows[0]["end_time"]))
        self.assertEqual((1, "00:00", "02:00"), (rows[1]["weekday"], rows[1]["start_time"], rows[1]["end_time"]))

    def test_cross_midnight_slot_hits_both_segments(self):
        self.db.add_blocked_window("华东", 0, "23:00", "02:00", "夜间禁播")
        # Monday 23:30 intersects the Monday evening segment.
        with self.assertRaisesRegex(DomainError, "禁播"):
            self.db.schedule_slot("2026-09-28", "23:30", self.p, "华东")
        # Tuesday 00:30 intersects the Tuesday early-morning segment.
        with self.assertRaisesRegex(DomainError, "禁播"):
            self.db.schedule_slot("2026-09-29", "00:30", self.p, "华东")
        # Tuesday 02:00 is exactly at the end of the ban window -> allowed.
        self.db.schedule_slot("2026-09-29", "02:00", self.p, "华东")


class TimelineGapTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_cooldown_across_midnight(self):
        p = self.db.add_program("冷却节目", "music", 30, "2026-01-01", "2026-12-31", None, 60, ["华东"])
        self.db.schedule_slot("2026-09-28", "23:00", p, "华东")  # ends 23:30
        with self.assertRaisesRegex(DomainError, "冷却"):
            self.db.schedule_slot("2026-09-29", "00:00", p, "华东")  # gap 30
        self.db.schedule_slot("2026-09-29", "00:30", p, "华东")  # gap 60

    def test_sponsor_interval_across_midnight(self):
        self.db.add_sponsor_policy("青柠", 90)
        a = self.db.add_program("节目A", "music", 30, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        b = self.db.add_program("节目B", "music", 30, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        self.db.schedule_slot("2026-09-28", "23:00", a, "华东")  # ends 23:30
        with self.assertRaisesRegex(DomainError, "赞助商"):
            self.db.schedule_slot("2026-09-29", "00:30", b, "华东")  # gap 60
        self.db.schedule_slot("2026-09-29", "01:00", b, "华东")  # gap 90


class MigrationTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)

    def tearDown(self):
        os.unlink(self.path)

    def _open_legacy_db(self):
        import sqlite3
        conn = sqlite3.connect(self.path)
        conn.executescript(
            """
            CREATE TABLE programs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, kind TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
              sponsor TEXT, cooldown_minutes INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
              UNIQUE(title, start_date, end_date));
            CREATE TABLE program_regions (program_id INTEGER NOT NULL, region TEXT NOT NULL, PRIMARY KEY(program_id, region));
            CREATE TABLE blocked_windows (
              id INTEGER PRIMARY KEY AUTOINCREMENT, region TEXT NOT NULL, weekday INTEGER NOT NULL,
              start_time TEXT NOT NULL, end_time TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE sponsor_policies (sponsor TEXT PRIMARY KEY, min_gap_minutes INTEGER NOT NULL);
            CREATE TABLE slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT, air_date TEXT NOT NULL, start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL, program_id INTEGER NOT NULL, region TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'planned', replaced_from INTEGER, created_at TEXT NOT NULL);
            CREATE TABLE playout_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, slot_id INTEGER NOT NULL, actual_start TEXT NOT NULL,
              actual_duration_minutes INTEGER NOT NULL, actual_program_id INTEGER, note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL);
            CREATE TABLE reconciliation_exceptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, air_date TEXT NOT NULL, slot_id INTEGER NOT NULL,
              kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(air_date, slot_id, kind));
            """
        )
        conn.commit()
        return conn

    def test_legacy_slots_backfill_broadcast_day(self):
        conn = self._open_legacy_db()
        cur = conn.execute(
            "INSERT INTO programs(title,kind,duration_minutes,start_date,end_date,cooldown_minutes,active) "
            "VALUES('老节目','music',30,'2026-01-01','2026-12-31',0,1)"
        )
        pid = cur.lastrowid
        conn.execute(
            "INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,created_at) "
            "VALUES('2026-09-28','02:00',30,?, '华东','2026-09-28T02:00:00')", (pid,)
        )
        conn.commit()
        conn.close()

        db = RadioDB(self.path)
        slot = db.get_slot(1)
        self.assertEqual("2026-09-27", slot["broadcast_day"])
        db.close()

    def test_legacy_overnight_ban_window_splits(self):
        conn = self._open_legacy_db()
        conn.execute(
            "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES('华东',0,'23:00','02:00','夜间')"
        )
        conn.commit()
        conn.close()

        db = RadioDB(self.path)
        rows = db.conn.execute(
            "SELECT * FROM blocked_windows WHERE region='华东' ORDER BY weekday, start_time"
        ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual((0, "23:00", "24:00"), (rows[0]["weekday"], rows[0]["start_time"], rows[0]["end_time"]))
        self.assertEqual((1, "00:00", "02:00"), (rows[1]["weekday"], rows[1]["start_time"], rows[1]["end_time"]))
        db.close()


class MoveSlotTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.long = self.db.add_program("深夜长节目", "talk", 120, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.early = self.db.add_program("凌晨档", "music", 60, "2026-01-01", "2026-12-31", None, 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_move_revalidates_and_keeps_original_on_failure(self):
        slot = self.db.schedule_slot("2026-09-28", "23:00", self.long, "华东")  # 23:00-01:00
        # A different slot on the other side of midnight that the move would collide with.
        self.db.schedule_slot("2026-09-29", "01:00", self.early, "华东")  # 01:00-02:00
        # Moving to 00:30 the next day spans 00:30-01:30 and overlaps the 01:00 slot.
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.move_slot(slot, "2026-09-29", "00:30")
        # Original arrangement is kept.
        kept = self.db.get_slot(slot)
        self.assertEqual("2026-09-28", kept["air_date"])
        self.assertEqual("23:00", kept["start_time"])
        # A valid move succeeds and updates the broadcast day.
        moved = self.db.move_slot(slot, "2026-09-30", "23:00")
        self.assertEqual("2026-09-30", moved["air_date"])
        self.assertEqual("2026-09-30", moved["broadcast_day"])

    def test_move_cross_midnight_checks_both_sides(self):
        slot = self.db.schedule_slot("2026-09-28", "23:00", self.long, "华东")  # 23:00-01:00
        # Place a blocker on the early-morning side of the proposed move target.
        self.db.schedule_slot("2026-09-30", "00:30", self.early, "华东")  # 00:30-01:30
        # Moving the long show to 2026-09-29 23:00 spans 23:00-01:00 and hits 00:30.
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.move_slot(slot, "2026-09-29", "23:00")


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.p = self.db.add_program("节目", "music", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_late_submitter_sees_other_editors_changes(self):
        s1 = self.db.schedule_slot("2026-09-28", "09:00", self.p, "华东")
        # Editor A loads the plan at version 1.
        state_a = self.db._plan_state("2026-09-28")
        self.assertEqual(1, state_a["version"])
        # Editor B submits first, advancing the version to 2.
        s2 = self.db.schedule_slot("2026-09-28", "10:00", self.p, "华东", base_version=1)
        self.assertEqual(2, self.db._get_version("2026-09-28"))
        # Editor A's stale submission is rejected with the current state.
        with self.assertRaises(ConflictError) as ctx:
            self.db.schedule_slot("2026-09-28", "11:00", self.p, "华东", base_version=1)
        self.assertEqual(2, ctx.exception.state["version"])
        ids = {s["id"] for s in ctx.exception.state["slots"]}
        self.assertIn(s1, ids)
        self.assertIn(s2, ids)
        # After reloading at version 2, the submission succeeds.
        s3 = self.db.schedule_slot("2026-09-28", "11:00", self.p, "华东", base_version=2)
        self.assertEqual(3, self.db._get_version("2026-09-28"))
        self.assertGreater(s3, s2)


class ReconciliationSnapshotTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.a = self.db.add_program("节目A", "music", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.b = self.db.add_program("节目B", "music", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_reconcile_uses_version_at_broadcast_time(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.a, "华东")
        # Actually aired A as planned.
        self.db.record_playout(slot, "09:00", 30)
        # After the fact, the editor replaces the planned programme with B.
        self.db.replace_slot(slot, self.b)
        # Reconciliation must compare against the version at broadcast time (A),
        # not the current plan (B), so no wrong_program false positive.
        exceptions = self.db.reconcile_date("2026-09-28")
        self.assertEqual([], exceptions)

    def test_wrong_program_still_detected_against_snapshot(self):
        slot = self.db.schedule_slot("2026-09-28", "09:00", self.a, "华东")
        self.db.record_playout(slot, "09:00", 30, actual_program_id=self.b)
        exceptions = self.db.reconcile_date("2026-09-28")
        kinds = {(e["slot_id"], e["kind"]) for e in exceptions}
        self.assertIn((slot, "wrong_program"), kinds)


if __name__ == "__main__":
    unittest.main()
