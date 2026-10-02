param(
    [string]$WorkspaceRoot = "H:\AI",
    [string[]]$OwnerOpenId = @()
)

$ErrorActionPreference = "Stop"
$sourcePath = Join-Path $WorkspaceRoot "codex_team.py"
$targetPath = Join-Path $WorkspaceRoot "assistant-data\config\service-credentials.json"
if (-not (Test-Path -LiteralPath $sourcePath)) {
    throw "codex_team.py was not found"
}

$source = Get-Content -Raw -LiteralPath $sourcePath
function Get-PythonStringConstant([string]$Name) {
    $pattern = '(?m)^\s*' + [regex]::Escape($Name) + '\s*=\s*(["''])(?<value>.*?)\1\s*$'
    $match = [regex]::Match($source, $pattern)
    if (-not $match.Success) { return "" }
    return $match.Groups['value'].Value
}

$appId = Get-PythonStringConstant "FEISHU_APP_ID"
$appSecret = Get-PythonStringConstant "FEISHU_APP_SECRET"
$verificationToken = Get-PythonStringConstant "FEISHU_VERIFICATION_TOKEN"
$encryptKey = Get-PythonStringConstant "FEISHU_ENCRYPT_KEY"
if ([string]::IsNullOrWhiteSpace($appId) -or [string]::IsNullOrWhiteSpace($appSecret)) {
    throw "codex_team.py does not contain non-empty Feishu App ID and Secret"
}

$document = if (Test-Path -LiteralPath $targetPath) {
    Get-Content -Raw -LiteralPath $targetPath | ConvertFrom-Json
}
else {
    [pscustomobject]@{}
}
if ($null -eq $document.services) {
    $document | Add-Member -MemberType NoteProperty -Name services -Value ([pscustomobject]@{})
}
$ownerOpenIds = @()
if ($null -ne $document.services.feishu -and $null -ne $document.services.feishu.owner_open_ids) {
    $ownerOpenIds = @($document.services.feishu.owner_open_ids)
}
$ownerOpenIds = @($ownerOpenIds + $OwnerOpenId | Where-Object {
    -not [string]::IsNullOrWhiteSpace($_) -and $_ -notin @("user-1", "owner", "another-owner")
} | Select-Object -Unique)
$feishu = [ordered]@{
    enabled = $true
    receive_mode = "websocket"
    app_id = $appId
    app_secret = $appSecret
    verification_token = $verificationToken
    encrypt_key = $encryptKey
    owner_open_ids = $ownerOpenIds
}
$document.services | Add-Member -MemberType NoteProperty -Name feishu -Value $feishu -Force

$targetDirectory = Split-Path -Parent $targetPath
New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
if (Test-Path -LiteralPath $targetPath) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    Copy-Item -LiteralPath $targetPath -Destination "$targetPath.$stamp.bak"
}
$temporary = "$targetPath.tmp"
$document | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $temporary -Encoding utf8
Move-Item -LiteralPath $temporary -Destination $targetPath -Force
Write-Output "FEISHU_CONFIG_IMPORTED; app_id_present=true; app_secret_present=true; receive_mode=websocket"
