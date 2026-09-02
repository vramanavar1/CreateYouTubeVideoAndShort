# YouTube API Services — compliance audit submission

Draft answers for the [compliance audit](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits)
form. Adapt the bracketed values, keep the substance.

**Why you are filing this:** API projects created after 28 July 2020 that have not
passed the audit have every `videos.insert` upload **force-locked to private**.
Passing the audit is the only way to publish publicly through the API.

**File it early.** It is the longest-lead item in the whole project, and the
pipeline runs perfectly well against private uploads while you wait.

---

## 1. Application overview

**Name:** ytshort

**What it does:** A single-operator automation that turns photos and short video
clips, emailed to a dedicated mailbox by an allow-listed sender, into vertical
YouTube Shorts. Each candidate is screened for malware, file-type spoofing, and
personally identifiable information; a title card is rendered from the email
subject; the clip is composed over a licensed background track; and **a human
reviews and approves every single video before it is uploaded.**

**Users:** One — the channel owner. This is not a multi-user product, there is no
sign-up, and no third party's data is processed.

**Platform:** A containerised Python application on Azure Container Apps. An
hourly scheduled job performs ingestion and publishing; a separate,
authentication-gated web app presents the human approval queue.

**Public availability:** None. Private deployment, single operator.

---

## 2. API methods used, and why

| Method | Purpose | Frequency |
|---|---|---|
| `videos.insert` | Upload a Short **after** a human approves it | ≤10/day, typically far fewer |
| `thumbnails.set` | Apply the generated title-card thumbnail | Once per upload |
| `videos.update` | Change `privacyStatus` on videos this application uploaded | Rare — backlog promotion after this audit |
| `videos.list` | Read current `status` before an update, so unrelated properties are not overwritten | Once per update |

No other endpoints are called. In particular the application does not use
`search.list`, does not read other channels' data, does not access analytics, and
does not enumerate or modify any video it did not itself upload.

**OAuth scopes requested:**

| Scope | Justification |
|---|---|
| `youtube.upload` | `videos.insert` and `thumbnails.set` |
| `youtube.force-ssl` | `videos.update` for the privacy change; no narrower scope permits it |

Also requested, unrelated to YouTube: `gmail.readonly` and `gmail.send`, to read
the source mailbox and send the operator a notification containing the resulting
link. `gmail.modify` is explicitly **not** requested.

---

## 3. Quota profile

- **Uploads:** bounded at 10 per day. The cap is applied at ingest —
  `YTSHORT_MAX_EMAILS_PER_DAY` limits how many *emails* become jobs per UTC day,
  enforced by a persistent counter checked before any mail is fetched. Since one
  email yields at most one upload, and only after a human approves it, uploads are
  always at or below that number. Well inside the 100/day upload allocation.
- **Other calls:** a handful of `videos.list`/`videos.update` calls only when
  visibility is being changed. Nominal against the 10,000-unit daily quota.
- **Polling:** the schedule polls a mailbox, not the YouTube API. An hour with no
  new mail consumes zero YouTube quota.
- **Retries:** upload failures are retried by a bounded, idempotent stage runner.
  A job that already holds a `video_id` returns immediately without re-uploading,
  so a retry storm cannot produce duplicate uploads.

---

## 4. Human review before publication — the core control

Nothing reaches YouTube without a person approving it. This is structural, not a
policy statement:

1. The pipeline renders the video and then **suspends**, parking the job in an
   `awaiting_review` state. The publish stage is never reached.
2. A reviewer opens an authenticated web page showing the source email, the
   complete screening findings, an inline playable preview of the exact video
   that would be uploaded, and the thumbnail.
3. Only an explicit **Approve** — with an editable title and description —
   records a decision. The decision is stored on the job with the reviewer's
   authenticated identity and a timestamp.
4. **Rejection is terminal.** The job cannot be resumed into publication.
5. Any finding classified as `blocking` quarantines the job so it never appears
   as approvable at all.

The approval interface holds no YouTube credential. It records the decision and
signals the backend job, which is the only component able to call the API.

---

## 5. Content provenance and screening

Content cannot arrive from an arbitrary source:

