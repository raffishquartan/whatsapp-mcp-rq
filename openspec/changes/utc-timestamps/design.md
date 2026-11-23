## Context

See proposal.md for motivation. Verified facts (2026-11-23, read-only queries on a deployed `messages.db`):

- go-sqlite3 v1.14.52 binds a `time.Time` as text using `SQLiteTimestampFormats[0]` (`2006-01-02 15:04:05.999999999-07:00`) in the value's own `Location` (sqlite3.go bind case `time.Time`, ~line 2265). A UTC value renders as `+00:00` (not `Z`, because the layout uses `-07:00`). Whole seconds drop the fractional part, so every stored value is 25 characters (confirmed: all 11356 rows).
- Reads of `TIMESTAMP` columns try the same formats with `ParseInLocation(..., time.UTC)`, so offsets round-trip; `_loc` only affects reads and is not set in the DSN (`main.go:58`).
- The bridge produces `time.Unix(ts, 0)` (history sync, `main.go:1086` and `:1153`) and whatsmeow's `Info.Timestamp` (live path, `main.go:422`, `:444`; whatsmeow builds it with `ag.UnixTime("t")`, i.e. `time.Unix`). Both are in `time.Local`, so the stored offset is the host zone's offset at write time.
- Mixed offsets confirmed: `+00:00`, `+01:00` and `+02:00` all occur in `messages.timestamp` and in `chats.last_message_time`. The cause (the host time zone at write time) is not needed for the fix. Column type is text for every row; no zero-time or NULL messages values.
- The real bugs are wider than display. The Python server compares and orders timestamps as strings in SQL (`whatsapp.py` `WHERE messages.timestamp > ?`, `ORDER BY messages.timestamp`, and `chats.last_message_time = messages.timestamp` joins). With mixed offsets these are wrong: string order differs from instant order, and the join silently drops the last-message text when the same instant is stored with two offsets. Single-offset UTC storage fixes all of these; Python-side conversion alone would not.
- `format_message` prints `%Y-%m-%d %H:%M:%S` with no offset, so callers cannot tell which zone a time is in.

Complete write and read site inventory:

- Go writes: `StoreChat` (`main.go:103`) called from `:422` (live), `:1091` (history sync); `StoreMessage` (`main.go:112`) called from `:439` (live), `:1158` (history sync). `StoreMediaInfo` does not touch timestamps. No other statement writes the two columns (`CREATE TABLE` at `:68`, `:76` only declare them).
- Go reads: `GetMessages` (`:129`) and `GetChats` (`:155`) scan to `time.Time`; they are unaffected by a uniform UTC column. Log lines at `:459`, `:1180`, `:1183` format with `.Format` for console only.
- Python reads (`whatsapp-mcp-server/whatsapp.py`): `datetime.fromisoformat` at 193, 249, 272, 295, 376, 469, 515, 569, 612; filter parsing at 150 and 159; SQL comparisons at 154, 163, 264, 287 and string-equality joins at 346, 555, 599; ordering at 183, 265, 288, 360, 458, 505; `format_message` at 99 and 101; dataclasses `Message.timestamp` and `Chat.last_message_time` at 15 and 28. `main.py` and `audio.py` do not handle timestamps.

## Goals / Non-Goals

**Goals:**
- One stored representation (UTC, `+00:00`) written at one choke point per table.
- A migration that is provably instant-preserving, re-runnable and reversible from backup.
- UK-local presentation and UK-local filter semantics in the MCP server.

