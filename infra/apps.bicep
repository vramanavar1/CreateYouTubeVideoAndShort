// ============================================================================
// ytshort -- workloads
//
// The scheduled Job and the review App. Deployed AFTER `az acr build`, because
// a container referencing a missing image tag fails to start.
//
// The security shape this file encodes:
//   * the Job has the Key Vault-reading identity and no ingress
//   * the App has ingress and CANNOT read the Google credential -- its only
//     vault access is a role assignment scoped to the single csrf-secret, and
//     its only write permission is "start this one Job"
//   * ingress defaults to internal, so there is no window where the app is
//     reachable before EasyAuth is configured
// ============================================================================

targetScope = 'resourceGroup'

@description('Workload name, used as the naming stem.')
param workload string = 'ytshort'

@description('Environment name.')
@allowed(['dev', 'prod'])
param environment string

@description('Azure region.')
param location string = resourceGroup().location

@description('Container Apps environment name, from the foundation deployment.')
param environmentName string

@description('Environment storage link name, from the foundation deployment.')
param environmentStorageName string

@description('ACR login server, e.g. crytshortdev123456.azurecr.io.')
param registryLoginServer string

@description('Image tag built by `az acr build`.')
param imageTag string

@description('Key Vault name, from the foundation deployment.')
param keyVaultName string

@description('Resource id of the Job managed identity.')
param jobIdentityResourceId string

@description('Client id of the Job managed identity (AZURE_CLIENT_ID for DefaultAzureCredential).')
param jobIdentityClientId string

@description('Resource id of the review app managed identity.')
param reviewIdentityResourceId string

@description('Client id of the review app managed identity.')
param reviewIdentityClientId string

@description('Principal id of the review app managed identity.')
param reviewIdentityPrincipalId string

@description('Cron expression for the ingest job. Hourly by default.')
param cronExpression string = '0 * * * *'

@description('Cron expression for the media retention job.')
param pruneCronExpression string = '30 3 * * *'

@description('Expose the review app publicly. Keep FALSE until EasyAuth is configured -- see deployment.md phases 7 and 9.')
param ingressExternal bool = false

@description('Comma-separated senders whose mail is processed. Mandatory: an empty list would let anyone queue media for publication.')
param allowedSenders string

@description('Comma-separated recipients for the notification email.')
param emailRecipients string = ''

@description('Comma-separated sinks.')
param sinks string = 'file,email'

@description('Requested upload visibility. YouTube force-locks this to private until the compliance audit clears.')
@allowed(['private', 'unlisted', 'public'])
param privacyStatus string = 'private'

@description('Gmail search query for candidate mail.')
param gmailQuery string = 'has:attachment newer_than:7d'

@description('Maximum emails ingested per day.')
param maxEmailsPerDay int = 10

@description('Days to keep rendered media for finished jobs.')
param mediaRetentionDays int = 30

@description('Malware scanner. "virustotal" looks the file hash up by reputation and never uploads the file; "none" disables scanning and every job then carries a malware.not_scanned warning.')
@allowed(['virustotal', 'none'])
param malwareScanner string = 'virustotal'

@description('Set true once a virustotal-api-key secret exists in Key Vault. The Job reads it via its managed identity.')
param virusTotalSecretConfigured bool = false

@description('Key Vault secret holding the App Insights connection string, from the foundation deployment. A secret *name* is not a secret.')
param appInsightsSecretName string = 'appinsights-connection-string'

@description('Extra tags merged over the defaults.')
param tags object = {}

// ---------------------------------------------------------------------------
// Names, tags, shared config
// ---------------------------------------------------------------------------

var namingPrefix = '${workload}-${environment}'
var names = {
  ingestJob: 'aj-${namingPrefix}-run'
  pruneJob: 'aj-${namingPrefix}-prune'
  reviewApp: 'ca-${namingPrefix}-review'
}

