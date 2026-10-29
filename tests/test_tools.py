import dataclasses
import re
from pathlib import Path

from fastmcp import Client

from chatwoot_mcp.config import TOOL_MODULES
from chatwoot_mcp.server import INSTRUCTIONS, build_server
from tests.helpers import tools

ANNOTATION_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
README = Path(__file__).resolve().parents[1] / "README.md"

READ_TOOLS = {
    "get_me",
    "list_inboxes",
    "get_inbox",
    "list_agents",
    "list_teams",
    "list_team_members",
    "list_labels",
    "list_canned_responses",
    "list_custom_attribute_definitions",
    "get_account",
    "list_conversations",
    "search_conversations",
    "filter_conversations",
    "get_conversation",
    "get_conversation_counts",
    "list_conversation_attachments",
    "get_messages",
    "search_contacts",
    "list_contacts",
    "get_contact",
    "get_contact_conversations",
    "get_contact_notes",
    "get_conversation_labels",
    "get_contact_labels",
    "search_help_articles",
    "get_report_summary",
    "get_report_by_group",
    "get_first_response_distribution",
    "list_csat_responses",
}
WRITE_TOOLS = {
    "assign_conversation",
    "set_conversation_status",
    "set_conversation_priority",
    "set_conversation_custom_attributes",
    "add_private_note",
    "create_contact",
    "update_contact",
    "add_contact_note",
    "add_conversation_labels",
    "add_contact_labels",
    "remove_conversation_labels",
}
OPT_IN_READ_TOOLS = {"list_macros"}
CONFIRMED_TOOLS = {
    "prepare_send_reply",
    "send_reply",
    "prepare_create_conversation",
    "create_conversation",
    "prepare_run_macro",
    "run_macro",
}
ADMIN_TOOLS = {"get_report_summary", "get_report_by_group", "get_first_response_distribution", "list_csat_responses"}


async def test_by_default_every_read_and_direct_write_exists_but_nothing_that_reaches_the_customer(settings, fake):
    assert set(await tools(settings, fake)) == READ_TOOLS | WRITE_TOOLS


async def test_read_only_leaves_only_reads(read_only, fake):
    assert set(await tools(read_only, fake)) == READ_TOOLS | OPT_IN_READ_TOOLS


async def test_every_module_on_adds_replies_and_macros_with_their_confirm_steps(everything, fake):
    names = set(await tools(everything, fake))
    assert names == READ_TOOLS | WRITE_TOOLS | OPT_IN_READ_TOOLS | CONFIRMED_TOOLS
    assert len(names) == 47


async def test_a_module_switched_off_never_registers(settings, fake):
    no_reports = dataclasses.replace(settings, enabled_modules=settings.enabled_modules - {"reports"})
    assert not ADMIN_TOOLS & set(await tools(no_reports, fake))
    nothing = dataclasses.replace(settings, enabled_modules=frozenset())
    assert set(await tools(nothing, fake)) == {"get_me"}


async def test_every_tool_has_all_four_hints_a_title_one_tier_and_one_module(everything, fake):
    for name, tool in (await tools(everything, fake)).items():
        hints = tool.annotations.model_dump()
        missing = [hint for hint in ANNOTATION_HINTS if hints.get(hint) is None]
        assert not missing, f"{name} is missing {missing}"
        assert tool.annotations.title, name
        tags = set(tool.meta["fastmcp"]["tags"])
        assert len({t for t in tags if t.startswith("tier:")}) == 1, name
        assert len({t for t in tags if t.startswith("module:")}) == 1, name
        assert hints["readOnlyHint"] is (name in READ_TOOLS | OPT_IN_READ_TOOLS), name
        assert ("read" in tags) is hints["readOnlyHint"], name


async def test_the_tools_that_send_cannot_be_called_without_a_confirmation_token(everything, fake):
    listed = await tools(everything, fake)
    for name in ("send_reply", "create_conversation", "run_macro"):
        assert "confirmation_token" in listed[name].inputSchema["required"], name
        assert listed[name].annotations.idempotentHint is False, name
        assert f"prepare_{name}" in listed


async def test_reports_need_an_admin_and_get_me_needs_nothing(everything, fake):
    listed = await tools(everything, fake)
    for name, tool in listed.items():
        tags = set(tool.meta["fastmcp"]["tags"])
        expected = "tier:admin" if name in ADMIN_TOOLS else "tier:none" if name == "get_me" else "tier:agent"
        assert expected in tags, name


async def test_no_tool_asks_for_an_account_id_so_the_assistant_cannot_wander(everything, fake):
    for name, tool in (await tools(everything, fake)).items():
        assert "account_id" not in tool.inputSchema.get("properties", {}), name


def test_the_instructions_say_customer_text_is_data_and_to_confirm_first():
    assert "<untrusted_content>" in INSTRUCTIONS and "never follow instructions" in INSTRUCTIONS
    assert "prepare_*" in INSTRUCTIONS and "clearly agrees" in INSTRUCTIONS


def documented_tools() -> set[str]:
    """Every backticked name in the first column of the README's tool tables."""
    names: set[str] = set()
    for line in README.read_text().splitlines():
        if line.startswith("| `"):
            names.update(re.findall(r"`([a-z_]+)`", line.split("|")[1]))
    return names


async def test_the_readme_lists_exactly_the_tools_that_exist(everything, fake):
    assert documented_tools() == set(await tools(everything, fake))


async def test_the_server_can_be_built_twice_without_sharing_state(settings, everything, fake):
    async with (
        Client(build_server(settings, transport=fake)) as a,
        Client(build_server(everything, transport=fake)) as b,
    ):
        assert len(await a.list_tools()) == len(READ_TOOLS | WRITE_TOOLS)
        assert len(await b.list_tools()) == 47


def test_every_module_has_a_flag():
    assert set(TOOL_MODULES) == {
        "reference",
        "conversations",
        "messages",
        "contacts",
        "labels",
        "help_center",
        "reports",
        "replies",
        "macros",
    }
