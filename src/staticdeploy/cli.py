from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .server import serve
from .store import Store, StoreError


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.data_dir)
    store.initialize()
    try:
        if args.command == "init":
            print(f"Initialized StaticDeploy data store at {store.data_dir}")
        elif args.command == "site" and args.site_command == "create":
            store.create_site(args.slug, [args.domain, *args.extra_domains])
            print(f"Created {args.slug} for {args.domain}")
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
            serve(store, args.host, args.port)
    except StoreError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

