#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Rewrite WhatsApp bridge timestamps to UTC without changing any instant.

`messages.timestamp` and `chats.last_message_time` in the bridge's `messages.db`
hold text such as `2026-07-03 10:49:42+01:00`, written with whatever UTC offset
the bridge host had at the time. This script rewrites every value to the same
instant in UTC (`2026-07-03 09:49:42+00:00`), the form the bridge now writes.

Safety:
- Dry run by default: the migration runs against an in-memory copy and is
  rolled back, so the report shows exactly what `--apply` would do.
- `--apply` refuses to run while the bridge API port accepts connections,
  writes a consistent backup first, and runs in one `BEGIN IMMEDIATE`
  transaction that is rolled back unless every check passes.
- Every value must be `YYYY-MM-DD HH:MM:SS[.f]+HH:MM`; anything else aborts
  before a write. No zone is guessed.
- Row counts, NULL counts, min and max instants and a SHA-256 over every
  row's key and instant must be identical before and after.

Standard library only (Python 3.11+; run directly or with `uv run`); re-running on a migrated database changes nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import socket
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

VALUE_PATTERN = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(\.\d+)?[+-]\d\d:\d\d$")
UTC_SUFFIX = "+00:00"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DEFAULT_BRIDGE_HOST = "localhost"
DEFAULT_BRIDGE_PORT = 8080


@dataclass(frozen=True, slots=True)
class Column:
    """A timestamp column and the key columns that identify its rows."""

    table: str
    column: str
    keys: tuple[str, ...]


COLUMNS: tuple[Column, ...] = (
    Column("messages", "timestamp", ("id", "chat_jid")),
    Column("chats", "last_message_time", ("jid",)),
)


class MigrationError(Exception):
    """The migration cannot run or failed verification; nothing was committed."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Instant-level summary of one column, independent of how values are written."""

    rows: int
    nulls: int
    min_epoch_us: int | None
    max_epoch_us: int | None
    sha256: str
    offsets: dict[str, int]


@dataclass(frozen=True, slots=True)
class ColumnReport:
    column: Column
    before: Snapshot
    after: Snapshot
    changed_by_offset: dict[str, int]

    @property
    def changed(self) -> int:
        return sum(self.changed_by_offset.values())


@dataclass(frozen=True, slots=True)
class Report:
    applied: bool
    backup: Path | None
    columns: tuple[ColumnReport, ...]
    chats_matching_last_message_before: int
    chats_matching_last_message_after: int

    @property
    def changed(self) -> int:
        return sum(c.changed for c in self.columns)


def parse_value(value: object) -> datetime:
    """Parse a stored value strictly; raise ValueError if it lacks an explicit offset."""
    if not isinstance(value, str) or not VALUE_PATTERN.match(value):
        raise ValueError(f"not 'YYYY-MM-DD HH:MM:SS[.f]+HH:MM': {value!r}")
    return datetime.fromisoformat(value)


def epoch_us(dt: datetime) -> int:
    return (dt - EPOCH) // timedelta(microseconds=1)


def canonical_utc(dt: datetime) -> str:
    """Render the instant as go-sqlite3 does for a UTC time.Time (fraction trimmed)."""
    utc = dt.astimezone(timezone.utc)
    text = utc.strftime("%Y-%m-%d %H:%M:%S")
    if utc.microsecond:
        text += f".{utc.microsecond:06d}".rstrip("0")
    return text + UTC_SUFFIX


