using 'main.bicep'

param prefix = 'hive'
param embedder = 'onnx'
param googleClientId = ''
param googleAllowedDomains = ['example.com']
// Secrets: pass on the command line instead of committing them, e.g.
//   -p dbPassword=$(openssl rand -hex 16) cookieSecret=$(openssl rand -hex 32) googleClientSecret=...
param dbPassword = readEnvironmentVariable('HIVE_DB_PASSWORD', '')
param cookieSecret = readEnvironmentVariable('HIVE_COOKIE_SECRET', '')
param googleClientSecret = readEnvironmentVariable('HIVE_GOOGLE_CLIENT_SECRET', '')
