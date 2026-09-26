"""The app's memory: what the operator told it, what it tested, what it picked.

The chat model forgets everything between questions. This file is what it
does not forget, kept on this computer next to the bus
(state/memory/memory.db), and shown and edited in the Memory tab:

- **notes**: the operator's preferences ("I prefer dividend stocks"), why they
  hold a stock (a thesis: why, risks, when to exit), and plain notes. Written
  by the operator, in the tab or by saying "remember that ..." in the chat.
  The model never writes here on its own.
- **conversation**: recent questions and answers, so a new chat can pick up
  where the last one ended.
- **lab journal**: every Strategy Lab run and its verdict, so a rule tested in
  March is not tested again as if new.
- **picks**: what the scan ranked first and what the bot's plan wanted to buy
  or sell, with the price at the time, so `review.py` can later say how those
  picks actually did.

What is here is handed to the model as the operator's own words, not as
verified fact, and it never unlocks an action: the bot still cannot press
Buy, whatever a note says.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

KINDS = ("preference", "thesis", "note")
MAX_TEXT = 2000
KEEP_EXCHANGES = 200
#: Caps on what one prompt carries, so memory cannot crowd out the question.
CONTEXT_NOTES = 30
CONTEXT_CHARS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'you',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lab_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    sessions INTEGER NOT NULL,
    synthetic INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE (day, source, symbol, side)
);
"""


def memory_path_for(bus_file: str | Path) -> Path:
    """Memory lives next to the bus: state/memory/memory.db."""
    return Path(bus_file).parent / "memory" / "memory.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Note:
    id: int
    kind: str
    symbol: str
    text: str
    source: str
    created: str
    updated: str


@dataclass(frozen=True)
class Exchange:
    ts: str
    question: str
    answer: str


@dataclass(frozen=True)
class LabRun:
    ts: str
    key: str
    title: str
    sessions: int
    synthetic: bool
    passed: bool
    verdict: str
    evidence: str


@dataclass(frozen=True)
class Pick:
    day: date
    source: str       # "scan" | "plan"
    symbol: str
    side: str         # "buy" | "sell"
    price: float
    reason: str


def _symbol(value: Optional[str]) -> str:
    value = (value or "").strip().upper()
    return value if not value or value.endswith(".CA") or ":" in value else value + ".CA"


