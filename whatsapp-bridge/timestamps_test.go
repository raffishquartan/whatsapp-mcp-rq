package main

import (
	"testing"
	"time"
)

// rawText returns a stored timestamp column exactly as SQLite holds it. The CAST
// strips the TIMESTAMP declared type, so go-sqlite3 does not parse it into a time.Time.
func rawText(t *testing.T, store *MessageStore, query string, args ...any) string {
	t.Helper()
	var s string
	if err := store.db.QueryRow(query, args...).Scan(&s); err != nil {
		t.Fatalf("query %q: %v", query, err)
	}
	return s
}

func storedMessageTime(t *testing.T, store *MessageStore, id string) string {
	t.Helper()
	return rawText(t, store, "SELECT CAST(timestamp AS TEXT) FROM messages WHERE id = ?", id)
}

func storedChatTime(t *testing.T, store *MessageStore, jid string) string {
	t.Helper()
	return rawText(t, store, "SELECT CAST(last_message_time AS TEXT) FROM chats WHERE jid = ?", jid)
}

func mustLoadLocation(t *testing.T, name string) *time.Location {
	t.Helper()
	loc, err := time.LoadLocation(name)
	if err != nil {
		t.Fatalf("LoadLocation(%q): %v", name, err)
	}
	return loc
}

func timestampCases(t *testing.T) []struct {
	name string
	in   time.Time
	want string
} {
	london := mustLoadLocation(t, "Europe/London")
	plusTwo := time.FixedZone("UTC+2", 2*60*60)
	bst := time.Date(2026, 7, 3, 9, 49, 42, 0, time.UTC)
	gmt := time.Date(2026, 11, 23, 12, 24, 53, 0, time.UTC)
	cest := time.Date(2026, 8, 24, 22, 25, 15, 0, time.UTC)
	return []struct {
		name string
		in   time.Time
		want string
	}{
		{"london bst", bst.In(london), "2026-07-03 09:49:42+00:00"},
		{"london gmt", gmt.In(london), "2026-11-23 12:24:53+00:00"},
		{"fixed plus two", cest.In(plusTwo), "2026-08-24 22:25:15+00:00"},
		{"utc", gmt, "2026-11-23 12:24:53+00:00"},
	}
}

func TestStoreMessagePersistsUTC(t *testing.T) {
	store := newTestStore(t)
	for _, tc := range timestampCases(t) {
		t.Run(tc.name, func(t *testing.T) {
			id := "ts-" + tc.name
			if _, err := store.StoreMessage(id, "chat@g.us", "sender", "hello", tc.in, false,
				"", "", "", nil, nil, nil, 0); err != nil {
				t.Fatalf("StoreMessage: %v", err)
			}
			if got := storedMessageTime(t, store, id); got != tc.want {
				t.Errorf("stored timestamp = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestStoreChatPersistsUTC(t *testing.T) {
	store := newTestStore(t)
	for _, tc := range timestampCases(t) {
		t.Run(tc.name, func(t *testing.T) {
			jid := "ts-" + tc.name + "@g.us"
			if err := store.StoreChat(jid, "Test", tc.in); err != nil {
				t.Fatalf("StoreChat: %v", err)
			}
			if got := storedChatTime(t, store, jid); got != tc.want {
				t.Errorf("stored last_message_time = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestStoreMessageReplaceKeepsUTC(t *testing.T) {
	store := newTestStore(t)
	instant := time.Date(2026, 7, 3, 9, 49, 42, 0, time.UTC)
	plusTwo := time.FixedZone("UTC+2", 2*60*60)
	london := mustLoadLocation(t, "Europe/London")
	for _, in := range []time.Time{instant.In(london), instant.In(plusTwo)} {
		if _, err := store.StoreMessage("dup", "chat@g.us", "sender", "hello", in, false,
			"", "", "", nil, nil, nil, 0); err != nil {
			t.Fatalf("StoreMessage: %v", err)
		}
		if got, want := storedMessageTime(t, store, "dup"), "2026-07-03 09:49:42+00:00"; got != want {
			t.Errorf("after replace with %s: stored = %q, want %q", in.Location(), got, want)
		}
	}
	var n int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM messages WHERE id = 'dup'").Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Errorf("row count = %d, want 1", n)
	}
}

func TestChatAndMessageTimesJoinEqual(t *testing.T) {
	store := newTestStore(t)
	// Message and chat get the same instant in different zones, as the live and
	// history-sync paths can when the host zone differs from the sender's.
	instant := time.Date(2026, 8, 24, 22, 25, 15, 0, time.UTC)
	if _, err := store.StoreMessage("last", "chat@g.us", "sender", "hello",
		instant.In(time.FixedZone("UTC+2", 2*60*60)), false, "", "", "", nil, nil, nil, 0); err != nil {
		t.Fatalf("StoreMessage: %v", err)
	}
	if err := store.StoreChat("chat@g.us", "Test", instant.In(mustLoadLocation(t, "Europe/London"))); err != nil {
		t.Fatalf("StoreChat: %v", err)
	}
	var content string
	err := store.db.QueryRow(`SELECT m.content FROM chats c
		JOIN messages m ON m.chat_jid = c.jid AND c.last_message_time = m.timestamp
		WHERE c.jid = 'chat@g.us'`).Scan(&content)
	if err != nil {
		t.Fatalf("join on last_message_time = timestamp found no row: %v", err)
	}
	if content != "hello" {
		t.Errorf("joined content = %q, want %q", content, "hello")
	}
}
