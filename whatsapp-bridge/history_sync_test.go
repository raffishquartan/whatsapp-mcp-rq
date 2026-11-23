package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// storeText stores a plain text message in chat@g.us and returns whether the row already existed.
func storeText(t *testing.T, store *MessageStore, id string, ts time.Time, fromMe bool) bool {
	t.Helper()
	existed, err := store.StoreMessage(id, "chat@g.us", "sender", "text "+id, ts, fromMe,
		"", "", "", nil, nil, nil, 0)
	if err != nil {
		t.Fatalf("StoreMessage(%s): %v", id, err)
	}
	return existed
}

func countMessages(t *testing.T, store *MessageStore, chatJID string) int {
	t.Helper()
	var n int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM messages WHERE chat_jid = ?", chatJID).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestStoreChatNeverLowersLastMessageTime(t *testing.T) {
	store := newTestStore(t) // chat@g.us at 1700000000
	if err := store.StoreChat("chat@g.us", "Renamed", time.Unix(1600000000, 0)); err != nil {
		t.Fatalf("StoreChat: %v", err)
	}
	if got, want := storedChatTime(t, store, "chat@g.us"), "2023-11-14 22:13:20+00:00"; got != want {
		t.Errorf("last_message_time = %q, want %q (older chunk must not lower it)", got, want)
	}
	if got := rawText(t, store, "SELECT name FROM chats WHERE jid = 'chat@g.us'"); got != "Renamed" {
		t.Errorf("name = %q, want Renamed", got)
	}

	if err := store.StoreChat("chat@g.us", "Renamed", time.Unix(1800000000, 0)); err != nil {
		t.Fatalf("StoreChat: %v", err)
	}
	if got, want := storedChatTime(t, store, "chat@g.us"), "2027-01-15 08:00:00+00:00"; got != want {
		t.Errorf("last_message_time = %q, want %q (newer time must raise it)", got, want)
	}
}

func TestStoreChatInsertsNewChat(t *testing.T) {
	store := newTestStore(t)
	if err := store.StoreChat("new@g.us", "Chat A", time.Unix(1700000000, 0)); err != nil {
		t.Fatalf("StoreChat: %v", err)
	}
	if got, want := storedChatTime(t, store, "new@g.us"), "2023-11-14 22:13:20+00:00"; got != want {
		t.Errorf("last_message_time = %q, want %q", got, want)
	}
}

func TestStoreMessageReportsNewRow(t *testing.T) {
	store := newTestStore(t)
	if existed := storeText(t, store, "m1", time.Unix(1700000100, 0), false); existed {
		t.Error("existed = true for a new row")
	}
	if n := countMessages(t, store, "chat@g.us"); n != 1 {
		t.Errorf("row count = %d, want 1", n)
	}
}

func TestStoreMessageReportsExistingRowAndRewrites(t *testing.T) {
	store := newTestStore(t)
	storeText(t, store, "m1", time.Unix(1700000100, 0), false)
	existed, err := store.StoreMessage("m1", "chat@g.us", "sender", "rewritten", time.Unix(1700000100, 0), false,
		"", "", "", nil, nil, nil, 0)
	if err != nil {
		t.Fatalf("StoreMessage: %v", err)
	}
	if !existed {
		t.Error("existed = false for an existing row")
	}
	if got := rawText(t, store, "SELECT content FROM messages WHERE id = 'm1'"); got != "rewritten" {
		t.Errorf("content = %q, want rewritten", got)
	}
	if n := countMessages(t, store, "chat@g.us"); n != 1 {
		t.Errorf("row count = %d, want 1", n)
	}
}

func TestStoreMessageEmptyContentAndNoMediaIsNoOp(t *testing.T) {
	store := newTestStore(t)
	existed, err := store.StoreMessage("empty", "chat@g.us", "sender", "", time.Unix(1700000100, 0), false,
		"", "", "", nil, nil, nil, 0)
	if err != nil || existed {
		t.Errorf("StoreMessage = (%v, %v), want (false, nil)", existed, err)
	}
	if n := countMessages(t, store, "chat@g.us"); n != 0 {
		t.Errorf("row count = %d, want 0", n)
	}
}

