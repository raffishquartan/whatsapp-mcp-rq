## 1. Bridge: store UTC

- [x] 1.1 Add Go tests (temp dir, chdir, `NewMessageStore`) asserting `StoreMessage` and `StoreChat` persist `+00:00` text for inputs in `Europe/London` (BST and GMT), a fixed `+02:00` zone and UTC, and that `INSERT OR REPLACE` of the same id keeps UTC; run `go test ./...` and see them fail first
- [x] 1.2 Normalise with `.UTC()` inside `StoreMessage` and `StoreChat` in `whatsapp-bridge/main.go`; verify the 1.1 tests pass and `go vet ./...` and `go build` succeed
- [x] 1.3 Add a test that a chat stored with a message's timestamp equals that message's stored text (join equality), verified by `go test`

## 2. Migration script

- [x] 2.1 Write `scripts/migrate_timestamps_to_utc.py` per design decision 5 (dry-run default, `--apply`, bridge-running refusal, backup, strict parse, snapshot and hash verification, single transaction, rollback); verify `--help` and a dry-run against a copy of a database backup reports the per-offset row counts
- [x] 2.2 Add pytest tests with synthetic DBs for: mixed offsets migrate to `+00:00` with instants, counts and min/max unchanged; second run changes zero rows; NULL `last_message_time` preserved; unparseable value aborts with no change; naive value aborts; injected verification failure rolls back; bridge-port-open refusal; backup file created and restorable; run the tests and confirm all pass
- [x] 2.3 Run `--apply` on a copy of the real backup and verify: all values `+00:00`, `SELECT count(*)` equal before and after, min and max instants equal, second run reports zero changes; record outputs in the session worklog (live DB is not touched in this task)

## 3. MCP server: UK-local presentation and filters

- [x] 3.1 Add `tzdata` to `whatsapp-mcp-server/pyproject.toml`, a pytest dev group, and helper functions (`parse_db_timestamp`, `to_london`, `parse_filter`) in `whatsapp-mcp-server/whatsapp.py`; verify import and unit tests
- [x] 3.2 Replace every `datetime.fromisoformat(<db value>)` site (`list_messages`, `get_message_context` x3, `list_chats`, `get_chat`, `get_direct_chat_by_contact`, `get_contact_chats`) with `parse_db_timestamp`; verify no remaining raw `fromisoformat` on DB values via grep
- [x] 3.3 Change `format_message` to render Europe/London with offset; add tests for a BST sample (`09:49:42+00:00` shows `10:49:42+01:00`), a GMT sample, a legacy `+02:00` row, and the BST/GMT changeover instants (2026-03-29T01:00:00Z, 2026-10-25T01:00:00Z)
- [x] 3.4 Rework the `after`/`before` filters to naive-as-London, explicit-offset-honoured, bound as UTC strings in stored format; add tests for naive BST, naive GMT, explicit offset, ambiguous autumn hour, spring gap, and invalid input error message naming the parameter
- [x] 3.5 Add a test that `include_last_message` returns the last message text for a chat once both columns are UTC (join equality), and that `ORDER BY` returns instant order for a mixed-offset fixture after migration

## 4. Verification and rollout

- [x] 4.1 Run every configured check (`go vet`, `go build`, `go test`, pytest) and confirm exit 0; confirm `git status` shows no stray files and there is no CHANGELOG or version file to update (none exists in the repo)
- [ ] 4.2 Execute the Migration Plan in design.md on the live install (bridge stop, migrate with `--apply`, run `install-whatsapp-bridge.sh`, restart MCP server) with the owner's go-ahead; verify `SELECT DISTINCT substr(timestamp,-6) FROM messages` and the same for `chats.last_message_time` return only `+00:00`, and `list_messages` for a BST date and a GMT date match WhatsApp Web
- [ ] 4.3 Archive the change with `openspec-sync-specs` / `openspec-archive-change` before shipping
