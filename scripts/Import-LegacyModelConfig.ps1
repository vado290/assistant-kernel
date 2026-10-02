param(
    [string]$WorkspaceRoot = "H:\AI"
)

$ErrorActionPreference = "Stop"
$modelsPath = Join-Path $WorkspaceRoot "models.json"
$apisPath = Join-Path $WorkspaceRoot "APIs.json"
$configRoot = Join-Path $WorkspaceRoot "assistant-data\config"
$secretsPath = Join-Path $configRoot "provider-secrets.json"
$catalogPath = Join-Path $configRoot "model-catalog.json"

New-Item -ItemType Directory -Force -Path $configRoot | Out-Null
$models = Get-Content -Raw -LiteralPath $modelsPath | ConvertFrom-Json
$apis = Get-Content -Raw -LiteralPath $apisPath | ConvertFrom-Json
$secrets = Get-Content -Raw -LiteralPath $secretsPath | ConvertFrom-Json

$providerMap = @{
    qwen = "dashscope"
    doubao = "volcengine"
}
$providerTypes = @{
    openai = "openai_responses"
    google_gemini = "gemini_native"
    doubao = "openai_compatible"
    deepseek = "openai_compatible"
    kimi = "openai_compatible"
    qwen = "openai_compatible"
    claude = "anthropic_native"
}
$baseUrls = @{
    openai = "https://api.openai.com/v1"
    google_gemini = "https://generativelanguage.googleapis.com/v1beta"
    doubao = "https://ark.cn-beijing.volces.com/api/v3"
    deepseek = "https://api.deepseek.com/v1"
    kimi = "https://api.moonshot.ai/v1"
    qwen = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    claude = "https://api.anthropic.com/v1"
}

foreach ($property in $models.psobject.Properties) {
    $legacyName = $property.Name
    $targetName = if ($providerMap.ContainsKey($legacyName)) { $providerMap[$legacyName] } else { $legacyName }
    $provider = $secrets.providers.$targetName
    if ($null -eq $provider) {
        $provider = [pscustomobject]@{
            enabled = $true
            api_key = ""
            workspace_id = ""
            region = "cn-beijing"
            openai_compatible_base_url = $baseUrls[$legacyName]
            native_base_url = $baseUrls[$legacyName]
            rerank_base_url = ""
            models = [pscustomobject]@{}
        }
        $secrets.providers | Add-Member -NotePropertyName $targetName -NotePropertyValue $provider
    }
    $provider | Add-Member -Force -NotePropertyName provider_type -NotePropertyValue $providerTypes[$legacyName]
    $provider | Add-Member -Force -NotePropertyName chat_models -NotePropertyValue $property.Value.models
    if ([string]::IsNullOrWhiteSpace([string]$provider.openai_compatible_base_url)) {
        $provider.openai_compatible_base_url = $baseUrls[$legacyName]
    }
    $legacyKeys = $apis.$legacyName
    if ($legacyKeys -isnot [array]) { $legacyKeys = @($legacyKeys) }
    $legacyKey = [string]($legacyKeys | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace([string]$provider.api_key) -and -not [string]::IsNullOrWhiteSpace($legacyKey)) {
        $provider.api_key = $legacyKey
    }
    $provider.enabled = -not [string]::IsNullOrWhiteSpace([string]$provider.api_key)
}

$models | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $catalogPath -Encoding utf8
$secrets | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $secretsPath -Encoding utf8
Write-Output "MODEL_CONFIG_IMPORTED"
