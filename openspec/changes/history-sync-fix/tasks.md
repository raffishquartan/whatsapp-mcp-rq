## 0. Preconditions

- [x] 0.1 Confirm `caption-extraction` and `utc-timestamps` are merged to `main` (and the UTC migration has run on the deployed `messages.db`), then branch from `main`; verify with `git log main --oneline` showing both merges

## 1. Spike (time-boxed: 2 hours, 3 attempts over at least 2 days - design.md Decision 9)

- [ ] 1.1 On a throwaway branch, add a minimal unauthenticated loopback route that builds `types.MessageInfo` from the oldest stored message of the canary group (`<canary-group-jid>`), calls `client.SendPeerMessage(ctx, client.BuildHistorySyncRequest(&info, 50))`, logs the `SendResponse.ID`, and in `handleHistorySync` logs `Data.GetSyncType()`, `Notification.GetOriginalMessageID()`, `Notification.GetPeerDataRequestSessionID()` and conversation IDs; verify by building with `go build ./...`
- [ ] 1.2 Run the spike build locally against the real account (phone online), call the route for the canary group, one `@lid` direct chat and one other group; record in the session WORKLOG which correlation field is filled, whether LID chats answer, what a start-of-chat answer looks like (empty conversation for the chat, or no conversation at all), and whether any message from the canary chat's suspected gap arrived
- [ ] 1.3 Apply the Decision 9 outcome: proceed, ship as best effort, or stop and ask the owner before deleting; record the outcome in the WORKLOG; discard the spike branch

## 2. Store layer (Go)

- [x] 2.0 Put this change's Go tests in `whatsapp-bridge/history_sync_test.go`, reusing the temp-dir/chdir `NewMessageStore` test helper introduced by `caption-extraction`/`utc-timestamps` (do not add a second helper); verify `go test ./...` runs both files
- [x] 2.1 Keeping the `.UTC()` normalisation `utc-timestamps` adds, change `MessageStore.StoreChat` to an upsert that never lowers `last_message_time` (`MAX(existing, new)`); verify with a Go test that storing an older timestamp keeps the newer one and that a new chat is inserted
- [x] 2.2 Change `MessageStore.StoreMessage` to report whether the row already existed (existence check and `INSERT OR REPLACE` in one transaction), update its callers in `handleMessage` and `handleHistorySync`; verify with Go tests for new row, existing row rewritten, and the empty-content-and-no-media no-op
- [x] 2.3 Add `MessageStore` lookups for the anchor: oldest message in a chat, newest message in a chat, and a message by (`id`, `chat_jid`), each returning `id`, `is_from_me`, `timestamp`, and a distinct not-found error; verify with Go tests covering found and not-found for each

## 3. History-sync handling (Go)

- [x] 3.1 Make `handleHistorySync` return per-conversation results (received, stored_new, stored_existing, skipped, oldest timestamp, oldest stored id) while keeping its current storage and logging behaviour; verify with a Go test feeding a constructed `events.HistorySync` with text, media, empty and nil messages
- [x] 3.2 Add the pending-request registry (single in-flight entry, completion by original message id then chat JID, unsolicited syncs ignored); call it from `handleHistorySync` for `ON_DEMAND` syncs only; verify with Go tests for id match, chat fallback match, non-matching sync, and non-`ON_DEMAND` sync not completing the request
- [x] 3.3 Spike evidence (real account, 2026-11-23): group requests are answered within about 13 seconds; three requests for a `@lid` direct chat, with the phone locked and unlocked, were sent but never answered (504). The handler now asks the LID store (`GetPNForLID`) for the phone-number JID of a `@lid` chat and builds the request with it (device part removed), anchored on the LID chat's oldest stored message; a conversation under that JID is stored under the LID chat and answers the request; the request falls back to the LID when no phone number is known or the lookup fails; verified with Go tests for the mapped request, a second page, both fallbacks and the canonical-chat mapping
- [ ] 3.4 Real-account run of one `@lid` chat after deploying 3.3: record whether the phone answers the mapped request and under which JID; if it does not, record that and ask the owner before running the other `@lid` chats

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

