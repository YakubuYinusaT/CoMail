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
from app.generate import generate_campaign
from app.qualify import qualify_campaign
from app.models import Campaign, Contact, Page, Target


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


def cmd_campaign(args: argparse.Namespace) -> int:
    import json
    profile = json.loads(args.profile) if args.profile else {
        "stage": "pre-seed",
        "raise_target_usd": 250000,
        "sectors": ["energy", "embedded finance", "climate", "infrastructure"],
        "geography": "nigeria",
        "competitors": ["Optasia"],
    }
    with session() as s:
        existing = s.scalar(select(Campaign).where(Campaign.name == args.name))
        if existing is None:
            s.add(Campaign(name=args.name, sender_profile=profile))
            print(f"created campaign {args.name!r}")
        else:
            existing.sender_profile = profile
            print(f"updated campaign {args.name!r}")
    for k, v in profile.items():
        print(f"  {k:18} {v}")
    return 0


def cmd_qualify(args: argparse.Namespace) -> int:
    try:
        results = qualify_campaign(args.campaign, only=args.domains or None)
    except LookupError as exc:
        print(exc)
        return 2
    if not results:
        print("nothing enriched yet to qualify")
        return 0

    order = {"qualified": 0, "needs_review": 1, "rejected": 2}
    for domain, verdict, channel in sorted(results, key=lambda r: (order[r[1].verdict], -r[1].score)):
        print(f"  {verdict.score:>3}  {verdict.verdict:<13} {channel:<6} {domain}")
        if verdict.rejection_reason:
            print(f"        {verdict.rejection_reason}")
        if args.verbose:
            for c in verdict.criteria:
                print(f"          {c.name:<10} {c.points:>3}/{c.max_points:<3} {c.note}")

    counts: dict[str, int] = {}
    for _, v, _ in results:
        counts[v.verdict] = counts.get(v.verdict, 0) + 1
    print("\n" + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    try:
        report = generate_campaign(args.campaign, only=args.domains or None,
                                   model=args.model, max_retries=args.retries)
    except RuntimeError as exc:
        print(exc)
        return 2
    except LookupError as exc:
        print(exc)
        return 2

    if not report.written and not report.failed:
        print("no targets to draft for: generation needs targets that are "
              "enriched and routed to email.\n"
              "run extract, then qualify, then this.")
        return 0

    thin = set(report.without_facts)
    for domain in report.written:
        worst = report.worst.get(domain, 0.0)
        marks = []
        if domain in thin:
            marks.append("no verified facts, written without specifics")
        if domain in report.still_similar:
            marks.append("still similar")
        suffix = f"   ({'; '.join(marks)})" if marks else ""
        print(f"  drafted   {domain:32} sameness {worst:.2f}{suffix}")

    for domain, why in report.failed:
        print(f"  failed    {domain:32} {why}")

    if report.regenerated:
        print(f"\nregenerated {len(report.regenerated)}: {', '.join(report.regenerated)}")
    if report.still_similar:
        print(f"still too similar after {args.retries} retries: "
              f"{', '.join(report.still_similar)}")
    print(f"\n{len(report.written)} drafted "
          f"({len(report.without_facts)} without verified facts), "
          f"{len(report.failed)} failed")
    return 0


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
    p_extract.add_argument("--model", default=None,
                           help="override; default comes from LLM_PROVIDER")
    p_extract.add_argument("--show-prompt", action="store_true",
                           help="print the prompt instead of calling the model")
    p_extract.add_argument("--chars", type=int, default=3000, help="with --show-prompt")
    p_extract.set_defaults(func=cmd_extract)

    p_campaign = sub.add_parser("campaign", help="create or update a campaign's sender profile")
    p_campaign.add_argument("name")
    p_campaign.add_argument("--profile", help="JSON; omit for the Connected Intelligence default")
    p_campaign.set_defaults(func=cmd_campaign)

    p_qualify = sub.add_parser("qualify", help="score and route enriched targets")
    p_qualify.add_argument("campaign")
    p_qualify.add_argument("domains", nargs="*")
    p_qualify.add_argument("-v", "--verbose", action="store_true", help="per-criterion breakdown")
    p_qualify.set_defaults(func=cmd_qualify)

    p_generate = sub.add_parser("generate", help="draft emails for qualified targets")
    p_generate.add_argument("campaign")
    p_generate.add_argument("domains", nargs="*")
    p_generate.add_argument("--model", default=None,
                            help="override; default comes from LLM_PROVIDER")
    p_generate.add_argument("--retries", type=int, default=2,
                            help="regeneration attempts for drafts that read alike")
    p_generate.set_defaults(func=cmd_generate)

    p_status = sub.add_parser("status", help="what is in the database")
    p_status.add_argument("--limit", type=int, default=25)
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    if args.command == "ingest" and not args.stdin and not args.file:
        parser.error("give a file or --stdin")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
