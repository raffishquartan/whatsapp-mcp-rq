## Purpose
Defines a single, unambiguous representation for WhatsApp message and chat timestamps in the bridge database, and how the MCP server presents and filters them in UK local time.

## ADDED Requirements

### Requirement: Stored timestamps are UTC
The bridge SHALL store every value written to `messages.timestamp` and `chats.last_message_time` as the UTC instant in the form `YYYY-MM-DD HH:MM:SS+00:00`, regardless of the host time zone and of the code path that wrote it (live message, history sync, chat update).

#### Scenario: Message written while the host zone is BST
- **WHEN** the bridge stores a message whose instant is 2026-07-03T09:49:42Z while the host zone is Europe/London (+01:00)
- **THEN** the stored `timestamp` is `2026-07-03 09:49:42+00:00`

#### Scenario: Chat last-message time written from history sync
- **WHEN** the history-sync handler stores a chat whose latest message instant is 2026-08-24T22:25:15Z while the host zone has a +02:00 offset
- **THEN** the stored `last_message_time` is `2026-08-24 22:25:15+00:00`

#### Scenario: Chat and message times agree
- **WHEN** a chat's last message is stored through any code path
- **THEN** `chats.last_message_time` is byte-for-byte equal to that message's `messages.timestamp`

### Requirement: Existing rows are migrated without changing instants
A migration SHALL rewrite every existing `messages.timestamp` and `chats.last_message_time` to UTC form, and SHALL leave the set of instants, the row counts and all other columns unchanged. The migration SHALL be re-runnable (a second run changes nothing) and SHALL abort and roll back, leaving the database untouched, if any verification fails.

#### Scenario: Mixed-offset database
- **WHEN** the migration runs on a database containing `+00:00`, `+01:00` and `+02:00` rows
- **THEN** afterwards every value ends in `+00:00`, row counts per table are equal to before, the per-row instants (keyed by `id`, `chat_jid` and by `jid`) are identical, and min and max instants per column are identical

#### Scenario: Second run
- **WHEN** the migration runs on an already migrated database
- **THEN** it reports zero rows changed and exits successfully

#### Scenario: Unparseable value
- **WHEN** a timestamp value is not an ISO-8601 string with an explicit offset
- **THEN** the migration aborts with an error naming the table, key and value, and no row is modified

#### Scenario: Bridge still running
- **WHEN** the migration is started while the bridge is accepting connections on its API port
- **THEN** it refuses to run and exits with a non-zero status

#### Scenario: Backup
- **WHEN** the migration is run in apply mode
- **THEN** a consistent backup of the database is written before any row is modified, and its path is reported

### Requirement: MCP output is Europe/London time
The MCP server SHALL present every message and chat timestamp converted to the Europe/London zone, with the correct BST or GMT offset for that instant, and SHALL include the offset in the rendered text.

#### Scenario: BST instant
- **WHEN** a message stored as `2026-07-03 09:49:42+00:00` is listed
- **THEN** it is shown as `2026-07-03 10:49:42+01:00`

#### Scenario: GMT instant
- **WHEN** a message stored as `2026-11-23 12:24:53+00:00` is listed
- **THEN** it is shown as `2026-11-23 12:24:53+00:00`

#### Scenario: Legacy offset row not yet migrated
- **WHEN** a row still stored as `2026-07-28 11:46:00+02:00` is listed
- **THEN** it is shown as `2026-07-28 10:46:00+01:00`

### Requirement: Date filters use UK local time and compare instants
The `after` and `before` filters of message listing SHALL interpret a timezone-naive ISO-8601 input as Europe/London local time, SHALL honour an explicit offset when one is given, and SHALL compare as UTC instants against stored values.

#### Scenario: Naive input in BST
- **WHEN** `after` is `2026-07-03T00:30:00` (no offset)
- **THEN** only messages with instant later than 2026-07-02T23:30:00Z are returned

#### Scenario: Naive input in GMT
- **WHEN** `before` is `2026-11-23T12:00:00`
- **THEN** only messages with instant earlier than 2026-11-23T12:00:00Z are returned

#### Scenario: Explicit offset
- **WHEN** `after` is `2026-07-03T00:30:00+00:00`
- **THEN** the filter is the instant 2026-07-03T00:30:00Z, not London local time

#### Scenario: Invalid input
- **WHEN** `after` or `before` is not valid ISO-8601
- **THEN** the call fails with an error naming the parameter and value
