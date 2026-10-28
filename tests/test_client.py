import httpx
import pytest

from chatwoot_mcp.client import ChatwootClient
from chatwoot_mcp.client.errors import ChatwootError
from chatwoot_mcp.config import Settings


def settings(**kw):
    base = {"CHATWOOT_BASE_URL": "https://chat.example.com", "CHATWOOT_ACCOUNT_ID": "7"}
    base.update(kw)
    return Settings.from_env(base)


def make_client(handler, token="secret-key", **kw):
    return ChatwootClient(settings(**kw), token_getter=lambda: token, transport=httpx.MockTransport(handler))


async def test_account_calls_are_scoped_to_the_configured_account():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"payload": []})

    await make_client(handler).get_account("/inboxes")
    assert str(seen[0].url) == "https://chat.example.com/api/v1/accounts/7/inboxes"


async def test_the_key_is_sent_in_chatwoots_own_header_and_nowhere_else():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    await make_client(handler).get_account("/labels")
    request = seen[0]
    assert request.headers["api_access_token"] == "secret-key"
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers
    assert not [h for h in request.headers if h.lower().startswith("x-auth-request")]


async def test_query_values_that_are_none_are_left_out():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    await make_client(handler).get_account("/conversations", params={"status": "open", "inbox_id": None, "page": 2})
    assert dict(seen[0].url.params) == {"status": "open", "page": "2"}


async def test_list_values_are_sent_as_repeated_brackets():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    await make_client(handler).get_account("/conversations", params={"labels": ["vip", "refund"]})
    assert seen[0].url.query.decode() == "labels%5B%5D=vip&labels%5B%5D=refund"


async def test_profile_and_v2_paths_are_not_account_scoped_the_same_way():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    client = make_client(handler)
    await client.get_profile()
    await client.get_account("/reports/summary", version=2)
    assert seen == [
        "https://chat.example.com/api/v1/profile",
        "https://chat.example.com/api/v2/accounts/7/reports/summary",
    ]


async def test_post_sends_json():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": 1})

    result = await make_client(handler).post_account("/conversations/5/messages", {"content": "hi", "private": True})
    assert result == {"id": 1}
    assert seen[0].method == "POST"
    assert seen[0].read() == b'{"content":"hi","private":true}'


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (401, "key was rejected"),
        (403, "not allowed"),
        (404, "not found"),
        (422, "refused the request"),
        (429, "too many requests"),
        (500, "failed on its side"),
    ],
)
async def test_http_errors_become_plain_messages_without_the_key(status, needle):
    def handler(request):
        return httpx.Response(status, json={"error": "details with secret-key inside"})

    with pytest.raises(ChatwootError) as caught:
        await make_client(handler).get_account("/labels")
    assert needle in str(caught.value)
    assert "secret-key" not in str(caught.value)
    assert caught.value.status == status


async def test_a_422_includes_the_apps_reason_but_not_the_key():
    def handler(request):
        return httpx.Response(422, json={"message": "Content can't be blank"})

    with pytest.raises(ChatwootError) as caught:
        await make_client(handler).post_account("/conversations/1/messages", {})
    assert "Content can't be blank" in str(caught.value)


async def test_network_problems_become_a_plain_message():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(ChatwootError, match="could not reach Chatwoot"):
        await make_client(handler).get_account("/labels")


async def test_timeouts_become_a_plain_message():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ChatwootError, match="took too long"):
        await make_client(handler).get_account("/labels")


async def test_a_reply_that_is_not_json_is_an_error():
    def handler(request):
        return httpx.Response(200, text="<html>login</html>")

    with pytest.raises(ChatwootError, match="not JSON"):
        await make_client(handler).get_account("/labels")


async def test_an_empty_reply_is_an_empty_dict():
    def handler(request):
        return httpx.Response(204)

    assert await make_client(handler).get_account("/labels") == {}


async def test_a_missing_key_stops_before_any_request():
    called = []

    def handler(request):
        called.append(request)
        return httpx.Response(200, json={})

    client = ChatwootClient(settings(), token_getter=lambda: None, transport=httpx.MockTransport(handler))
    with pytest.raises(ChatwootError, match="No Chatwoot key"):
        await client.get_account("/labels")
    assert called == []


async def test_redirects_are_not_followed_so_the_key_cannot_leak():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example.net/steal"})

    with pytest.raises(ChatwootError, match="redirected the request .HTTP 302.") as raised:
        await make_client(handler).get_account("/labels")
    assert seen == ["https://chat.example.com/api/v1/accounts/7/labels"]
    assert "evil.example.net" not in str(raised.value)
