# Platform mode

Platform mode serves Chatwoot to MCP clients that sign in with OAuth (AWS Cognito, optionally behind an mPass-style
auth proxy). Each signed-in user works in Chatwoot with their own Chatwoot key, bounded by their own Chatwoot role.
Nobody shares a key, and there is no fallback to one.

It needs the Chatwoot fork running with `AUTH_TYPE=SSO` (its `Sso::ProxyLoginController` and `Mpass::ProxyIdentity`)
and reachable on an internal URL that the public SSO proxy does not front. Install the `[platform]` extra (the Docker
image includes it) and set `COGNITO_USER_POOL_ID`; `chatwoot-mcp http` then runs platform mode. `stdio` is
unaffected.

## Routes

| Route | Auth | Credential |
|---|---|---|
| `/mcp` | FastMCP's Cognito provider (OAuth 2.0 with dynamic client registration and RFC 8414/9728 discovery) | The caller's own key, fetched as below |
| `/authorize`, `/auth/callback`, `/token`, `/register`, `/.well-known/*` | Public OAuth endpoints | n/a |
| `/http/api-key/mcp` | None at the MCP layer; **only mounted with `CHATWOOT_HTTP_API_KEY_ROUTE=true`** | The caller's own `X-Chatwoot-Token`; `X-Auth-Request-*` headers are stripped |
| `/healthz` | None | n/a |

Register `<MCP_BASE_URL>/auth/callback` as a callback URL on the Cognito app client. The router in front of the server
must not add an SSO ForwardAuth to these paths: the Cognito provider is the only auth layer on `/mcp`.

### The personal-key route is off by default

`/http/api-key/mcp` sends whatever `X-Chatwoot-Token` the caller supplies to `CHATWOOT_INTERNAL_BASE_URL`. That URL is
deliberately not behind the SSO proxy, so the request skips mPass/oauth2-proxy and the corporate-ID gate: a personal
key that belongs to a leaver, or one that leaked, keeps working from anywhere that reaches this server. In platform
mode the route is therefore not mounted at all, and startup logs one line saying so. Requests to it, including
encoded spellings such as `/http%2Fapi-key/mcp`, fall through to the OAuth app and get a 404.

Set `CHATWOOT_HTTP_API_KEY_ROUTE=true` only for a deployment that accepts that bypass; startup then logs a WARNING.

## How a user's key is fetched

Chatwoot gives every user one permanent personal key and has no API to mint a narrower or expiring one. So instead of
minting, the server fetches that key the same way the browser login does, with no Platform app token and no change to
Chatwoot. On a user's first call, or when their cached key needs checking, it opens a fresh httpx client of its own
(no cookies, no redirects followed) against `CHATWOOT_INTERNAL_BASE_URL` and:

1. Sends `GET /auth/sso/proxy-login` with `X-Auth-Request-Email: <identity>` and, when the session has one,
   `X-Auth-Request-Access-Token: <upstream Cognito access token>` for the corporate-ID gate (`SMB_CORPORATE_ID`).
   Chatwoot resolves or provisions the user, joins them to the account as an agent (never an administrator), and
   answers `302` to `/app/login?email=...&sso_auth_token=...`: a five-minute, single-use handoff token. A `404` means
   Chatwoot is not in SSO mode; a `403` means the corporate gate refused.
2. Sends `POST /auth/sign_in` with that email and token, exactly as the login page does. Chatwoot answers with the
   user, including their `access_token`, and opens a devise_token_auth session.
3. Always, even when an earlier step failed: `DELETE /auth/sign_out` with that session's `access-token`, `client` and
   `uid`, so the session it opened does not linger.
4. Reads `GET /api/v1/profile` with the key: the user's role in `CHATWOOT_ACCOUNT_ID`. A user who is not a member is
   refused.

The key is never reset or revoked: it is the same key the user may use elsewhere, and resetting it would break
those.

The identity is the id_token's `email` (lowercased), or `cognito:username` when there is no email. An unverified
platform user carries exactly `<cognito:username>@<DEFAULT_EMAIL_DOMAIN>`, and Chatwoot's `Mpass::ProxyIdentity`
turns a value without `@` into `<value>@DEFAULT_EMAIL_DOMAIN`.

What happens to those users follows `LAUNCHPAD_EMAIL_CAPTURE`, the same setting mpass-auth-proxy and the launchpad
read:

- **Off** (the default, and the platform today): every user has the synthetic address, and it is the account the
  web login uses too. The server hands it to Chatwoot like any other identity.
- **On**: the launchpad verify-gate holds unverified users at the edge until their app accounts are relinked to the
  verified address. The MCP does not pass the edge, so it refuses both shapes before sending anything to Chatwoot
  and tells the user to verify their email in the portal first; otherwise its first request would create the
  duplicate account the relink later collides with. Any other address in that domain is accepted only from a token
  mpass-auth-proxy's email overlay stamped (`preferred_username == cognito:username`).

## Cache, re-checks and role changes

- The key is cached under `chatwoot-mcp:key:v1:<namespace>:<sha256(identity)>` for a week, Fernet-encrypted with a key
  derived from `OIDC_CLIENT_SECRET` (or `MCP_JWT_SIGNING_KEY`). The namespace is a short hash of `OIDC_CLIENT_ID` and
  `CHATWOOT_INTERNAL_BASE_URL`, so two deployments sharing one Valkey database never read each other's keys. The value
  holds the key, the account permissions, the tier and when it was last checked.
- Every four hours the whole handoff runs again, so a user removed from the corporate ID or from the account loses
  access within four hours, and a role change is picked up. A key on its own does not pass the corporate gate.
- A role refusal from Chatwoot (a `403`, or its `401 You are not authorized to do this action`) sets a "due" flag,
  behind a `SET NX EX` limiter (at most once per five minutes per identity); the next call re-checks.
