# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Reporting a bug

A useful report includes:

- the server version or commit, and the mode (stdio, HTTP, platform);
- the Chatwoot version, and whether it is the Enterprise edition;
- the MCP client (Claude Code, Claude Desktop, Claude.ai, MCP Inspector, ...);
- the smallest steps that reproduce it, with keys and personal data removed.

Security issues go through [SECURITY.md](SECURITY.md), not public issues.

## Development setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Work in the `mcp/` folder.

```bash
uv sync --all-extras
uv run ruff format .
uv run ruff check .
uv run pytest --cov=chatwoot_mcp --cov-fail-under=80
```

CI runs the same checks on every pull request that touches `mcp/`. Tests must not need a live Chatwoot: use the
`FakeChatwoot` transport in `tests/conftest.py`.

## Branches

| Branch | Role |
|---|---|
| `foss-sandbox` | Default branch and staging. Every change lands here first. |
| `foss-main` | Production. Updated only by a promotion PR from `foss-sandbox` (merge commit, never squash). |

Open your pull request against `foss-sandbox`.

## Commits and pull requests

- Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): subject`,
  imperative, lowercase, no trailing period, e.g. `feat(mcp): add search_contacts`.
- No AI attribution in commits or pull requests.
- Keep pull requests small and focused, with tests for new behaviour.
- Every new tool is registered through `context.tool(...)` with a spec, returns an `Error: ...` string instead of
  raising, and wraps text that customers or users wrote as untrusted content.