func TestAnchorLookups(t *testing.T) {
	store := newTestStore(t)
	storeText(t, store, "mid", time.Unix(1700000200, 0), false)
	storeText(t, store, "old", time.Unix(1700000100, 0), true)
	storeText(t, store, "new", time.Unix(1700000300, 0), false)

	check := func(name string, got storedAnchor, err error, wantID string, wantFromMe bool, wantUnix int64) {
		t.Helper()
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if got.ID != wantID || got.IsFromMe != wantFromMe || got.Timestamp.Unix() != wantUnix {
			t.Errorf("%s = %+v, want id %s fromMe %v unix %d", name, got, wantID, wantFromMe, wantUnix)
		}
	}
	a, err := store.OldestMessage("chat@g.us")
	check("OldestMessage", a, err, "old", true, 1700000100)
	a, err = store.NewestMessage("chat@g.us")
	check("NewestMessage", a, err, "new", false, 1700000300)
	a, err = store.MessageByID("mid", "chat@g.us")
	check("MessageByID", a, err, "mid", false, 1700000200)
}

func TestAnchorLookupsNotFound(t *testing.T) {
	store := newTestStore(t)
	storeText(t, store, "m1", time.Unix(1700000100, 0), false)

	if _, err := store.OldestMessage("empty@g.us"); !errors.Is(err, errNoStoredMessage) {
		t.Errorf("OldestMessage err = %v, want errNoStoredMessage", err)
	}
	if _, err := store.NewestMessage("empty@g.us"); !errors.Is(err, errNoStoredMessage) {
		t.Errorf("NewestMessage err = %v, want errNoStoredMessage", err)
	}
	if _, err := store.MessageByID("missing", "chat@g.us"); !errors.Is(err, errNoStoredMessage) {
		t.Errorf("MessageByID err = %v, want errNoStoredMessage", err)
	}
	if _, err := store.MessageByID("m1", "other@g.us"); !errors.Is(err, errNoStoredMessage) {
		t.Errorf("MessageByID in another chat err = %v, want errNoStoredMessage", err)
	}
}

// --- history-sync handling ---

func historyMsg(id string, ts uint64, fromMe bool, msg *waE2E.Message) *waHistorySync.HistorySyncMsg {
	return &waHistorySync.HistorySyncMsg{Message: &waWeb.WebMessageInfo{
		Key:              &waCommon.MessageKey{ID: proto.String(id), FromMe: proto.Bool(fromMe)},
		Message:          msg,
		MessageTimestamp: proto.Uint64(ts),
	}}
}

func textMsg(text string) *waE2E.Message {
	return &waE2E.Message{Conversation: proto.String(text)}
}

func conversation(chatJID string, msgs ...*waHistorySync.HistorySyncMsg) *waHistorySync.Conversation {
	return &waHistorySync.Conversation{ID: proto.String(chatJID), Messages: msgs}
}

func historySyncEvent(syncType waHistorySync.HistorySync_HistorySyncType, originalID string,
	convs ...*waHistorySync.Conversation) *events.HistorySync {
	return &events.HistorySync{
		Data:         &waHistorySync.HistorySync{SyncType: syncType.Enum(), Conversations: convs},
		Notification: &waE2E.HistorySyncNotification{OriginalMessageID: proto.String(originalID)},
	}
}

func fixedName(types.JID, string, *waHistorySync.Conversation) string { return "Chat A" }

func storeSync(store *MessageStore, hs *events.HistorySync) []conversationResult {
	return storeHistorySync(store, hs, fixedName, "own", waLog.Noop)
}

func TestStoreHistorySyncReportsPerConversationResults(t *testing.T) {
	store := newTestStore(t)
	storeText(t, store, "known", time.Unix(1700000300, 0), false)

	hs := historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "",
		conversation("chat@g.us",
			historyMsg("known", 1700000300, false, textMsg("known again")),
			historyMsg("img", 1700000200, true, imageMsg("Coffee 4.50")),
			nil,
			&waHistorySync.HistorySyncMsg{},
			historyMsg("empty", 1700000150, false, &waE2E.Message{}),
			historyMsg("oldest-text", 1700000100, false, textMsg("hi")),
			historyMsg("oldest-empty", 1700000050, false, &waE2E.Message{}),
		),
		conversation("other@g.us", historyMsg("o1", 1700000400, false, textMsg("x"))),
	)
	results := storeSync(store, hs)
	if len(results) != 2 {
		t.Fatalf("got %d results, want 2: %+v", len(results), results)
	}
	got := results[0]
	want := conversationResult{
		ChatJID: "chat@g.us", Received: 5, StoredNew: 2, StoredExisting: 1, Skipped: 2,
		OldestTimestamp: time.Unix(1700000050, 0).UTC(), OldestStoredID: "oldest-text",
	}
	if got.ChatJID != want.ChatJID || got.Received != want.Received || got.StoredNew != want.StoredNew ||
		got.StoredExisting != want.StoredExisting || got.Skipped != want.Skipped ||
		!got.OldestTimestamp.Equal(want.OldestTimestamp) || got.OldestStoredID != want.OldestStoredID {
		t.Errorf("result = %+v, want %+v", got, want)
	}
	if n := countMessages(t, store, "chat@g.us"); n != 3 {
		t.Errorf("stored rows = %d, want 3", n)
	}
	if got := rawText(t, store, "SELECT sender FROM messages WHERE id = 'img'"); got != "own" {
		t.Errorf("from-me sender = %q, want own", got)
	}
	if got := rawText(t, store, "SELECT content FROM messages WHERE id = 'img'"); got != "Coffee 4.50" {
		t.Errorf("caption = %q", got)
	}
	// The older chunk must not lower the chat's last_message_time (set to 1700000300 via the chat row).
	if err := store.StoreChat("chat@g.us", "Chat A", time.Unix(1700000500, 0)); err != nil {
		t.Fatal(err)
	}
	storeSync(store, hs)
	if got, want := storedChatTime(t, store, "chat@g.us"), "2023-11-14 22:21:40+00:00"; got != want {
		t.Errorf("last_message_time = %q, want %q", got, want)
	}
}