var defaultTags = {
  Environment: environment
  Application: workload
  ManagedBy: 'Bicep'
}
var allTags = union(defaultTags, tags)

var image = '${registryLoginServer}/${workload}:${imageTag}'
var dataMountPath = '/data'
var keyVaultUri = 'https://${keyVaultName}${az.environment().suffixes.keyvaultDns}/'

var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

// Configuration shared by every container. Deliberately contains no secrets --
// those arrive as Container Apps secrets or, for the Google credential, are read
// from Key Vault by the app itself at run time.
var commonEnv = [
  { name: 'YTSHORT_DATA_DIR', value: '${dataMountPath}/var' }
  // On the mounted share, not in the image: assets/audio is gitignored and
  // .dockerignored (music is not ours to redistribute), so the in-image directory
  // is always empty. Pointing at the image would make every compose fail with
  // "No licensed audio track found" on every scheduled run, forever. Drop a
  // licensed track here -- deployment.md covers it.
  { name: 'YTSHORT_AUDIO_DIR', value: '${dataMountPath}/assets/audio' }
  { name: 'YTSHORT_LOG_FORMAT', value: 'json' }
  { name: 'YTSHORT_LOG_TO_FILE', value: 'false' }
  { name: 'YTSHORT_ENVIRONMENT', value: environment }
  { name: 'YTSHORT_SERVICE_VERSION', value: imageTag }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-connection-string' }
  { name: 'YTSHORT_ALLOWED_SENDERS', value: allowedSenders }
  { name: 'YTSHORT_EMAIL_RECIPIENTS', value: emailRecipients }
  { name: 'YTSHORT_SINKS', value: sinks }
  { name: 'YTSHORT_GMAIL_QUERY', value: gmailQuery }
  { name: 'YTSHORT_MAX_EMAILS_PER_DAY', value: string(maxEmailsPerDay) }
  { name: 'YTSHORT_MEDIA_RETENTION_DAYS', value: string(mediaRetentionDays) }
  { name: 'YTSHORT_PRIVACY_STATUS', value: privacyStatus }
  // Windows Defender does not exist in a Linux container, so the deployed
  // scanner is a VirusTotal hash lookup -- only the SHA-256 is sent, never the
  // file. Set to 'none' only if you accept a `malware.not_scanned` warning on
  // every job.
  { name: 'YTSHORT_MALWARE_SCANNER', value: malwareScanner }
]

// The Job reads the Google credential from Key Vault via its own identity.
var jobEnv = concat(commonEnv, [
  { name: 'YTSHORT_CREDENTIAL_STORE', value: 'keyvault' }
  { name: 'YTSHORT_KEY_VAULT_URI', value: keyVaultUri }
  { name: 'AZURE_CLIENT_ID', value: jobIdentityClientId }
  // Distinct per workload so App Insights can tell the two cloud roles apart.
  { name: 'YTSHORT_SERVICE_NAME', value: 'ytshort-job' }
], virusTotalSecretConfigured ? [
  { name: 'VIRUSTOTAL_API_KEY', secretRef: 'virustotal-api-key' }
] : [])

// The App Insights secret is unconditional: the foundation deployment always
// creates it, and both workloads always export telemetry.
var telemetrySecret = [
  {
    name: 'appinsights-connection-string'
    keyVaultUrl: '${keyVaultUri}secrets/${appInsightsSecretName}'
    identity: jobIdentityResourceId
  }
]

var jobSecrets = concat(telemetrySecret, virusTotalSecretConfigured ? [
  {
    name: 'virustotal-api-key'
    keyVaultUrl: '${keyVaultUri}secrets/virustotal-api-key'
    identity: jobIdentityResourceId
  }
] : [])

