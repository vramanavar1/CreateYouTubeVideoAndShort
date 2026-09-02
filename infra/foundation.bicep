// ============================================================================
// ytshort -- foundation
//
// Everything the workloads need to exist before an image can be built and
// deployed: registry, vault, storage, observability, identities, and the
// Container Apps environment.
//
// Deployed BEFORE the image exists. apps.bicep comes after `az acr build`,
// because a Job or App that references a missing tag fails to deploy. Splitting
// at that seam is what makes the ordering explicit instead of relying on a
// placeholder image.
//
// Nothing secret is emitted as an output: deployment history is stored in plain
// text and is readable by anyone with Reader on the resource group.
// ============================================================================

targetScope = 'resourceGroup'

@description('Workload name, used as the naming stem.')
@minLength(2)
@maxLength(12)
param workload string = 'ytshort'

@description('Environment name.')
@allowed(['dev', 'prod'])
param environment string

@description('Azure region.')
param location string = resourceGroup().location

@description('Object id of the human operator. Gets Key Vault Secrets Officer so they can set the Google credential secrets, and is the principal allowed into the review UI.')
param operatorObjectId string

@description('Extra tags merged over the defaults.')
param tags object = {}

@description('Days to retain Log Analytics data.')
param logRetentionDays int = environment == 'prod' ? 90 : 30

// ---------------------------------------------------------------------------
// Naming and tags
// ---------------------------------------------------------------------------

var namingPrefix = '${workload}-${environment}'
var uniqueSuffix = uniqueString(resourceGroup().id)

var names = {
  logAnalytics: 'log-${namingPrefix}'
  appInsights: 'appi-${namingPrefix}'
  // ACR and storage names are alphanumeric-only and globally unique.
  registry: toLower('cr${replace(namingPrefix, '-', '')}${substring(uniqueSuffix, 0, 6)}')
  keyVault: take(toLower('kv-${namingPrefix}-${substring(uniqueSuffix, 0, 6)}'), 24)
  storage: take(toLower('st${replace(namingPrefix, '-', '')}${substring(uniqueSuffix, 0, 6)}'), 24)
  environment: 'cae-${namingPrefix}'
  jobIdentity: 'id-${namingPrefix}-job'
  reviewIdentity: 'id-${namingPrefix}-review'
}

var defaultTags = {
  Environment: environment
  Application: workload
  ManagedBy: 'Bicep'
}
var allTags = union(defaultTags, tags)

var fileShareName = 'ytshort-data'

// Well-known built-in role definition ids.
var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
}

// ---------------------------------------------------------------------------
// Observability -- first, so everything else can point diagnostics at it
// ---------------------------------------------------------------------------

module logAnalytics 'br/avm:res/operational-insights/workspace:0.16.1' = {
  name: 'log-analytics'
  params: {
    name: names.logAnalytics
    location: location
    tags: allTags
    dataRetention: logRetentionDays
  }
}

module appInsights 'br/avm:res/insights/component:0.8.0' = {
  name: 'app-insights'
  params: {
    name: names.appInsights
    location: location
    tags: allTags
    workspaceResourceId: logAnalytics.outputs.resourceId
  }
}

// Read the connection string the same way the storage account key is read below:
// with a deploy-time reference() off an `existing` resource. The AVM module does
// expose it as an output, but a module output is a *nested deployment* output and
// is recorded in deployment history in plain text -- so using it would leak an
// ingestion key through the back door.
resource appInsightsRef 'Microsoft.Insights/components@2020-02-02' existing = {
  name: names.appInsights
  dependsOn: [appInsights]
}

// ---------------------------------------------------------------------------
// Identities -- one per workload, so they can be granted differently.
// This separation is the point: the review identity must never be able to read
// the vault.
// ---------------------------------------------------------------------------

module jobIdentity 'br/avm:res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'job-identity'
  params: {
    name: names.jobIdentity
    location: location
    tags: allTags
  }
}

module reviewIdentity 'br/avm:res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'review-identity'
  params: {
    name: names.reviewIdentity
    location: location
    tags: allTags
  }
}

// ---------------------------------------------------------------------------
// Container registry
// ---------------------------------------------------------------------------