**Non-Goals:**
- Changing the column types, adding indexes, or storing epoch integers.
- Backfilling or fixing missing messages (history-sync-fix) or caption content (caption-extraction).
- Changing `whatsapp.db` (whatsmeow's own store).
- Making the display zone configurable (Europe/London is a constant).

## Decisions

1. Normalise inside `StoreChat` and `StoreMessage` with `timestamp.UTC()`, not at the four call sites. One place per table cannot be missed by a future caller. Alternatives: normalise at each call site (drift risk, rejected); set `time.Local = time.UTC` in `main` (global side effect that also changes log output and any other `time.Local` use, rejected); `_loc=UTC` DSN param (affects reads only, does not fix writes, rejected).
2. Keep text `YYYY-MM-DD HH:MM:SS+00:00` rather than epoch integers. It is exactly what go-sqlite3 already writes for UTC, needs no schema change, keeps `fromisoformat` working, and makes lexical order equal instant order once the offset is uniform. Epoch integers would be cleaner but force a schema and read-path change in both languages for no user-visible gain.
3. Python: add a small `to_london(dt)` and `parse_filter(s)` helper pair and a single `parse_db_timestamp(s)` that returns an aware UTC datetime (`fromisoformat(s).astimezone(UTC)`); all nine read sites call it, then `format_message` converts with `ZoneInfo("Europe/London")` and renders `%Y-%m-%d %H:%M:%S%z` as `+01:00` form (use `isoformat(sep=" ")`). Parsing through `astimezone` keeps the server correct on any not-yet-migrated row, so MCP deploy order relative to the migration is not critical for display, only for SQL comparison correctness.
4. Filters: naive input gets `tzinfo=ZoneInfo("Europe/London")`; aware input is kept. Convert to UTC and bind a formatted string `"%Y-%m-%d %H:%M:%S+00:00"` explicitly. Do not rely on the sqlite3 default datetime adapter (deprecated in 3.12, and its output format is not guaranteed to match the stored format). DST edges follow `zoneinfo` fold=0 semantics: an ambiguous local time (autumn repeat hour) resolves to the first occurrence (BST); a non-existent time (spring gap) is shifted forward by `zoneinfo`'s normal rules. Documented in the tool docstring.
5. Migration is a standalone Python script, `scripts/migrate_timestamps_to_utc.py`, standard library only, dry-run by default (`--apply` to write). Design:
   - Refuse to run if a TCP connect to `localhost:8080` succeeds (bridge up) or `--db` is missing.
   - Take a consistent copy with the SQLite backup API to `messages.db.bak-<yyyyMMdd-HHmm>-pre-utc` (apply mode only; skipped by `--no-backup` for tests).
   - Pre-snapshot: for `messages` (key `id`,`chat_jid`) and `chats` (key `jid`): row count, NULL count, min and max epoch, and a SHA-256 over the sorted `key|epoch` lines.
   - Strict parse: each non-NULL value must match `^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(\.\d+)?[+-]\d\d:\d\d$`; anything else (naive, `Z`, `T` separator, garbage) aborts before any write, listing table, key and value. No guessing a zone.
   - Single `BEGIN IMMEDIATE` transaction updates only rows whose value is not already canonical, using `UPDATE ... WHERE rowid = ?`.
   - Post-snapshot inside the same transaction; require equal counts, equal NULL counts, equal min and max epoch, equal hash, and every non-NULL value canonical UTC (`+00:00`, 25 chars). Any mismatch raises, rolls back, exit non-zero. Success prints a table of before and after and rows changed per offset.
   - Re-run is a no-op: zero rows changed, snapshots equal, exit 0.
   - Also report, informationally, the number of `chats` whose `last_message_time` equals the latest message `timestamp` before and after (expected to rise, never fall).
6. Python tests use `pytest` as a dev dependency group in `whatsapp-mcp-server/pyproject.toml` with temp SQLite fixtures; Go tests chdir to `t.TempDir()` before `NewMessageStore()` (the DSN is the relative `store/messages.db`), avoiding a refactor of the constructor.
7. Deployment order (see Migration Plan) puts the new bridge binary in place before any bridge restart after migration, because an old binary restarted on a BST host would immediately write `+01:00` rows again.

## Risks / Trade-offs

- [Old bridge writes local offsets between migration and rebuild] -> the migration step is run with the bridge stopped and the very next step is the install script, which builds before starting; no old binary is started in between.
- [Bridge writes during migration] -> bridge-running check plus `BEGIN IMMEDIATE`; the bridge is stopped by hand first.
- [A row has an unexpected format] -> strict parse aborts before any write; dry-run run first.
- [WhatsApp history sync later re-delivers old messages] -> `INSERT OR REPLACE` writes them through `StoreMessage`, now UTC, so no regression.
- [Output change breaks a consumer parsing `YYYY-MM-DD HH:MM:SS`] -> offset appended after seconds; known consumers are Claude sessions reading text. The output format is an owner decision; the offset-appended form is the default chosen here.
- [Concurrent proposals edit `main.go` and `whatsapp.py`] -> this change touches only `StoreChat`/`StoreMessage` bodies in Go and the read/format helpers in Python.
- [Host `tzdata` missing] -> add `tzdata` dependency; macOS has `/usr/share/zoneinfo`, verified.

## Migration Plan

1. Merge and pull the change; bridge still running the old binary.
2. Dry-run on a copy: `scripts/migrate_timestamps_to_utc.py --db <copy>`; read the report.
3. Stop the bridge (`lsof -i :8080`, kill the pid).
4. `scripts/migrate_timestamps_to_utc.py --db <bridge-dir>/store/messages.db --apply` (writes backup, verifies, commits or rolls back).
5. Run the bridge install script (pulls, builds, restarts the bridge with the UTC-writing binary).
6. Restart the MCP server (new Claude session) and check: `list_messages` over a BST date and a GMT date match WhatsApp Web; `SELECT DISTINCT substr(timestamp,-6) FROM messages` returns only `+00:00`; send or receive one message and re-check.
Rollback: stop the bridge, copy the backup over `messages.db`, reinstall the previous commit via the install script with `--no-pull` after checking out the previous commit, restart.

## Open Questions

- None that block the specs; the presentation format (offset appended) and the DB-level guard (none) are decided in this design.
