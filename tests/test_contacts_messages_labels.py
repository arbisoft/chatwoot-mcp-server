import dataclasses
import json

import httpx
import pytest

from tests.helpers import data, unframed

CONTACT = {"id": 4, "name": "Sam", "email": "sam@example.com", "custom_attributes": {"plan": "pro"}}


# --- contacts -------------------------------------------------------------------------------------


async def test_search_and_open_a_contact(settings, fake):
    fake.on("GET", "/contacts/search", json={"meta": {"count": 1}, "payload": [CONTACT]})
    fake.on("GET", "/contacts/4", json={"payload": CONTACT})
    found = await data(settings, fake, "search_contacts", {"query": "sam@example.com"})
    assert fake.calls("GET", "/contacts/search")[-1].url.params["q"] == "sam@example.com"
    assert unframed(found["contacts"][0])["custom_attributes"] == {"plan": "pro"}
    one = await data(settings, fake, "get_contact", {"contact_id": 4})
    assert unframed(one["name"]) == "Sam" and one["url"].endswith("/contacts/4")


async def test_list_contacts_their_chats_and_notes(settings, fake):
    fake.on("GET", "/contacts", json={"meta": {"count": 1}, "payload": [CONTACT]})
    fake.on("GET", "/contacts/4/conversations", json={"payload": [{"id": 12}]})
    fake.on("GET", "/contacts/4/notes", json=[{"id": 1, "content": "prefers phone", "user": {"id": 8, "name": "Ana"}}])
    assert (await data(settings, fake, "list_contacts", {"labels": ["vip"]}))["contacts"][0]["id"] == 4
    assert fake.calls("GET", "/contacts")[-1].url.params.get_list("labels[]") == ["vip"]
    assert (await data(settings, fake, "get_contact_conversations", {"contact_id": 4}))["conversations"][0]["id"] == 12
    notes = await data(settings, fake, "get_contact_notes", {"contact_id": 4})
    assert unframed(notes["notes"][0]) == {"id": 1, "content": "prefers phone", "author": {"id": 8, "name": "Ana"}}


async def test_create_contact_needs_a_way_to_reach_them(settings, fake):
    fake.on("POST", "/contacts", json={"payload": {"contact": {"id": 22, "name": "New"}}})
    assert "at least one" in await data(settings, fake, "create_contact", {"name": "New"})
    out = await data(settings, fake, "create_contact", {"name": "New", "email": "new@example.com"})
    assert fake.last_json("POST", "/contacts") == {"name": "New", "email": "new@example.com"} and out["id"] == 22


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"email": "not-an-email"}, "email"),
        ({"phone_number": "555 0100"}, "international"),
        ({"identifier": "  "}, "identifier is empty"),
        ({"name": "  "}, "name is empty"),
    ],
)
async def test_bad_contact_fields_never_reach_chatwoot(settings, fake, fields, reason):
    out = await data(settings, fake, "update_contact", {"contact_id": 4, **fields})
    assert out.startswith("Error:") and reason in out and fake.writes() == []


async def test_update_contact_sends_only_the_given_fields(settings, fake):
    fake.on("PUT", "/contacts/4", json={"payload": {**CONTACT, "phone_number": "+15550100"}})
    out = await data(settings, fake, "update_contact", {"contact_id": 4, "phone_number": "+15550100"})
    assert fake.last_json("PUT", "/contacts/4") == {"phone_number": "+15550100"}
    assert unframed(out["phone_number"]) == "+15550100"
    assert "at least one field" in await data(settings, fake, "update_contact", {"contact_id": 4})


async def test_add_contact_note_sends_chatwoots_nested_shape(settings, fake):
    fake.on("POST", "/contacts/4/notes", json={"id": 9, "content": "VIP"})
    out = await data(settings, fake, "add_contact_note", {"contact_id": 4, "content": "VIP"})
    assert fake.last_json("POST", "/contacts/4/notes") == {"note": {"content": "VIP"}}
    assert out["contact_id"] == 4 and out["id"] == 9


# --- messages -------------------------------------------------------------------------------------


