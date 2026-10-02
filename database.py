from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


class DomainError(ValueError):
    """A business-rule violation that should be shown to the API caller."""


class ConflictError(DomainError):
    """The broadcast day was modified by someone else after the caller loaded it."""

    def __init__(self, state: dict) -> None:
        super().__init__("该播出日已被其他编辑修改，请刷新后再提交")
        self.state = state


PROGRAM_KINDS = {"music", "ad", "talk", "live"}
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# 播出日边界：早上六点。播出日 D = [D 06:00, D+1 06:00)。
BROADCAST_DAY_START_MINUTES = 6 * 60


def _minutes(value: str) -> int:
    """Parse HH:MM into minutes within the day. ``24:00`` means end-of-day."""
    if not isinstance(value, str):
        raise ValueError("时间必须是 HH:MM")
    if value == "24:00":
        return 24 * 60
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _broadcast_day(air_date: str, start_time: str) -> str:
    """Return the broadcast day that a slot starting at ``air_date start_time`` belongs to.

    A slot that starts before 06:00 belongs to the previous broadcast day.
    """
    day = datetime.strptime(air_date, "%Y-%m-%d").date()
    if _minutes(start_time) < BROADCAST_DAY_START_MINUTES:
        day = day - timedelta(days=1)
    return day.isoformat()


def _slot_abs(air_date: str, start_time: str, duration: int) -> tuple[int, int]:
    """Return the half-open absolute-minute interval [start, end) of a slot."""
    day = datetime.strptime(air_date, "%Y-%m-%d").date()
    start = day.toordinal() * 24 * 60 + _minutes(start_time)
    return start, start + duration


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


