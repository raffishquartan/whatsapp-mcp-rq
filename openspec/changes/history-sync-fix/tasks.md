## 0. Preconditions

- [x] 0.1 Confirm `caption-extraction` and `utc-timestamps` are merged to `main` (and the UTC migration has run on the deployed `messages.db`), then branch from `main`; verify with `git log main --oneline` showing both merges

## 1. Spike (time-boxed: 2 hours, 3 attempts over at least 2 days - design.md Decision 9)

- [ ] 1.1 On a throwaway branch, add a minimal unauthenticated loopback route that builds `types.MessageInfo` from the oldest stored message of the expenses test group (`<test-group-jid>`), calls `client.SendPeerMessage(ctx, client.BuildHistorySyncRequest(&info, 50))`, logs the `SendResponse.ID`, and in `handleHistorySync` logs `Data.GetSyncType()`, `Notification.GetOriginalMessageID()`, `Notification.GetPeerDataRequestSessionID()` and conversation IDs; verify by building with `go build ./...`
- [ ] 1.2 Run the spike build locally against the real account (phone online), call the route for the expenses test group, one `@lid` direct chat and one other group; record in the session WORKLOG which correlation field is filled, whether LID chats answer, and whether any message dated 2026-04-28 to 2026-05-03 arrived
- [ ] 1.3 Apply the Decision 9 outcome: proceed, ship as best effort, or stop and ask the owner before deleting; record the outcome in the WORKLOG; discard the spike branch

## 2. Store layer (Go)

- [x] 2.0 Put this change's Go tests in `whatsapp-bridge/history_sync_test.go`, reusing the temp-dir/chdir `NewMessageStore` test helper introduced by `caption-extraction`/`utc-timestamps` (do not add a second helper); verify `go test ./...` runs both files
- [x] 2.1 Keeping the `.UTC()` normalisation `utc-timestamps` adds, change `MessageStore.StoreChat` to an upsert that never lowers `last_message_time` (`MAX(existing, new)`); verify with a Go test that storing an older timestamp keeps the newer one and that a new chat is inserted
- [x] 2.2 Change `MessageStore.StoreMessage` to report whether the row already existed (existence check and `INSERT OR REPLACE` in one transaction), update its callers in `handleMessage` and `handleHistorySync`; verify with Go tests for new row, existing row rewritten, and the empty-content-and-no-media no-op
- [x] 2.3 Add `MessageStore` lookups for the anchor: oldest message in a chat, newest message in a chat, and a message by (`id`, `chat_jid`), each returning `id`, `is_from_me`, `timestamp`, and a distinct not-found error; verify with Go tests covering found and not-found for each

## 3. History-sync handling (Go)

- [x] 3.1 Make `handleHistorySync` return per-conversation results (received, stored_new, stored_existing, skipped, oldest timestamp, oldest stored id) while keeping its current storage and logging behaviour; verify with a Go test feeding a constructed `events.HistorySync` with text, media, empty and nil messages
- [x] 3.2 Add the pending-request registry (single in-flight entry, completion by original message id then chat JID, unsolicited syncs ignored); call it from `handleHistorySync` for `ON_DEMAND` syncs only; verify with Go tests for id match, chat fallback match, non-matching sync, and non-`ON_DEMAND` sync not completing the request
- [ ] 3.3 If the spike showed LID chats need the phone-number JID, map via `client.Store.LIDs.GetPNForLID` before building the request and add a test with a fake LID store; otherwise mark this task not needed with the spike evidence

## 4. Endpoint (Go)

