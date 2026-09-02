# ytshort — Gmail → YouTube Short, with a human in the loop

Watches a Gmail inbox for image and video attachments from an allow-listed sender,
screens the media for malware and PII, renders a vertical YouTube Short (title
bumper → media → title bumper, over a licensed background track), **parks for
human approval**, publishes to YouTube, and fans the resulting short URL out to
configurable sinks.

Nothing is published without a person clicking Approve.

Runs two ways from one codebase: a local CLI plus a localhost review UI for
development, and a scheduled Azure Container Apps Job plus an authenticated web
app for production.

---

## Architecture

### Deployed shape

```
                 ┌──────────────── Container Apps Environment ────────────────┐
                 │                                                            │
   cron 0 * * *  │  ┌──────────────────────┐      ┌──────────────────────┐   │
   ─────────────►│  │ Job: ytshort-run     │      │ App: ytshort-review  │◄──┼── HTTPS
                 │  │ Schedule trigger     │◄─────┤ external ingress     │   │   + Entra ID
                 │  │ cmd: ytshort run     │ start│ cmd: review --serve  │   │     EasyAuth
                 │  │ ★ holds Google creds │ (ARM)│ ★ NO Google creds    │   │
                 │  └──────────┬───────────┘      └──────────┬───────────┘   │
                 └─────────────┼─────────────────────────────┼───────────────┘
                               └──────────┬──────────────────┘
                                          │ /data  (Azure Files)
                              ┌───────────▼────────────┐     ┌──────────────────┐
                              │ job records · media    │     │ Key Vault        │
                              │ outputs (no secrets)   │     │ google refresh   │
                              └────────────────────────┘     │ token, csrf, …   │
                                                             └──────────────────┘
```

**The review app is the only thing exposed to the internet, and it deliberately
holds nothing worth stealing.** It records the approval and asks the Job — the
only workload that can read Key Vault or talk to Google — to run. A compromise of
the web tier costs you a job record, not your mailbox. See
[docs/security.md](docs/security.md).

### Pipeline

```
   ingest ──► safety ──► pii ──► thumbnail ──► compose ──► ⏸ review ──► publish ──► shorten ──► distribute
      │          │         │                                   │
   allow-list  magic     OCR +                          human approves
   ≤10/day     bytes    detectors                       or rejects
   ≤20 MB      EXIF strip  │
                malware    └── blocking finding ──► quarantined (never reviewable)
                moderation
```

State machine, persisted after every stage so any run is resumable:

```
discovered → ingested → screened → composed → awaiting_review
                → approved → published → distributed → done
      ↘ quarantined        ↘ rejected              ↘ failed
```

---

## Tech stack

| Piece | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Matches the media/AI tooling |
| Packaging | `uv` + hatchling | Fast, lockfile-backed, `src/` layout |
| Data model | pydantic v2 | One `Job` document validated on every read and write |
| CLI | typer | Subcommands with real `--help` for free |
| Review UI | FastAPI + Jinja2 | Previews the actual rendered MP4 before approval |
| Images | Pillow | Thumbnail composition, EXIF stripping, bomb guards |
| Video | ffmpeg (subprocess) | Concat + normalise + audio mix; no GPL binding in-process |
| Google APIs | google-api-python-client | Gmail read/send and YouTube upload on one OAuth client |
| Moderation | Claude vision (optional) | Off by default; strict-schema verdict, never free-form JSON |
| Compute | Azure Container Apps | Scheduled Job for the pipeline, Container App for review |
| Secrets | Azure Key Vault | Refresh token read at run time, never written to disk |
| State | Azure Files | Job records and media, shared by both workloads |
| Infra | Bicep + Azure Verified Modules | Two templates; ordering made explicit |
| Auth | Entra ID EasyAuth | Platform-enforced; no auth code in the app |
| Tests | pytest | Fakes for Gmail/YouTube/scanner — full pipeline runs offline |

---

## Local development

Enough to run and hack on it. **For first-time setup of the Google account,
Google Cloud project, and Azure, follow [deployment.md](deployment.md) instead** —
it starts from having no accounts at all.

```bash
uv sync                       # add --native-tls if your network intercepts TLS
winget install Gyan.FFmpeg    # or set YTSHORT_FFMPEG_PATH
cp .env.example .env          # then edit
```

At minimum set `YTSHORT_ALLOWED_SENDERS` — the app refuses to start without it —
and drop a licensed track into `assets/audio/` (see
[AUDIO_LICENSES.md](assets/audio/AUDIO_LICENSES.md)).

```bash
uv run ytshort doctor         # checks ffmpeg, credentials, audio, storage
uv run ytshort auth login     # one-time browser consent
uv run pytest                 # media tests self-skip without ffmpeg
```

