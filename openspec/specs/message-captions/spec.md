# message-captions Specification

## Purpose
Store the caption of image, video and document messages as the message's text in the bridge database, identically for live messages and history sync, so captions are listed and searchable through the MCP server.

## Requirements

### Requirement: Media captions are stored as message text
When the bridge stores an image, video or document message that carries a caption, it SHALL store the caption in the message's `content` field, and SHALL continue to store the media type, filename, URL and download metadata unchanged.

#### Scenario: Captioned image received live
- **WHEN** an image message with caption "Coffee 4.50" arrives
- **THEN** the stored row has `content` = "Coffee 4.50" and `media_type` = "image" with its URL and media key

#### Scenario: Captioned video received live
- **WHEN** a video message with a caption arrives
- **THEN** the stored row has `content` equal to the caption and `media_type` = "video"

#### Scenario: Captioned document received live
- **WHEN** a document message with a caption arrives, whether sent directly or wrapped as a document-with-caption message
- **THEN** the stored row has `content` equal to the caption, `media_type` = "document", the document's filename, and the media key needed to download it

#### Scenario: Media without a caption
- **WHEN** an image, video or document message with no caption arrives
- **THEN** the row is stored with empty `content` and its media metadata, as before

#### Scenario: Plain text unchanged
- **WHEN** a plain or extended text message arrives
- **THEN** `content` holds the text and `media_type` is empty, as before

### Requirement: Live and history-sync paths extract content identically
Messages delivered through history sync SHALL be unwrapped and have their `content` and media metadata derived by the same rules as live messages, so the same message produces the same row regardless of how it arrived.

#### Scenario: Captioned image delivered by history sync
- **WHEN** a history-sync conversation contains an image message with a caption
- **THEN** the stored row has `content` equal to the caption

#### Scenario: Wrapped message delivered by history sync
- **WHEN** a history-sync conversation contains a captioned image inside an ephemeral or view-once wrapper, or a document inside a document-with-caption wrapper
- **THEN** the stored row has the same `content` and `media_type` as the unwrapped message would have

#### Scenario: History sync re-delivers an existing caption-less row
- **WHEN** history sync delivers a message whose id and chat already exist in the database with empty `content`
- **THEN** the row is replaced with the caption now present in `content`

### Requirement: Captions are visible through the MCP server
Stored captions SHALL be returned by the MCP server's message listing tools in the message text and SHALL be matched by its text search.

#### Scenario: Caption appears in listings
- **WHEN** `list_messages` returns a captioned image row
- **THEN** the output shows the media prefix followed by the caption text

#### Scenario: Caption is searchable
- **WHEN** `list_messages` is called with a query that matches only a caption
- **THEN** the captioned row is returned
