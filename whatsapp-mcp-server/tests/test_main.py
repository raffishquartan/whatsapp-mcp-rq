import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402


async def run_tool(name, **kwargs):
    tool = main.mcp._tool_manager.get_tool(name)
    return await tool.run(kwargs, convert_result=True)


@pytest.mark.parametrize(
    "tool_name,kwargs",
    [
        ("list_chats", {"query": "Test Group"}),
        ("list_chats", {"query": "Test Group", "include_last_message": False}),
        ("list_chats", {"query": "no-match-at-all"}),
        ("list_messages", {"chat_jid": "333333333@s.whatsapp.net", "include_context": False}),
        ("list_messages", {"query": "no-such-content-anywhere", "include_context": False}),
        ("search_contacts", {"query": "Bob"}),
        ("search_contacts", {"query": "no-match-at-all"}),
        ("get_chat", {"chat_jid": "111111111@g.us"}),
        ("get_chat", {"chat_jid": "111111111@g.us", "include_last_message": False}),
        ("get_chat", {"chat_jid": "doesnotexist@g.us"}),
        ("get_direct_chat_by_contact", {"sender_phone_number": "333333333"}),
        ("get_direct_chat_by_contact", {"sender_phone_number": "doesnotexist"}),
        ("get_contact_chats", {"jid": "333333333@s.whatsapp.net"}),
        ("get_last_interaction", {"jid": "333333333@s.whatsapp.net"}),
        ("get_last_interaction", {"jid": "doesnotexist@s.whatsapp.net"}),
    ],
)
async def test_tool_output_passes_validation(fixture_db, tool_name, kwargs):
    """Regression test for the output-validation bug: each of these tools used
    to raise a Pydantic ToolError because it returned a whatsapp.py dataclass
    (or, for list_messages, a formatted display string) instead of a plain
    dict/list/str/None matching its declared output schema."""
    unstructured_content, structured_content = await run_tool(tool_name, **kwargs)


async def test_get_message_context_output_passes_validation(fixture_db):
    unstructured_content, structured_content = await run_tool(
        "get_message_context", message_id="msg2"
    )
    assert structured_content["result"]["message"]["content"] == "Hi Bob"


async def test_list_chats_include_last_message_false_returns_real_match(fixture_db):
    """Regression test: include_last_message=False used to silently return []
    for every query because of a swallowed sqlite3.OperationalError."""
    _, structured_content = await run_tool(
        "list_chats", query="Test Group", include_last_message=False
    )

    assert len(structured_content["result"]) == 1
    assert structured_content["result"][0]["jid"] == "111111111@g.us"


async def test_list_messages_empty_result_is_empty_list(fixture_db):
    _, structured_content = await run_tool(
        "list_messages", query="no-such-content-anywhere", include_context=False
    )

    assert structured_content["result"] == []


async def test_get_chat_not_found_returns_none(fixture_db):
    _, structured_content = await run_tool("get_chat", chat_jid="doesnotexist@g.us")

    assert structured_content["result"] is None