- **Sender allow-list.** Only mail from explicitly configured addresses is
  processed. The application refuses to start with an empty allow-list, so this
  cannot be disabled by omission.
- **Media allow-list.** Only `jpg/jpeg/png/heic/webp` and `mp4/mov`, verified
  against the file's actual magic bytes rather than its extension or declared
  MIME type.
- **Size cap.** 20 MB total per email, checked before download.
- **Malware scanning** through a pluggable provider before any processing.
- **Structural guards** against decompression bombs and malformed containers.
- **PII screening** over the email subject, body, and — where OCR is available —
  text visible in the image. Detections are checksum-validated where the format
  allows (Luhn for payment cards, Verhoeff for national identifiers) to keep the
  signal meaningful. Findings are shown to the reviewer, or block outright under
  strict policy.
- **Metadata removal.** EXIF/GPS is stripped from images and all container
  metadata from video, so uploads do not carry the location or device identity of
  whoever sent the photo.
- **Optional automated content moderation** for sexual content, graphic violence,
  hate, self-harm, illegal goods, identity documents, and the presence of minors.

**Music licensing:** background audio is supplied by the operator from a local
folder of tracks they hold rights to, with the licence for each recorded in a
tracked manifest. The application deliberately contains **no** capability to
download audio or video from YouTube or any other platform.

---

## 6. Data handling and retention

- **Data collected:** only the operator's own emails to their own dedicated
  mailbox. No end-user data, no third-party data, no analytics.
- **Where it lives:** an Azure Files share in the operator's own subscription.
  Nothing is sent to any third party other than Google.
- **Retention:** rendered media is deleted automatically after 30 days by a
  scheduled prune job. Job records — which retain the audit trail of what was
  screened and who approved it — are kept.
- **Credentials:** the OAuth refresh token is stored in Azure Key Vault, read into
  memory at run time, and never written to disk. Access is restricted by managed
  identity to the single backend workload.
- **Access:** the review interface is protected by Microsoft Entra ID, single
  tenant, with assignment restricted to the operator.
- **Deletion:** the operator can revoke access at
  `myaccount.google.com/permissions`, which immediately invalidates all stored
  tokens. A documented revocation runbook exists and has been exercised.

---

## 7. Compliance statements

- The application does **not** download, extract, or store YouTube audio or video
  content.
- It does **not** modify, delete, or interact with videos it did not upload.
- It does **not** artificially inflate any metric — no automated views, likes,
  subscriptions, or comments.
- It does **not** show YouTube content outside an official YouTube player.
- It does **not** aggregate or sell YouTube data; no data leaves the operator's
  own infrastructure.
- Every upload is attributable to a human approval recorded with an authenticated
  identity and timestamp.
- Uploads are marked `selfDeclaredMadeForKids: false`; the pipeline is not
  directed at children.

---

## 8. Demo walkthrough script

For the required demonstration. Roughly five minutes.

1. **Source.** Show an email arriving at the dedicated mailbox with a photo
   attached, sent from an allow-listed address.
2. **Ingestion.** Trigger a run. Show the log output tracing one job by
   correlation id: attachment downloaded, size and type accepted.
3. **Screening.** Show the findings table — malware scan result, metadata
   stripped, any PII detected with its value masked. Optionally show a rejection:
   send an executable renamed `.png` and show the job quarantined without ever
   reaching review.
4. **Rendering.** Show the generated thumbnail and the composed vertical video.
5. **The approval gate.** Open the review UI. Show the Entra ID sign-in. Show the
   parked job, the findings, and the inline video preview. Point out that no
   upload has occurred at this point.
6. **Approve.** Click Approve, with an edited title. Show the decision recorded
   against the reviewer's identity.
7. **Publication.** Show the video in YouTube Studio, with the title, description
   including `#Shorts`, and thumbnail as approved.
8. **Idempotency.** Trigger a second run and show it is a no-op — no duplicate
   upload.
9. **Rejection.** Park a second job and reject it. Show that it is terminal and
   nothing was uploaded.

---

## 9. Contacts

- **Developer / operator:** [name], [email]
- **API project id:** [project-id]
- **Channel:** [channel URL]
- **Source:** private repository; access available on request.
