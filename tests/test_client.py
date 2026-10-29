import random

import httpx
import pytest

from chatwoot_mcp.client import ChatwootClient, to_tool_error
from chatwoot_mcp.client.errors import (
    AuthError,
    ChatwootAPIError,
    ChatwootTransportError,
    GatewayDeniedError,
    IdentityHeaderError,
    InvalidTokenError,
    MissingTokenError,
    NotFoundError,
    PermissionDenied,
    RateLimitedError,
    RedirectedError,
    ServerError,
    UnprocessableError,
    error_for_status,
)
from chatwoot_mcp.client.http import (
    accepted_but_unreadable,
    never_sent,
    outcome_unknown,
    query,
)
from tests.conftest import BASE, RecordingTransport

KEY = "secret-key-123"


async def no_sleep(_seconds: float) -> None:
    return None


def client_for(handler, **options) -> tuple[ChatwootClient, RecordingTransport]:
    transport = RecordingTransport(handler)
    client = ChatwootClient(BASE, transport=transport, sleep=no_sleep, rng=random.Random(0), **options)
    return client, transport


async def test_the_key_goes_only_in_chatwoots_header_and_nothing_else_is_sent():
    client, transport = client_for(lambda r: httpx.Response(200, json={"ok": True}, headers={"set-cookie": "s=1"}))
    assert await client.get("/api/v1/profile", token=KEY) == {"ok": True}
    await client.get("/api/v1/profile", token=KEY)
    first, second = transport.requests
    assert first.headers["api_access_token"] == KEY
    assert "authorization" not in first.headers
    assert "cookie" not in second.headers, "a cookie Chatwoot set must never be sent back"
    assert KEY not in str(first.url)
    await client.aclose()


async def test_an_identity_header_can_never_leave_the_process():
    client, transport = client_for(lambda r: httpx.Response(200, json={}))
    http = client._client()
    request = http.build_request("GET", "/x", headers={"X-Auth-Request-Email": "boss@example.com"})
    with pytest.raises(IdentityHeaderError):
        await http.send(request)
    request = http.build_request("GET", "/x", headers={"x_auth_request_user": "boss"})
    with pytest.raises(IdentityHeaderError):
        await http.send(request)
    assert transport.requests == []


async def test_redirects_are_not_followed_so_the_key_cannot_reach_another_host():
    client, transport = client_for(lambda r: httpx.Response(302, headers={"location": "https://evil.test/steal"}))
    with pytest.raises(RedirectedError) as raised:
        await client.get("/api/v1/profile", token=KEY)
    assert [str(r.url) for r in transport.requests] == [f"{BASE}/api/v1/profile"]
    assert "login page" in to_tool_error(raised.value) and "evil" not in to_tool_error(raised.value)


async def test_chatwoots_error_text_never_carries_the_key():
    client, _ = client_for(lambda r: httpx.Response(422, json={"message": f"bad key {KEY} here"}))
    with pytest.raises(UnprocessableError) as raised:
        await client.request("POST", "/x", token=KEY, json={})
    assert KEY not in str(raised.value) and KEY not in to_tool_error(raised.value)
    assert "[key hidden]" in raised.value.detail


@pytest.mark.parametrize("token", [None, ""])
async def test_no_key_means_no_request(token):
    client, transport = client_for(lambda r: httpx.Response(200))
    with pytest.raises(MissingTokenError):
        await client.get("/x", token=token)
    assert transport.requests == []


@pytest.mark.parametrize("token", ["has space", "naïve", "line\nbreak"])
async def test_a_key_that_cannot_be_a_header_is_refused(token):
    client, transport = client_for(lambda r: httpx.Response(200))
    with pytest.raises(InvalidTokenError):
        await client.get("/x", token=token)
    assert transport.requests == []


def test_lists_are_sent_the_rails_way_and_none_is_dropped():
    assert query({"labels": ["a", "b"], "page": 2, "inbox_id": None}) == {"labels[]": ["a", "b"], "page": 2}


