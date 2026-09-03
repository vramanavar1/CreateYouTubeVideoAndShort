---
layout: default
title: Privacy policy
permalink: /privacy/
---

# Privacy policy

_Last updated: 3 September 2026_

**ytshort** is a personal tool run by one person for their own use. It has no
users other than its operator, no accounts, and no way for a third party to
submit data to it. This policy describes what the software actually does with
data, because that is the only honest thing a policy for a single-operator tool
can describe.

## Whose data this covers

Only the operator's own Google account — the account that installs the app and
grants it access. If you are reading this because you are being asked to authorise
ytshort, and you are not the operator, **do not authorise it**. It is not intended
for you.

## What the app accesses, and why

| Access | Scope requested | What it is used for |
|---|---|---|
| Read Gmail | `gmail.readonly` | Find messages from an approved sender address and download their photo or video attachments. **Read only — the app cannot modify, label, or delete mail.** |
| Send Gmail | `gmail.send` | Send one notification message after a video is published. |
| Upload to YouTube | `youtube.upload` | Upload the finished Short and set its thumbnail, to the operator's own channel. |
| Manage YouTube videos | `youtube.force-ssl` | Change a video's visibility later — used to make previously private uploads public. |

`gmail.modify` is deliberately **not** requested. The app has no ability to change
anything in the mailbox.

Mail from any address that is not on the operator's allow-list is skipped without
its attachments being downloaded.

## What is stored, where, and for how long

Everything is stored in the operator's own Microsoft Azure subscription. Nothing
is stored on infrastructure belonging to the author of this software.

| Data | Where | Retained |
|---|---|---|
| Downloaded attachments and rendered video | Azure Files, in the operator's subscription | Deleted 30 days after a job finishes (configurable) |
| Job records — subject line, sender address, screening findings, publication result | Azure Files, same subscription | Kept, so the same email is never processed twice |
| Application logs | Azure Log Analytics / Application Insights, same subscription | Per the workspace's retention setting |

Image metadata is stripped during screening: EXIF and GPS location data are
removed from images before anything is rendered or published.

## What is sent to third parties

Only these, and only when the relevant feature is switched on:

- **YouTube** — the finished video, its title, description and thumbnail. This is
  the purpose of the application, and it happens only after a human approves.
- **VirusTotal** (optional malware scanning) — the **SHA-256 hash** of an
  attachment, to look up whether that file is already known to be malicious. **The
  file itself is never uploaded.** A hash reveals nothing about the file's
  contents.
- **Azure AI Foundry** (optional thumbnail text) — a **downscaled copy** of an
  attached image, sent to a model deployment **inside the operator's own Azure
  tenant**, to suggest short thumbnail wording. It does not leave that tenant.

There is no analytics, no advertising, no tracking, and no sale or sharing of data
with anyone else. The application makes no other outbound calls.

## Credentials

The Google credential is stored in Azure Key Vault in the operator's subscription
and read at run time by a managed identity. It is never written to the video
storage share, never included in logs, and never leaves that subscription.

## Revoking access

The operator can revoke this application's access to their Google account at any
time at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).
Doing so immediately stops all mail reading and publishing. Stored media and job
records can be deleted by removing the Azure resource group.

## Changes

This policy lives in the project's public repository. Its history is the change
log:
[github.com/vramanavar1/CreateYouTubeVideoAndShort](https://github.com/vramanavar1/CreateYouTubeVideoAndShort).

## Contact

Via the support email shown on the Google consent screen, or by opening an issue
on the repository.
