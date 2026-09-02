# Goal
-   Access attached Images and/or a video from a given gmail account & Create a create a YouTube short video and publish it to youtube
after Human Approval (HITL);

# Steps
-   Include pipeline to validate the sent images for Security, PII and other factors to consider before publishing.
    Concretely, the screening layers are: magic-byte vs extension check, image structure and pixel-bomb limits,
    EXIF/GPS stripping, video container sanity, malware scanning, image moderation, and PII detection
    (email, payment card, IBAN, PAN, Aadhaar, phone, postal address). Every layer records a finding
    -- including when it could not run, so an unscanned job never looks like a clean one.
-   From the given Image(s) and video file; generate a Thumbnail with image in it as is. Use Email Subject as the
    thumbnail text. (The Description is not drawn on the thumbnail -- it seeds the YouTube description.)
-   If the email carries a video but no image, take a still from the video and use that as the image.
    It is extracted during ingest so it passes through the same security, moderation and PII screening as a sent image.
-   Prefix and sufix this Thumbnail image to the video file
-   Use a licensed background track supplied by the operator from `assets/audio/`, with its licence recorded in
    `assets/audio/AUDIO_LICENSES.md`.
    NOTE: this replaces the original ask (audio from a specific YouTube video). Downloading audio from YouTube
    breaks its Terms of Service and the track is copyrighted, so the pipeline deliberately contains no downloader.
    See docs/decisions.md (C7).
-   Finally generate short url. YouTube already mints one (`https://youtu.be/<id>`), so that is what is used --
    no third-party shortener. The `Shortener` seam exists if a branded domain is ever wanted. See docs/decisions.md (C3).
-   Create a Sink based design to publish this short url to multiple targets; e.g. email, file.
    NOTE: a WhatsApp *group* sink was originally asked for and is not implementable -- neither the WhatsApp Cloud API
    nor Twilio can post to a group. See docs/decisions.md (C8).

NOTE: Limit 10 Emails per day (counted as jobs created per UTC day) and max 20 MiB (20,971,520 bytes) across all
attachments of one email.

# Cross-Cutting Observability Layer (CCOL) to be used across all AI Projects
-   Build a re-usable observability layer using Azure Resources (Application Insights, Azure Log Analytics, Azure Monitor and others)
-   It should cover traceability based on severity levels with context details traceable using CorrelationId
-   Next Phase (Consider include Token usage, estimate cost, security etc)

NOTE: Use this CCOL in this project

## How it is built
-   The layer lives at `libs/ccol/` as a standalone package with its own version and **no required dependencies**,
    so another project can adopt it without inheriting this one.
-   Used here at: process bootstrap, every logger in the app, four correlation-binding sites (pipeline run, job
    creation, HTTP request, CLI invocation), every recorded finding, seven metric instruments, and spans per run
    and per stage.
-   Severity: the domain's `info`/`warn`/`blocking` map to INFO/WARNING/ERROR, so a blocking finding -- which
    quarantines a job -- is visible to a severity-based alert.
-   Without a connection string it degrades to structured JSON on stdout and imports no telemetry package at all.
