// Grants one managed identity permission to call inference on a Foundry
// (Azure OpenAI) account that lives in a different resource group.
//
// This is a separate module purely because the grant has to be deployed at the
// Foundry account's scope, and a Bicep module is the only way to change scope
// mid-deployment. It creates nothing -- it only assigns a role on a resource
// somebody else owns.

targetScope = 'resourceGroup'

@description('Name of the existing Foundry (Azure OpenAI) account.')
param accountName string

@description('Principal id of the identity that will call inference.')
param principalId string

// Cognitive Services OpenAI User: inference only. Deliberately not
// "Contributor", which would also allow creating and deleting deployments.
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource account 'Microsoft.CognitiveServices/accounts@2023-05-01' existing = {
  name: accountName
}

resource inferenceAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  // Scoped to the account, not the resource group -- the job gets to call this
  // one Foundry resource and nothing else that happens to live beside it.
  scope: account
  name: guid(account.id, principalId, openAiUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      openAiUserRoleId
    )
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
