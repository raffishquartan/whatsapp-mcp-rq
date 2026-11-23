"""Backfill messages.db with older history from the primary phone, chat by chat.

For every non-broadcast chat with at least one stored message on or after
`--since` (UTC), the command pages backwards through the bridge's
`POST /api/request_history_sync` endpoint, starting at the chat's newest stored
message, until the answer reaches back past `--since`, the start of the chat is
reached, or the per-chat page cap is hit. It then writes `report.md` and
`report.csv` to `--out` comparing each chat's messages in the window before
and after (ids and timestamps only, never message content).

Safety (each request is a peer message to the user's own phone):
- one request at a time, at least `--delay` seconds plus 0-5 seconds of random
  jitter between requests, and a hard `--budget` on the number of requests;
- the run stops after 3 consecutive failed requests (404 is not a failure);
- `messages.db` is opened read-only; only the bridge writes to it;
- progress is saved after each chat, so a rerun skips completed chats.
"""

from __future__ import annotations

import argparse
import csv
import enum
import json
import logging
import random
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from whatsapp import DB_TIMESTAMP_FORMAT, MESSAGES_DB_PATH, WHATSAPP_API_BASE_URL, parse_db_timestamp

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
DEFAULT_DELAY_SECONDS = 10.0
JITTER_MAX_SECONDS = 5.0
DEFAULT_BUDGET = 150
DEFAULT_MAX_PAGES_PER_CHAT = 10
MAX_FAILURE_STREAK = 3
# The bridge waits up to 60 seconds for the phone; allow for that plus overhead.
HTTP_TIMEOUT_SECONDS = 90.0
JID_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+$")
BROADCAST_SUFFIX = "@broadcast"
GROUP_SUFFIX = "@g.us"


class BackfillError(Exception):
    """The run cannot start or continue (bad input, unreadable database or progress file)."""


class BridgeUnavailable(Exception):
    """The bridge could not be reached or did not answer the HTTP request."""


class MalformedResponse(Exception):
    """A 200 response from the bridge lacked a required field."""


class StopReason(enum.Enum):
    REACHED_SINCE = "reached since"
    START_OF_CHAT = "start of chat"
    PAGE_CAP = "page cap"
    NO_ANCHOR = "no anchor"
    BUDGET = "budget exhausted"
    FAILED = "failed"


# A chat with one of these stop reasons is not requested again on a rerun.
COMPLETE_REASONS = frozenset({StopReason.REACHED_SINCE, StopReason.START_OF_CHAT, StopReason.PAGE_CAP, StopReason.NO_ANCHOR})
# A chat with one of these stop reasons is known to be complete back to --since.
COVERED_REASONS = frozenset({StopReason.REACHED_SINCE, StopReason.START_OF_CHAT})


@dataclass(frozen=True, slots=True)
class Config:
    since: datetime
    db: Path
    bridge_url: str
    out: Path
    only: tuple[str, ...]
    canary: str | None
    delay: float
    budget: int
    max_pages_per_chat: int
    progress: Path
    dry_run: bool


@dataclass(frozen=True, slots=True)
class ChatSnapshot:
    """A chat's stored messages in the window: id -> stored UTC timestamp text."""

    jid: str
    name: str
    messages: Mapping[str, str]
    newest_id: str
    newest_timestamp: str


@dataclass(frozen=True, slots=True)
class ChatResult:
    jid: str
    stop: StopReason
    requests: int
    pages: int
    received: int
    stored_new: int
    stored_existing: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PageResult:
    received: int
    stored_new: int
    stored_existing: int
    oldest_timestamp: datetime | None
    next_before_message_id: str | None


@dataclass(frozen=True, slots=True)
class RunOutcome:
    results: tuple[ChatResult, ...]
    aborted: bool
    budget_exhausted: bool
    exit_code: int


@dataclass(slots=True)
class _RunState:
    requests_sent: int = 0
    failure_streak: int = 0
    aborted: bool = False
    budget_exhausted: bool = False


PostFn = Callable[[dict[str, object]], tuple[int, Mapping[str, object]]]
SleepFn = Callable[[float], None]
JitterFn = Callable[[], float]


# --- arguments ---