- [x] 4.1 Extract a named handler constructor for `POST /api/request_history_sync` that depends on the sender interface, the store, the registry and an injectable timeout (design.md Decision 5), register it in `startRESTServer`, and delete `requestHistorySync`; verify `go build ./...` and `go vet ./...` pass
- [x] 4.2 Implement validation and anchoring per specs/history-sync/spec.md; verify with `httptest` tests for: non-POST 405, malformed JSON 400, missing/empty/unparseable `chat_jid` 400, broadcast JID 400, `count` 0 and 101 400, no stored messages 404, unknown `before_message_id` 404 - each asserting nothing was sent
- [x] 4.3 Implement state and send errors; verify with tests for: not connected 503, not logged in 503, second request while one is pending 409, `SendPeerMessage` error 502, timeout 504 (short injected timeout) with request id in the body, and a late answer after 504 still stored
- [x] 4.4 Implement the success response; verify with tests that check the request built from the anchor (chat, id, from_me, Unix-seconds timestamp, count), the response fields `request_id`, `received`, `stored_new`, `stored_existing`, `skipped`, `oldest_timestamp` (UTC RFC 3339), `next_before_message_id`, the start-of-chat case (`received: 0`, null next id), and that repeating the same page reports all rows as existing
- [x] 4.5 Run the full Go check suite (`gofmt -l .` empty, `go vet ./...`, `go test ./...`) in `whatsapp-bridge/` and verify every command exits 0

## 5. Backfill command (Python)

- [x] 5.1 Reuse the pytest dev group added by `utc-timestamps` (add it only if absent), add mypy to it, add a `[build-system]` and `[project.scripts] whatsapp-history-backfill = "history_backfill:main"` to `whatsapp-mcp-server/pyproject.toml`, run `uv lock`; verify `uv run whatsapp-history-backfill --help` prints usage and `uv run main.py` still starts the MCP server
- [x] 5.2 Implement argument parsing and config (`--since` required ISO date, `--db`, `--bridge-url`, `--out` required, `--only`, `--canary`, `--delay`, `--budget`, `--max-pages-per-chat`, `--progress`, `--dry-run`) with validation that rejects a run that would do nothing (no chats selected, budget < 1); verify with tests for each rejection
- [x] 5.3 Implement read-only chat selection and snapshot (non-broadcast chats with a message at or after `--since` UTC, ordering canary, groups, direct chats by recency); verify with tests on a temporary SQLite fixture, including that the connection is opened `mode=ro` and that writing through it fails
- [x] 5.4 Implement the paging loop, pacing, budget, 409 retry, 504 handling, 3-failure abort and progress file; verify with tests using a fake HTTP client and fake sleep covering every stop reason (reached since, start of chat, page cap, budget, abort) and resume skipping completed chats
- [x] 5.5 Implement `report.md` and `report.csv` (no message content; per-chat before/after counts, new ids and timestamps, pages, stop reason, errors; summary of chats not reaching `--since`); verify with a snapshot-style test of both outputs
- [x] 5.6 Run `uv run pytest` and `uv run mypy --strict history_backfill.py` in `whatsapp-mcp-server/` and verify both exit 0

## 6. Documentation

- [x] 6.1 Add a README section for the endpoint (request, responses, error codes, that it pages backwards only, phone must be online) and the backfill procedure from design.md Decision 6 including the ban-risk checklist and Decision 9 fallback; verify the section renders and every documented flag matches `--help`

## 7. Version

- [x] 7.1 Bump `whatsapp-mcp-server/pyproject.toml` version 0.1.0 -> 0.2.0 (minor: new command; no CHANGELOG exists so none is created); verify with `grep version whatsapp-mcp-server/pyproject.toml`

## 8. Deployment and acceptance (after merge)

- [ ] 8.1 Run `<claude-code-config-sync>/scripts/install-whatsapp-bridge.sh`; verify `INSTALLED_COMMIT_HASH` matches the merge commit and a POST with an empty body returns 400
- [ ] 8.2 Back up `messages.db`, then run the canary `whatsapp-history-backfill --only <test-group-jid> --since 2026-04-01 --out <session>/working/backfill-canary`; verify the report shows new messages dated 2026-04-28 to 2026-05-03, or record evidence that they cannot be retrieved
- [ ] 8.3 Run the expense-reconciliation Step 0 ban-report web search, then `whatsapp-history-backfill --since 2026-11-01 --canary <test-group-jid> --out <session>/working/backfill-2026-11-01`; verify the report lists all 51 non-broadcast chats, each with stop reason "reached since" or "start of chat", and share the summary with the owner
- [ ] 8.4 Send a ccmsg to `claude-code-config-sync` with the outcome and the new invocation for expense-reconciliation Step 1a (POST endpoint or backfill command); verify with `ccmsg list` showing it sent
