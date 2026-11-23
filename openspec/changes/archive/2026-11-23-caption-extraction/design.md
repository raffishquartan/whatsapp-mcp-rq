## Context

Verified against `whatsapp-bridge/main.go` at commit 289ccdc and the pinned whatsmeow (v0.0.0-20260929112325-8b41cfe6d9c4):

- `extractTextContent` returned only `GetConversation()` or `GetExtendedTextMessage().GetText()`.
- Live path: `handleMessage` calls `extractTextContent` and `extractMediaInfo` on `evt.Message`. whatsmeow dispatches live events after `evt.UnwrapRaw()` (message.go), which removes the bot-invoke, ephemeral, view-once (v1, v2, v2 extension), lottie-sticker, document-with-caption and edited wrappers. The live path therefore never sees a wrapped message.
- History-sync path: `handleHistorySync` re-implemented text extraction inline (text-only) and passed the raw `msg.Message.Message` to `extractMediaInfo`. Raw messages keep their wrappers, so wrapped messages (disappearing-message chats, view-once, edits, captioned documents) were skipped entirely by the "no content and no media" check.
- `StoreMessage` already stores `media_type`, `filename`, `url` and the key/hash columns next to `content`, using `INSERT OR REPLACE` keyed on `(id, chat_jid)`. Both callers skip a message only when `content == "" && mediaType == ""`, so no skip logic changes.
- The Python server (`whatsapp-mcp-server/whatsapp.py`) selects `messages.content` in every listing query and filters with `LOWER(content) LIKE ...`; it needs no change.
- There are no Go tests in the repo today. `package main` contains `main()`, which does not prevent `go test` for pure functions.
- Getters `GetCaption()` on Image/Video/Document messages are nil-safe.

Data (read-only query of the deployed `store/messages.db`, 2026-11-23): 11356 messages; 1554 have a non-empty `media_type` and empty `content` (image 1354, video 156, document 22, audio 22); 0 media rows have non-empty content. Audio has no caption field, so at most 1532 rows could ever have had a caption.

## Goals / Non-Goals

**Goals:**
- Caption text is stored in `content` for image, video and document messages on both write paths, with media metadata unchanged.
- The same message produces the same row whether it arrives live or via history sync.
- Unit tests for each branch.

**Non-Goals:**
- Backfilling existing rows (Decision 3).
- Fixing the `time.Now()`-based synthetic filenames in `extractMediaInfo`, the history-sync trigger, or timestamp storage. These belong to the history-sync-fix and utc-timestamps changes.
- Any change to the Python MCP server or the DB schema.

## Decisions

1. **Extend `extractTextContent` rather than add a second function.** Order: `GetConversation()`, `GetExtendedTextMessage().GetText()`, `GetImageMessage().GetCaption()`, `GetVideoMessage().GetCaption()`, `GetDocumentMessage().GetCaption()`. Return the first non-empty value. Nil-safe getters mean no explicit nil checks. The function receives an already-unwrapped message, so it does not look inside wrappers.

2. **History sync unwraps exactly like the live path.** A small pure function `unwrapMessage(raw)` builds an `events.Message{RawMessage: raw}`, calls `UnwrapRaw()` and returns `evt.Message`. `handleHistorySync` passes its result to both `extractTextContent` and `extractMediaInfo`. This reuses whatsmeow's own unwrapping, so ephemeral, view-once, edited and document-with-caption messages are handled the same on both paths and no wrapper-specific getters are needed in `extractTextContent` or `extractMediaInfo`.
   - Alternative rejected: `client.ParseWebMessage`. It also unwraps, but it can fail (missing sender, not logged in) for reasons unrelated to content, and needs a live client in tests.
   - Alternative rejected: wrapper-specific getter chains in the two helpers. They would never run on the live path and would drift from whatsmeow's wrapper list.

3. **No backfill in this change (recommended).** The DB holds no caption for old rows, so they can only be recovered from WhatsApp. History sync is not a reliable source: the bridge only receives history on pairing or when the server pushes it, the explicit history-request endpoint is handled by the separate history-sync-fix change, and WhatsApp may return only a bounded window. After this change ships, any history-sync event that does arrive repairs rows for free, because `StoreMessage` uses `INSERT OR REPLACE` on `(id, chat_jid)` and the history path now computes captions. A test covers re-delivery.
   - Risk of re-delivery overwriting: a history record carries the same message id, so replacing is the intended outcome.

## Risks / Trade-offs

- [Content now holds caption text for media rows; any consumer that assumed media rows have empty content breaks] -> The only consumer is `whatsapp-mcp-server/whatsapp.py`, which prints `content` after a `[media_type - Message ID ...]` prefix; verified safe. Tasks include a manual check.
- [Merge conflicts with sibling changes editing `main.go`] -> This change edits `extractTextContent`, adds `unwrapMessage` and edits the content/media lines of `handleHistorySync`. history-sync-fix likely edits the same handler, so that region is the conflict hotspot; stack the changes in order.
- [History sync now stores messages it previously skipped (wrapped messages)] -> Intended: those rows exist on the live path already.

## Migration Plan

1. Merge, rebuild and restart the bridge with `install-whatsapp-bridge.sh`. No DDL, no data migration, no new files in the store directory.
2. Send an image with a caption to a test chat; confirm the row in `messages.db` and via the MCP `list_messages` tool.
3. Rollback: redeploy the previous commit. Rows written meanwhile keep their captions harmlessly.
