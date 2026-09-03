# Decision register

Why this pipeline differs from a literal reading of [prd.md](../prd.md), and what
would make us revisit each choice.

`CLAUDE.md`'s "Things that are deliberate, not oversights" is the short version
for someone editing the code. This is the long version, with dates and revisit
triggers, and it is where a PRD deviation gets recorded rather than living only
in a source docstring.

All entries dated 2026-09-02 unless stated. Owner: repository maintainer.

---

## C1 — A video-only email derives a poster frame

**Decision.** When an email carries a video but no image, ingest extracts a still
from the video and uses it as the thumbnail source.

**Why.** `prd.md` accepts "Images **and/or** a video", but the thumbnail step
needs an image, so a video-only email used to halt with
`thumbnail.no_source_image`. ffmpeg is already a dependency, so a still costs
nothing.

**Why during ingest, not at thumbnail time.** Stage order is `ingest → safety →
pii → thumbnail`. A frame produced at thumbnail time would be published having
passed through neither image moderation nor PII OCR — and it would turn the
honest `pii.not_screened` finding (which exists *because* video frames are never
examined) into a false one. Extracting at ingest means the frame is screened like
any other image.

**Revisit if** the pipeline ever gains real video-frame moderation, at which
point the frame could be chosen later and better.

---

## C2 — The email subject is the thumbnail text; the description is not

**Decision.** Only the subject is drawn on the thumbnail. The description seeds
the YouTube description.

**Why.** `prd.md` listed "Image(s) and video file and Description" as thumbnail
inputs, which read as a list of available material rather than a requirement to
render all three. A paragraph of body text on a 1080×1920 card is unreadable.

**Revisit if** a caption or overlay line is genuinely wanted — the renderer
already fits and truncates text.

---

## C3 — No third-party URL shortener

**Decision.** `ShortenStage` passes through YouTube's own canonical
`https://youtu.be/<id>`, minted at publish time. `YouTubeCanonicalShortener` does
no network call.

**Why.** `prd.md` says "generate short url". The canonical link *is* short, and it
is already guaranteed to exist. A Bitly or branded domain adds an account, an API
key to store and rotate, a network call on the publish path, and another service
that can be down — for no user-visible gain.

**This was the one PRD deviation recorded nowhere but a source docstring**, which
is why this register exists.

**Revisit if** branded links or click analytics are wanted. The `Shortener`
protocol in `stages/shorten.py` is the seam; `DistributeStage` needs no change.

---

## C4 — "20 MB" is implemented as 20 MiB

**Decision.** The attachment cap is `20 * 1024 * 1024` = 20,971,520 bytes.

**Why.** `prd.md` said "20 MB" without saying which. 20 MiB is 4.8% more generous
than a decimal 20 MB and is the conventional reading for a file-size limit.

**Revisit if** a contract or policy ever requires decimal megabytes.

---

## C5 — The daily cap counts emails, not uploads

**Decision.** `YTSHORT_MAX_EMAILS_PER_DAY` limits *jobs created per UTC day*,
enforced before any mail is fetched and persisted across restarts.

**Why.** That is what `prd.md` asks for, and capping at the front door is what
actually bounds cost and blast radius.

**Note.** `docs/youtube-audit.md` previously described this same counter to Google
as an *upload* cap. Uploads are always ≤ emails because of the human gate, so the
claim was conservative — but it conflated two quantities in a compliance
submission, and has been corrected.

---

## C6 — "Other factors to consider" is now an explicit list

**Decision.** The screening layers are enumerated in `prd.md` rather than left
open.

**Why.** "And other factors" is unbounded and untestable — there is no way to say
whether it is done. The build already chose a concrete set; writing it down makes
"done" decidable and makes a gap visible.

---

## C7 — No YouTube audio download

**Decision.** Background music comes only from `assets/audio/`, supplied by the
operator, with each track's licence recorded in `AUDIO_LICENSES.md`. The pipeline
contains no downloader for YouTube or any other platform.

**Why.** `prd.md` originally named a specific YouTube video to take audio from.
Downloading it breaks YouTube's Terms of Service, the track is copyrighted, and a
Short using it would collect a Content ID claim within minutes. This is also what
`docs/youtube-audit.md` asserts to Google.

**Revisit** — no. A change here would contradict a statement made to a platform
auditor.

**Enforcement.** `ytshort doctor` warns about any track with no manifest row —
the audit submission calls the manifest authoritative, so it should not be an
honour system. The check matches **table rows only**: an earlier version scanned
the whole document, which let a filename mentioned in prose count as recorded,
including in a heading saying to delete the file. A control you can satisfy by
naming the problem is no control.

