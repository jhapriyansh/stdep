# StaticDeploy

StaticDeploy is a deliberately small, self-hostable control plane for plain HTML,
CSS, and JavaScript sites. It does **not** run application code or install site
dependencies. A deploy is an immutable, content-addressed snapshot; making it
live or rolling it back is a single SQLite pointer update.

## Current vertical slice

- Create sites and map one or more hostnames to them.
- Deploy a local directory into an immutable artifact store.
- Serve the active deploy by `Host` header.
- Roll back atomically to any retained deploy.
- Correct cache semantics: HTML is revalidated, fingerprinted assets are immutable.
- Defensive path resolution; request paths cannot escape a deploy directory.

The server is intentionally HTTP-only in this first slice. Put Nginx/Caddy in
front for production TLS/SNI while the control plane gains ACME automation.

## Run locally

No packages need to be installed. The project currently uses only Python's
standard library, so it cannot modify system Python packages.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .

staticdeploy --data-dir ./var init
staticdeploy --data-dir ./var site create portfolio localhost
staticdeploy --data-dir ./var deploy portfolio ./example-site
staticdeploy --data-dir ./var serve --port 8080
```

Then open `http://localhost:8080`. Use a custom host locally with, for example,
`curl -H 'Host: localhost' http://127.0.0.1:8080/`.

## Commands

```text
staticdeploy init
staticdeploy site create <slug> <domain> [--domain DOMAIN ...]
staticdeploy site list
staticdeploy deploy <slug> <directory>
staticdeploy deploy list <slug>
staticdeploy rollback <slug> <deploy-id>
staticdeploy serve [--host 127.0.0.1] [--port 8080]
```

## Roadmap

1. Signed Git webhook endpoint and queued deploy worker.
2. Branch preview hostnames and retention cleanup.
3. ACME DNS-01 automation, certificate lifecycle, and Nginx/Caddy reload hook.
4. Per-site quotas, authenticated control-plane API, and Prometheus metrics.