func TestStoreHistorySyncRepeatReportsExisting(t *testing.T) {
	store := newTestStore(t)
	hs := historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "",
		conversation("chat@g.us",
			historyMsg("a", 1700000200, false, textMsg("a")),
			historyMsg("b", 1700000100, false, textMsg("b"))))
	storeSync(store, hs)
	r := storeSync(store, hs)[0]
	if r.StoredNew != 0 || r.StoredExisting != 2 {
		t.Errorf("repeat result = %+v, want 0 new 2 existing", r)
	}
	if n := countMessages(t, store, "chat@g.us"); n != 2 {
		t.Errorf("row count = %d, want 2", n)
	}
}

// --- pending-request registry ---

func onDemandResult(chatJID string) []conversationResult {
	return []conversationResult{{ChatJID: chatJID, Received: 3, StoredNew: 3}}
}

func beginWithID(t *testing.T, reg *historyRequestRegistry, chatJID, requestID string) *pendingHistoryRequest {
	t.Helper()
	p, ok := reg.begin(chatJID)
	if !ok {
		t.Fatal("begin: registry busy")
	}
	reg.setRequestID(p, requestID)
	return p
}

func expectCompleted(t *testing.T, p *pendingHistoryRequest, wantChat string) {
	t.Helper()
	select {
	case a := <-p.done:
		if a.Result.ChatJID != wantChat || !a.ChatFound {
			t.Errorf("completed with %+v, want chat %s found", a, wantChat)
		}
	default:
		t.Error("request not completed")
	}
}

func expectPending(t *testing.T, p *pendingHistoryRequest) {
	t.Helper()
	select {
	case a := <-p.done:
		t.Errorf("request completed unexpectedly with %+v", a)
	default:
	}
}

func TestRegistryCompletesOnOriginalMessageID(t *testing.T) {
	reg := &historyRequestRegistry{}
	p := beginWithID(t, reg, "chat@g.us", "REQ1")
	hs := historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "REQ1")
	reg.offer(hs, []conversationResult{{ChatJID: "chat@g.us"}})
	select {
	case a := <-p.done:
		if a.Result.ChatJID != "chat@g.us" || a.Result.Received != 0 || !a.ChatFound {
			t.Errorf("start-of-chat answer = %+v, want empty found result for chat@g.us", a)
		}
	default:
		t.Fatal("request not completed by matching original message id")
	}
	if _, ok := reg.begin("chat@g.us"); !ok {
		t.Error("registry still busy after completion")
	}
}

func TestRegistryFallsBackToChatJID(t *testing.T) {
	reg := &historyRequestRegistry{}
	p := beginWithID(t, reg, "chat@g.us", "REQ1")
	reg.offer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, ""), onDemandResult("chat@g.us"))
	expectCompleted(t, p, "chat@g.us")
}

func TestRegistryIgnoresNonMatchingSync(t *testing.T) {
	reg := &historyRequestRegistry{}
	p := beginWithID(t, reg, "chat@g.us", "REQ1")
	reg.offer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "OTHER"), onDemandResult("chat@g.us"))
	reg.offer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, ""), onDemandResult("other@g.us"))
	expectPending(t, p)
	if _, ok := reg.begin("x@g.us"); ok {
		t.Error("registry not busy while request pending")
	}
}

func TestRegistryIgnoresNonOnDemandSync(t *testing.T) {
	reg := &historyRequestRegistry{}
	p := beginWithID(t, reg, "chat@g.us", "REQ1")
	reg.offer(historySyncEvent(waHistorySync.HistorySync_RECENT, "REQ1"), onDemandResult("chat@g.us"))
	expectPending(t, p)
}