def _since_date(value: str) -> datetime:
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an ISO date YYYY-MM-DD, got {value!r}") from None
    if len(value) != 10:
        raise argparse.ArgumentTypeError(f"expected an ISO date YYYY-MM-DD, got {value!r}")
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _chat_jid(value: str) -> str:
    if not JID_PATTERN.match(value):
        raise argparse.ArgumentTypeError(f"not a chat JID: {value!r}")
    if value.endswith(BROADCAST_SUFFIX):
        raise argparse.ArgumentTypeError(f"broadcast chats cannot be backfilled: {value!r}")
    return value


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"must not be negative, got {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whatsapp-history-backfill",
        description="Request older history from the primary phone for every chat active since a date, "
        "and report what was added to messages.db.",
    )
    parser.add_argument("--since", required=True, type=_since_date, help="window start, ISO date (UTC), e.g. 2026-11-01")
    parser.add_argument("--out", required=True, type=Path, help="directory for report.md, report.csv and progress.json")
    parser.add_argument("--db", type=Path, default=Path(MESSAGES_DB_PATH), help="messages.db path (opened read-only)")
    parser.add_argument("--bridge-url", default=WHATSAPP_API_BASE_URL, help="bridge REST API base URL (default: %(default)s)")
    parser.add_argument("--only", action="append", type=_chat_jid, default=[], metavar="JID",
                        help="process only this chat; repeat for several")
    parser.add_argument("--canary", type=_chat_jid, metavar="JID", help="process this chat first")
    parser.add_argument("--delay", type=_non_negative_float, default=DEFAULT_DELAY_SECONDS,
                        help="minimum seconds between requests, plus 0-5 s random jitter (default: %(default)s)")
    parser.add_argument("--budget", type=_positive_int, default=DEFAULT_BUDGET,
                        help="maximum number of requests in this run (default: %(default)s)")
    parser.add_argument("--max-pages-per-chat", type=_positive_int, default=DEFAULT_MAX_PAGES_PER_CHAT,
                        help="maximum pages of %d messages per chat (default: %%(default)s)" % PAGE_SIZE)
    parser.add_argument("--progress", type=Path, help="progress file (default: OUT/progress.json)")
    parser.add_argument("--dry-run", action="store_true", help="list the chats that would be processed; send nothing")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> Config:
    args = build_parser().parse_args(argv)
    out: Path = args.out
    progress: Path | None = args.progress
    return Config(
        since=args.since,
        db=args.db,
        bridge_url=args.bridge_url,
        out=out,
        only=tuple(args.only),
        canary=args.canary,
        delay=args.delay,
        budget=args.budget,
        max_pages_per_chat=args.max_pages_per_chat,
        progress=progress if progress is not None else out / "progress.json",
        dry_run=args.dry_run,
    )


# --- database (read-only) ---


def open_readonly(db: Path) -> sqlite3.Connection:
    """Open messages.db read-only; a missing file is an error, never created."""
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)


def snapshot_window(db: Path, since: datetime, jids: Sequence[str] | None = None) -> dict[str, ChatSnapshot]:
    """Every non-broadcast chat's stored messages at or after since, keyed by JID."""
    try:
        conn = open_readonly(db)
    except sqlite3.Error as exc:
        raise BackfillError(f"cannot open {db} read-only: {exc}") from exc
    try:
        rows: list[tuple[str, str, str, str]] = conn.execute(
            """SELECT m.chat_jid, COALESCE(c.name, ''), m.id, m.timestamp
            FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid
            WHERE m.timestamp >= ? AND m.chat_jid NOT LIKE ?
            ORDER BY m.chat_jid, m.timestamp, m.id""",
            (since.strftime(DB_TIMESTAMP_FORMAT), "%" + BROADCAST_SUFFIX),
        ).fetchall()
    except sqlite3.Error as exc:
        raise BackfillError(f"cannot read {db}: {exc}") from exc
    finally:
        conn.close()

    grouped: dict[str, tuple[str, dict[str, str]]] = {}
    for jid, name, message_id, timestamp in rows:
        if jids is not None and jid not in jids:
            continue
        grouped.setdefault(jid, (name or jid, {}))[1][message_id] = timestamp
    snapshots: dict[str, ChatSnapshot] = {}
    for jid, (name, messages) in grouped.items():
        newest_id = next(reversed(messages))
        snapshots[jid] = ChatSnapshot(jid, name, messages, newest_id, messages[newest_id])
    return snapshots


def order_chats(chats: Sequence[ChatSnapshot], canary: str | None) -> list[ChatSnapshot]:
    """Canary first, then groups, then direct chats, each most recently active first."""

    def rank(chat: ChatSnapshot) -> int:
        return 0 if chat.jid == canary else 1 if chat.jid.endswith(GROUP_SUFFIX) else 2

    # Python's sort is stable, so ranking a recency-ordered list keeps recency within each rank.
    by_recency = sorted(chats, key=lambda c: c.newest_timestamp, reverse=True)
    return sorted(by_recency, key=rank)


