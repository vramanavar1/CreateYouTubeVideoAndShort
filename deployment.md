# Deployment

The complete runbook, starting from **no Google account and no Azure resources**.
Twelve phases, forty-one numbered steps. Every step says what it produces and how
to confirm it worked, so you should never have to improvise.

You do **not** need Docker locally — `az acr build` builds server-side.

> **Shell.** Phase 3 is PowerShell (it installs Windows prerequisites). **From
> phase 4 onward every command is bash** — `export`, `$(…)`, `jq`, `openssl`,
> `/dev/null`. On Windows, switch to WSL, Git Bash, or Azure Cloud Shell before
> phase 4 and stay there.

> **Read phase 0 before buying or configuring anything.** The choice of Google
> account there is the single biggest security decision in the whole deployment,
> and it is painful to change later.

**Contents**

| Phase | What | Time |
|---|---|---|
| [0](#phase-0--the-google-account) | The Google account and channel | 20 min |
| [1](#phase-1--google-cloud-project) | Google Cloud project, APIs, OAuth client | 20 min |
| [2](#phase-2--submit-the-compliance-audit) | Submit the compliance audit | 15 min, then wait |
| [3](#phase-3--local-install-and-the-one-time-consent) | Local install, consent, prove it works | 30 min |
| [4](#phase-4--azure-foundation) | Azure foundation | 15 min |
| [5](#phase-5--secrets-and-assets-into-place) | Secrets into Key Vault, background track onto the share | 10 min |
| [6](#phase-6--build-the-image) | Build the image | 10 min |
| [7](#phase-7--deploy-the-workloads-ingress-internal) | Deploy workloads (internal) | 10 min |
| [8](#phase-8--entra-app-registration-and-easyauth) | Entra app registration + EasyAuth | 20 min |
| [9](#phase-9--go-external) | Go external | 5 min |
| [10](#phase-10--smoke-test-and-security-verification) | Smoke test and security verification | 30 min |
| [11](#phase-11--day-2-operations) | Day-2 operations | reference |

---

## Phase 0 — The Google account

This account will hold a credential that can **read your mail and send as you**,
stored in the cloud. Treat that as the design constraint it is.

1. **Create a dedicated Google account.** Not your personal one. The pipeline
   only needs a mailbox that receives photos, so the blast radius of a leaked
   credential should be "a purpose-built mailbox", not "my entire identity".

2. **Enable 2-Step Verification**, then add a **passkey or hardware security
   key**. Do not leave SMS as the only second factor — SIM-swap is a real attack
   on accounts that own a YouTube channel.

3. **Set recovery options that do not chain back to your primary identity.** A
   recovery email pointing at your main account re-links the two.

4. **Create the YouTube channel** on this account.

5. **Phone-verify the channel** at <https://youtube.com/verify>.
   Without this, `thumbnails.set` returns 403 and every publish logs a
   `publish.thumbnail_rejected` warning.

> ✅ **Verify:** sign in at <https://myaccount.google.com/security>. 2FA on, a
> passkey listed, and no recovery address belonging to your main identity.

---

## Phase 1 — Google Cloud project

6. Go to <https://console.cloud.google.com> and create a project named
   `ytshort`, signed in **as the dedicated account**.

7. Enable exactly two APIs — no more:

   ```bash
   gcloud config set project ytshort
   gcloud services enable gmail.googleapis.com youtube.googleapis.com
   ```

   Or via the console: *APIs & Services → Enable APIs* → "Gmail API" and
   "YouTube Data API v3".

8. **OAuth consent screen** — *APIs & Services → OAuth consent screen*,
   User type **External**. Add exactly these four scopes:

   | Scope | Why |
   |---|---|
   | `.../auth/gmail.readonly` | Read candidate mail and download attachments |
   | `.../auth/gmail.send` | Send the "published" notification |
   | `.../auth/youtube.upload` | `videos.insert`, `thumbnails.set` |
   | `.../auth/youtube.force-ssl` | `videos.update`, to promote private uploads after the audit |

   `gmail.modify` is deliberately **not** requested — it grants write over the
   whole mailbox, and the pipeline does not need it.

   Grant `youtube.force-ssl` **now**, even though it is only used after the audit
   clears. Adding a scope later means re-consenting and re-rotating the stored
   credential.

9. **Publish the app** — set the publishing status to *In production*.

   It will remain **unverified**, so you will see a "Google hasn't verified this
   app" interstitial when you consent. That is expected and fine for the owner
   account: click *Advanced → Go to ytshort (unsafe)*.

   > This step is not cosmetic. While the consent screen is in **Testing**,
   > Google expires refresh tokens after **7 days**. The hourly job would work
   > for a week and then start failing with an opaque `invalid_grant`, at
   > 3am, silently. Publishing is what prevents that.

10. **Credentials → Create credentials → OAuth client ID → Desktop app.**
    Download the JSON and save it **outside this repository**, e.g.
    `C:\secrets\ytshort\client_secret.json`.

> ✅ **Verify:** *APIs & Services → Enabled APIs* lists exactly Gmail API and
> YouTube Data API v3. The consent screen says *In production*.

---

## Phase 2 — Submit the compliance audit

**Do this now, not at the end.** It is the only unbounded wait in the project.

11. Submit the [YouTube API Services compliance audit](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits).
    [`docs/youtube-audit.md`](docs/youtube-audit.md) is written to be pasted into
    that form — it covers every API method the app calls, the quota profile, the
    human approval gate, content provenance, and data handling.

Until the audit clears, **every upload is force-locked to private** regardless of
what `privacyStatus` requests. That is a YouTube policy, not a bug in this
pipeline, and nothing in the configuration changes it. Everything below works in
the meantime; you simply get private videos, and
[phase 11](#phase-11--day-2-operations) promotes them once the audit passes.

---

## Phase 3 — Local install and the one-time consent

The browser consent flow cannot run in a container, so the credential is minted
here, once. **Prove the whole pipeline works locally before containerising it** —
debugging a filter graph through a container log is miserable.

12. Install prerequisites and configure:

    ```powershell
    winget install Gyan.FFmpeg
    uv sync
    Copy-Item .env.example .env
    ```

    Edit `.env` and set at minimum:

    ```ini
    YTSHORT_GOOGLE_CLIENT_SECRET_FILE=C:/secrets/ytshort/client_secret.json
    YTSHORT_GOOGLE_TOKEN_FILE=C:/secrets/ytshort/token.json
    YTSHORT_ALLOWED_SENDERS=you@example.com
    YTSHORT_EMAIL_RECIPIENTS=you@example.com
    ```

    `YTSHORT_ALLOWED_SENDERS` is **mandatory** — the app refuses to start without
    it. An empty allow-list would let anyone who emails the watched mailbox queue
    media for publication.

13. Drop a licensed track into `assets/audio/` and record it in
    [`assets/audio/AUDIO_LICENSES.md`](assets/audio/AUDIO_LICENSES.md).
    [Mixkit](https://mixkit.co/free-stock-music/) needs no account and no
    attribution; the YouTube Audio Library (YouTube Studio → Audio library) needs
    a sign-in but is the only source where Google guarantees its tracks are not
    Content ID claimed.

    The same file goes onto the Azure share in step 23 — the image never carries
    it, because `assets/audio/` is gitignored and dockerignored.

14. ```bash
    uv run ytshort doctor
    ```
    Must be all green before continuing.

15. ```bash
    uv run ytshort auth login
    ```
    A browser opens. Accept the unverified-app warning, grant all four scopes.

16. **Prove it end to end.** Mail the watched account a photo with a real subject
    line, then:

    ```bash
    uv run ytshort run            # expect: parks at awaiting_review
    uv run ytshort review --serve # open http://127.0.0.1:8080/reviews, approve
    ```

    Confirm a private video appears in YouTube Studio and the notification email
    arrives. Then re-run `uv run ytshort run` and confirm it is a **no-op** —
    that is the idempotency guarantee working.

17. Extract the three values that go to Key Vault:

    ```bash
    uv run ytshort auth export --show
    ```

    This prints ready-made `az keyvault secret set` commands. Only the refresh
    token, client id, and client secret leave your machine — **the token file
    itself never goes to Azure**, because the short-lived access token in it
    would be stale within the hour and is not what the vault is for.

> ✅ **Verify:** a private video in YouTube Studio, an email in your inbox, and a
> second `ytshort run` that does nothing.

---

## Phase 4 — Azure foundation

18. Sign in and prepare:

    ```bash
    az login
    az account set --subscription <subscription-id>
    az extension add --name containerapp --upgrade
    az provider register --namespace Microsoft.App --wait
    az provider register --namespace Microsoft.OperationalInsights --wait
    ```

19. Create the resource group and export the values the parameter file reads:

    ```bash
    export RG=rg-ytshort-dev
    export LOCATION=westeurope
    export OPERATOR_OBJECT_ID=$(az ad signed-in-user show --query id -o tsv)

    az group create -n $RG -l $LOCATION
    ```

20. Preview, then deploy:

    ```bash
    az deployment group what-if -g $RG \
      -f infra/foundation.bicep \
      -p infra/environments/foundation.dev.bicepparam

    az deployment group create -g $RG -n foundation \
      -f infra/foundation.bicep \
      -p infra/environments/foundation.dev.bicepparam
    ```

    This creates the registry, Key Vault, storage account and file share, Log
    Analytics, Application Insights, **two** managed identities, and the
    Container Apps environment.

    > The two identities are the point. The Job identity can read Key Vault; the
    > review identity cannot. Everything else in this deployment follows from
    > that split.

    Creating role assignments requires **User Access Administrator** or **Owner**
    on the resource group. A plain Contributor deployment fails here.

21. Capture the outputs — every later phase reads them from the environment.

    **These exports are load-bearing and invisible.** The `.bicepparam` files pull
    them with `readEnvironmentVariable()`, so `apps.bicep` never names them on the
    command line. In a new shell they are gone and the deployment fails with
    `BCP427` before it touches Azure. Phases 7, 9 and 11 all re-run that
    deployment — re-run this block first each time.

    ```bash
    OUT=$(az deployment group show -g $RG -n foundation --query properties.outputs -o json)
    export ACR_NAME=$(echo $OUT | jq -r .registryName.value)
    export ACR_LOGIN_SERVER=$(echo $OUT | jq -r .registryLoginServer.value)
    export KEY_VAULT_NAME=$(echo $OUT | jq -r .keyVaultName.value)
    export ACA_ENV_NAME=$(echo $OUT | jq -r .environmentName.value)
    export ACA_STORAGE_NAME=$(echo $OUT | jq -r .environmentStorageName.value)
    export STORAGE_ACCOUNT=$(echo $OUT | jq -r .storageAccountName.value)
    export FILE_SHARE=$(echo $OUT | jq -r .fileShareName.value)
    export JOB_IDENTITY_ID=$(echo $OUT | jq -r .jobIdentityResourceId.value)
    export JOB_IDENTITY_CLIENT_ID=$(echo $OUT | jq -r .jobIdentityClientId.value)
    export REVIEW_IDENTITY_ID=$(echo $OUT | jq -r .reviewIdentityResourceId.value)
    export REVIEW_IDENTITY_CLIENT_ID=$(echo $OUT | jq -r .reviewIdentityClientId.value)
    export REVIEW_IDENTITY_PRINCIPAL_ID=$(echo $OUT | jq -r .reviewIdentityPrincipalId.value)
    ```

> ✅ **Verify:** `az deployment group show -g $RG -n foundation --query properties.outputs`
> returns identifiers only — **no keys, no connection strings**. That is
> deliberate: deployment history is plain text and readable by anyone with Reader.

---

## Phase 5 — Secrets and assets into place

22. Set the three Google secrets (from step 17) plus a CSRF signing secret:

    ```bash
    az keyvault secret set --vault-name $KEY_VAULT_NAME -n google-refresh-token --value "<refresh_token>"
    az keyvault secret set --vault-name $KEY_VAULT_NAME -n google-client-id     --value "<client_id>"
    az keyvault secret set --vault-name $KEY_VAULT_NAME -n google-client-secret --value "<client_secret>"
    az keyvault secret set --vault-name $KEY_VAULT_NAME -n csrf-secret          --value "$(openssl rand -base64 32)"
    ```

    **Optional but recommended — malware scanning.** The deployed container is
    Linux, so Windows Defender is unavailable. The pipeline instead looks the
    file's SHA-256 up on VirusTotal; only the hash is sent, never the file. Get a
    free API key at <https://www.virustotal.com/gui/join-us> and:

    ```bash
    az keyvault secret set --vault-name $KEY_VAULT_NAME -n virustotal-api-key --value "<key>"
    ```

    Then set `virusTotalSecretConfigured = true` in the parameter file. Skip this
    and every job carries a visible `malware.not_scanned` warning into review —
    which is honest, but it means you are the scanner.

    `csrf-secret` **must exist before phase 7** — `apps.bicep` grants the review
    identity read access scoped to that one secret, which requires the secret to
    be there.

    > **The trailing-newline trap.** `--value "$(cat file)"` or a copy-paste from
    > a terminal can append `\n`, and Google rejects the refresh token with a
    > useless error. The Key Vault credential store strips whitespace defensively,
    > but check anyway.

    **Not needed: the App Insights connection string.** The foundation deployment
    already wrote `appinsights-connection-string` into the vault, reading it with a
    deploy-time `reference()` so it never lands in deployment history. Both
    workloads read it through their own identities — the review app via a role
    assignment scoped to that one secret, so it still cannot reach the Google
    credential in the same vault.

> ✅ **Verify:**
> ```bash
> az keyvault secret list --vault-name $KEY_VAULT_NAME --query "[].name" -o tsv
> ```
> At least these five: the four you just set plus `appinsights-connection-string`,
> which the foundation deployment created for you. Add `virustotal-api-key` if you
> took the optional step. Phase 8 adds a sixth, `easyauth-client-secret`, so this
> list grows later — that is expected.

23. **Put the background track on the share.** `assets/audio/` is gitignored
    **and** dockerignored — music is not ours to redistribute — so the directory
    inside the image is always empty. In Azure the app reads
    `/data/assets/audio` on the mounted file share instead
    (`YTSHORT_AUDIO_DIR`, set in `apps.bicep`).

    ```bash
    export STORAGE_KEY=$(az storage account keys list -n $STORAGE_ACCOUNT -g $RG --query "[0].value" -o tsv)

    az storage directory create --account-name $STORAGE_ACCOUNT --account-key $STORAGE_KEY \
      --share-name $FILE_SHARE --name assets
    az storage directory create --account-name $STORAGE_ACCOUNT --account-key $STORAGE_KEY \
      --share-name $FILE_SHARE --name assets/audio

    az storage file upload --account-name $STORAGE_ACCOUNT --account-key $STORAGE_KEY \
      --share-name $FILE_SHARE --source ./assets/audio/SereneView-ByArulo.mp3 \
      --path assets/audio/SereneView-ByArulo.mp3
    az storage file upload --account-name $STORAGE_ACCOUNT --account-key $STORAGE_KEY \
      --share-name $FILE_SHARE --source ./assets/audio/AUDIO_LICENSES.md \
      --path assets/audio/AUDIO_LICENSES.md
    ```

    The licence manifest goes up too, not just the track: `ytshort doctor` reads it
    from beside the audio, and `docs/youtube-audit.md` tells Google that manifest
    is authoritative for audio licensing. A track on the share with no manifest
    beside it is a claim you cannot dispute.

    **Skip this step and nothing ever publishes.** Every `compose` fails with "No
    licensed audio track found", the job retries with backoff, and after
    `YTSHORT_MAX_STAGE_ATTEMPTS` it dead-letters to `failed` — silently, on every
    scheduled run.

> ✅ **Verify:**
> ```bash
> az storage file list --account-name $STORAGE_ACCOUNT --account-key $STORAGE_KEY \
>   --share-name $FILE_SHARE --path assets/audio -o table
> ```
> Both the `.mp3` and `AUDIO_LICENSES.md` are listed.

---

## Phase 6 — Build the image

24. Resolve the base-image digests, then build:

    ```bash
    export IMAGE_TAG=$(date +%Y%m%d%H%M%S)

    # Resolve once, pin thereafter. A floating tag means the image you scanned is
    # not necessarily the image you shipped.
    docker buildx imagetools inspect python:3.12-slim-bookworm --format '{{.Manifest.Digest}}'

    az acr build -r $ACR_NAME -t ytshort:$IMAGE_TAG -t ytshort:latest \
      --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<digest> \
      --build-arg UV_VERSION=0.9.18 .
    ```

    The build runs in Azure, so no local Docker daemon is needed for the build
    itself — only for `imagetools inspect`, and you can read the digest from the
    registry UI instead. ffmpeg is installed from Debian inside the image.

    Both `--build-arg` values have defaults in the `Dockerfile`, so omitting them
    still builds; passing them is what makes the build reproducible.
    **Keep `UV_VERSION` in step with the `uv` that wrote `uv.lock`** — an older
    `uv` cannot read a lockfile written by a newer one, and the failure surfaces
    as a confusing resolution error inside the build rather than at `uv lock`
    time.

> ✅ **Verify:** `az acr repository show-tags -n $ACR_NAME --repository ytshort -o tsv`

---

## Phase 7 — Deploy the workloads, ingress **internal**

25. ```bash
    export ALLOWED_SENDERS=you@example.com
    export EMAIL_RECIPIENTS=you@example.com

    az deployment group create -g $RG -n apps \
      -f infra/apps.bicep \
      -p infra/environments/apps.dev.bicepparam
    ```

    **Both exports are mandatory.** Neither has a fallback in the parameter file,
    so a missing one fails the build with `BCP427` before anything reaches Azure.
    That is deliberate: the `email` sink is on by default and the app treats "email
    sink enabled, no recipients" as fatal, so an empty fallback would produce a
    *successful* deployment and three crash-looping containers.

    **Optional — AI thumbnail hooks.** A model can write a short hook for the
    thumbnail instead of using the raw email subject. It needs **no secret**: the
    ingest Job authenticates to your Foundry deployment with the same managed
    identity it already uses for Key Vault. Set `foundryResourceGroup` and
    `foundryAccountName` in `apps.dev.bicepparam` and this deployment grants that
    identity `Cognitive Services OpenAI User`, scoped to the account alone.

    Leave them empty for the first deployment. Get the pipeline working end to end
    first — the role takes **up to five minutes** to propagate, and a `403` during
    that window is easy to mistake for a deployment problem. (It is not fatal: the
    job records a `thumbnail.hooks_not_generated` warning and renders with the
    subject.)

    `ingressExternal` is `false` in the parameter file. **Leave it that way for
    now.** The next phase explains why.

    Like phase 4, this needs **Owner** or **User Access Administrator** — it
    creates a custom role definition and three role assignments. A plain
    Contributor gets through the foundation deployment and fails here.

    This creates the hourly ingest Job, the nightly prune Job, the review App, and
    three least-privilege grants for the review identity: read on `csrf-secret`,
    read on `appinsights-connection-string`, and a custom role whose entire
    permission set is `Microsoft.App/jobs/read` + `Microsoft.App/jobs/start/action`
    on the one Job. Each Key Vault grant is scoped to a **single secret**, never
    the vault.

> ✅ **Verify:** `az containerapp job list -g $RG -o table` shows two jobs, and
> `az containerapp show -n ca-ytshort-dev-review -g $RG --query properties.configuration.ingress.external`
> returns `false`.

---

## Phase 8 — Entra app registration and EasyAuth

**The ordering problem:** the Entra redirect URI must contain the app's hostname,
but the hostname only exists after the app is deployed. If you deploy with
external ingress first and configure auth second, there is a window — minutes,
maybe hours if you get interrupted — where **an unauthenticated public endpoint
can approve YouTube uploads**. Phases 7 and 9 exist to close that window.

26. Get the FQDN the app *will* use:

    ```bash
    export REVIEW_APP=ca-ytshort-dev-review
    export FQDN=$(az containerapp show -n $REVIEW_APP -g $RG \
      --query properties.configuration.ingress.fqdn -o tsv)
    echo $FQDN
    ```

27. Register the Entra application:

    ```bash
    export APP_ID=$(az ad app create \
      --display-name "ytshort review (dev)" \
      --sign-in-audience AzureADMyOrg \
      --web-redirect-uris "https://$FQDN/.auth/login/aad/callback" \
      --query appId -o tsv)

    export APP_SECRET=$(az ad app credential reset --id $APP_ID \
      --display-name easyauth --query password -o tsv)
    ```

    `--sign-in-audience AzureADMyOrg` is **single tenant**. The multi-tenant
    default would let any Entra account anywhere sign in.

28. Store the provider secret and turn auth on:

    ```bash
    az keyvault secret set --vault-name $KEY_VAULT_NAME \
      -n easyauth-client-secret --value "$APP_SECRET"

    az containerapp secret set -n $REVIEW_APP -g $RG \
      --secrets easyauth-client-secret=$APP_SECRET

    az containerapp auth microsoft update -n $REVIEW_APP -g $RG \
      --client-id $APP_ID \
      --client-secret-name easyauth-client-secret \
      --tenant-id $(az account show --query tenantId -o tsv) \
      --yes

    az containerapp auth update -n $REVIEW_APP -g $RG \
      --enabled true \
      --action RedirectToLoginPage \
      --redirect-provider azureactivedirectory \
      --exclude-paths "/health"
    ```

    `--exclude-paths "/health"` lets the platform readiness probe through. This
    is only safe because `/health` returns `{"status":"ok"}` and nothing else —
    the diagnostics live at `/health/detail`, behind auth.

    > **Verify the flag name** against your `az containerapp` extension version:
    > `az containerapp auth update --help`. If `--exclude-paths` is unavailable,
    > set `identityProviders.excludedPaths` via
    > `az containerapp auth show/update --set`, or temporarily set the probe to a
    > TCP probe instead. Do not solve it by disabling auth.

29. **Restrict who may sign in.** In the portal, *Entra ID → Enterprise
    applications → ytshort review (dev) → Properties*, set **Assignment
    required = Yes**, then under *Users and groups* assign only yourself.
    Without this, every account in your tenant can reach the approval UI.

> ✅ **Verify:** `az containerapp auth show -n $REVIEW_APP -g $RG` shows
> `"enabled": true` and the single-tenant issuer.

---

## Phase 9 — Go external

30. Now, and only now, expose the app. Edit
    `infra/environments/apps.dev.bicepparam`, set `ingressExternal = true`, and
    redeploy:

    ```bash
    # If this is a new shell, re-run step 21's export block first -- the
    # parameter file reads those values from the environment.
    az deployment group create -g $RG -n apps \
      -f infra/apps.bicep \
      -p infra/environments/apps.dev.bicepparam
    ```

31. **Repair what the redeploy just broke.** Two things change underneath you
    here, and both silently break sign-in:

    * **The hostname changes.** Internal ingress gives
      `…-review.internal.<env>.<region>.azurecontainerapps.io`; external drops the
      `.internal.` segment. The redirect URI you registered in step 27 no longer
      matches.
    * **The EasyAuth secret is deleted.** `apps.bicep` declares only
      `csrf-secret` and `appinsights-connection-string`, and an ARM deployment
      replaces the whole secret collection — so `easyauth-client-secret`, which
      you set out-of-band in step 28, is gone.

    ```bash
    export FQDN=$(az containerapp show -n $REVIEW_APP -g $RG \
      --query properties.configuration.ingress.fqdn -o tsv)
    echo $FQDN            # note: no '.internal.' now

    az ad app update --id $APP_ID \
      --web-redirect-uris "https://$FQDN/.auth/login/aad/callback"

    az containerapp secret set -n $REVIEW_APP -g $RG \
      --secrets easyauth-client-secret="$APP_SECRET"
    ```

    If `$APP_SECRET` is no longer in your shell, read it back from Key Vault:
    `az keyvault secret show --vault-name $KEY_VAULT_NAME -n easyauth-client-secret --query value -o tsv`.

    This applies to **every** later `apps.bicep` redeploy, not just this one — see
    phase 11.

> ✅ **Verify:** open `https://$FQDN/reviews` and **complete a sign-in**. Landing
> on a Microsoft sign-in page is not enough — a stale redirect URI gets you that
> far and then fails. You must end up on the review queue.

---

## Phase 10 — Smoke test and security verification

32. Force an immediate run rather than waiting for the hour:

    ```bash
    az containerapp job start -n aj-ytshort-dev-run -g $RG
    az containerapp job execution list -n aj-ytshort-dev-run -g $RG -o table
    ```

33. Watch the logs. One job's whole lifecycle shares a `correlation_id`:

    ```bash
    az containerapp job logs show -n aj-ytshort-dev-run -g $RG --container ytshort --follow
    ```

    Or in Log Analytics:

    ```kusto
    ContainerAppConsoleLogs_CL
    | where ContainerName_s == "ytshort"
    | extend p = parse_json(Log_s)
    | project TimeGenerated, p.correlation_id, p.stage, p.message
    | order by TimeGenerated desc
    ```

    The same records also reach **Application Insights** now, with `correlation_id`
    and every `extra={...}` field as `customDimensions`, plus a span per run and
    per stage and the `ytshort.*` metrics. Useful starting points:

    ```kusto
    // Everything one job did, across both tiers
    traces | where customDimensions.correlation_id == "<job_id>" | order by timestamp asc

    // Blocking findings -- these quarantine a job and are the alert-worthy ones
    traces | where severityLevel >= 3 and customDimensions.event == "finding"

    // Which approval started which job execution
    traces | where customDimensions.event == "job_trigger.requested"
    ```

    Cloud role name distinguishes the workloads: `ytshort-job` for the ingest Job
    and `ytshort-review` for the review app. The nightly prune Job has no explicit
    service name and reports as plain `ytshort` — filter on the container app name
    if you need to isolate it.

34. Mail the watched account a photo. After the next tick (or another
    `job start`), it should reach `awaiting_review`. Open the review URL, sign
    in, check the preview and the findings table, and approve. The app records
    the decision and **starts the Job** — it never uploads anything itself.

**Security checks — part of "done", not extra credit.** Every one of these
verifies a specific design decision:

35. **Unauthenticated access is refused.**
    ```bash
    curl -sS -o /dev/null -w '%{http_code}\n' https://$FQDN/reviews   # expect 302 to login
    curl -sS https://$FQDN/health                                     # expect {"status":"ok"}
    curl -sS https://$FQDN/health/detail                              # expect a login redirect
    ```

36. **`/health` leaks nothing.** The body must be exactly `{"status":"ok"}` — no
    version, no paths, no credential state.

37. **The review app holds no Google credential.**
    ```bash
    az containerapp show -n $REVIEW_APP -g $RG -o json | grep -iE "refresh_token|client_secret|google" || echo "clean"
    ```
    Expect `clean`. Then confirm its identity has no vault-wide access:
    ```bash
    az role assignment list --assignee $REVIEW_IDENTITY_PRINCIPAL_ID -o table
    ```
    Expect **four**: `AcrPull`, the custom *ytshort Job Starter* role, and **two**
    Key Vault Secrets User assignments — one scoped to `.../secrets/csrf-secret`,
    one to `.../secrets/appinsights-connection-string`.

    The count is not the point; the **scope** is. Each Key Vault grant must name a
    single secret. A `Key Vault Secrets User` assignment scoped to the *vault* is
    the failure this step exists to catch — it would let the internet-facing app
    read the Google credential sitting in the same vault.

38. **No credential on the share.**
    ```bash
    az storage file list --account-name $STORAGE_ACCOUNT --account-key $STORAGE_KEY \
      --share-name $FILE_SHARE -o table
    ```
    There must be no `token.json`. The share holds job records, rendered media and
    the background track — never a credential.

39. **No secrets in deployment history.**
    ```bash
    az deployment group show -g $RG -n foundation --query properties.outputs
    az deployment group show -g $RG -n apps       --query properties.parameters
    ```
    Nothing secret-shaped in either.

40. **The allow-list holds.** Mail the watched account from an address that is
    not in `ALLOWED_SENDERS`. Trigger a run. It must be ignored — no job created.

41. **The revocation runbook works.** Walk
    [`docs/security.md`](docs/security.md) → *Revocation* end to end, then
    re-consent and restore. A runbook nobody has executed is a guess.

---

## Phase 11 — Day-2 operations

### Ship a new image

```bash
export IMAGE_TAG=$(date +%Y%m%d%H%M%S)
az acr build -r $ACR_NAME -t ytshort:$IMAGE_TAG -t ytshort:latest \
  --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<digest> \
  --build-arg UV_VERSION=0.9.18 .
az containerapp job update -n aj-ytshort-dev-run   -g $RG --image $ACR_LOGIN_SERVER/ytshort:$IMAGE_TAG
az containerapp job update -n aj-ytshort-dev-prune -g $RG --image $ACR_LOGIN_SERVER/ytshort:$IMAGE_TAG
az containerapp update     -n $REVIEW_APP          -g $RG --image $ACR_LOGIN_SERVER/ytshort:$IMAGE_TAG
```

These are targeted `update` calls, not a template redeploy, so they leave the
secret collection alone — no auth repair needed afterwards. **A full
`az deployment group create` on `apps.bicep` is different**: it replaces the
secrets and needs step 31 run again.

### Rotate the Google credential

Re-consent locally, then update the vault. No redeploy:

```bash
uv run ytshort auth login --force
uv run ytshort auth export --show
az keyvault secret set --vault-name $KEY_VAULT_NAME -n google-refresh-token --value "<new>"
```

The next job run picks it up — the credential is read at run time, not baked
into a revision.

### Change the schedule

Edit `cronExpression` in the parameter file and redeploy `apps.bicep`.

> **Any `apps.bicep` redeploy needs two things around it.** Re-run step 21's
> export block first (the parameter file reads the environment), and re-run
> **step 31** afterwards to restore `easyauth-client-secret`. This applies to this
> section and to the audit-clearing one below.

### When the compliance audit clears

```bash
# 1. New uploads go public: set privacyStatus = 'public' in apps.dev.bicepparam,
#    then redeploy apps.bicep.
# 2. Promote the backlog of private uploads:
uv run ytshort visibility --all --to public
```

That second command needs `youtube.force-ssl`, which you granted back in phase 1
precisely so this would not require a re-consent.

### Revocation

See [`docs/security.md`](docs/security.md). Short version: revoke at
<https://myaccount.google.com/permissions>, delete the Key Vault secrets, stop
the jobs.

### Tear down

```bash
az group delete -n $RG --yes --no-wait
az keyvault purge --name $KEY_VAULT_NAME   # purge protection means it lingers otherwise
az ad app delete --id $APP_ID
```

---

## Ordering traps, collected

All five fail silently, which is why they are called out repeatedly above:

| Trap | Phases | What happens if you skip it |
|---|---|---|
| Secrets before workloads | 5 → 7 | `apps.bicep` fails: it grants access to a `csrf-secret` that does not exist |
| **Background track on the share** | 5 → 10 | Nothing ever publishes. Every `compose` fails "No licensed audio track found", retries with backoff, then dead-letters to `failed` — on every scheduled run, for as long as the deployment exists |
| Image before workloads | 6 → 7 | Containers deploy but never start — the tag is missing |
| **Internal before external** | 7 → 8 → 9 | A public, unauthenticated endpoint that can approve YouTube uploads |
| **Re-fix auth after any `apps.bicep` redeploy** | 8 → 9, and every redeploy | Sign-in breaks: the redeploy replaces the secret collection (dropping `easyauth-client-secret`) and, going external, changes the hostname the redirect URI was registered against |

---

## CI/CD

Deployment is currently driven by the `az` CLI steps above. An **Azure DevOps
pipeline is planned and will be added later** — it will wrap phases 6, 7 and 9
(validate → `az acr build` → `what-if` → deploy) using workload identity
federation instead of a service principal secret.

Phases 0–3 stay manual by nature; they are one-time human steps. Phase 4 is a
one-time bootstrap. Phase 5 stays manual too, for two reasons that will not go
away: the Google secrets are extracted from an interactive consent, and the
background track is a licensed file held locally, not in the repo. Phase 8 is a
one-time Entra registration, and phase 10 is a human smoke test.
