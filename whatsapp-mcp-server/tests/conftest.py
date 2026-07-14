import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import whatsapp  # noqa: E402

SCHEMA = """
CREATE TABLE chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TIMESTAMP
);

CREATE TABLE messages (
    id TEXT,
    chat_jid TEXT,
    sender TEXT,
    content TEXT,
    timestamp TIMESTAMP,
    is_from_me BOOLEAN,
    media_type TEXT,
    filename TEXT,
    url TEXT,
    media_key BLOB,
    file_sha256 BLOB,
    file_enc_sha256 BLOB,
    file_length INTEGER,
    PRIMARY KEY (id, chat_jid),
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
"""

# Fictional fixture data - no real contacts, phone numbers, or message content.
CHATS = [
    ("111111111@g.us", "Test Group", "2026-01-01T10:00:00+00:00"),
    ("222222222@s.whatsapp.net", "Alice Example", None),
    ("333333333@s.whatsapp.net", "Bob Example", "2026-01-02T09:00:00+00:00"),
]

MESSAGES = [
    ("msg1", "111111111@g.us", "444444444", "Hello group", "2026-01-01T10:00:00+00:00", 0, ""),
    ("msg2", "333333333@s.whatsapp.net", "333333333", "Hi Bob", "2026-01-02T09:00:00+00:00", 1, ""),
]


@pytest.fixture
def fixture_db(tmp_path, monkeypatch):
    db_path = tmp_path / "messages.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)", CHATS)
    conn.executemany(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        MESSAGES,
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", str(db_path))
    return db_path
