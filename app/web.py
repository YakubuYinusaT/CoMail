"""The review queue.

The screen where a person reads what the model wrote next to the pages it
read, and decides. Everything else in this project exists to fill it.

Deliberately plain: server-rendered, no build step, no client framework.
It is an internal tool for five people, and the interesting part is the
facts sitting beside the draft, not the widgets.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.config import settings
from app.db import session
from app.models import Assessment, Campaign, Contact, Draft, DraftFact, Fact, Page, Target
from app.qualify import email_advice

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _campaign(s):
    """Whichever campaign is active. One at a time, for now."""
    return s.scalar(select(Campaign).order_by(Campaign.created_at.desc()))


@router.get("/", response_class=HTMLResponse)
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
            rows.append({
                "id": str(d.id), "domain": t.domain, "name": t.name or t.domain,
                "score": a.score if a else None, "hook_type": d.hook_type,
                "similarity_max": d.similarity_max, "status": d.status,
                "email": c.email if c else None,
                "verified": c.verified if c else False, "email_ok": ok,
            })
    return templates.TemplateResponse(request, "queue.html", {
        "drafts": rows, "nav": "queue",
        "campaign": campaign.name if campaign else None,
        "threshold": settings().sameness_threshold,
    })


@router.get("/draft/{draft_id}", response_class=HTMLResponse)
def draft_detail(request: Request, draft_id: str):
    with session() as s:
        d = s.get(Draft, draft_id)
        if d is None:
            return RedirectResponse("/", status_code=303)
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
        ctx = {
            "nav": "queue", "d": d, "t": t, "contact": c,
            "score": a.score if a else None,
            "verdict": a.verdict if a else None,
            "criteria": (a.reasons or {}).get("criteria") if a else None,
            "facts": facts, "cited_ids": cited,
            "app": profile.get("application") or {},
            "observations": profile.get("observations") or [],
            "email_ok": ok, "advice_why": why,
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
    with session() as s:
        d = s.get(Draft, draft_id)
        if d is None:
            return RedirectResponse("/", status_code=303)

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
        elif action == "reject":
            d.status = "rejected"

    return RedirectResponse("/" if action != "save" else f"/draft/{draft_id}",
                            status_code=303)


@router.get("/targets", response_class=HTMLResponse)
def targets(request: Request):
    rows = []
    with session() as s:
        for t in s.scalars(select(Target).order_by(Target.domain)):
            a = s.scalar(select(Assessment).where(Assessment.target_id == t.id))
            d = s.scalar(select(Draft).where(Draft.target_id == t.id))
            contacts = s.scalars(select(Contact).where(
                Contact.target_id == t.id, Contact.email.is_not(None))).all()
            rows.append({
                "domain": t.domain, "name": t.name, "channel": t.channel,
                "score": a.score if a else None,
                "verdict": a.verdict if a else None,
                "pages": s.scalar(select(func.count()).select_from(Page)
                                  .where(Page.target_id == t.id)) or 0,
                "facts": s.scalar(select(func.count()).select_from(Fact)
                                  .where(Fact.target_id == t.id)) or 0,
                "contacts": len(contacts),
                "unverified": sum(1 for c in contacts if not c.verified),
                "draft_id": str(d.id) if d else None,
                "draft_status": d.status if d else None,
            })
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    return templates.TemplateResponse(request, "targets.html",
                                      {"rows": rows, "nav": "targets"})


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
