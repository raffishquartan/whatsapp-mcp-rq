## Context

See proposal.md - Why for the motivation and the verified root cause. Facts that shape the design, all checked on 2026-11-23:

- whatsmeow `v0.0.0-20260929112325-8b41cfe6d9c4` (module cache, read-only):
  - `send.go:579` `BuildHistorySyncRequest(lastKnownMessageInfo *types.MessageInfo, count int)` dereferences the pointer unconditionally. Doc comment: send with `SendPeerMessage`; the response "will contain `count` messages immediately before the given message"; recommended `count` is 50; the timestamp field takes Unix seconds despite its `MS` name.
  - `send.go:482` `SendPeerMessage(ctx, msg)` sends to the own non-AD JID with `SendRequestExtra{Peer: true}`, returns `ErrNotLoggedIn` if there is no own ID, and on success returns a `SendResponse` whose `ID` is the request message id. `send.go:1092` marks on-demand requests `push_priority=high_force`.
  - Answers arrive through the normal history-sync notification loop (`message.go:891-906`, auto-downloaded because `ManualHistorySyncDownload` is false) and are dispatched as `*events.HistorySync`. `Data.GetSyncType()` is `HistorySync_ON_DEMAND` (value 6). `Notification` carries `GetOriginalMessageID()` and `GetPeerDataRequestSessionID()`, which are the candidate correlation keys.
- Bridge (`whatsapp-bridge/main.go`): no `/api/request_history_sync` route exists; `requestHistorySync` (1194-1227) is never called. `handleHistorySync` (1052-1191) stores every conversation in any history sync. It calls `StoreChat` (103-109, `INSERT OR REPLACE`) with `messages[0]`'s timestamp, which for an on-demand chunk of old messages would move `last_message_time` backwards. `StoreMessage` (112-126) is `INSERT OR REPLACE` on PK (`id`, `chat_jid`), so it is already idempotent but cannot tell new rows from existing ones. There are no Go tests and no Python tests in the repo.
- `messages.db` (deployed copy, read-only query): a few tens of chats have a message in the acceptance window, most of them `@lid` direct chats, the rest groups plus `status@broadcast`; the window holds on the order of a thousand messages. The canary group's known gap lies entirely before its oldest stored message, so the default anchor must be the oldest stored message. Timestamps are text with mixed offsets (`+00:00`, `+01:00`, `+02:00`), which `utc-timestamps` normalises.

## Goals / Non-Goals

**Goals:**
- A synchronous, single-chat endpoint whose response says exactly what was added, so a simple client can page through history.
- A bulk backfill that gives the owner evidence, per chat, that the window since a chosen date is complete.
- Every error path returns a structured error with a test.

**Non-Goals:**
- Recovering messages newer than the newest stored message in a chat (the on-demand protocol only pages backwards; such messages arrive through the normal offline queue on reconnect).
- An MCP tool for history requests (see Decision 7).
- Backfilling `status@broadcast`.
- Updating the downstream workflow that calls the endpoint (lives outside this repo; handed off after deployment).

## Decisions

### 1. Synchronous endpoint with a single in-flight request
The handler sends the peer message, then blocks on a channel until the matching `ON_DEMAND` sync is processed or a timeout (60 seconds, constant) expires. A package-level `historyRequests` registry (mutex plus at most one pending entry) enforces one request at a time and returns 409 otherwise.
- Why: callers (curl, the backfill command, the downstream workflow) get a definitive result in one call; one-at-a-time is also the pacing the ban-risk analysis wants.
- Alternatives: fire-and-forget returning 202 and a status endpoint (more surface, and the client must poll); allowing parallel requests (no benefit, more ban risk, harder correlation).

### 2. Correlation by original message id, falling back to chat JID
When an `ON_DEMAND` sync arrives, the handler matches it to the pending request if `Notification.GetOriginalMessageID()` equals the `SendResponse.ID` of the request. If that field is empty, it matches when the sync contains a conversation whose ID equals the pending `chat_jid`. Which key the phone actually fills is UNVERIFIED and is the first thing the spike (tasks 1.x) logs. Non-matching or unsolicited `ON_DEMAND` syncs are stored as today and do not complete the pending request.
- Alternative: chat-only matching. Rejected as the primary key because a late answer to a timed-out request for the same chat would be mistaken for the current one.
- The request id is known only after `SendPeerMessage` returns. An answer that carries an original message id and arrives before then is held on the pending entry, keyed by that id; recording the request id completes the request if one of the held answers matches. Chat-only matching is used only when the answer carries no original message id, so a late answer to an earlier request never completes the current one.
- An answer that matches by request id but holds no conversation for the pending chat (for example a LID chat answered under its phone-number JID) completes the request with a flag, and the handler responds 502 with `reason: "chat_not_in_answer"` and the conversation JIDs the answer held. Treating it as an empty page would wrongly report the start of the chat.

