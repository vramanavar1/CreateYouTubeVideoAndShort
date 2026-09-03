---
layout: default
title: ytshort
---

# ytshort

**ytshort** is a personal, single-operator tool. It watches one Gmail mailbox for
photos and videos sent from an approved address, screens them, renders a vertical
YouTube Short, and waits for a human to approve it before anything is published.

It is not a service. It has no users other than the person who runs it, no
sign-up, and no way for anyone else to submit content to it.

## What it does, in order

1. Reads new mail from an **allow-listed sender** — nothing else is looked at.
2. Screens each attachment: file-type checks, image sanity limits, EXIF and GPS
   stripping, malware scanning, and detection of personal information.
3. Builds a thumbnail from the sender's own image and the email subject.
4. Renders a vertical Short with a licensed background track.
5. **Stops, and waits for a person to approve or reject it.**
6. Publishes to the operator's own YouTube channel and sends a notification.

Nothing is uploaded without step 5. That gate is the point of the design.

## Source

The complete source, including the security notes and the design decisions behind
it, is at
[github.com/vramanavar1/CreateYouTubeVideoAndShort](https://github.com/vramanavar1/CreateYouTubeVideoAndShort).

## Policies

- [Privacy policy](privacy/)
- [Terms of service](terms/)

## Contact

Questions about this application go to the operator via the email address listed
on the Google consent screen when you authorise it, or by opening an issue on the
repository above.
