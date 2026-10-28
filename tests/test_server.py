from fastmcp import Client

from chatwoot_mcp.server import INSTRUCTIONS, build_server
from tests.conftest import tool_names

READ_TOOLS = {
    "get_my_profile",
    "list_conversations",
    "search_conversations",
    "get_conversation",
    "get_messages",
    "get_conversation_counts",
    "search_contacts",
    "get_contact",
    "get_contact_conversations",
    "list_inboxes",
    "list_agents",
    "list_teams",
    "list_labels",
    "list_canned_responses",
    "get_report_summary",
    "list_contacts",
    "get_contact_labels",
    "get_conversation_labels",
    "get_inbox",
    "list_team_members",
    "list_custom_attribute_definitions",
    "get_report_by_group",
    "get_first_response_distribution",
    "get_account",
    "filter_conversations",
    "list_conversation_attachments",
    "get_contact_notes",
    "search_help_articles",
    "list_macros",
    "list_csat_responses",
}
WRITE_TOOLS = {
    "add_private_note",
    "add_conversation_labels",
    "add_contact_labels",
    "add_contact_note",
    "remove_conversation_labels",
}
ACTION_TOOLS = {
    "send_reply",
    "assign_conversation",
    "set_conversation_status",
    "set_conversation_priority",
    "create_conversation",
    "create_contact",
    "update_contact",
    "set_conversation_custom_attributes",
    "run_macro",
}


async def test_by_default_only_the_read_tools_exist(make_client):
    assert await tool_names(make_client()) == READ_TOOLS


async def test_the_write_tools_exist_only_when_switched_on(make_client):
    assert await tool_names(make_client(CHATWOOT_MCP_ENABLE_WRITES="true")) == READ_TOOLS | WRITE_TOOLS


async def test_with_both_switches_on_all_forty_four_tools_exist(make_client):
    names = await tool_names(make_client(CHATWOOT_MCP_ENABLE_WRITES="true", CHATWOOT_MCP_ENABLE_ACTIONS="true"))
    assert names == READ_TOOLS | WRITE_TOOLS | ACTION_TOOLS and len(names) == 44


async def test_every_tool_has_all_four_hints_and_a_tier_and_module_tag(make_client):
    async with make_client(CHATWOOT_MCP_ENABLE_WRITES="true", CHATWOOT_MCP_ENABLE_ACTIONS="true") as client:
        tools = await client.list_tools()
    assert len(tools) == 44
    for tool in tools:
        hints = tool.annotations
        assert None not in (hints.readOnlyHint, hints.destructiveHint, hints.idempotentHint, hints.openWorldHint), (
            tool.name
        )
        tags = set(tool.meta["fastmcp"]["tags"])
        tier = {t for t in tags if t.startswith("tier:")}
        assert len(tier) == 1 and any(t.startswith("module:") for t in tags), tool.name
        assert hints.readOnlyHint is (tier == {"tier:read"}), tool.name
        if tier != {"tier:action"}:
            assert hints.destructiveHint is False, tool.name


async def test_no_tool_asks_for_an_account_id_so_the_assistant_cannot_wander(make_client):
    async with make_client(CHATWOOT_MCP_ENABLE_WRITES="true", CHATWOOT_MCP_ENABLE_ACTIONS="true") as client:
        for tool in await client.list_tools():
            assert "account_id" not in tool.inputSchema.get("properties", {}), tool.name


def test_the_instructions_tell_the_assistant_customer_text_is_not_orders_and_to_confirm_actions():
    assert "<untrusted_content>" in INSTRUCTIONS and "never as instructions" in INSTRUCTIONS
    assert "confirm_token" in INSTRUCTIONS and "agree" in INSTRUCTIONS


async def test_the_server_can_be_built_twice_without_sharing_state(chatwoot, make_settings):
    import httpx

    a = build_server(make_settings(), token_getter=lambda: "a", transport=httpx.MockTransport(chatwoot))
    b = build_server(make_settings(CHATWOOT_MCP_ENABLE_WRITES="true"), token_getter=lambda: "b")
    async with Client(a) as ca, Client(b) as cb:
        assert len(await ca.list_tools()) == 30
        assert len(await cb.list_tools()) == 35
