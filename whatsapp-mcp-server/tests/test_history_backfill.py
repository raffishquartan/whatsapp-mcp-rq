"""Tests for history_backfill.py against temporary databases and a fake bridge."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

import history_backfill as hb
from test_migrate_timestamps_to_utc import SCHEMA

SINCE = "2026-11-01"

# (id, chat_jid, timestamp)
BASE_MESSAGES: list[tuple[str, str, str]] = [
    ("g1-old", "group1@g.us", "2026-10-20 10:00:00+00:00"),
    ("g1-a", "group1@g.us", "2026-11-02 10:00:00+00:00"),
    ("g1-b", "group1@g.us", "2026-11-05 10:00:00+00:00"),
    ("g2-a", "group2@g.us", "2026-11-10 09:00:00+00:00"),
    ("d1-a", "111@lid", "2026-11-12 08:00:00+00:00"),
    ("d2-a", "222@s.whatsapp.net", "2026-11-03 08:00:00+00:00"),
    ("old-only", "old@g.us", "2026-10-01 08:00:00+00:00"),
    ("status-1", "status@broadcast", "2026-11-04 08:00:00+00:00"),
]
BASE_CHATS: list[tuple[str, str]] = [
    ("group1@g.us", "Chat A"),
    ("group2@g.us", "Chat B"),
    ("111@lid", "Chat C"),
    ("222@s.whatsapp.net", "Chat D"),
    ("old@g.us", "Chat E"),
    ("status@broadcast", "status"),
]


def insert_messages(db: Path, messages: Iterable[tuple[str, str, str]]) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, 'x', 'Coffee 4.50', ?, 0)",
            messages,
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "messages.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO chats VALUES (?, ?, NULL)", BASE_CHATS)
        conn.commit()
    finally:
        conn.close()
    insert_messages(path, BASE_MESSAGES)
    return path


def make_config(db: Path, tmp_path: Path, *extra: str) -> hb.Config:
    return hb.parse_args(["--since", SINCE, "--db", str(db), "--out", str(tmp_path / "out"), *extra])


Response = tuple[int, dict[str, object]]


def ok(received: int, oldest: str | None, next_id: str | None, new: int = 0) -> Response:
    return 200, {
        "success": True,
        "request_id": "REQ",
        "received": received,
        "stored_new": new,
        "stored_existing": received - new,
        "skipped": 0,
        "oldest_timestamp": oldest,
        "next_before_message_id": next_id,
    }


def err(status: int, message: str = "boom") -> Response:
    return status, {"success": False, "message": message}


class FakeBridge:
    """Scripted bridge: per chat, a queue of responses; records every request body."""

    def __init__(self, script: dict[str, list[Response]], default: Response | None = None) -> None:
        self.script = {jid: list(responses) for jid, responses in script.items()}
        self.default = default if default is not None else ok(0, None, None)
        self.calls: list[dict[str, object]] = []

    def __call__(self, body: dict[str, object]) -> Response:
        self.calls.append(body)
        jid = str(body["chat_jid"])
        queue = self.script.get(jid)
        return queue.pop(0) if queue else self.default

    def chats_called(self) -> list[str]:
        seen: list[str] = []
        for call in self.calls:
            jid = str(call["chat_jid"])
            if jid not in seen:
                seen.append(jid)
        return seen


class Clock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def run(config: hb.Config, bridge: FakeBridge, clock: Clock | None = None) -> hb.RunOutcome:
    return hb.run_backfill(config, bridge, (clock or Clock()).sleep, lambda: 2.5)


# --- 5.2 argument parsing and config ---


def test_parse_args_defaults(tmp_path: Path) -> None:
    config = hb.parse_args(["--since", SINCE, "--out", str(tmp_path)])
    assert config.since == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert config.delay == 10.0
    assert config.budget == 150
    assert config.max_pages_per_chat == 10
    assert config.progress == tmp_path / "progress.json"
    assert config.only == ()
    assert config.canary is None
    assert config.dry_run is False
    assert config.bridge_url == "http://localhost:8080/api"


@pytest.mark.parametrize(
    "argv",
    [
        ["--out", "x"],  # --since missing
        ["--since", SINCE],  # --out missing
        ["--since", "01/11/2026", "--out", "x"],
        ["--since", "2026-11-01T10:00", "--out", "x"],
        ["--since", SINCE, "--out", "x", "--budget", "0"],
        ["--since", SINCE, "--out", "x", "--max-pages-per-chat", "0"],
        ["--since", SINCE, "--out", "x", "--delay", "-1"],
        ["--since", SINCE, "--out", "x", "--only", "status@broadcast"],
        ["--since", SINCE, "--out", "x", "--canary", "123@broadcast"],
        ["--since", SINCE, "--out", "x", "--only", "not-a-jid"],
    ],
)
def test_parse_args_rejections(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        hb.parse_args(argv)
    assert exc.value.code == 2


def test_run_rejects_when_no_chats_selected(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path, "--only", "nobody@g.us")
    with pytest.raises(hb.BackfillError, match="no chats"):
        run(config, FakeBridge({}))


def test_main_exits_2_when_no_chats_selected(db: Path, tmp_path: Path) -> None:
    code = hb.main(["--since", "2027-01-01", "--db", str(db), "--out", str(tmp_path / "out"), "--dry-run"])
    assert code == 2


# --- 5.3 read-only chat selection and snapshot ---


def test_select_chats_window_order_and_counts(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path)
    chats = hb.select_chats(config)
    assert [c.jid for c in chats] == ["group2@g.us", "group1@g.us", "111@lid", "222@s.whatsapp.net"]
    group1 = chats[1]
    assert group1.name == "Chat A"
    assert set(group1.messages) == {"g1-a", "g1-b"}
    assert group1.newest_id == "g1-b"


def test_select_chats_canary_first_and_only(db: Path, tmp_path: Path) -> None:
    chats = hb.select_chats(make_config(db, tmp_path, "--canary", "222@s.whatsapp.net"))
    assert [c.jid for c in chats][:2] == ["222@s.whatsapp.net", "group2@g.us"]

    only = hb.select_chats(make_config(db, tmp_path, "--only", "111@lid", "--only", "group1@g.us"))
    assert [c.jid for c in only] == ["group1@g.us", "111@lid"]


def test_open_readonly_rejects_writes(db: Path) -> None:
    conn = hb.open_readonly(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == len(BASE_MESSAGES)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO chats VALUES ('x@g.us', 'x', NULL)")
    finally:
        conn.close()


def test_open_readonly_does_not_create_missing_db(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        hb.open_readonly(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


# --- 5.4 paging loop, pacing, budget, retries, abort, progress ---


def stop_reasons(outcome: hb.RunOutcome) -> dict[str, hb.StopReason]:
    return {r.jid: r.stop for r in outcome.results}


def test_paging_follows_next_id_until_since(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path, "--only", "group1@g.us")
    bridge = FakeBridge({
        "group1@g.us": [
            ok(50, "2026-11-01T12:00:00Z", "p1"),
            ok(50, "2026-10-25T12:00:00Z", "p2"),
        ]
    })
    clock = Clock()
    outcome = run(config, bridge, clock)
    assert bridge.calls == [
        {"chat_jid": "group1@g.us", "count": 50, "before_message_id": "g1-b"},
        {"chat_jid": "group1@g.us", "count": 50, "before_message_id": "p1"},
    ]
    result = outcome.results[0]
    assert result.stop is hb.StopReason.REACHED_SINCE
    assert result.pages == 2
    assert clock.sleeps == [12.5]  # one gap: delay 10 + jitter 2.5, none before the first request
    assert outcome.exit_code == 0


@pytest.mark.parametrize(
    ("response", "reason", "exit_code"),
    [
        (ok(0, None, None), hb.StopReason.START_OF_CHAT, 0),
        (ok(3, "2026-11-02T12:00:00Z", None), hb.StopReason.START_OF_CHAT, 0),
        (err(404, "message g1-b is not stored"), hb.StopReason.NO_ANCHOR, 1),
    ],
)
def test_single_page_stop_reasons(
    db: Path, tmp_path: Path, response: Response, reason: hb.StopReason, exit_code: int
) -> None:
    outcome = run(make_config(db, tmp_path, "--only", "group1@g.us"), FakeBridge({"group1@g.us": [response]}))
    assert outcome.results[0].stop is reason
    assert outcome.exit_code == exit_code


def test_page_cap(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path, "--only", "group1@g.us", "--max-pages-per-chat", "2")
    bridge = FakeBridge({}, default=ok(50, "2026-11-03T00:00:00Z", "again"))
    outcome = run(config, bridge)
    assert len(bridge.calls) == 2
    assert outcome.results[0].stop is hb.StopReason.PAGE_CAP
    assert outcome.exit_code == 1


def test_budget_stops_before_exceeding(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path, "--budget", "3")
    bridge = FakeBridge({}, default=ok(50, "2026-11-03T00:00:00Z", "again"))
    outcome = run(config, bridge)
    assert len(bridge.calls) == 3
    assert outcome.budget_exhausted
    assert outcome.results[0].stop is hb.StopReason.BUDGET
    assert [r.jid for r in outcome.results] == ["group2@g.us"]
    assert outcome.exit_code == 1


def test_three_consecutive_failures_abort_and_report(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path)
    bridge = FakeBridge({}, default=err(503, "bridge is not connected"))
    outcome = run(config, bridge)
    assert len(bridge.calls) == 3
    assert outcome.aborted
    assert outcome.exit_code == 1
    assert [r.stop for r in outcome.results] == [hb.StopReason.FAILED] * 3
    report = (tmp_path / "out" / "report.md").read_text()
    assert "Aborted after 3 consecutive failures" in report
    assert "bridge is not connected" in report


def test_404_does_not_count_towards_abort(db: Path, tmp_path: Path) -> None:
    bridge = FakeBridge({
        "group2@g.us": [err(504)],
        "group1@g.us": [err(504)],
        "111@lid": [err(404)],
        "222@s.whatsapp.net": [err(504)],
    })
    outcome = run(make_config(db, tmp_path), bridge)
    assert outcome.aborted
    assert stop_reasons(outcome)["111@lid"] is hb.StopReason.NO_ANCHOR


def test_success_resets_failure_streak(db: Path, tmp_path: Path) -> None:
    bridge = FakeBridge({
        "group2@g.us": [err(504)],
        "group1@g.us": [err(502)],
        "111@lid": [ok(0, None, None)],
        "222@s.whatsapp.net": [err(504)],
    })
    outcome = run(make_config(db, tmp_path), bridge)
    assert not outcome.aborted
    assert len(outcome.results) == 4


def test_504_moves_to_next_chat(db: Path, tmp_path: Path) -> None:
    bridge = FakeBridge({"group2@g.us": [err(504, "no answer")]})
    outcome = run(make_config(db, tmp_path), bridge)
    assert bridge.chats_called() == ["group2@g.us", "group1@g.us", "111@lid", "222@s.whatsapp.net"]
    first = outcome.results[0]
    assert first.stop is hb.StopReason.FAILED
    assert first.errors == ("HTTP 504: no answer",)
    assert outcome.exit_code == 1


def test_409_waits_and_retries_once(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path, "--only", "group1@g.us")
    bridge = FakeBridge({"group1@g.us": [err(409), ok(0, None, None)]})
    clock = Clock()
    outcome = run(config, bridge, clock)
    assert len(bridge.calls) == 2
    assert outcome.results[0].stop is hb.StopReason.START_OF_CHAT
    assert clock.sleeps == [12.5]


def test_409_twice_is_a_failure(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path, "--only", "group1@g.us")
    bridge = FakeBridge({"group1@g.us": [err(409), err(409, "busy")]})
    outcome = run(config, bridge)
    assert len(bridge.calls) == 2
    assert outcome.results[0].stop is hb.StopReason.FAILED
    assert outcome.results[0].errors == ("HTTP 409: busy",)


def test_transport_error_is_a_failure(db: Path, tmp_path: Path) -> None:
    def broken(body: dict[str, object]) -> Response:
        raise hb.BridgeUnavailable("connection refused")

    outcome = hb.run_backfill(make_config(db, tmp_path, "--only", "group1@g.us"), broken, Clock().sleep, lambda: 0.0)
    assert outcome.results[0].stop is hb.StopReason.FAILED
    assert outcome.results[0].errors == ("connection refused",)


def test_malformed_200_is_a_failure(db: Path, tmp_path: Path) -> None:
    bridge = FakeBridge({"group1@g.us": [(200, {"success": True})]})
    outcome = run(make_config(db, tmp_path, "--only", "group1@g.us"), bridge)
    assert outcome.results[0].stop is hb.StopReason.FAILED
    assert "malformed" in outcome.results[0].errors[0]


def test_resume_skips_completed_chats(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path, "--budget", "2")
    first = run(config, FakeBridge({}, default=ok(0, None, None)))
    assert first.budget_exhausted
    assert [r.jid for r in first.results] == ["group2@g.us", "group1@g.us"]

    bridge = FakeBridge({}, default=ok(0, None, None))
    second = run(replace(config, budget=150), bridge)
    assert bridge.chats_called() == ["111@lid", "222@s.whatsapp.net"]
    assert [r.jid for r in second.results] == ["group2@g.us", "group1@g.us", "111@lid", "222@s.whatsapp.net"]
    assert second.exit_code == 0


def test_resume_retries_failed_chats(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path, "--only", "group1@g.us")
    run(config, FakeBridge({"group1@g.us": [err(504)]}))
    bridge = FakeBridge({}, default=ok(0, None, None))
    outcome = run(config, bridge)
    assert bridge.chats_called() == ["group1@g.us"]
    assert outcome.results[0].stop is hb.StopReason.START_OF_CHAT


def test_progress_for_other_since_is_rejected(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path, "--only", "group1@g.us")
    run(config, FakeBridge({}))
    with pytest.raises(hb.BackfillError, match="since"):
        run(replace(config, since=datetime(2026, 10, 1, tzinfo=timezone.utc)), FakeBridge({}))


def test_dry_run_sends_nothing(db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = hb.main(["--since", SINCE, "--db", str(db), "--out", str(tmp_path / "out"), "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "group1@g.us" in out and "Chat A" in out and "2 messages" in out
    assert "status@broadcast" not in out
    assert not (tmp_path / "out").exists()


# --- 5.5 reports ---


EXPECTED_MD = """# WhatsApp history backfill report

