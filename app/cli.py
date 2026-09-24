"""Command line entry point.

    python -m app.cli ingest targets.txt
    python -m app.cli ingest --stdin < targets.txt
    python -m app.cli crawl                    # everything never crawled
    python -m app.cli crawl madica.vc delta40.com
    python -m app.cli status
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.crawl import crawl_many
from app.extract import build_prompt, extract_target, load_pages
from app.db import session
from app.ingest import ingest
from app.models import Contact, Page, Target


def cmd_ingest(args: argparse.Namespace) -> int:
    blob = sys.stdin.read() if args.stdin else open(args.file, encoding="utf-8").read()
    result = ingest(blob, kind=args.kind)

    for domain in result.created:
        print(f"  new       {domain}")
    for domain in result.existing:
        print(f"  known     {domain}")
    for email in result.contacts_added:
        print(f"  contact   {email}")
    for raw, why in result.rejected:
        print(f"  rejected  {raw}  ({why})")

    print(f"\n{result.summary}")
    return 0


def cmd_crawl(args: argparse.Namespace) -> int:
    with session() as s:
        if args.domains:
            domains = list(args.domains)
        else:
            stmt = select(Target.domain)
            if not args.recrawl:
                stmt = stmt.where(Target.enriched_at.is_(None))
            domains = list(s.scalars(stmt))

    if not domains:
        print("nothing to crawl")
        return 0

    print(f"crawling {len(domains)} domain(s)\n")
    reports = asyncio.run(crawl_many(domains, concurrency=args.concurrency))

    for report in sorted(reports, key=lambda r: r.domain):
        print(f"  {report}")
        for err in (report.errors or [])[:3]:
            print(f"      ! {err}")

    total_pages = sum(r.pages for r in reports)
    total_chars = sum(r.chars for r in reports)
    needs = sum(r.needs_render for r in reports)
    blocked = sum(1 for r in reports if r.blocked_by_robots)
    empty = [r.domain for r in reports if r.pages == 0 and not r.blocked_by_robots]

    print(f"\n{total_pages} pages, {total_chars:,} chars")
    if needs:
        print(f"{needs} page(s) came back too thin to use — client-rendered, need the renderer")
    if blocked:
        print(f"{blocked} domain(s) declined by robots.txt")
    if empty:
        print(f"nothing fetched for: {', '.join(empty)}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    with session() as s:
        domains = list(args.domains) if args.domains else list(
            s.scalars(select(Target.domain).where(Target.enriched_at.is_(None)))
        )
    if not domains:
        print("nothing to extract")
        return 0

    if args.show_prompt:
        # Lets the prompt be read and argued with before a key exists.
        for domain in domains:
            pages = load_pages(domain)
            print(f"\n===== {domain}: {len(pages)} pages =====")
            print(build_prompt(domain, pages)[: args.chars])
        return 0

    failures = 0
    for domain in domains:
        try:
            report = extract_target(domain, model=args.model)
        except RuntimeError as exc:          # no key configured
            print(f"  {domain}: {exc}")
            return 2
        except LookupError as exc:
            print(f"  {domain}: {exc}")
            failures += 1
            continue
        print(f"  {domain}: {len(report.kept_facts)} facts, "
              f"{len(report.kept_contacts)} contacts, {len(report.dropped)} dropped")
        for what, why in report.dropped[:5]:
            print(f"      dropped: {what}  ({why})")
    return 1 if failures else 0


def cmd_status(args: argparse.Namespace) -> int:
    with session() as s:
        targets = s.scalar(select(func.count()).select_from(Target)) or 0
        contacts = s.scalar(select(func.count()).select_from(Contact)) or 0
        pages = s.scalar(select(func.count()).select_from(Page)) or 0
        thin = s.scalar(
            select(func.count()).select_from(Page).where(Page.needs_render.is_(True))
        ) or 0
        uncrawled = s.scalar(
            select(func.count()).select_from(Target)
            .where(~Target.id.in_(select(Page.target_id)))
        ) or 0

        print(f"targets    {targets}")
        print(f"contacts   {contacts}")
        print(f"pages      {pages}  ({thin} too thin without a browser)")
        print(f"uncrawled  {uncrawled}")

        rows = s.execute(
            select(Target.domain, func.count(Page.id), func.sum(Page.text_chars))
            .join(Page, Page.target_id == Target.id, isouter=True)
            .group_by(Target.domain)
            .order_by(Target.domain)
            .limit(args.limit)
        ).all()
        if rows:
            print()
            for domain, n, chars in rows:
                print(f"  {domain:34} {n or 0:>2} pages  {chars or 0:>8,} chars")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cohandle")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="add targets from a list of URLs or emails")
    p_ingest.add_argument("file", nargs="?", help="file to read")
    p_ingest.add_argument("--stdin", action="store_true", help="read from stdin instead")
    p_ingest.add_argument("--kind", default="vc",
                          choices=["vc", "grant", "accelerator", "angel", "oem", "disco", "other"])
    p_ingest.set_defaults(func=cmd_ingest)

    p_crawl = sub.add_parser("crawl", help="fetch and extract pages")
    p_crawl.add_argument("domains", nargs="*", help="specific domains; default is everything uncrawled")
    p_crawl.add_argument("--recrawl", action="store_true", help="include already-crawled targets")
    p_crawl.add_argument("--concurrency", type=int, default=4)
    p_crawl.set_defaults(func=cmd_crawl)

    p_extract = sub.add_parser("extract", help="page text into sourced facts")
    p_extract.add_argument("domains", nargs="*", help="default is everything not yet enriched")
    p_extract.add_argument("--model", default="claude-sonnet-5")
    p_extract.add_argument("--show-prompt", action="store_true",
                           help="print the prompt instead of calling the model")
    p_extract.add_argument("--chars", type=int, default=3000, help="with --show-prompt")
    p_extract.set_defaults(func=cmd_extract)

    p_status = sub.add_parser("status", help="what is in the database")
    p_status.add_argument("--limit", type=int, default=25)
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    if args.command == "ingest" and not args.stdin and not args.file:
        parser.error("give a file or --stdin")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