// The review app gets no credential store configuration at all. It cannot read
// the vault, and it does not need to: it records decisions and starts the Job.
var reviewEnv = concat(commonEnv, [
  { name: 'YTSHORT_AUTH_MODE', value: 'platform' }
  { name: 'YTSHORT_REVIEW_HOST', value: '0.0.0.0' }
  { name: 'YTSHORT_REVIEW_PORT', value: '8080' }
  { name: 'YTSHORT_JOB_TRIGGER_ENABLED', value: 'true' }
  { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
  { name: 'AZURE_RESOURCE_GROUP', value: resourceGroup().name }
  { name: 'YTSHORT_AZURE_JOB_NAME', value: names.ingestJob }
  { name: 'AZURE_CLIENT_ID', value: reviewIdentityClientId }
  { name: 'YTSHORT_CSRF_SECRET', secretRef: 'csrf-secret' }
  { name: 'YTSHORT_SERVICE_NAME', value: 'ytshort-review' }
])

var dataVolume = [
  {
    name: 'data'
    storageType: 'AzureFile'
    storageName: environmentStorageName
  }
]
var dataVolumeMount = [
  {
    volumeName: 'data'
    mountPath: dataMountPath
  }
]

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

// ---------------------------------------------------------------------------
// Scheduled ingest job -- the only workload that touches Google
// ---------------------------------------------------------------------------

module ingestJob 'br/avm:res/app/job:0.7.2' = {
  name: 'ingest-job'
  params: {
    name: names.ingestJob
    location: location
    tags: allTags
    environmentResourceId: managedEnvironment.id
    triggerType: 'Schedule'
    scheduleTriggerConfig: {
      cronExpression: cronExpression
      // One replica, one completion: two concurrent runs would contend for the
      // per-job lock and achieve nothing.
      parallelism: 1
      replicaCompletionCount: 1
    }
    // A long render must not be killed halfway through.
    replicaTimeout: 1800
    replicaRetryLimit: 1
    managedIdentities: {
      userAssignedResourceIds: [jobIdentityResourceId]
    }
    registries: [
      {
        server: registryLoginServer
        identity: jobIdentityResourceId
      }
    ]
    secrets: jobSecrets
    volumes: dataVolume
    containers: [
      {
        name: 'ytshort'
        image: image
        command: ['ytshort']
        args: ['run']
        resources: {
          cpu: json('1.0')
          memory: '2Gi' // ffmpeg is the reason
        }
        env: jobEnv
        volumeMounts: dataVolumeMount
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Media retention job -- keeps the share from growing without bound
// ---------------------------------------------------------------------------

module pruneJob 'br/avm:res/app/job:0.7.2' = {
  name: 'prune-job'
  params: {
    name: names.pruneJob
    location: location
    tags: allTags
    environmentResourceId: managedEnvironment.id
    triggerType: 'Schedule'
    scheduleTriggerConfig: {
      cronExpression: pruneCronExpression
      parallelism: 1
      replicaCompletionCount: 1
    }
    replicaTimeout: 600
    managedIdentities: {
      userAssignedResourceIds: [jobIdentityResourceId]
    }
    registries: [
      {
        server: registryLoginServer
        identity: jobIdentityResourceId
      }
    ]
    // commonEnv carries the telemetry secretRef, so every workload that uses it
    // must declare the secret -- including this one.
    secrets: telemetrySecret
    volumes: dataVolume
    containers: [
      {
        name: 'ytshort'
        image: image
        command: ['ytshort']
        args: ['prune']
        resources: {
          cpu: json('0.25')
          memory: '0.5Gi'
        }
        env: commonEnv
        volumeMounts: dataVolumeMount
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Review app -- internet-facing, and therefore deliberately powerless
// ---------------------------------------------------------------------------

module reviewApp 'br/avm:res/app/container-app:0.23.0' = {
  name: 'review-app'
  params: {
    name: names.reviewApp
    location: location
    tags: allTags
    environmentResourceId: managedEnvironment.id
    // Defaults to internal. Flip to external only once EasyAuth is configured,
    // otherwise there is a window with an unauthenticated public endpoint.
    ingressExternal: ingressExternal
    ingressTargetPort: 8080
    ingressTransport: 'auto'
    ingressAllowInsecure: false
    // Scale to zero between reviews; one replica maximum bounds concurrent
    // writers on the shared file system.
    scaleSettings: {
      minReplicas: 0
      maxReplicas: 1
    }
    managedIdentities: {
      userAssignedResourceIds: [reviewIdentityResourceId]
    }
    registries: [
      {
        server: registryLoginServer
        identity: reviewIdentityResourceId
      }
    ]
    secrets: [
      {
        name: 'csrf-secret'
        keyVaultUrl: '${keyVaultUri}secrets/csrf-secret'
        identity: reviewIdentityResourceId
      }
      {
        name: 'appinsights-connection-string'
        keyVaultUrl: '${keyVaultUri}secrets/${appInsightsSecretName}'
        identity: reviewIdentityResourceId
      }
    ]
    volumes: dataVolume
    containers: [
      {
        name: 'ytshort'
        image: image
        command: ['ytshort']
        args: ['review', '--serve']
        resources: {
          cpu: json('0.5')
          memory: '1Gi'
        }
        env: reviewEnv
        volumeMounts: dataVolumeMount
        probes: [
          {
            type: 'Readiness'
            httpGet: {
              path: '/health'
              port: 8080
            }
            initialDelaySeconds: 5
            periodSeconds: 10
          }
        ]
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Least-privilege grants for the review identity
// ---------------------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// Scoped to ONE secret, not the vault. This is what lets the review app read its
// CSRF secret while remaining unable to read the Google credential sitting in
// the same vault. The secret must already exist -- deployment.md sets secrets in
// phase 5, before this template runs in phase 7.
resource csrfSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: keyVault
  name: 'csrf-secret'
}

resource reviewCsrfSecretAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: csrfSecret
  name: guid(csrfSecret.id, reviewIdentityPrincipalId, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      keyVaultSecretsUserRoleId
    )
    principalId: reviewIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Telemetry from the review tier matters most -- it is the only internet-facing
// component and the only place a human decides to publish. Granting it is still
// least-privilege because this, like the CSRF grant above, is scoped to a single
// secret: the review identity ends with two per-secret grants and no role over
// the vault, so the Google credential in the same vault stays out of reach.
resource appInsightsSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: keyVault
  name: appInsightsSecretName
}

resource reviewAppInsightsSecretAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsightsSecret
  name: guid(appInsightsSecret.id, reviewIdentityPrincipalId, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      keyVaultSecretsUserRoleId
    )
    principalId: reviewIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// A custom role with exactly two actions. "Container Apps Contributor" would
// also work and would let the review app rewrite its own infrastructure.
resource jobStarterRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'ytshort-job-starter', environment)
  properties: {
    roleName: 'ytshort Job Starter (${environment})'
    description: 'Start the ytshort ingest job. Nothing else.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.App/jobs/read'
          'Microsoft.App/jobs/start/action'
        ]
        notActions: []
      }
    ]
    assignableScopes: [resourceGroup().id]
  }
}

resource ingestJobRef 'Microsoft.App/jobs@2024-03-01' existing = {
  name: names.ingestJob
  dependsOn: [ingestJob]
}

resource reviewCanStartJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: ingestJobRef
  name: guid(ingestJobRef.id, reviewIdentityPrincipalId, jobStarterRole.id)
  properties: {
    roleDefinitionId: jobStarterRole.id
    principalId: reviewIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Outputs -- identifiers only
// ---------------------------------------------------------------------------

output ingestJobName string = names.ingestJob
output pruneJobName string = names.pruneJob
output reviewAppName string = names.reviewApp
output reviewAppFqdn string = ingressExternal ? reviewApp.outputs.fqdn : ''
output reviewAppUrl string = ingressExternal ? 'https://${reviewApp.outputs.fqdn}' : 'internal ingress -- not publicly reachable yet'
