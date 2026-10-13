param(
    [string]$WorkspaceRoot = "H:\AI"
)

$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path $WorkspaceRoot "assistant-data\backups\$stamp"
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

$pgTemp = "/tmp/assistant-$stamp.dump"
docker exec rag-postgres sh -lc ('pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "{0}"' -f $pgTemp)
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
docker cp "rag-postgres:$pgTemp" (Join-Path $backupRoot "postgres.dump")
if ($LASTEXITCODE -ne 0) { throw "docker cp for PostgreSQL backup failed" }
docker exec rag-postgres rm -f -- $pgTemp

Copy-Item -LiteralPath (Join-Path $WorkspaceRoot "docker-compose.yml") -Destination $backupRoot
Copy-Item -LiteralPath (Join-Path $WorkspaceRoot "assistant-kernel") -Destination (Join-Path $backupRoot "assistant-kernel") -Recurse
$discordInterfaces = Join-Path $WorkspaceRoot "assistant-data\config\discord-interfaces.json"
if (Test-Path -LiteralPath $discordInterfaces) {
    $configBackup = Join-Path $backupRoot "config"
    New-Item -ItemType Directory -Force -Path $configBackup | Out-Null
    Copy-Item -LiteralPath $discordInterfaces -Destination $configBackup
}
$vaultSource = Join-Path $WorkspaceRoot "assistant-data\vaults"
$vaultDestination = Join-Path $backupRoot "vaults"
if (Test-Path -LiteralPath $vaultSource) {
    Copy-Item -LiteralPath $vaultSource -Destination $vaultDestination -Recurse
    # Model transcripts and runtime databases are recoverable state, but
    # authentication material must remain in the separately managed secret store.
    Get-ChildItem -LiteralPath $vaultDestination -Filter "auth.json" -File -Recurse |
        Remove-Item -Force
}
$hashes = @()
$skippedEphemeral = @()
Get-ChildItem -LiteralPath $backupRoot -File -Recurse | ForEach-Object {
    $file = $_
    try {
        $hashes += Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256 -ErrorAction Stop
    }
    catch {
        $normalized = $file.FullName.Replace('/', '\')
        if (
            $normalized -match '\\runtime\\codex-home\\(?:\.tmp|tmp|thread-writer-locks)\\' -or
            $normalized -match '\\\.codex\\(?:\.tmp|tmp|thread-writer-locks)\\'
        ) {
            $skippedEphemeral += $file.FullName
        }
        else {
            throw
        }
    }
}
$hashes | Select-Object Path, Hash | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath (Join-Path $backupRoot "sha256-manifest.json") -Encoding utf8
if ($skippedEphemeral.Count -gt 0) {
    $skippedEphemeral | ConvertTo-Json -Depth 2 |
        Set-Content -LiteralPath (Join-Path $backupRoot "skipped-ephemeral.json") -Encoding utf8
}
Write-Output "BACKUP_READY=$backupRoot"
