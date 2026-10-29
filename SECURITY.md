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

## Deployment notes

- **The shared-key route acts as the key's owner.** `/mcp` is only served in http mode when
  `CHATWOOT_HTTP_SHARED_TOKEN_ROUTE=true`, and then every caller who can reach the port works in Chatwoot as the owner
  of `CHATWOOT_HTTP_TOKEN`. The server refuses to start in http mode with the key set and the flag off. Multi-user
  deployments should use `/http/api-key/mcp`, where each caller sends their own key, or platform mode.
- **Platform mode does not serve the personal-key route by default.** `/http/api-key/mcp` replays the caller's
  `X-Chatwoot-Token` to the internal Chatwoot URL, which skips the SSO proxy and the corporate-ID gate. It is only
  mounted when `CHATWOOT_HTTP_API_KEY_ROUTE=true`, with a startup warning. Do not rely on a router rule to hide it
  instead: a path-prefix exclusion can be bypassed with an encoded path such as `/http%2Fapi-key/mcp`.
- **Writes are on unless `CHATWOOT_READ_ONLY=true`.** None of them reach a customer; the ones that do (replies,
  macros) are separate modules, off by default, behind a confirmation step.

## Out of scope

- Prompt injection that only makes the model misuse tools the user already holds, unless it bypasses a confirmation
  step, a disabled flag or read-only mode.
- Issues in Chatwoot itself; report those to the Chatwoot project.