- [ ] 8.1 Run the owner's bridge install script; verify `INSTALLED_COMMIT_HASH` matches the merge commit and a POST with an empty body returns 400
- [ ] 8.2 Back up `messages.db`, then run the canary `whatsapp-history-backfill --only <canary-group-jid> --since <date before the gap> --out <working-dir>/backfill-canary`; verify the report shows new messages from the suspected gap, or record evidence that they cannot be retrieved
- [ ] 8.3 Search for recent ban reports, then `whatsapp-history-backfill --since <window-start> --canary <canary-group-jid> --out <working-dir>/backfill`; verify the report lists every non-broadcast chat in the window (count from `--dry-run`), each with stop reason "reached since" or "start of chat", and share the summary with the owner
- [ ] 8.4 Hand the outcome and the new invocation (POST endpoint or backfill command) to the owner's downstream workflow; verify the handoff was sent

## 9. Review fixes (PR #12)

- [x] 9.1 Bridge: emit a result for every conversation (newest timestamp for the chat row, no `messages[0]` dependence); an id-matched answer without the pending chat responds 502 `chat_not_in_answer`; verify with Go tests for nil/untimestamped first messages, an empty conversation, and the 502
- [x] 9.2 Bridge: hold answers that carry an original message id while the request id is unknown, and complete only on a matching id; verify with Go tests for a late answer to an earlier request and an answer that arrives before the send returns
- [x] 9.3 Bridge: record history `sender` with the live handler's `senderID` helper (user part); verify with a Go test for phone-number, LID and direct-chat senders
- [x] 9.4 Bridge: `_txlock=immediate` and `_busy_timeout` in the DSN; verify with a concurrent `StoreMessage` Go test that fails without it
- [x] 9.5 Bridge tests for the anchor lookup 500 and the client-gone 504 (registry cleared)
- [x] 9.6 Backfill: new stop reasons "no stored message in answer" and "chat not in answer" (not complete, not covered); page cap and budget resume from the recorded anchor with counts carried across runs; verify with pytest for each, plus 409 with the budget exhausted and a corrupt progress file exiting 2
- [x] 9.7 Backfill `--db`: show the resolved default in `--help`, name `--db` in the missing-database error, document in README; verify with pytest
- [x] 9.8 README: `next_before_message_id` null semantics, 502 `chat_not_in_answer`, resume rules; change docs generalised to omit facts about the private message store

## 10. Second review fixes (PR #12)

- [x] 10.1 Bridge: own history messages use the key's participant when set (own LID user in LID-addressed groups), falling back to the own phone-number user; verify with a Go test
- [x] 10.2 Bridge: the 502 `chat_not_in_answer` body carries a `conversations` JSON array (empty when none); verify with Go tests for a populated and an empty list
- [x] 10.3 Bridge: delete `MessageStore.NewestMessage` (no production caller) and its test assertions
- [x] 10.4 Bridge: bound `SendPeerMessage` by a 30-second send timeout so a request takes at most 90 seconds (send plus 60-second wait); backfill HTTP timeout = that maximum + 30-second margin; verify with a Go test of a blocked send and a pytest that reads the Go constants
- [x] 10.5 Backfill: stop a chat with "no progress" (a failure; not complete, not covered) when the next anchor was already requested for it in the run; verify with pytest for the same anchor, a cycle, the rerun and the abort
- [x] 10.6 Backfill: neutralise formula-leading cells in `report.csv`; verify with pytest
- [x] 10.7 Backfill: validate every progress-file field's type and exit 2 on a bad one; verify with pytest for each corrupt shape
- [x] 10.8 Backfill: warn about and report `--only` chats with no stored message in the window; verify with pytest
- [x] 10.9 Backfill: annotate all module-level constants; verify with a pytest and `mypy --strict`
- [x] 10.10 Docs: neutral wording for the private message store in design.md; generic `--since` example in README

## 11. Owner decision: chat-not-in-answer does not abort

- [x] 11.1 Backfill: a 502 `chat_not_in_answer` is recorded per chat but neither increments nor resets the three-failure abort streak; verify with pytest (many such chats do not abort; the streak is unchanged by one)
- [x] 11.2 Backfill: ten consecutive chats ending as `chat_not_in_answer` stop the run with their own report reason (a processed answer resets the count); verify with pytest for the cap, the reset and the unchanged failure-abort report
- [x] 11.3 Backfill: the not-in-answer count is genuinely consecutive (any other chat outcome resets it), abort state has a single source of truth with a logged reason, the report wording is consistent, and the rerun behaviour is documented; verify with pytest (interleaved 404, failure, processed answer; abort on the last chat)

## 12. LID mapping (patch release 0.2.1)

- [x] 12.1 Bump `whatsapp-mcp-server` to 0.2.1 (`pyproject.toml`, `uv.lock`); verify `uv lock --check`