- A `401` with a rejected key (the user reset it) drops the cache entry, fetches once and retries the request once.
- A denial while fetching (corporate gate, handoff error, not a member) drops the cache entry and fails the request.
- Any cache or fetch error fails the request with an `Error: ...` message. Nothing falls back to another key. The one
  exception is an entry that no longer decrypts (a rotated secret): it is treated as absent and fetched again.
- Every decision writes one JSON line on the `chatwoot_mcp.audit` logger: `key.fetched` (with the reason),
  `key.recheck_unchanged`, `key.recheck_requested`, `key.renew_reused`, `key.denied` and `key.refused`. Each carries
  the identity hash and, where there is a key, its tier and permissions. Never the key or the email.

## Tool visibility

`tools/list` is filtered per user by tier: administrators (and custom roles with `report_manage`) see every tool,
agents see everything except reports, and a user with no membership in the account sees only `get_me`. The tier is
memoised in process for 60 seconds per identity.

Unlike community mode, platform mode fails closed: if the identity is missing, the fetch fails or anything raises,
the caller is treated as having no tier and only `get_me` stays visible, so calling it shows why. Chatwoot still
enforces every permission; the filter only keeps the model from offering tools that would fail.

## Storage

With `MCP_OAUTH_STORAGE_URL` set (`redis://`, `rediss://`, `valkey://` or `valkeys://`, query string kept), one Valkey
database holds:

| Keys | Content |
|---|---|
| FastMCP collections | OAuth state (clients, transactions, codes, upstream tokens, JTI mappings), Fernet-encrypted |
| `chatwoot-mcp:key:v1:<namespace>:*` | Cached keys, Fernet-encrypted |
| `chatwoot-mcp:recheck-limit:v1:<namespace>:*` | Re-check limiter, 5-minute TTL |
| `chatwoot-mcp:recheck-due:v1:<namespace>:*` | Re-check flags, cleared by the next re-check |
| `chatwoot-mcp:confirm:v1:*` | Two-step confirmations, Fernet-encrypted |

Without `MCP_OAUTH_STORAGE_URL` everything lives in process: it is lost on restart and not shared between replicas.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `COGNITO_USER_POOL_ID` | required | Turns platform mode on |
| `COGNITO_AWS_REGION` | required | Cognito region |
| `OIDC_CLIENT_ID` | required | Cognito app client id |
| `OIDC_CLIENT_SECRET` | | Cognito app client secret (confidential client); also the encryption key material |
| `MCP_JWT_SIGNING_KEY` | | Signing key for public (PKCE) clients; needed when there is no client secret |
| `MCP_BASE_URL` | required | Public URL of this server, e.g. `https://support-mcp.example.com` |
| `CHATWOOT_INTERNAL_BASE_URL` | required | Chatwoot on the internal network, e.g. `http://chatwoot-web:3000`; every Chatwoot call goes here |
| `CHATWOOT_ACCOUNT_ID` | required | The account the tools work in |
| `DEFAULT_EMAIL_DOMAIN` | required | Synthetic domain of unverified users (a bare domain) |
| `LAUNCHPAD_EMAIL_CAPTURE` | `false` | Set it to the platform's value. `true` refuses unverified users (see above) |
| `CHATWOOT_PUBLIC_URL` | `CHATWOOT_URL` | Base URL for the `url` links in results |
| `MCP_OAUTH_STORAGE_URL` | unset | Valkey URL for OAuth state, the key cache and confirmations |
| `MCP_ALLOWED_CLIENT_REDIRECT_URIS` | built-in list | Comma-separated redirect URI patterns (fnmatch) added to the built-in list (`DEFAULT_REDIRECT_URIS` in `platform/settings.py`: localhost, Cursor, VS Code, Claude.ai, ChatGPT, Gemini), e.g. Askii's callback |
| `MCP_ALLOW_ANY_REDIRECT_URI` | `false` | `true` accepts any redirect URI, with a startup warning |
| `COGNITO_UPSTREAM_AUTH_URL` | discovered | Send users to an auth proxy's `/authorize` instead of Cognito's hosted UI |
| `COGNITO_UPSTREAM_TOKEN_URL` | discovered | Exchange codes through an auth proxy's `/token` |
| `MCP_OIDC_SCOPES` | `openid` | Upstream scopes, space- or comma-separated |
| `MCP_ACCESS_TOKEN_TTL_SECONDS` | `86400` | Lifetime of the token FastMCP issues to the MCP client |
| `MCP_ALLOWED_ORIGINS` | `*` | CORS origins |
| `MCP_LOG_LEVEL` | `INFO` | Log level; logs are JSON lines on stderr and audit lines are always kept |
| `MCP_ENV` | | `production` adds a note to the warning when storage is in process |
| `CHATWOOT_HTTP_API_KEY_ROUTE` | `false` | `true` mounts `/http/api-key/mcp` for personal keys, which bypasses the SSO proxy and the corporate-ID gate; logs a WARNING at startup |

The community settings (`CHATWOOT_ENABLE_*`, `CHATWOOT_READ_ONLY`, timeouts, `MCP_HTTP_PORT`) apply as usual.
`CHATWOOT_HTTP_TOKEN` and `CHATWOOT_HTTP_SHARED_TOKEN_ROUTE` are refused in platform mode.

## Security notes

- The steady-state client sends only `api_access_token`, never a cookie and never an `X-Auth-Request-*` header (a hard
  guard in the client). The identity headers appear only on the handoff `GET`.
- Any container on the internal network can send `X-Auth-Request-Email` to Chatwoot directly. That exposure exists
  without this server; keep the internal URL off untrusted networks.
- Logs never contain keys, session headers or bodies; cache keys are hashes of the identity.
