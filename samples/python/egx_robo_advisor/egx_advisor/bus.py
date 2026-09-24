"""The state bus: the single channel between the agent and the dashboard.

Two processes share one SQLite file in WAL mode:

    egx_cua_agent.py  --writes-->  [ events | snapshot ]  --reads-->  dashboard
    egx_cua_agent.py  --reads--->  [ control ]            <--writes-- dashboard

WAL gives us many concurrent readers alongside a single writer, atomic commits,
and durability across a crash, with no broker to install or keep alive on a
laptop. That matters more than throughput here: the agent emits tens of events
per minute, not thousands per second.

Why two processes at all, rather than one Streamlit app holding the agent?

  * The kill switch has to work when the agent loop is wedged mid-click. A
    button living inside the stuck process cannot be pressed.
  * The dashboard has to stay readable after the agent crashes, so the last
    thing it did is still on screen when you open your phone.
  * The dashboard is internet-exposed for mobile access; the agent, which drives
    a broker UI, is not. Keeping them apart keeps that boundary honest.

The halt flag is deliberately redundant: a row in `control` *and* a sentinel
file. Either one halts the bot, and `is_halted()` returns True when the read
itself fails. A kill switch that can be defeated by a corrupt database is not a
kill switch.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

logger = logging.getLogger(__name__)

SENTINEL_NAME = "HALTED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT    NOT NULL,
    kind    TEXT    NOT NULL,
    phase   TEXT    NOT NULL DEFAULT '',
    message TEXT    NOT NULL DEFAULT '',
    data    TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_kind_idx ON events(kind, seq);

CREATE TABLE IF NOT EXISTS snapshot (
    key     TEXT PRIMARY KEY,
    ts      TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    halted       INTEGER NOT NULL DEFAULT 1,
    reason       TEXT    NOT NULL DEFAULT 'initial state is halted until explicitly armed',
    actor        TEXT    NOT NULL DEFAULT 'system',
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS control_audit (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    halted     INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    actor      TEXT NOT NULL
);
"""


class EventKind(str, Enum):
    """Event taxonomy. `UI_ACTION` is what the dashboard's live log renders."""

    LIFECYCLE = "lifecycle"
    #: Every primitive the agent sends to the screen, with its coordinates.
    UI_ACTION = "ui_action"
    #: Demo-mode assertion outcomes.
    GUARD = "guard"
    REGIME = "regime"
    PLAN = "plan"
    ORDER = "order"
    CONTROL = "control"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    ts: datetime
    kind: str
    phase: str
    message: str
    data: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts.isoformat(),
            "kind": self.kind,
            "phase": self.phase,
            "message": self.message,
            "data": self.data,
        }


@dataclass(frozen=True, slots=True)
class ControlState:
    halted: bool
    reason: str
    actor: str
    updated_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "halted": self.halted,
            "reason": self.reason,
            "actor": self.actor,
            "updated_at": self.updated_at.isoformat(),
        }


