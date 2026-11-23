## Why

The bridge stores image, video and document messages with an empty `content` column, so any caption the sender typed is lost. In expense-tracking chats the caption is the description and amount ("Coffee 4.50"), so the database is useless for reconciliation and callers fall back to scraping WhatsApp Web by hand.

## What Changes

- Extend the shared text extraction so it returns the user-visible text of a message: plain text and extended text as today, then the caption of an image, video or document message.
- Make both write paths use it: the live handler `handleMessage` and the history-sync handler `handleHistorySync` (which today duplicates a text-only extraction inline instead of calling `extractTextContent`).
- Make the history-sync handler unwrap messages the way whatsmeow does for live events (`events.Message.UnwrapRaw`: ephemeral, view-once, edited and document-with-caption wrappers), so the same message produces the same row on both paths. Today history messages are passed raw, so wrapped messages are skipped.
- Add the first Go unit tests in the repo (`whatsapp-bridge/main_test.go`) covering every caption type, no-caption media, plain text, nil message and the unwrap step, plus store round-trip tests.
- Existing rows are not backfilled by this change (see design.md, Decision 4). No schema change and no data migration.

## Capabilities

### New Capabilities
- `message-captions`: media captions are stored as message text in `messages.content` alongside the media metadata, on both the live and history-sync write paths, and are returned by the MCP server's existing list/search tools.

### Modified Capabilities
<!-- None: openspec/specs/ is empty. -->

## Impact

- Code: `whatsapp-bridge/main.go` (`extractTextContent`, new `unwrapMessage`, `handleHistorySync`), new `whatsapp-bridge/main_test.go`.
- Database: `messages.content` now non-empty for captioned media rows written after deployment. No DDL change.
- MCP server (`whatsapp-mcp-server/whatsapp.py`): no code change. It already selects `content` and searches it with `LIKE`, so captions become searchable and appear in listings automatically.
- Deployment: bridge rebuild and restart via the `install-whatsapp-bridge.sh` script in the claude-code-config-sync repository. No DB migration.
- No CHANGELOG.md or version file exists in the repo, so no changelog or semver task.
