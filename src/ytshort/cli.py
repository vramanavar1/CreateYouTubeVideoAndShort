"""Command line entry point.

``run`` is the one to know: discover new mail, then drive each job as far as it
can go. It is safe to run repeatedly -- completed stages are skipped, published
videos are never re-uploaded, and an email that already has a job is never
ingested twice.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import typer
from ccol import current, new_correlation_id, use_correlation

from ytshort.config import ConfigError, Settings
from ytshort.contracts.models import JobState
from ytshort.observability.logging import get_logger
from ytshort.runtime import bootstrap, build_context, record_decision, resume_job, run_job

app = typer.Typer(
    help="Turn Gmail attachments into YouTube Shorts, with a human approval gate.",
    no_args_is_help=True,
    add_completion=False,
)
auth_app = typer.Typer(help="Google OAuth for Gmail and YouTube.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")

log = get_logger(__name__)

_OK = "  [ok]  "
_BAD = " [fail] "
_WARN = " [warn] "


def _settings(strict: bool = True) -> Settings:
    try:
        return bootstrap(Settings.load(strict=strict))
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def run(
    limit: int = typer.Option(
        None, "--limit", "-n", help="Cap how many new emails to ingest this run."
    ),
    no_discover: bool = typer.Option(
        False, "--no-discover", help="Only advance existing jobs; do not read new mail."
    ),
) -> None:
    """Ingest new mail, screen it, render a Short, and park it for review."""
    from ytshort.stages import discover_jobs

    settings = _settings()
    ctx = build_context(settings)

    jobs = []
    if not no_discover:
        if ctx.gmail is None:
            typer.secho(
                "Gmail is not authorised. Run 'ytshort auth login' first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        jobs = discover_jobs(ctx, limit=limit)
        typer.echo(f"Discovered {len(jobs)} new email(s).")

    # Anything not yet finished gets another push -- this is what makes a rerun
    # after fixing ffmpeg, or after an approval, do the right thing.
    pending = [
        job
        for job in ctx.job_store.iter_jobs()
        if not job.is_terminal and job.state is not JobState.awaiting_review
    ]
    seen = {job.job_id for job in jobs}
    pending = jobs + [job for job in pending if job.job_id not in seen]

    if not pending:
        typer.echo("Nothing to do.")
        return

    for job in pending:
        outcome = run_job(job, ctx)
        _print_outcome(outcome)


@app.command()
def resume(job_id: str) -> None:
    """Push one job as far as it can go (after approval, or after a fix)."""
    settings = _settings()
    ctx = build_context(settings)
    outcome = resume_job(job_id, ctx)
    if outcome is None:
        typer.secho(f"No job with id {job_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    _print_outcome(outcome)


@app.command()
def approve(
    job_id: str,
    title: str = typer.Option(None, "--title", help="Override the video title."),
    publish: bool = typer.Option(
        True, "--publish/--no-publish", help="Continue the pipeline immediately."
    ),
) -> None:
    """Approve a parked job from the terminal instead of the review UI."""
    settings = _settings()
    ctx = build_context(settings)

    job = ctx.job_store.load(job_id)
    if job is None:
        typer.secho(f"No job with id {job_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if job.state is not JobState.awaiting_review:
        typer.secho(
            f"Job is {job.state.value}, not awaiting review.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)

    if title:
        job.title = title[:100]
    record_decision(job, ctx, decision="approved", reviewer="cli")
    typer.echo(f"Approved {job_id}.")

    if publish:
        _print_outcome(run_job(job, ctx))


@app.command()
def reject(
    job_id: str,
    reason: str = typer.Option("", "--reason", "-r", help="Why it was rejected."),
) -> None:
    """Reject a parked job."""
    settings = _settings()
    ctx = build_context(settings, with_google=False)

    job = ctx.job_store.load(job_id)
    if job is None:
        typer.secho(f"No job with id {job_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    record_decision(job, ctx, decision="rejected", reviewer="cli", reason=reason)
    typer.echo(f"Rejected {job_id}.")


@app.command()
def review(
    serve: bool = typer.Option(True, "--serve/--list", help="Serve the UI, or just list."),
) -> None:
    """Open the local review UI (or list what is waiting)."""
    settings = _settings(strict=False)

    if not serve:
        ctx = build_context(settings, with_google=False)
        waiting = ctx.job_store.list_jobs(JobState.awaiting_review)
        if not waiting:
            typer.echo("Nothing awaiting review.")
            return
        for job in waiting:
            typer.echo(f"{job.job_id}  {job.title or job.source.subject}")
        return

    from ytshort.web.app import serve as serve_ui

    serve_ui(settings)


@app.command()
def status(job_id: str = typer.Argument(None, help="Show one job in detail.")) -> None:
    """Show what is in flight, or one job's full stage trail and findings."""
    settings = _settings(strict=False)
    ctx = build_context(settings, with_google=False)

    if job_id is None:
        jobs = list(ctx.job_store.iter_jobs())
        if not jobs:
            typer.echo("No jobs yet.")
            return
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        width = max(len(j.state.value) for j in jobs)
        for job in jobs:
            title = (job.title or job.source.subject or "(no subject)")[:52]
            typer.echo(f"{job.job_id[:12]}  {job.state.value:<{width}}  {title}")
        return

    job = ctx.job_store.load(job_id)
    if job is None:
        typer.secho(f"No job with id {job_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo(f"job      {job.job_id}")
    typer.echo(f"state    {job.state.value}")
    typer.echo(f"subject  {job.source.subject}")
    typer.echo(f"from     {job.source.sender}")
    if job.publication:
        typer.echo(f"url      {job.publication.short_url} ({job.publication.privacy_status})")
    if job.error:
        typer.echo(f"error    {job.error}")

    typer.echo("\nstages")
    for name, record in job.stages.items():
        detail = f"  {record.detail}" if record.detail else ""
        typer.echo(f"  {name:<12} {record.status.value:<10} {record.duration_ms or 0}ms{detail}")

    if job.findings:
        typer.echo("\nfindings")
        for finding in job.findings:
            typer.echo(f"  [{finding.severity.value:<8}] {finding.kind}: {finding.detail}")

    if job.deliveries:
        typer.echo("\ndeliveries")
        for delivery in job.deliveries:
            typer.echo(
                f"  {delivery.sink:<8} {'ok' if delivery.ok else 'failed'}  {delivery.detail}"
            )


@app.command()
def prune(
    older_than: int = typer.Option(
        None, "--older-than", help="Days. Defaults to YTSHORT_MEDIA_RETENTION_DAYS."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without deleting."),
) -> None:
    """Delete media for finished jobs, keeping the job records.

    Without this, every photo ever emailed accumulates forever -- a storage bill
    and a privacy liability. The job record and its findings survive, so the audit
    trail is intact; only the bytes go.
    """
    import shutil
    from datetime import UTC, datetime, timedelta

    settings = _settings(strict=False)
    ctx = build_context(settings, with_google=False)
    days = older_than if older_than is not None else settings.media_retention_days
    cutoff = datetime.now(UTC) - timedelta(days=days)

    freed = 0
    pruned = 0
    for job in ctx.job_store.iter_jobs():
        if not job.is_terminal or job.updated_at > cutoff:
            continue
        job_dir = settings.media_dir / job.job_id
        if not job_dir.is_dir():
            continue

        size = sum(f.stat().st_size for f in job_dir.rglob("*") if f.is_file())
        typer.echo(f"{'would prune' if dry_run else 'pruning'} {job.job_id[:12]}  {size:,} bytes")
        if not dry_run:
            shutil.rmtree(job_dir, ignore_errors=True)
            job.media.composed_video = None
            job.media.thumbnail_tall = None
            job.media.thumbnail_wide = None
            job.media.primary_image = None
            job.media.primary_video = None
            ctx.job_store.save(job)
        freed += size
        pruned += 1

    verb = "would free" if dry_run else "freed"
    typer.echo(f"{pruned} job(s), {verb} {freed:,} bytes (retention {days}d)")


@app.command()
def visibility(
    job_id: str = typer.Argument(None, help="One job, or omit with --all."),
    to: str = typer.Option("public", "--to", help="private | unlisted | public"),
    all_jobs: bool = typer.Option(False, "--all", help="Every published job."),
) -> None:
    """Change the visibility of already-published videos.

    The reason this exists: YouTube force-locks uploads from an API project that
    has not passed its compliance audit to private. Once the audit clears, this
    promotes the backlog in one go. Needs the youtube.force-ssl scope.
    """
    if to not in ("private", "unlisted", "public"):
        typer.secho("--to must be private|unlisted|public", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if not job_id and not all_jobs:
        typer.secho("Give a job id or --all.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    settings = _settings()
    ctx = build_context(settings)
    if ctx.youtube is None:
        typer.secho("YouTube is not authorised.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if all_jobs:
        targets = [
            j
            for j in ctx.job_store.iter_jobs()
            if j.publication and j.publication.privacy_status != to
        ]
    else:
        job = ctx.job_store.load(job_id)
        if job is None or job.publication is None:
            typer.secho(f"No published job with id {job_id}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        targets = [job]

    if not targets:
        typer.echo(f"Nothing to change; everything is already {to}.")
        return

    changed = 0
    for job in targets:
        assert job.publication is not None
        try:
            applied = ctx.youtube.set_visibility(job.publication.video_id, to)
        except Exception as exc:  # noqa: BLE001 - report and continue the batch
            typer.secho(f"  {job.publication.video_id}: {exc!r}", fg=typer.colors.RED)
            continue

        job.publication.privacy_status = applied
        ctx.job_store.save(job)
        changed += 1
        colour = typer.colors.GREEN if applied == to else typer.colors.YELLOW
        typer.secho(f"  {job.publication.short_url} -> {applied}", fg=colour)
        if applied != to:
            typer.secho(
                "    YouTube kept it private -- the API project has not passed its "
                "compliance audit yet.",
                fg=typer.colors.YELLOW,
            )

    typer.echo(f"{changed} of {len(targets)} updated.")


@app.command()
def doctor() -> None:
    """Check every prerequisite before a run, and say exactly what is missing."""
    from ytshort.integrations.audio import AudioSource
    from ytshort.integrations.ffmpeg import FFmpeg
    from ytshort.integrations.google_auth import describe_credentials
    from ytshort.integrations.scanner import DefenderScanner

    settings = _settings(strict=False)
    problems = 0

    def report(ok: bool, message: str, *, warn_only: bool = False) -> None:
        nonlocal problems
        if ok:
            typer.secho(f"{_OK}{message}", fg=typer.colors.GREEN)
        elif warn_only:
            typer.secho(f"{_WARN}{message}", fg=typer.colors.YELLOW)
        else:
            typer.secho(f"{_BAD}{message}", fg=typer.colors.RED)
            problems += 1

    typer.echo("ytshort doctor\n")

    ffmpeg = FFmpeg.from_settings(settings)
    report(
        ffmpeg.available,
        f"ffmpeg: {ffmpeg.ffmpeg or 'not found'} / {ffmpeg.ffprobe or 'not found'}"
        if ffmpeg.available
        else "ffmpeg/ffprobe not found -- install it (winget install Gyan.FFmpeg) "
        "or set YTSHORT_FFMPEG_PATH",
    )

    credentials = describe_credentials(settings)
    report(credentials.ok, f"google auth: {credentials.detail}")

    report(
        bool(settings.allowed_senders),
        f"sender allow-list: {len(settings.allowed_senders)} address(es)"
        if settings.allowed_senders
        else "YTSHORT_ALLOWED_SENDERS is empty -- anyone could queue media for publication",
    )

    if settings.auth_mode == "platform":
        report(True, "review UI: authentication delegated to the platform (EasyAuth)")
        report(
            bool(settings.csrf_secret),
            "csrf: configured secret (survives restarts)"
            if settings.csrf_secret
            else "YTSHORT_CSRF_SECRET unset -- forms break across restarts",
            warn_only=True,
        )

    if settings.job_trigger_enabled:
        report(
            bool(settings.azure_subscription_id and settings.azure_job_name),
            f"job trigger: {settings.azure_job_name} in {settings.azure_resource_group}",
        )

    tracks = AudioSource(settings.audio_dir).tracks()
    report(
        bool(tracks),
        f"audio: {len(tracks)} licensed track(s) in {settings.audio_dir}"
        if tracks
        else f"no audio track in {settings.audio_dir} -- drop in an .mp3 you may use",
    )

    # The audit submission tells Google the manifest is authoritative; a track
    # missing from it is a copyright claim you cannot dispute.
    for problem in settings.audio_licence_problems():
        report(False, problem, warn_only=True)

    if settings.malware_scanner == "defender":
        scanner = DefenderScanner()
        report(
            scanner.available,
            "malware scanner: Windows Defender available"
            if scanner.available
            else "MpCmdRun.exe not found; attachments will be flagged as unscanned",
            warn_only=True,
        )
    else:
        report(True, f"malware scanner: {settings.malware_scanner}", warn_only=True)

    if "email" in settings.sinks:
        report(
            bool(settings.email_recipients),
            f"email sink: {len(settings.email_recipients)} recipient(s)"
            if settings.email_recipients
            else "email sink is enabled but YTSHORT_EMAIL_RECIPIENTS is empty",
        )

    writable = _is_writable(settings.data_dir)
    report(writable, f"data dir: {settings.data_dir}" if writable else
           f"data dir is not writable: {settings.data_dir}")

    report(
        shutil.which("tesseract") is not None,
        "OCR: tesseract found -- images will be screened for visible PII"
        if shutil.which("tesseract")
        else "tesseract not found -- image PII screening will be skipped and flagged",
        warn_only=True,
    )

    # Never prints the connection string, only whether one is wired.
    report(
        True,
        f"telemetry: exporting to Application Insights as "
        f"'{settings.service_name}' ({settings.environment_name})"
        if current().azure_enabled
        else "telemetry: not configured -- structured JSON to stdout only",
        warn_only=True,
    )
    for problem in [p for p in settings.validation_problems() if "observability" in p]:
        report(False, problem, warn_only=True)

    if settings.privacy_status != "private":
        typer.secho(
            f"{_WARN}privacy: requesting '{settings.privacy_status}'. YouTube locks uploads "
            "from API projects that have not passed its compliance audit to private "
            "regardless.",
            fg=typer.colors.YELLOW,
        )

    typer.echo()
    if problems:
        typer.secho(f"{problems} problem(s) must be fixed before a run.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho("Ready.", fg=typer.colors.GREEN)


@auth_app.command("login")
def auth_login(
    force: bool = typer.Option(
        False, "--force", help="Discard the cached token and re-consent from scratch."
    ),
) -> None:
    """Authorise Gmail + YouTube once; the token is cached outside the repo."""
    from ytshort.integrations.google_auth import AuthError, load_credentials, run_consent_flow

    settings = _settings(strict=False)
    token_path = Path(settings.google_token_file)

    if force and token_path.exists():
        token_path.unlink()
        typer.echo("Cached token discarded.")

    try:
        if force:
            run_consent_flow(settings)
        else:
            load_credentials(settings, allow_interactive=True)
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(f"Authorised. Token cached at {token_path}", fg=typer.colors.GREEN)


@auth_app.command("export")
def auth_export(
    show: bool = typer.Option(
        False, "--show", help="Print the secret values instead of just the az commands."
    ),
) -> None:
    """Print the `az keyvault secret set` commands for the cached credential.

    Only the refresh token, client id, and client secret go to Azure -- never the
    token file itself. Values are withheld unless --show is passed, so this is
    safe to run with someone watching.
    """
    from ytshort.integrations.credential_store import CredentialError, extract_for_key_vault

    settings = _settings(strict=False)
    try:
        values = extract_for_key_vault(settings.google_token_file)
    except (OSError, CredentialError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    names = {
        "refresh_token": settings.kv_secret_refresh_token,
        "client_id": settings.kv_secret_client_id,
        "client_secret": settings.kv_secret_client_secret,
    }
    typer.echo("# Run these against your vault, then delete the local token file if you like.")
    for field, secret_name in names.items():
        value = values[field] if show else "<from ytshort auth export --show>"
        typer.echo(f'az keyvault secret set --vault-name <kv> --name {secret_name} \\')
        typer.echo(f'  --value "{value}"')
    if not show:
        typer.secho(
            "\nValues withheld. Re-run with --show to print them.", fg=typer.colors.YELLOW
        )


@auth_app.command("status")
def auth_status() -> None:
    """Report credential health without triggering a browser flow."""
    from ytshort.integrations.google_auth import describe_credentials

    settings = _settings(strict=False)
    result = describe_credentials(settings)
    colour = typer.colors.GREEN if result.ok else typer.colors.RED
    typer.secho(result.detail, fg=colour)
    if not result.ok:
        raise typer.Exit(code=1)


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _print_outcome(outcome) -> None:
    job = outcome.job
    label = job.title or job.source.subject or job.job_id[:12]
    ran = ", ".join(outcome.stages_run) or "nothing"
    typer.echo(f"{job.job_id[:12]}  {job.state.value:<16} {label[:48]}")
    typer.echo(f"              ran: {ran}")
    if outcome.stopped_by:
        typer.echo(f"              stopped at {outcome.stopped_by}: {outcome.reason}")
    if job.state is JobState.awaiting_review:
        typer.secho(
            "              -> run 'ytshort review --serve' to approve",
            fg=typer.colors.YELLOW,
        )
    if job.publication:
        typer.secho(f"              -> {job.publication.short_url}", fg=typer.colors.GREEN)


def main() -> None:
    try:
        # A run-level id so `ytshort status`, `doctor` and `prune` are correlated
        # too. The runner's per-job binding nests inside this and restores on exit,
        # so a run reads as: one invocation id, one id per job it touched.
        with use_correlation(new_correlation_id()):
            app()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        typer.echo("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