class Memory:
    """SQLite-backed, one short connection per call: safe from any thread."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _write(self, sql: str, args: Sequence[Any] = ()) -> int:
        with closing(self._connect()) as conn, conn:
            return conn.execute(sql, args).lastrowid or 0

    def _read(self, sql: str, args: Sequence[Any] = ()) -> list[tuple]:
        with closing(self._connect()) as conn:
            return conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ notes

    def add_note(self, kind: str, text: str, symbol: str = "", source: str = "you") -> int:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            raise ValueError("a note needs text")
        now = _now()
        return self._write(
            "INSERT INTO notes (kind, symbol, text, source, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?)", (kind, _symbol(symbol), text, source, now, now))

    def update_note(self, note_id: int, kind: str, text: str, symbol: str = "") -> None:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            raise ValueError("a note needs text")
        self._write("UPDATE notes SET kind = ?, symbol = ?, text = ?, updated = ? WHERE id = ?",
                    (kind, _symbol(symbol), text, _now(), note_id))

    def delete_note(self, note_id: int) -> None:
        self._write("DELETE FROM notes WHERE id = ?", (note_id,))

    def notes(self, kind: Optional[str] = None) -> list[Note]:
        rows = self._read(
            "SELECT id, kind, symbol, text, source, created, updated FROM notes"
            + (" WHERE kind = ?" if kind else "") + " ORDER BY updated DESC",
            (kind,) if kind else ())
        return [Note(*row) for row in rows]

    # ------------------------------------------------------------ conversation

    def add_exchange(self, question: str, answer: str) -> None:
        self._write("INSERT INTO conversation (ts, question, answer) VALUES (?, ?, ?)",
                    (_now(), question[:MAX_TEXT], answer[:MAX_TEXT]))
        self._write("DELETE FROM conversation WHERE id NOT IN "
                    "(SELECT id FROM conversation ORDER BY id DESC LIMIT ?)", (KEEP_EXCHANGES,))

    def recent_exchanges(self, limit: int = 6) -> list[Exchange]:
        rows = self._read("SELECT ts, question, answer FROM conversation "
                          "ORDER BY id DESC LIMIT ?", (limit,))
        return [Exchange(*row) for row in reversed(rows)]

    def clear_conversation(self) -> None:
        self._write("DELETE FROM conversation")

    # -------------------------------------------------------------- lab journal

    def record_lab_run(self, key: str, title: str, sessions: int, synthetic: bool,
                       passed: bool, verdict: str, evidence: str) -> None:
        self._write(
            "INSERT INTO lab_runs (ts, key, title, sessions, synthetic, passed, verdict, "
            "evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), key, title, sessions, int(synthetic), int(passed), verdict, evidence))

    def lab_runs(self, key: Optional[str] = None, limit: int = 100) -> list[LabRun]:
        rows = self._read(
            "SELECT ts, key, title, sessions, synthetic, passed, verdict, evidence FROM lab_runs"
            + (" WHERE key = ?" if key else "") + " ORDER BY id DESC LIMIT ?",
            ((key, limit) if key else (limit,)))
        return [LabRun(r[0], r[1], r[2], r[3], bool(r[4]), bool(r[5]), r[6], r[7]) for r in rows]

    # -------------------------------------------------------------------- picks

    def record_picks(self, picks: Iterable[Pick]) -> int:
        """Record picks; the same source, stock and side is kept once a day."""
        added = 0
        with closing(self._connect()) as conn, conn:
            for p in picks:
                if p.price <= 0:
                    continue
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO picks (day, source, symbol, side, price, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (p.day.isoformat(), p.source, _symbol(p.symbol), p.side, float(p.price),
                     p.reason[:300]))
                added += cursor.rowcount
        return added

    def picks(self, source: Optional[str] = None) -> list[Pick]:
        rows = self._read(
            "SELECT day, source, symbol, side, price, reason FROM picks"
            + (" WHERE source = ?" if source else "") + " ORDER BY day DESC, id",
            (source,) if source else ())
        return [Pick(date.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]

    # ------------------------------------------------------------ for the model

    def context(self, question: str = "", include_conversation: bool = False) -> str:
        """What the chat model is given: notes, recent runs and, on request, talk."""
        mentioned = mentioned_symbols(question, {n.symbol for n in self.notes() if n.symbol})
        notes = self.notes()
        # Preferences first, then notes about stocks the question names, then the rest.
        notes.sort(key=lambda n: (n.kind != "preference", n.symbol not in mentioned))
        lines: list[str] = []
        used = 0
        for n in notes[:CONTEXT_NOTES]:
            line = f"- [{n.kind}{' ' + n.symbol if n.symbol else ''}] {n.text}"
            if used + len(line) > CONTEXT_CHARS:
                break
            lines.append(line)
            used += len(line)
        parts = ["<operator_memory>",
                 "The operator's own notes, kept at their request. Their words, not "
                 "verified facts.", *(lines or ["(none yet)"]), "</operator_memory>"]
        runs = self.lab_runs(limit=5)
        if runs:
            parts += ["", "<lab_journal>", *(
                f"- {r.ts[:10]} {r.title}: {r.verdict} ({r.evidence})"
                + (" [synthetic data]" if r.synthetic else "") for r in runs), "</lab_journal>"]
        if include_conversation:
            talk = self.recent_exchanges(4)
            if talk:
                parts += ["", "<earlier_conversation>", *(
                    f"{e.ts[:16]} operator: {e.question[:300]}\n  you: {e.answer[:400]}"
                    for e in talk), "</earlier_conversation>"]
        return "\n".join(parts)


#: Upper-case words that are not EGX tickers.
_NOT_TICKERS = {"EGX", "EGP", "USD", "RSI", "HALT", "ARM", "START", "BUY", "SELL", "THNDR",
                "API", "ETF", "SMA", "EMA", "AGM", "IPO", "CBE", "IMF", "GDP", "CPI"}


def mentioned_symbols(text: str, known: Iterable[str] = ()) -> set[str]:
    """EGX tickers named in text: "COMI", "comi.ca", or any spelling of a known one."""
    known = set(known)
    found = set()
    for token in re.findall(r"\b[A-Za-z]{3,5}(?:\.[Cc][Aa])?\b", text or ""):
        upper = token.upper()
        symbol = upper if upper.endswith(".CA") else upper + ".CA"
        if symbol in known or upper.endswith(".CA") or (
                token.isupper() and upper not in _NOT_TICKERS):
            found.add(symbol)
    return found


# --------------------------------------------------------------------------- #
# "Remember that ..." in the chat
# --------------------------------------------------------------------------- #

_REMEMBER = re.compile(
    r"^\s*(?:من فضلك\s+)?(?:افتكر|إفتكر|احفظ|سجّل|سجل|خليك فاكر|remember|note)"
    r"(?:\s+(?:إن|ان|أن|انه|إنه|that))?\s*[:،,]?\s*(?P<text>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_PREFERENCE_WORDS = re.compile(r"(بحب|بفضل|أفضل|افضل|مش بحب|ماحبش|i prefer|i like|i don't|"
                               r"i do not|my risk|مخاطر|أولوية|اولوية)", re.IGNORECASE)


def remember_request(question: str) -> Optional[tuple[str, str, str]]:
    """(kind, symbol, text) when the operator asks the app to remember something."""
    match = _REMEMBER.match(question or "")
    if not match:
        return None
    text = match.group("text").strip()
    if len(text) < 3:
        return None
    symbols = sorted(mentioned_symbols(text))
    symbol = symbols[0] if len(symbols) == 1 else ""
    kind = "preference" if _PREFERENCE_WORDS.search(text) and not symbol else "note"
    return kind, symbol, text


def lab_key(rules: Sequence[Any], title: str, years: int, synthetic: bool) -> str:
    """Identifies "the same test" across runs: rule kinds and parameters, span, data."""
    body = [[r.kind, sorted((k, float(v)) for k, v in dict(r.params).items())] for r in rules]
    return json.dumps({"rules": body, "title": title, "years": years, "synthetic": synthetic},
                      sort_keys=True)
