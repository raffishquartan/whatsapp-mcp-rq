"""Tests for scripts/migrate_timestamps_to_utc.py against synthetic databases."""

from __future__ import annotations

import importlib.util
import socket
import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_timestamps_to_utc.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_timestamps_to_utc", SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # scripts/ has no .gitignore for __pycache__; do not leave bytecode there.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


mig = load_script()

SCHEMA = """
CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
CREATE TABLE messages (
    id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
    is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT, media_key BLOB,
    file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
    PRIMARY KEY (id, chat_jid), FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
"""

# Same instants written with the offsets seen in real data.
MIXED_MESSAGES = [
    ("m1", "a@s.whatsapp.net", "Coffee 4.50", "2026-11-23 12:24:53+00:00"),
    ("m2", "a@s.whatsapp.net", "Lunch 9.00", "2026-07-03 10:49:42+01:00"),
    ("m3", "b@g.us", "Train 21.30", "2026-08-25 00:25:15+02:00"),
    ("m4", "b@g.us", "Late reply", "2026-07-28 11:46:00+02:00"),
]
MIXED_CHATS = [
    ("a@s.whatsapp.net", "Chat A", "2026-11-23 12:24:53+00:00"),
    ("b@g.us", "Group", "2026-08-25 00:25:15+02:00"),
    ("c@s.whatsapp.net", "No messages", None),
]
EXPECTED_MESSAGES = {
    "m1": "2026-11-23 12:24:53+00:00",
    "m2": "2026-07-03 09:49:42+00:00",
    "m3": "2026-08-24 22:25:15+00:00",
    "m4": "2026-07-28 09:46:00+00:00",
}


