param(
    [string]$WorkspaceRoot = 'H:\AI',
    [string]$BackupDirectory = '',
    [switch]$Rollback
)
$ErrorActionPreference = 'Stop'
$projectFile = Join-Path $WorkspaceRoot 'assistant-kernel\pyproject.toml'
$versionMatch = Select-String -LiteralPath $projectFile -Pattern '^version = "(0\.31\.[0-9]+)"$'
if (-not $versionMatch) { throw 'Unsupported scheduler release version' }
$releaseVersion = $versionMatch.Matches[0].Groups[1].Value
if (-not $BackupDirectory) {
    $BackupDirectory = Join-Path $WorkspaceRoot ('assistant-data\backups\scheduler-cutover-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$resolvedWorkspace = [IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd('\')
$resolvedBackup = [IO.Path]::GetFullPath($BackupDirectory)
if (-not $resolvedBackup.StartsWith($resolvedWorkspace + '\assistant-data\backups\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Cutover backup must be inside the workspace backup directory'
}
New-Item -ItemType Directory -Force -Path $resolvedBackup | Out-Null
$containerBackup = '/home/v/AI/' + $resolvedBackup.Substring($resolvedWorkspace.Length + 1).Replace('\','/')
$decision = '/home/v/AI/assistant-data/upgrade-decisions/scheduler-' + $releaseVersion + '.json'
docker exec AI test -f $decision
if ($LASTEXITCODE -ne 0) { throw 'Actual approved scheduler implementation instruction is missing' }
if (-not $Rollback) {
    $dumpPath = '/tmp/scheduler-cutover-' + [guid]::NewGuid().ToString('N') + '.dump'
    docker exec rag-postgres sh -lc ('pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "{0}"' -f $dumpPath)
    if ($LASTEXITCODE -ne 0) { throw 'Cutover pg_dump failed' }
    docker cp ('rag-postgres:' + $dumpPath) (Join-Path $resolvedBackup 'postgres.dump')
    if ($LASTEXITCODE -ne 0) { throw 'Cutover dump copy failed' }
    Get-FileHash -LiteralPath (Join-Path $resolvedBackup 'postgres.dump') | Select-Object Hash |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $resolvedBackup 'database-sha256.json') -Encoding utf8
}
$arguments = @('--backup',$containerBackup,'--decision',$decision)
if ($Rollback) { $arguments += '--rollback' }
docker exec -w /home/v/AI/assistant-kernel AI /usr/bin/env -u PYTHONPATH -u PYTHONHOME `
    "/home/v/AI/assistant-data/runtime/kernel-venvs/$releaseVersion/bin/python" -m scripts.deploy_scheduler_release @arguments
if ($LASTEXITCODE -ne 0) { throw "Scheduler deployment failed; inspect receipts in $resolvedBackup" }
Write-Output "SCHEDULER_DEPLOYMENT_RECEIPTS=$resolvedBackup"
