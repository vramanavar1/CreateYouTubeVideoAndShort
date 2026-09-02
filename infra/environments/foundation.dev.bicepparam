using '../foundation.bicep'

// Values that vary per operator come from the environment so nothing personal
// or subscription-specific is committed. deployment.md phase 4 exports them.
param environment = 'dev'
param workload = 'ytshort'
param operatorObjectId = readEnvironmentVariable('OPERATOR_OBJECT_ID')

param tags = {
  CostCenter: 'Personal'
  Owner: 'vrama'
}
