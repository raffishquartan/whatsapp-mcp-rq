"""UTC storage, Europe/London presentation and filter semantics in whatsapp.py."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import whatsapp
from test_migrate_timestamps_to_utc import SCHEMA, mig

LINE_TIME = re.compile(r"^\[([^\]]+)\]", re.MULTILINE)


def make_db(path: Path, messages: list[tuple[str, str, str, str]], chats: list[tuple[str, str, str | None]]) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO chats VALUES (?, ?, ?)", chats)
        conn.executemany(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, 'x', ?, ?, 1)",
            messages,
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def use_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def install(messages: list[tuple[str, str, str, str]], chats: list[tuple[str, str, str | None]]) -> Path:
        db = make_db(tmp_path / "messages.db", messages, chats)
        monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", str(db))
        return db

    return install


def message_at(stored: str) -> whatsapp.Message:
    return whatsapp.Message(
        timestamp=whatsapp.parse_db_timestamp(stored),
        sender="x",
        content="Coffee 4.50",
        is_from_me=True,
        chat_jid="a@s.whatsapp.net",
        id="m1",
    )


def shown_time(stored: str) -> str:
    match = LINE_TIME.match(whatsapp.format_message(message_at(stored), show_chat_info=False))
    assert match is not None
    return match.group(1)


# --- helpers -----------------------------------------------------------------


def test_parse_db_timestamp_returns_aware_utc() -> None:
    dt = whatsapp.parse_db_timestamp("2026-08-25 00:25:15+02:00")
    assert dt == datetime(2026, 8, 24, 22, 25, 15, tzinfo=timezone.utc)
    assert dt.utcoffset() is not None and dt.utcoffset().total_seconds() == 0


def test_parse_db_timestamp_rejects_naive() -> None:
    with pytest.raises(ValueError, match="no UTC offset"):
        whatsapp.parse_db_timestamp("2026-08-25 00:25:15")


def test_to_london_rejects_naive() -> None:
    with pytest.raises(ValueError, match="naive"):
        whatsapp.to_london(datetime(2026, 7, 3, 9, 0))


# --- presentation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("2026-07-03 09:49:42+00:00", "2026-07-03 10:49:42+01:00"),  # BST
        ("2026-11-23 12:24:53+00:00", "2026-11-23 12:24:53+00:00"),  # GMT
        ("2026-07-28 11:46:00+02:00", "2026-07-28 10:46:00+01:00"),  # legacy, not yet migrated
        ("2026-03-29 00:59:59+00:00", "2026-03-29 00:59:59+00:00"),  # last GMT second
        ("2026-03-29 01:00:00+00:00", "2026-03-29 02:00:00+01:00"),  # first BST instant
        ("2026-10-25 00:59:59+00:00", "2026-10-25 01:59:59+01:00"),  # last BST second
        ("2026-10-25 01:00:00+00:00", "2026-10-25 01:00:00+00:00"),  # first GMT instant
    ],
)
def test_format_message_shows_london_time_with_offset(stored: str, shown: str) -> None:
    assert shown_time(stored) == shown


def test_chat_last_message_time_is_london(use_db) -> None:
    use_db([("m1", "a@s.whatsapp.net", "Coffee 4.50", "2026-07-03 09:49:42+00:00")],
           [("a@s.whatsapp.net", "Chat A", "2026-07-03 09:49:42+00:00")])
    chat = whatsapp.get_chat("a@s.whatsapp.net")
    assert chat is not None and chat.last_message_time is not None
    assert chat.last_message_time.isoformat(sep=" ") == "2026-07-03 10:49:42+01:00"


# --- filters -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "bound"),
    [
        ("2026-07-03T00:30:00", "2026-07-02 23:30:00+00:00"),  # naive BST
        ("2026-11-23T12:00:00", "2026-11-23 12:00:00+00:00"),  # naive GMT
        ("2026-07-03T00:30:00+00:00", "2026-07-03 00:30:00+00:00"),  # explicit offset honoured
        ("2026-07-03T02:30:00+02:00", "2026-07-03 00:30:00+00:00"),
        ("2026-10-25T01:30:00", "2026-10-25 00:30:00+00:00"),  # ambiguous: first (BST) occurrence
        ("2026-03-29T01:30:00", "2026-03-29 01:30:00+00:00"),  # spring gap: shifted forward
        ("2026-07-03", "2026-07-02 23:00:00+00:00"),  # date only = London midnight
    ],
)
def test_parse_filter(value: str, bound: str) -> None:
    assert whatsapp.parse_filter("after", value) == bound


@pytest.mark.parametrize("name", ["after", "before"])
def test_invalid_filter_names_parameter_and_value(use_db, name: str) -> None:
    use_db([], [])
    with pytest.raises(ValueError, match=f"'{name}': yesterday"):
        whatsapp.list_messages(**{name: "yesterday"})


FILTER_MESSAGES = [
    ("early", "a@s.whatsapp.net", "before midnight BST", "2026-07-02 23:29:59+00:00"),
    ("late", "a@s.whatsapp.net", "after 00:30 BST", "2026-07-02 23:30:01+00:00"),
    ("noon-before", "a@s.whatsapp.net", "GMT morning", "2026-11-23 11:59:59+00:00"),
    ("noon-after", "a@s.whatsapp.net", "GMT afternoon", "2026-11-23 12:00:01+00:00"),
]
FILTER_CHATS = [("a@s.whatsapp.net", "Chat A", "2026-11-23 12:00:01+00:00")]


def contents(output: str) -> list[str]:
    return [line.split("From: Me: ", 1)[1] for line in output.splitlines() if line]


def test_naive_after_in_bst_compares_instants(use_db) -> None:
    use_db(FILTER_MESSAGES, FILTER_CHATS)
    out = whatsapp.list_messages(after="2026-07-03T00:30:00", before="2026-07-04T00:00:00", include_context=False)
    assert contents(out) == ["after 00:30 BST"]


def test_naive_before_in_gmt_compares_instants(use_db) -> None:
    use_db(FILTER_MESSAGES, FILTER_CHATS)
    out = whatsapp.list_messages(after="2026-11-23T00:00:00", before="2026-11-23T12:00:00", include_context=False)
    assert contents(out) == ["GMT morning"]


def test_explicit_offset_filter_is_an_instant(use_db) -> None:
    use_db(FILTER_MESSAGES, FILTER_CHATS)
    out = whatsapp.list_messages(after="2026-07-02T23:30:00+00:00", before="2026-07-03T00:00:00+00:00",
                                 include_context=False)
    assert contents(out) == ["after 00:30 BST"]


# --- joins and ordering after migration ---------------------------------------


MIXED = [
    ("m1", "a@s.whatsapp.net", "first", "2026-07-03 10:00:00+01:00"),  # 09:00Z
    ("m2", "a@s.whatsapp.net", "second", "2026-07-03 09:30:00+00:00"),  # 09:30Z
    ("m3", "a@s.whatsapp.net", "third", "2026-07-03 11:45:00+02:00"),  # 09:45Z
    ("m4", "a@s.whatsapp.net", "fourth", "2026-07-03 10:50:00+01:00"),  # 09:50Z
]
MIXED_CHATS = [("a@s.whatsapp.net", "Chat A", "2026-07-03 11:50:00+02:00")]  # same instant as m4


def migrate_in_place(db: Path) -> None:
    mig.migrate(db, apply=True, make_backup=False, bridge_host="127.0.0.1", bridge_port=1)


def test_include_last_message_joins_once_columns_are_utc(use_db) -> None:
    db = use_db(MIXED, MIXED_CHATS)
    before = whatsapp.list_chats()
    assert before[0].last_message is None  # same instant, different text: the join misses
    migrate_in_place(db)
    (chat,) = whatsapp.list_chats()
    assert chat.last_message == "fourth"
    direct = whatsapp.get_chat("a@s.whatsapp.net")
    assert direct is not None and direct.last_message == "fourth"


def test_order_by_is_instant_order_after_migration(use_db) -> None:
    db = use_db(MIXED, MIXED_CHATS)
    migrate_in_place(db)
    out = whatsapp.list_messages(include_context=False)
    assert contents(out) == ["fourth", "third", "second", "first"]
    assert LINE_TIME.findall(out) == [
        "2026-07-03 10:50:00+01:00",
        "2026-07-03 10:45:00+01:00",
        "2026-07-03 10:30:00+01:00",
        "2026-07-03 10:00:00+01:00",
    ]


def test_message_context_uses_instant_order(use_db) -> None:
    db = use_db(MIXED, MIXED_CHATS)
    migrate_in_place(db)
    ctx = whatsapp.get_message_context("m2", before=5, after=5)
    assert [m.content for m in ctx.before] == ["first"]
    assert [m.content for m in ctx.after] == ["third", "fourth"]
    assert ctx.message.timestamp.isoformat(sep=" ") == "2026-07-03 10:30:00+01:00"
