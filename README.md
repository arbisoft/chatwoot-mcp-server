# Chatwoot MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server for [Chatwoot](https://www.chatwoot.com) support
conversations. It lets Claude, or any MCP client, read and work on chats, customers and reports using **the caller's
own Chatwoot permissions**: every request carries the caller's own Chatwoot key, never a shared admin key, so
Chatwoot itself decides what each user can see and change.

It lives in `mcp/` inside the Chatwoot fork but is its own Python project and never imports from the Rails app.
Every tool calls an API route Chatwoot already has; nothing in Chatwoot is changed.

Status: 49 tools in stdio and HTTP. Replies and macros are available behind opt-in flags. Platform mode (Moneta SSO,
each user's own key fetched automatically) is available.

## Modes

### Community mode

- **stdio**: for one person on their own machine. The MCP client starts `chatwoot-mcp stdio` and the server uses the
  key in `CHATWOOT_HTTP_TOKEN` (Chatwoot: Profile Settings, Access Token).
- **http**: hosted. Each caller sends their own key as `X-Chatwoot-Token` to `/http/api-key/mcp`; any
  `X-Auth-Request-*` header they send is dropped on arrival. `/mcp` with a shared `CHATWOOT_HTTP_TOKEN` is only
  served with `CHATWOOT_HTTP_SHARED_TOKEN_ROUTE=true`, because anyone who reaches it acts as that key's owner.

### Platform mode

`chatwoot-mcp http` with `COGNITO_USER_POOL_ID` set. MCP clients sign in through Moneta (AWS Cognito, OAuth 2.0
with dynamic client registration) and nobody pastes a key: the server fetches each user's own Chatwoot key through
Chatwoot's existing SSO handoff on the internal network, caches it encrypted, and re-checks it every four hours. It
needs the `[platform]` extra and Chatwoot running with `AUTH_TYPE=SSO`. See [docs/platform-mode.md](docs/platform-mode.md).

## Quick start

```bash
cd mcp
uv sync --all-extras
CHATWOOT_URL=https://chat.example.com CHATWOOT_ACCOUNT_ID=1 CHATWOOT_HTTP_TOKEN=... uv run chatwoot-mcp stdio
```

Claude Code (`.mcp.json`) or Claude Desktop:

```json
{
  "mcpServers": {
    "chatwoot": {
      "command": "uv",
      "args": ["--directory", "/path/to/chatwoot/mcp", "run", "chatwoot-mcp", "stdio"],
      "env": { "CHATWOOT_URL": "https://chat.example.com", "CHATWOOT_ACCOUNT_ID": "1", "CHATWOOT_HTTP_TOKEN": "..." }
    }
  }
}
```

Hosted:

```bash
CHATWOOT_URL=http://chatwoot-web:3000 CHATWOOT_ACCOUNT_ID=1 MCP_HTTP_HOST=0.0.0.0 uv run chatwoot-mcp http
# Each MCP client then connects to http://<host>:8215/http/api-key/mcp with the header X-Chatwoot-Token: <their key>
```

Docker: `docker build -t chatwoot-mcp-server mcp/`, then run it with the same variables (the image serves `http`).

## Configuration

Settings are read once at start-up; a bad value stops the server with a plain message. `.env.example` lists them.

| Variable | Default | Purpose |
|---|---|---|
| `CHATWOOT_URL` | required | Chatwoot's address, no path. Hosted next to Chatwoot, use its internal address |
| `CHATWOOT_ACCOUNT_ID` | required | The one account the tools work in. Tools never take an account id |
| `CHATWOOT_HTTP_TOKEN` | | The key for stdio, or for `/mcp` with the shared-key opt-in |
| `CHATWOOT_PUBLIC_URL` | `CHATWOOT_URL` | Base URL for the `url` links in results |
| `CHATWOOT_READ_ONLY` | `false` | `true` registers no tool that changes anything |
| `CHATWOOT_ENABLE_<MODULE>` | on, except `REPLIES` and `MACROS` | Turn a tool module on or off (see the `module` column below) |
| `CHATWOOT_FILTER_TOOLS_BY_ROLE` | `false` | Hide tools the key's role cannot use (reports for agents). Platform mode always filters |
| `CHATWOOT_CONFIRM_TTL_SECONDS` | `600` | How long a confirmation token from a `prepare_*` tool stays valid |
| `CHATWOOT_MAX_ITEMS` | `25` | Most items a list returns |
| `CHATWOOT_MAX_TEXT_CHARS` | `2000` | Characters of message, note or article text kept |
| `CHATWOOT_TIMEOUT_SECONDS` / `CHATWOOT_CONNECT_TIMEOUT_SECONDS` | `30` / `10` | Chatwoot timeouts |
| `CHATWOOT_HTTP_SHARED_TOKEN_ROUTE` | `false` | Serve `/mcp` with `CHATWOOT_HTTP_TOKEN` in http mode |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `127.0.0.1` / `8215` | Where http mode listens (the Docker image sets `0.0.0.0`) |
| `MCP_LOG_LEVEL` | `INFO` | Logs go to stderr; request URLs are never logged |

Platform mode adds its own settings; see [docs/platform-mode.md](docs/platform-mode.md#configuration).

## Tools

Every tool carries all four MCP annotation hints and returns an `Error: ...` string instead of raising. Text that a
customer, visitor or user wrote (messages, names, emails, phones, notes, survey feedback, custom attribute text,
agent names and canned responses) is
wrapped in `<untrusted_content source=... id=...>` with every `<` and `&` escaped inside, so it is read as data,
never as instructions. Reports need a Chatwoot administrator (or a custom role with `report_manage`); everything else
needs an agent. `get_me` is always on.

Read:

| Tool | Module | Description |
|---|---|---|
| `get_me` | always | Who this connection acts as, their role and which account the tools work in. Never shows a key. |
| `list_inboxes` / `get_inbox` | reference | Inboxes with their ids; one inbox's greeting, hours and assignment settings. |
| `list_agents` / `list_teams` / `list_team_members` | reference | Staff, teams and who is in a team. |
| `list_labels` / `list_canned_responses` | reference | Label titles; saved quick replies. |
| `list_custom_attribute_definitions` | reference | Custom fields for chats or customers, with their keys and allowed values. |
| `get_account` | reference | The account's name, language, status and support address. |
| `list_conversations` | conversations | Chats by status, assignee, inbox, team or label (any of them), newest activity first. |
| `search_conversations` | conversations | Chats whose messages contain some words. Chatwoot searches every status, so a status filter is applied to each page of matches. |
| `filter_conversations` | conversations | Chats by conditions joined by all or any: status, priority, assignee, team, inbox, labels, dates (including "no activity for N days"), subject or a custom field. |
| `get_conversation` | conversations | One chat: status, customer, assignee, team, labels, custom fields, last message, link. |
| `get_conversation_counts` | conversations | Mine, assigned, unassigned and all. |
| `list_conversation_attachments` | conversations | Files sent in a chat: type, size, sender, 25 per page. No download links. |
| `get_messages` | messages | A chat's messages, oldest first; the latest 20 by default, `before`/`after` to page. |
| `search_contacts` / `list_contacts` | contacts | Find customers by name, email, phone or identifier; browse them by label. |
| `get_contact` / `get_contact_conversations` / `get_contact_notes` | contacts | One customer, their latest 25 chats, and the team's notes about them (paged). |
| `get_conversation_labels` / `get_contact_labels` | labels | The labels on a chat or a customer. |
| `search_help_articles` | help_center | Help-centre articles; drafts are included and marked by `status`. |
| `get_report_summary` | reports | Counts, reply and resolution times for a period, with the previous period for comparison. |
| `get_report_by_group` | reports | The same split by agent, team, inbox, label or channel. |
| `get_first_response_distribution` | reports | How quickly first replies went out, in time bands per channel. |
| `list_csat_responses` | reports | Satisfaction-survey answers: score and the customer's words, by score, agent, inbox or team. |
| `list_macros` | macros (opt-in) | Macros the key's owner can run, with their steps. |

Write (absent when `CHATWOOT_READ_ONLY=true`; none of these reach the customer, so they run at once; resolving can, so it is a [two-step tool](#two-step-tools)):

| Tool | Module | Description |
|---|---|---|
| `assign_conversation` | conversations | Assign to an agent, a team or both. The team is set first, because changing it drops an agent who is not in it. |
| `set_conversation_status` | conversations | Open, pending or snoozed (with a wake time). Opening also assigns the chat to the key's owner when their role is agent (Chatwoot's rule). |
| `set_conversation_priority` | conversations | None, low, medium, high or urgent. |
| `set_conversation_custom_attributes` | conversations | Fill in custom fields; only defined fields are accepted and the others are kept. |
| `add_private_note` | messages | An internal note. Refuses to report success unless Chatwoot marked it private. |
| `create_contact` / `update_contact` | contacts | Create a customer (with an email, phone or identifier), or change only the fields given. |
| `add_contact_note` | contacts | A note about a customer, shown to the team beside every chat with them. |
| `add_conversation_labels` / `add_contact_labels` | labels | Add existing labels; others stay; unknown labels are refused instead of created. |
| `remove_conversation_labels` | labels | Take labels off a chat; others stay. |

### Two-step tools

A message to a customer cannot be unsent, and a macro can do several things at once, so these modules are **off by
default** (`CHATWOOT_ENABLE_REPLIES=true`, `CHATWOOT_ENABLE_MACROS=true`) and every action takes two calls.
Resolving a chat sends the customer a satisfaction survey when the inbox has one, so it takes two calls too, but it
is on with the other conversation writes. The
`prepare_*` tool changes nothing: it looks up what the action would touch and returns a preview and a
`confirmation_token`. The assistant shows the preview to the user, and only after they agree calls the matching tool
with the token and the same arguments. A token is bound to the caller, the action and the exact arguments, works
once and expires (`CHATWOOT_CONFIRM_TTL_SECONDS`).

| Tool | Module | Description |
|---|---|---|
| `prepare_resolve_conversation` | conversations | Preview resolving a chat, with a warning when the inbox will send the customer a satisfaction survey. |
| `resolve_conversation` | conversations | Resolve the approved chat. |
| `prepare_send_reply` | replies | Preview a reply: who it goes to, on which channel. Refused if the chat cannot be replied to. |
| `send_reply` | replies | Send the approved reply. Refuses to report success if Chatwoot marked it private; a failure says whether it went. |
| `prepare_create_conversation` | replies | Preview a new chat with a customer in an inbox, with an optional first message. |
| `create_conversation` | replies | Start the approved chat. |
| `prepare_run_macro` | macros | Preview every step of a macro on one chat, with a warning for steps that message the customer, send a file or transcript, or call a webhook. |
| `run_macro` | macros | Run the approved macro. Chatwoot runs it in the background. |

## Security notes

- The key goes to Chatwoot only in its `api_access_token` header. It never appears in an error (Chatwoot's own error
  text is scrubbed of it), a tool result or a log.
- The client refuses cookies, never follows redirects, ignores proxy settings from the environment, and raises
  before sending any `X-Auth-Request-*` header: an SSO-fronted Chatwoot trusts those as proof of identity.
- Tools never take an account id; the account comes from `CHATWOOT_ACCOUNT_ID`.
- `get_me` returns an allow-list of fields: Chatwoot's profile also holds the user's access and pubsub tokens.
- Request URLs are never logged, because a customer search puts an email or phone number in the URL.
- Confirmations are stored under a hash of the token, never the token. Platform mode keeps them, and the cached
  keys, Fernet-encrypted in Valkey.
- See [SECURITY.md](SECURITY.md) for reporting and deployment notes.

## Development

```bash
uv sync --all-extras
uv run ruff format . && uv run ruff check .
uv run pytest --cov=chatwoot_mcp --cov-fail-under=80
docker build --platform linux/amd64 .
```

The tests use a pretend Chatwoot (`httpx.MockTransport`) and an in-memory MCP client, so no network is needed.
`tests/test_tools.py` checks that every tool has its hints and tags and that the tables above list exactly the tools
that exist. See [CLAUDE.md](CLAUDE.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT.
