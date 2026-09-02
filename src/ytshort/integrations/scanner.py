"""Malware scanning behind a swappable provider.

The default provider shells out to Windows Defender's ``MpCmdRun.exe``, which is
already present and updated on the target machine -- no extra service to run.
ClamAV, VirusTotal, or Azure Defender drop in behind the same three-line
protocol.

Failure posture: if the scanner cannot run, the job is **not** silently passed as
clean. It gets a ``warn`` finding saying screening did not happen, which surfaces
in the review UI so the human knows what they are approving. That is the right
trade-off for a personal pipeline with a human gate; a system publishing without
review should flip this to blocking.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ytshort.observability.logging import get_logger

log = get_logger(__name__)

# Defender is slow to start; a small file still costs a few seconds.
_SCAN_TIMEOUT_SECONDS = 120


@dataclass
class ScanResult:
    clean: bool
    provider: str
    detail: str = ""
    skipped: bool = False


class ScanProvider(Protocol):
    name: str

    def scan(self, path: Path) -> ScanResult: ...


class NoopScanner:
    """Explicitly does nothing. Chosen via YTSHORT_MALWARE_SCANNER=none."""

    name = "none"

    def scan(self, path: Path) -> ScanResult:
        return ScanResult(
            clean=True,
            provider=self.name,
            detail="malware scanning disabled by configuration",
            skipped=True,
        )


def _find_mpcmdrun() -> Path | None:
    """Locate MpCmdRun.exe, preferring the newest platform-versioned copy.

    Defender updates itself into
    ``ProgramData\\Microsoft\\Windows Defender\\Platform\\<version>\\`` and the
    copy under Program Files can be an older stub, so check Platform first and
    take the highest version directory.
    """
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    platform_root = program_data / "Microsoft" / "Windows Defender" / "Platform"
    if platform_root.is_dir():
        candidates = sorted(
            (p for p in platform_root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        for candidate in candidates:
            exe = candidate / "MpCmdRun.exe"
            if exe.exists():
                return exe

    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    fallback = program_files / "Windows Defender" / "MpCmdRun.exe"
    return fallback if fallback.exists() else None


class DefenderScanner:
    """Windows Defender on-demand file scan."""

    name = "defender"

    def __init__(self, executable: Path | None = None) -> None:
        self._exe = executable or _find_mpcmdrun()

    @property
    def available(self) -> bool:
        return self._exe is not None

    def scan(self, path: Path) -> ScanResult:
        if self._exe is None:
            return ScanResult(
                clean=True,
                provider=self.name,
                detail="MpCmdRun.exe not found; file was not scanned",
                skipped=True,
            )

        try:
            completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
                [
                    str(self._exe),
                    "-Scan",
                    "-ScanType",
                    "3",
                    "-File",
                    str(path.resolve()),
                    # Report only. Without this Defender may quarantine or delete
                    # the file out from under us mid-pipeline.
                    "-DisableRemediation",
                ],
                capture_output=True,
                text=True,
                timeout=_SCAN_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ScanResult(
                clean=True,
                provider=self.name,
                detail=f"scan timed out after {_SCAN_TIMEOUT_SECONDS}s; file was not screened",
                skipped=True,
            )
        except OSError as exc:
            return ScanResult(
                clean=True,
                provider=self.name,
                detail=f"scanner could not be launched ({exc}); file was not screened",
                skipped=True,
            )

        output = f"{completed.stdout}\n{completed.stderr}".strip()

        # MpCmdRun: 0 = nothing found, 2 = threat found. Anything else is a tool
        # problem rather than a verdict, so report it as "not screened" instead
        # of pretending the file is clean.
        if completed.returncode == 0:
            return ScanResult(clean=True, provider=self.name, detail="no threats found")
        if completed.returncode == 2:
            return ScanResult(
                clean=False,
                provider=self.name,
                detail=output or "Defender reported a threat",
            )
        return ScanResult(
            clean=True,
            provider=self.name,
            detail=f"scanner exited {completed.returncode}; file was not screened: {output}",
            skipped=True,
        )


class VirusTotalScanner:
    """Reputation lookup by file hash. The right fit for a Linux container.

    Windows Defender does not exist in the deployed image, and bundling ClamAV
    means a 200 MB image plus a signature-update daemon for a pipeline that
    processes a handful of files a day. This instead looks up the SHA-256 we
    already compute at ingest.

    Only the hash is sent -- **the file itself is never uploaded**, so nothing
    leaves the system that could contain the sender's private photo. The trade is
    that a file nobody has ever submitted to VirusTotal is *unknown*, not clean,
    and this reports it as such rather than passing it.
    """

    name = "virustotal"
    _URL = "https://www.virustotal.com/api/v3/files/{sha256}"
    _TIMEOUT = 20

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def scan(self, path: Path) -> ScanResult:
        if not self._api_key:
            return ScanResult(
                clean=True,
                provider=self.name,
                detail="no VIRUSTOTAL_API_KEY set; file was not screened",
                skipped=True,
            )

        request = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            self._URL.format(sha256=self._sha256(path)),
            headers={"x-apikey": self._api_key, "Accept": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, timeout=self._TIMEOUT) as response:  # noqa: S310
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Never submitted to VirusTotal. Unknown, not clean -- and the
                # distinction is exactly what the reviewer needs to see.
                return ScanResult(
                    clean=True,
                    provider=self.name,
                    detail="hash unknown to VirusTotal; the file was not screened",
                    skipped=True,
                )
            return ScanResult(
                clean=True,
                provider=self.name,
                detail=f"VirusTotal returned {exc.code}; file was not screened",
                skipped=True,
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return ScanResult(
                clean=True,
                provider=self.name,
                detail=f"VirusTotal unreachable ({exc}); file was not screened",
                skipped=True,
            )

        stats = (
            data.get("data", {}).get("attributes", {}).get("last_analysis_stats", {}) or {}
        )
        malicious = int(stats.get("malicious", 0))
        suspicious = int(stats.get("suspicious", 0))

        if malicious or suspicious:
            return ScanResult(
                clean=False,
                provider=self.name,
                detail=(
                    f"{malicious} engines flagged this file as malicious, "
                    f"{suspicious} as suspicious"
                ),
            )
        return ScanResult(
            clean=True,
            provider=self.name,
            detail=f"no detections across {sum(int(v) for v in stats.values())} engines",
        )


def build_scanner(provider: str, api_key: str = "") -> ScanProvider:
    if provider == "defender":
        return DefenderScanner()
    if provider == "virustotal":
        return VirusTotalScanner(api_key)
    if provider in ("none", ""):
        return NoopScanner()
    log.warning("unknown malware scanner, falling back to none", extra={"provider": provider})
    return NoopScanner()