func TestRegistryOfferWithoutPendingIsHarmless(t *testing.T) {
	reg := &historyRequestRegistry{}
	reg.offer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "REQ1"), onDemandResult("chat@g.us"))
	if _, ok := reg.begin("chat@g.us"); !ok {
		t.Error("registry busy after unsolicited sync")
	}
}

// --- POST /api/request_history_sync ---

type fakeSender struct {
	connected bool
	loggedIn  bool
	sendErr   error
	respID    string

	mu     sync.Mutex
	sent   []*waE2E.Message
	sentCh chan struct{}
}

func newFakeSender() *fakeSender {
	return &fakeSender{connected: true, loggedIn: true, respID: "REQ1", sentCh: make(chan struct{}, 10)}
}

func (f *fakeSender) IsConnected() bool { return f.connected }
func (f *fakeSender) HasDeviceID() bool { return f.loggedIn }

// BuildHistorySyncRequest uses whatsmeow's real builder, which reads no client state.
func (f *fakeSender) BuildHistorySyncRequest(info *types.MessageInfo, count int) *waE2E.Message {
	return (&whatsmeow.Client{}).BuildHistorySyncRequest(info, count)
}

func (f *fakeSender) SendPeerMessage(_ context.Context, msg *waE2E.Message) (whatsmeow.SendResponse, error) {
	f.mu.Lock()
	f.sent = append(f.sent, msg)
	f.mu.Unlock()
	f.sentCh <- struct{}{}
	if f.sendErr != nil {
		return whatsmeow.SendResponse{}, f.sendErr
	}
	return whatsmeow.SendResponse{ID: f.respID}, nil
}

func (f *fakeSender) sentCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.sent)
}

func (f *fakeSender) waitSent(t *testing.T) *waE2E.Message {
	t.Helper()
	select {
	case <-f.sentCh:
	case <-time.After(5 * time.Second):
		t.Fatal("no request was sent")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.sent[len(f.sent)-1]
}

type historyFixture struct {
	sender  *fakeSender
	store   *MessageStore
	reg     *historyRequestRegistry
	handler http.Handler
}

func newHistoryFixture(t *testing.T, timeout time.Duration) *historyFixture {
	t.Helper()
	f := &historyFixture{sender: newFakeSender(), store: newTestStore(t), reg: &historyRequestRegistry{}}
	f.handler = newHistorySyncHandler(f.sender, f.store, f.reg, timeout)
	return f
}

func (f *historyFixture) do(method, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, "/api/request_history_sync", strings.NewReader(body))
	rec := httptest.NewRecorder()
	f.handler.ServeHTTP(rec, req)
	return rec
}

// doAsync runs a POST in the background; the returned channel yields the response.
func (f *historyFixture) doAsync(body string) <-chan *httptest.ResponseRecorder {
	ch := make(chan *httptest.ResponseRecorder, 1)
	go func() { ch <- f.do(http.MethodPost, body) }()
	return ch
}

// answer delivers a history sync through the same path handleHistorySync uses.
func (f *historyFixture) answer(hs *events.HistorySync) {
	f.reg.offer(hs, storeSync(f.store, hs))
}

func waitResponse(t *testing.T, ch <-chan *httptest.ResponseRecorder) *httptest.ResponseRecorder {
	t.Helper()
	select {
	case rec := <-ch:
		return rec
	case <-time.After(5 * time.Second):
		t.Fatal("handler did not respond")
		return nil
	}
}

func decodeBody(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("response is not JSON: %q: %v", rec.Body.String(), err)
	}
	return body
}

func expectError(t *testing.T, rec *httptest.ResponseRecorder, wantStatus int, wantInMessage string) map[string]any {
	t.Helper()
	if rec.Code != wantStatus {
		t.Errorf("status = %d, want %d (body %s)", rec.Code, wantStatus, rec.Body.String())
	}
	body := decodeBody(t, rec)
	if body["success"] != false {
		t.Errorf("success = %v, want false", body["success"])
	}
	if msg, _ := body["message"].(string); !strings.Contains(msg, wantInMessage) {
		t.Errorf("message = %q, want it to contain %q", msg, wantInMessage)
	}
	return body
}

