import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as app_module
from database import ConflictError, DomainError, RadioDB, _clock_minutes


class BroadcastDayTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.news = self.db.add_program("深夜新闻", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.music = self.db.add_program("夜间音乐", "music", 120, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.show = self.db.add_program("品牌秀", "talk", 60, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        self.ad = self.db.add_program("青柠广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 60, ["华东"])
        self.db.add_sponsor_policy("青柠", 90)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_early_morning_slot_belongs_to_previous_broadcast_day(self):
        # 2026-09-29（周二）凌晨 02:00 应归入 09-28 播出日。
        slot = self.db.schedule_slot("2026-09-28", "02:00", self.news, "华东")
        data = self.db.get_slot(slot)
        self.assertEqual("2026-09-28", data["broadcast_day"])
        self.assertEqual("2026-09-29T02:00", data["start_at"])
        self.assertEqual("2026-09-29T02:30", data["end_at"])
        self.assertEqual(slot, self.db.day_view("2026-09-28")["slots"][0]["id"])
        self.assertEqual([], self.db.day_view("2026-09-29")["slots"])

    def test_program_can_run_from_evening_into_next_day(self):
        # 23:00 开播、120 分钟，一直连到次日 01:00。
        slot = self.db.schedule_slot("2026-09-28", "23:00", self.music, "华东")
        data = self.db.get_slot(slot)
        self.assertEqual("2026-09-28T23:00", data["start_at"])
        self.assertEqual("2026-09-29T01:00", data["end_at"])

    def test_overnight_overlap_against_next_calendar_day_slot(self):
        # 前夜 23:00 排 120 分钟音乐（属于 09-28 播出日，连到日历 29 日 01:00）
        self.db.schedule_slot("2026-09-28", "23:00", self.music, "华东")
        # 日历 29 日 00:10 的凌晨档同样属于 09-28 播出日，时间轴上重叠
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.schedule_slot("2026-09-28", "00:10", self.news, "华东")

    def test_move_overnight_slot_checks_both_sides_and_keeps_original(self):
        slot = self.db.schedule_slot("2026-09-28", "01:00", self.news, "华东")
        evening = self.db.add_program("前夜长节目", "talk", 90, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.db.schedule_slot("2026-09-29", "23:00", evening, "华东")  # 29-23:00 ~ 30-00:30
        with self.assertRaisesRegex(DomainError, "重叠"):
            # 移到 09-29 播出日的 00:15（日历 30 日凌晨），与前夜节目尾段相撞
            self.db.move_slot(slot, "2026-09-29", "00:15")
        # 校验失败，原安排保留
        self.assertEqual("2026-09-29T01:00", self.db.get_slot(slot)["start_at"])

    def test_conflict_message_names_other_plan(self):
        other = self.db.schedule_slot("2026-09-28", "05:00", self.news, "华东")
        with self.assertRaises(DomainError) as ctx:
            self.db.schedule_slot("2026-09-28", "05:10", self.news, "华东")
        message = str(ctx.exception)
        self.assertIn(f"#{other}", message)
        self.assertIn("深夜新闻", message)
        self.assertIn("05:00", message)

    def test_blocked_window_crossing_midnight_splits_into_two_segments(self):
        # 周二 23:00–02:00 跨日禁播，拆成周二深夜 + 周三凌晨两段
        group = self.db.add_blocked_window("华东", 1, "23:00", "02:00", "深夜维护")
        rows = self.db.conn.execute(
            "SELECT * FROM blocked_windows WHERE seg_group=? ORDER BY weekday,start_minute", (group,)
        ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual((1, _clock_minutes("23:00"), 1440),
                         (rows[0]["weekday"], rows[0]["start_minute"], rows[0]["end_minute"]))
        self.assertEqual((2, 0, _clock_minutes("02:00")),
                         (rows[1]["weekday"], rows[1]["start_minute"], rows[1]["end_minute"]))
        # 2026-09-29 是周二：23:30 落在深夜段
        with self.assertRaisesRegex(DomainError, "深夜维护"):
            self.db.schedule_slot("2026-09-29", "23:30", self.news, "华东")
        # 2026-09-30 是周三：01:30 属于 09-29 播出日，落在凌晨段
        with self.assertRaisesRegex(DomainError, "深夜维护"):
            self.db.schedule_slot("2026-09-29", "01:30", self.news, "华东")

    def test_blocked_window_spanning_into_next_day_checked_on_timeline(self):
        self.db.add_blocked_window("华东", 1, "23:30", "00:30", "零点检修")
        # 节目 23:00 开播、120 分钟，23:30–00:30 被禁播盖住，即便开播钟点本身不在禁播段内
        with self.assertRaisesRegex(DomainError, "零点检修"):
            self.db.schedule_slot("2026-09-29", "23:00", self.music, "华东")

    def test_cooldown_checked_across_midnight_both_sides(self):
        repeated = self.db.add_program("点歌台", "music", 30, "2026-01-01", "2026-12-31", None, 120, ["华东"])
        self.db.schedule_slot("2026-09-28", "23:30", repeated, "华东")  # 到 29 日 00:00
        # 次日凌晨 01:00 再来一期，间隔只有 60 分钟 < 冷却 120
        with self.assertRaisesRegex(DomainError, "冷却"):
            self.db.schedule_slot("2026-09-28", "01:00", repeated, "华东")
        # 02:00 时间隔 120 分钟，放行
        self.db.schedule_slot("2026-09-28", "02:00", repeated, "华东")

    def test_sponsor_gap_checked_across_midnight(self):
        self.db.schedule_slot("2026-09-28", "23:00", self.ad, "华东")  # 23:00–23:05
        # 次日 00:30 同赞助商节目，间隔 85 < 90
        with self.assertRaisesRegex(DomainError, "青柠"):
            self.db.schedule_slot("2026-09-28", "00:30", self.show, "华东")
        # 00:35 间隔 90，放行
        self.db.schedule_slot("2026-09-28", "00:35", self.show, "华东")

    def test_license_window_checked_against_calendar_days_touched(self):
        short = self.db.add_program("年末特辑", "talk", 90, "2026-12-31", "2026-12-31", None, 0, ["华东"])
        # 23:30 开播会盖到 2027-01-01，超出授权
        with self.assertRaisesRegex(DomainError, "授权窗口"):
            self.db.schedule_slot("2026-12-31", "23:30", short, "华东")

    def test_replace_overnight_slot_validates_both_sides_and_keeps_original(self):
        # 09-29 凌晨 00:00–00:30 属于 09-28 播出日
        slot = self.db.schedule_slot("2026-09-28", "00:00", self.news, "华东")
        # 凌晨侧邻居 01:00–01:30：与当前 30 分钟档不重叠（间隔 30 分钟），
        # 但替换成 120 分钟节目后会越过零点一路顶到 02:00，必然撞它。
        neighbor = self.db.schedule_slot("2026-09-28", "01:00", self.news, "华东")
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.replace_slot(slot, self.music)
        kept = self.db.get_slot(slot)
        self.assertEqual(self.news, kept["program_id"])
        self.assertEqual("planned", kept["status"])
        self.assertEqual("2026-09-29T00:00", kept["start_at"])
        # 邻居移开后再替换，成功
        self.db.move_slot(neighbor, "2026-09-27", "23:15")
        replaced = self.db.replace_slot(slot, self.music)
        self.assertEqual(self.music, replaced["program_id"])
        self.assertEqual("2026-09-29T02:00", replaced["end_at"])

    def test_move_validates_against_neighbor_days_and_bumps_versions(self):
        slot = self.db.schedule_slot("2026-09-28", "02:00", self.news, "华东")
        self.assertEqual(1, self.db.day_view("2026-09-28")["version"])
        moved = self.db.move_slot(slot, "2026-09-29", "02:00")
        self.assertEqual("2026-09-29", moved["broadcast_day"])
        self.assertEqual("2026-09-30T02:00", moved["start_at"])
        self.assertEqual(2, self.db.day_view("2026-09-28")["version"])
        self.assertEqual(1, self.db.day_view("2026-09-29")["version"])

    def test_optimistic_concurrency_second_editor_sees_updated_day(self):
        first = self.db.schedule_slot("2026-09-28", "09:00", self.news, "华东")  # version -> 1
        # 编辑 B 基于版本 1 打开计划页；编辑 A 先加了一条，版本推进到 2
        self.db.schedule_slot("2026-09-28", "10:00", self.news, "华东")
        with self.assertRaises(ConflictError) as ctx:
            self.db.schedule_slot("2026-09-28", "11:00", self.news, "华东", expected_version=1)
        day = ctx.exception.payload
        self.assertEqual(2, day["version"])
        self.assertEqual(2, len(day["slots"]))
        self.assertIn(first, {s["id"] for s in day["slots"]})
        # 看到对方改动后带新版本重试即可成功
        self.db.schedule_slot("2026-09-28", "11:00", self.news, "华东", expected_version=2)

    def test_concurrent_editors_serialized_by_write_lock(self):
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def editor(start_time):
            try:
                barrier.wait()
                self.db.schedule_slot("2026-09-28", start_time, self.news, "华东")
            except Exception as exc:  # noqa: BLE001 - 记录线程内错误
                errors.append(exc)

        t1 = threading.Thread(target=editor, args=("09:00",))
        t2 = threading.Thread(target=editor, args=("10:00",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual([], [str(e) for e in errors])
        self.assertEqual(2, len(self.db.day_view("2026-09-28")["slots"]))

    def test_reconcile_uses_version_at_playout_time_after_move(self):
        # 09-28 23:00 排新闻，实播登记后把档口移走；对账仍按实播当时的版本进入 09-28
        slot = self.db.schedule_slot("2026-09-28", "23:00", self.news, "华东")
        self.db.record_playout(slot, "23:00", 30, self.news, "正常播出")
        self.db.move_slot(slot, "2026-09-29", "23:00")
        self.assertEqual([], self.db.reconcile_date("2026-09-28"))  # 按旧版对齐，不报漏播
        kinds = {(row["slot_id"], row["kind"]) for row in self.db.reconcile_date("2026-09-29")}
        self.assertIn((slot, "missed"), kinds)  # 移动后的新位置没有实播

    def test_reconcile_wrong_program_against_pinned_revision(self):
        slot = self.db.schedule_slot("2026-09-28", "23:00", self.news, "华东")
        self.db.record_playout(slot, "23:00", 30, self.ad, "临时替广告")
        self.db.replace_slot(slot, self.ad)  # 事后改成广告：计划版本变化
        kinds = {row["kind"] for row in self.db.reconcile_date("2026-09-28")}
        self.assertIn("wrong_program", kinds)  # 实播当时的版本是新闻

    def test_playout_clock_only_resolves_overnight(self):
        slot = self.db.schedule_slot("2026-09-28", "00:30", self.news, "华东")  # 日历 29 日
        log_id = self.db.record_playout(slot, "00:35", 25)
        log = self.db.conn.execute("SELECT * FROM playout_logs WHERE id=?", (log_id,)).fetchone()
        self.assertEqual("2026-09-29T00:35", log["actual_start_at"])
        self.assertEqual(1, log["plan_revision"])


class LegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        conn = sqlite3.connect(self.path)
        # 手工搭一个旧结构库（日历日期 + 钟点）。禁播表不带 CHECK，
        # 模拟从更老来源导入、允许存在 start>=end 的跨日窗口。
        conn.executescript(
            """
            CREATE TABLE programs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, kind TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
              sponsor TEXT, cooldown_minutes INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
              UNIQUE(title, start_date, end_date));
            CREATE TABLE program_regions (
              program_id INTEGER NOT NULL REFERENCES programs(id), region TEXT NOT NULL,
              PRIMARY KEY(program_id, region));
            CREATE TABLE blocked_windows (
              id INTEGER PRIMARY KEY AUTOINCREMENT, region TEXT NOT NULL, weekday INTEGER NOT NULL,
              start_time TEXT NOT NULL, end_time TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE sponsor_policies (
              sponsor TEXT PRIMARY KEY, min_gap_minutes INTEGER NOT NULL);
            CREATE TABLE slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT, air_date TEXT NOT NULL, start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL, program_id INTEGER NOT NULL, region TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'planned', replaced_from INTEGER, created_at TEXT NOT NULL);
            CREATE TABLE playout_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, slot_id INTEGER NOT NULL,
              actual_start TEXT NOT NULL, actual_duration_minutes INTEGER NOT NULL,
              actual_program_id INTEGER, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE reconciliation_exceptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, air_date TEXT NOT NULL, slot_id INTEGER NOT NULL,
              kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(air_date, slot_id, kind));
            INSERT INTO programs(title,kind,duration_minutes,start_date,end_date,cooldown_minutes)
              VALUES('凌晨档','talk',30,'2026-01-01','2026-12-31',0);
            INSERT INTO program_regions VALUES(1,'华东');
            INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,created_at)
              VALUES('2026-09-29','02:00',30,1,'华东','t');
            INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,created_at)
              VALUES(1,'02:00',30,'t');
            INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason)
              VALUES('华北',2,'23:00','01:00','旧跨日窗口');
            INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason)
              VALUES('华东',0,'08:00','08:30','周一设备检修');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_legacy_upgrade_reattributes_slots_and_splits_windows(self):
        db = RadioDB(self.path)
        try:
            slot = db.get_slot(1)
            self.assertEqual("2026-09-28", slot["broadcast_day"])  # 02:00 按开播时刻归入前一播出日
            self.assertEqual("2026-09-29T02:00", slot["start_at"])
            self.assertEqual([], db.day_view("2026-09-29")["slots"])
            log = db.conn.execute("SELECT * FROM playout_logs WHERE id=1").fetchone()
            self.assertEqual("2026-09-29T02:00", log["actual_start_at"])
            self.assertEqual("2026-09-29T02:00", log["plan_start_at"])
            self.assertEqual(1, log["plan_revision"])
            segments = db.conn.execute(
                "SELECT * FROM blocked_windows WHERE reason='旧跨日窗口' ORDER BY weekday,start_minute"
            ).fetchall()
            self.assertEqual(2, len(segments))
            self.assertEqual([2, 3], [s["weekday"] for s in segments])
            self.assertEqual((_clock_minutes("23:00"), 1440),
                             (segments[0]["start_minute"], segments[0]["end_minute"]))
            self.assertEqual((0, _clock_minutes("01:00")),
                             (segments[1]["start_minute"], segments[1]["end_minute"]))
            # 不跨日的窗口原样保留
            normal = db.conn.execute(
                "SELECT * FROM blocked_windows WHERE reason='周一设备检修'"
            ).fetchall()
            self.assertEqual(1, len(normal))
            # 升级后的库可继续工作：02:00 凌晨档归属正确，新增按时间轴校验
            from database import DomainError as DE
            with self.assertRaises(DE):
                db.schedule_slot("2026-09-28", "02:10",
                                 db.conn.execute("SELECT id FROM programs").fetchone()[0], "华东")
        finally:
            db.close()


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        handler_cls = app_module.Handler
        handler_cls.db = app_module.RadioDB(self.path)
        self.server = app_module.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.db = handler_cls.db
        self.pid = self.db.add_program("网测新闻", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.db.close()
        os.unlink(self.path)

    def _post(self, path: str, body: dict):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())

    def test_stale_version_returns_409_with_other_editors_slots(self):
        status, payload = self._post("/api/schedule", {
            "air_date": "2026-09-28", "start_time": "09:00", "program_id": self.pid, "region": "华东",
        })
        self.assertEqual(201, status)
        sid = payload["id"]
        # 另一名编辑先提交，版本推进
        status, _ = self._post("/api/schedule", {
            "air_date": "2026-09-28", "start_time": "10:00", "program_id": self.pid, "region": "华东",
        })
        self.assertEqual(201, status)
        # 本端仍基于版本 1 提交 -> 409，响应里带对方已改的时段
        status, payload = self._post(f"/api/slots/{sid}/move", {
            "air_date": "2026-09-28", "start_time": "11:00", "expected_version": 1,
        })
        self.assertEqual(409, status)
        self.assertTrue(payload["conflict"])
        self.assertEqual(2, len(payload["day"]["slots"]))

    def test_move_endpoint_uses_broadcast_day(self):
        status, created = self._post("/api/schedule", {
            "air_date": "2026-09-28", "start_time": "02:00", "program_id": self.pid, "region": "华东",
        })
        self.assertEqual(201, status)
        status, payload = self._post(f"/api/slots/{created['id']}/move", {
            "air_date": "2026-09-29", "start_time": "03:00",
        })
        self.assertEqual(200, status)
        self.assertEqual("2026-09-29", payload["slot"]["broadcast_day"])
        self.assertEqual("2026-09-30T03:00", payload["slot"]["start_at"])


if __name__ == "__main__":
    unittest.main()
