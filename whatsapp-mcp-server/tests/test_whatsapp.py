from datetime import datetime

import whatsapp


def test_list_chats_returns_chat_dataclasses(fixture_db):
    chats = whatsapp.list_chats(query="Test Group")

    assert len(chats) == 1
    chat = chats[0]
    assert isinstance(chat, whatsapp.Chat)
    assert chat.jid == "111111111@g.us"
    assert chat.last_message == "Hello group"


def test_list_chats_include_last_message_false_still_matches(fixture_db):
    """Regression test: the SELECT used to reference messages.* columns even
    when the messages join was skipped, raising a sqlite3.OperationalError
    that was silently swallowed and returned an empty list."""
    chats = whatsapp.list_chats(query="Test Group", include_last_message=False)

    assert len(chats) == 1
    chat = chats[0]
    assert chat.jid == "111111111@g.us"
    assert chat.last_message is None
    assert chat.last_sender is None
    assert chat.last_is_from_me is None


def test_list_chats_no_query_include_last_message_false_returns_all(fixture_db):
    chats = whatsapp.list_chats(include_last_message=False)

    assert {c.jid for c in chats} == {
        "111111111@g.us",
        "222222222@s.whatsapp.net",
        "333333333@s.whatsapp.net",
    }


def test_get_chat_include_last_message_true(fixture_db):
    chat = whatsapp.get_chat("111111111@g.us", include_last_message=True)

    assert isinstance(chat, whatsapp.Chat)
    assert chat.last_message == "Hello group"


def test_get_chat_include_last_message_false(fixture_db):
    """Regression test: same swallowed-SQL-error bug as list_chats."""
    chat = whatsapp.get_chat("111111111@g.us", include_last_message=False)

    assert isinstance(chat, whatsapp.Chat)
    assert chat.jid == "111111111@g.us"
    assert chat.last_message is None


def test_get_chat_not_found_returns_none(fixture_db):
    assert whatsapp.get_chat("doesnotexist@g.us") is None


def test_list_messages_returns_message_dataclasses(fixture_db):
    messages = whatsapp.list_messages(
        chat_jid="333333333@s.whatsapp.net", include_context=False
    )

    assert isinstance(messages, list)
    assert len(messages) == 1
    message = messages[0]
    assert isinstance(message, whatsapp.Message)
    assert message.content == "Hi Bob"
    assert isinstance(message.timestamp, datetime)


def test_list_messages_empty_result_returns_empty_list_not_string(fixture_db):
    """Regression test: list_messages used to always format results through
    format_messages_list, which returned the literal string
    'No messages to display.' instead of an empty list."""
    messages = whatsapp.list_messages(query="no-such-content-anywhere", include_context=False)

    assert messages == []


def test_list_messages_with_context_returns_list(fixture_db):
    messages = whatsapp.list_messages(
        chat_jid="333333333@s.whatsapp.net", include_context=True
    )

    assert isinstance(messages, list)
    assert all(isinstance(m, whatsapp.Message) for m in messages)