func TestHistoryEndpointValidation(t *testing.T) {
	tests := []struct {
		name, method, body string
		status             int
		inMessage          string
	}{
		{"GET not allowed", http.MethodGet, "", http.StatusMethodNotAllowed, "method"},
		{"PUT not allowed", http.MethodPut, `{"chat_jid":"chat@g.us"}`, http.StatusMethodNotAllowed, "method"},
		{"malformed JSON", http.MethodPost, `{"chat_jid":`, http.StatusBadRequest, "invalid JSON"},
		{"empty body", http.MethodPost, ``, http.StatusBadRequest, "invalid JSON"},
		{"missing chat_jid", http.MethodPost, `{}`, http.StatusBadRequest, "chat_jid is required"},
		{"empty chat_jid", http.MethodPost, `{"chat_jid":""}`, http.StatusBadRequest, "chat_jid is required"},
		{"chat_jid without server", http.MethodPost, `{"chat_jid":"not-a-jid"}`, http.StatusBadRequest, "invalid chat_jid"},
		{"chat_jid with bad device", http.MethodPost, `{"chat_jid":"1.2.3@s.whatsapp.net"}`, http.StatusBadRequest, "invalid chat_jid"},
		{"status broadcast", http.MethodPost, `{"chat_jid":"status@broadcast"}`, http.StatusBadRequest, "broadcast"},
		{"broadcast list", http.MethodPost, `{"chat_jid":"123@broadcast"}`, http.StatusBadRequest, "broadcast"},
		{"count zero", http.MethodPost, `{"chat_jid":"chat@g.us","count":0}`, http.StatusBadRequest, "count"},
		{"count 101", http.MethodPost, `{"chat_jid":"chat@g.us","count":101}`, http.StatusBadRequest, "count"},
		{"no stored messages", http.MethodPost, `{"chat_jid":"chat@g.us"}`, http.StatusNotFound, "no stored message"},
		{"unknown anchor", http.MethodPost, `{"chat_jid":"chat@g.us","before_message_id":"missing"}`, http.StatusNotFound, "missing"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f := newHistoryFixture(t, time.Second)
			expectError(t, f.do(tt.method, tt.body), tt.status, tt.inMessage)
			if n := f.sender.sentCount(); n != 0 {
				t.Errorf("sent %d requests, want 0", n)
			}
		})
	}
}

func TestHistoryEndpointUnknownAnchorWithOtherMessagesStored(t *testing.T) {
	f := newHistoryFixture(t, time.Second)
	storeText(t, f.store, "m1", time.Unix(1700000100, 0), false)
	expectError(t, f.do(http.MethodPost, `{"chat_jid":"chat@g.us","before_message_id":"missing"}`), http.StatusNotFound, "missing")
	if n := f.sender.sentCount(); n != 0 {
		t.Errorf("sent %d requests, want 0", n)
	}
}

func TestHistoryEndpointNotConnected(t *testing.T) {
	f := newHistoryFixture(t, time.Second)
	storeText(t, f.store, "m1", time.Unix(1700000100, 0), false)
	f.sender.connected = false
	expectError(t, f.do(http.MethodPost, `{"chat_jid":"chat@g.us"}`), http.StatusServiceUnavailable, "not connected")
	if n := f.sender.sentCount(); n != 0 {
		t.Errorf("sent %d requests, want 0", n)
	}
}

func TestHistoryEndpointNotLoggedIn(t *testing.T) {
	f := newHistoryFixture(t, time.Second)
	storeText(t, f.store, "m1", time.Unix(1700000100, 0), false)
	f.sender.loggedIn = false
	expectError(t, f.do(http.MethodPost, `{"chat_jid":"chat@g.us"}`), http.StatusServiceUnavailable, "not logged in")
	if n := f.sender.sentCount(); n != 0 {
		t.Errorf("sent %d requests, want 0", n)
	}
}

func TestHistoryEndpointSecondRequestWhilePending(t *testing.T) {
	f := newHistoryFixture(t, 5*time.Second)
	storeText(t, f.store, "m1", time.Unix(1700000100, 0), false)
	first := f.doAsync(`{"chat_jid":"chat@g.us"}`)
	f.sender.waitSent(t)

	expectError(t, f.do(http.MethodPost, `{"chat_jid":"chat@g.us"}`), http.StatusConflict, "already in flight")
	if n := f.sender.sentCount(); n != 1 {
		t.Errorf("sent %d requests, want 1", n)
	}

	f.answer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "REQ1", conversation("chat@g.us")))
	if rec := waitResponse(t, first); rec.Code != http.StatusOK {
		t.Errorf("first request status = %d, want 200", rec.Code)
	}
}

func TestHistoryEndpointSendFailure(t *testing.T) {
	f := newHistoryFixture(t, time.Second)
	storeText(t, f.store, "m1", time.Unix(1700000100, 0), false)
	f.sender.sendErr = errors.New("server said no")
	expectError(t, f.do(http.MethodPost, `{"chat_jid":"chat@g.us"}`), http.StatusBadGateway, "server said no")
	if _, ok := f.reg.begin("chat@g.us"); !ok {
		t.Error("registry still busy after send failure")
	}
}