def bridge_running(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def read_rows(conn: sqlite3.Connection, col: Column) -> list[tuple[int, tuple[str, ...], object]]:
    keys = ", ".join(col.keys)
    cursor = conn.execute(f"SELECT rowid, {keys}, {col.column} FROM {col.table}")
    return [(row[0], tuple(str(k) for k in row[1:-1]), row[-1]) for row in cursor]


def check_parseable(conn: sqlite3.Connection) -> None:
    """Abort, listing every offending row, if any non-NULL value is not strictly parseable."""
    problems: list[str] = []
    for col in COLUMNS:
        for _rowid, key, value in read_rows(conn, col):
            if value is None:
                continue
            try:
                parse_value(value)
            except ValueError:
                key_text = ", ".join(f"{k}={v!r}" for k, v in zip(col.keys, key, strict=True))
                problems.append(f"{col.table}.{col.column} [{key_text}]: {value!r}")
    if problems:
        raise MigrationError("unparseable timestamp values, nothing changed:\n  " + "\n  ".join(problems))


def snapshot(conn: sqlite3.Connection, col: Column) -> Snapshot:
    lines: list[str] = []
    epochs: list[int] = []
    offsets: Counter[str] = Counter()
    nulls = 0
    rows = read_rows(conn, col)
    for _rowid, key, value in rows:
        if value is None:
            nulls += 1
            lines.append("\x1f".join((*key, "NULL")))
            continue
        dt = parse_value(value)
        e = epoch_us(dt)
        epochs.append(e)
        offsets[str(value)[-6:]] += 1
        lines.append("\x1f".join((*key, str(e))))
    digest = hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()
    return Snapshot(
        rows=len(rows),
        nulls=nulls,
        min_epoch_us=min(epochs, default=None),
        max_epoch_us=max(epochs, default=None),
        sha256=digest,
        offsets=dict(sorted(offsets.items())),
    )


def chats_matching_last_message(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM chats c WHERE c.last_message_time = "
        "(SELECT MAX(m.timestamp) FROM messages m WHERE m.chat_jid = c.jid)"
    ).fetchone()
    return int(row[0])


def rewrite(conn: sqlite3.Connection, col: Column) -> dict[str, int]:
    changed: Counter[str] = Counter()
    for rowid, _key, value in read_rows(conn, col):
        if value is None:
            continue
        text = str(value)
        target = canonical_utc(parse_value(text))
        if text != target:
            conn.execute(f"UPDATE {col.table} SET {col.column} = ? WHERE rowid = ?", (target, rowid))
            changed[text[-6:]] += 1
    return dict(sorted(changed.items()))


def verify(col: Column, before: Snapshot, after: Snapshot, conn: sqlite3.Connection) -> None:
    failures = [
        f"{name}: before={b!r} after={a!r}"
        for name, b, a in (
            ("rows", before.rows, after.rows),
            ("nulls", before.nulls, after.nulls),
            ("min instant", before.min_epoch_us, after.min_epoch_us),
            ("max instant", before.max_epoch_us, after.max_epoch_us),
            ("key|instant sha256", before.sha256, after.sha256),
        )
        if b != a
    ]
    for _rowid, key, value in read_rows(conn, col):
        if value is not None and value != canonical_utc(parse_value(value)):
            failures.append(f"non-canonical value remains at {key}: {value!r}")
            break
    if failures:
        raise MigrationError(f"verification failed for {col.table}.{col.column}: " + "; ".join(failures))


def run_migration(
    conn: sqlite3.Connection,
    commit: bool,
    after_update: Callable[[sqlite3.Connection], None] | None = None,
) -> tuple[tuple[ColumnReport, ...], int, int]:
    """Migrate in one transaction; commit only if `commit` and every check passes.

    `after_update` runs after the rewrite and before verification (test hook).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        check_parseable(conn)
        befores = {col: snapshot(conn, col) for col in COLUMNS}
        matching_before = chats_matching_last_message(conn)
        changes = {col: rewrite(conn, col) for col in COLUMNS}
        if after_update is not None:
            after_update(conn)
        reports: list[ColumnReport] = []
        for col in COLUMNS:
            after = snapshot(conn, col)
            verify(col, befores[col], after, conn)
            reports.append(ColumnReport(col, befores[col], after, changes[col]))
        matching_after = chats_matching_last_message(conn)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT" if commit else "ROLLBACK")
    return tuple(reports), matching_before, matching_after


def backup_path(db: Path, now: datetime) -> Path:
    return db.with_name(f"{db.name}.bak-{now:%Y%m%d-%H%M}-pre-utc")


def write_backup(conn: sqlite3.Connection, target: Path) -> None:
    if target.exists():
        raise MigrationError(f"backup target already exists: {target}")
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()


def migrate(
    db: Path,
    apply: bool,
    make_backup: bool = True,
    bridge_host: str = DEFAULT_BRIDGE_HOST,
    bridge_port: int = DEFAULT_BRIDGE_PORT,
    now: datetime | None = None,
    after_update: Callable[[sqlite3.Connection], None] | None = None,
) -> Report:
    if not db.is_file():
        raise MigrationError(f"database not found: {db}")
    if not apply:
        source = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        work = sqlite3.connect(":memory:", isolation_level=None)
        try:
            source.backup(work)
            columns, before, after = run_migration(work, commit=False, after_update=after_update)
        finally:
            source.close()
            work.close()
        return Report(False, None, columns, before, after)

    if bridge_running(bridge_host, bridge_port):
        raise MigrationError(
            f"the bridge is accepting connections on {bridge_host}:{bridge_port}; stop it before --apply"
        )
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        backup: Path | None = None
        if make_backup:
            backup = backup_path(db, now or datetime.now())
            write_backup(conn, backup)
        columns, before, after = run_migration(conn, commit=True, after_update=after_update)
    finally:
        conn.close()
    return Report(True, backup, columns, before, after)


def format_epoch(us: int | None) -> str:
    if us is None:
        return "-"
    return canonical_utc(EPOCH + timedelta(microseconds=us))


def format_report(report: Report) -> Iterable[str]:
    yield "APPLIED" if report.applied else "DRY RUN (nothing written; rerun with --apply)"
    if report.backup is not None:
        yield f"backup: {report.backup}"
    for c in report.columns:
        yield ""
        yield f"{c.column.table}.{c.column.column}"
        yield f"  rows        {c.before.rows} -> {c.after.rows}"
        yield f"  nulls       {c.before.nulls} -> {c.after.nulls}"
        yield f"  min instant {format_epoch(c.before.min_epoch_us)} -> {format_epoch(c.after.min_epoch_us)}"
        yield f"  max instant {format_epoch(c.before.max_epoch_us)} -> {format_epoch(c.after.max_epoch_us)}"
        yield f"  sha256      {c.before.sha256[:16]} -> {c.after.sha256[:16]}"
        yield f"  offsets before: {c.before.offsets}"
        yield f"  offsets after:  {c.after.offsets}"
        yield f"  rows changed: {c.changed} {c.changed_by_offset}"
    yield ""
    yield (
        "chats whose last_message_time equals their latest message timestamp: "
        f"{report.chats_matching_last_message_before} -> {report.chats_matching_last_message_after}"
    )
    yield f"total rows changed: {report.changed}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True, help="path to the bridge's messages.db")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--no-backup", action="store_true", help="skip the pre-migration backup (tests only)")
    parser.add_argument("--bridge-host", default=DEFAULT_BRIDGE_HOST, help="bridge API host to probe")
    parser.add_argument("--bridge-port", type=int, default=DEFAULT_BRIDGE_PORT, help="bridge API port to probe")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = migrate(
            args.db,
            apply=args.apply,
            make_backup=not args.no_backup,
            bridge_host=args.bridge_host,
            bridge_port=args.bridge_port,
        )
    except MigrationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for line in format_report(report):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