### Commands

| Command | What it does |
|---|---|
| `ytshort run` | Ingest new mail, screen, render, park for review |
| `ytshort review --serve` | Serve the review UI on 127.0.0.1:8080 |
| `ytshort status [job_id]` | What is in flight, or one job's stage trail and findings |
| `ytshort approve <job_id>` | Approve from the terminal instead of the UI |
| `ytshort reject <job_id> -r "reason"` | Reject with a reason |
| `ytshort resume <job_id>` | Continue an approved or retried job |
| `ytshort prune --older-than 30` | Delete media for finished jobs, keeping records |
| `ytshort visibility --all --to public` | Promote private uploads once the audit clears |
| `ytshort auth login \| status \| export` | Consent, check, and export for Key Vault |
| `ytshort doctor` | Check every prerequisite before a run |

`run` is safe to re-run at any time: completed stages are skipped, a published job
is never uploaded twice, and an email that already has a job is never ingested
again.

---

## Deployment

First-time Google account, Google Cloud project, compliance audit, and the full
Azure deployment are covered end to end in **[deployment.md](deployment.md)** —
eleven phases, starting from no accounts at all, with a verification step after
each one.

> **CI/CD:** deployment is currently driven by the documented `az` CLI steps. An
> **Azure DevOps pipeline is planned and will be added later**, wrapping the
> build and deploy phases with workload identity federation.

---

## Security posture

Full detail in **[docs/security.md](docs/security.md)**. The essentials:

- **Allow-list, never block-list**, for both senders and media types. The sender
  allow-list is mandatory — an empty one would let anyone who emails the mailbox
  queue media for publication.
- **Magic-byte verification.** A `.exe` renamed `photo.png` is quarantined.
- **Malware scanning by hash reputation** in the deployed container — only the
  SHA-256 is sent to VirusTotal, never the file itself.
- **Metadata is stripped**, not ignored: EXIF/GPS from images, all container
  metadata from video.
- **Untrusted filenames** are flattened through `safe_filename()`; traversal
  cannot escape the job directory.
- **Findings are never silently dropped.** A gate that could not run says so —
  "not scanned" and "scanned, clean" are different states, and the reviewer sees
  which is which.
- **A `blocking` finding never becomes approvable.** The job is quarantined
  instead.
- **Minimal OAuth scopes:** `gmail.readonly`, `gmail.send`, `youtube.upload`,
  `youtube.force-ssl`. `gmail.modify` — write over the whole mailbox — is
  deliberately not requested.
- **The refresh token lives in Key Vault**, is read into memory at run time, and
  is never written to disk in the deployed system.

**Review UI authentication differs by environment, deliberately:**

| | Local development | Deployed |
|---|---|---|
| Bind | `127.0.0.1` only | `0.0.0.0` behind ingress |
| Auth | **None** | Entra ID EasyAuth, single tenant, one assigned user |
| Setting | `YTSHORT_AUTH_MODE=none` | `YTSHORT_AUTH_MODE=platform` |

The local mode has no authentication and must never be exposed on a network
interface. `platform` mode asserts that a gateway is authenticating in front of
it; setting it without one is the mistake that turns this into a public
approve-anything button.

---

## Known constraints

| Constraint | Detail |
|---|---|
| Uploads are locked to private | YouTube force-locks `videos.insert` uploads to private for any API project created after 2020-07-28 that has not passed a [compliance audit](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits). `YTSHORT_PRIVACY_STATUS` only takes effect afterwards; `ytshort visibility` then promotes the backlog. |
| `thumbnails.set` needs a verified channel | Returns 403 without phone verification. The job still publishes; `thumbnail_set` is recorded as `false`. |
| Shorts format | Vertical 1080×1920, ≤180 s, `#Shorts` in the description. Longer renders are trimmed at compose time. |
| No YouTube audio download | Background music comes from `assets/audio/`. Downloading audio from YouTube breaks its Terms of Service, so the pipeline cannot do it. |
| WhatsApp group sink | Not implemented. Neither the WhatsApp Cloud API nor Twilio can post to a *group* — a future version needs a share link or an automation-platform hop. |

---

## Tests

```bash
uv run pytest                 # full suite; media tests self-skip without ffmpeg
uv run pytest -m ffmpeg       # only the tests that need a real ffmpeg
uv run pytest --cov=ytshort   # with coverage
uv run ruff check .
```

The suite never touches the network. `tests/fakes.py` provides in-memory Gmail and
YouTube clients with the same protocols as the real ones, so the entire pipeline —
including publish and fan-out — runs end to end offline.
