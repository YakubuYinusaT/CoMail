"""Schema.

Follows the build spec, with one deliberate change: `draft.fact_ids` is a
`draft_fact` join table rather than an array column.

The spec asks that a draft cannot be queued unless every fact it cites
has a source URL. With an array that needs a trigger, which is easy to
disable and easy to forget. With a join table it falls out of a foreign
key plus `fact.source_url NOT NULL`: an unsourced claim has nowhere to be
stored, so the guarantee holds without anyone maintaining it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


def _enum(name: str, *values: str) -> CheckConstraint:
    joined = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{name} IN ({joined})", name=f"ck_{name}")


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Campaign(Base, Timestamped):
    """One raise or outreach push."""

    __tablename__ = "campaign"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # The sender profile qualification scores against: stage, sectors,
    # geography, raise size, named competitors. Held per campaign so the
    # same engine serves an investor raise and an OEM push.
    sender_profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    __table_args__ = (_enum("status", "active", "paused", "closed"),)


class Target(Base, Timestamped):
    """An organisation. Deduplicated by domain."""

    __tablename__ = "target"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    domain: Mapped[str] = mapped_column(String(253), nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="vc")

    # The address as pasted, when it named a page rather than a site.
    # Deduplication is by domain, but somebody who pastes
    # /ventra-capital means that page, and the crawler should start
    # there rather than rediscovering the homepage.
    seed_url: Mapped[str | None] = mapped_column(Text)

    # Why you are writing to them, in your own words. Cohandle can read
    # what a fund does off its website; it cannot know whether this is a
    # qualifying question before you spend an hour on their form, or an
    # introduction to the round.
    brief: Mapped[str | None] = mapped_column(Text)

    # Ticked off by hand: emailed, or their form submitted. Cohandle
    # cannot see either happen, so it is a person saying so. Set, the
    # target leaves the Add board, which is the point.
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Structured summary from enrichment: stages, sectors, geographies,
    # cheque range, portfolio, thesis quote, last deal, accepts_cold.
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    channel: Mapped[str | None] = mapped_column(String(20))
    channel_note: Mapped[str | None] = mapped_column(Text)

    contacts: Mapped[list["Contact"]] = relationship(back_populates="target", cascade="all, delete-orphan")
    facts: Mapped[list["Fact"]] = relationship(back_populates="target", cascade="all, delete-orphan")

    __table_args__ = (
        _enum("kind", "vc", "grant", "accelerator", "angel", "oem", "disco", "other"),
        _enum("channel", "email", "form", "intro", "hold", "blocked"),
        Index("ix_target_enriched_at", "enriched_at"),
        Index("ix_target_done_at", "done_at"),
    )


class Contact(Base, Timestamped):
    """A person at a target. A target may qualify with no known address."""

    __tablename__ = "contact"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(200))
    # Lowercased on write. Postgres citext would be tidier but needs an
    # extension, and one normalising call site is cheaper than that.
    email: Mapped[str | None] = mapped_column(String(320))
    # Where the address was published. Required when there is an address:
    # we do not hold contact details we cannot account for.
    source_url: Mapped[str | None] = mapped_column(Text)
    # False means the model read it off a page and nobody has checked it
    # reaches a person. True means the operator put it there or signed
    # it off. Extraction cannot set this.
    verified: Mapped[bool] = mapped_column(nullable=False, default=False)

    target: Mapped[Target] = relationship(back_populates="contacts")

    __table_args__ = (
        CheckConstraint(
            "email IS NULL OR source_url IS NOT NULL", name="ck_contact_email_needs_source"
        ),
        CheckConstraint("email IS NULL OR email = lower(email)", name="ck_contact_email_lower"),
        Index("ix_contact_target", "target_id"),
        Index("ix_contact_email", "email"),
    )


class Fact(Base, Timestamped):
    """The ledger. Every personalised claim draws from here.

    `source_url` is NOT NULL by design. A claim with no source cannot be
    written down, so it cannot reach an email.
    """

    __tablename__ = "fact"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 'inferred' facts may inform qualification. They may not be quoted.
    confidence: Mapped[str] = mapped_column(String(10), nullable=False, default="verified")

    target: Mapped[Target] = relationship(back_populates="facts")

    __table_args__ = (
        _enum("kind", "portfolio", "thesis", "deal", "publication", "person", "cheque_size"),
        _enum("confidence", "verified", "inferred"),
        Index("ix_fact_target_kind", "target_id", "kind"),
    )


class Assessment(Base, Timestamped):
    """Qualification result, one per target per campaign."""

    __tablename__ = "assessment"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"), nullable=False)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    # Per-criterion scores and, on a rejection, the criterion that failed.
    reasons: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        _enum("verdict", "qualified", "rejected", "needs_review"),
        CheckConstraint("score BETWEEN 0 AND 100", name="ck_assessment_score_range"),
        UniqueConstraint("target_id", "campaign_id", name="uq_assessment_target_campaign"),
    )


class Draft(Base, Timestamped):
    """One email, for one target, in one campaign."""

    __tablename__ = "draft"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"), nullable=False)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contact.id", ondelete="SET NULL"))

    # 1 is the first approach, 2 the first follow-up, and so on. A
    # follow-up is another email to the same fund, not another kind of
    # thing, so it gets the same review and the same guards.
    step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    hook_type: Mapped[str] = mapped_column(String(24), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Highest pairwise similarity against the rest of its batch.
    similarity_max: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="generated")
    edited_by: Mapped[str | None] = mapped_column(String(200))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    cited_facts: Mapped[list["DraftFact"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )

    __table_args__ = (
        _enum(
            "hook_type",
            "portfolio_adjacent",
            "thesis_quote",
            "recent_cluster",
            "publication",
            "fund_event",
            "none",
        ),
        _enum(
            "status",
            "generated",
            "edited",
            "approved",
            "rejected",
            "queued",
            "sent",
        ),
        UniqueConstraint("target_id", "campaign_id", "step",
                         name="uq_draft_target_campaign_step"),
        CheckConstraint("step >= 1", name="ck_step"),
        Index("ix_draft_status", "status"),
    )


class DraftFact(Base):
    """Which facts a draft cites.

    The foreign key is the enforcement: a draft can only cite a row that
    exists in `fact`, and every row in `fact` has a source URL. There is
    no path by which an unsourced claim gets cited.
    """

    __tablename__ = "draft_fact"

    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("draft.id", ondelete="CASCADE"), primary_key=True
    )
    fact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fact.id", ondelete="RESTRICT"), primary_key=True
    )

    draft: Mapped[Draft] = relationship(back_populates="cited_facts")
    fact: Mapped[Fact] = relationship()


class Send(Base, Timestamped):
    """Delivery record."""

    __tablename__ = "send"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    draft_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("draft.id", ondelete="CASCADE"), nullable=False)
    zoho_message_id: Mapped[str | None] = mapped_column(String(200))
    thread_id: Mapped[str | None] = mapped_column(String(200))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # "drafted" means Cohandle put it in Zoho's Drafts folder and a
        # person sends it from there. It is deliberately not "sent":
        # nobody has received anything, and it may sit in Drafts forever.
        _enum("state", "queued", "drafted", "sent", "bounced", "failed", "cancelled"),
        Index("ix_send_state_scheduled", "state", "scheduled_for"),
        Index("ix_send_thread", "thread_id"),
    )


class Reply(Base, Timestamped):
    """Detected inbound. The signal that stops everything for a target."""

    __tablename__ = "reply"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    send_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("send.id", ondelete="SET NULL"))
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    from_email: Mapped[str] = mapped_column(String(320), nullable=False)
    snippet: Mapped[str | None] = mapped_column(Text)
    # An out-of-office matches the same way a real reply does. Marked
    # here so a person decides rather than the poller guessing.
    is_auto_reply: Mapped[bool] = mapped_column(nullable=False, default=False)

    __table_args__ = (Index("ix_reply_target", "target_id"),)


class Job(Base, Timestamped):
    """The queue.

    Postgres rather than Redis: at this volume a table with SKIP LOCKED
    is enough, and it is one less service to run and pay for.
    """

    __tablename__ = "job"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        _enum("kind", "enrich", "qualify", "generate", "send", "poll_replies"),
        _enum("state", "pending", "running", "done", "failed"),
        Index("ix_job_claim", "state", "run_after"),
    )


class Page(Base, Timestamped):
    """A crawled page.

    Kept as extracted text rather than raw HTML. The extractor reads
    text, nothing downstream wants the markup, and storing it would grow
    the database by an order of magnitude for no gain. `needs_render`
    marks the pages a plain fetch could not get anything out of, which in
    practice means a client-rendered site.
    """

    __tablename__ = "page"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="other")
    http_status: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(Text)
    text_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rendered: Mapped[bool] = mapped_column(nullable=False, default=False)
    needs_render: Mapped[bool] = mapped_column(nullable=False, default=False)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        _enum("kind", "home", "about", "team", "portfolio", "thesis", "contact", "apply", "other"),
        UniqueConstraint("target_id", "url", name="uq_page_target_url"),
        Index("ix_page_target_kind", "target_id", "kind"),
    )
