from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, date, time, timedelta


class DomainError(ValueError):
    """A business-rule violation that should be shown to the API caller."""


class ConflictError(DomainError):
    """Optimistic-lock conflict; payload carries the other editor's day view."""

    def __init__(self, message: str, payload: dict) -> None:
        super().__init__(message)
        self.payload = payload


PROGRAM_KINDS = {"music", "ad", "talk", "live"}
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# 播出日从早上 06:00 开始，到次日 06:00 结束。
DAY_START_MINUTE = 6 * 60
ISO_FMT = "%Y-%m-%dT%H:%M"


def _clock_minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _parse_day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise DomainError("播出日必须使用 YYYY-MM-DD") from exc


def _fmt_clock(total_minutes: int) -> str:
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}"


def _iso(value: datetime) -> str:
    return value.strftime(ISO_FMT)


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, ISO_FMT)


def _broadcast_day_of(moment: datetime) -> date:
    """开播时刻所属的播出日：06:00 之前算前一天。"""
    if moment.time() >= time(6, 0):
        return moment.date()
    return moment.date() - timedelta(days=1)


def _resolve_start(broadcast_day: date, clock_minutes: int) -> datetime:
    """播出日 + 钟点 -> 绝对开播时刻；钟点早于 06:00 落在次日凌晨。"""
    clock = time(clock_minutes // 60, clock_minutes % 60)
    calendar_day = broadcast_day if clock_minutes >= DAY_START_MINUTE else broadcast_day + timedelta(days=1)
    return datetime.combine(calendar_day, clock)


class RadioDB:
    """SQLite-backed radio scheduling service.

    所有时段都以绝对时间（start_at/end_at）为准，播出日只是 06:00 切日的
    归集口径：重叠、授权、禁播、冷却、赞助间隔全部在时间轴上判断，因此节目
    可以从前一晚一直连到次日凌晨。

    计划的每次修改都会留下 slot_revisions；实播登记时钉住当时的版本，
    对账永远拿实播当时的计划来比，事后移动/替换不会改写历史。
    """

    def __init__(self, path: str = "radio.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.row_lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        # BEGIN IMMEDIATE 让两名编辑对同一播出日的并发提交在 SQLite 层串行化，
        # 后到者在锁内做版本比对，必然能看到先到者已写入的时段。
        # row_lock 保证同一进程内多线程不会交错使用同一个 sqlite 连接。
        with self.row_lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # ---------------------------------------------------------------- schema

    def _schema(self) -> None:
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        slot_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(slots)").fetchall()}
        legacy_slots = "slots" in tables and "broadcast_day" not in slot_cols

        try:
            if legacy_slots:
                # 旧表按“日历日期 + 钟点”存放，先改名再按新模型重建并搬迁。
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute("ALTER TABLE slots RENAME TO slots_old")
                self.conn.execute("ALTER TABLE blocked_windows RENAME TO blocked_windows_old")
                self.conn.execute(
                    "ALTER TABLE playout_logs ADD COLUMN plan_revision INTEGER"
                )
                self.conn.execute("ALTER TABLE playout_logs ADD COLUMN plan_start_at TEXT")
                self.conn.execute("ALTER TABLE playout_logs ADD COLUMN actual_start_at TEXT")
                self._create_tables()
                self._migrate_legacy()
                self.conn.execute("DROP TABLE slots_old")
                self.conn.execute("DROP TABLE blocked_windows_old")
                self.conn.commit()
            else:
                self._create_tables()
                self._ensure_column("playout_logs", "plan_revision", "INTEGER")
                self._ensure_column("playout_logs", "plan_start_at", "TEXT")
                self._ensure_column("playout_logs", "actual_start_at", "TEXT")
                self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cols = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _create_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS programs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              kind TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              start_date TEXT NOT NULL,
              end_date TEXT NOT NULL,
              sponsor TEXT,
              cooldown_minutes INTEGER NOT NULL DEFAULT 0 CHECK(cooldown_minutes >= 0),
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              UNIQUE(title, start_date, end_date)
            );
            CREATE TABLE IF NOT EXISTS program_regions (
              program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
              region TEXT NOT NULL,
              PRIMARY KEY(program_id, region)
            );
            CREATE TABLE IF NOT EXISTS sponsor_policies (
              sponsor TEXT PRIMARY KEY,
              min_gap_minutes INTEGER NOT NULL CHECK(min_gap_minutes >= 0)
            );
            -- 禁播以“段”存放：段自身不跨日（0 <= start < end <= 1440），
            -- 跨日窗口在写入/升级时拆成 23:xx-24:00 与 00:00-0x:xx 两段。
            CREATE TABLE IF NOT EXISTS blocked_windows (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              region TEXT NOT NULL,
              weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
              start_minute INTEGER NOT NULL CHECK(start_minute BETWEEN 0 AND 1440),
              end_minute INTEGER NOT NULL CHECK(end_minute BETWEEN 0 AND 1440),
              reason TEXT NOT NULL,
              seg_group INTEGER,
              CHECK(start_minute < end_minute)
            );
            CREATE TABLE IF NOT EXISTS slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              broadcast_day TEXT NOT NULL,
              start_at TEXT NOT NULL,
              end_at TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              program_id INTEGER NOT NULL REFERENCES programs(id),
              region TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'planned'
                CHECK(status IN ('planned','replaced','cancelled')),
              replaced_from INTEGER REFERENCES programs(id),
              revision INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_slots_day_region ON slots(broadcast_day, region);
            CREATE INDEX IF NOT EXISTS idx_slots_timeline ON slots(region, start_at, end_at);
            CREATE TABLE IF NOT EXISTS slot_revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              revision INTEGER NOT NULL,
              broadcast_day TEXT NOT NULL,
              start_at TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL,
              program_id INTEGER NOT NULL REFERENCES programs(id),
              replaced_from INTEGER REFERENCES programs(id),
              status TEXT NOT NULL,
              region TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(slot_id, revision)
            );
            CREATE TABLE IF NOT EXISTS day_versions (
              broadcast_day TEXT PRIMARY KEY,
              version INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS playout_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              actual_start TEXT NOT NULL,
              actual_start_at TEXT,
              actual_duration_minutes INTEGER NOT NULL CHECK(actual_duration_minutes >= 0),
              actual_program_id INTEGER REFERENCES programs(id),
              plan_revision INTEGER,
              plan_start_at TEXT,
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_exceptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              kind TEXT NOT NULL,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(air_date, slot_id, kind)
            );
            """
        )

    def _migrate_legacy(self) -> None:
        """旧数据升级：时段按开播时刻归入播出日；跨日禁播窗口拆成两段。"""
        old_blocked = self.conn.execute("SELECT * FROM blocked_windows_old").fetchall()
        for window in old_blocked:
            self._insert_blocked_segments(
                window["region"], window["weekday"],
                _clock_minutes(window["start_time"]), _clock_minutes(window["end_time"]),
                window["reason"],
            )

        old_slots = self.conn.execute("SELECT * FROM slots_old").fetchall()
        days: set[str] = set()
        for row in old_slots:
            start_dt = datetime.combine(_parse_day(row["air_date"]), datetime.strptime(row["start_time"], "%H:%M").time())
            day = _broadcast_day_of(start_dt)
            end_dt = start_dt + timedelta(minutes=row["duration_minutes"])
            self.conn.execute(
                "INSERT INTO slots(id,broadcast_day,start_at,end_at,duration_minutes,program_id,region,"
                "status,replaced_from,revision,created_at) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
                (row["id"], day.isoformat(), _iso(start_dt), _iso(end_dt), row["duration_minutes"],
                 row["program_id"], row["region"], row["status"], row["replaced_from"], row["created_at"]),
            )
            self.conn.execute(
                "INSERT INTO slot_revisions(slot_id,revision,broadcast_day,start_at,duration_minutes,"
                "program_id,replaced_from,status,region,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (row["id"], 1, day.isoformat(), _iso(start_dt), row["duration_minutes"],
                 row["program_id"], row["replaced_from"], row["status"], row["region"], row["created_at"]),
            )
            days.add(day.isoformat())

        # 旧实播只有钟点：按旧排期的日历日期补成绝对时刻，钉住第 1 版计划。
        logs = self.conn.execute(
            "SELECT l.*, s.air_date FROM playout_logs l JOIN slots_old s ON s.id=l.slot_id"
        ).fetchall()
        for log in logs:
            actual_dt = datetime.combine(_parse_day(log["air_date"]), datetime.strptime(log["actual_start"], "%H:%M").time())
            self.conn.execute(
                "UPDATE playout_logs SET plan_revision=1, plan_start_at=?, actual_start_at=? WHERE id=?",
                (_iso(actual_dt), _iso(actual_dt), log["id"]),
            )

        stamp = datetime.now().isoformat()
        for day in days:
            self.conn.execute(
                "INSERT INTO day_versions(broadcast_day,version,updated_at) VALUES(?,1,?)", (day, stamp)
            )

    # ------------------------------------------------------------- catalogues

    def add_program(self, title: str, kind: str, duration_minutes: int, start_date: str, end_date: str,
                    sponsor: str | None = None, cooldown_minutes: int = 0,
                    regions: list[str] | None = None) -> int:
        if not title.strip():
            raise DomainError("节目名称不能为空")
        if kind not in PROGRAM_KINDS:
            raise DomainError(f"不支持的节目类型: {kind}")
        if duration_minutes <= 0:
            raise DomainError("节目时长必须大于0")
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        if end < start:
            raise DomainError("授权结束日期不能早于开始日期")
        if cooldown_minutes < 0:
            raise DomainError("冷却时间不能为负数")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO programs(title,kind,duration_minutes,start_date,end_date,sponsor,cooldown_minutes) VALUES(?,?,?,?,?,?,?)",
                (title.strip(), kind, duration_minutes, start_date, end_date, (sponsor or "").strip() or None, cooldown_minutes),
            )
            program_id = int(cur.lastrowid)
            for region in regions or []:
                self.conn.execute("INSERT INTO program_regions(program_id,region) VALUES(?,?)", (program_id, region.strip()))
        return program_id

    def authorize_region(self, program_id: int, region: str) -> None:
        if not region.strip():
            raise DomainError("地区不能为空")
        with self.transaction():
            if not self.conn.execute("SELECT 1 FROM programs WHERE id=?", (program_id,)).fetchone():
                raise DomainError("节目不存在")
            self.conn.execute("INSERT OR IGNORE INTO program_regions(program_id,region) VALUES(?,?)", (program_id, region.strip()))

    def add_sponsor_policy(self, sponsor: str, min_gap_minutes: int) -> None:
        if not sponsor.strip() or min_gap_minutes < 0:
            raise DomainError("赞助商和最小间隔必须有效")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO sponsor_policies(sponsor,min_gap_minutes) VALUES(?,?) "
                "ON CONFLICT(sponsor) DO UPDATE SET min_gap_minutes=excluded.min_gap_minutes",
                (sponsor.strip(), min_gap_minutes),
            )

    def _insert_blocked_segments(self, region: str, weekday: int, start_minute: int,
                                 end_minute: int, reason: str) -> int:
        """落禁播段；跨日（start>=end）拆成“当日深夜 + 次日凌晨”两段。"""
        if weekday not in range(7):
            raise DomainError("禁播时段星期无效")
        if not (0 <= start_minute <= 1440 and 0 <= end_minute <= 1440) or start_minute == end_minute:
            raise DomainError("禁播时段参数无效")
        text = reason.strip() or "禁播"
        if start_minute < end_minute:
            cur = self.conn.execute(
                "INSERT INTO blocked_windows(region,weekday,start_minute,end_minute,reason) VALUES(?,?,?,?,?)",
                (region.strip(), weekday, start_minute, end_minute, text),
            )
            group = int(cur.lastrowid)
            self.conn.execute("UPDATE blocked_windows SET seg_group=? WHERE id=?", (group, group))
            return group
        first = self.conn.execute(
            "INSERT INTO blocked_windows(region,weekday,start_minute,end_minute,reason) VALUES(?,?,?,?,?)",
            (region.strip(), weekday, start_minute, 1440, text),
        )
        group = int(first.lastrowid)
        self.conn.execute(
            "INSERT INTO blocked_windows(region,weekday,start_minute,end_minute,reason,seg_group) VALUES(?,?,?,?,?,?)",
            (region.strip(), (weekday + 1) % 7, 0, end_minute, text, group),
        )
        self.conn.execute("UPDATE blocked_windows SET seg_group=? WHERE id=?", (group, group))
        return group

    def add_blocked_window(self, region: str, weekday: int, start_time: str, end_time: str, reason: str) -> int:
        with self.transaction():
            return self._insert_blocked_segments(
                region.strip(), weekday, _clock_minutes(start_time), _clock_minutes(end_time), reason
            )

    # ------------------------------------------------------------- versions

    def _day_version(self, broadcast_day: str) -> int:
        row = self.conn.execute(
            "SELECT version FROM day_versions WHERE broadcast_day=?", (broadcast_day,)
        ).fetchone()
        return int(row["version"]) if row else 0

    def _assert_day_version(self, broadcast_day: str, expected: int | None) -> None:
        if expected is None:
            return
        current = self._day_version(broadcast_day)
        if current != expected:
            raise ConflictError(
                f"播出日 {broadcast_day} 已被其他编辑修改（当前版本 {current}，你提交基于版本 {expected}），请先查看最新计划",
                self.day_view(broadcast_day),
            )

    def _bump_day(self, broadcast_day: str) -> None:
        self.conn.execute(
            "INSERT INTO day_versions(broadcast_day,version,updated_at) VALUES(?,1,?) "
            "ON CONFLICT(broadcast_day) DO UPDATE SET version=version+1, updated_at=excluded.updated_at",
            (broadcast_day, datetime.now().isoformat()),
        )

    def day_view(self, broadcast_day: str) -> dict:
        """后提交的编辑在冲突响应里看到的该播出日最新时段。"""
        slots = [dict(row) for row in self.conn.execute(
            "SELECT s.id,s.broadcast_day,s.start_at,s.end_at,s.duration_minutes,s.program_id,"
            "s.region,s.status,s.revision,p.title FROM slots s JOIN programs p ON p.id=s.program_id "
            "WHERE s.broadcast_day=? ORDER BY s.start_at",
            (broadcast_day,),
        ).fetchall()]
        return {"broadcast_day": broadcast_day, "version": self._day_version(broadcast_day), "slots": slots}

    # ------------------------------------------------------------ validation

    @staticmethod
    def _slot_label(row) -> str:
        return (f"#{row['id']}（{row['title']}，"
                f"{_parse_iso(row['start_at']).strftime('%m-%d %H:%M')}–"
                f"{_parse_iso(row['end_at']).strftime('%m-%d %H:%M')}）")

    def _blocked_conflict(self, region: str, start_dt: datetime, end_dt: datetime):
        """时间轴上逐日查禁播段；跨日窗口的两段会分别落在相邻日历日。"""
        first_day = start_dt.date()
        last_day = (end_dt - timedelta(minutes=1)).date()
        weekdays: dict[int, date] = {}
        cursor = first_day
        while cursor <= last_day:
            weekdays[cursor.weekday()] = cursor
            cursor += timedelta(days=1)
        if not weekdays:
            return None
        marks = ",".join("?" * len(weekdays))
        rows = self.conn.execute(
            f"SELECT * FROM blocked_windows WHERE region=? AND weekday IN ({marks})",
            [region, *weekdays.keys()],
        ).fetchall()
        for window in rows:
            win_start = datetime.combine(weekdays[window["weekday"]], time.min) + timedelta(minutes=window["start_minute"])
            win_end = datetime.combine(weekdays[window["weekday"]], time.min) + timedelta(minutes=window["end_minute"])
            if win_start < end_dt and start_dt < win_end:
                return window
        return None

    def _validate_timeline(self, start_dt: datetime, duration: int, program, region: str,
                           ignore_slot_id: int | None = None) -> None:
        if duration <= 0:
            raise DomainError("排期时长必须大于0")
        if not region.strip():
            raise DomainError("地区不能为空")
        end_dt = start_dt + timedelta(minutes=duration)

        # 授权日期窗口：跨零点节目会盖住两个日历日，两天都必须在授权期内。
        touched_days = {start_dt.date(), (end_dt - timedelta(minutes=1)).date()}
        for touched in touched_days:
            if not (program["start_date"] <= touched.isoformat() <= program["end_date"]):
                raise DomainError(f"{touched.isoformat()} 超出节目授权窗口（{program['start_date']} ~ {program['end_date']}）")
        if not self.conn.execute(
            "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (program["id"], region)
        ).fetchone():
            raise DomainError(f"节目未授权在{region}播出")

        window = self._blocked_conflict(region, start_dt, end_dt)
        if window:
            raise DomainError(
                f"与禁播时段冲突: {window['reason']}（周{window['weekday'] + 1} "
                f"{_fmt_clock(window['start_minute'])}–{_fmt_clock(window['end_minute'])}）"
            )

        cooldown = int(program["cooldown_minutes"] or 0)
        policy = None
        if program["sponsor"]:
            policy = self.conn.execute(
                "SELECT min_gap_minutes FROM sponsor_policies WHERE sponsor=?", (program["sponsor"],)
            ).fetchone()
        margin = max([cooldown, int(policy["min_gap_minutes"]) if policy else 0])

        # 向时间轴两侧各放宽一个冷却/赞助间隔，前后邻居（含相邻播出日）一次查全。
        candidates = self.conn.execute(
            "SELECT s.*, p.title, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id "
            "WHERE s.region=? AND s.status!='cancelled' AND s.id IS NOT ? "
            "AND s.start_at < ? AND s.end_at > ?",
            (region, ignore_slot_id or -1, _iso(end_dt + timedelta(minutes=margin)),
             _iso(start_dt - timedelta(minutes=margin))),
        ).fetchall()
        for other in candidates:
            o_start = _parse_iso(other["start_at"])
            o_end = _parse_iso(other["end_at"])
            if o_start < end_dt and start_dt < o_end:
                raise DomainError(f"与排期 {self._slot_label(other)} 时间重叠")
            gap = int((o_start - end_dt).total_seconds() // 60) if o_start >= end_dt else int(
                (start_dt - o_end).total_seconds() // 60
            )
            if other["program_id"] == program["id"] and cooldown and gap < cooldown:
                raise DomainError(
                    f"与排期 {self._slot_label(other)} 间隔仅 {gap} 分钟，"
                    f"不足节目冷却时间 {cooldown} 分钟"
                )
            if (policy and other["sponsor"] == program["sponsor"] and gap < int(policy["min_gap_minutes"])):
                raise DomainError(
                    f"与排期 {self._slot_label(other)} 间隔仅 {gap} 分钟，"
                    f"不足赞助商 {program['sponsor']} 要求的 {policy['min_gap_minutes']} 分钟间隔"
                )

    # -------------------------------------------------------------- mutations

    def _write_revision(self, slot_id: int, revision: int, broadcast_day: str, start_at: str,
                        duration: int, program_id: int, replaced_from, status: str, region: str) -> None:
        self.conn.execute(
            "INSERT INTO slot_revisions(slot_id,revision,broadcast_day,start_at,duration_minutes,"
            "program_id,replaced_from,status,region,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (slot_id, revision, broadcast_day, start_at, duration, program_id, replaced_from,
             status, region, datetime.now().isoformat()),
        )

    def schedule_slot(self, air_date: str, start_time: str, program_id: int, region: str,
                      expected_version: int | None = None) -> int:
        broadcast_day = _parse_day(air_date)
        try:
            clock = _clock_minutes(start_time)
        except ValueError as exc:
            raise DomainError("开始时间必须使用 HH:MM") from exc
        start_dt = _resolve_start(broadcast_day, clock)
        program = self.conn.execute("SELECT * FROM programs WHERE id=? AND active=1", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在或未启用")
        duration = int(program["duration_minutes"])
        end_dt = start_dt + timedelta(minutes=duration)
        with self.transaction():
            self._assert_day_version(broadcast_day.isoformat(), expected_version)
            self._validate_timeline(start_dt, duration, program, region)
            stamp = datetime.now().isoformat()
            cur = self.conn.execute(
                "INSERT INTO slots(broadcast_day,start_at,end_at,duration_minutes,program_id,region,"
                "revision,created_at) VALUES(?,?,?,?,?,?,1,?)",
                (broadcast_day.isoformat(), _iso(start_dt), _iso(end_dt), duration, program_id, region, stamp),
            )
            slot_id = int(cur.lastrowid)
            self._write_revision(slot_id, 1, broadcast_day.isoformat(), _iso(start_dt), duration,
                                 program_id, None, "planned", region)
            self._bump_day(broadcast_day.isoformat())
        return slot_id

    def move_slot(self, slot_id: int, air_date: str, start_time: str,
                  expected_version: int | None = None,
                  expected_target_version: int | None = None) -> dict:
        """移动档口（常用于跨零点节目前后挪动）。

        新位置在时间轴上与前后两侧（含相邻播出日）一次性校验；任一冲突都回滚，
        原安排保持不变。跨播出日移动时源日与目标日的版本都要核对。
        """
        broadcast_day = _parse_day(air_date)
        try:
            clock = _clock_minutes(start_time)
        except ValueError as exc:
            raise DomainError("开始时间必须使用 HH:MM") from exc
        start_dt = _resolve_start(broadcast_day, clock)
        with self.transaction():
            slot = self.conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("排期不存在")
            if slot["status"] == "cancelled":
                raise DomainError("已取消的排期不能移动")
            self._assert_day_version(slot["broadcast_day"], expected_version)
            if broadcast_day.isoformat() != slot["broadcast_day"]:
                self._assert_day_version(broadcast_day.isoformat(), expected_target_version)
            program = self.conn.execute(
                "SELECT * FROM programs WHERE id=? AND active=1", (slot["program_id"],)
            ).fetchone()
            duration = int(slot["duration_minutes"])
            self._validate_timeline(start_dt, duration, program, slot["region"], slot_id)
            revision = int(slot["revision"]) + 1
            end_at = _iso(start_dt + timedelta(minutes=duration))
            self.conn.execute(
                "UPDATE slots SET broadcast_day=?, start_at=?, end_at=?, revision=? WHERE id=?",
                (broadcast_day.isoformat(), _iso(start_dt), end_at, revision, slot_id),
            )
            self._write_revision(slot_id, revision, broadcast_day.isoformat(), _iso(start_dt), duration,
                                 slot["program_id"], slot["replaced_from"], slot["status"], slot["region"])
            self._bump_day(slot["broadcast_day"])
            if broadcast_day.isoformat() != slot["broadcast_day"]:
                self._bump_day(broadcast_day.isoformat())
        return self.get_slot(slot_id)

    def replace_slot(self, slot_id: int, new_program_id: int,
                     expected_version: int | None = None) -> dict:
        """替换计划节目并在时间轴上重新校验整盘；失败则原安排原样保留。"""
        with self.transaction():
            slot = self.conn.execute("SELECT * FROM slots WHERE id=? AND status!='cancelled'", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("排期不存在或已取消")
            self._assert_day_version(slot["broadcast_day"], expected_version)
            program = self.conn.execute("SELECT * FROM programs WHERE id=? AND active=1", (new_program_id,)).fetchone()
            if not program:
                raise DomainError("替换节目不存在或未启用")
            start_dt = _parse_iso(slot["start_at"])
            duration = int(program["duration_minutes"])
            self._validate_timeline(start_dt, duration, program, slot["region"], slot_id)
            revision = int(slot["revision"]) + 1
            self.conn.execute(
                "UPDATE slots SET program_id=?, duration_minutes=?, end_at=?, replaced_from=?, "
                "status='replaced', revision=? WHERE id=?",
                (new_program_id, duration, _iso(start_dt + timedelta(minutes=duration)),
                 slot["program_id"], revision, slot_id),
            )
            self._write_revision(slot_id, revision, slot["broadcast_day"], slot["start_at"], duration,
                                 new_program_id, slot["program_id"], "replaced", slot["region"])
            self._bump_day(slot["broadcast_day"])
        return self.get_slot(slot_id)

    def get_slot(self, slot_id: int) -> dict:
        row = self.conn.execute(
            "SELECT s.*, p.title, p.kind, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id WHERE s.id=?",
            (slot_id,),
        ).fetchone()
        if not row:
            raise DomainError("排期不存在")
        data = dict(row)
        data["start_clock"] = _parse_iso(data["start_at"]).strftime("%H:%M")
        return data

    # -------------------------------------------------------------- playout

    def _resolve_actual_start(self, plan_start: datetime, value: str) -> datetime:
        parsed = None
        for fmt in (ISO_FMT, "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        if parsed is not None:
            return parsed
        clock = datetime.strptime(value, "%H:%M").time()
        # 只给钟点时（跨零点实播常见），在计划日前后各一天里选离计划开播最近的那个。
        base = plan_start.date()
        candidates = [datetime.combine(base + timedelta(days=delta), clock) for delta in (-1, 0, 1)]
        return min(candidates, key=lambda candidate: abs((candidate - plan_start).total_seconds()))

    def record_playout(self, slot_id: int, actual_start: str, actual_duration_minutes: int,
                       actual_program_id: int | None = None, note: str = "") -> int:
        slot = self.conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise DomainError("排期不存在")
        if actual_duration_minutes < 0:
            raise DomainError("实际时长不能为负数")
        plan_start = _parse_iso(slot["start_at"])
        actual_dt = self._resolve_actual_start(plan_start, actual_start)
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO playout_logs(slot_id,actual_start,actual_start_at,actual_duration_minutes,"
                "actual_program_id,plan_revision,plan_start_at,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (slot_id, actual_start, _iso(actual_dt), actual_duration_minutes, actual_program_id,
                 int(slot["revision"]), slot["start_at"], note, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    # ----------------------------------------------------------- reconcile

    def _pinned_revision(self, slot_id: int, log) -> dict:
        revision = self.conn.execute(
            "SELECT * FROM slot_revisions WHERE slot_id=? AND revision=?",
            (slot_id, log["plan_revision"]),
        ).fetchone()
        if not revision:
            # 旧数据没有钉版本时，退回到实播登记时刻之前最近的一版。
            revision = self.conn.execute(
                "SELECT * FROM slot_revisions WHERE slot_id=? AND created_at<=? ORDER BY revision DESC LIMIT 1",
                (slot_id, log["created_at"]),
            ).fetchone()
        if not revision:
            revision = self.conn.execute(
                "SELECT * FROM slot_revisions WHERE slot_id=? ORDER BY revision DESC LIMIT 1", (slot_id,)
            ).fetchone()
        return dict(revision)

    def reconcile_date(self, air_date: str) -> list[dict]:
        """按播出日对账；每条实播都按其登记时钉住的计划版本来比。"""
        broadcast_day = _parse_day(air_date).isoformat()
        with self.transaction():
            self.conn.execute("DELETE FROM reconciliation_exceptions WHERE air_date=?", (broadcast_day,))
            current = self.conn.execute(
                "SELECT * FROM slots WHERE broadcast_day=? AND status!='cancelled' ORDER BY start_at",
                (broadcast_day,),
            ).fetchall()
            latest_logs = {
                row["slot_id"]: row for row in self.conn.execute(
                    "SELECT l.* FROM playout_logs l JOIN (SELECT slot_id, MAX(id) AS mid FROM playout_logs GROUP BY slot_id) m "
                    "ON l.id=m.mid"
                ).fetchall()
            }
            exceptions: list[tuple[int, str, str]] = []

            def evaluate(slot_id: int, plan: dict, log) -> None:
                plan_start = _parse_iso(plan["start_at"])
                plan_end = plan_start + timedelta(minutes=plan["duration_minutes"])
                if not log:
                    exceptions.append((slot_id, "missed", "没有实播记录"))
                    return
                actual_program_id = log["actual_program_id"] or plan["program_id"]
                if actual_program_id != plan["program_id"]:
                    exceptions.append((slot_id, "wrong_program",
                                       f"计划节目 #{plan['program_id']}，实播节目 #{actual_program_id}"))
                delta = log["actual_duration_minutes"] - plan["duration_minutes"]
                if abs(delta) > 30:
                    kind = "overrun" if delta > 0 else "underrun"
                    exceptions.append((slot_id, kind, f"与计划相差 {delta:+d} 分钟"))
                actual = self.conn.execute("SELECT * FROM programs WHERE id=?", (actual_program_id,)).fetchone()
                if actual:
                    region_ok = self.conn.execute(
                        "SELECT 1 FROM program_regions WHERE program_id=? AND region=?",
                        (actual_program_id, plan["region"]),
                    ).fetchone()
                    touched = {plan_start.date().isoformat(), (plan_end - timedelta(minutes=1)).date().isoformat()}
                    licensed = region_ok and all(
                        actual["start_date"] <= day <= actual["end_date"] for day in touched
                    )
                    if not licensed:
                        exceptions.append((slot_id, "out_of_license", "实播节目超出地区或日期授权"))

            seen: set[int] = set()
            for slot in current:
                seen.add(slot["id"])
                log = latest_logs.get(slot["id"])
                if not log:
                    evaluate(slot["id"], dict(slot), None)
                    continue
                pinned = self._pinned_revision(slot["id"], log)
                if pinned["broadcast_day"] == broadcast_day and pinned["start_at"] == log["plan_start_at"]:
                    # 实播钉住的就是本播出日的当前位置，按实播当时的版本对账
                    evaluate(slot["id"], pinned, log)
                else:
                    # 档口事后才被移进本播出日：当前版本没有对应实播 -> 漏播；
                    # 旧实播仍归它登记时所在的播出日对账。
                    evaluate(slot["id"], dict(slot), None)
                    if pinned["broadcast_day"] == broadcast_day:
                        evaluate(slot["id"], pinned, log)

            # 已移走/取消但实播登记在本播出日的：仍按实播当时的版本进入本播出日对账。
            for slot_id, log in latest_logs.items():
                if slot_id in seen:
                    continue
                plan = self._pinned_revision(slot_id, log)
                if plan["broadcast_day"] == broadcast_day:
                    evaluate(slot_id, plan, log)

            for slot_id, kind, detail in exceptions:
                self.conn.execute(
                    "INSERT OR IGNORE INTO reconciliation_exceptions(air_date,slot_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                    (broadcast_day, slot_id, kind, detail, datetime.now().isoformat()),
                )
        return self.get_exceptions(broadcast_day)

    def get_exceptions(self, air_date: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions WHERE air_date=? ORDER BY slot_id, kind", (air_date,)
        ).fetchall()]

    def snapshot(self) -> dict:
        programs = [dict(row) for row in self.conn.execute("SELECT * FROM programs ORDER BY id").fetchall()]
        slots = []
        for row in self.conn.execute(
            "SELECT s.*, p.title, p.kind FROM slots s JOIN programs p ON p.id=s.program_id ORDER BY s.start_at"
        ).fetchall():
            item = dict(row)
            item["start_clock"] = _parse_iso(item["start_at"]).strftime("%H:%M")
            slots.append(item)
        blocked = []
        for row in self.conn.execute("SELECT * FROM blocked_windows ORDER BY weekday, start_minute").fetchall():
            item = dict(row)
            item["start_time"] = _fmt_clock(item.pop("start_minute"))
            item["end_time"] = _fmt_clock(item.pop("end_minute"))
            blocked.append(item)
        day_versions = {row["broadcast_day"]: row["version"] for row in self.conn.execute(
            "SELECT * FROM day_versions"
        ).fetchall()}
        return {
            "programs": programs,
            "slots": slots,
            "blocked_windows": blocked,
            "day_versions": day_versions,
            "exceptions": [dict(row) for row in self.conn.execute(
                "SELECT * FROM reconciliation_exceptions ORDER BY id DESC LIMIT 50"
            ).fetchall()],
        }

    def seed_demo(self) -> None:
        existing = self.conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
        if existing:
            return
        music = self.add_program("晨间轻音乐", "music", 30, "2026-01-01", "2026-12-31", "青柠饮品", 45, ["华东"])
        news = self.add_program("城市早报", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        ad = self.add_program("青柠饮品广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠饮品", 60, ["华东"])
        night = self.add_program("今夜不眠夜", "talk", 90, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.add_sponsor_policy("青柠饮品", 90)
        self.add_blocked_window("华东", 0, "08:00", "08:30", "周一设备检修")
        self.schedule_slot("2026-09-28", "09:00", music, "华东")
        self.schedule_slot("2026-09-28", "10:00", news, "华东")
        self.schedule_slot("2026-09-28", "11:00", ad, "华东")
        # 23:30 开播、90 分钟，从前一晚连到次日 01:00，仍属于 2026-09-28 播出日。
        self.schedule_slot("2026-09-28", "23:30", night, "华东")
