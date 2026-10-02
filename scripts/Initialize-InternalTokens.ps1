param(
    [string]$WorkspaceRoot = "H:\AI"
)

$ErrorActionPreference = "Stop"
$path = Join-Path $WorkspaceRoot "assistant-data\config\provider-secrets.json"
$document = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json

function New-Token {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToHexString($bytes).ToLowerInvariant()
}

foreach ($name in @("kernel_api_token", "mcp_bearer_token", "n8n_callback_secret")) {
    if ([string]::IsNullOrWhiteSpace([string]$document.internal.$name)) {
        $document.internal.$name = New-Token
    }
}
$document | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $path -Encoding utf8
Write-Output "INTERNAL_TOKENS_READY"