def make_db(path: Path, messages: list[tuple[str, str, str, str]], chats: list[tuple[str, str, str | None]]) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO chats VALUES (?, ?, ?)", chats)
        conn.executemany(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, 'x', ?, ?, 0)",
            messages,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def dump(path: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT 'm', id, chat_jid, sender, content, timestamp, is_from_me FROM messages "
            "UNION ALL SELECT 'c', jid, name, NULL, NULL, last_message_time, NULL FROM chats ORDER BY 1, 2"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def mixed_db(tmp_path: Path) -> Path:
    return make_db(tmp_path / "messages.db", MIXED_MESSAGES, MIXED_CHATS)


@pytest.fixture
def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


@pytest.fixture
def listening_port() -> Iterator[int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        yield s.getsockname()[1]


def apply(db: Path, port: int, **kwargs: object) -> object:
    return mig.migrate(db, apply=True, make_backup=False, bridge_host="127.0.0.1", bridge_port=port, **kwargs)


def test_mixed_offsets_become_utc_with_instants_unchanged(mixed_db: Path, closed_port: int) -> None:
    report = apply(mixed_db, closed_port)

    conn = sqlite3.connect(mixed_db)
    stored = dict(conn.execute("SELECT id, timestamp FROM messages").fetchall())
    chats = dict(conn.execute("SELECT jid, last_message_time FROM chats").fetchall())
    conn.close()
    assert stored == EXPECTED_MESSAGES
    assert chats == {
        "a@s.whatsapp.net": "2026-11-23 12:24:53+00:00",
        "b@g.us": "2026-08-24 22:25:15+00:00",
        "c@s.whatsapp.net": None,
    }
    for col in report.columns:
        assert col.before.rows == col.after.rows
        assert col.before.min_epoch_us == col.after.min_epoch_us
        assert col.before.max_epoch_us == col.after.max_epoch_us
        assert col.before.sha256 == col.after.sha256
    messages, chat_col = report.columns
    assert messages.before.offsets == {"+00:00": 1, "+01:00": 1, "+02:00": 2}
    assert messages.changed_by_offset == {"+01:00": 1, "+02:00": 2}
    assert chat_col.changed_by_offset == {"+02:00": 1}
    assert report.chats_matching_last_message_after >= report.chats_matching_last_message_before


def test_other_columns_untouched(mixed_db: Path, closed_port: int) -> None:
    before = dump(mixed_db)
    apply(mixed_db, closed_port)
    after = dump(mixed_db)
    strip = [tuple(v for i, v in enumerate(row) if i != 5) for row in before]
    assert strip == [tuple(v for i, v in enumerate(row) if i != 5) for row in after]


def test_second_run_changes_nothing(mixed_db: Path, closed_port: int) -> None:
    apply(mixed_db, closed_port)
    first = dump(mixed_db)
    report = apply(mixed_db, closed_port)
    assert report.changed == 0
    assert dump(mixed_db) == first
    assert mig.main(["--db", str(mixed_db), "--apply", "--no-backup", "--bridge-host", "127.0.0.1",
                     "--bridge-port", str(closed_port)]) == 0


def test_null_last_message_time_preserved(mixed_db: Path, closed_port: int) -> None:
    report = apply(mixed_db, closed_port)
    conn = sqlite3.connect(mixed_db)
    (value,) = conn.execute("SELECT last_message_time FROM chats WHERE jid = 'c@s.whatsapp.net'").fetchone()
    conn.close()
    assert value is None
    assert report.columns[1].before.nulls == report.columns[1].after.nulls == 1


def test_dry_run_reports_without_writing(mixed_db: Path, listening_port: int) -> None:
    before = mixed_db.read_bytes()
    report = mig.migrate(mixed_db, apply=False, bridge_host="127.0.0.1", bridge_port=listening_port)
    assert not report.applied
    assert report.changed == 4
    assert mixed_db.read_bytes() == before


@pytest.mark.parametrize(
    "bad",
    [
        "2026-07-03 10:49:42",  # naive: no zone guessed
        "2026-07-03T10:49:42+01:00",  # T separator
        "2026-07-03 10:49:42Z",
        "not a timestamp",
    ],
)
def test_unparseable_value_aborts_with_no_change(tmp_path: Path, closed_port: int, bad: str) -> None:
    db = make_db(tmp_path / "messages.db", [*MIXED_MESSAGES, ("bad1", "b@g.us", "Odd", bad)], MIXED_CHATS)
    before = dump(db)
    with pytest.raises(mig.MigrationError) as err:
        apply(db, closed_port)
    message = str(err.value)
    assert "messages.timestamp" in message and "bad1" in message and repr(bad) in message
    assert dump(db) == before


def test_unparseable_value_exits_non_zero(tmp_path: Path, closed_port: int, capsys: pytest.CaptureFixture[str]) -> None:
    db = make_db(tmp_path / "messages.db", MIXED_MESSAGES, [("a@s.whatsapp.net", "Chat A", "yesterday")])
    code = mig.main(["--db", str(db), "--apply", "--no-backup", "--bridge-host", "127.0.0.1",
                     "--bridge-port", str(closed_port)])
    assert code == 1
    assert "chats.last_message_time" in capsys.readouterr().err


def test_verification_failure_rolls_back(mixed_db: Path, closed_port: int) -> None:
    before = dump(mixed_db)

    def corrupt(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE messages SET timestamp = '2000-01-01 00:00:00+00:00' WHERE id = 'm1'")

    with pytest.raises(mig.MigrationError, match="verification failed for messages.timestamp"):
        apply(mixed_db, closed_port, after_update=corrupt)
    assert dump(mixed_db) == before


def test_verification_failure_on_row_count_rolls_back(mixed_db: Path, closed_port: int) -> None:
    before = dump(mixed_db)

    def drop(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM messages WHERE id = 'm4'")

    with pytest.raises(mig.MigrationError, match="rows: before=4 after=3"):
        apply(mixed_db, closed_port, after_update=drop)
    assert dump(mixed_db) == before


def test_refuses_while_bridge_port_open(mixed_db: Path, listening_port: int) -> None:
    before = dump(mixed_db)
    with pytest.raises(mig.MigrationError, match="stop it before --apply"):
        apply(mixed_db, listening_port)
    assert mig.main(["--db", str(mixed_db), "--apply", "--bridge-host", "127.0.0.1",
                     "--bridge-port", str(listening_port)]) == 1
    assert dump(mixed_db) == before
    assert list(mixed_db.parent.glob("*.bak-*")) == []


def test_missing_db_refused(tmp_path: Path) -> None:
    with pytest.raises(mig.MigrationError, match="database not found"):
        mig.migrate(tmp_path / "absent.db", apply=False)


def test_backup_created_and_restorable(mixed_db: Path, closed_port: int) -> None:
    original = dump(mixed_db)
    now = datetime(2026, 11, 23, 14, 30)
    report = mig.migrate(mixed_db, apply=True, bridge_host="127.0.0.1", bridge_port=closed_port, now=now)

    assert report.backup == mixed_db.with_name("messages.db.bak-20261123-1430-pre-utc")
    assert report.backup.is_file()
    assert dump(report.backup) == original
    assert dump(mixed_db) != original

    restored = mixed_db.with_name("restored.db")
    restored.write_bytes(report.backup.read_bytes())
    assert dump(restored) == original


def test_existing_backup_not_overwritten(mixed_db: Path, closed_port: int) -> None:
    now = datetime(2026, 11, 23, 14, 30)
    target = mixed_db.with_name("messages.db.bak-20261123-1430-pre-utc")
    target.write_bytes(b"keep")
    before = dump(mixed_db)
    with pytest.raises(mig.MigrationError, match="already exists"):
        mig.migrate(mixed_db, apply=True, bridge_host="127.0.0.1", bridge_port=closed_port, now=now)
    assert target.read_bytes() == b"keep"
    assert dump(mixed_db) == before


def test_fractional_seconds_round_trip() -> None:
    dt = mig.parse_value("2026-07-03 10:49:42.250+01:00")
    assert mig.canonical_utc(dt) == "2026-07-03 09:49:42.25+00:00"
