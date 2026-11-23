package main

import (
	"bytes"
	"testing"
	"time"

	waProto "go.mau.fi/whatsmeow/binary/proto"
	"google.golang.org/protobuf/proto"
)

func imageMsg(caption string) *waProto.Message {
	return &waProto.Message{ImageMessage: &waProto.ImageMessage{
		Caption:  proto.String(caption),
		URL:      proto.String("https://example.invalid/img"),
		MediaKey: []byte{1, 2, 3},
	}}
}

func videoMsg(caption string) *waProto.Message {
	return &waProto.Message{VideoMessage: &waProto.VideoMessage{Caption: proto.String(caption)}}
}

func documentMsg(caption string) *waProto.Message {
	return &waProto.Message{DocumentMessage: &waProto.DocumentMessage{
		Caption:  proto.String(caption),
		FileName: proto.String("receipt.pdf"),
		URL:      proto.String("https://example.invalid/doc"),
		MediaKey: []byte{4, 5, 6},
	}}
}

func wrappedDocumentMsg(caption string) *waProto.Message {
	return &waProto.Message{DocumentWithCaptionMessage: &waProto.FutureProofMessage{
		Message: documentMsg(caption),
	}}
}

func TestExtractTextContent(t *testing.T) {
	tests := []struct {
		name string
		msg  *waProto.Message
		want string
	}{
		{"nil message", nil, ""},
		{"plain conversation", &waProto.Message{Conversation: proto.String("hello")}, "hello"},
		{"extended text", &waProto.Message{ExtendedTextMessage: &waProto.ExtendedTextMessage{Text: proto.String("link text")}}, "link text"},
		{"image caption", imageMsg("Coffee 4.50"), "Coffee 4.50"},
		{"video caption", videoMsg("video caption"), "video caption"},
		{"document caption", documentMsg("doc caption"), "doc caption"},
		{"image without caption", &waProto.Message{ImageMessage: &waProto.ImageMessage{}}, ""},
		{"video without caption", &waProto.Message{VideoMessage: &waProto.VideoMessage{}}, ""},
		{"document without caption", &waProto.Message{DocumentMessage: &waProto.DocumentMessage{}}, ""},
		{"text wins over caption", &waProto.Message{
			Conversation: proto.String("text"),
			ImageMessage: &waProto.ImageMessage{Caption: proto.String("caption")},
		}, "text"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := extractTextContent(tt.msg); got != tt.want {
				t.Errorf("extractTextContent() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestUnwrapMessage(t *testing.T) {
	inner := imageMsg("inner")
	tests := []struct {
		name string
		raw  *waProto.Message
		want *waProto.Message
	}{
		{"nil", nil, nil},
		{"plain message unchanged", inner, inner},
		{"ephemeral", &waProto.Message{EphemeralMessage: &waProto.FutureProofMessage{Message: inner}}, inner},
		{"view once", &waProto.Message{ViewOnceMessage: &waProto.FutureProofMessage{Message: inner}}, inner},
		{"view once v2", &waProto.Message{ViewOnceMessageV2: &waProto.FutureProofMessage{Message: inner}}, inner},
		{"edited", &waProto.Message{EditedMessage: &waProto.FutureProofMessage{Message: inner}}, inner},
		{"document with caption", wrappedDocumentMsg("c"), documentMsg("c")},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := unwrapMessage(tt.raw)
			if tt.name == "document with caption" {
				if got.GetDocumentMessage().GetCaption() != "c" || got.GetDocumentMessage().GetFileName() != "receipt.pdf" {
					t.Errorf("unwrapped document = %v", got)
				}
				return
			}
			if got != tt.want {
				t.Errorf("unwrapMessage() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestExtractMediaInfoDocumentAndText(t *testing.T) {
	mediaType, filename, url, mediaKey, _, _, _ := extractMediaInfo(documentMsg("c"))
	if mediaType != "document" || filename != "receipt.pdf" || url != "https://example.invalid/doc" || !bytes.Equal(mediaKey, []byte{4, 5, 6}) {
		t.Errorf("got (%q, %q, %q, %v)", mediaType, filename, url, mediaKey)
	}
	if mt, _, _, _, _, _, _ := extractMediaInfo(&waProto.Message{Conversation: proto.String("hi")}); mt != "" {
		t.Errorf("text message mediaType = %q, want empty", mt)
	}
}

func newTestStore(t *testing.T) *MessageStore {
	t.Helper()
	t.Chdir(t.TempDir())
	store, err := NewMessageStore()
	if err != nil {
		t.Fatalf("NewMessageStore: %v", err)
	}
	t.Cleanup(func() { store.Close() })
	if err := store.StoreChat("chat@g.us", "Test", time.Unix(1700000000, 0)); err != nil {
		t.Fatalf("StoreChat: %v", err)
	}
	return store
}

type storedRow struct {
	content, mediaType, filename, url string
}

func readRow(t *testing.T, store *MessageStore, id string) storedRow {
	t.Helper()
	var r storedRow
	err := store.db.QueryRow(
		"SELECT content, media_type, filename, url FROM messages WHERE id = ? AND chat_jid = 'chat@g.us'", id,
	).Scan(&r.content, &r.mediaType, &r.filename, &r.url)
	if err != nil {
		t.Fatalf("read row %s: %v", id, err)
	}
	return r
}

func storeExtracted(t *testing.T, store *MessageStore, id string, msg *waProto.Message) {
	t.Helper()
	unwrapped := unwrapMessage(msg)
	content := extractTextContent(unwrapped)
	mediaType, filename, url, key, sha, encSha, length := extractMediaInfo(unwrapped)
	err := store.StoreMessage(id, "chat@g.us", "sender", content, time.Unix(1700000100, 0), false,
		mediaType, filename, url, key, sha, encSha, length)
	if err != nil {
		t.Fatalf("StoreMessage: %v", err)
	}
}

func TestStoreCaptionedMediaRoundTrip(t *testing.T) {
	store := newTestStore(t)

	storeExtracted(t, store, "img1", imageMsg("Coffee 4.50"))
	storeExtracted(t, store, "doc1", wrappedDocumentMsg("Printer paper 12"))
	storeExtracted(t, store, "vid1", videoMsg("trip"))

	img := readRow(t, store, "img1")
	if img.content != "Coffee 4.50" || img.mediaType != "image" || img.url != "https://example.invalid/img" {
		t.Errorf("image row = %+v", img)
	}
	doc := readRow(t, store, "doc1")
	if doc.content != "Printer paper 12" || doc.mediaType != "document" || doc.filename != "receipt.pdf" || doc.url != "https://example.invalid/doc" {
		t.Errorf("document row = %+v", doc)
	}
	vid := readRow(t, store, "vid1")
	if vid.content != "trip" || vid.mediaType != "video" {
		t.Errorf("video row = %+v", vid)
	}
}

func TestStoreTextMessageUnchanged(t *testing.T) {
	store := newTestStore(t)
	storeExtracted(t, store, "txt1", &waProto.Message{Conversation: proto.String("hello")})
	if r := readRow(t, store, "txt1"); r.content != "hello" || r.mediaType != "" {
		t.Errorf("row = %+v, want text with empty media type", r)
	}
}

func TestStoreMediaWithoutCaptionKeepsEmptyContent(t *testing.T) {
	store := newTestStore(t)
	storeExtracted(t, store, "img2", imageMsg(""))
	if r := readRow(t, store, "img2"); r.content != "" || r.mediaType != "image" {
		t.Errorf("row = %+v, want empty content and image media type", r)
	}
}

func TestStoreMessageRedeliveryBackfillsCaption(t *testing.T) {
	store := newTestStore(t)
	storeExtracted(t, store, "img3", imageMsg(""))
	storeExtracted(t, store, "img3", imageMsg("now with caption"))

	var n int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM messages WHERE id = 'img3'").Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Errorf("row count = %d, want 1", n)
	}
	if r := readRow(t, store, "img3"); r.content != "now with caption" {
		t.Errorf("content = %q, want the caption", r.content)
	}
}