- Since: 2026-11-01 (UTC)
- Chats processed: 2 of 2
- Chats with additions: 1
- New messages in window: 2
- Requests sent: 3
- Chats not reaching since: none

| Chat | JID | Before | After | New | Stored new (all pages) | Pages | Stop reason | Newest stored | Errors |
|---|---|---|---|---|---|---|---|---|---|
| Chat B | group2@g.us | 1 | 1 | 0 | 0 | 1 | start of chat | 2026-11-10T09:00:00+00:00 |  |
| Chat A | group1@g.us | 2 | 4 | 2 | 3 | 2 | reached since | 2026-11-05T10:00:00+00:00 |  |

## New messages

### Chat A (group1@g.us)

- new-1 2026-11-01T09:00:00+00:00
- new-2 2026-11-03T09:00:00+00:00
"""

EXPECTED_CSV = [
    ["name", "jid", "before", "after", "new_in_window", "stored_new", "pages", "stop_reason", "newest_stored", "errors", "new_messages"],
    ["Chat B", "group2@g.us", "1", "1", "0", "0", "1", "start of chat", "2026-11-10T09:00:00+00:00", "", ""],
    [
        "Chat A", "group1@g.us", "2", "4", "2", "3", "2", "reached since", "2026-11-05T10:00:00+00:00", "",
        "new-1 2026-11-01T09:00:00+00:00;new-2 2026-11-03T09:00:00+00:00",
    ],
]


def test_report_outputs(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path, "--only", "group1@g.us", "--only", "group2@g.us")

    def bridge(body: dict[str, object]) -> Response:
        # Simulate the bridge storing what the phone returned before it answers.
        calls.append(body)
        if body["chat_jid"] == "group1@g.us" and body["before_message_id"] == "g1-b":
            insert_messages(db, [("new-2", "group1@g.us", "2026-11-03 09:00:00+00:00"),
                                 ("new-1", "group1@g.us", "2026-11-01 09:00:00+00:00")])
            return ok(20, "2026-11-01T09:00:00Z", "new-1", new=2)
        if body["chat_jid"] == "group1@g.us":
            insert_messages(db, [("older", "group1@g.us", "2026-10-15 09:00:00+00:00")])
            return ok(20, "2026-10-15T09:00:00Z", "older", new=1)
        return ok(0, None, None)

    calls: list[dict[str, object]] = []
    outcome = hb.run_backfill(config, bridge, Clock().sleep, lambda: 0.0)
    assert outcome.exit_code == 0
    out = tmp_path / "out"
    assert (out / "report.md").read_text() == EXPECTED_MD
    with (out / "report.csv").open(newline="") as fh:
        assert list(csv.reader(fh)) == EXPECTED_CSV
    progress = json.loads((out / "progress.json").read_text())
    assert set(progress["chats"]) == {"group1@g.us", "group2@g.us"}


def test_report_lists_chats_not_reaching_since(db: Path, tmp_path: Path) -> None:
    config = make_config(db, tmp_path, "--only", "group1@g.us", "--max-pages-per-chat", "1")
    run(config, FakeBridge({}, default=ok(50, "2026-11-03T00:00:00Z", "again")))
    report = (tmp_path / "out" / "report.md").read_text()
    assert "- Chats not reaching since: Chat A (group1@g.us): page cap" in report


# --- real HTTP poster ---


class FakeHttpResponse:
    def __init__(self, status_code: int, payload: object, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_http_poster_posts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_post(url: str, json: dict[str, object], timeout: float) -> FakeHttpResponse:
        seen.update(url=url, json=json, timeout=timeout)
        return FakeHttpResponse(200, {"received": 0})

    monkeypatch.setattr(requests, "post", fake_post)
    status, body = hb.http_poster("http://127.0.0.1:9/api/")({"chat_jid": "chat@g.us"})
    assert (status, body) == (200, {"received": 0})
    assert seen == {"url": "http://127.0.0.1:9/api/request_history_sync", "json": {"chat_jid": "chat@g.us"},
                    "timeout": hb.HTTP_TIMEOUT_SECONDS}


def test_http_poster_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, json: dict[str, object], timeout: float) -> FakeHttpResponse:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(hb.BridgeUnavailable, match="refused"):
        hb.http_poster("http://127.0.0.1:9/api")({})


def test_http_poster_non_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, json: dict[str, object], timeout: float) -> FakeHttpResponse:
        return FakeHttpResponse(404, ValueError("not json"), text="404 page not found\n")

    monkeypatch.setattr(requests, "post", fake_post)
    assert hb.http_poster("http://127.0.0.1:9/api")({}) == (404, {"message": "404 page not found"})