### 3. Anchor from a stored message
`types.MessageInfo` is built from the stored row: `Chat` = parsed `chat_jid`, `ID` = `id`, `IsFromMe` = `is_from_me`, `Timestamp` = parsed `timestamp`. Default anchor is the oldest stored message by timestamp (this is what the canary group gap test needs). `before_message_id` lets a client anchor anywhere. `Sender` is not needed by `BuildHistorySyncRequest`.
- The response's `next_before_message_id` is the oldest message from the answer that was actually stored (messages with no text and no media are skipped by the handler, so the absolute oldest may not be in the DB). Pages may therefore overlap by a few messages, which is harmless because storage is idempotent. `next_before_message_id` is therefore null both at the start of the chat (`received: 0`) and when an answer held messages but stored none (`received` > 0); only the first means the start of the chat.
- Alternative: accept a raw anchor triple (id, from_me, timestamp) in the request. Rejected: lets a client forge anchors and duplicates data the DB already holds.

### 4. Store-layer changes
- `StoreChat` keeps the name update but uses an upsert that sets `last_message_time = MAX(existing, new)` so old chunks never regress it (applies to all history syncs, not only on-demand).
- `StoreMessage` returns whether the row already existed (checked with `SELECT EXISTS` in the same transaction before the `INSERT OR REPLACE`), so `handleHistorySync` can count `stored_new` versus `stored_existing`. Existing rows are rewritten, not skipped: with `caption-extraction` and `utc-timestamps` deployed first, a rewrite repairs captions and timestamps in the backfilled range.
- `handleHistorySync` returns a per-conversation result struct (received, new, existing, skipped, oldest timestamp, oldest stored id) for every conversation in the sync, including conversations with no messages, that the registry hands to the waiting handler. The chat row uses the newest timestamp among the answer's messages (not `messages[0]`, which may be nil or untimestamped). Its existing log output is kept.
- History-sync messages record `sender` as the user part of the participant JID, through the same `senderID` helper the live handler uses, so a rewrite of a live row keeps its sender value.
- The SQLite DSN sets `_txlock=immediate` and `_busy_timeout=5000`: `StoreMessage`'s read-then-write transaction takes the write lock at `BEGIN`, so concurrent live and history writes wait for each other instead of failing with "database is locked" on a deferred lock upgrade.
- Alternative: `INSERT OR IGNORE` for on-demand rows. Rejected: would leave pre-fix rows without captions or with wrong offsets.

### 5. Testability seam
The handler depends on a small interface (`IsConnected() bool`, `IsLoggedIn` via store ID, `SendPeerMessage(ctx, *waE2E.Message) (whatsmeow.SendResponse, error)`) instead of `*whatsmeow.Client`, and the timeout is injectable. Tests use a fake sender and a temporary SQLite `MessageStore`, and complete or time out the pending request by calling the same completion path `handleHistorySync` uses. The REST handler is extracted from the `startRESTServer` closure into a named `http.Handler` constructor so `httptest` can drive it.

### 6. Backfill command in Python
`whatsapp-mcp-server/history_backfill.py`, exposed as `[project.scripts] whatsapp-history-backfill`. It reads `messages.db` with `sqlite3` in read-only URI mode (`mode=ro`) and calls the bridge with `requests` (already a dependency). Inputs are validated with argparse plus a frozen dataclass config. Pure logic (chat selection, stop rules, report building) is separate from I/O; sleep and HTTP are injected for tests.
- Why Python: the MCP server already reads `messages.db` and calls the bridge; Go would put a client loop inside the daemon binary.
- Alternative: a documented curl + sqlite3 shell procedure. Rejected: no tests, no resume, error-prone report.

