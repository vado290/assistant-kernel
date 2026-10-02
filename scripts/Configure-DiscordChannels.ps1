param(
    [string]$WorkspaceRoot = "H:\AI",
    [string]$ApplicationId = "1481082916296720404",
    [string]$GuildId = "1481086241414975562",
    [string]$HomeChannelId = "1481086242824388683",
    [string[]]$ArchiveChannelId = @(
        "1481102553281331220",
        "1481102739617480818",
        "1481102786912456839"
    )
)

$ErrorActionPreference = "Stop"
$sourcePath = Join-Path $WorkspaceRoot "run\quant_memory_cc.env"
$values = @{}
foreach ($line in Get-Content -LiteralPath $sourcePath) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
        $values[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
    }
}
$botToken = [string]$values.DISCORD_BOT_TOKEN
if ([string]::IsNullOrWhiteSpace($botToken)) { throw "Discord Bot Token is empty" }
$headers = @{
    Authorization = "Bot $botToken"
    "User-Agent" = "AssistantKernel/0.13.0"
}
$apiRoot = "https://discord.com/api/v10"
$channels = (
    Invoke-WebRequest -Uri "$apiRoot/guilds/$GuildId/channels" `
        -Headers $headers -TimeoutSec 20
).Content | ConvertFrom-Json
$targets = @($HomeChannelId) + @($ArchiveChannelId)
foreach ($target in $targets) {
    if (-not ($channels | Where-Object { [string]$_.id -eq $target })) {
        throw "Discord channel $target was not found; no changes performed"
    }
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path $WorkspaceRoot "assistant-data\backups\discord-channels"
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
$backupPath = Join-Path $backupRoot "$stamp.json"
$channels | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $backupPath -Encoding utf8

function Patch-Channel([string]$ChannelId, [hashtable]$Body) {
    Invoke-RestMethod -Method Patch -Uri "$apiRoot/channels/$ChannelId" `
        -Headers $headers -ContentType "application/json" `
        -Body ($Body | ConvertTo-Json -Compress) -TimeoutSec 20 | Out-Null
}

Patch-Channel -ChannelId $HomeChannelId -Body @{ name = "chief-of-staff" }

$denyBits = (
    [bigint](1 -shl 6) -bor
    [bigint](1 -shl 11) -bor
    ([bigint]1 -shl 35) -bor
    ([bigint]1 -shl 36) -bor
    ([bigint]1 -shl 38)
)
foreach ($channelId in $ArchiveChannelId) {
    $channel = $channels | Where-Object { [string]$_.id -eq $channelId } | Select-Object -First 1
    $oldName = [string]$channel.name
    $archiveName = if ($oldName.StartsWith("archive-")) { $oldName } else { "archive-$oldName" }
    Patch-Channel -ChannelId $channelId -Body @{ name = $archiveName.Substring(0, [Math]::Min(100, $archiveName.Length)) }
    $overwrite = @($channel.permission_overwrites) | Where-Object {
        [string]$_.id -eq $GuildId -and [int]$_.type -eq 0
    } | Select-Object -First 1
    $allow = if ($overwrite) { [bigint]::Parse([string]$overwrite.allow) } else { [bigint]0 }
    $deny = if ($overwrite) { [bigint]::Parse([string]$overwrite.deny) } else { [bigint]0 }
    $allow = $allow -band (-bnot $denyBits)
    $deny = $deny -bor $denyBits
    Invoke-RestMethod -Method Put `
        -Uri "$apiRoot/channels/$channelId/permissions/$GuildId" `
        -Headers $headers -ContentType "application/json" `
        -Body (@{ type = 0; allow = [string]$allow; deny = [string]$deny } | ConvertTo-Json -Compress) `
        -TimeoutSec 20 | Out-Null
}

$updated = (
    Invoke-WebRequest -Uri "$apiRoot/guilds/$GuildId/channels" `
        -Headers $headers -TimeoutSec 20
).Content | ConvertFrom-Json
$summary = $updated | Where-Object { [string]$_.id -in $targets } | ForEach-Object {
    [pscustomobject]@{ id = [string]$_.id; name = [string]$_.name }
}
Write-Output "DISCORD_CHANNELS_CONFIGURED; backup=$backupPath"
$summary | ConvertTo-Json -Depth 3
