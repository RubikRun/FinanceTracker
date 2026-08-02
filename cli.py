"""A tiny command-line client.

This exists mainly to demonstrate that the `finance` core API is fully usable
without the Qt UI (one of the project requirements). It reads/writes the very
same JSON file the GUI uses.

Examples:
    python cli.py add-spending Boris 14.20 Food 2026-06-11 --note "toilet paper"
    python cli.py add-income Milena 3560 2026-06-03
    python cli.py summary 2026 6
    python cli.py summary 2026 6 --user Boris
    python cli.py list 2026 6 --user Milena
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from typing import Optional

from finance.config import ISO_DATE_FORMAT, USERS, default_data_path, format_amount
from finance.store import FinanceStore, month_range


def _parse_iso(value: str) -> date:
    return datetime.strptime(value, ISO_DATE_FORMAT).date()


def _parse_user(value: str) -> str:
    if value not in USERS:
        raise argparse.ArgumentTypeError(
            f"unknown user {value!r}; choose one of: {', '.join(USERS)}"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FinanceTracker command-line client")
    sub = parser.add_subparsers(dest="command", required=True)

    p_spend = sub.add_parser("add-spending", help="add a spending record")
    p_spend.add_argument("user", type=_parse_user, help="Boris or Milena")
    p_spend.add_argument("amount")
    p_spend.add_argument("category")
    p_spend.add_argument("date", nargs="?", type=_parse_iso, default=date.today())
    p_spend.add_argument("--note", default="")

    p_income = sub.add_parser("add-income", help="add an income record")
    p_income.add_argument("user", type=_parse_user, help="Boris or Milena")
    p_income.add_argument("amount")
    p_income.add_argument("date", nargs="?", type=_parse_iso, default=date.today())
    p_income.add_argument("--note", default="")

    p_summary = sub.add_parser("summary", help="show totals for a month")
    p_summary.add_argument("year", type=int)
    p_summary.add_argument("month", type=int)
    p_summary.add_argument(
        "--user",
        type=_parse_user,
        default=None,
        help="filter to one person (default: both)",
    )

    p_list = sub.add_parser("list", help="list records for a month")
    p_list.add_argument("year", type=int)
    p_list.add_argument("month", type=int)
    p_list.add_argument(
        "--user",
        type=_parse_user,
        default=None,
        help="filter to one person (default: both)",
    )

    return parser


def _scope_label(user: Optional[str]) -> str:
    return user if user else "both"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    store = FinanceStore.load(default_data_path())

    if args.command == "add-spending":
        record = store.add_spending(
            args.amount, args.category, args.date, args.user, args.note
        )
        store.save()
        print(
            f"Added spending #{record.id} for {record.user}: "
            f"{format_amount(record.amount)} ({record.category}) on {record.date}"
        )

    elif args.command == "add-income":
        record = store.add_income(args.amount, args.date, args.user, args.note)
        store.save()
        print(
            f"Added income #{record.id} for {record.user}: "
            f"{format_amount(record.amount)} on {record.date}"
        )

    elif args.command == "summary":
        start, end = month_range(args.year, args.month)
        earned = store.total_earned(start, end, args.user)
        spent = store.total_spent(start, end, args.user)
        print(f"Period: {args.year}-{args.month:02d} ({_scope_label(args.user)})")
        print(f"  Earned: {format_amount(earned)}")
        print(f"  Spent:  {format_amount(spent)}")
        print(f"  Gap:    {format_amount(earned - spent)}")

    elif args.command == "list":
        start, end = month_range(args.year, args.month)
        print(f"Spending ({_scope_label(args.user)}):")
        for r in sorted(
            store.spending_between(start, end, args.user), key=lambda r: r.date
        ):
            print(
                f"  #{r.id} {r.user} {r.date} {format_amount(r.amount)} "
                f"{r.category} {r.note}".rstrip()
            )
        print(f"Income ({_scope_label(args.user)}):")
        for r in sorted(
            store.income_between(start, end, args.user), key=lambda r: r.date
        ):
            print(
                f"  #{r.id} {r.user} {r.date} {format_amount(r.amount)} "
                f"{r.note}".rstrip()
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