func TestHistoryEndpointTimeoutThenLateAnswerStored(t *testing.T) {
	f := newHistoryFixture(t, 20*time.Millisecond)
	storeText(t, f.store, "m1", time.Unix(1700000100, 0), false)
	body := expectError(t, f.do(http.MethodPost, `{"chat_jid":"chat@g.us"}`), http.StatusGatewayTimeout, "REQ1")
	if body["request_id"] != "REQ1" {
		t.Errorf("request_id = %v, want REQ1", body["request_id"])
	}
	probe, ok := f.reg.begin("chat@g.us")
	if !ok {
		t.Fatal("registry still busy after timeout")
	}
	f.reg.finish(probe)

	f.answer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "REQ1",
		conversation("chat@g.us", historyMsg("late", 1700000050, false, textMsg("late")))))
	if _, err := f.store.MessageByID("late", "chat@g.us"); err != nil {
		t.Errorf("late answer not stored: %v", err)
	}
}

func onDemandRequest(t *testing.T, msg *waE2E.Message) *waE2E.PeerDataOperationRequestMessage_HistorySyncOnDemandRequest {
	t.Helper()
	pm := msg.GetProtocolMessage()
	if pm.GetType() != waE2E.ProtocolMessage_PEER_DATA_OPERATION_REQUEST_MESSAGE {
		t.Fatalf("sent message type = %v", pm.GetType())
	}
	req := pm.GetPeerDataOperationRequestMessage().GetHistorySyncOnDemandRequest()
	if req == nil {
		t.Fatal("sent message has no on-demand history request")
	}
	return req
}

func TestHistoryEndpointSuccess(t *testing.T) {
	f := newHistoryFixture(t, 5*time.Second)
	storeText(t, f.store, "newer", time.Unix(1700000300, 0), false)
	storeText(t, f.store, "anchor", time.Unix(1700000200, 0), true)

	pending := f.doAsync(`{"chat_jid":"chat@g.us","count":30}`)
	req := onDemandRequest(t, f.sender.waitSent(t))
	if req.GetChatJID() != "chat@g.us" || req.GetOldestMsgID() != "anchor" || !req.GetOldestMsgFromMe() ||
		req.GetOldestMsgTimestampMS() != 1700000200 || req.GetOnDemandMsgCount() != 30 {
		t.Errorf("request = %v, want anchor on the oldest stored message with count 30", req)
	}

	hs := historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "REQ1",
		conversation("chat@g.us",
			historyMsg("p2", 1700000150, false, textMsg("Coffee 4.50")),
			historyMsg("p1", 1700000100, false, textMsg("Tea 2.10")),
			historyMsg("p0", 1700000090, false, &waE2E.Message{})))
	f.answer(hs)

	rec := waitResponse(t, pending)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body %s", rec.Code, rec.Body.String())
	}
	body := decodeBody(t, rec)
	want := map[string]any{
		"success": true, "request_id": "REQ1", "received": 3.0, "stored_new": 2.0, "stored_existing": 0.0,
		"skipped": 1.0, "oldest_timestamp": "2023-11-14T22:14:50Z", "next_before_message_id": "p1",
	}
	for k, v := range want {
		if body[k] != v {
			t.Errorf("%s = %#v, want %#v", k, body[k], v)
		}
	}

	// Repeating the same page reports every stored row as existing.
	again := f.doAsync(`{"chat_jid":"chat@g.us","before_message_id":"anchor","count":30}`)
	if r := onDemandRequest(t, f.sender.waitSent(t)); r.GetOldestMsgID() != "anchor" {
		t.Errorf("repeat anchor = %q, want anchor", r.GetOldestMsgID())
	}
	f.answer(hs)
	body = decodeBody(t, waitResponse(t, again))
	if body["stored_new"] != 0.0 || body["stored_existing"] != 2.0 {
		t.Errorf("repeat = %v, want stored_new 0 and stored_existing 2", body)
	}
	if n := countMessages(t, f.store, "chat@g.us"); n != 4 {
		t.Errorf("row count = %d, want 4", n)
	}
}

func TestHistoryEndpointAnchorOnGivenMessageAndDefaultCount(t *testing.T) {
	f := newHistoryFixture(t, 5*time.Second)
	storeText(t, f.store, "old", time.Unix(1700000100, 0), false)
	storeText(t, f.store, "given", time.Unix(1700000300, 0), false)

	pending := f.doAsync(`{"chat_jid":"chat@g.us","before_message_id":"given"}`)
	req := onDemandRequest(t, f.sender.waitSent(t))
	if req.GetOldestMsgID() != "given" || req.GetOldestMsgFromMe() || req.GetOldestMsgTimestampMS() != 1700000300 ||
		req.GetOnDemandMsgCount() != 50 {
		t.Errorf("request = %v, want anchor on the given message with default count 50", req)
	}
	f.answer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "REQ1", conversation("chat@g.us")))
	waitResponse(t, pending)
}

