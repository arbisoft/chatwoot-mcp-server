# Chatwoot MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI assistant (Claude Desktop, Claude Code, Cursor, MCP Inspector, ...) read and triage support chats in [Chatwoot](https://www.chatwoot.com).

It is a small [FastMCP v3](https://gofastmcp.com) program that sits next to Chatwoot and calls Chatwoot's existing HTTP API. **Chatwoot itself is not changed.** Every call is made with the caller's own Chatwoot key, so Chatwoot decides what each person may see and do.

This is the first version: 17 tools, mostly read-only. See [What is not here yet](#what-is-not-here-yet).

## Tools

Read (always available):

| Tool | Description |
|---|---|
| `get_my_profile` | Who this connection acts as, and which account the tools work in. Never shows a key. |
| `list_conversations` | List chats by status, assignee, inbox, team or label, newest activity first. |
| `search_conversations` | Find chats whose messages contain some words. |
| `get_conversation` | Open one chat: status, customer, assignee, team, labels, last message. |
| `get_messages` | Read the messages in a chat. Customer text is wrapped in `<untrusted_content>`. |
| `get_conversation_counts` | Count chats: yours, assigned, unassigned, all. |
| `search_contacts` | Find customers by name, email, phone or identifier. |
| `get_contact` | Open one customer. |
| `get_contact_conversations` | All chats one customer has had. |
| `list_inboxes` / `list_agents` / `list_teams` / `list_labels` / `list_canned_responses` | Small lists of names and ids the assistant needs for the other tools. |
| `get_report_summary` | Chat counts, reply and resolution times and satisfaction scores for a period, with the period before for comparison. |

Write (only exist when `CHATWOOT_MCP_ENABLE_WRITES=true`; neither can reach a customer):

| Tool | Description |
|---|---|
| `add_private_note` | Add an internal note to a chat. Refuses to report success if Chatwoot did not mark it private. |
| `add_conversation_labels` | Add labels to a chat. Existing labels stay; labels that do not exist are refused. |

Tools never take an account id. The account is fixed by `CHATWOOT_ACCOUNT_ID`, so an assistant cannot move to another account.

## Two ways to run it

- **stdio** (`chatwoot-mcp stdio`): one person, on their own machine, started by the MCP client. The key comes from `CHATWOOT_API_TOKEN`.
- **http** (`chatwoot-mcp http`): hosted for many people. Each caller sends their own key as the `X-Chatwoot-Token` header to `/http/api-key/mcp`. `GET /healthz` is for health checks.

## How the key is handled

- The key is sent to Chatwoot in Chatwoot's own `api_access_token` header and nowhere else. It is never shown in a tool result, an error or a log.
- The client never sends cookies or `X-Auth-Request-*` headers, and never follows redirects. In hosted mode any `X-Auth-Request-*` header a caller sends is dropped on arrival: those headers carry identity inside Moneta and must only come from the front door.
- A missing, wrong or expired key gives a short plain error. No request is made without a key.
- Request URLs are not logged, because a customer search puts the customer's email or phone in the URL.

## Other safety habits

- **Read-only by default.** The two write tools are not registered unless you switch them on.
- **Customer text is marked untrusted.** Message text is wrapped in `<untrusted_content>` and the server tells the assistant to treat it as data, never as instructions. A closing tag typed by a customer is neutralised.
- **Short replies.** Chatwoot's large payloads are reduced to the useful fields; lists and text are capped (`CHATWOOT_MCP_MAX_ITEMS`, `CHATWOOT_MCP_MAX_TEXT_CHARS`).
- **No deleting, no settings, no user management, no platform API.**

## Install

Not published to PyPI. From this folder:

```bash
uv venv && uv pip install -e ".[dev]"
```

## Run

Laptop (stdio):

```bash
CHATWOOT_BASE_URL=https://chatwoot.example.com \
CHATWOOT_ACCOUNT_ID=1 \
CHATWOOT_API_TOKEN=<your key: Chatwoot, Profile Settings, Access Token> \
chatwoot-mcp stdio
```

Claude Code:

```bash
claude mcp add chatwoot \
  -e CHATWOOT_BASE_URL=https://chatwoot.example.com -e CHATWOOT_ACCOUNT_ID=1 -e CHATWOOT_API_TOKEN=<key> \
  -- chatwoot-mcp stdio
```

Hosted (http):

```bash
CHATWOOT_BASE_URL=http://chatwoot:3000 CHATWOOT_ACCOUNT_ID=1 chatwoot-mcp http   # listens on 127.0.0.1:8214
```

then point the MCP client at `http://<host>:8214/http/api-key/mcp` with the header `X-Chatwoot-Token: <your key>`.

All settings are in [`.env.example`](./.env.example), each with a comment.

## Docker

```bash
docker build -t chatwoot-mcp-server mcp/      # from the repository root
docker run --rm -p 8214:8214 -e CHATWOOT_BASE_URL=http://chatwoot:3000 -e CHATWOOT_ACCOUNT_ID=1 chatwoot-mcp-server
```

The image runs as a normal user and has a health check on `/healthz`. Do not publish the port to the internet: in hosted mode anyone who can reach it and holds a valid Chatwoot key can use that key's rights.

## Development

```bash
uv run ruff format . && uv run ruff check .
uv run pytest --cov=chatwoot_mcp --cov-fail-under=80
```

The tests use a pretend Chatwoot and need no real server. See [CLAUDE.md](./CLAUDE.md) for the layout and the rules for changing this.

## What is not here yet

- **Moneta login and per-user keys.** Hosted mode today expects each caller to bring their own Chatwoot key. Signing in with the Moneta login (Cognito) and getting a key made automatically for each person is planned, and would live in a separate `platform/` package.
- **Tools that change things** (send a reply, assign, change status or priority, create or update a customer). They need a confirm step first.
- **Chatwoot features outside support chats:** campaigns, help centre, automation, webhooks.
