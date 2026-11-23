## Purpose

Lets the user recover messages that the bridge missed (for example while it was offline) by asking the primary phone for older history of a chat, storing what comes back, and reporting exactly what was added - for one chat or for every chat active since a given date.

## ADDED Requirements

### Requirement: On-demand history request endpoint
The bridge SHALL expose `POST /api/request_history_sync` on its loopback REST server. The JSON body SHALL contain a required `chat_jid` string, an optional integer `count` (default 50, allowed range 1-100) and an optional `before_message_id` string. The bridge SHALL send the history request to the user's own primary device as a peer message, anchored on a message already stored in `messages.db` for that chat: the message named by `before_message_id`, or the oldest stored message in the chat when it is omitted.

#### Scenario: Request anchored on the oldest stored message
- **WHEN** a client posts `{"chat_jid": "<group>@g.us"}` for a chat with stored messages and the phone answers in time
- **THEN** the bridge anchors the request on the chat's oldest stored message (by timestamp), stores the returned messages, and responds 200 with `success: true`

#### Scenario: Request anchored on a given message
- **WHEN** a client posts `chat_jid` and a `before_message_id` that is stored for that chat
- **THEN** the request asks for up to `count` messages immediately before that message

#### Scenario: Non-POST methods rejected
- **WHEN** a client calls the endpoint with any method other than POST
- **THEN** the bridge responds 405 and sends nothing to WhatsApp

### Requirement: Input validation
The endpoint SHALL reject, without sending anything to WhatsApp, any request that is malformed or cannot be anchored, with a JSON body `{"success": false, "message": "<reason>"}`.

#### Scenario: Missing or invalid chat_jid
- **WHEN** `chat_jid` is absent, empty or not a parseable JID
- **THEN** the bridge responds 400

#### Scenario: Unsupported chat
- **WHEN** `chat_jid` is `status@broadcast` or any other broadcast JID
- **THEN** the bridge responds 400 stating that broadcast chats cannot be backfilled

#### Scenario: Count out of range
- **WHEN** `count` is less than 1 or greater than 100
- **THEN** the bridge responds 400

#### Scenario: No stored messages in the chat
- **WHEN** `before_message_id` is omitted and `messages.db` holds no message for `chat_jid`
- **THEN** the bridge responds 404 stating that there is no stored message to anchor on

#### Scenario: Unknown anchor message
- **WHEN** `before_message_id` is given but no message with that id is stored for `chat_jid`
- **THEN** the bridge responds 404

### Requirement: Connection and concurrency errors
The endpoint SHALL report bridge state and send failures as structured errors and SHALL never crash the bridge.

#### Scenario: Bridge not connected or not logged in
- **WHEN** the WhatsApp client is disconnected or has no stored device identity
- **THEN** the bridge responds 503 and sends nothing

#### Scenario: Request already in flight
- **WHEN** a history request is still waiting for its answer and a second request arrives
- **THEN** the bridge responds 409 and sends nothing for the second request

#### Scenario: Send failure
- **WHEN** sending the peer message returns an error
- **THEN** the bridge responds 502 with the error text

#### Scenario: Phone does not answer in time
- **WHEN** no matching on-demand history sync arrives within the wait timeout (default 60 seconds)
- **THEN** the bridge responds 504 with the request id, and still stores the answer if it arrives later

### Requirement: Result reporting
A successful response SHALL report what the phone returned and what changed in `messages.db`, so the caller can prove what was added.

#### Scenario: Successful answer
- **WHEN** the matching on-demand history sync arrives
- **THEN** the response contains `request_id`, `received` (messages in the answer for that chat), `stored_new` (rows that did not exist before), `stored_existing` (rows that already existed and were rewritten), `skipped` (messages with no text and no media), `oldest_timestamp` (UTC, RFC 3339) of the answer, and `next_before_message_id` (the oldest stored message from the answer, or null when the answer stored nothing)

#### Scenario: Start of chat reached
- **WHEN** the phone returns no messages older than the anchor
- **THEN** the response has `received: 0` and `next_before_message_id: null`

### Requirement: History sync storage is idempotent and does not regress chat metadata
Storing messages from any history sync SHALL be idempotent on (`id`, `chat_jid`), and SHALL NOT move a chat's `last_message_time` earlier than its current value.

#### Scenario: Re-requesting the same page
- **WHEN** the same page of history is requested twice
- **THEN** the second answer reports every message as `stored_existing`, and the row count for the chat is unchanged

#### Scenario: Old chunk for an active chat
- **WHEN** an on-demand answer contains only messages older than the chat's stored `last_message_time`
- **THEN** `chats.last_message_time` keeps its previous value

### Requirement: Bulk backfill procedure
The project SHALL provide a backfill command that requests history for every chat with at least one stored message on or after a given `--since` date (UTC), excluding broadcast chats, reading `messages.db` read-only for its chat list and its before/after counts. For each chat it SHALL page backwards from the newest stored message until the answer's oldest message is older than `--since`, the start of the chat is reached, or a per-chat page cap is hit.

#### Scenario: Full run
- **WHEN** the owner runs the command with `--since 2026-11-01` and every request succeeds
- **THEN** every non-broadcast chat with messages since 2026-11-01 is processed, and a report lists, per chat, the message counts in the window before and after, the new message ids and timestamps, pages requested and the stop reason

#### Scenario: Pacing and budget
- **WHEN** the command runs
- **THEN** it sends at most one request at a time, waits at least the configured delay (default 10 seconds, plus random jitter) between requests, and stops before exceeding the total request budget (default 150)

#### Scenario: Error streak abort
- **WHEN** three consecutive requests fail (any non-200 response other than 404)
- **THEN** the command stops sending, writes the report with the failures and exits non-zero

#### Scenario: Resume after interruption
- **WHEN** the command is rerun with the same progress file after an interrupted run
- **THEN** chats already marked complete are skipped and the remaining chats are processed

#### Scenario: Dry run
- **WHEN** the command runs with `--dry-run`
- **THEN** it lists the chats it would process and their current counts, and sends no requests