def _encode(value: Any) -> Any:
    """JSON encoder that understands our frozen dataclasses and Decimals."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__} onto the bus")


def _dumps(payload: Any) -> str:
    return json.dumps(payload, default=_encode, ensure_ascii=False)


class StateBus:
    """Process-safe handle on the shared journal.

    Cheap to construct; hold one per process. Connections are thread-local
    because the dashboard serves concurrent requests and SQLite connections are
    not safe to share across threads.
    """

    def __init__(self, path: str | os.PathLike[str], *, retention: int = 20_000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sentinel = self.path.parent / SENTINEL_NAME
        self._retention = retention
        self._local = threading.local()
        # executescript() implicitly commits, so the schema goes on the bare
        # connection and only the seed row runs inside a transaction.
        self._conn.executescript(_SCHEMA)
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO control (id, halted, updated_at) VALUES (1, 1, ?)",
                (_now_iso(),),
            )

    # ----------------------------------------------------------------- plumbing

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        if conn.in_transaction:  # already inside an outer _write(); just join it
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            if conn.in_transaction:
                conn.execute("COMMIT")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self) -> "StateBus":
        return self

    def __exit__(self, *exc: object) -> None:
        # Windows refuses to delete a file SQLite still holds open, so a bus in
        # a temporary directory has to be closed before the directory goes.
        self.close()

    # ------------------------------------------------------------------- events

    def publish(
        self,
        kind: EventKind | str,
        message: str,
        *,
        phase: str = "",
        data: Optional[dict[str, Any]] = None,
    ) -> int:
        kind_value = kind.value if isinstance(kind, EventKind) else str(kind)
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO events (ts, kind, phase, message, data) VALUES (?, ?, ?, ?, ?)",
                (_now_iso(), kind_value, phase, message, _dumps(data or {})),
            )
            seq = int(cursor.lastrowid or 0)
            if self._retention and seq % 500 == 0:
                conn.execute(
                    "DELETE FROM events WHERE seq <= ?", (seq - self._retention,)
                )
        logger.debug("bus event %s [%s] %s", seq, kind_value, message)
        return seq

    def events_since(self, after_seq: int = 0, *, limit: int = 250) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after_seq, limit)
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def recent_events(self, *, limit: int = 100, kind: Optional[str] = None) -> list[Event]:
        """Newest-last window, for a dashboard that has just connected."""
        if kind:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE kind = ? ORDER BY seq DESC LIMIT ?", (kind, limit)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_event(r) for r in reversed(rows)]

    def latest_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM events").fetchone()
        return int(row["s"])

    # ----------------------------------------------------------------- snapshot

    def put(self, key: str, payload: Any) -> None:
        """Overwrite a named snapshot. Last write wins; the agent is sole writer."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO snapshot (key, ts, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts, payload=excluded.payload",
                (key, _now_iso(), _dumps(payload)),
            )

    def get(self, key: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT ts, payload FROM snapshot WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return {"ts": row["ts"], "payload": json.loads(row["payload"])}

    def all_snapshots(self) -> dict[str, Any]:
        rows = self._conn.execute("SELECT key, ts, payload FROM snapshot").fetchall()
        return {
            r["key"]: {"ts": r["ts"], "payload": json.loads(r["payload"])} for r in rows
        }

    # ------------------------------------------------------------------ control

    def halt(self, *, actor: str, reason: str) -> ControlState:
        """Engage the kill switch. Writes the sentinel file first, on purpose.

        If the process dies between the two writes we want to be left halted,
        never armed, so the more durable signal goes down first.
        """
        try:
            self.sentinel.write_text(
                f"{_now_iso()}\nactor={actor}\nreason={reason}\n", encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover - disk-full territory
            logger.error("could not write halt sentinel: %s", exc)
        return self._set_control(halted=True, actor=actor, reason=reason)

    def resume(self, *, actor: str, reason: str = "operator resumed") -> ControlState:
        """Release the kill switch. Never called automatically -- see README."""
        state = self._set_control(halted=False, actor=actor, reason=reason)
        try:
            self.sentinel.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover
            logger.error("could not clear halt sentinel: %s", exc)
            return self._set_control(
                halted=True, actor="system", reason=f"sentinel stuck: {exc}"
            )
        return state

    def _set_control(self, *, halted: bool, actor: str, reason: str) -> ControlState:
        ts = _now_iso()
        with self._write() as conn:
            conn.execute(
                "UPDATE control SET halted=?, reason=?, actor=?, updated_at=? WHERE id=1",
                (int(halted), reason, actor, ts),
            )
            conn.execute(
                "INSERT INTO control_audit (ts, halted, reason, actor) VALUES (?, ?, ?, ?)",
                (ts, int(halted), reason, actor),
            )
            conn.execute(
                "INSERT INTO events (ts, kind, phase, message, data) VALUES (?, ?, ?, ?, ?)",
                (
                    ts,
                    EventKind.CONTROL.value,
                    "",
                    f"{'HALT' if halted else 'RESUME'} by {actor}: {reason}",
                    _dumps({"halted": halted, "actor": actor}),
                ),
            )
        return ControlState(halted, reason, actor, datetime.fromisoformat(ts))

    def control_state(self) -> ControlState:
        """Current control state, biased towards halted whenever unsure."""
        if self.sentinel.exists():
            try:
                detail = self.sentinel.read_text(encoding="utf-8").strip().splitlines()
            except OSError:
                detail = []
            reason = next(
                (line.split("=", 1)[1] for line in detail if line.startswith("reason=")),
                "halt sentinel present",
            )
            actor = next(
                (line.split("=", 1)[1] for line in detail if line.startswith("actor=")),
                "unknown",
            )
            return ControlState(True, reason, actor, _mtime(self.sentinel))
        try:
            row = self._conn.execute("SELECT * FROM control WHERE id = 1").fetchone()
        except sqlite3.Error as exc:
            logger.error("control read failed, assuming halted: %s", exc)
            return ControlState(True, f"control read failed: {exc}", "system", _now())
        if row is None:
            return ControlState(True, "no control row", "system", _now())
        return ControlState(
            bool(row["halted"]),
            row["reason"],
            row["actor"],
            datetime.fromisoformat(row["updated_at"]),
        )

    def is_halted(self) -> bool:
        """Fail-closed halt check. Any doubt at all means halted."""
        try:
            return self.control_state().halted
        except Exception as exc:  # noqa: BLE001 - the whole point is to not raise
            logger.error("halt check failed, assuming halted: %s", exc)
            return True

    def control_audit(self, *, limit: int = 50) -> Sequence[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM control_audit ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                "ts": r["ts"],
                "halted": bool(r["halted"]),
                "reason": r["reason"],
                "actor": r["actor"],
            }
            for r in rows
        ]


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        seq=int(row["seq"]),
        ts=datetime.fromisoformat(row["ts"]),
        kind=row["kind"],
        phase=row["phase"],
        message=row["message"],
        data=json.loads(row["data"]),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _mtime(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:  # pragma: no cover
        return _now()
