## Why

The local `messages.db` silently misses every message that arrived while the bridge was down, and there is no working way to backfill it. The expense-reconciliation reconciliation skill already tells sessions to call `/api/request_history_sync`, but that route does not exist in this fork, and the only code behind it (`requestHistorySync`, `whatsapp-bridge/main.go:1194-1227`, never called) panics deterministically. the owner wants the backfill fixed rather than removed, and wants to be confident that no chat, group or contact active since 2026-11-01 is missing messages. Source request: ccmsg 20261122T233222Z-ae36.

Verified against the pinned whatsmeow snapshot (`v0.0.0-20260929112325-8b41cfe6d9c4`, `send.go:579-597`): `BuildHistorySyncRequest(lastKnownMessageInfo, count)` reads `lastKnownMessageInfo.Chat`, `.ID`, `.IsFromMe` and `.Timestamp` with no nil check, so passing `nil` always panics. It is not a readiness problem. The request must be sent with `SendPeerMessage` (`send.go:482`) to the user's own primary device, not with `SendMessage` to `status@s.whatsapp.net`. The phone answers asynchronously with an `events.HistorySync` of sync type `ON_DEMAND` holding up to `count` messages immediately before the anchor message.

Two claims in the source request do not match the code: there is no `/api/request_history_sync` route and no `recover()` wrapper in this repo (dev clone or deployed copy). Both belong to an earlier install. This change therefore adds the endpoint rather than repairing it.

## What Changes

- Add `POST /api/request_history_sync` to the bridge. It takes a required `chat_jid`, an optional `count` (default 50) and an optional `before_message_id` anchor (default: the oldest stored message in that chat). It builds the request from a stored message, sends it as a peer message, waits a bounded time for the matching `ON_DEMAND` history sync, and returns how many messages arrived, how many were new to `messages.db`, and the anchor for the next older page.
- Clear errors instead of panics: bad input (400), no stored message or unknown anchor (404), bridge not connected or not logged in (503), another request already in flight (409), send failure (502), no answer from the phone in time (504).
- Fix the history-sync write path so an `ON_DEMAND` chunk of old messages cannot move `chats.last_message_time` backwards (today `StoreChat` does `INSERT OR REPLACE` with the chunk's newest timestamp).
- Report new versus already-stored messages from the history-sync handler, so callers can prove what was added.
- Delete the dead `requestHistorySync` function.
- Add a backfill command (`whatsapp-history-backfill`, Python, in `whatsapp-mcp-server/`) that lists every chat with messages since a given date from `messages.db` (read-only), pages backwards through each one via the endpoint with rate limiting, a request budget, an error-streak abort and resumable progress, and writes a before/after report of what was added. the owner's acceptance run uses `--since 2026-11-01`.
- Add the first Go tests for the endpoint and store changes, and the first Python tests for the backfill command.
- Bump `whatsapp-mcp-server` version 0.1.0 -> 0.2.0 (new command). No CHANGELOG exists, so none is created.

## Capabilities

### New Capabilities
- `history-sync`: on-demand backfill of a chat's older messages from the primary phone through the bridge REST API, including how results are stored and reported, and the bulk backfill procedure across all recently active chats.

### Modified Capabilities
<!-- None: openspec/specs/ is empty. -->

## Impact

- Code: `whatsapp-bridge/main.go` (`startRESTServer`, `handleHistorySync`, `MessageStore.StoreChat`, `MessageStore.StoreMessage`, new history-request helpers; `requestHistorySync` deleted), new `whatsapp-bridge/history_sync_test.go`; new `whatsapp-mcp-server/history_backfill.py` and tests; `whatsapp-mcp-server/pyproject.toml`.
- Database: no DDL change and no migration. Writes to `messages` (all columns, keyed on `id, chat_jid`) and `chats.last_message_time` / `chats.name`.
- Depends on the sibling changes `caption-extraction` and `utc-timestamps` (both edit the history-sync write path; the anchor timestamp must be correct UTC). Implement and deploy this one last.
- Deployment: bridge rebuild and restart via `<claude-code-config-sync>/scripts/install-whatsapp-bridge.sh`. No DB migration.
- External follow-up (not in this repo): the expense-reconciliation skill's Step 1a in `claude-code-config-sync` must switch from `curl GET` to the new POST endpoint or the backfill command.
- Ban risk: each request is a peer message to the owner's own phone, the same kind of message WhatsApp Web sends when scrolling up in a chat. The backfill procedure caps volume and pacing (see design.md).