def select_chats(config: Config) -> list[ChatSnapshot]:
    snapshots = snapshot_window(config.db, config.since, config.only or None)
    if not snapshots:
        raise BackfillError(f"no chats with stored messages since {config.since.date().isoformat()} match the selection")
    if config.canary is not None and config.canary not in snapshots:
        logger.warning("canary %s has no stored messages in the window; it is not processed", config.canary)
    return order_chats(list(snapshots.values()), config.canary)


# --- bridge ---


def http_poster(bridge_url: str) -> PostFn:
    url = bridge_url.rstrip("/") + "/request_history_sync"

    def post(body: dict[str, object]) -> tuple[int, Mapping[str, object]]:
        try:
            response = requests.post(url, json=body, timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise BridgeUnavailable(f"bridge request failed: {exc}") from exc
        try:
            payload: object = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return response.status_code, {"message": response.text.strip()[:200]}
        return response.status_code, payload

    return post


def _int_field(body: Mapping[str, object], key: str) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MalformedResponse(f"malformed response: {key} = {value!r}")
    return value


def _optional_str_field(body: Mapping[str, object], key: str) -> str | None:
    if key not in body:
        raise MalformedResponse(f"malformed response: {key} missing")
    value = body[key]
    if value is not None and not isinstance(value, str):
        raise MalformedResponse(f"malformed response: {key} = {value!r}")
    return value


def parse_page(body: Mapping[str, object]) -> PageResult:
    oldest = _optional_str_field(body, "oldest_timestamp")
    try:
        oldest_dt = datetime.fromisoformat(oldest) if oldest is not None else None
    except ValueError:
        raise MalformedResponse(f"malformed response: oldest_timestamp = {oldest!r}") from None
    return PageResult(
        received=_int_field(body, "received"),
        stored_new=_int_field(body, "stored_new"),
        stored_existing=_int_field(body, "stored_existing"),
        oldest_timestamp=oldest_dt,
        next_before_message_id=_optional_str_field(body, "next_before_message_id"),
    )


def _error_text(status: int, body: Mapping[str, object]) -> str:
    return f"HTTP {status}: {body.get('message', '')}"


# --- paging loop ---


@dataclass(slots=True)
class _Pacer:
    delay: float
    sleep: SleepFn
    jitter: JitterFn
    started: bool = False

    def wait(self) -> None:
        if self.started:
            self.sleep(self.delay + self.jitter())
        self.started = True


@dataclass(slots=True)
class _ChatProgress:
    requests: int = 0
    pages: int = 0
    received: int = 0
    stored_new: int = 0
    stored_existing: int = 0
    errors: list[str] = field(default_factory=list)

    def result(self, jid: str, stop: StopReason) -> ChatResult:
        return ChatResult(jid, stop, self.requests, self.pages, self.received, self.stored_new,
                          self.stored_existing, tuple(self.errors))


def _send(body: dict[str, object], post: PostFn, state: _RunState, pacer: _Pacer,
          progress: _ChatProgress) -> tuple[int, Mapping[str, object]]:
    pacer.wait()
    state.requests_sent += 1
    progress.requests += 1
    try:
        return post(body)
    except BridgeUnavailable as exc:
        return 0, {"message": str(exc)}


def backfill_chat(chat: ChatSnapshot, config: Config, post: PostFn, state: _RunState, pacer: _Pacer) -> ChatResult:
    progress = _ChatProgress()
    anchor = chat.newest_id

    def fail(message: str) -> ChatResult:
        progress.errors.append(message)
        state.failure_streak += 1
        if state.failure_streak >= MAX_FAILURE_STREAK:
            state.aborted = True
        return progress.result(chat.jid, StopReason.FAILED)

    while True:
        if progress.pages >= config.max_pages_per_chat:
            return progress.result(chat.jid, StopReason.PAGE_CAP)
        body: dict[str, object] = {"chat_jid": chat.jid, "count": PAGE_SIZE, "before_message_id": anchor}
        if state.requests_sent >= config.budget:
            state.budget_exhausted = True
            return progress.result(chat.jid, StopReason.BUDGET)
        status, payload = _send(body, post, state, pacer, progress)
        if status == 409:
            if state.requests_sent >= config.budget:
                state.budget_exhausted = True
                return progress.result(chat.jid, StopReason.BUDGET)
            status, payload = _send(body, post, state, pacer, progress)
        if status == 0:
            return fail(str(payload.get("message", "")))
        if status == 404:
            progress.errors.append(_error_text(status, payload))
            return progress.result(chat.jid, StopReason.NO_ANCHOR)
        if status != 200:
            return fail(_error_text(status, payload))
        try:
            page = parse_page(payload)
        except MalformedResponse as exc:
            return fail(str(exc))

        state.failure_streak = 0
        progress.pages += 1
        progress.received += page.received
        progress.stored_new += page.stored_new
        progress.stored_existing += page.stored_existing
        logger.info("%s page %d: received %d, new %d, oldest %s", chat.jid, progress.pages, page.received,
                    page.stored_new, page.oldest_timestamp)
        if page.oldest_timestamp is not None and page.oldest_timestamp < config.since:
            return progress.result(chat.jid, StopReason.REACHED_SINCE)
        if page.received == 0 or page.next_before_message_id is None:
            return progress.result(chat.jid, StopReason.START_OF_CHAT)
        anchor = page.next_before_message_id


# --- progress file ---


@dataclass(slots=True)
class _Progress:
    since: str
    before: dict[str, ChatSnapshot]
    results: dict[str, ChatResult]


def _load_progress(config: Config) -> _Progress:
    since = config.since.date().isoformat()
    if not config.progress.exists():
        return _Progress(since, {}, {})
    try:
        raw = json.loads(config.progress.read_text())
        if raw["since"] != since:
            raise BackfillError(f"progress file {config.progress} is for since {raw['since']}, not {since}; "
                                "use another --progress or --out")
        before = {jid: ChatSnapshot(jid, s["name"], s["messages"], s["newest_id"], s["newest_timestamp"])
                  for jid, s in raw["before"].items()}
        results = {jid: ChatResult(jid, StopReason(r["stop"]), r["requests"], r["pages"], r["received"],
                                   r["stored_new"], r["stored_existing"], tuple(r["errors"]))
                   for jid, r in raw["chats"].items()}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BackfillError(f"cannot read progress file {config.progress}: {exc}") from exc
    return _Progress(since, before, results)


def _save_progress(path: Path, progress: _Progress) -> None:
    data = {
        "since": progress.since,
        "before": {jid: {"name": s.name, "messages": dict(s.messages), "newest_id": s.newest_id,
                         "newest_timestamp": s.newest_timestamp} for jid, s in progress.before.items()},
        "chats": {jid: {**asdict(r), "stop": r.stop.value, "errors": list(r.errors)}
                  for jid, r in progress.results.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


# --- report ---


@dataclass(frozen=True, slots=True)
class ReportRow:
    name: str
    jid: str
    before: int
    after: int
    new_messages: tuple[tuple[str, str], ...]  # (id, ISO timestamp), oldest first
    result: ChatResult | None
    newest_stored: str


def _iso(stored: str) -> str:
    return parse_db_timestamp(stored).isoformat()


def build_report_rows(chats: Sequence[ChatSnapshot], before: Mapping[str, ChatSnapshot],
                      after: Mapping[str, ChatSnapshot], results: Mapping[str, ChatResult]) -> list[ReportRow]:
    rows = []
    for chat in chats:
        old = before[chat.jid]
        new = after.get(chat.jid, old)
        added = sorted(((ts, mid) for mid, ts in new.messages.items() if mid not in old.messages))
        rows.append(ReportRow(
            name=new.name, jid=chat.jid, before=len(old.messages), after=len(new.messages),
            new_messages=tuple((mid, _iso(ts)) for ts, mid in added),
            result=results.get(chat.jid), newest_stored=_iso(new.newest_timestamp),
        ))
    return rows


def _stop_text(row: ReportRow) -> str:
    return row.result.stop.value if row.result is not None else "not processed"


def _not_covered(rows: Sequence[ReportRow]) -> list[ReportRow]:
    return [r for r in rows if r.result is None or r.result.stop not in COVERED_REASONS]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(rows: Sequence[ReportRow], since: datetime, aborted: bool, budget_exhausted: bool,
                    budget: int) -> str:
    processed = [r for r in rows if r.result is not None]
    missing = _not_covered(rows)
    lines = [
        "# WhatsApp history backfill report",
        "",
        f"- Since: {since.date().isoformat()} (UTC)",
        f"- Chats processed: {len(processed)} of {len(rows)}",
        f"- Chats with additions: {sum(1 for r in rows if r.new_messages)}",
        f"- New messages in window: {sum(len(r.new_messages) for r in rows)}",
        f"- Requests sent: {sum(r.result.requests for r in processed if r.result is not None)}",
        "- Chats not reaching since: "
        + ("none" if not missing else "; ".join(f"{r.name} ({r.jid}): {_stop_text(r)}" for r in missing)),
    ]
    if aborted:
        lines.append(f"- Aborted after {MAX_FAILURE_STREAK} consecutive failures; remaining chats were not processed")
    if budget_exhausted:
        lines.append(f"- Stopped: request budget of {budget} exhausted; rerun with the same progress file to continue")
    lines += [
        "",
        "| Chat | JID | Before | After | New | Stored new (all pages) | Pages | Stop reason | Newest stored | Errors |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        stored_new = r.result.stored_new if r.result is not None else 0
        pages = r.result.pages if r.result is not None else 0
        errors = "; ".join(r.result.errors) if r.result is not None else ""
        lines.append(f"| {_cell(r.name)} | {r.jid} | {r.before} | {r.after} | {len(r.new_messages)} | {stored_new} "
                     f"| {pages} | {_stop_text(r)} | {r.newest_stored} | {_cell(errors)} |")
    lines += ["", "## New messages", ""]
    with_new = [r for r in rows if r.new_messages]
    if not with_new:
        lines += ["None.", ""]
    for r in with_new:
        lines += [f"### {r.name} ({r.jid})", ""]
        lines += [f"- {mid} {ts}" for mid, ts in r.new_messages]
        lines.append("")
    return "\n".join(lines)


CSV_HEADER = ("name", "jid", "before", "after", "new_in_window", "stored_new", "pages", "stop_reason",
              "newest_stored", "errors", "new_messages")


def write_csv(path: Path, rows: Sequence[ReportRow]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for r in rows:
            writer.writerow([
                r.name, r.jid, r.before, r.after, len(r.new_messages),
                r.result.stored_new if r.result is not None else 0,
                r.result.pages if r.result is not None else 0,
                _stop_text(r), r.newest_stored,
                "; ".join(r.result.errors) if r.result is not None else "",
                ";".join(f"{mid} {ts}" for mid, ts in r.new_messages),
            ])


# --- run ---


def run_backfill(config: Config, post: PostFn, sleep: SleepFn, jitter: JitterFn) -> RunOutcome:
    chats = select_chats(config)
    progress = _load_progress(config)
    for chat in chats:
        progress.before.setdefault(chat.jid, chat)
    config.out.mkdir(parents=True, exist_ok=True)

    state = _RunState()
    pacer = _Pacer(config.delay, sleep, jitter)
    for chat in chats:
        previous = progress.results.get(chat.jid)
        if previous is not None and previous.stop in COMPLETE_REASONS:
            logger.info("%s already complete (%s); skipped", chat.jid, previous.stop.value)
            continue
        if state.requests_sent >= config.budget:
            state.budget_exhausted = True
        if state.aborted or state.budget_exhausted:
            break
        progress.results[chat.jid] = backfill_chat(chat, config, post, state, pacer)
        _save_progress(config.progress, progress)

    after = snapshot_window(config.db, config.since, [c.jid for c in chats])
    rows = build_report_rows(chats, progress.before, after, progress.results)
    (config.out / "report.md").write_text(
        render_markdown(rows, config.since, state.aborted, state.budget_exhausted, config.budget))
    write_csv(config.out / "report.csv", rows)
    exit_code = 1 if state.aborted or state.budget_exhausted or _not_covered(rows) else 0
    return RunOutcome(
        results=tuple(progress.results[c.jid] for c in chats if c.jid in progress.results),
        aborted=state.aborted,
        budget_exhausted=state.budget_exhausted,
        exit_code=exit_code,
    )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = parse_args(argv)
    try:
        if config.dry_run:
            chats = select_chats(config)
            for chat in chats:
                print(f"{chat.jid}\t{chat.name}\t{len(chat.messages)} messages\tnewest {_iso(chat.newest_timestamp)}")
            print(f"{len(chats)} chats would be processed; no requests sent.")
            return 0
        outcome = run_backfill(config, http_poster(config.bridge_url), time.sleep,
                               lambda: random.uniform(0.0, JITTER_MAX_SECONDS))
    except BackfillError as exc:
        logger.error("%s", exc)
        return 2
    logger.info("report written to %s (exit %d)", config.out / "report.md", outcome.exit_code)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