async def test_get_messages_frames_text_and_says_where_to_continue(settings, fake):
    messages = [{"id": i, "content": f"m{i}", "message_type": 0} for i in (41, 42, 43)]
    fake.on("GET", "/conversations/12/messages", json={"meta": {"labels": ["vip"]}, "payload": messages})
    out = await data(
        dataclasses.replace(settings, max_items=2), fake, "get_messages", {"conversation_id": 12, "after": 40}
    )
    assert fake.calls("GET", "/conversations/12/messages")[-1].url.params["after"] == "40"
    assert [m["id"] for m in out["messages"]] == [41, 42] and "after=42" in out["truncated"]
    assert out["labels"] == ["vip"] and out["messages"][0]["content"].startswith("<untrusted_content")


async def test_a_private_note_asks_for_private_and_refuses_success_otherwise(settings, fake):
    fake.on("POST", "/conversations/12/messages", json={"id": 70, "content": "x", "message_type": 1, "private": True})
    out = await data(settings, fake, "add_private_note", {"conversation_id": 12, "content": "call back"})
    assert fake.last_json("POST", "/conversations/12/messages") == {
        "content": "call back",
        "message_type": "outgoing",
        "private": True,
    }
    assert out["private"] is True
    fake.on("POST", "/conversations/12/messages", json={"id": 71, "content": "x", "private": False})
    assert "did not mark the message private" in await data(
        settings, fake, "add_private_note", {"conversation_id": 12, "content": "x"}
    )
    assert (await data(settings, fake, "add_private_note", {"conversation_id": 12, "content": "  "})).startswith(
        "Error"
    )


# --- labels ---------------------------------------------------------------------------------------


def label_routes(fake, existing):
    fake.on("GET", "/labels", json={"payload": [{"id": i, "title": t} for i, t in enumerate(["vip", "refund", "bug"])]})
    fake.on("GET", "/conversations/12/labels", json={"payload": list(existing)})
    # Chatwoot answers with the label set it saved.
    fake.on(
        "POST",
        "/conversations/12/labels",
        handler=lambda request: httpx.Response(200, json={"payload": json.loads(request.content)["labels"]}),
    )


async def test_adding_labels_keeps_the_others_matches_any_case_and_never_duplicates(settings, fake):
    label_routes(fake, ["vip"])
    out = await data(settings, fake, "add_conversation_labels", {"conversation_id": 12, "labels": ["Refund", "refund"]})
    assert fake.last_json("POST", "/conversations/12/labels") == {"labels": ["vip", "refund"]}
    assert out == {"conversation_id": 12, "labels": ["vip", "refund"]}


async def test_unknown_labels_are_refused_so_none_are_invented(settings, fake):
    label_routes(fake, [])
    out = await data(settings, fake, "add_conversation_labels", {"conversation_id": 12, "labels": ["vip", "made-up"]})
    assert "do not exist: made-up" in out and fake.writes() == []


async def test_removing_labels_keeps_the_others(settings, fake):
    label_routes(fake, ["vip", "refund", "bug"])
    out = await data(
        settings, fake, "remove_conversation_labels", {"conversation_id": 12, "labels": ["REFUND", "nope"]}
    )
    assert out == {"conversation_id": 12, "labels": ["vip", "bug"]}


async def test_contact_labels(settings, fake):
    fake.on("GET", "/labels", json={"payload": [{"id": 1, "title": "vip"}]})
    fake.on("GET", "/contacts/4/labels", json={"payload": []})
    fake.on("POST", "/contacts/4/labels", json={"payload": ["vip"]})
    assert await data(settings, fake, "get_contact_labels", {"contact_id": 4}) == {"contact_id": 4, "labels": []}
    assert (await data(settings, fake, "add_contact_labels", {"contact_id": 4, "labels": ["vip"]}))["labels"] == ["vip"]
    fake.on("GET", "/conversations/9/labels", json={"payload": ["bug"]})
    assert await data(settings, fake, "get_conversation_labels", {"conversation_id": 9}) == {
        "conversation_id": 9,
        "labels": ["bug"],
    }