func TestHistoryEndpointStartOfChat(t *testing.T) {
	f := newHistoryFixture(t, 5*time.Second)
	storeText(t, f.store, "first", time.Unix(1700000100, 0), false)
	pending := f.doAsync(`{"chat_jid":"chat@g.us"}`)
	f.sender.waitSent(t)
	f.answer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "REQ1", conversation("chat@g.us")))

	rec := waitResponse(t, pending)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body %s", rec.Code, rec.Body.String())
	}
	body := decodeBody(t, rec)
	if body["received"] != 0.0 || body["next_before_message_id"] != nil || body["oldest_timestamp"] != nil {
		t.Errorf("start of chat = %v, want received 0 and null next id and oldest timestamp", body)
	}
	if _, present := body["next_before_message_id"]; !present {
		t.Error("next_before_message_id missing, want explicit null")
	}
}

// --- review fixes ---

func TestStoreHistorySyncEmitsResultForEveryConversation(t *testing.T) {
	store := newTestStore(t)
	untimed := historyMsg("untimed", 0, false, textMsg("no time"))
	untimed.Message.MessageTimestamp = nil
	hs := historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "",
		// messages[0] is nil: the rest must still be stored and the chat time is the newest timestamp.
		conversation("chat@g.us", nil,
			historyMsg("b", 1700000100, false, textMsg("b")),
			historyMsg("a", 1700000600, false, textMsg("a"))),
		// messages[0] has no timestamp.
		conversation("second@g.us", untimed, historyMsg("c", 1700000200, false, textMsg("c"))),
		// No messages at all (start of chat).
		conversation("empty@g.us"),
	)
	results := storeSync(store, hs)
	if len(results) != 3 {
		t.Fatalf("got %d results, want 3: %+v", len(results), results)
	}
	if r := results[0]; r.ChatJID != "chat@g.us" || r.Received != 2 || r.StoredNew != 2 || r.OldestStoredID != "b" {
		t.Errorf("chat@g.us result = %+v, want 2 received, 2 new, oldest b", r)
	}
	if got, want := storedChatTime(t, store, "chat@g.us"), "2023-11-14 22:23:20+00:00"; got != want {
		t.Errorf("last_message_time = %q, want %q (newest message in the answer)", got, want)
	}
	if r := results[1]; r.ChatJID != "second@g.us" || r.Received != 2 || r.Skipped != 1 || r.StoredNew != 1 || r.OldestStoredID != "c" {
		t.Errorf("second@g.us result = %+v, want 2 received, 1 skipped, 1 new, oldest c", r)
	}
	if r := results[2]; r.ChatJID != "empty@g.us" || r.Received != 0 || r.OldestStoredID != "" {
		t.Errorf("empty@g.us result = %+v, want empty result", r)
	}
	if n := countMessages(t, store, "empty@g.us"); n != 0 {
		t.Errorf("empty@g.us rows = %d, want 0", n)
	}
}

func TestStoreHistorySyncSenderMatchesLiveRepresentation(t *testing.T) {
	store := newTestStore(t)
	withParticipant := func(id, participant string) *waHistorySync.HistorySyncMsg {
		m := historyMsg(id, 1700000100, false, textMsg(id))
		m.Message.Key.Participant = proto.String(participant)
		return m
	}
	storeSync(store, historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "",
		conversation("chat@g.us",
			withParticipant("pn", "111:2@s.whatsapp.net"),
			withParticipant("lid", "222@lid")),
		conversation("333@s.whatsapp.net", historyMsg("dm", 1700000100, false, textMsg("dm")))))
	for id, want := range map[string]string{"pn": "111", "lid": "222", "dm": "333"} {
		if got := rawText(t, store, "SELECT sender FROM messages WHERE id = ?", id); got != want {
			t.Errorf("sender of %s = %q, want %q (live handler stores Info.Sender.User)", id, got, want)
		}
	}
}

func TestSenderIDIsUserPart(t *testing.T) {
	if got := senderID(types.NewJID("111", types.DefaultUserServer)); got != "111" {
		t.Errorf("senderID = %q, want 111", got)
	}
}

