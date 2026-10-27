// hive-mcp as ONE container on Azure Container Apps: no database service.
// SQLite lives on the container's local disk (it must not sit on a network share); the server backs
// itself up to an Azure Files share every few minutes and restores from it when a new container
// starts on an empty disk (HIVE_BACKUP_DIR; see docs/SINGLE-CONTAINER.md). Two steps, because the
// image must first be built into the new registry:
//   1. az deployment group create -g <rg> -f deploy/azure/single.bicep -p cookieSecret=... deployApp=false
//   2. az acr build -r <acrName> -t hive-mcp:latest --build-arg BAKE_MODEL=true .
//   3. the same deployment again with deployApp=true (the default)
targetScope = 'resourceGroup'

@description('Prefix for resource names (3-12 lowercase letters/digits).')
@minLength(3)
@maxLength(12)
param prefix string = 'hive'

param location string = resourceGroup().location

@description('false = create everything except the app (step 1, before the image exists).')
param deployApp bool = true

@description('Image tag in the registry created by this template.')
param imageTag string = 'latest'

@description('Public base URL if you front the app with your own domain. Empty = the Container Apps address.')
param publicUrl string = ''

@description('Embedder: onnx (offline, bake the model into the image), openai (hosted) or hashing (dev only).')
@allowed(['onnx', 'openai', 'hashing'])
param embedder string = 'onnx'

@description('Google OAuth client for Workspace sign-in (empty = static tokens only).')
param googleClientId string = ''

@secure()
param googleClientSecret string = ''

@description('Workspace domains allowed to sign in, e.g. ["acme.com"].')
param googleAllowedDomains array = []

@secure()
@description('Signs login cookies. Generate with: openssl rand -hex 32')
param cookieSecret string

@description('Minutes between automatic backups (taken only when something changed).')
@minValue(1)
param backupEveryMinutes int = 15

@description('How many backup archives to keep.')
@minValue(1)
param backupKeep int = 8

@description('Hosted embeddings only: full /embeddings URL, key and model.')
param embeddingUrl string = ''
@secure()
param embeddingApiKey string = ''
param embeddingModel string = 'text-embedding-3-small'

@description('Extra HIVE_* settings as name/value pairs (connectors, Slack, retention...). Secrets belong in Key Vault references, not here.')
param extraSettings array = []

var suffix = uniqueString(resourceGroup().id)
var acrName = '${prefix}acr${suffix}'
var appName = '${prefix}-mcp'
var storageName = take('${prefix}st${suffix}', 24)

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-id'
  location: location
}

resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, 'acrpull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: { minimumTlsVersion: 'TLS1_2', allowBlobPublicAccess: false, supportsHttpsTrafficOnly: true }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  name: '${storage.name}/default/hive-backups'
  properties: { shareQuota: 256 }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'hivebackups'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: 'hive-backups'
      accessMode: 'ReadWrite'
    }
  }
  dependsOn: [share]
}

var defaultUrl = 'https://${appName}.${env.properties.defaultDomain}'
var baseUrl = empty(publicUrl) ? defaultUrl : publicUrl

var secrets = concat(
  [{ name: 'cookie-secret', value: cookieSecret }],
  empty(googleClientSecret) ? [] : [{ name: 'google-secret', value: googleClientSecret }],
  empty(embeddingApiKey) ? [] : [{ name: 'embedding-key', value: embeddingApiKey }]
)

var settings = concat(
  [
    { name: 'HIVE_PROFILE', value: 'embedded' }
    { name: 'HIVE_PORT', value: '8080' }
    { name: 'HIVE_DATA_DIR', value: '/data' } // the container's own disk
    { name: 'HIVE_BACKUP_DIR', value: '/backups' } // the Azure Files share
    { name: 'HIVE_BACKUP_EVERY_MINUTES', value: string(backupEveryMinutes) }
    { name: 'HIVE_BACKUP_KEEP', value: string(backupKeep) }
    { name: 'HIVE_PUBLIC_URL', value: baseUrl }
    { name: 'HIVE_SECRET_KEY', secretRef: 'cookie-secret' }
    { name: 'HIVE_EMBEDDER', value: embedder }
    { name: 'HIVE_FORWARDED_ALLOW_IPS', value: '*' } // Container Apps ingress is the only way in
  ],
  empty(googleClientId) ? [] : [
    { name: 'HIVE_GOOGLE_CLIENT_ID', value: googleClientId }
    { name: 'HIVE_GOOGLE_CLIENT_SECRET', secretRef: 'google-secret' }
    { name: 'HIVE_GOOGLE_ALLOWED_DOMAINS', value: string(googleAllowedDomains) }
  ],
  empty(embeddingUrl) ? [] : [
    { name: 'HIVE_EMBEDDING_URL', value: embeddingUrl }
    { name: 'HIVE_EMBEDDING_API_KEY', secretRef: 'embedding-key' }
    { name: 'HIVE_EMBEDDING_MODEL', value: embeddingModel }
  ],
  extraSettings
)

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployApp) {
  name: appName
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identity.id}': {} } }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, targetPort: 8080, transport: 'auto', allowInsecure: false }
      registries: [{ server: acr.properties.loginServer, identity: identity.id }]
      secrets: secrets
    }
    template: {
      // Time for the final backup when the container stops (planned restarts lose nothing).
      terminationGracePeriodSeconds: 60
      containers: [
        {
          name: 'hive'
          image: '${acr.properties.loginServer}/hive-mcp:${imageTag}'
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: settings
          volumeMounts: [{ volumeName: 'backups', mountPath: '/backups' }]
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 15 }
            { type: 'Readiness', httpGet: { path: '/readyz', port: 8080 }, periodSeconds: 5 }
          ]
        }
      ]
      // Exactly one replica: SQLite is single-process, and two revisions must never overlap.
      scale: { minReplicas: 1, maxReplicas: 1 }
      volumes: [{ name: 'backups', storageType: 'AzureFile', storageName: envStorage.name }]
    }
  }
  dependsOn: [pull]
}

output url string = baseUrl
output registry string = acr.properties.loginServer
output acrName string = acr.name
output appName string = appName
