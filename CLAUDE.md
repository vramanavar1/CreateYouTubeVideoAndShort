# CLAUDE.md — ytshort

Gmail → YouTube Short pipeline with a human approval gate. Read [README.md](README.md)
for setup and architecture; this file is the working agreement for changing the code.

## Skills to apply

- `coding-principles` and `coding-guidelines` for any implementation work
- `secure-coding-practices` when touching ingest, safety, PII, the web layer, or
  anything handling credentials
- `claude-api` before writing or changing `integrations/moderation.py`

## Design rules that are load-bearing

Break these and something quietly stops being safe.

1. **Findings are never dropped.** Every screening layer records a `Finding` —
   including when it *could not run*. A job that was not scanned must never look
   like a job that was scanned and came back clean. See `stages/safety.py`.
2. **`blocking` means the reviewer never sees it as approvable.** Blocking
   findings halt into `quarantined`. Do not add a UI path that lets someone
   approve past one.
3. **Stage names are an API.** They are the keys in `Job.stages`, and the runner
   skips completed stages by name. Renaming one makes every existing job re-run
   it — including `publish`, which would upload a second copy.
4. **Publish is guarded by `video_id`.** `PublishStage` returns early if the job
   already has one, and persists the job immediately after upload, before the
   thumbnail call. Keep that ordering.
5. **Sinks never fail the job.** `DistributeStage` catches everything. The video
   is already live by then; a failed notification must not drive the job into
   `failed`, because a retry of a failed job re-enters the pipeline.
6. **Attachment filenames are attacker-supplied.** Everything written to disk
   goes through `safe_filename()`; everything read back goes through
   `MediaStore.resolve()`, which refuses to escape the job directory.
7. **No module reads `os.environ` directly** — they take a `Settings`. That is
   what keeps the tests off the developer's real inbox.
8. **The review app must never hold a Google credential.** It is the only
   internet-facing component. It records decisions and asks the scheduled Job to
   do privileged work; it does not publish. Enforced in `web/app.py`
   (`with_google=not job_trigger_enabled`), in `apps.bicep` (its identity gets no
   vault-wide role), and by
   `tests/unit/test_web_security.py::TestNoGoogleCredentialInTheWebTier`.
9. **No secret may become a Bicep `output` or a non-`@secure()` parameter.**
   Deployment history is plain text and readable by anyone with Reader on the
   resource group.
10. **`/health` must stay empty.** It is excluded from platform authentication so
    the readiness probe can reach it, which makes it internet-reachable without
    credentials. Diagnostics belong on `/health/detail`, behind auth.
11. **The pipeline holds `gmail.readonly`, not `gmail.modify`.** Do not add a
    stage that labels, marks read, or otherwise writes to the mailbox — the
    narrow `GmailClientProtocol` exists to make that impossible by accident.

## Things that are deliberate, not oversights

- **No YouTube audio download.** The PRD asked for the audio from a specific
  YouTube video; that breaks YouTube's ToS and the track is copyrighted. Audio
  comes from `assets/audio/` only. Do not add a downloader.
- **Uploads default to `private`.** YouTube force-locks uploads from an
  unaudited API project to private regardless of the request. The default matches
  reality rather than fighting it.
- **The review UI has no auth and binds to 127.0.0.1.** The CSRF double-submit
  token is what stops a random web page POSTing an approval to localhost. If this
  ever moves off loopback it needs real authentication first.
- **The malware scanner fails *open* with a `warn` finding**, because there is a
  human gate downstream. A variant that publishes without review must flip this
  to blocking.
- **Medium-confidence PII never hard-blocks.** Only checksum-validated hits
  (Luhn, Verhoeff, PAN) can quarantine under `YTSHORT_PII_POLICY=block`;
  otherwise an order number would strand jobs.

## Adding a stage

1. Subclass `BaseStage` in `src/ytshort/stages/`, set a stable `name` and a
   `success_state` (or `None` if it only annotates).
2. Raise `HaltPipeline` / `SuspendPipeline` / `RetryableFailure` — never return a
   status. Anything else that escapes is treated as a bug and fails the job.
3. Register it in `stages/__init__.py:build_stages()` in execution order.
4. Add tests using the fakes in `tests/fakes.py`. Do not reach for the network.

## Adding a sink

Implement the `Sink` protocol (`sinks/base.py`) and register it in
`sinks/registry.py`. Use `job.delivery_id_for(name)` for any idempotency key the
target supports. The `file` sink is always enabled and must stay that way — it is
the audit trail.

## Tests

```bash
uv run pytest                # full suite; media tests self-skip without ffmpeg
uv run pytest -m ffmpeg      # only the real-render tests
uv run ruff check .
```

The suite never touches the network. If a change makes a test need credentials,
the change is wrong — extend `tests/fakes.py` instead.

## Secrets

`.env` is gitignored, and so are `client_secret*.json` / `token*.json`. The OAuth
client secret and cached token live at paths named in `.env`, outside the repo.
Never commit a real value into `.env.example`.