func TestStoreMessageConcurrentWritesDoNotFail(t *testing.T) {
	store := newTestStore(t)
	const writers = 16
	var wg sync.WaitGroup
	errs := make(chan error, writers*10)
	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < 10; i++ {
				// Half the writes rewrite one shared row, so read-then-write transactions overlap.
				id := "shared"
				if i%2 == 0 {
					id = fmt.Sprintf("w%d-%d", w, i)
				}
				if _, err := store.StoreMessage(id, "chat@g.us", "s", "x", time.Unix(1700000100, 0), false,
					"", "", "", nil, nil, nil, 0); err != nil {
					errs <- err
				}
			}
		}(w)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Errorf("concurrent StoreMessage: %v", err)
	}
}

func TestRegistryIDMatchWithoutChatConversationIsFlagged(t *testing.T) {
	reg := &historyRequestRegistry{}
	p := beginWithID(t, reg, "111@lid", "REQ1")
	reg.offer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "REQ1"), onDemandResult("111@s.whatsapp.net"))
	select {
	case a := <-p.done:
		if a.ChatFound {
			t.Errorf("answer = %+v, want ChatFound false", a)
		}
		if len(a.Conversations) != 1 || a.Conversations[0] != "111@s.whatsapp.net" {
			t.Errorf("conversations = %v, want [111@s.whatsapp.net]", a.Conversations)
		}
	default:
		t.Fatal("request not completed by matching original message id")
	}
}

func TestRegistryLateAnswerToEarlierRequestIgnoredBeforeRequestIDKnown(t *testing.T) {
	reg := &historyRequestRegistry{}
	p, ok := reg.begin("chat@g.us")
	if !ok {
		t.Fatal("begin: registry busy")
	}
	// A late answer to an earlier request for the same chat arrives while the send is in progress.
	reg.offer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "OLD"), onDemandResult("chat@g.us"))
	expectPending(t, p)
	reg.setRequestID(p, "REQ1")
	expectPending(t, p)
	reg.offer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "REQ1"), onDemandResult("chat@g.us"))
	expectCompleted(t, p, "chat@g.us")
}

func TestRegistryAnswerBeforeRequestIDKnownCompletesOnSetRequestID(t *testing.T) {
	reg := &historyRequestRegistry{}
	p, ok := reg.begin("chat@g.us")
	if !ok {
		t.Fatal("begin: registry busy")
	}
	reg.offer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "REQ1"), onDemandResult("chat@g.us"))
	expectPending(t, p)
	reg.setRequestID(p, "REQ1")
	expectCompleted(t, p, "chat@g.us")
	if _, ok := reg.begin("chat@g.us"); !ok {
		t.Error("registry still busy after completion")
	}
}

func TestHistoryEndpointAnswerWithoutChatConversation(t *testing.T) {
	f := newHistoryFixture(t, 5*time.Second)
	storeText(t, f.store, "m1", time.Unix(1700000100, 0), false)
	pending := f.doAsync(`{"chat_jid":"chat@g.us"}`)
	f.sender.waitSent(t)
	f.answer(historySyncEvent(waHistorySync.HistorySync_ON_DEMAND, "REQ1",
		conversation("other@g.us", historyMsg("o1", 1700000050, false, textMsg("o1")))))

	body := expectError(t, waitResponse(t, pending), http.StatusBadGateway, "other@g.us")
	if body["reason"] != historyReasonChatNotInAnswer || body["request_id"] != "REQ1" {
		t.Errorf("body = %v, want reason %s and request_id REQ1", body, historyReasonChatNotInAnswer)
	}
	if _, ok := f.reg.begin("chat@g.us"); !ok {
		t.Error("registry still busy")
	}
}

func TestHistoryEndpointAnchorLookupFailure(t *testing.T) {
	f := newHistoryFixture(t, time.Second)
	f.store.Close()
	expectError(t, f.do(http.MethodPost, `{"chat_jid":"chat@g.us"}`), http.StatusInternalServerError, "failed to read anchor")
	if n := f.sender.sentCount(); n != 0 {
		t.Errorf("sent %d requests, want 0", n)
	}
}

func TestHistoryEndpointClientGoneAway(t *testing.T) {
	f := newHistoryFixture(t, 5*time.Second)
	storeText(t, f.store, "m1", time.Unix(1700000100, 0), false)
	ctx, cancel := context.WithCancel(context.Background())
	ch := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		req := httptest.NewRequest(http.MethodPost, "/api/request_history_sync", strings.NewReader(`{"chat_jid":"chat@g.us"}`)).WithContext(ctx)
		rec := httptest.NewRecorder()
		f.handler.ServeHTTP(rec, req)
		ch <- rec
	}()
	f.sender.waitSent(t)
	cancel()
	expectError(t, waitResponse(t, ch), http.StatusGatewayTimeout, "client went away")
	if _, ok := f.reg.begin("chat@g.us"); !ok {
		t.Error("registry still busy after the client went away")
	}
}
