param(
    [string]$WorkspaceRoot = "H:\AI",
    [string]$ApplicationId = "1481082916296720404",
    [int]$ExpectedCount = 48
)

$ErrorActionPreference = "Stop"
$sourcePath = Join-Path $WorkspaceRoot "run\quant_memory_cc.env"
if (-not (Test-Path -LiteralPath $sourcePath)) {
    throw "Legacy Discord environment file was not found"
}
$values = @{}
foreach ($line in Get-Content -LiteralPath $sourcePath) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
        $values[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
    }
}
$botToken = [string]$values.DISCORD_BOT_TOKEN
if ([string]::IsNullOrWhiteSpace($botToken)) {
    throw "Legacy Discord Bot Token is empty"
}

$uri = "https://discord.com/api/v10/applications/$ApplicationId/commands"
$headers = @{
    Authorization = "Bot $botToken"
    "User-Agent" = "AssistantKernel/0.12.0"
}
$raw = (Invoke-WebRequest -Uri $uri -Headers $headers -TimeoutSec 20).Content
$commands = $raw | ConvertFrom-Json
if ($commands.Count -ne $ExpectedCount) {
    throw "Expected $ExpectedCount global commands but found $($commands.Count); no deletion performed"
}

$backupRoot = Join-Path $WorkspaceRoot "assistant-data\backups\discord-global-commands"
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupPath = Join-Path $backupRoot "$stamp.json"
$raw | Set-Content -LiteralPath $backupPath -Encoding utf8

$response = Invoke-WebRequest -Method Put -Uri $uri -Headers $headers `
    -ContentType "application/json" -Body "[]" -TimeoutSec 20
$remainingRaw = (Invoke-WebRequest -Uri $uri -Headers $headers -TimeoutSec 20).Content
$remaining = $remainingRaw | ConvertFrom-Json
if ($remaining.Count -ne 0) {
    throw "Discord still reports $($remaining.Count) global commands after deletion"
}
Write-Output (
    "DISCORD_GLOBAL_COMMANDS_REMOVED; removed={0}; remaining=0; backup={1}; status={2}" `
    -f $commands.Count, $backupPath, $response.StatusCode
)
