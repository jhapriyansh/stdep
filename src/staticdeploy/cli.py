from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .server import serve
from .store import Store, StoreError
from .worker import Worker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="staticdeploy", description="Host immutable static-site deploys.")
    parser.add_argument("--data-dir", type=Path, default=Path("./var"), help="metadata and deploy storage directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize the data store")

    site = commands.add_parser("site", help="manage sites")
    site_commands = site.add_subparsers(dest="site_command", required=True)
    site_create = site_commands.add_parser("create", help="create a site")
    site_create.add_argument("slug")
    site_create.add_argument("domain")
    site_create.add_argument("--domain", dest="extra_domains", action="append", default=[])
    site_create.add_argument("--repository-url")
    site_create.add_argument("--branch", default="main")
    site_create.add_argument("--build-command")
    site_create.add_argument("--quota-mb", type=int, default=100)
    site_commands.add_parser("list", help="list sites")

    deploy = commands.add_parser("deploy", help="create or inspect deploys")
    deploy_commands = deploy.add_subparsers(dest="deploy_command", required=True)
    deploy_create = deploy_commands.add_parser("create", help="deploy a local directory")
    deploy_create.add_argument("slug")
    deploy_create.add_argument("directory", type=Path)
    deploy_list = deploy_commands.add_parser("list", help="list retained deploys")
    deploy_list.add_argument("slug")

    rollback = commands.add_parser("rollback", help="atomically activate an older deploy")
    rollback.add_argument("slug")
    rollback.add_argument("deploy_id")
    edge = commands.add_parser("serve", help="run the HTTP static edge")
    edge.add_argument("--host", default="127.0.0.1")
    edge.add_argument("--port", default=8080, type=int)
    edge.add_argument("--admin-token", default=os.environ.get("STATICDEPLOY_ADMIN_TOKEN"))
    edge.add_argument("--preview-suffix", default=os.environ.get("STATICDEPLOY_PREVIEW_SUFFIX"))
    edge.add_argument(
        "--routing-mode",
        choices=("subdomain", "path"),
        default=os.environ.get("STATICDEPLOY_ROUTING_MODE", "subdomain"),
        help="serve by Host header (subdomain) or /sites/<slug>/ paths (path)",
    )
    worker = commands.add_parser("worker", help="process one queued Git deployment")
    worker.add_argument("--preview-ttl-hours", type=int, default=72)
    worker.add_argument("--command-timeout", type=int, default=180)
    worker.add_argument("--watch", action="store_true", help="poll continuously for queued deploys")
    worker.add_argument("--poll-seconds", type=float, default=3)
    commands.add_parser("cleanup", help="remove expired preview mappings")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.data_dir)
    store.initialize()
    try:
        if args.command == "init":
            print(f"Initialized StaticDeploy data store at {store.data_dir}")
        elif args.command == "site" and args.site_command == "create":
            secret = store.create_site(
                args.slug, [args.domain, *args.extra_domains], args.repository_url, args.branch,
                args.build_command, args.quota_mb * 1024 * 1024,
            )
            print(f"Created {args.slug} for {args.domain}")
            if secret:
                print(f"Webhook secret (save it now): {secret}")
        elif args.command == "site" and args.site_command == "list":
            for slug, current, domains in store.list_sites():
                print(f"{slug}\tactive={current or '-'}\tdomains={','.join(domains)}")
        elif args.command == "deploy" and args.deploy_command == "create":
            deployed = store.deploy(args.slug, args.directory)
            print(f"Deployed {deployed.site_slug}@{deployed.id} ({deployed.file_count} files, {deployed.size_bytes} bytes)")
        elif args.command == "deploy" and args.deploy_command == "list":
            for deployed in store.list_deploys(args.slug):
                print(f"{deployed.id}\t{deployed.created_at}\t{deployed.file_count} files\t{deployed.size_bytes} bytes")
        elif args.command == "rollback":
            store.rollback(args.slug, args.deploy_id)
            print(f"Rolled {args.slug} back to {args.deploy_id}")
        elif args.command == "serve":
            serve(
                store, args.host, args.port, admin_token=args.admin_token,
                preview_suffix=args.preview_suffix, routing_mode=args.routing_mode,
            )
        elif args.command == "worker":
            worker = Worker(store, preview_ttl_hours=args.preview_ttl_hours, command_timeout=args.command_timeout)
            while True:
                job = worker.process_one()
                print(f"Processed {job.id}" if job else "No queued jobs")
                if not args.watch:
                    break
                store.remove_expired_previews()
                time.sleep(args.poll_seconds)
        elif args.command == "cleanup":
            print(f"Removed {store.remove_expired_previews()} expired preview mappings")
    except StoreError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

