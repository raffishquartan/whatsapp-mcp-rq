## Why

`whatsapp-bridge/store/messages.db` stores message and chat timestamps with whatever UTC offset the bridge host had when each row was written, so the same column mixes `+00:00`, `+01:00` and `+02:00`. The instants are correct, but every consumer that reads wall-clock digits, compares the strings in SQL, or joins on string equality gets wrong answers (a 00:28 BST message appears as `01:28+02:00`). The mismatch recurs whenever the host zone differs from Europe/London.

## What Changes

- Bridge: every timestamp written to `messages.timestamp` and `chats.last_message_time` is normalised to UTC at the two write choke points (`StoreMessage`, `StoreChat`), so all four call sites (live message handler, history-sync chat and message loops) store `+00:00`.
- One-off, re-runnable, verifiable migration script that rewrites existing rows to the equivalent UTC instant (row counts, per-row instants, min and max instants proven unchanged) with an automatic backup and a rollback on any verification failure.
- MCP server: timestamps returned to callers are converted to Europe/London (correct BST/GMT offset, shown in the output); the `after` and `before` filters interpret naive input as Europe/London local time and compare in UTC; SQL comparisons and the `last_message_time = timestamp` join use the single UTC representation.
- Tests: Go tests for UTC storage; Python tests for BST and GMT round trips, a legacy `+02:00` row, naive filter input, DST edges; migration script tests including every error path.
- **BREAKING (output only)**: the timestamp text in `list_messages`, `list_chats` and related tool output changes from raw stored digits to Europe/London time with an explicit offset.

## Capabilities

### New Capabilities
- `message-timestamps`: how the bridge stores message and chat timestamps (UTC only) and how the MCP server presents and filters them (Europe/London).

### Modified Capabilities
<!-- None: openspec/specs/ is empty. -->

## Impact

- Code: `whatsapp-bridge/main.go` (`StoreChat`, `StoreMessage`; read-side `GetMessages`/`GetChats` unchanged), `whatsapp-mcp-server/whatsapp.py` (`format_message`, `list_messages`, `get_message_context`, `list_chats`, `get_chat`, `get_direct_chat_by_contact`, `get_contact_chats`, `get_last_interaction`), new `scripts/migrate_timestamps_to_utc.py`, new tests.
- Data: tables `messages` (column `timestamp`) and `chats` (column `last_message_time`) in `whatsapp-bridge/store/messages.db`; `whatsapp.db` (whatsmeow session store) is not touched.
- Deployment: bridge rebuild and restart plus one DB migration with the bridge stopped; MCP server picks up Python changes on its next start.
- Dependencies: none new in Go; Python `zoneinfo` is stdlib (3.11+), `tzdata` added as a dependency so Europe/London resolves on hosts without system zoneinfo.
