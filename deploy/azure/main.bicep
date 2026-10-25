// hive-mcp on Azure Container Apps with Postgres (pgvector) and Azure Files.
// Two steps, because the app needs an image that must first be built into the new registry:
//   1. az deployment group create -g <rg> -f deploy/azure/main.bicep -p deploy/azure/main.bicepparam -p deployApp=false
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

@description('Public base URL if you front the app with your own domain or a tunnel. Empty = the Container Apps address.')
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
@description('Postgres admin password.')
param dbPassword string

@secure()
@description('Signs login cookies. Generate with: openssl rand -hex 32')
param cookieSecret string

@description('Postgres SKU (Burstable is fine to start; scale up with data).')
param dbSku string = 'Standard_B1ms'

@description('Postgres storage in GB (can grow, not shrink).')
param dbStorageGb int = 64

@description('Hosted embeddings only: full /embeddings URL, key and model.')
param embeddingUrl string = ''
@secure()
param embeddingApiKey string = ''
param embeddingModel string = 'text-embedding-3-small'

var suffix = uniqueString(resourceGroup().id)
var acrName = '${prefix}acr${suffix}'
var appName = '${prefix}-mcp'
var dbHost = '${prefix}-pg-${suffix}'
var storageName = take('${prefix}st${suffix}', 24)
var dbUrl = 'postgresql://hiveadmin:${uriComponent(dbPassword)}@${dbHost}.postgres.database.azure.com:5432/hive?sslmode=require'

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

// AcrPull for the app's identity
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, identity.id, 'acrpull')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: dbHost
  location: location
  sku: { name: dbSku, tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: 'hiveadmin'
    administratorLoginPassword: dbPassword
    storage: { storageSizeGB: dbStorageGb, autoGrow: 'Enabled' }
    backup: { backupRetentionDays: 14, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
  }
}

resource pgExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2023-12-01-preview' = {
  parent: pg
  name: 'azure.extensions'
  properties: { value: 'VECTOR', source: 'user-override' }
}

resource db 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-12-01-preview' = {
  parent: pg
  name: 'hive'
  dependsOn: [pgExtensions]
}

// "Allow Azure services" (0.0.0.0). For stricter isolation use VNet integration + a private endpoint.
resource pgFirewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = {
  parent: pg
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: { minimumTlsVersion: 'TLS1_2', allowBlobPublicAccess: false, supportsHttpsTrafficOnly: true }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  name: '${storage.name}/default/hive-data'
  properties: { shareQuota: 1024 }
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
  name: 'hivedata'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: 'hive-data'
      accessMode: 'ReadWrite'
    }
  }
  dependsOn: [share]
}

var defaultUrl = 'https://${appName}.${env.properties.defaultDomain}'
var baseUrl = empty(publicUrl) ? defaultUrl : publicUrl

var secrets = concat(
  [
    { name: 'database-url', value: dbUrl }
    { name: 'cookie-secret', value: cookieSecret }
  ],
  empty(googleClientSecret) ? [] : [{ name: 'google-secret', value: googleClientSecret }],
  empty(embeddingApiKey) ? [] : [{ name: 'embedding-key', value: embeddingApiKey }]
)

var settings = concat(
  [
    { name: 'HIVE_PROFILE', value: 'external' }
    { name: 'HIVE_PORT', value: '8080' }
    { name: 'HIVE_PUBLIC_URL', value: baseUrl }
    { name: 'HIVE_DATABASE_URL', secretRef: 'database-url' }
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
  ]
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
      terminationGracePeriodSeconds: 30
      containers: [
        {
          name: 'hive'
          image: '${acr.properties.loginServer}/hive-mcp:${imageTag}'
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: settings
          volumeMounts: [{ volumeName: 'data', mountPath: '/data' }]
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8080 }, periodSeconds: 15 }
            { type: 'Readiness', httpGet: { path: '/readyz', port: 8080 }, periodSeconds: 5 }
          ]
        }
      ]
      // One replica: rate limits and the token/user files are per instance.
      scale: { minReplicas: 1, maxReplicas: 1 }
      volumes: [{ name: 'data', storageType: 'AzureFile', storageName: envStorage.name }]
    }
  }
  dependsOn: [pull, db, pgFirewall]
}

output url string = baseUrl
output registry string = acr.properties.loginServer
output acrName string = acr.name
output appName string = appName
