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

#### Scenario: Send does not complete in time
- **WHEN** sending the peer message has not completed within the send timeout (30 seconds)
- **THEN** the bridge abandons the send and responds 502 with the error text, so one request takes at most 90 seconds (30-second send plus 60-second wait) after the anchor lookup

#### Scenario: LID chat requested by its phone-number JID
- **WHEN** `chat_jid` is a `@lid` JID and the client's LID store maps it to a phone-number JID
- **THEN** the request names the chat by that JID without a device part, anchored on the stored message of the LID chat; a conversation under that JID is stored under `chat_jid`, completes the request, and the response's `next_before_message_id` names a stored message of `chat_jid`; with no mapping, or a lookup error, the request names the LID

#### Scenario: Answer without the requested chat
- **WHEN** the on-demand history sync that matches the request id holds no conversation for `chat_jid` (for example the phone answered under a JID other than the chat's or, for a `@lid` chat, its mapped phone-number JID)
- **THEN** the bridge stores what the answer holds and responds 502 with `reason: "chat_not_in_answer"`, the `request_id`, and a `conversations` JSON array of the conversation JIDs the answer held (empty when it held none); it does not report the start of the chat

#### Scenario: Late answer to an earlier request
- **WHEN** an on-demand history sync carrying another request's original message id arrives while a request is pending, including while that request is still being sent
- **THEN** the bridge stores it and does not complete the pending request with it

#### Scenario: Phone does not answer in time
- **WHEN** no matching on-demand history sync arrives within the wait timeout (60 seconds, counted from when the send returns)
- **THEN** the bridge responds 504 with the request id, and still stores the answer if it arrives later

### Requirement: Result reporting
A successful response SHALL report what the phone returned and what changed in `messages.db`, so the caller can prove what was added.

#### Scenario: Successful answer
- **WHEN** the matching on-demand history sync arrives
- **THEN** the response contains `request_id`, `received` (messages in the answer for that chat), `stored_new` (rows that did not exist before), `stored_existing` (rows that already existed and were rewritten), `skipped` (messages with no text and no media), `oldest_timestamp` (UTC, RFC 3339) of the answer, and `next_before_message_id` (the oldest stored message from the answer, or null when the answer stored nothing)

#### Scenario: Start of chat reached
- **WHEN** the matching answer holds the chat's conversation with no messages older than the anchor
- **THEN** the response has `received: 0` and `next_before_message_id: null`

#### Scenario: Answer with nothing storable
- **WHEN** the matching answer holds messages for the chat but none of them can be stored (no text and no media, or no timestamp)
- **THEN** the response has `received` greater than 0 and `next_before_message_id: null`

### Requirement: History sync storage is idempotent and does not regress chat metadata
Storing messages from any history sync SHALL be idempotent on (`id`, `chat_jid`), SHALL NOT move a chat's `last_message_time` earlier than its current value, SHALL record `sender` in the same form as live messages (the user part of the sender JID), and SHALL report a result for every conversation in the sync, including conversations with no messages.

#### Scenario: Re-requesting the same page
- **WHEN** the same page of history is requested twice
- **THEN** the second answer reports every message as `stored_existing`, and the row count for the chat is unchanged

#### Scenario: Old chunk for an active chat
- **WHEN** an on-demand answer contains only messages older than the chat's stored `last_message_time`
- **THEN** `chats.last_message_time` keeps its previous value

#### Scenario: Sender form matches live messages
- **WHEN** a history-sync message from a group participant (a phone-number or LID JID, possibly with a device) is stored
- **THEN** `messages.sender` holds the participant's user part, as the live message handler stores it

#### Scenario: Own message sender matches live messages
- **WHEN** a history-sync message sent by this account is stored
- **THEN** `messages.sender` holds the participant's user part when the message key carries a participant (this account's LID user in a LID-addressed group, as the live handler stores it), and this account's phone-number user otherwise

#### Scenario: Concurrent live and history writes
- **WHEN** live messages and history-sync messages are stored at the same time
- **THEN** no write fails because the database is locked