module registry 'br/avm:res/container-registry/registry:0.13.0' = {
  name: 'registry'
  params: {
    name: names.registry
    location: location
    tags: allTags
    acrSku: environment == 'prod' ? 'Premium' : 'Basic'
    // Managed identity only; no admin user, so there is no registry password
    // anywhere in the system.
    acrAdminUserEnabled: false
    diagnosticSettings: [
      {
        workspaceResourceId: logAnalytics.outputs.resourceId
      }
    ]
    roleAssignments: [
      {
        principalId: jobIdentity.outputs.principalId
        roleDefinitionIdOrName: roles.acrPull
        principalType: 'ServicePrincipal'
      }
      {
        principalId: reviewIdentity.outputs.principalId
        roleDefinitionIdOrName: roles.acrPull
        principalType: 'ServicePrincipal'
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Key Vault -- the Google credential lives here, never on disk
// ---------------------------------------------------------------------------

module keyVault 'br/avm:res/key-vault/vault:0.14.0' = {
  name: 'key-vault'
  params: {
    name: names.keyVault
    location: location
    tags: allTags
    // RBAC, not access policies: assignments are visible in the same place as
    // every other permission in the subscription.
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    sku: 'standard'
    diagnosticSettings: [
      {
        workspaceResourceId: logAnalytics.outputs.resourceId
      }
    ]
    roleAssignments: [
      {
        // The Job reads the Google credential. The review identity is
        // deliberately absent from this list.
        principalId: jobIdentity.outputs.principalId
        roleDefinitionIdOrName: roles.keyVaultSecretsUser
        principalType: 'ServicePrincipal'
      }
      {
        // The operator sets the secrets from their workstation.
        principalId: operatorObjectId
        roleDefinitionIdOrName: roles.keyVaultSecretsOfficer
        principalType: 'User'
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Storage -- job records, media, outputs. No credentials.
// ---------------------------------------------------------------------------

module storage 'br/avm:res/storage/storage-account:0.33.0' = {
  name: 'storage'
  params: {
    name: names.storage
    location: location
    tags: allTags
    skuName: environment == 'prod' ? 'Standard_ZRS' : 'Standard_LRS'
    kind: 'StorageV2'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    // Azure Files SMB mounts authenticate with the account key, so shared-key
    // access cannot be disabled here. Stated plainly rather than pretended away;
    // the compensating controls are that no credential is stored on the share
    // and the key never leaves the deployment.
    allowSharedKeyAccess: true
    fileServices: {
      shares: [
        {
          name: fileShareName
          accessTier: 'TransactionOptimized'
          shareQuota: environment == 'prod' ? 512 : 100
        }
      ]
      diagnosticSettings: [
        {
          workspaceResourceId: logAnalytics.outputs.resourceId
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Container Apps environment
// ---------------------------------------------------------------------------

module managedEnvironment 'br/avm:res/app/managed-environment:0.15.0' = {
  name: 'managed-environment'
  params: {
    name: names.environment
    location: location
    tags: allTags
    // 'azure-monitor' routes container stdout/stderr through diagnostic settings
    // rather than the legacy path that requires the workspace shared key. One
    // less secret to handle, and the destination is managed by RBAC.
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
    diagnosticSettings: [
      {
        workspaceResourceId: logAnalytics.outputs.resourceId
      }
    ]
    zoneRedundant: false
  }
}

// The Azure Files link is declared directly rather than through the module, so
// the account key is read with listKeys() at deploy time and never becomes a
// template parameter (parameters are recorded in deployment history).
resource environmentRef 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: names.environment
  dependsOn: [managedEnvironment]
}

resource storageRef 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: names.storage
  dependsOn: [storage]
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environmentRef
  name: 'ytshort-data'
  properties: {
    azureFile: {
      accountName: names.storage
      accountKey: storageRef.listKeys().keys[0].value
      shareName: fileShareName
      accessMode: 'ReadWrite'
    }
  }
}

resource keyVaultRef 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: names.keyVault
  dependsOn: [keyVault]
}

// Declared here rather than through the vault module's `secrets` parameter, for
// the same reason environmentStorage is declared directly: a module parameter is
// a nested deployment parameter. Writing it during the foundation deployment is
// also what keeps this off the operator's checklist -- unlike csrf-secret, nobody
// has to set it by hand before the apps deployment can reference it.
resource appInsightsSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVaultRef
  name: 'appinsights-connection-string'
  properties: {
    value: appInsightsRef.properties.ConnectionString
    contentType: 'App Insights connection string (contains an ingestion key)'
  }
}

// ---------------------------------------------------------------------------
// Outputs -- identifiers only. Never a secret, never a key.
// ---------------------------------------------------------------------------

output registryName string = names.registry
output registryLoginServer string = registry.outputs.loginServer
output keyVaultName string = names.keyVault
output keyVaultUri string = keyVault.outputs.uri
output storageAccountName string = names.storage
output fileShareName string = fileShareName
output environmentName string = names.environment
output environmentResourceId string = managedEnvironment.outputs.resourceId
output environmentStorageName string = environmentStorage.name
output jobIdentityResourceId string = jobIdentity.outputs.resourceId
output jobIdentityClientId string = jobIdentity.outputs.clientId
output reviewIdentityResourceId string = reviewIdentity.outputs.resourceId
output reviewIdentityClientId string = reviewIdentity.outputs.clientId
output reviewIdentityPrincipalId string = reviewIdentity.outputs.principalId
output appInsightsName string = names.appInsights
// The secret's *name*, never its value: the connection string embeds an ingestion
// key and deployment history is plain text. Both workloads read it from the vault
// through their own identities.
output appInsightsSecretName string = appInsightsSecret.name
