# Security

What this system holds, who can reach it, and what to do when something goes
wrong.

The uncomfortable summary: **a credential that can read and send mail as a Google
account, and upload to a YouTube channel, lives in the cloud.** Every decision
below follows from taking that seriously.

---

## Secret inventory

Everything secret, and where it actually lives.

| Secret | Home | Who can read it | Notes |
|---|---|---|---|
| Google **refresh token** | Key Vault (`google-refresh-token`) | Job identity only | Never written to disk. See below. |
| Google client id / secret | Key Vault | Job identity only | Needed to redeem the refresh token |
| CSRF signing secret | Key Vault (`csrf-secret`) | Review identity, **scoped to this one secret** | Not a credential; makes scale-to-zero safe |
| EasyAuth provider secret | Key Vault + Container Apps secret | Platform | Created in deployment phase 8 |
| `ANTHROPIC_API_KEY` (optional) | Key Vault | Job identity only | Only when moderation is enabled |
| `VIRUSTOTAL_API_KEY` (optional) | Key Vault (`virustotal-api-key`) | Job identity only | Sent as a header, never in a URL |
| Storage account key | Read by Bicep via `listKeys()` at deploy time | Deployment principal | Never a parameter, never an output |
| Registry credentials | **None exist** | — | Managed identity with `AcrPull` |
| Azure control-plane credentials | **None exist** | — | Managed identity throughout |

Two rules that prevent the classic leaks:

- **Nothing secret is ever a Bicep `output`.** Deployment history stores outputs
  in plain text, readable by anyone with Reader on the resource group.
- **Every secret parameter is `@secure()`.** Non-secure parameter values are
  recorded in deployment history too.

Verify both with deployment.md step 37.

---

## Why the refresh token is not a file

The obvious design puts `token.json` on the shared file system. It is wrong.

Only the **refresh token** is long-lived; the access token expires in an hour. So
there is no reason to persist a credential file at all: the refresh token sits in
Key Vault, `KeyVaultCredentialStore` reads it into memory, and the access token it
mints dies with the process.

Consequences, all good:

- No credential material on the file share, so a share compromise is not a Google
  compromise.
- Rotation is `az keyvault secret set` — no redeploy, no file to clean up.
- The store is **write-never**. If Google ever rotates the refresh token the job
  fails loudly and tells you to re-consent, rather than silently needing Key Vault
  write access.

`FileCredentialStore` still exists for local development, where the interactive
consent flow legitimately writes a token file to your own machine.

---

## The two-tier split

The single most important structural decision:

```
Job (no ingress)              Review App (public HTTPS)
├─ reads Key Vault            ├─ CANNOT read the Google credential
├─ talks to Gmail + YouTube   ├─ can read csrf-secret, and nothing else
└─ does all publishing        └─ can start the Job, and nothing else
```

The review app is the only component exposed to the internet, so it holds nothing
worth stealing. Approving does not publish in the web request: it records the
decision and asks the Job — over ARM, with a managed identity whose entire
permission set is `Microsoft.App/jobs/read` and `Microsoft.App/jobs/start/action`
on one Job — to run now instead of at the next tick.

**A compromise of the web tier costs you a job record, not your mailbox.**

This is enforced in three places, so it cannot regress quietly:

- `web/app.py` builds the app's context with `with_google=False` whenever a Job
  exists to delegate to.
- `apps.bicep` never grants the review identity vault-wide access.
- `tests/unit/test_web_security.py::TestNoGoogleCredentialInTheWebTier` fails if
  either changes.

---

## Gmail account hardening

The checklist, in priority order. Item 1 matters more than everything else
combined.

1. **Use a dedicated Google account.** Not your personal one. This converts the
   worst case from "my entire email history and identity" to "a purpose-built
   mailbox". It costs nothing.
2. **2-Step Verification with a passkey or hardware key.** No SMS-only fallback —
   SIM swap is a real attack against accounts that own a YouTube channel.
3. **Recovery options that do not chain back to your primary identity.** A
   recovery email pointing at your main account re-links the two.
4. **Least scopes.** The app requests four and no more:
   `gmail.readonly`, `gmail.send`, `youtube.upload`, `youtube.force-ssl`.
   `gmail.modify` — write over the entire mailbox — is deliberately **not**
   requested. It was only ever used to add a "processed" label, which
   `JobStore.known_message_ids()` already makes unnecessary.
5. **The sender allow-list is mandatory.** `YTSHORT_ALLOWED_SENDERS` cannot be
   empty; the app refuses to start. An empty list means anyone who emails the
   watched address can queue media for a public YouTube upload. This is the
   pipeline's front door and it is bolted by default.
6. **Publish the OAuth consent screen**, but keep it restricted to the owner.
   Publishing stops the 7-day refresh-token expiry that Testing mode imposes.
7. **Review third-party access** at <https://myaccount.google.com/permissions>
   periodically. The pipeline should be the only entry.

### Worth doing next: drop `gmail.send`

The email sink is the only reason the app can send as you. If the recipient list
is fixed, an Azure Communication Services Email sink or a webhook does the same
job with **zero** Gmail write privilege — leaving `gmail.readonly` as the only
Gmail scope. The `Sink` protocol in `sinks/base.py` makes this a drop-in.

---

## Content safety

The pipeline publishes to a public platform, so screening is a security control,
not a nicety. Full detail in the README; the properties that matter here:

