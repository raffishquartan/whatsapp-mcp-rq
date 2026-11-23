# WhatsApp MCP Server

This is a Model Context Protocol (MCP) server for WhatsApp.

With this you can search and read your personal Whatsapp messages (including images, videos, documents, and audio messages), search your contacts and send messages to either individuals or groups. You can also send media files including images, videos, documents, and audio messages.

It connects to your **personal WhatsApp account** directly via the Whatsapp web multidevice API (using the [whatsmeow](https://github.com/tulir/whatsmeow) library). All your messages are stored locally in a SQLite database and only sent to an LLM (such as Claude) when the agent accesses them through tools (which you control).

Here's an example of what you can do when it's connected to Claude.

![WhatsApp MCP](./example-use.png)

> To get updates on this and other projects I work on [enter your email here](https://docs.google.com/forms/d/1rTF9wMBTN0vPfzWuQa2BjfGKdKIpTbyeKxhPMcEzgyI/preview)

> *Caution:* as with many MCP servers, the WhatsApp MCP is subject to [the lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/). This means that project injection could lead to private data exfiltration.

## Installation

### Prerequisites

- Go
- Python 3.6+
- Anthropic Claude Desktop app (or Cursor)
- UV (Python package manager), install with `curl -LsSf https://astral.sh/uv/install.sh | sh`
- FFmpeg (_optional_) - Only needed for audio messages. If you want to send audio files as playable WhatsApp voice messages, they must be in `.ogg` Opus format. With FFmpeg installed, the MCP server will automatically convert non-Opus audio files. Without FFmpeg, you can still send raw audio files using the `send_file` tool.

### Steps

1. **Clone this repository**

   ```bash
   git clone https://github.com/lharries/whatsapp-mcp.git
   cd whatsapp-mcp
   ```

2. **Run the WhatsApp bridge**

   Navigate to the whatsapp-bridge directory and run the Go application:

   ```bash
   cd whatsapp-bridge
   go run main.go
   ```

   The first time you run it, you will be prompted to scan a QR code. Scan the QR code with your WhatsApp mobile app to authenticate.

   After approximately 20 days, you will might need to re-authenticate.

3. **Connect to the MCP server**

   Copy the below json with the appropriate {{PATH}} values:

   ```json
   {
     "mcpServers": {
       "whatsapp": {
         "command": "{{PATH_TO_UV}}", // Run `which uv` and place the output here
         "args": [
           "--directory",
           "{{PATH_TO_SRC}}/whatsapp-mcp/whatsapp-mcp-server", // cd into the repo, run `pwd` and enter the output here + "/whatsapp-mcp-server"
           "run",
           "main.py"
         ]
       }
     }
   }
   ```

   For **Claude**, save this as `claude_desktop_config.json` in your Claude Desktop configuration directory at:

   ```
   ~/Library/Application Support/Claude/claude_desktop_config.json
   ```

   For **Cursor**, save this as `mcp.json` in your Cursor configuration directory at:

   ```
   ~/.cursor/mcp.json
   ```

4. **Restart Claude Desktop / Cursor**

   Open Claude Desktop and you should now see WhatsApp as an available integration.

   Or restart Cursor.

### Windows Compatibility

If you're running this project on Windows, be aware that `go-sqlite3` requires **CGO to be enabled** in order to compile and work properly. By default, **CGO is disabled on Windows**, so you need to explicitly enable it and have a C compiler installed.

#### Steps to get it working:

1. **Install a C compiler**  
   We recommend using [MSYS2](https://www.msys2.org/) to install a C compiler for Windows. After installing MSYS2, make sure to add the `ucrt64\bin` folder to your `PATH`.  
   → A step-by-step guide is available [here](https://code.visualstudio.com/docs/cpp/config-mingw).

2. **Enable CGO and run the app**

   ```bash
   cd whatsapp-bridge
   go env -w CGO_ENABLED=1
   go run main.go
   ```

Without this setup, you'll likely run into errors like:

> `Binary was compiled with 'CGO_ENABLED=0', go-sqlite3 requires cgo to work.`

## Architecture Overview

This application consists of two main components:

1. **Go WhatsApp Bridge** (`whatsapp-bridge/`): A Go application that connects to WhatsApp's web API, handles authentication via QR code, and stores message history in SQLite. It serves as the bridge between WhatsApp and the MCP server.

2. **Python MCP Server** (`whatsapp-mcp-server/`): A Python server implementing the Model Context Protocol (MCP), which provides standardized tools for Claude to interact with WhatsApp data and send/receive messages.

### Data Storage

- All message history is stored in a SQLite database within the `whatsapp-bridge/store/` directory
- The database maintains tables for chats and messages
- Messages are indexed for efficient searching and retrieval

## Usage

Once connected, you can interact with your WhatsApp contacts through Claude, leveraging Claude's AI capabilities in your WhatsApp conversations.

### MCP Tools

Claude can access the following tools to interact with WhatsApp:

- **search_contacts**: Search for contacts by name or phone number
- **list_messages**: Retrieve messages with optional filters and context
- **list_chats**: List available chats with metadata
- **get_chat**: Get information about a specific chat
- **get_direct_chat_by_contact**: Find a direct chat with a specific contact
- **get_contact_chats**: List all chats involving a specific contact
- **get_last_interaction**: Get the most recent message with a contact
- **get_message_context**: Retrieve context around a specific message
- **send_message**: Send a WhatsApp message to a specified phone number or group JID
- **send_file**: Send a file (image, video, raw audio, document) to a specified recipient
- **send_audio_message**: Send an audio file as a WhatsApp voice message (requires the file to be an .ogg opus file or ffmpeg must be installed)
- **download_media**: Download media from a WhatsApp message and get the local file path

### Media Handling Features

The MCP server supports both sending and receiving various media types:

#### Media Sending

You can send various media types to your WhatsApp contacts:

- **Images, Videos, Documents**: Use the `send_file` tool to share any supported media type.
- **Voice Messages**: Use the `send_audio_message` tool to send audio files as playable WhatsApp voice messages.
  - For optimal compatibility, audio files should be in `.ogg` Opus format.
  - With FFmpeg installed, the system will automatically convert other audio formats (MP3, WAV, etc.) to the required format.
  - Without FFmpeg, you can still send raw audio files using the `send_file` tool, but they won't appear as playable voice messages.

#### Media Downloading

By default, just the metadata of the media is stored in the local database. The message will indicate that media was sent. To access this media you need to use the download_media tool which takes the `message_id` and `chat_jid` (which are shown when printing messages containing the meda), this downloads the media and then returns the file path which can be then opened or passed to another tool.

### Backfilling Missed History

Messages that arrive while the bridge is not running are not in `messages.db`. The bridge can ask your phone for older messages of a chat, one page at a time. This only pages **backwards** from a message already stored: it cannot recover messages newer than a chat's newest stored message (those arrive through WhatsApp's offline queue when the bridge reconnects). Your phone must be online (Wi-Fi or mobile data), because it serves the answer.

#### Endpoint: `POST /api/request_history_sync`

Loopback only (`http://localhost:8080/api/request_history_sync`). One request at a time; the call blocks until the phone answers or 60 seconds pass after the request was sent. Sending is itself limited to 30 seconds, so a call takes at most 90 seconds after the anchor lookup.

Request body (JSON):

| Field | Required | Meaning |
|---|---|---|
| `chat_jid` | yes | Chat to backfill, e.g. `123456789@g.us` or `123@lid`. Broadcast chats are rejected. |
| `count` | no | Messages to ask for, 1-100 (default 50). |
| `before_message_id` | no | Stored message id to page back from (default: the chat's oldest stored message). |

Successful response (200):

```json
{
  "success": true,
  "message": "received 50 messages for 123456789@g.us",
  "request_id": "3EB0...",
  "received": 50,
  "stored_new": 12,
  "stored_existing": 36,
  "skipped": 2,
  "oldest_timestamp": "2026-10-28T09:14:03Z",
  "next_before_message_id": "3A1F..."
}
```

- `received`: messages in the answer for the chat; `stored_new`: rows that were not in `messages.db`; `stored_existing`: rows that were already there and were rewritten; `skipped`: messages with no text and no media.
- `next_before_message_id` is the oldest stored message from the answer: pass it as `before_message_id` to get the next older page. It is `null` whenever the answer stored nothing: at the start of the chat (`received: 0`), and also when the answer held messages but none could be stored (`received` > 0, every message skipped). Only `received: 0` means the start of the chat; with `received` > 0 and a `null` next id there is no anchor to page further from.
- Storage is idempotent, so overlapping pages are harmless, and an older page never moves a chat's last-message time backwards.

Errors return `{"success": false, "message": "..."}` and send nothing to WhatsApp unless noted:

| Status | When |
|---|---|
| 400 | Malformed JSON, missing/invalid `chat_jid`, broadcast chat, `count` outside 1-100 |
| 404 | No stored message in the chat to anchor on, or `before_message_id` not stored for the chat |
| 405 | Method other than POST |
| 409 | Another history request is still waiting for its answer |
| 502 | Sending the request to the phone failed or did not complete within 30 seconds (message has the error); or the phone's answer to this request held no conversation for `chat_jid` (body has `"reason": "chat_not_in_answer"`, `request_id`, and `conversations`, a JSON array of the conversation JIDs the answer did hold). The latter says nothing about the chat's history; it is not the start of the chat |
| 503 | Bridge not connected or not logged in |
| 504 | No answer within 60 seconds (body includes `request_id`); a late answer is still stored |

Example:

```bash
curl -s -X POST http://localhost:8080/api/request_history_sync \
  -H 'Content-Type: application/json' \
  -d '{"chat_jid": "123456789@g.us", "count": 50}'
```

#### Bulk backfill: `whatsapp-history-backfill`

Run from `whatsapp-mcp-server/` with the bridge running:

```bash
uv run whatsapp-history-backfill --since 2026-01-01 --out /path/to/working/backfill --dry-run   # list chats, send nothing
uv run whatsapp-history-backfill --since 2026-01-01 --out /path/to/working/backfill --canary <group-jid>
```

What it does:

1. Snapshots, from `messages.db` opened read-only, every non-broadcast chat with a message on or after `--since` (UTC), with the ids of its messages in that window.
2. Orders the chats: `--canary` first, then groups, then direct chats, each most recently active first.
3. For each chat, requests 50 messages before the chat's newest stored message, then follows `next_before_message_id` until the answer's oldest message is older than `--since` ("reached since"), the answer for the chat is empty ("start of chat"), or `--max-pages-per-chat` is hit ("page cap"). Two inconclusive answers also stop the chat without claiming coverage: an answer with messages but nothing stored, so no next anchor ("no stored message in answer"), and a 502 `chat_not_in_answer` ("chat not in answer", recorded for the chat but not counted toward the abort). An answer whose `next_before_message_id` is an anchor already requested for the chat in this run also stops it ("no progress", counted as a failure), so the command never sends the same request twice in a run.
4. Waits up to 120 seconds for each response (the bridge's 90-second maximum plus a 30-second margin). Paces requests: one at a time, at least `--delay` seconds plus 0-5 seconds of random jitter apart, never more than `--budget` requests in total. A 409 waits and retries once; a 504 or other failure moves on to the next chat; three consecutive failures (404 and the 502 `chat_not_in_answer` do not count) abort the run; ten consecutive chats answered with `chat_not_in_answer` also stop it, because that points to a systemic matching problem. Any processed answer resets that count.
5. Saves progress after each chat to `--progress`; rerunning with the same file skips chats that are complete ("reached since", "start of chat", or a 404 "no anchor") and resumes every other chat from the anchor it stopped at. Page and request counts are totals across runs, so a chat that hit the page cap continues only when rerun with a higher `--max-pages-per-chat`.
6. Re-snapshots and writes `report.md` and `report.csv` to `--out`: per chat, the window counts before and after, the new message ids and timestamps (never message content), pages, stop reason, newest stored timestamp and errors, plus a summary listing any chat that did not reach `--since` and any `--only` chat with no stored message in the window (also logged as a warning). Text cells starting with `=`, `+`, `-`, `@`, a tab or a carriage return get a leading `'` in `report.csv` so spreadsheets do not run them as formulas.

Exit status: 0 when every chat reached `--since` or its start; 1 when the run aborted, ran out of budget, or any chat stopped for another reason; 2 for invalid input (including a selection with no chats, and a progress file that is not valid JSON or has a missing or wrongly typed field).

| Flag | Default | Meaning |
|---|---|---|
| `--since DATE` | required | Window start, ISO date in UTC (`YYYY-MM-DD`) |
| `--out DIR` | required | Directory for `report.md`, `report.csv` and `progress.json`; keep it outside the repository |
| `--db PATH` | `whatsapp-bridge/store/messages.db` of this checkout | Database to read (read-only). The default is resolved from the location of `whatsapp-mcp-server/whatsapp.py`, so it is right for `uv run` in this checkout; pass `--db` if the command is installed elsewhere. `--help` prints the resolved default |
| `--bridge-url URL` | `http://localhost:8080/api` | Bridge REST API base URL |
| `--only JID` | all chats | Process only this chat; repeat for several (a chat with no messages in the window is skipped with a warning and listed in the report) |
| `--canary JID` | none | Process this chat first (ignored with a warning if it has no messages in the window) |
| `--delay SECONDS` | 10 | Minimum gap between requests, plus 0-5 s jitter |
| `--budget N` | 150 | Maximum requests in this run |
| `--max-pages-per-chat N` | 10 | Maximum pages of 50 messages per chat, counted across reruns |
| `--progress PATH` | `OUT/progress.json` | Progress file used to resume |
| `--dry-run` | off | List the chats and their current counts; send nothing |

#### Ban-risk checklist

Each request is a peer message to your own phone, the same request WhatsApp Web sends when you scroll up past its local cache. Risk comes from volume and pattern, so before a bulk run:

- Search for recent reports of WhatsApp bans affecting unofficial/linked-device clients; do not run if there is a fresh wave.
- Back up `messages.db` (copy it while the bridge is stopped, or use `sqlite3 messages.db ".backup backup.db"`).
- Run with `--dry-run` first and check the chat list.
- Start with a canary: `--only <one-chat-jid>` or `--canary <one-chat-jid>`, and check its report before the full run.
- Keep the default pacing and budget; never run two backfills at once.
- If WhatsApp shows any warning, stop and do not rerun.

#### If the phone never answers

Answers are not guaranteed. If requests keep timing out (504) or always come back empty for a chat you know has older messages, treat the endpoint as best effort: report the chats concerned, check the gap by hand on the phone, and do not retry in a loop. If no request is ever answered across several attempts on different days with the phone confirmed online, stop using the feature.

## Technical Details

1. Claude sends requests to the Python MCP server
2. The MCP server queries the Go bridge for WhatsApp data or directly to the SQLite database
3. The Go accesses the WhatsApp API and keeps the SQLite database up to date
4. Data flows back through the chain to Claude
5. When sending messages, the request flows from Claude through the MCP server to the Go bridge and to WhatsApp

## Troubleshooting

- If you encounter permission issues when running uv, you may need to add it to your PATH or use the full path to the executable.
- Make sure both the Go application and the Python server are running for the integration to work properly.

### Authentication Issues

- **QR Code Not Displaying**: If the QR code doesn't appear, try restarting the authentication script. If issues persist, check if your terminal supports displaying QR codes.
- **WhatsApp Already Logged In**: If your session is already active, the Go bridge will automatically reconnect without showing a QR code.
- **Device Limit Reached**: WhatsApp limits the number of linked devices. If you reach this limit, you'll need to remove an existing device from WhatsApp on your phone (Settings > Linked Devices).
- **No Messages Loading**: After initial authentication, it can take several minutes for your message history to load, especially if you have many chats.
- **WhatsApp Out of Sync**: If your WhatsApp messages get out of sync with the bridge, delete both database files (`whatsapp-bridge/store/messages.db` and `whatsapp-bridge/store/whatsapp.db`) and restart the bridge to re-authenticate.

For additional Claude Desktop integration troubleshooting, see the [MCP documentation](https://modelcontextprotocol.io/quickstart/server#claude-for-desktop-integration-issues). The documentation includes helpful tips for checking logs and resolving common issues.
