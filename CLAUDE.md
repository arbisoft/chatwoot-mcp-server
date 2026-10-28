# CLAUDE.md: mcp/ (chatwoot-mcp-server)

## What this is

A FastMCP v3 server that exposes Chatwoot support chats as MCP tools. It lives in `mcp/` inside the Chatwoot fork
but is its own Python project: it shares no code with the Rails app and must never import from it.

**No changes to Chatwoot are allowed.** Every tool calls an API route that already exists. The API description is
`swagger/swagger.json` in the repository root; check a route there (and in `app/views/api/`) before adding a tool.

## Layout

```
mcp/
├── pyproject.toml  README.md  CLAUDE.md  Dockerfile  .env.example
├── chatwoot_mcp/
│   ├── __main__.py        # `chatwoot-mcp stdio|http`; settings errors exit with code 2
│   ├── config.py          # Settings.from_env: every value checked once at start-up
│   ├── server.py          # build_server(settings, token_getter=...), build_http_app(mcp)
│   ├── shaping.py         # compact_* reduce Chatwoot's payloads; wrap_untrusted marks customer text
│   ├── client/            # ChatwootClient: the only code that talks to Chatwoot (errors.py = plain messages)
│   ├── credentials/       # static.py (stdio: key from env), header.py (http: X-Chatwoot-Token per caller)
│   └── tools/             # one module per area, each with register(mcp, ctx); support.py = shared pieces
└── tests/                 # pretend Chatwoot (httpx.MockTransport) + in-memory MCP client; no network
```

## Commands

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest --cov=chatwoot_mcp --cov-fail-under=80
uv run ruff format . && uv run ruff check .
CHATWOOT_BASE_URL=... CHATWOOT_ACCOUNT_ID=1 CHATWOOT_API_TOKEN=... python -m chatwoot_mcp stdio
```

## Rules that must keep holding (each has a test)

- **The key** goes to Chatwoot only in the `api_access_token` header, never appears in an error, a tool result or a
  log, and is never sent anywhere else. The client sends no cookies and no `X-Auth-Request-*` headers and does not
  follow redirects. Do not add a way around any of these.
- **No account id in tool arguments.** The account comes from `CHATWOOT_ACCOUNT_ID`.
- **Writes and actions are opt-in.** `tier:write` tools (private note, labels) are registered only when
  `CHATWOOT_MCP_ENABLE_WRITES=true` and must never be able to reach a customer. `tier:action` tools (reply, assign,
  status, priority, create chat, create or update customer) only when `CHATWOOT_MCP_ENABLE_ACTIONS=true`.
  `add_private_note` checks Chatwoot marked the note private; `send_reply` checks it did not.
- **Every action has two steps** (`gated()` in `tools/actions.py`). The first call changes nothing and returns a
  preview plus a `confirm_token`; the second, with the same arguments and the token, does it. Tokens are tied to the
  caller's key, the tool and the exact arguments, work once and expire (`confirmations.py`). Validate and look things
  up *before* issuing a token, so a doomed action never gets one. Tokens live in memory, so one process only.
- **Customer text is untrusted.** Wrap any text a customer or outsider wrote with `shaping.wrap_untrusted`.
- **`get_my_profile` returns an allow-list of fields.** Chatwoot's profile holds access and pubsub tokens.
- **Do not log request URLs** (`httpx` is set to WARNING in `__main__.py`): a customer search puts an email or phone
  in the URL.
- **Every tool** has all four MCP annotation hints, one `tier:*` tag and one `module:*` tag, and raises `ToolError`
  with a plain message (via `@reporting_errors`) instead of leaking a traceback. `tests/test_server.py` checks this.

## Adding an action

Same as a tool, but use `ACTION_ADDS` (it only adds) or `ACTION_OVERWRITES` (it replaces what was there), tag it
`TIER_ACTION`, and wrap it in `gated(...)` with a `build_preview` that reads what it would touch. Say in the preview
anything Chatwoot does as a side effect (for example, reopening a chat assigns it to the acting agent).

## Adding a tool

1. Find the route in `swagger/swagger.json` and the response shape in `app/views/api/`.
2. Write the test first in `tests/` using `FakeChatwoot` and `call(...)`.
3. Add the tool in the right `tools/<area>.py` with `@mcp.tool(annotations=..., tags={TIER_..., "module:..."})`
   and `@reporting_errors`. Shape the result in `shaping.py`; do not return Chatwoot's raw payload.
4. Update the tool table in README.md and the expected sets in `tests/test_server.py`.

## Chatwoot facts worth knowing

- Account routes are `/api/v1/accounts/{id}/...`; reports are `/api/v2/...`; the profile is `/api/v1/profile`.
- `POST .../conversations/{id}/labels` **replaces** the whole label set, so `add_conversation_labels` reads the
  current labels first and sends the union. Labels that do not exist in the account are refused, to avoid creating
  stray tags.
- Label titles are stored in lower case, so the label tools lower-case what they are given before matching.
- `toggle_status` reads `snoozed_until` with `strptime('%s')`: send Unix seconds. An ISO string parses as a
  moment in 1970, and the chat wakes at once.
- Changing a chat's team drops an assignee who is not in that team, so `assign_conversation` sets the team first.
- The `labels` filter on chat and customer lists matches *any* of the labels, not all.
- Report timestamps are Unix seconds. The tool takes `YYYY-MM-DD` dates (UTC) and converts them.
- Some report routes exist only in the Enterprise edition or in some versions.
- Under the Moneta SSO setup the public host puts logins in front of `/api`, so a hosted MCP server should call
  Chatwoot on the internal network.

## Branch policy

Same as the rest of the fork: feature branch, then a pull request into `foss-sandbox`, then a promotion pull
request from `foss-sandbox` into `foss-main` (merge commit, never squash). Never push directly to either.
