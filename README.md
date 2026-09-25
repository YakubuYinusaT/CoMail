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

Zoho applies the signature configured in its web client to neither mail
sent through the API nor drafts created through it. Both were tested and
both arrived without it. So Cohandle carries the designed signature
itself, which means the message is HTML.

Only the signature is designed. The letter is paragraphs and nothing
else: no fonts, no colours, no tables, no tracking. A first approach to
an investor that arrives styled reads as a campaign, and this tool exists
so that it does not. `HTML_EMAIL=false` falls back to the plain text
signature, and so does a missing `signature.html`.

`DELIVERY_MODE` decides what approving a draft does, and it is `draft`.

In draft mode Cohandle writes the email into your Zoho **Drafts** folder
and stops. You open Zoho, read it once more and press send yourself.
That is where your designed HTML signature gets applied, because Zoho's
web composer adds it and its API does not. Nothing is delivered by
Cohandle, so the arming switch, the sending window and the caps do not
apply: a message in your own Drafts folder has no recipient to annoy.

The row records `drafted`, never `sent`, and leaves `sent_at` empty.
Nobody has received anything and the message may sit in Drafts forever,
so saying otherwise would put a claim in the database that nothing
supports.

Zoho's reply is checked rather than trusted. If it ever stops honouring
`mode`, the difference is a private note versus a cold email an investor
has already read, so an unconfirmed draft is treated as a failure.

Set `DELIVERY_MODE=send` to deliver directly instead. Everything below
governs that mode only.

Nothing leaves until `SEND_ENABLED=true`. Credentials are not permission:
with a working Zoho token and that flag false, Cohandle does every step
up to the API call and records what it would have sent.

Set Zoho up in one sitting by double-clicking `zoho-setup.cmd` in the
CoHandle folder. It runs from its own folder, so it does not matter
where you start it from. From a terminal, be inside the project first:

```bash
cd "C:/Users/USER/Documents/Connected Intelligence/CoHandle"
python scripts/zoho_setup.py
```

It asks for your region, your client id, your client secret and the code
from Zoho's API console, trades the code for a refresh token, finds your
account id for you and writes all of it into .env. Nothing is echoed and
nothing is passed as an argument where a shell history would keep it.

A note on plans. Zoho removed IMAP, POP and SMTP from new free accounts,
and describes API access as depending on the plan. Whether the send API
works on the free plan is something the setup script will tell you at the
end, when it asks Zoho for your mailbox list. If that call is refused
because of the plan, no amount of configuration gets around it and the
answer is to move to Mail Lite or to send through something else.

Prove the credentials again later without putting anything in an inbox:

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

## Noticing when somebody writes back

A follow-up sent to a fund that already replied undoes the one thing that
went right, and reads as though nobody was paying attention. So the
signal that stops it exists before follow-ups do.

Matching is on the sender, not on a thread. Zoho's headers carry no
thread id, and a reply often comes from a different person at the same
fund: you write to hello@fund.com and a partner answers from their own
address. So inbound counts as a reply when the sender is an address held
for that target, or the sender's domain is the target's domain, and it
arrived after the send.

Three kinds of inbound are told apart, because confusing them is wrong in
three different ways. A reply is a person and stops everything. An
automatic reply is a machine saying nobody is reading, and must not be
mistaken for interest. A bounce means the address is wrong and nothing
arrived, so the target has not been contacted at all, and the send is
marked bounced rather than sent.

Every folder inbound can land in is scanned, not just the Inbox. Filters
sort mail into others, and a reply put in Newsletter by a rule is still a
reply. Spam is included for the same reason: a missed reply there is the
expensive kind of miss.

A target that replied sorts above everything else on the Add screen,
because it is the only state meaning somebody is waiting on you.

Nothing reads a message body. The snippet Zoho puts in the header is
enough to recognise what arrived.

## Knowing what you actually sent

Cohandle writes a draft into Zoho and then loses sight of it. You might
send it, edit it first, sit on it for a week, or delete it. Until it
knows which, its record of who you have approached is half a record, and
anything built on top of that, follow-ups most of all, is built on a
guess.

So it reads your Sent folder and marks the ones that went:

```bash
python -m app.cli reconcile
```

The worker also does this every ten minutes while it is running.

Matching is on recipient and time, not on the message id. Zoho hands back
an id when the draft is created, and a draft edited and sent from the web
client does not reliably keep it. The recipient is the stable fact: one
target, one address, one approach. The subject is compared loosely,
because editing it before sending is normal and should not break the
match, and a message sent before the draft existed is skipped as an
earlier approach to the same fund.

This needs `ZohoMail.messages.READ` and `ZohoMail.folders.READ`, which
the first setup did not ask for. Re-run `zoho-setup.cmd` and the scope it
prints now includes them. Until then the command says so rather than
failing obscurely. Nothing here writes to the mailbox.

## Addresses

Cohandle once asked the model for email addresses and it produced five,
plausible and wrong, for real named staff at a real fund. The field was
removed, and every address came from the operator by hand.

Finding is back; inventing is not. Addresses are matched out of the
crawled page with a regular expression, in both the markup and the
extracted text, so a `mailto:` link and a printed address are both found
and an address that is not on the page cannot be produced. Each one is
stored against the page it appeared on.

A found address is marked unverified and does not move a target along. A
pattern match proves the characters were on the page, not that anybody
reads what arrives there, so the row shows the address with a link to
where it was found and waits for you to press Use this.

The noise filtering matters as much as the finding. `logo@2x.png` reads
as an address, so do a site builder's telemetry addresses and the
placeholders in template markup. A panel that lists those is a panel
that gets ignored.

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
(`/targets`) is everything known, and every row opens
`/target/<domain>`: the facts with the page each came from, the scoring
broken down criterion by criterion, the addresses and where they were
found, the pages crawled, and whatever has happened to the email. **Apply & intro** (`/apply`) is the targets where email is the
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