### Requirement: Bulk backfill procedure
The project SHALL provide a backfill command that requests history for every chat with at least one stored message on or after a given `--since` date (UTC), excluding broadcast chats, reading `messages.db` read-only for its chat list and its before/after counts. For each chat it SHALL page backwards from the newest stored message until the answer's oldest message is older than `--since`, the answer for the chat is empty (start of chat), or a per-chat page cap is hit. An answer that cannot be paged from (messages but no stored anchor), that does not hold the chat, or whose next anchor was already requested for the chat in the run SHALL stop the chat without marking it complete. The command's HTTP timeout SHALL exceed the bridge's 90-second maximum per request by a fixed margin, so it never abandons a request the bridge is still serving. Its CSV report SHALL neutralise cells that a spreadsheet would read as formulas.

#### Scenario: Full run
- **WHEN** the owner runs the command with `--since <date>` and every request succeeds
- **THEN** every non-broadcast chat with messages since that date is processed, and a report lists, per chat, the message counts in the window before and after, the new message ids and timestamps, pages requested and the stop reason

#### Scenario: Pacing and budget
- **WHEN** the command runs
- **THEN** it sends at most one request at a time, waits at least the configured delay (default 10 seconds, plus random jitter) between requests, and stops before exceeding the total request budget (default 150)

#### Scenario: Error streak abort
- **WHEN** three consecutive requests fail (any non-200 response other than 404 and other than the 502 `chat_not_in_answer`)
- **THEN** the command stops sending, writes the report with the failures and exits non-zero

#### Scenario: Chat not in answer does not trigger the failure abort
- **WHEN** the bridge responds 502 with `reason: "chat_not_in_answer"` for a few consecutive chats
- **THEN** each chat is recorded as "chat not in answer" (not complete, not covered, retried on rerun), the failure streak is neither increased nor reset, and the run continues with the next chat

#### Scenario: Many consecutive chats not in answer stop the run
- **WHEN** ten consecutive chats end as "chat not in answer" (any other chat outcome in between, such as a processed answer, a 404 or a failure, resets the count)
- **THEN** the command stops sending, writes the report naming this reason (not the failure streak) and exits non-zero

#### Scenario: Resume after interruption
- **WHEN** the command is rerun with the same progress file after an interrupted run
- **THEN** chats already marked complete (reached since, start of chat, or unknown anchor) are skipped and every other chat resumes from the anchor it stopped at

#### Scenario: Inconclusive answer
- **WHEN** an answer has `received` greater than 0 and `next_before_message_id: null`, or the bridge responds 502 with `reason: "chat_not_in_answer"`
- **THEN** the chat is listed as not reaching `--since` with its own stop reason (never "start of chat"), and a rerun requests it again

#### Scenario: No progress
- **WHEN** an answer has `received` greater than 0, does not reach `--since`, and its `next_before_message_id` is an anchor already requested for the chat in this run (including the anchor just requested)
- **THEN** the chat stops with the reason "no progress" without sending the repeated request, the stop counts as a failure towards the error-streak abort, the chat is listed as not reaching `--since` (never "start of chat"), and a rerun requests it again from its last anchor

#### Scenario: Selected chat with no messages in the window
- **WHEN** a chat given with `--only` has no stored message on or after `--since`
- **THEN** the command logs a warning naming it, does not request it, and lists it in the report summary

#### Scenario: Corrupt progress file
- **WHEN** the progress file is not valid JSON, lacks a field, or has a field of the wrong type (for example a count that is not a non-negative integer, or an unknown stop reason)
- **THEN** the command sends nothing and exits 2 with an error naming the progress file

#### Scenario: Page cap raised on rerun
- **WHEN** a chat stopped at the page cap and the command is rerun with a higher `--max-pages-per-chat`
- **THEN** the chat continues from its last anchor, and its page count is a total across runs

#### Scenario: Dry run
- **WHEN** the command runs with `--dry-run`
- **THEN** it lists the chats it would process and their current counts, and sends no requests