**The track in use (2026-09-03):** "SereneView" by Arulo, from
[Mixkit](https://mixkit.co/free-stock-music/), under the Mixkit Stock Music Free
License — no account, no attribution, commercial use on web platforms permitted.
The licence text is quoted verbatim in `assets/audio/AUDIO_LICENSES.md` because
Mixkit's page is JS-rendered and cannot be re-fetched as text.

A first attempt used an ElevenLabs track generated on their **free tier**, which
was discarded: their commercial page limits commercial use to "starter+ plans", so
free-tier commercial rights were never established, while `docs/youtube-audit.md`
tells Google the operator holds those rights. Attribution and commercial use are
separate obligations — the ElevenLabs free tier required a credit line *and* did
not grant commercial use, and satisfying the first would not have settled the
second.

**Residual risk, accepted knowingly.** Mixkit carries no equivalent of Google's
guarantee that Audio Library tracks "won't be claimed by a rights holder through
the Content ID system". Its licence forbids others registering its tracks with a
rights management service, which lowers the odds without removing them. This was
traded for not needing an account. **Revisit if** a Content ID claim ever lands —
the YouTube Audio Library is the fallback, and only the manifest row changes.

---

## C8 — No WhatsApp group sink

**Decision.** Dropped from the PRD. The shipped sinks are `file` (always on, the
audit trail) and `email`.

**Why.** Not implementable as asked: neither the WhatsApp Cloud API nor Twilio can
post to a WhatsApp *group*. The sink architecture the PRD actually asked for —
pluggable fan-out with per-sink idempotency — exists and is proven by the two
sinks.

**Revisit if** a 1:1 WhatsApp sink or a relay through an automation platform is
acceptable. Note a relay moves the last mile outside this codebase and outside its
audit trail.

---

## CCOL-1 — The observability layer is a standalone package, not a module

**Decision.** `libs/ccol/` is a uv workspace member with its own version and
**zero required dependencies**. `ytshort` depends on it like any third-party
package.

**Why.** `prd.md` asks for a layer "re-usable across all AI Projects". As a module
inside `ytshort`, that claim would be untestable: another project would have to
install a Gmail-to-YouTube pipeline to get a logger, and nothing would stop `ccol`
importing `ytshort` — with no failing test to notice. The package boundary is the
deliverable.

**Consequence.** `ccol` cannot read `os.environ` and cannot import `ytshort`; it
takes an `ObservabilityConfig` the project builds.

---

## CCOL-2 — The Azure Monitor Distro, behind an optional extra

**Decision.** `azure-monitor-opentelemetry` (the Distro), not the raw exporter
plus a hand-assembled SDK. It is an optional extra; the base install pulls
nothing.

**Why.** `configure_azure_monitor()` attaches to the root logger, exactly where
this app already installs handlers — so every existing `extra={...}` dict becomes
`customDimensions` with no call-site changes, and all three signals wire up in one
call. Assembling providers by hand is ~80 lines we would then own forever.

**Consequence.** With no connection string, no telemetry package is imported at
all. `tests/unit/test_telemetry_offline.py` asserts this against `sys.modules` —
it is the strongest available proof that the suite cannot reach the network.

---

## CCOL-3 — `job_id` is the correlation id; no new field on `Job`

**Decision.** No `correlation_id` field was added to the `Job` model.

**Why.** `job_id` is already derived deterministically from the Gmail message id,
and is already the join key for the job record, the media directory, delivery
idempotency, and the lock file. A second identifier needs a reconciliation rule
and a migration of every persisted job, for no new information.

---

## CCOL-4 — No ARM template override to carry a trace across the job trigger

**Decision.** The review app does not pass a `traceparent` when it starts the
scheduled Job. Both tiers key on `job_id`, and the ARM execution name is logged so
the two can be joined in KQL.

**Why.** The Container Apps Jobs `start` API *does* support env overrides — but
the override **replaces** the container spec rather than merging into it, so
omitting `env` would launch the execution with no data dir, no allow-list and no
vault URI. And one `ytshort run` execution advances *every* pending job, so a
single traceparent would falsely parent other jobs' work.

**Revisit if** true distributed tracing is wanted. The correct shape is an OTel
span *link*, not a parent, carried over the file share the two tiers already
share — and that is the only reason to add a field to `Job`.

---

## SEC-1 — Bounded retries with backoff

**Decision.** A stage records its attempt count and a `retry_not_before`
timestamp. Retries back off exponentially with jitter and are capped; past the cap
the job is dead-lettered to `failed`.

**Why.** Nothing counted attempts before, so a permanently broken dependency was
re-attempted on every scheduled tick forever — a self-inflicted DoS against
YouTube, Anthropic and VirusTotal, and a cost that grows on its own. A malicious
attachment that reliably crashes ffmpeg was an unbounded retry loop anyone could
trigger by sending an email.

Jitter matters because one execution advances every pending job: without it, ten
jobs failing against the same rate-limited API would retry in lockstep.

`RetryableFailure.retry_after_seconds` had been dead code implying a guarantee
that did not exist. It is now the floor for the backoff.

---

## SEC-2 — Only transient faults are retried

**Decision.** `is_transient()` classifies a fault before it becomes a
`RetryableFailure`. 408/429/5xx and socket-level errors retry; everything else,
including any unrecognised exception, halts.

**Why.** Publish caught bare `Exception` and retried all of it, so a 403 for an
exhausted quota, an unverified channel or a revoked credential retried as eagerly
as a timeout. An unrecognised exception defaults to *permanent* because a bug in
our own code retried on a timer is a bug you learn about from the bill.

---

## SEC-3 — The audio directory lives on the mounted share

**Decision.** `YTSHORT_AUDIO_DIR` points at `/data/assets/audio` in Azure.

**Why.** `assets/audio/*` is gitignored *and* dockerignored (music is not ours to
redistribute), so the in-image directory is always empty. Pointing at the image
meant every deployed compose failed with "No licensed audio track found" — on
every scheduled run, forever, which SEC-1 would now dead-letter but which should
not happen at all.

---

## AI-1 — The thumbnail is AI-*directed*, never AI-*generated*

**Decision.** A model writes the thumbnail *hook* and picks a colour and a text
position. The picture is always the sender's own screened attachment, composited
by Pillow exactly as before. No pixels are generated.

**Why.** The email subject is accurate but reads as a caption, and the goal is
click-through. Rewriting it into a short hook is what a language model is good at.
Layout, wrapping and contrast are geometry, which code does exactly and a model
does approximately, slower and for money — so those stay in `render_thumbnail`.

**Why it does not weaken the safety chain.** `stages/safety.py` and the PII OCR
screen *attachments*. Because the composited picture is still the screened
attachment, those guarantees are untouched. Synthesised imagery would break this,
which is why it is out of scope.

**Model output is untrusted.** Every hook passes `ThumbnailDirection.sanitised()`
(length, closed sets, `#RRGGBB` validation, emphasis-must-occur) and then
`sanitise_title()` at render time — the same treatment an attacker-supplied email
subject gets, because that is exactly what it is.

**Revisit if** genuinely generated imagery is ever wanted. That is a different
decision with licensing and compliance consequences, and it contradicts what
`docs/youtube-audit.md` tells Google about where the content comes from.

---

## AI-2 — Microsoft Foundry over the Anthropic API

**Decision.** `YTSHORT_ART_DIRECTOR=foundry` against an existing Azure Foundry
`gpt-4o-mini` deployment, authenticated with the job's **managed identity**.

**Why not Anthropic.** The original request was to use a Claude Max subscription
with the key in Key Vault. Max includes no API access or credits — Anthropic sells
Individual, Team & Enterprise and API as separate products — and a subscription
session is the wrong credential shape for an unattended cron job regardless: it
expires and needs interactive refresh.

**Why Foundry is better here, not merely adequate.** It needs **no API key at
all**, so the original "pull the key from Key Vault" requirement disappears —
nothing to store, rotate or leak. `DefaultAzureCredential` already has
`AZURE_CLIENT_ID` set on the ingest job. Billing lands on the existing Azure
subscription, and image data stays in the tenant, which matters given the pipeline
screens for PII and makes source claims to Google.

**Cost control.** Images are downscaled to a 768 px long edge before sending.
Vision input is billed by pixel count, so this is the difference between a few
hundred tokens and tens of thousands per job.

**Revisit if** hook quality on `gpt-4o-mini` reads flat. The deployment name is an
env var, so trying `gpt-4o` is a config change and a re-render.

---

## AI-3 — Three hooks, chosen by a human

**Decision.** The model returns three hooks, plainest to boldest; the reviewer
picks one or writes their own, and the thumbnail re-renders.

**Why.** Misleading thumbnails are exactly what YouTube's metadata policy targets,
and the channel is mid-compliance-audit. The system prompt requires every hook to
describe what is visible; bolder variants are bolder *phrasing*, not stronger
*claims*. A human choosing is the real control.

**The non-obvious consequence.** `ComposeStage` splices the thumbnail onto both
ends of the video, so re-rendering after compose would leave the uploaded
thumbnail and the video's own bumper frames disagreeing — silently, and only
visible once published. The re-render therefore clears the `compose` stage record,
using the runner's existing resume mechanism to rebuild the video before publish.

**Why the review app can do this safely.** Re-rendering is local Pillow work. It
calls no model (the hooks were generated during the pipeline run and stored on the
job) and holds no credential, so the internet-facing tier stays as powerless as
design rule 8 requires.
