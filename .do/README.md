# Deploying picx-mcp to DigitalOcean App Platform

## Prerequisites

- `doctl` CLI installed and authenticated
- Access to the Type-Think-AI GitHub org (for repo access grant)
- DNS control for `picxstudio.com` (to add the CNAME)

## 1. Authenticate

```bash
doctl auth init
```

## 2. Generate secrets

```bash
# REQUEST_STATE_KEY — shared HMAC key for interactive round-trip validation.
# MUST be identical across all replicas.
python -c "import secrets; print(secrets.token_hex(32))"

# JWT_SIGNING_KEY — explicit JWT signing. Without it, rotating the OAuth
# client secret invalidates every issued token.
python -c "import secrets; print(secrets.token_hex(32))"

# STORAGE_ENCRYPTION_KEY — Fernet key for encrypting stored OAuth tokens.
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 3. Validate the spec

```bash
doctl apps spec validate .do/app.yaml
```

Fix any schema warnings before proceeding.

## 4. Create the app

```bash
doctl apps create --spec .do/app.yaml --wait
```

The `--wait` flag blocks until the first deploy completes or fails.

## 5. Set secret values

The spec ships with `REPLACE_ME` placeholders. After creation, update them
through the DigitalOcean console (Apps → picx-mcp → Settings → Environment)
or via CLI:

```bash
APP_ID=$(doctl apps list --format ID --no-header | head -1)

doctl apps update $APP_ID --spec <(
  doctl apps spec get $APP_ID |
  sed 's/REPLACE_ME/<actual-value>/'
)
```

Or edit in the console UI — secrets are encrypted at rest and never exposed
in logs.

### Which values to use

| Env var | Source |
|---------|--------|
| `REQUEST_STATE_KEY` | Generate fresh (step 2) |
| `JWT_SIGNING_KEY` | Generate fresh (step 2) |
| `STORAGE_ENCRYPTION_KEY` | Generate fresh (step 2) |
| `GOOGLE_CLIENT_ID` | Copy from PicX API's `GOOGLE_OAUTH_CLIENT_ID` |
| `GOOGLE_CLIENT_SECRET` | Copy from PicX API's `GOOGLE_OAUTH_CLIENT_SECRET` |

⚠️ **Name difference**: the PicX API stores these as `GOOGLE_OAUTH_CLIENT_ID` /
`GOOGLE_OAUTH_CLIENT_SECRET`. Here they are `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.

## 6. Watch the deploy

```bash
# List recent deployments
doctl apps list-deployments $APP_ID

# Stream build logs
DEPLOY_ID=$(doctl apps list-deployments $APP_ID --format ID --no-header | head -1)
doctl apps logs $APP_ID --type build --deployment $DEPLOY_ID --follow

# Stream runtime logs
doctl apps logs $APP_ID --type run --follow
```

## 7. Add the DNS record

After the app is live, App Platform provides an onboarding domain like
`picx-mcp-xxxxx.ondigitalocean.app`. Add a CNAME for the custom domain:

```
mcp.picxstudio.com.  CNAME  picx-mcp-xxxxx.ondigitalocean.app.
```

The exact target is shown in **Apps → picx-mcp → Settings → Domains** after
the app is created. Managed TLS provisions automatically once the CNAME
resolves.

Alternatively, if using Cloudflare DNS (likely), add the CNAME there with
proxy OFF (DNS-only / gray cloud) — App Platform needs to terminate TLS itself
for managed certs.

## 8. Verify

```bash
curl https://mcp.picxstudio.com/health
# → {"status":"healthy","service":"picx-mcp","tools":N}
```

## Rolling back

```bash
# List deployments
doctl apps list-deployments $APP_ID --format ID,Phase,Progress --no-header

# Rollback to a previous deployment
doctl apps create-deployment $APP_ID --force-rebuild
```

There is no native `doctl apps rollback` — redeploy from a known-good commit
by pushing a revert or by using `doctl apps update` with a pinned commit SHA
in the spec's `github.branch` field temporarily.

## Architecture notes

- **Stateless HTTP**: the server runs `stateless_http=True`. MCP clients (Cursor,
  Claude Code) use `fetch()` and do not forward `Set-Cookie`, so sticky sessions
  are impossible. Two instances proves the architecture works without affinity.
- **REQUEST_STATE_KEY**: the HMAC key that validates interactive round-trips
  across replicas. App Platform shares env vars across all instances of a service,
  so this works out of the box — but the key MUST be set.
- **Redis/Valkey**: backs tasks, cache, OAuth storage. Not optional with >1 replica.
- **Root domain**: OAuth `.well-known` routes live at root. No path prefix.
