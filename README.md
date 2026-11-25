# StaticDeploy

StaticDeploy is a self-hostable, multi-tenant platform for **static** HTML, CSS,
and JavaScript sites. It deliberately does not run web applications or offer
databases. That constraint keeps the data plane inexpensive and makes deploys
immutable, auditable, and instant to roll back.

## What it does

- Creates sites with one or more custom domains and a per-site artifact quota.
- Copies a directory into a content-addressed immutable deploy store.
- Activates a deploy or rolls back with one SQLite pointer update.
- Serves sites by `Host` header with path-traversal protection, ETags, gzip, and
  correct immutable-asset cache headers.
- Switches between production subdomain routing and localhost-friendly path routing.
- Accepts signed Gitea push webhooks, queues deploy jobs, and runs a bounded
  Git checkout/build worker.
- Publishes non-primary branches as expiring preview deployments.
- Provides a token-protected JSON management API, dashboard, health endpoint,
  and Prometheus-compatible request metrics.
- Ships with Docker Compose and Caddy on-demand TLS. Caddy asks the control
  plane before it obtains a certificate, preventing arbitrary certificate use.

## Local development — no system Python changes

The control plane has no third-party Python runtime dependencies. Create a
virtual environment before installing its CLI; nothing is installed globally.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .

staticdeploy --data-dir ./var init
staticdeploy --data-dir ./var site create portfolio localhost
staticdeploy --data-dir ./var deploy create portfolio ./example-site
STATICDEPLOY_ADMIN_TOKEN=dev-token staticdeploy --data-dir ./var serve --port 8080
```

Visit `http://localhost:8080`. The control API requires
`Authorization: Bearer dev-token`; for example:

```bash
curl -H 'Authorization: Bearer dev-token' http://127.0.0.1:8080/_platform/api/sites
```

## Routing modes

`subdomain` is the default production mode. It resolves a site's custom domain
from the HTTP `Host` header; preview URLs use
`<branch>--<site>.<preview suffix>`. It requires DNS records and, for public
HTTPS, a real domain.

`path` is intended for local development and demos, where no domain is needed:

```bash
STATICDEPLOY_ROUTING_MODE=path staticdeploy --data-dir ./var serve --port 8080
```

With the example site deployed as `portfolio`, browse:

```text
http://localhost:8080/sites/portfolio/
http://localhost:8080/previews/portfolio/<branch>/
```

The management endpoints remain under `/_platform/` in both modes. Path mode
does not use custom-domain or wildcard-DNS routing. Sites used in path mode
must use relative asset URLs (for example, `assets/app.js`, not `/assets/app.js`)
or configure their static-site generator's base/public path as
`/sites/<site>/`; root-absolute URLs have no site identity to route from.

## Git deploys and previews

Create a Git-connected site. The command prints a webhook secret once; save it
in the Gitea or GitHub webhook configuration.

```bash
staticdeploy --data-dir ./var site create docs docs.example.com \
  --repository-url https://gitea.example.com/me/docs.git --branch main

# Gitea:  POST /_platform/webhooks/gitea/docs, event: push
# GitHub: POST /_platform/webhooks/github/docs, event: push
# In both cases, use the value printed during site creation as the secret.
staticdeploy --data-dir ./var worker --watch
```

`main` updates the production deploy. Other branches produce previews at
`<branch>--docs.<STATICDEPLOY_PREVIEW_SUFFIX>` and expire after 72 hours by
default. Run `staticdeploy --data-dir ./var cleanup` periodically to remove
expired preview mappings.

An optional `--build-command` is intended only for a static export. It runs
without a shell, has a timeout and POSIX memory/CPU limits, and publishes
`dist/` when present. For plain sites, omit it entirely.

## Production with Caddy

Copy `.env.example` to `.env`, use a long generated admin token, and set a
preview suffix whose wildcard DNS record points to the machine. Point every
customer domain at this server and forward public TCP ports 80 and 443.

```bash
docker compose up --build -d
```

The Caddy configuration uses its documented on-demand TLS `ask` callback to
check `/_platform/tls/allow`. It will only authorize domains already registered
in StaticDeploy. The first HTTPS handshake can take a few seconds while a
certificate is issued; future handshakes use the cached certificate.

For local-only use, skip Caddy and use the HTTP edge directly. For a home
network, public certificates require a reachable IP/port forwarding and DNS.

## Commands

```text
staticdeploy init
staticdeploy site create <slug> <domain> [--domain DOMAIN] [--repository-url URL]
staticdeploy site list
staticdeploy deploy create <slug> <directory>
staticdeploy deploy list <slug>
staticdeploy rollback <slug> <deploy-id>
staticdeploy worker [--watch]
staticdeploy cleanup
staticdeploy serve [--routing-mode subdomain|path] [--admin-token TOKEN] [--preview-suffix DOMAIN]
```

## Architecture

```text
Gitea push -> signed webhook -> SQLite deploy queue -> worker -> immutable files
                                                  |                  |
                                                  +---- main --------+-> active pointer
                                                  +---- branch ------+-> preview pointer

browser -> Caddy TLS/SNI -> StaticDeploy edge -> Host / preview lookup -> deploy files
```

## Security boundaries

This is an educational, single-node hosting platform, not a multi-region cloud.
Run it in containers with a non-privileged user and restrict who can create
sites. The Git worker is bounded but is not a replacement for a hardened VM or
microVM sandbox when accepting untrusted repositories. Never expose the admin
token or webhook secrets in a client-side site.
