# Cohandle

Internal tool for personalised outreach at volume.

Give it a list of websites or email addresses. It researches each target,
rejects the ones that do not fit, drafts a different email for each of the
rest, and sends them from the Connected Intelligence Zoho mailbox on a
throttle, so replies land in the normal inbox and the conversation carries
on there.

Built first for the Connected Intelligence raise. Designed to generalise to
OEM, DisCo and partner outreach without rework.

## Sending

Nothing leaves until `SEND_ENABLED=true`. Credentials are not permission:
with a working Zoho token and that flag false, Cohandle does every step
up to the API call and records what it would have sent.

Prove the credentials without putting anything in anybody's inbox:

```bash
python -m app.cli zoho
```

That is a read. It lists the mailboxes the token can see, which settles
the three things that go wrong and in the order they go wrong: whether
the refresh token works, whether the region is right, and what the
account id actually is. Zoho is regional, and a `.eu` account called
against `.com` fails with an error that reads like a scope problem and is
not one.

Approving a draft queues a send. It does not send one. Between approval
and delivery sit a window, an hourly cap and a daily cap, all in
settings: 08:30 to 17:30 Africa/Lagos, six an hour, thirty a day. A send
outside those is not an error; it waits, and the row says what it is
waiting for.

One send per draft, ever. The row is written and committed before the API
call, so a crash between the two leaves something a person can look at
rather than an email nobody recorded. Sending twice is worse than not
sending.

```bash
python -m app.cli sends
```

## The two directions a draft can lie

Claims about the **recipient** are checked against the pages Cohandle
crawled. A fact carries the url it came from, a draft can only cite facts
that exist, and an address that does not appear on the page it claims to
come from is thrown away.

Claims about **us** have no page to check against, and the first real
batch showed what that costs. The campaign profile said "Partnership
letters sent to EKEDC and Ikeja Electric. No signed agreement yet" and
both drafts wrote that partnerships had been initiated. The prompt
forbade exactly that, in as many words, and it happened anyway.

So the review screen now lists every sentence that asserts something
about us, with the profile lines beside it, and says plainly that it
cannot verify them. It does not block or rewrite: the operator vets these
drafts either way, and the job of the panel is to make that two sentences
rather than the whole email.

## Screens

The menu holds two kinds of screen, and the rule in it separates them.

Left of the rule is the path work takes. **Add** (`/`) is where targets
go in and where you watch them get crawled and read; it is also where you
paste an address Cohandle will not invent and press Draft. **Review**
(`/queue`) is the drafts waiting on your decision, read against the
sourced facts beside them.

Right of the rule is reference, not step three and four. **Targets**
(`/targets`) is everything known, for answering what happened to
something. **Apply & intro** (`/apply`) is the targets where email is the
wrong move because the fund publishes a form, which is work you do by
hand on somebody else's website.

## How it works

```
Paste URLs or emails
  → enrich    crawl, extract facts with sources
  → qualify   score fit, reject with a reason, route to email / form / intro
  → generate  choose a hook from what the research found, compose
  → gate      reject drafts too similar to the rest of the batch
  → review    a person approves or edits every message
  → send      Zoho Mail API, scheduled under a daily cap
  → stop      a reply halts all further contact with that target
```

Two rules the code enforces rather than trusts:

**No claim about a recipient without a source.** Every personalised
sentence traces to a `fact` row with a URL. If enrichment finds nothing
specific enough to open with, the target is flagged for manual research
rather than given a generated pleasantry.

**Nothing sends without a human approving it.**

## Stack

| | |
|---|---|
| API and review queue | FastAPI, Jinja, HTMX |
| Extraction | httpx and trafilatura, with a Chromium renderer for JS-heavy sites |
| Database | Lakebase Postgres on Neon, Frankfurt |
| LLM | Provider SDK behind `instructor`, so outputs validate or raise |
| Hosting | Google Cloud Run, three pieces |
| Mail | Zoho Mail API as the real mailbox, not a sending service |

## Running it

Deployment, secrets and the reasoning behind the infrastructure choices
are in [`deploy/CLOUDRUN.md`](deploy/CLOUDRUN.md).

### On your own machine, which costs nothing to host

Install it once:

```bash
pip install -r requirements.txt
cp .env.example .env     # then fill in DATABASE_URL and a model key
powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1
```

That puts Cohandle on your Desktop and in the Start Menu. Double-click
it: no console, no command line, a chromeless window that looks like an
application. Stop it from the Quit button in its own header, which is
there because with no console there is nothing to Ctrl+C.

Clicking the shortcut again while it is running brings the window back
rather than starting a second copy.

`run-cohandle.cmd` is the same thing with a console attached, for when
something is wrong and you want to watch it. In windowed mode anything
that goes wrong lands in `cohandle-desktop.log` and, if it is fatal, in a
message box.

The intake worker runs the whole time it is open, which is the only thing
the Cloud Run deploy was buying.

It listens on 127.0.0.1 and nothing else, so an app with no login is not
also on the office network.

The only thing that still costs money this way is the model: extraction
and generation are API calls whether Cohandle runs in Frankfurt or on
your desk. Neon's free tier covers the database, because a laptop that
gets closed lets the compute suspend.

### Or by hand

```bash
uvicorn app.main:app --reload --port 8080
curl localhost:8080/readyz
```

`/readyz` returning the Postgres version means the Neon connection works.

### Who gets in

Set `APP_PASSWORD` and the whole app sits behind one shared password and a
signed session cookie, good for 14 days. Leave it unset and there is no
login at all, which is what you want on localhost and what you want behind
Identity-Aware Proxy, and what you must not do on a public URL. Either way
the choice is written to the log at boot.

```bash
python scripts/setkey.py APP_PASSWORD
```

Signing out drops the cookie in that browser. To end every session
everywhere, change the password: the signing key is derived from it.

## State

The deployment and the Neon wiring are done and verified. The pipeline
above is not written yet: `app/main.py` is health checks and a stubbed
poller endpoint. Infrastructure came first so there is somewhere to put
the logic, and so the database and mail paths are proven before anything
depends on them.

## Spec

The full build spec — data model, Zoho OAuth flow, enrichment and
qualification logic, hook selection, the anti-sameness gate, and the v0
task breakdown — lives in the Cohandle build spec document.
