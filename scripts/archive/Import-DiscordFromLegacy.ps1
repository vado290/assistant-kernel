param(
    [string]$WorkspaceRoot = "H:\AI",
    [Parameter(Mandatory = $true)]
    [string]$ApplicationId,
    [Parameter(Mandatory = $true)]
    [string]$HomeChannelId,
    [string[]]$ChannelId = @(),
    [string[]]$ArchiveChannelId = @(
        "1481102553281331220",
        "1481102739617480818",
        "1481102786912456839"
    ),
    [switch]$Enable
)

$ErrorActionPreference = "Stop"
$sourceCandidates = @(
    (Join-Path $WorkspaceRoot "run\quant_memory_cc.env"),
    (Join-Path $WorkspaceRoot "run\quant_memory.env")
)
$sourcePath = $sourceCandidates | Where-Object {
    Test-Path -LiteralPath $_
} | Select-Object -First 1
if (-not $sourcePath) {
    throw "Legacy Discord environment file was not found"
}

$values = @{}
foreach ($line in Get-Content -LiteralPath $sourcePath) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
        $values[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
    }
}
$botToken = [string]$values.DISCORD_BOT_TOKEN
$guildIds = @(
    ([string]$values.DISCORD_GUILD_IDS -split ',') |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ }
)
$userIds = @(
    ([string]$values.DISCORD_ALLOWED_USER_IDS -split ',') |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ }
)
$channelIds = @(
    $ChannelId |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ } |
    Select-Object -Unique
)
if ([string]::IsNullOrWhiteSpace($botToken)) {
    throw "Legacy Discord Bot Token is empty"
}
if ($guildIds.Count -eq 0 -or $userIds.Count -eq 0) {
    throw "Discord Guild IDs and Owner User IDs must be non-empty"
}

$targetPath = Join-Path $WorkspaceRoot "assistant-data\config\service-credentials.json"
$document = if (Test-Path -LiteralPath $targetPath) {
    Get-Content -Raw -LiteralPath $targetPath | ConvertFrom-Json
}
else {
    [pscustomobject]@{}
}
if ($null -eq $document.services) {
    $document | Add-Member -MemberType NoteProperty -Name services -Value ([pscustomobject]@{})
}
$discord = [ordered]@{
    enabled = [bool]$Enable
    application_id = $ApplicationId
    bot_token = $botToken
    home_channel_id = $HomeChannelId
    guild_ids = $guildIds
    allowed_channel_ids = @()
    allowed_user_ids = @()
    owner_user_ids = $userIds
    archived_channel_ids = @($ArchiveChannelId | Select-Object -Unique)
    allow_dm = $false
    message_content = $true
}
$document.services | Add-Member -MemberType NoteProperty -Name discord -Value $discord -Force

$targetDirectory = Split-Path -Parent $targetPath
New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
if (Test-Path -LiteralPath $targetPath) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    Copy-Item -LiteralPath $targetPath -Destination "$targetPath.$stamp.bak"
}
$temporary = "$targetPath.tmp"
$document | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $temporary -Encoding utf8
Move-Item -LiteralPath $temporary -Destination $targetPath -Force
Write-Output ((
    "DISCORD_CONFIG_IMPORTED; enabled={0}; token_present=true; guild_count={1}; " +
    "allowed_channel_count=0; allowed_user_count=0; owner_count={2}"
) -f ([bool]$Enable).ToString().ToLowerInvariant(), $guildIds.Count, $userIds.Count)
