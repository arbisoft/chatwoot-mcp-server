# CLAUDE.md: mcp/ (chatwoot-mcp-server)

Guidance for Claude Code when working in this folder. The layout and conventions follow
`Pressingly/zammad-mcp-server` (branch `foss-sandbox`); keep them aligned.

## Project

`chatwoot-mcp-server`: an MIT-licensed MCP server for Chatwoot, built on FastMCP. Every Chatwoot call carries the
caller's own key in `api_access_token`, never a shared admin key. It lives in `mcp/` inside the Chatwoot fork but is
its own Python project: it must never import from the Rails app.

**No changes to Chatwoot are allowed.** Every tool calls an API route that already exists. Check a route in
`swagger/swagger.json`, `config/routes.rb` and `app/views/api/` (repository root) before adding a tool.

- `chatwoot_mcp/__main__.py`: `chatwoot-mcp stdio|http`; `http` hands over to `platform/` when
  `COGNITO_USER_POOL_ID` is set.
- `chatwoot_mcp/server.py`: `build_server(settings)` registers tools; `build_http_app(mcp, settings)` serves
  `/http/api-key/mcp`, `GET /healthz`, and `/mcp` only with `CHATWOOT_HTTP_SHARED_TOKEN_ROUTE=true`.
- `chatwoot_mcp/client/`: the async Chatwoot client (`http.py`), its errors and `to_tool_error` (`errors.py`), and
  tolerant pydantic views of Chatwoot objects (`models.py`). It refuses cookies, never follows redirects, raises if
  an `X-Auth-Request-*` header is about to be sent, and scrubs the key from Chatwoot's error text. Keep all four.
- `chatwoot_mcp/credentials/`: where each request's key comes from (`CHATWOOT_HTTP_TOKEN`, or `X-Chatwoot-Token` on
  `/http/api-key/mcp`), and the owner's role in `CHATWOOT_ACCOUNT_ID` from `GET /api/v1/profile`.
- `chatwoot_mcp/platform/`: Cognito OAuth plus each user's own key, fetched through Chatwoot's SSO handoff. See
  `docs/platform-mode.md`.
- `chatwoot_mcp/tiers.py`: `tier:none|agent|admin` and the one `auth=` check that filters tools by tier.
- `chatwoot_mcp/shaping.py`: compact output, `<untrusted_content>` framing, truncation, web UI links.
  `confirmations.py`: two-step confirm tokens.
- `chatwoot_mcp/tools/`: one module per Chatwoot area, each with `register(mcp, context)`; `context.py` holds the
  session, the specs and the confirm helpers.

## Commands

```bash
uv sync --all-extras
uv run ruff format .
uv run ruff check .
uv run pytest --cov=chatwoot_mcp --cov-fail-under=80
docker build --platform linux/amd64 .
```

## Isolation rule

- The community core (`client/`, `credentials/`, `tools/`, `server.py`) works against any Chatwoot and imports
  nothing from `platform/`.
- Platform-only logic (Cognito, key fetching, Valkey storage) lives in `platform/` and is reached by a one-line hook,
  only when `COGNITO_USER_POOL_ID` is set.
- Disabled tool modules never register. Each module has one `CHATWOOT_ENABLE_*` flag, read through
  `env_flags.env_flag`; `replies` and `macros` are off by default. `CHATWOOT_READ_ONLY` keeps every write unregistered.

## Tool conventions

- `snake_case` verb_noun names. Tools never take an account id.
- Register through `@mcp.tool(**context.tool(title, <spec>, module=..., tier=...))` with a spec from
  `tools/context.py` (`READ`, `CREATE`, `ADD`, `OVERWRITE`, `PREPARE`, `IRREVERSIBLE`): it sets the title, all four
  `ToolAnnotations` hints, the `tier:*`, `module:*` and read/write tags and the tier check. `tests/test_tools.py`
  enforces it.
- Tools return an `Error: ...` string instead of raising: catch `ChatwootError` and return `to_tool_error(error)`.
- Validate responses with `Model.parse(...)` / `parse_rows(...)` inside the tool's `try`, so an odd payload becomes an
  `Error:` string. Shape the result in `shaping.py`; never return Chatwoot's raw payload.
- Text a customer, visitor or user wrote is untrusted: return it through `shaping.untrusted_field` or
  `frame_untrusted`, never raw. Names only administrators write (inboxes, teams, labels, macros) are returned as is.
- Anything that reaches the customer or runs many steps (replies, macros) is a `prepare_*` tool plus a confirming
  tool, through `prepared(...)` and `confirmation_refusal(...)` in `tools/context.py`, and its module is off by
  default. Validate and look things up in the `prepare_*` tool, so a doomed action never gets a token. A failed send
  says whether it surely did not go, may have gone, or went (`replies.send_failure`).
- `get_me` returns an allow-list of fields: the profile holds the user's access and pubsub tokens.
- Do not log request URLs (`httpx` is set to WARNING): a customer search puts an email or phone in the URL.
- Update the README tool tables with every tool change; `tests/test_tools.py` checks they match.

## Chatwoot facts worth knowing

- Account routes are `/api/v1/accounts/{id}/...`; reports are `/api/v2/...`; the profile is `/api/v1/profile`.
- Chatwoot answers **401** both for a bad key (`Invalid Access Token`) and for a role refusal
  (`You are not authorized to do this action`); `error_for_status` reads the text.
- `POST .../labels` and `POST .../custom_attributes` **replace** the whole set, so writes read the current values
  first and send the result. Label titles are stored lower-case; unknown labels are refused instead of created.
- `toggle_status` reads `snoozed_until` with `strptime('%s')`: send Unix seconds. An ISO string parses as 1970.
- Changing a chat's team drops an assignee who is not in it, so `assign_conversation` sets the team first.
- Opening a chat assigns it to the key's owner when their role is agent (not administrator).
- The `labels` filter on chat and customer lists matches *any* of the labels.
- Macros run in a background job (Sidekiq): `execute` answers at once.
- Reports and CSAT need an administrator, or an Enterprise custom role with `report_manage`.
- Report timestamps are Unix seconds; the tools take `YYYY-MM-DD` dates (UTC). Some report routes exist only in the
  Enterprise edition or in some versions.
- Under Moneta SSO the public host puts a login in front of `/api`, so a hosted server calls the internal URL.
- Attachment and avatar URLs need `FRONTEND_URL` set in Chatwoot; without it those responses fail with a 500.

## Commits and branch policy

- Conventional Commits: `type(scope): subject`, imperative, lowercase. No AI attribution anywhere: no `Co-Authored-By`
  trailer and no "Generated with" line, in commits or PRs.
- Feature branch, then a pull request into `foss-sandbox`, then a promotion pull request from `foss-sandbox` into
  `foss-main` (merge commit, never squash). Never push directly to either.