async def test_an_empty_body_is_none_and_a_non_json_body_is_an_error():
    client, _ = client_for(lambda r: httpx.Response(200))
    assert await client.get("/x", token=KEY) is None
    client, _ = client_for(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(ChatwootAPIError) as raised:
        await client.get("/x", token=KEY)
    assert accepted_but_unreadable(raised.value)


async def test_get_retries_a_gateway_error_but_post_does_not():
    answers = iter([httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    client, transport = client_for(lambda r: next(answers))
    assert await client.get("/x", token=KEY) == {"ok": 1}
    assert len(transport.requests) == 2
    client, transport = client_for(lambda r: httpx.Response(503))
    with pytest.raises(ServerError) as raised:
        await client.request("POST", "/messages", token=KEY, json={"content": "hi"})
    assert len(transport.requests) == 1 and outcome_unknown(raised.value)


async def test_a_read_only_post_may_retry():
    answers = iter([httpx.Response(502), httpx.Response(200, json=[])])
    client, transport = client_for(lambda r: next(answers))
    assert await client.request("POST", "/filter", token=KEY, json={}, retry_safe=True) == []
    assert len(transport.requests) == 2


async def test_a_post_retries_only_when_it_provably_never_left():
    attempts = []

    def connect_fails_once(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"id": 1})

    client, _ = client_for(connect_fails_once)
    assert await client.request("POST", "/messages", token=KEY, json={}) == {"id": 1}

    def read_times_out(request):
        raise httpx.ReadTimeout("slow", request=request)

    client, transport = client_for(read_times_out)
    with pytest.raises(ChatwootTransportError) as raised:
        await client.request("POST", "/messages", token=KEY, json={})
    assert len(transport.requests) == 1
    assert outcome_unknown(raised.value) and not never_sent(raised.value)


async def test_an_unreachable_chatwoot_is_never_sent():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    client, transport = client_for(refuse)
    with pytest.raises(ChatwootTransportError) as raised:
        await client.get("/x", token=KEY)
    assert never_sent(raised.value) and len(transport.requests) == 3


async def test_a_429_is_retried_once_after_retry_after():
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    answers = iter([httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(429)])
    transport = RecordingTransport(lambda r: next(answers))
    client = ChatwootClient(BASE, transport=transport, sleep=sleep)
    with pytest.raises(RateLimitedError):
        await client.request("POST", "/x", token=KEY, json={})
    assert waits == [3.0] and len(transport.requests) == 2


async def test_the_rejection_hook_can_swap_the_key_once():
    seen = []

    def handler(request):
        seen.append(request.headers["api_access_token"])
        return httpx.Response(401 if request.headers["api_access_token"] == "old" else 200, json={"ok": 1})

    async def renew(error, token):
        assert isinstance(error, AuthError) and token == "old"
        return "new"

    client, _ = client_for(handler, on_rejected=renew)
    assert await client.get("/x", token="old") == {"ok": 1}
    assert seen == ["old", "new"]


async def test_the_rejection_hook_returning_nothing_keeps_the_error():
    async def nothing(error, token):
        return None

    client, transport = client_for(
        lambda r: httpx.Response(401, json={"error": "Invalid Access Token"}), on_rejected=nothing
    )
    with pytest.raises(AuthError):
        await client.get("/x", token=KEY)
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("status", "detail", "kind", "needle"),
    [
        (301, "Moved", RedirectedError, "redirected"),
        (401, "Invalid Access Token", AuthError, "rejected the key"),
        (401, "You are not authorized to do this action", PermissionDenied, "role does not allow"),
        (403, "Forbidden", PermissionDenied, "role does not allow"),
        (403, '{"error": "access_denied"}', GatewayDeniedError, "sign-in gateway"),
        (404, "nope", NotFoundError, "not found"),
        (422, "Conversation is resolved", UnprocessableError, "Conversation is resolved"),
        (429, "slow down", RateLimitedError, "rate limiting"),
        (500, "boom", ServerError, "server error"),
        (418, "teapot", ChatwootAPIError, "HTTP 418"),
    ],
)
def test_statuses_become_plain_errors(status, detail, kind, needle):
    error = error_for_status(status, detail)
    assert type(error) is kind
    assert needle in to_tool_error(error) and to_tool_error(error).startswith("Error: ")


def test_a_plain_error_says_what_went_wrong():
    assert to_tool_error(MissingTokenError("no key")) == "Error: no key"


async def test_chatwoot_reason_is_read_from_its_different_error_shapes():
    for body, reason in [
        ({"message": "m"}, "m"),
        ({"error": "e"}, "e"),
        ({"description": "d"}, "d"),
        ({"errors": ["a", "b"]}, "a; b"),
        (["not", "a", "dict"], "Unprocessable Entity"),
    ]:
        client, _ = client_for(lambda r, b=body: httpx.Response(422, json=b))
        with pytest.raises(UnprocessableError) as raised:
            await client.get("/x", token=KEY)
        assert raised.value.detail == reason
