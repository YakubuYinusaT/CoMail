"""The review queue.

The screen where a person reads what the model wrote next to the pages it
read, and decides. Everything else in this project exists to fill it.

Deliberately plain: server-rendered, no build step, no client framework.
It is an internal tool for five people, and the interesting part is the
facts sitting beside the draft, not the widgets.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app import auth
from app.config import settings
from app.db import session
from app.ingest import ingest
from app.intake import board, counts
from app.models import (Assessment, Campaign, Contact, Draft, DraftFact, Fact,
                        Page, Reply, Send, Target)
from app.house_style import find as find_slop
from app.qualify import email_advice
from app.sending import drafting, may_send, queue as queue_send, signature
from app.sender_claims import review as review_sender
from app.worker import ENRICH, GENERATE, enqueue

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Every page's header asks whether to show a sign-out button. Passing it
# through four route handlers would be four chances to forget.
templates.env.globals["auth_on"] = auth.enabled


def _safe_next(raw: str) -> str:
    """Only ever redirect inside this app.

    `next` arrives in a query string, so it is attacker-controlled. A
    value like `//evil.example` is a protocol-relative URL the browser
    would happily follow off-site.
    """
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if not auth.enabled() or auth.valid(request.cookies.get(auth.COOKIE)):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"nav": "login", "next": _safe_next(next)})


@router.post("/login")
def login_submit(request: Request, password: str = Form(""), next: str = Form("/")):
    destination = _safe_next(next)
    if not auth.enabled():
        return RedirectResponse(destination, status_code=303)

    ip = auth.client_ip(request)
    wait = auth.locked_out(ip)
    if wait:
        return templates.TemplateResponse(
            request, "login.html",
            {"nav": "login", "next": destination,
             "error": f"Too many attempts. Try again in {wait} seconds."},
            status_code=429)

    if not auth.check_password(password):
        auth.note_failure(ip)
        return templates.TemplateResponse(
            request, "login.html",
            {"nav": "login", "next": destination, "error": "That password is wrong."},
            status_code=401)

    auth.clear_failures(ip)
    response = RedirectResponse(destination, status_code=303)
    auth.set_cookie(response, request)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    auth.clear_cookie(response)
    return response


def _campaign(s):
    """Whichever campaign is active. One at a time, for now."""
    return s.scalar(select(Campaign).order_by(Campaign.created_at.desc()))


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    rows = []
    with session() as s:
        campaign = _campaign(s)
        for d in s.scalars(select(Draft).order_by(Draft.updated_at.desc())):
            t = s.get(Target, d.target_id)
            c = s.get(Contact, d.contact_id) if d.contact_id else None
            a = s.scalar(select(Assessment).where(
                Assessment.target_id == t.id,
                Assessment.campaign_id == d.campaign_id))
            ok, _ = email_advice(t.profile or {}, c is not None)
            sent = s.scalar(select(Send).where(Send.draft_id == d.id))
            rows.append({
                "send_state": sent.state if sent else None,
                "send_note": (sent.error or "") if sent else "",
                "id": str(d.id), "domain": t.domain, "name": t.name or t.domain,
                "score": a.score if a else None, "hook_type": d.hook_type,
                "similarity_max": d.similarity_max, "status": d.status,
                "email": c.email if c else None,
                "verified": c.verified if c else False, "email_ok": ok,
            })
    pending = sum(1 for r in rows if r["status"] in ("generated", "edited"))
    decision = may_send()
    return templates.TemplateResponse(request, "queue.html", {
        "drafts": rows, "nav": "queue", "pending": pending,
        "send_ok": decision.ok, "send_why": decision.why,
        "drafting": drafting(),
        "campaign": campaign.name if campaign else None,
        "threshold": settings().sameness_threshold,
    })


@router.get("/draft/{draft_id}", response_class=HTMLResponse)
def draft_detail(request: Request, draft_id: str):
    with session() as s:
        d = s.get(Draft, draft_id)
        if d is None:
            return RedirectResponse("/queue", status_code=303)
        t = s.get(Target, d.target_id)
        c = s.get(Contact, d.contact_id) if d.contact_id else None
        a = s.scalar(select(Assessment).where(
            Assessment.target_id == t.id, Assessment.campaign_id == d.campaign_id))

        facts = s.scalars(select(Fact).where(Fact.target_id == t.id)
                          .order_by(Fact.kind)).all()
        cited = {r.fact_id for r in s.scalars(
            select(DraftFact).where(DraftFact.draft_id == d.id))}

        profile = t.profile or {}
        ok, why = email_advice(profile, c is not None)

        # Claims the draft makes about us. Checked at render time rather
        # than stored, so editing the body updates the list instead of
        # leaving stale warnings against text that no longer says it.
        campaign = s.get(Campaign, d.campaign_id)
        pitch = (campaign.sender_profile or {}).get("pitch", "") if campaign else ""
        claims = review_sender(d.body, pitch)
        # Checked live too, so editing a phrase out clears it.
        slop = find_slop(d.subject, d.body)
        sign = signature(campaign)
        ctx = {
            "nav": "queue", "d": d, "t": t, "contact": c,
            "score": a.score if a else None,
            "verdict": a.verdict if a else None,
            "criteria": (a.reasons or {}).get("criteria") if a else None,
            "facts": facts, "cited_ids": cited,
            "app": profile.get("application") or {},
            "observations": profile.get("observations") or [],
            "email_ok": ok, "advice_why": why,
            "sender_claims": claims, "slop": slop, "signature": sign,
            "claims_wrong": sum(1 for cl in claims
                                if cl.severity in ("contradicted", "unsupported")),
        }
        return templates.TemplateResponse(request, "draft.html", ctx)


@router.post("/draft/{draft_id}/save")
def draft_save(
    draft_id: str,
    action: str = Form(...),
    subject: str = Form(""),
    body: str = Form(""),
    to: str = Form(""),
):
    """Save, approve or reject. Edits are kept even when rejecting.

    Approving records who and when, because 'nothing sends without a
    human approving it' is only true if the approval is written down.
    """
    approved: str | None = None
    with session() as s:
        d = s.get(Draft, draft_id)
        if d is None:
            return RedirectResponse("/queue", status_code=303)

        changed = d.subject != subject.strip() or d.body != body.strip()
        d.subject = subject.strip() or d.subject
        d.body = body.strip() or d.body

        address = to.strip().lower()
        if address:
            existing = s.scalar(select(Contact).where(
                Contact.target_id == d.target_id, Contact.email == address))
            if existing is None:
                # Typed into the review screen by a person, so verified
                # by definition.
                existing = Contact(target_id=d.target_id, email=address,
                                   verified=True, source_url="operator:review-queue")
                s.add(existing)
                s.flush()
            else:
                existing.verified = True
            d.contact_id = existing.id

        # A Python datetime, not func.now(): once assigned, a SQL
        # expression cannot be tested with `or`, which is exactly how the
        # first version of this broke.
        now = datetime.now(timezone.utc)
        if changed:
            d.status = "edited"
            d.edited_by = "operator"
            d.edited_at = now
        if action == "approve":
            d.status = "approved"
            d.edited_by = d.edited_by or "operator"
            d.edited_at = d.edited_at or now
            approved = str(d.id)
        elif action == "reject":
            d.status = "rejected"
            approved = None

    # Queued outside the session above, so the approval is committed
    # before anything intends to send it. Queueing is not sending: the
    # window, the caps and SEND_ENABLED all still apply.
    if approved:
        queue_send(approved)

    return RedirectResponse("/queue" if action != "save" else f"/draft/{draft_id}",
                            status_code=303)


KINDS = ["vc", "grant", "accelerator", "angel", "oem", "disco", "other"]


def _intake_context(request: Request, result=None) -> dict:
    rows = board()
    with session() as s:
        campaign = _campaign(s)
    return {
        "nav": "intake", "rows": rows, "counts": counts(rows),
        "kinds": KINDS, "result": result,
        "campaign": campaign.name if campaign else None,
    }


def desktop_mode() -> bool:
    """True when this process was started by the desktop launcher."""
    return os.environ.get("COHANDLE_LOOPBACK") == "1"


templates.env.globals["desktop_mode"] = desktop_mode


@router.post("/quit")
def quit_app(request: Request):
    """Stop the server from inside the app.

    The windowed launcher has no console, so there is nothing to Ctrl+C.
    Refuses to exist anywhere but loopback: on a deployed service this
    would be an unauthenticated way to take the thing down.
    """
    if not desktop_mode():
        raise HTTPException(status_code=404)
    server = getattr(request.app.state, "server", None)
    if server is not None:
        server.should_exit = True
    return templates.TemplateResponse(request, "quit.html", {"nav": "quit"})


# Intake is the front door: the first thing you do is add targets.
@router.get("/", response_class=HTMLResponse)
@router.get("/intake", response_class=HTMLResponse)
def intake_page(request: Request):
    return templates.TemplateResponse(request, "intake.html",
                                      _intake_context(request))


@router.get("/intake/board", response_class=HTMLResponse)
def intake_board(request: Request):
    """The table on its own, for the page to poll while work is running."""
    return templates.TemplateResponse(request, "_board.html",
                                      _intake_context(request))


@router.post("/intake", response_class=HTMLResponse)
def intake_submit(request: Request, blob: str = Form(""), kind: str = Form("vc")):
    """Paste in, targets out, crawling started.

    Safe to submit twice: ingest keys on domain, enqueue refuses a second
    job for a domain already in flight, and anything already read is left
    alone rather than crawled again.
    """
    if kind not in KINDS:
        kind = "vc"
    outcome = ingest(blob, kind=kind)

    started, already_read = [], []
    with session() as s:
        for domain in outcome.created + outcome.existing:
            target = s.scalar(select(Target).where(Target.domain == domain))
            if target is not None and target.enriched_at is not None:
                already_read.append(domain)
            elif enqueue(ENRICH, domain=domain):
                started.append(domain)

    result = {
        "created": outcome.created, "existing": outcome.existing,
        "contacts": outcome.contacts_added, "rejected": outcome.rejected,
        "started": started, "already_read": already_read,
    }
    return templates.TemplateResponse(request, "intake.html",
                                      _intake_context(request, result))


@router.post("/intake/contact")
def intake_contact(domain: str = Form(...), email: str = Form("")):
    """Attach an address you found, from the intake screen.

    Typed by a person, so it is verified by definition — the same rule the
    review queue uses. Cohandle never puts an address here itself.
    """
    address = email.strip().lower()
    if address:
        with session() as s:
            target = s.scalar(select(Target).where(Target.domain == domain))
            if target is not None:
                existing = s.scalar(select(Contact).where(
                    Contact.target_id == target.id, Contact.email == address))
                if existing is None:
                    s.add(Contact(target_id=target.id, email=address,
                                  verified=True, source_url="operator:intake"))
                else:
                    existing.verified = True
    return RedirectResponse("/intake", status_code=303)


@router.post("/intake/confirm")
def intake_confirm(domain: str = Form(...), email: str = Form(...)):
    """Mark an address found on their site as one you have checked.

    Finding is done by pattern match over the crawled page, so the
    address is certainly printed somewhere on the site. Whether anybody
    reads it is a separate question, and this is a person answering it.
    """
    address = email.strip().lower()
    with session() as s:
        target = s.scalar(select(Target).where(Target.domain == domain))
        if target is not None and address:
            contact = s.scalar(select(Contact).where(
                Contact.target_id == target.id, Contact.email == address))
            if contact is not None:
                contact.verified = True
    return RedirectResponse("/intake", status_code=303)


@router.post("/intake/draft")
def intake_draft(domain: str = Form(...)):
    """The step intake deliberately does not take by itself."""
    with session() as s:
        campaign = _campaign(s)
    if campaign is not None:
        enqueue(GENERATE, domain=domain, campaign=campaign.name)
    return RedirectResponse("/intake", status_code=303)


@router.get("/targets", response_class=HTMLResponse)
def targets(request: Request):
    """Everything Cohandle knows about, densest view.

    Counted in grouped queries rather than five per row. On a laptop
    talking to Frankfurt, the difference is seconds.
    """
    rows = []
    with session() as s:
        all_targets = list(s.scalars(select(Target).order_by(Target.domain)))
        ids = [t.id for t in all_targets]

        pages: dict = {}
        facts: dict = {}
        contacts: dict[object, list[Contact]] = {}
        assessments: dict = {}
        drafts: dict = {}
        if ids:
            pages = dict(s.execute(
                select(Page.target_id, func.count())
                .where(Page.target_id.in_(ids)).group_by(Page.target_id)).all())
            facts = dict(s.execute(
                select(Fact.target_id, func.count())
                .where(Fact.target_id.in_(ids)).group_by(Fact.target_id)).all())
            for c in s.scalars(select(Contact).where(
                    Contact.target_id.in_(ids), Contact.email.is_not(None))):
                contacts.setdefault(c.target_id, []).append(c)
            # Oldest first so the newest wins the key.
            assessments = {a.target_id: a for a in s.scalars(
                select(Assessment).where(Assessment.target_id.in_(ids))
                .order_by(Assessment.created_at))}
            drafts = {d.target_id: d for d in s.scalars(
                select(Draft).where(Draft.target_id.in_(ids))
                .order_by(Draft.created_at))}

        for t in all_targets:
            a = assessments.get(t.id)
            d = drafts.get(t.id)
            found = contacts.get(t.id, [])
            rows.append({
                "domain": t.domain, "name": t.name, "channel": t.channel,
                "score": a.score if a else None,
                "verdict": a.verdict if a else None,
                "pages": pages.get(t.id, 0),
                "facts": facts.get(t.id, 0),
                "contacts": len(found),
                "unverified": sum(1 for c in found if not c.verified),
                "draft_id": str(d.id) if d else None,
                "draft_status": d.status if d else None,
            })
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    return templates.TemplateResponse(request, "targets.html",
                                      {"rows": rows, "nav": "targets"})


@router.get("/target/{domain}", response_class=HTMLResponse)
def target_detail(request: Request, domain: str):
    """Everything Cohandle knows about one target.

    The Targets table reported "13 facts" with nowhere to go and see
    them, and a row was only clickable once it had a draft, so a target
    that had been read but not drafted was a dead end. This is the
    destination that was missing.
    """
    with session() as s:
        t = s.scalar(select(Target).where(Target.domain == domain))
        if t is None:
            return RedirectResponse("/targets", status_code=303)

        assessment = s.scalar(select(Assessment).where(
            Assessment.target_id == t.id).order_by(Assessment.created_at))
        draft = s.scalar(select(Draft).where(Draft.target_id == t.id)
                         .order_by(Draft.created_at))
        reply = s.scalar(select(Reply).where(Reply.target_id == t.id)
                         .order_by(Reply.received_at.desc()))
        send = s.scalar(select(Send).where(Send.draft_id == draft.id)) if draft else None

        contacts = list(s.scalars(select(Contact).where(
            Contact.target_id == t.id, Contact.email.is_not(None))
            .order_by(Contact.created_at)))
        facts = list(s.scalars(select(Fact).where(Fact.target_id == t.id)
                               .order_by(Fact.kind)))
        pages = list(s.scalars(select(Page).where(Page.target_id == t.id)
                               .order_by(Page.kind)))

        profile = t.profile or {}
        return templates.TemplateResponse(request, "target.html", {
            "nav": "targets", "t": t, "profile": profile,
            "application": profile.get("application") or {},
            "observations": profile.get("observations") or [],
            "score": assessment.score if assessment else None,
            "verdict": assessment.verdict if assessment else None,
            "criteria": (assessment.reasons or {}).get("criteria") if assessment else None,
            "rejection": (assessment.reasons or {}).get("rejection_reason") if assessment else None,
            "facts": facts, "pages": pages, "contacts": contacts,
            "draft": draft, "send": send, "reply": reply,
        })


@router.get("/apply", response_class=HTMLResponse)
def apply_list(request: Request):
    rows = []
    with session() as s:
        for t in s.scalars(select(Target).where(
                Target.channel.in_(["form", "intro", "hold"]))):
            a = s.scalar(select(Assessment).where(Assessment.target_id == t.id))
            app = (t.profile or {}).get("application") or {}
            rows.append({
                "domain": t.domain, "name": t.name, "channel": t.channel,
                "channel_note": t.channel_note,
                "score": a.score if a else None,
                "url": app.get("url"), "how": app.get("how_to_apply"),
                "status": app.get("status"), "deadline": app.get("deadline"),
                "asks_for": app.get("asks_for") or [], "note": app.get("notes"),
            })
    # Closed last: they are the ones not to spend time on today.
    order = {"open": 0, "rolling": 1, "cyclical": 2, "unknown": 3, "closed": 4}
    rows.sort(key=lambda r: (order.get(r["status"] or "unknown", 3),
                             -(r["score"] or 0)))
    return templates.TemplateResponse(request, "apply.html",
                                      {"rows": rows, "nav": "apply"})