class RadioDB:
    """SQLite-backed radio scheduling service.

    The service keeps planning and actual playout separate. A replacement is
    accepted only when the complete plan remains valid; reconciliation never
    rewrites the plan, it records discrepancies for operators.

    Slots are stored by their broadcast day (06:00 boundary) and validated on
    a continuous timeline, so a programme that crosses midnight is checked
    against both calendar days at once.
    """

    def __init__(self, path: str = "radio.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._schema()
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
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
            CREATE TABLE IF NOT EXISTS blocked_windows (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              region TEXT NOT NULL,
              weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
              start_time TEXT NOT NULL,
              end_time TEXT NOT NULL,
              reason TEXT NOT NULL,
              CHECK(start_time < end_time)
            );
            CREATE TABLE IF NOT EXISTS sponsor_policies (
              sponsor TEXT PRIMARY KEY,
              min_gap_minutes INTEGER NOT NULL CHECK(min_gap_minutes >= 0)
            );
            CREATE TABLE IF NOT EXISTS slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              broadcast_day TEXT NOT NULL,
              start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              program_id INTEGER NOT NULL REFERENCES programs(id),
              region TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'planned'
                CHECK(status IN ('planned','replaced','cancelled')),
              replaced_from INTEGER REFERENCES programs(id),
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_slots_date_region ON slots(air_date, region);
            CREATE TABLE IF NOT EXISTS plan_versions (
              air_date TEXT PRIMARY KEY,
              version INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS playout_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              actual_start TEXT NOT NULL,
              actual_duration_minutes INTEGER NOT NULL CHECK(actual_duration_minutes >= 0),
              actual_program_id INTEGER REFERENCES programs(id),
              note TEXT NOT NULL DEFAULT '',
              planned_air_date TEXT,
              planned_broadcast_day TEXT,
              planned_start_time TEXT,
              planned_duration_minutes INTEGER,
              planned_program_id INTEGER REFERENCES programs(id),
              planned_region TEXT,
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
        self.conn.commit()

    def _columns(self, table: str) -> set[str]:
        return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _migrate(self) -> None:
        """Upgrade legacy date-only data to the broadcast-day model."""
        with self.transaction():
            slot_cols = self._columns("slots")
            if "broadcast_day" not in slot_cols:
                self.conn.execute("ALTER TABLE slots ADD COLUMN broadcast_day TEXT")
            # Backfill broadcast_day from air_date + start_time for legacy rows.
            legacy = self.conn.execute(
                "SELECT id, air_date, start_time FROM slots WHERE broadcast_day IS NULL OR broadcast_day=''"
            ).fetchall()
            for row in legacy:
                self.conn.execute(
                    "UPDATE slots SET broadcast_day=? WHERE id=?",
                    (_broadcast_day(row["air_date"], row["start_time"]), row["id"]),
                )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_slots_broadcast_day_region ON slots(broadcast_day, region)"
            )

            # Split legacy overnight ban windows (start_time >= end_time) into two
            # same-day segments. The current schema forbids them, but keep the
            # migration defensive for databases created without that check.
            overnight = self.conn.execute(
                "SELECT id, region, weekday, start_time, end_time, reason FROM blocked_windows "
                "WHERE start_time >= end_time"
            ).fetchall()
            for row in overnight:
                self.conn.execute("DELETE FROM blocked_windows WHERE id=?", (row["id"],))
                self.conn.execute(
                    "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES(?,?,?,?,?)",
                    (row["region"], row["weekday"], row["start_time"], "24:00", row["reason"]),
                )
                self.conn.execute(
                    "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES(?,?,?,?,?)",
                    (row["region"], (row["weekday"] + 1) % 7, "00:00", row["end_time"], row["reason"]),
                )

            log_cols = self._columns("playout_logs")
            for col, ddl in (
                ("planned_air_date", "TEXT"),
                ("planned_broadcast_day", "TEXT"),
                ("planned_start_time", "TEXT"),
                ("planned_duration_minutes", "INTEGER"),
                ("planned_program_id", "INTEGER"),
                ("planned_region", "TEXT"),
            ):
                if col not in log_cols:
                    self.conn.execute(f"ALTER TABLE playout_logs ADD COLUMN {col} {ddl}")
            # Snapshot the planned slot at playout time for legacy logs.
            legacy_logs = self.conn.execute(
                "SELECT id, slot_id FROM playout_logs WHERE planned_program_id IS NULL"
            ).fetchall()
            for row in legacy_logs:
                slot = self.conn.execute("SELECT * FROM slots WHERE id=?", (row["slot_id"],)).fetchone()
                if not slot:
                    continue
                self.conn.execute(
                    "UPDATE playout_logs SET planned_air_date=?, planned_broadcast_day=?, "
                    "planned_start_time=?, planned_duration_minutes=?, planned_program_id=?, planned_region=? "
                    "WHERE id=?",
                    (slot["air_date"], slot["broadcast_day"], slot["start_time"], slot["duration_minutes"],
                     slot["program_id"], slot["region"], row["id"]),
                )

    def seed_demo(self) -> None:
        existing = self.conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
        if existing:
            return
        music = self.add_program("晨间轻音乐", "music", 30, "2026-01-01", "2026-12-31", "青柠饮品", 45, ["华东"])
        news = self.add_program("城市早报", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        ad = self.add_program("青柠饮品广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠饮品", 60, ["华东"])
        self.add_sponsor_policy("青柠饮品", 90)
        self.add_blocked_window("华东", 0, "08:00", "08:30", "周一设备检修")
        self.schedule_slot("2026-09-28", "09:00", music, "华东")
        self.schedule_slot("2026-09-28", "10:00", news, "华东")
        self.schedule_slot("2026-09-28", "11:00", ad, "华东")

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
                "INSERT INTO programs(title,kind,duration_minutes,start_date,end_date,sponsor,cooldown_minutes) "
                "VALUES(?,?,?,?,?,?,?)",
                (title.strip(), kind, duration_minutes, start_date, end_date, (sponsor or "").strip() or None,
                 cooldown_minutes),
            )
            program_id = int(cur.lastrowid)
            for region in regions or []:
                self.conn.execute(
                    "INSERT INTO program_regions(program_id,region) VALUES(?,?)",
                    (program_id, region.strip()),
                )
        return program_id

    def authorize_region(self, program_id: int, region: str) -> None:
        if not region.strip():
            raise DomainError("地区不能为空")
        with self.transaction():
            if not self.conn.execute("SELECT 1 FROM programs WHERE id=?", (program_id,)).fetchone():
                raise DomainError("节目不存在")
            self.conn.execute(
                "INSERT OR IGNORE INTO program_regions(program_id,region) VALUES(?,?)",
                (program_id, region.strip()),
            )

    def add_sponsor_policy(self, sponsor: str, min_gap_minutes: int) -> None:
        if not sponsor.strip() or min_gap_minutes < 0:
            raise DomainError("赞助商和最小间隔必须有效")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO sponsor_policies(sponsor,min_gap_minutes) VALUES(?,?) "
                "ON CONFLICT(sponsor) DO UPDATE SET min_gap_minutes=excluded.min_gap_minutes",
                (sponsor.strip(), min_gap_minutes),
            )

    def add_blocked_window(self, region: str, weekday: int, start_time: str, end_time: str, reason: str) -> int:
        if weekday not in range(7):
            raise DomainError("禁播时段参数无效")
        try:
            start_min = _minutes(start_time)
            end_min = _minutes(end_time)
        except ValueError as exc:
            raise DomainError("禁播时间必须使用 HH:MM") from exc
        if start_min == end_min:
            raise DomainError("禁播时段参数无效")
        region = region.strip()
        reason = reason.strip() or "禁播"
        with self.transaction():
            if start_min < end_min:
                cur = self.conn.execute(
                    "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES(?,?,?,?,?)",
                    (region, weekday, start_time, end_time, reason),
                )
                return int(cur.lastrowid)
            # Overnight ban window: split into two same-day segments, e.g.
            # 23:00-02:00 becomes (weekday 23:00-24:00) and (weekday+1 00:00-02:00).
            cur = self.conn.execute(
                "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES(?,?,?,?,?)",
                (region, weekday, start_time, "24:00", reason),
            )
            first_id = int(cur.lastrowid)
            self.conn.execute(
                "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES(?,?,?,?,?)",
                (region, (weekday + 1) % 7, "00:00", end_time, reason),
            )
            return first_id

    # -- optimistic concurrency -------------------------------------------

    def _get_version(self, air_date: str) -> int:
        row = self.conn.execute("SELECT version FROM plan_versions WHERE air_date=?", (air_date,)).fetchone()
        return int(row["version"]) if row else 0

    def _bump_version(self, air_date: str) -> None:
        self.conn.execute(
            "INSERT INTO plan_versions(air_date,version,updated_at) VALUES(?,1,?) "
            "ON CONFLICT(air_date) DO UPDATE SET version=version+1, updated_at=excluded.updated_at",
            (air_date, datetime.now().isoformat()),
        )

    def _plan_state(self, air_date: str) -> dict:
        slots = [dict(row) for row in self.conn.execute(
            "SELECT s.*, p.title, p.kind FROM slots s JOIN programs p ON p.id=s.program_id "
            "WHERE s.broadcast_day=? ORDER BY s.start_time",
            (air_date,),
        ).fetchall()]
        return {"air_date": air_date, "version": self._get_version(air_date), "slots": slots}

    def _check_version(self, air_date: str, base_version: int | None) -> None:
        if base_version is not None and int(base_version) != self._get_version(air_date):
            raise ConflictError(self._plan_state(air_date))

    # -- timeline validation ----------------------------------------------

    def _active_slots(self, region: str, ignore_slot_id: int | None) -> list[sqlite3.Row]:
        sql = (
            "SELECT s.*, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id "
            "WHERE s.region=? AND s.status!='cancelled'"
        )
        params: list[object] = [region]
        if ignore_slot_id is not None:
            sql += " AND s.id!=?"
            params.append(ignore_slot_id)
        return self.conn.execute(sql, params).fetchall()

    def _validate_slot(self, air_date: str, start_time: str, duration: int, program_id: int,
                       region: str, ignore_slot_id: int | None = None) -> str:
        """Validate a candidate slot on the continuous timeline.

        Returns the broadcast day the slot belongs to. Raises DomainError on any
        overlap / license / ban / cooldown / sponsor violation, naming the
        conflicting plan slot where applicable.
        """
        try:
            day = datetime.strptime(air_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("播出日期必须使用 YYYY-MM-DD") from exc
        try:
            start_min = _minutes(start_time)
        except ValueError as exc:
            raise DomainError("开始时间必须使用 HH:MM") from exc
        if duration <= 0:
            raise DomainError("排期时长必须大于0")

        program = self.conn.execute("SELECT * FROM programs WHERE id=? AND active=1", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在或未启用")
        if program["duration_minutes"] != duration:
            raise DomainError(f"排期时长必须等于节目时长 {program['duration_minutes']} 分钟")

        broadcast_day = _broadcast_day(air_date, start_time)
        if not (program["start_date"] <= broadcast_day <= program["end_date"]):
            raise DomainError("播出超出授权窗口")
        if not self.conn.execute(
            "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (program_id, region)
        ).fetchone():
            raise DomainError(f"节目未授权在{region}播出")

        start_abs = day.toordinal() * 24 * 60 + start_min
        end_abs = start_abs + duration

        # Bans: iterate every calendar day the slot touches and check the
        # same-day ban windows for that weekday. Overnight windows are stored
        # as two same-day segments, but the check also tolerates a single
        # overnight row defensively.
        first_day_ord = start_abs // (24 * 60)
        last_day_ord = (end_abs - 1) // (24 * 60)
        for ordinal in range(first_day_ord, last_day_ord + 1):
            cal = datetime.fromordinal(ordinal).date()
            windows = self.conn.execute(
                "SELECT * FROM blocked_windows WHERE region=? AND weekday=?",
                (region, cal.weekday()),
            ).fetchall()
            for window in windows:
                win_start = ordinal * 24 * 60 + _minutes(window["start_time"])
                win_end = ordinal * 24 * 60 + _minutes(window["end_time"])
                if _minutes(window["start_time"]) >= _minutes(window["end_time"]):
                    win_end += 24 * 60  # overnight row (defensive)
                if _overlap(start_abs, end_abs, win_start, win_end):
                    raise DomainError(f"与禁播时段冲突: {window['reason']}")

        # Overlap / cooldown / sponsor all judged on the absolute timeline.
        slots = self._active_slots(region, ignore_slot_id)

        for existing in slots:
            e_start, e_end = _slot_abs(existing["air_date"], existing["start_time"], existing["duration_minutes"])
            if _overlap(start_abs, end_abs, e_start, e_end):
                raise DomainError(f"与排期 #{existing['id']} 时间重叠")

        if program["cooldown_minutes"]:
            prev_end: int | None = None
            next_start: int | None = None
            for existing in slots:
                if existing["program_id"] != program_id:
                    continue
                e_start, e_end = _slot_abs(existing["air_date"], existing["start_time"], existing["duration_minutes"])
                if e_end <= start_abs and (prev_end is None or e_end > prev_end):
                    prev_end = e_end
                if e_start >= end_abs and (next_start is None or e_start < next_start):
                    next_start = e_start
            if prev_end is not None and start_abs - prev_end < program["cooldown_minutes"]:
                raise DomainError(
                    f"与上一期同节目排期间隔不足冷却时间 {program['cooldown_minutes']} 分钟"
                )
            if next_start is not None and next_start - end_abs < program["cooldown_minutes"]:
                raise DomainError(
                    f"与下一期同节目排期间隔不足冷却时间 {program['cooldown_minutes']} 分钟"
                )

        if program["sponsor"]:
            policy = self.conn.execute(
                "SELECT min_gap_minutes FROM sponsor_policies WHERE sponsor=?", (program["sponsor"],)
            ).fetchone()
            if policy:
                gap = policy["min_gap_minutes"]
                for other in slots:
                    if other["sponsor"] != program["sponsor"]:
                        continue
                    o_start, o_end = _slot_abs(other["air_date"], other["start_time"], other["duration_minutes"])
                    if _overlap(start_abs, end_abs, o_start, o_end):
                        raise DomainError(f"与赞助商 {program['sponsor']} 的排期 #{other['id']} 时间重叠")
                    if end_abs <= o_start:
                        distance = o_start - end_abs
                    elif o_end <= start_abs:
                        distance = start_abs - o_end
                    else:
                        continue
                    if distance < gap:
                        raise DomainError(
                            f"与赞助商 {program['sponsor']} 的排期 #{other['id']} 间隔不足 {gap} 分钟"
                        )

        return broadcast_day

    def schedule_slot(self, air_date: str, start_time: str, program_id: int, region: str,
                      base_version: int | None = None) -> int:
        program = self.conn.execute("SELECT duration_minutes FROM programs WHERE id=?", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在")
        broadcast_day = _broadcast_day(air_date, start_time)
        with self.transaction():
            self._check_version(broadcast_day, base_version)
            self._validate_slot(air_date, start_time, int(program["duration_minutes"]), program_id, region)
            cur = self.conn.execute(
                "INSERT INTO slots(air_date,broadcast_day,start_time,duration_minutes,program_id,region,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (air_date, broadcast_day, start_time, int(program["duration_minutes"]), program_id, region,
                 datetime.now().isoformat()),
            )
            self._bump_version(broadcast_day)
        return int(cur.lastrowid)

    def move_slot(self, slot_id: int, new_air_date: str, new_start_time: str,
                  base_version: int | None = None) -> dict:
        """Move a slot to a new date/time and revalidate the resulting plan atomically.

        A cross-midnight slot is validated as a single timeline interval, so both
        calendar sides are checked in one pass. On failure the original plan is kept.
        """
        with self.transaction():
            slot = self.conn.execute(
                "SELECT * FROM slots WHERE id=? AND status!='cancelled'", (slot_id,)
            ).fetchone()
            if not slot:
                raise DomainError("只能移动尚未取消的排期")
            program = self.conn.execute("SELECT * FROM programs WHERE id=?", (slot["program_id"],)).fetchone()
            if not program:
                raise DomainError("节目不存在")
            new_broadcast_day = _broadcast_day(new_air_date, new_start_time)
            self._check_version(new_broadcast_day, base_version)
            self._validate_slot(
                new_air_date, new_start_time, int(program["duration_minutes"]),
                slot["program_id"], slot["region"], slot_id,
            )
            self.conn.execute(
                "UPDATE slots SET air_date=?, start_time=?, broadcast_day=? WHERE id=?",
                (new_air_date, new_start_time, new_broadcast_day, slot_id),
            )
            self._bump_version(new_broadcast_day)
            if slot["broadcast_day"] != new_broadcast_day:
                self._bump_version(slot["broadcast_day"])
        return self.get_slot(slot_id)

    def replace_slot(self, slot_id: int, new_program_id: int, base_version: int | None = None) -> dict:
        """Replace a planned item and revalidate the resulting plan atomically."""
        with self.transaction():
            slot = self.conn.execute("SELECT * FROM slots WHERE id=? AND status='planned'", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("只能替换尚未播出且状态为 planned 的排期")
            program = self.conn.execute("SELECT * FROM programs WHERE id=?", (new_program_id,)).fetchone()
            if not program:
                raise DomainError("替换节目不存在")
            broadcast_day = slot["broadcast_day"]
            self._check_version(broadcast_day, base_version)
            self._validate_slot(
                slot["air_date"], slot["start_time"], int(program["duration_minutes"]),
                new_program_id, slot["region"], slot_id,
            )
            self.conn.execute(
                "UPDATE slots SET program_id=?, duration_minutes=?, replaced_from=?, status='replaced' WHERE id=?",
                (new_program_id, int(program["duration_minutes"]), slot["program_id"], slot_id),
            )
            self._bump_version(broadcast_day)
        return self.get_slot(slot_id)

    def get_slot(self, slot_id: int) -> dict:
        row = self.conn.execute(
            "SELECT s.*, p.title, p.kind, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id "
            "WHERE s.id=?",
            (slot_id,),
        ).fetchone()
        if not row:
            raise DomainError("排期不存在")
        return dict(row)

    def record_playout(self, slot_id: int, actual_start: str, actual_duration_minutes: int,
                       actual_program_id: int | None = None, note: str = "") -> int:
        slot = self.conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise DomainError("排期不存在")
        if actual_duration_minutes < 0:
            raise DomainError("实际时长不能为负数")
        _minutes(actual_start)
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,actual_program_id,note,"
                "planned_air_date,planned_broadcast_day,planned_start_time,planned_duration_minutes,"
                "planned_program_id,planned_region,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (slot_id, actual_start, actual_duration_minutes, actual_program_id, note,
                 slot["air_date"], slot["broadcast_day"], slot["start_time"], slot["duration_minutes"],
                 slot["program_id"], slot["region"], datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def reconcile_date(self, air_date: str) -> list[dict]:
        """Compare the latest playout per slot with the plan version at broadcast time.

        The planned values are snapshotted when the playout is registered, so later
        edits (moves/replacements) never rewrite what an already-aired slot is
        compared against.
        """
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("播出日期必须使用 YYYY-MM-DD") from exc
        with self.transaction():
            self.conn.execute("DELETE FROM reconciliation_exceptions WHERE air_date=?", (air_date,))
            slots = self.conn.execute(
                "SELECT s.*, p.title, p.sponsor, p.kind FROM slots s JOIN programs p ON p.id=s.program_id "
                "WHERE s.broadcast_day=? AND s.status!='cancelled' ORDER BY s.start_time",
                (air_date,),
            ).fetchall()
            exceptions: list[tuple[int, str, str]] = []
            for slot in slots:
                log = self.conn.execute(
                    "SELECT * FROM playout_logs WHERE slot_id=? ORDER BY id DESC LIMIT 1", (slot["id"],)
                ).fetchone()
                if not log:
                    exceptions.append((slot["id"], "missed", "没有实播记录"))
                    continue
                planned_program_id = log["planned_program_id"] or slot["program_id"]
                planned_duration = log["planned_duration_minutes"] or slot["duration_minutes"]
                planned_region = log["planned_region"] or slot["region"]
                actual_program_id = log["actual_program_id"] or planned_program_id
                if actual_program_id != planned_program_id:
                    exceptions.append(
                        (slot["id"], "wrong_program", f"计划节目 #{planned_program_id}，实播节目 #{actual_program_id}")
                    )
                delta = log["actual_duration_minutes"] - planned_duration
                if abs(delta) > 30:
                    kind = "overrun" if delta > 0 else "underrun"
                    exceptions.append((slot["id"], kind, f"与计划相差 {delta:+d} 分钟"))
                actual = self.conn.execute(
                    "SELECT p.* FROM programs p WHERE p.id=?", (actual_program_id,)
                ).fetchone()
                if actual:
                    region_ok = self.conn.execute(
                        "SELECT 1 FROM program_regions WHERE program_id=? AND region=?",
                        (actual_program_id, planned_region),
                    ).fetchone()
                    if not region_ok or not (actual["start_date"] <= air_date <= actual["end_date"]):
                        exceptions.append((slot["id"], "out_of_license", "实播节目超出地区或日期授权"))
            for slot_id, kind, detail in exceptions:
                self.conn.execute(
                    "INSERT INTO reconciliation_exceptions(air_date,slot_id,kind,detail,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (air_date, slot_id, kind, detail, datetime.now().isoformat()),
                )
        return self.get_exceptions(air_date)

    def get_exceptions(self, air_date: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions WHERE air_date=? ORDER BY slot_id, kind", (air_date,)
        ).fetchall()]

    def snapshot(self) -> dict:
        programs = [dict(row) for row in self.conn.execute("SELECT * FROM programs ORDER BY id").fetchall()]
        slots = [dict(row) for row in self.conn.execute(
            "SELECT s.*, p.title, p.kind, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id "
            "ORDER BY s.broadcast_day, s.start_time"
        ).fetchall()]
        versions = {row["air_date"]: int(row["version"]) for row in self.conn.execute(
            "SELECT * FROM plan_versions"
        ).fetchall()}
        return {
            "programs": programs,
            "slots": slots,
            "versions": versions,
            "exceptions": [dict(row) for row in self.conn.execute(
                "SELECT * FROM reconciliation_exceptions ORDER BY id DESC LIMIT 50"
            ).fetchall()],
        }
