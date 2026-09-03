using '../apps.bicep'

// A .bicepparam file cannot be combined with inline -p overrides, so the values
// produced by the foundation deployment are read from the environment instead.
// deployment.md phase 7 shows the exports that populate them.
param environment = 'dev'
param workload = 'ytshort'

param environmentName = readEnvironmentVariable('ACA_ENV_NAME')
param environmentStorageName = readEnvironmentVariable('ACA_STORAGE_NAME')
param registryLoginServer = readEnvironmentVariable('ACR_LOGIN_SERVER')
param imageTag = readEnvironmentVariable('IMAGE_TAG')
param keyVaultName = readEnvironmentVariable('KEY_VAULT_NAME')

param jobIdentityResourceId = readEnvironmentVariable('JOB_IDENTITY_ID')
param jobIdentityClientId = readEnvironmentVariable('JOB_IDENTITY_CLIENT_ID')
param reviewIdentityResourceId = readEnvironmentVariable('REVIEW_IDENTITY_ID')
param reviewIdentityClientId = readEnvironmentVariable('REVIEW_IDENTITY_CLIENT_ID')
param reviewIdentityPrincipalId = readEnvironmentVariable('REVIEW_IDENTITY_PRINCIPAL_ID')

// The pipeline's front door. Only mail from these addresses is processed.
param allowedSenders = readEnvironmentVariable('ALLOWED_SENDERS')
// No fallback, deliberately. `sinks` defaults to 'file,email' and the app treats
// "email sink enabled, no recipients" as a fatal config error -- so an empty
// fallback here produces a *successful* deployment whose containers then
// crash-loop. Without it, forgetting the export fails the build (BCP427) before
// anything reaches Azure, which is how every other value in this file behaves.
param emailRecipients = readEnvironmentVariable('EMAIL_RECIPIENTS')

// Hourly ingest; nightly prune.
param cronExpression = '0 * * * *'
param pruneCronExpression = '30 3 * * *'

// Keep FALSE until EasyAuth is configured (deployment.md phases 8-9). Flipping
// this before then publishes an unauthenticated endpoint.
param ingressExternal = false

// YouTube force-locks uploads to private until the compliance audit clears.
// Change to 'public' only after it does, then run `ytshort visibility --all`.
param privacyStatus = 'private'

param maxEmailsPerDay = 10
param mediaRetentionDays = 30

// Hash-reputation scanning; the file itself is never uploaded. Set the flag to
// true once a virustotal-api-key secret exists in Key Vault (deployment.md
// phase 5). Leaving it false means every job carries a malware.not_scanned
// warning into review.
param malwareScanner = 'virustotal'
param virusTotalSecretConfigured = false

// Thumbnail art direction. Both must be set for it to switch on; the job then
// authenticates to Foundry with its managed identity -- there is no API key.
// Leave empty and the thumbnail uses the email subject, as it always has.
param foundryResourceGroup = ''
param foundryAccountName = ''
param foundryDeployment = 'gpt-4o-mini'

param tags = {
  CostCenter: 'Personal'
  Owner: 'vrama'
}
