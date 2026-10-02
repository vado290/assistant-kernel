param(
    [string]$WorkspaceRoot = "H:\AI"
)

$ErrorActionPreference = "Stop"
$configRoot = Join-Path $WorkspaceRoot "assistant-data\config"
$examplePath = Join-Path $configRoot "service-credentials.example.json"
$targetPath = Join-Path $configRoot "service-credentials.json"
$ragEnvPath = Join-Path $WorkspaceRoot "rag-stack\.env"

$document = Get-Content -Raw -LiteralPath $examplePath | ConvertFrom-Json
$dotenv = @{}
foreach ($line in Get-Content -LiteralPath $ragEnvPath) {
    if ($line -match '^\s*([^#][A-Za-z0-9_]*)=(.*)$') {
        $dotenv[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
    }
}
$document.databases.rag_postgresql.database = [string]$dotenv.POSTGRES_DB
$document.databases.rag_postgresql.username = [string]$dotenv.POSTGRES_USER
$document.databases.rag_postgresql.password = [string]$dotenv.POSTGRES_PASSWORD
$document | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $targetPath -Encoding utf8
Write-Output "SERVICE_CREDENTIALS_READY"
