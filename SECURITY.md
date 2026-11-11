# Security Policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue or pull request: use GitHub private vulnerability
reporting on the repository's **Security** tab (**Report a vulnerability**), or ask a maintainer for a private channel
without giving details.

Include what you need for us to reproduce it: the affected version or commit, the mode (stdio, HTTP, platform), the
Chatwoot version, and the steps. We aim to acknowledge a report within three business days and keep you updated until
it is fixed. Please give us a reasonable time to release a fix before disclosing publicly.

## In scope

- Any way to act in Chatwoot as a different user, or with more permissions than the caller's own key allows.
- Leaking a key, a session, or another user's data across requests.
- Sending `X-Auth-Request-*` or other trusted identity headers to Chatwoot outside the platform-mode handoff.
- Reaching a customer without a confirmation step, or bypassing a disabled tool flag or `CHATWOOT_READ_ONLY`.

## Known Chatwoot behaviour

- **`X-Auth-Request-Email` is trusted on `/auth/sso/proxy-login`** when Chatwoot runs with `AUTH_TYPE=SSO`: it hands
  out a login for that user. This is why the steady-state client refuses to send any `X-Auth-Request-*` header,
  `/http/api-key/mcp` strips them on arrival, and the internal URL must stay off untrusted networks.
- **Chatwoot answers 401 for role refusals too** (`You are not authorized to do this action`), not only for bad keys.
  The server reads the text to tell them apart, so a role refusal never triggers a key re-fetch.
- **Personal keys never expire and cannot be narrowed.** A key works with the user's full role until they reset it.

## Trust boundaries

Everything that crosses into the server is checked where it enters; nothing downstream re-checks it.

| Boundary | Trusted? | What happens there |
|---|---|---|
| MCP client to server (tool arguments) | No | One middleware refuses control characters, strings over 10,000 characters and integers outside Chatwoot's 32-bit ids for every tool; each tool's schema then checks its own types, ranges, lengths and enums. HTTP bodies over 1 MiB are refused before they are read. |
| MCP client to server (headers) | No | `X-Auth-Request-*` headers are stripped on `/http/api-key`, and the Chatwoot client refuses to send any. Platform mode trusts only a FastMCP-verified bearer. |
| Chatwoot answers | Partly | Parsed into tolerant models; any text a customer, visitor or user can write is wrapped in `<untrusted_content>` before the model sees it. |
| Cognito / mpass-auth-proxy | Only once verified | The id_token comes from the token endpoint, never from the MCP client, and its RS256 signature and audience are checked against Cognito's JWKS (or mpass-auth-proxy's when it re-signs with email capture on); its `sub` must match the verified access token's. `email` is trusted only when verified or written by the overlay. |
| Valkey | Not with secrets | Keys, OAuth state and confirmations are Fernet-encrypted, so a reader of Valkey cannot use or forge them; the plain re-check and refusal markers hold no secrets. |
| Environment | Yes | Every setting is validated at start-up; a typo stops the server. |

## Least privilege

- **Each call uses the caller's own Chatwoot key**, so the server can never do more than that user can; there is no
  shared admin key. The OAuth scope requested from Cognito is `openid` only.
- **Tools follow the role:** report tools are hidden from agents, and `CHATWOOT_READ_ONLY=true` registers no write
  tool at all. Replies and macros are off unless switched on.
- **The container** runs as an unprivileged user, cannot rewrite its own code (owned by root), and needs no shell
  tools. Run it with `read_only: true`, `cap_drop: [ALL]` and `no-new-privileges` (as `devkit/` does). Platform mode
  then needs `MCP_OAUTH_STORAGE_URL`, since without Valkey FastMCP keeps its OAuth state in files.
- **CORS** allows only the methods and headers the MCP transport uses, and no cookies; with `MCP_ENV=production` no
  origin is allowed unless `MCP_ALLOWED_ORIGINS` names it.
- **Client redirects** are limited to each known MCP client's callback path; others are added with
  `MCP_ALLOWED_CLIENT_REDIRECT_URIS`. FastMCP's consent screen stays on.
- **Base images** are pinned by digest and updated through Dependabot.

## Deployment notes

- **The shared-key route acts as the key's owner.** `/mcp` is only served in http mode when
  `CHATWOOT_HTTP_SHARED_TOKEN_ROUTE=true`, and then every caller who can reach the port works in Chatwoot as the owner
  of `CHATWOOT_HTTP_TOKEN`. The server refuses to start in http mode with the key set and the flag off. Multi-user
  deployments should use `/http/api-key/mcp`, where each caller sends their own key, or platform mode.
- **Platform mode does not serve the personal-key route by default.** `/http/api-key/mcp` replays the caller's
  `X-Chatwoot-Token` to the internal Chatwoot URL, which skips the SSO proxy and the corporate-ID gate. It is only
  mounted when `CHATWOOT_HTTP_API_KEY_ROUTE=true`, with a startup warning. Do not rely on a router rule to hide it
  instead: a path-prefix exclusion can be bypassed with an encoded path such as `/http%2Fapi-key/mcp`.
- **Writes are on unless `CHATWOOT_READ_ONLY=true`.** Of the default writes, only resolving a chat can reach the
  customer (Chatwoot may send a satisfaction survey), so it goes through a confirmation step. Replies and macros reach
  the customer directly; they are separate modules, off by default, also behind a confirmation step.
- **Who approves a confirmation.** When the client supports MCP elicitation (stdio and stateful clients), the
  confirming tool asks the person in the client's own prompt, which the model cannot answer. Otherwise, including
  stateless HTTP, the model holds the token and the approval rests on it showing the preview: text in
  `<untrusted_content>` reduces, but cannot rule out, prompt injection that makes a model confirm on its own.

## Out of scope

- Prompt injection that only makes the model misuse tools the user already holds, unless it bypasses a confirmation
  step, a disabled flag or read-only mode.
- Issues in Chatwoot itself; report those to the Chatwoot project.