- **Allow-list, never block-list**, on both senders and media types.
- **Magic-byte verification** — a `.exe` renamed `photo.png` is quarantined.
- **Malware scanning by hash reputation.** The deployed container is Linux, so
  Windows Defender is unavailable and bundling ClamAV would mean a 200 MB image
  plus a signature daemon for a handful of files a day. Instead the SHA-256
  already computed at ingest is looked up on VirusTotal — **only the hash is
  sent, never the file**, so a private photo never leaves the system. A hash
  nobody has ever submitted is reported as *not screened*, not as clean.
- **Metadata is stripped**, not ignored: EXIF/GPS from images, all container
  metadata from video. A holiday photo's GPS coordinates are a real leak.
- **Untrusted filenames are flattened** through `safe_filename()`; path traversal
  cannot escape the job directory.
- **Findings are never silently dropped.** A gate that could not run records that
  fact rather than looking like a clean pass — "not scanned" and "scanned, clean"
  are different states and the reviewer sees which is which.
- **A `blocking` finding never reaches the reviewer as approvable.** "Click
  approve on the malware" is not a decision a human should be offered.

---

## Least-privilege matrix

| Principal | Grant | Scope |
|---|---|---|
| Job identity | `AcrPull` | Registry |
| Job identity | `Key Vault Secrets User` | Vault |
| Review identity | `AcrPull` | Registry |
| Review identity | `Key Vault Secrets User` | **The `csrf-secret` secret only** |
| Review identity | *ytshort Job Starter* (custom: `jobs/read`, `jobs/start/action`) | **The ingest Job only** |
| Operator | `Key Vault Secrets Officer` | Vault |

Neither workload identity has Contributor on anything. The custom role exists
because the nearest built-in alternative, *Container Apps Contributor*, would let
the review app rewrite its own infrastructure.

---

## Network and data

- **Key Vault:** RBAC authorization (not access policies), soft delete and purge
  protection on, secret-access diagnostics to Log Analytics.
- **Storage:** no public blob access, TLS 1.2 minimum, HTTPS only. Azure Files
  SMB mounts authenticate with the account key, so `allowSharedKeyAccess` cannot
  be disabled — stated plainly rather than claimed as key-free. The compensating
  control is that **no credential is stored on the share**.
- **Review app:** public ingress, platform-enforced Entra ID authentication,
  single tenant, assignment required, one assigned user. The app's own CSRF
  double-submit token remains as defence in depth.
- **Media retention:** the nightly prune job deletes rendered media for jobs in a
  terminal state older than `YTSHORT_MEDIA_RETENTION_DAYS` (default 30). Job
  records and findings survive, so the audit trail is intact — only the bytes go.
  Without this, every photo ever emailed accumulates forever.
- **Logging:** structured JSON with a `correlation_id`. PII detector findings
  carry **masked** values only (`*****123`), never the original. Nothing
  credential-shaped is ever passed as a logging `extra`.

---

## Rotation

| Secret | How | Downtime |
|---|---|---|
| Google refresh token | `ytshort auth login --force`, `ytshort auth export --show`, `az keyvault secret set` | None — read at run time |
| CSRF secret | `az keyvault secret set`, then restart the review app | Open forms 403 once |
| EasyAuth provider secret | `az ad app credential reset`, update both the vault and the app secret | Brief sign-in failure |
| Storage account key | `az storage account keys renew`, redeploy `foundation.bicep` | Job pauses until redeploy |

---

## Revocation

**When to use this:** a leaked credential, a lost device with an active session, a
suspicious upload you did not approve, or anything you cannot immediately explain.

Fastest first — step 1 alone stops all Google access:

1. **Revoke Google access.** <https://myaccount.google.com/permissions> → the
   ytshort app → *Remove access*. Every stored refresh token dies immediately.
2. **Remove the stored credential** so nothing retries with it:
   ```bash
   az keyvault secret delete --vault-name $KEY_VAULT_NAME -n google-refresh-token
   ```
3. **Stop the schedule:**
   ```bash
   az containerapp job stop -n aj-ytshort-dev-run -g $RG
   ```
4. **Close the review UI** by setting ingress internal, or disable the Entra app:
   ```bash
   az ad app update --id $APP_ID --sign-in-audience AzureADMyOrg --web-redirect-uris
   ```
5. **Check what happened.** Any upload appears in YouTube Studio; every approval
   is recorded on the job with the reviewer's Entra principal name:
   ```bash
   uv run ytshort status <job_id>
   ```

**Recovery:** re-consent locally (`ytshort auth login --force`), set the new
secret, restart the job. Consider rotating the OAuth client entirely if the client
secret may have leaked.

> Walk this end to end once, while nothing is wrong. A runbook nobody has executed
> is a guess. It is step 39 of `deployment.md` for exactly that reason.

---

## Known limitations

Stated rather than buried:

- **The local review UI has no authentication** and binds to `127.0.0.1`. That is
  fine on a laptop and unacceptable anywhere else — which is why the deployed app
  runs behind EasyAuth and `YTSHORT_AUTH_MODE=platform` exists to make the
  distinction explicit.
- **`allowSharedKeyAccess` must stay enabled** on the storage account for Azure
  Files SMB mounting.
- **Creating the custom role and role assignments** needs User Access
  Administrator or Owner; a Contributor-only deployment fails at phase 4.
- **No private endpoints** in this configuration. For an enterprise posture, add
  VNet integration plus private endpoints for Key Vault, storage, and the
  registry.
