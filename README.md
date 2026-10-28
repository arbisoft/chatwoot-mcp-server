# Chatwoot MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI assistant (Claude Desktop, Claude Code, Cursor, MCP Inspector, ...) read and triage support chats in [Chatwoot](https://www.chatwoot.com).

It is a small [FastMCP v3](https://gofastmcp.com) program that sits next to Chatwoot and calls Chatwoot's existing HTTP API. **Chatwoot itself is not changed.** Every call is made with the caller's own Chatwoot key, so Chatwoot decides what each person may see and do.

This is an early version: 34 tools. 24 only read, 3 make safe additions that cannot reach a customer, and 7 are actions that change things and always ask first. See [What is not here yet](#what-is-not-here-yet).

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
| `get_conversation_labels` | The labels on one chat. |
| `search_contacts` | Find customers by name, email, phone or identifier. |
| `get_contact` | Open one customer. |
| `list_contacts` | Browse customers, 15 per page, optionally by label. |
| `get_contact_labels` | The labels on one customer. |
| `get_contact_conversations` | All chats one customer has had. |
| `list_inboxes` / `list_agents` / `list_teams` / `list_labels` / `list_canned_responses` | Small lists of names and ids the assistant needs for the other tools. |
| `get_inbox` | One inbox's settings (greeting, working hours, auto-assignment). Never its webhook address or secrets. |
| `list_team_members` | The agents in one team. |
| `list_custom_attribute_definitions` | The custom fields defined for chats or customers, with their allowed values. |
| `get_account` | The account's name, language, status and support address. No billing or settings. |
| `get_report_summary` | Chat counts, reply and resolution times and satisfaction scores for a period, with the period before for comparison. |
| `get_report_by_group` | The same kind of numbers split by agent, team, inbox, label or channel. |
| `get_first_response_distribution` | How quickly first replies went out, in time bands, per channel. |

Write (only exist when `CHATWOOT_MCP_ENABLE_WRITES=true`; none can reach a customer):

| Tool | Description |
|---|---|
| `add_private_note` | Add an internal note to a chat. Refuses to report success if Chatwoot did not mark it private. |
| `add_conversation_labels` | Add labels to a chat. Existing labels stay; labels that do not exist are refused. |
| `add_contact_labels` | The same for a customer. |

Actions (only exist when `CHATWOOT_MCP_ENABLE_ACTIONS=true`; each one asks first, see [The confirm step](#the-confirm-step)):

| Tool | Description |
|---|---|
| `send_reply` | Send a reply to the customer in a chat. Refused if the chat cannot be replied to. |
| `assign_conversation` | Assign a chat to an agent, a team, or both. Replaces who it is assigned to now. |
| `set_conversation_status` | Set a chat to open, pending, snoozed or resolved. Reopening also assigns it to the person who owns the key (Chatwoot's rule), and the preview says so. |
| `set_conversation_priority` | Set a chat's priority: none, low, medium, high or urgent. |
| `create_conversation` | Start a new chat with a customer in an inbox, optionally with a first message the customer receives. |
| `create_contact` | Create a customer. Needs at least one of email, phone number or identifier. |
| `update_contact` | Change a customer's name, email, phone number or identifier. |

Tools never take an account id. The account is fixed by `CHATWOOT_ACCOUNT_ID`, so an assistant cannot move to another account.

## Two ways to run it

- **stdio** (`chatwoot-mcp stdio`): one person, on their own machine, started by the MCP client. The key comes from `CHATWOOT_API_TOKEN`.
- **http** (`chatwoot-mcp http`): hosted for many people. Each caller sends their own key as the `X-Chatwoot-Token` header to `/http/api-key/mcp`. `GET /healthz` is for health checks.

## The confirm step

An action never changes anything the first time you call it. The first call looks up what it would touch and returns a plain **preview** (who would get the message, what would change from and to) and a one-time `confirm_token`. The assistant is told to show the preview to the person and to call the tool again with the same arguments and the token only after they clearly agree.

The token is tied to the person's key, the tool and the exact arguments, so it cannot be used by someone else, for another action, or after the request was edited. It works once and expires after `CHATWOOT_MCP_CONFIRM_TTL_SECONDS` (default 5 minutes). Tokens are kept in this process's memory, so run a single server process; several processes behind a load balancer would need shared storage.

## How the key is handled

- The key is sent to Chatwoot in Chatwoot's own `api_access_token` header and nowhere else. It is never shown in a tool result, an error or a log.
- The client never sends cookies or `X-Auth-Request-*` headers, and never follows redirects. In hosted mode any `X-Auth-Request-*` header a caller sends is dropped on arrival: those headers carry identity inside Moneta and must only come from the front door.
- A missing, wrong or expired key gives a short plain error. No request is made without a key.
- Request URLs are not logged, because a customer search puts the customer's email or phone in the URL.

## Other safety habits

- **Read-only by default.** The three write tools need `CHATWOOT_MCP_ENABLE_WRITES=true` and the seven action tools need `CHATWOOT_MCP_ENABLE_ACTIONS=true`; otherwise they do not exist for the assistant.
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
- **Shared confirm storage** for running several server processes.
- **Chatwoot features outside support chats:** campaigns, help centre, automation, webhooks, attachments, WhatsApp templates, the audit log, merging customers and creating labels.
- **Deleting anything, and user, team or inbox management.** Left out on purpose.
