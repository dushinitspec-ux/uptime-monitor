"""SQLite storage: every check result, outages, and the current state of each device.

Timestamps are ISO 8601 strings in UTC with a fixed format, so string order is time order,
and the first 13 characters ("2026-10-24T09") are the hour - handy for hourly buckets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.checks import CheckResult
from app.state import DeviceState, Status

# The SQLite driver is imported when a database is opened, not here, so the plain data
# classes below (Incident, Counts) also load where it is missing: the in-browser demo
# (app/web_demo.py) uses them with an in-memory store.
if TYPE_CHECKING:
    import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id         INTEGER PRIMARY KEY,
    device     TEXT NOT NULL,
    ts         TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    latency_ms REAL,
    problems   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_checks_device_ts ON checks (device, ts);
CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks (ts);

CREATE TABLE IF NOT EXISTS incidents (
    id         INTEGER PRIMARY KEY,
    device     TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    problems   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS device_state (
    device           TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    failures         INTEGER NOT NULL,
    successes        INTEGER NOT NULL,
    first_failure_at TEXT,
    first_success_at TEXT,
    down_since       TEXT
);
"""

PROBLEM_SEPARATOR = "; "


def to_db(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def from_db(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class Incident:
    device: str
    started_at: datetime
    ended_at: datetime | None
    problems: str


@dataclass(frozen=True)
class Counts:
    ok: int
    total: int


class Repository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._db = conn

    @classmethod
    async def connect(cls, path: Path | str) -> Repository:
        import aiosqlite

        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._db.close()

    async def commit(self) -> None:
        await self._db.commit()

    # Writes (the caller commits)

    async def add_results(self, results: list[CheckResult]) -> None:
        await self._db.executemany(
            "INSERT INTO checks (device, ts, ok, latency_ms, problems) VALUES (?, ?, ?, ?, ?)",
            [
                (r.device, to_db(r.at), int(r.ok), r.latency_ms, PROBLEM_SEPARATOR.join(r.problems))
                for r in results
            ],
        )

    async def save_states(self, states: dict[str, DeviceState]) -> None:
        await self._db.executemany(
            """
            INSERT OR REPLACE INTO device_state
                (device, status, failures, successes, first_failure_at, first_success_at,
                 down_since)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    device,
                    s.status.value,
                    s.failures,
                    s.successes,
                    to_db(s.first_failure_at) if s.first_failure_at else None,
                    to_db(s.first_success_at) if s.first_success_at else None,
                    to_db(s.down_since) if s.down_since else None,
                )
                for device, s in states.items()
            ],
        )

    async def add_incidents(self, incidents: list[Incident]) -> None:
        await self._db.executemany(
            "INSERT INTO incidents (device, started_at, ended_at, problems) VALUES (?, ?, ?, ?)",
            [
                (
                    i.device,
                    to_db(i.started_at),
                    to_db(i.ended_at) if i.ended_at else None,
                    i.problems,
                )
                for i in incidents
            ],
        )

    async def close_incident(self, device: str, ended_at: datetime) -> None:
        await self._db.execute(
            "UPDATE incidents SET ended_at = ? WHERE device = ? AND ended_at IS NULL",
            (to_db(ended_at), device),
        )

    async def purge_checks(self, before: datetime) -> int:
        cur = await self._db.execute("DELETE FROM checks WHERE ts < ?", (to_db(before),))
        return cur.rowcount

    # Reads

    async def load_states(self) -> dict[str, DeviceState]:
        async with self._db.execute("SELECT * FROM device_state") as cur:
            rows = await cur.fetchall()
        return {
            row["device"]: DeviceState(
                status=Status(row["status"]),
                failures=row["failures"],
                successes=row["successes"],
                first_failure_at=from_db(row["first_failure_at"]),
                first_success_at=from_db(row["first_success_at"]),
                down_since=from_db(row["down_since"]),
            )
            for row in rows
        }

    async def has_history(self) -> bool:
        async with self._db.execute("SELECT 1 FROM checks LIMIT 1") as cur:
            return await cur.fetchone() is not None

    async def latest_results(self) -> dict[str, CheckResult]:
        async with self._db.execute(
            """
            SELECT c.* FROM checks c
              JOIN (SELECT device, MAX(id) AS id FROM checks GROUP BY device) last
                ON c.id = last.id
            """
        ) as cur:
            rows = await cur.fetchall()
        return {
            row["device"]: CheckResult(
                device=row["device"],
                ok=bool(row["ok"]),
                at=datetime.fromisoformat(row["ts"]),
                latency_ms=row["latency_ms"],
                problems=tuple(p for p in row["problems"].split(PROBLEM_SEPARATOR) if p),
            )
            for row in rows
        }

    async def counts_since(self, since: datetime) -> dict[str, Counts]:
        async with self._db.execute(
            "SELECT device, SUM(ok) AS ok, COUNT(*) AS total FROM checks WHERE ts >= ? "
            "GROUP BY device",
            (to_db(since),),
        ) as cur:
            return {row["device"]: Counts(row["ok"], row["total"]) for row in await cur.fetchall()}

    async def hourly_counts_since(self, since: datetime) -> dict[str, dict[str, Counts]]:
        """{device: {"2026-10-24T09": Counts}} - one bucket per UTC hour."""
        async with self._db.execute(
            "SELECT device, substr(ts, 1, 13) AS hour, SUM(ok) AS ok, COUNT(*) AS total "
            "FROM checks WHERE ts >= ? GROUP BY device, hour",
            (to_db(since),),
        ) as cur:
            rows = await cur.fetchall()
        result: dict[str, dict[str, Counts]] = {}
        for row in rows:
            result.setdefault(row["device"], {})[row["hour"]] = Counts(row["ok"], row["total"])
        return result

    async def recent_incidents(self, limit: int = 10) -> list[Incident]:
        async with self._db.execute(
            "SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [
            Incident(
                device=row["device"],
                started_at=datetime.fromisoformat(row["started_at"]),
                ended_at=from_db(row["ended_at"]),
                problems=row["problems"],
            )
            for row in rows
        ]
