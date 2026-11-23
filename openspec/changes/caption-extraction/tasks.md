## 1. Tests first (whatsapp-bridge/main_test.go, package main)

- [x] 1.1 Table test for `extractTextContent`: plain `Conversation`, `ExtendedTextMessage`, image caption, video caption, document caption, image/video/document with empty caption (expect ""), nil message (expect ""), and a message with text and a caption set (text wins)
- [x] 1.2 Table test for `unwrapMessage`: ephemeral, view-once, edited and document-with-caption wrappers return the inner message; a plain message and nil are returned unchanged
- [x] 1.3 Store round-trip tests: open a `MessageStore` on a temp-dir SQLite file (`t.TempDir()`), run the history pipeline (`unwrapMessage`, `extractTextContent`, `extractMediaInfo`, `StoreMessage`) for a captioned image, a wrapped captioned document, a captioned video, an uncaptioned image and a plain text message, read each row back, assert `content`, `media_type`, `filename`, `url`
- [x] 1.4 Run `go test ./...` in `whatsapp-bridge` and confirm the new cases fail for the right reason before implementing

## 2. Implementation (whatsapp-bridge/main.go)

- [x] 2.1 Extend `extractTextContent` with the image, video and document caption cases in the order given in design.md Decision 1; remove the stale "ignoring non-text messages" comment
- [x] 2.2 Add `unwrapMessage` (design.md Decision 2)
- [x] 2.3 In `handleHistorySync`, unwrap the message once and pass it to `extractTextContent` and `extractMediaInfo`; drop the inline text extraction and the redundant nil guard
- [x] 2.4 Confirm `StoreMessage` and the skip conditions in both handlers need no change (media rows with an empty caption are still stored)

## 3. History-sync tests

- [x] 3.1 The history conversion is covered by the pipeline tests in 1.3 (the handler body is only glue around the pure functions)
- [x] 3.2 Test that storing a message twice with the same `(id, chat_jid)`, first with empty content then with a caption, leaves one row with the caption (`INSERT OR REPLACE` back-fill behaviour)

## 4. Verification

- [x] 4.1 `go vet ./...` and `go test ./...` in `whatsapp-bridge` exit 0
- [ ] 4.2 Rebuild and restart the bridge with the `install-whatsapp-bridge.sh` script in the claude-code-config-sync repository; note the new commit hash
- [ ] 4.3 Send an image with a caption to a test chat; confirm with a read-only sqlite3 query that the row has the caption and `media_type` = "image", and that the MCP `list_messages` tool returns it
- [ ] 4.4 Repeat 4.3 for a captioned document and a captioned video
- [x] 4.5 DROPPED on 2026-11-23: no report-back to the claude-code-config-sync project is wanted

## 5. Specs

- [ ] 5.1 Run `openspec-sync-specs` / `openspec-archive-change` so `message-captions` lands in `openspec/specs/` before shipping
