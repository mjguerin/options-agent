"""Command line entry point: `tournament keys check`, `tournament hello`."""

from __future__ import annotations

import argparse
import sys

from tournament.secrets import (
    InsecureEnvFileError,
    MissingCredentialsError,
    env_file,
    load_alpaca_credentials,
)


def _keys_check(args: argparse.Namespace) -> int:
    try:
        load_alpaca_credentials(args.account)
    except (MissingCredentialsError, InsecureEnvFileError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{env_file()} has Alpaca credentials for {args.account!r}.")
    return 0


def _hello(args: argparse.Namespace) -> int:
    from tournament.adapters.alpaca import get_account_summary, paper_trading_client

    try:
        creds = load_alpaca_credentials(args.account)
    except (MissingCredentialsError, InsecureEnvFileError) as exc:
        print(exc, file=sys.stderr)
        return 1
    summary = get_account_summary(paper_trading_client(creds))
    print(f"Alpaca paper account {summary.account_number_masked} ({args.account})")
    print(f"  status:         {summary.status}")
    print(f"  equity:         {summary.equity} {summary.currency}")
    print(f"  cash:           {summary.cash}")
    print(f"  buying power:   {summary.buying_power}")
    print(f"  options level:  {summary.options_trading_level}")
    print(f"  trading blocked: {summary.trading_blocked}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tournament")
    sub = parser.add_subparsers(dest="command", required=True)

    keys = sub.add_parser("keys", help="check Alpaca credentials in .env")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    check = keys_sub.add_parser("check")
    check.add_argument("--account", default="dev", help="account alias (default: dev)")
    check.set_defaults(func=_keys_check)

    hello = sub.add_parser("hello", help="read the paper account through the Alpaca API")
    hello.add_argument("--account", default="dev", help="account alias (default: dev)")
    hello.set_defaults(func=_hello)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
