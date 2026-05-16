# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json

from ..services.shop_cookie_service import ShopCookieService


def _print(data, as_json: bool):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, str):
        print(data)
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shop cookie manager CLI")
    parser.add_argument("--json", action="store_true", help="Output JSON")

    sub = parser.add_subparsers(dest="command", required=True)

    instance = sub.add_parser("instance", help="Manage browser instances")
    instance_sub = instance.add_subparsers(dest="instance_cmd", required=True)

    instance_list = instance_sub.add_parser("list", help="List instances")
    instance_list.add_argument("--platform", default="zhuanzhuan")

    instance_create = instance_sub.add_parser("create", help="Create instance")
    instance_create.add_argument("--platform", default="zhuanzhuan")
    instance_create.add_argument("--name", required=True)
    instance_create.add_argument("--note", default="")

    instance_default = instance_sub.add_parser("set-default", help="Set default instance")
    instance_default.add_argument("--platform", default="zhuanzhuan")
    instance_default.add_argument("--instance-id", required=True)

    cookie = sub.add_parser("cookie", help="Export cookie")
    cookie.add_argument("--platform", default="zhuanzhuan")
    cookie.add_argument("--instance-id", required=True)

    account = sub.add_parser("account", help="Manage accounts")
    account_sub = account.add_subparsers(dest="account_cmd", required=True)

    account_list = account_sub.add_parser("list", help="List accounts")
    account_list.add_argument("--platform", default="")
    account_list.add_argument("--group", default="")

    account_save = account_sub.add_parser("save", help="Save or update account")
    account_save.add_argument("--name", required=True)
    account_save.add_argument("--cookie", required=True)
    account_save.add_argument("--platform", default="zhuanzhuan")
    account_save.add_argument("--group", default="default")
    account_save.add_argument("--instance-id", default="")
    account_save.add_argument("--note", default="")
    account_save.add_argument("--disabled", action="store_true")

    account_apply = account_sub.add_parser("apply-from-instance", help="Apply cookie from browser instance")
    account_apply.add_argument("--name", required=True)
    account_apply.add_argument("--platform", default="zhuanzhuan")
    account_apply.add_argument("--instance-id", required=True)
    account_apply.add_argument("--group", default="")

    validate = sub.add_parser("validate", help="Validate account cookies")
    validate.add_argument("--name", default="")
    validate.add_argument("--platform", default="zhuanzhuan")
    validate.add_argument("--all", action="store_true")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    svc = ShopCookieService()

    if args.command == "instance":
        if args.instance_cmd == "list":
            rows = [x.__dict__ for x in svc.list_instances(args.platform)]
            _print(rows, args.json)
            return 0
        if args.instance_cmd == "create":
            instance = svc.create_instance(args.platform, args.name, args.note)
            _print(instance.__dict__, args.json)
            return 0
        if args.instance_cmd == "set-default":
            instance = svc.set_default_instance(args.platform, args.instance_id)
            _print(instance.__dict__, args.json)
            return 0

    if args.command == "cookie":
        cookie_text, source = svc.export_cookie(args.platform, args.instance_id)
        _print({"cookie": cookie_text, "source": source, "length": len(cookie_text)}, args.json)
        return 0

    if args.command == "account":
        if args.account_cmd == "list":
            rows = [x.to_dict() for x in svc.list_accounts(platform=args.platform or None, group=args.group or None)]
            _print(rows, args.json)
            return 0
        if args.account_cmd == "save":
            account = svc.save_account(
                name=args.name,
                cookie=args.cookie,
                platform=args.platform,
                group=args.group,
                browser_instance_id=args.instance_id,
                note=args.note,
                enabled=not args.disabled,
            )
            _print(account.to_dict(), args.json)
            return 0
        if args.account_cmd == "apply-from-instance":
            account = svc.apply_cookie_from_instance(
                name=args.name,
                platform=args.platform,
                instance_id=args.instance_id,
                group=(args.group or None),
            )
            _print(account.to_dict(), args.json)
            return 0

    if args.command == "validate":
        if args.all:
            _print(svc.validate_all(platform=args.platform or None), args.json)
            return 0
        if not args.name:
            parser.error("validate 需要 --name 或 --all")
        ok, message = svc.validate_account(name=args.name, platform=args.platform)
        _print({"ok": ok, "message": message, "name": args.name, "platform": args.platform}, args.json)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
