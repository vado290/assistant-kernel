param(
    [string]$WorkspaceRoot = "H:\AI",
    [string]$GuildId = "1481086241414975562",
    [string]$BotUserId = "1481082916296720404"
)

$ErrorActionPreference = "Stop"
$values = @{}
foreach ($line in Get-Content -LiteralPath (Join-Path $WorkspaceRoot "run\quant_memory_cc.env")) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
        $values[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
    }
}
$headers = @{
    Authorization = "Bot $([string]$values.DISCORD_BOT_TOKEN)"
    "User-Agent" = "AssistantKernel/0.13.8"
}
$raw = (Invoke-WebRequest `
    -Uri "https://discord.com/api/v10/guilds/$GuildId/roles" `
    -Headers $headers -TimeoutSec 20).Content
$roles = $raw | ConvertFrom-Json
$role = $roles | Where-Object { [string]$_.tags.bot_id -eq $BotUserId } | Select-Object -First 1
if (-not $role) { throw "Discord Bot managed role was not found" }
$permissions = [bigint]::Parse([string]$role.permissions)
$recommended = (
    ([bigint]1 -shl 4) -bor ([bigint]1 -shl 6) -bor ([bigint]1 -shl 10) -bor
    ([bigint]1 -shl 11) -bor ([bigint]1 -shl 14) -bor
    ([bigint]1 -shl 15) -bor ([bigint]1 -shl 16) -bor
    ([bigint]1 -shl 31) -bor ([bigint]1 -shl 38)
)
[pscustomobject]@{
    role_id = [string]$role.id
    role_name = [string]$role.name
    managed = [bool]$role.managed
    administrator = (($permissions -band 8) -ne 0)
    current_permissions = [string]$permissions
    recommended_permissions = [string]$recommended
    automatic_update_supported = $false
    required_action = "Guild administrator must remove Administrator in Discord UI"
} | ConvertTo-Json -Depth 3