Procedure (what the command does):
1. Snapshot: for every non-broadcast chat with a message at or after `--since` (UTC), record the count and ids of messages in the window.
2. Order: the canary chat first if `--canary <jid>` is given (the owner's run uses a known-gap group), then groups, then direct chats, each by most recent activity.
3. Per chat: first request anchored on the newest stored message (`before_message_id` = newest id), then follow `next_before_message_id` until `oldest_timestamp < since`, `received == 0` (start of chat), or the per-chat cap (default 10 pages, counted across reruns) is hit. Each page is `count=50`. Two inconclusive outcomes stop the chat without marking it complete: `received > 0` with a null `next_before_message_id` ("no stored message in answer") and a 502 `chat_not_in_answer` ("chat not in answer", also counted as a failure).
4. Pacing: one request at a time; at least `--delay` seconds (default 10) plus 0-5 seconds random jitter between requests; global `--budget` (default 150 requests); abort after 3 consecutive failures; on 409 wait and retry once; on 504 count as a failure and move to the next chat.
5. Progress: after each chat, append its result to a JSON progress file, including the anchor the chat stopped at; reruns skip completed chats (reached since, start of chat, 404) and resume every other chat from its recorded anchor with its counts carried over (idempotent because storage is).
6. Report: re-snapshot and write `report.md` and `report.csv` to `--out` (the owner's session working folder, never the repo): per chat name, JID, before/after counts in the window, new message ids with timestamps (no message content), pages, stop reason, errors. The summary line states chats processed, chats with additions, total new messages, and any chat that did not reach `--since` (for example, hit the cap).

Expected size for the acceptance window: a few tens of chats and on the order of a thousand messages, so most chats need one or two pages: about 60-100 requests, 15-30 minutes.

### 7. No MCP tool in this change
A history request is a peer protocol message with (small) account risk; exposing it as an MCP tool would let any session fire it casually. The downstream workflow can call the backfill command with `--only <jid>`. The owner may override this.

### 8. Ban-risk controls
Each request is a peer message to the owner's own phone, the same request WhatsApp Web issues when a user scrolls up past its local cache. Risk comes from volume and pattern, so: one request at a time, paced with jitter, a hard budget, abort on error streaks, a canary chat first, and the operator searches for recent ban reports before a bulk run. The phone must be online and on Wi-Fi or data; it serves the answer.

### 9. Time-boxed fallback
The spike (tasks group 1) is limited to 2 hours of implementation time and 3 attempts against the canary group spread over at least 2 days with the phone confirmed online:
- Any answer containing messages for the chat -> proceed with the full change.
- Answers arrive but are always empty or never reach the gap -> ship the endpoint as documented best effort (spec unchanged, README states the limitation), skip the bulk acceptance claim, and report to the owner's downstream workflow.
- No answer at all in all 3 attempts -> stop; delete `requestHistorySync`, do not add the endpoint, and tell the downstream workflow to fall back to a manual gap check. This needs the owner's approval before the deletion is committed.

## Risks / Trade-offs

- [Correlation field unknown] -> spike logs both notification fields first; chat-JID fallback covers an empty original id.
- [Anchor timestamp wrong because of the mixed-offset bug] -> phone may not locate the anchor. Mitigation: deploy after `utc-timestamps`; the spike also tests one anchor from a `+02:00` row if any remain.
- [`@lid` chat JIDs] -> most chats in the window are LID-addressed; the phone may expect the phone-number JID. Spike tests one LID chat; if it fails, map LID to PN through `client.Store.LIDs` before building the request (adds one task, no spec change).
- [Phone caps or ignores `count`] -> paging handles short answers; the per-chat cap and report flag chats that did not reach `--since`.
- [Late answers after a 504] -> still stored by the normal handler; the next request for that chat is not confused because correlation uses the original message id first.
- [Rewrite of existing rows] -> could overwrite a richer row with a poorer one if the phone's copy lacks media keys; low likelihood, and the report counts rewrites separately.
- [Gaps newer than the newest stored message] -> not recoverable by this protocol; report states each chat's newest stored timestamp so the owner can eyeball it against the phone.
- [Account ban] -> see Decision 8; if WhatsApp shows any warning, stop and do not rerun.

## Migration Plan

1. Merge after `caption-extraction` and `utc-timestamps` (and after the UTC migration has run).
2. Run the owner's bridge install script to pull, rebuild and restart the bridge. No DB migration.
3. Back up `messages.db` (copy while the bridge is stopped, or `sqlite3 .backup`) before the first bulk run.
4. Canary: `whatsapp-history-backfill --only <canary-group-jid> --since <date before the gap>` and check the gap's messages appear.
5. Bulk: ban-report check, then `whatsapp-history-backfill --since <window-start> --canary <canary-group-jid> --out <working-dir>/backfill`.
6. Rollback: reinstall the previous commit; restore the `messages.db` backup if a bulk run stored bad data.

## Open Questions

- Which notification field (`OriginalMessageID` or `PeerDataRequestSessionID`) the phone fills; answered by the spike, and either way handled by Decision 2.
