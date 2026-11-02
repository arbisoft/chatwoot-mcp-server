# Chatwoot and its MCP server on the devkit

Runs this Chatwoot fork and its MCP server behind [Pressingly/mpass-sso-devkit](https://github.com/Pressingly/mpass-sso-devkit),
the local copy of the platform's mPass SSO edge. Clone the devkit next to this repository, so it can build from
`../chatwoot`, and copy `docker-compose.chatwoot.yml` and `setup-chatwoot.sh` into it:

```bash
git clone https://github.com/Pressingly/mpass-sso-devkit.git ../mpass-sso-devkit
cp mcp/devkit/docker-compose.chatwoot.yml mcp/devkit/setup-chatwoot.sh ../mpass-sso-devkit/
```

The QR login is the Moneta **sandbox**: scan it with the sandbox mPass app, activated with a sandbox activation (a
production activation does not work there).

## Once

1. Install Docker Desktop and mkcert (`brew install mkcert && mkcert -install`).
2. `./setup.sh`, then set `OIDC_CLIENT_SECRET` in `.env` (sent separately).
3. `./setup-chatwoot.sh`, and add the hosts line it prints.

## Run

```bash
docker compose -f docker-compose.yml -f docker-compose.chatwoot.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.chatwoot.yml logs -f chatwoot
```

The first build takes a while (Chatwoot's image compiles its assets). The first boot creates the database and one
account, "Devkit", which every mPass user joins as an agent.

## Check

1. `https://whoami.foss.local.dev`: scan the QR code and see the `X-Auth-Request-*` headers. This proves the edge works.
2. `https://chatwoot.foss.local.dev` in a private window: you land in Chatwoot as `<mpass-id>@askii.ai`, an agent in
   "Devkit". Then work through the README's "Test your app" list.
3. The MCP server: `https://chat-mcp.foss.local.dev/healthz` answers `ok`. Logging in to it from an MCP client
   (`https://chat-mcp.foss.local.dev/mcp`) also needs `https://chat-mcp.foss.local.dev/auth/callback` allowed on the
   Cognito client; until it is, that login fails with a redirect mismatch.

To make yourself an administrator (for the reports tools):

```bash
docker compose -f docker-compose.yml -f docker-compose.chatwoot.yml exec chatwoot \
  bundle exec rails runner 'AccountUser.where(account_id: 1).update_all(role: :administrator)'
```

## Clean up

```bash
docker compose -f docker-compose.yml -f docker-compose.chatwoot.yml down      # keep the data
docker compose -f docker-compose.yml -f docker-compose.chatwoot.yml down -v   # delete it too
```
