param(
    [string]$WorkspaceRoot = "H:\AI",
    [string]$N8nProjectId = "VJyN26pyhAlGr5Mr"
)

$ErrorActionPreference = "Stop"
$credentialPath = Join-Path $WorkspaceRoot "n8n\local-files\kernel-v2-templates\credential-import.json"
$workflowContainerPath = "/files/kernel-v2-templates/workflows.json"
$credentialContainerPath = "/files/kernel-v2-templates/credential-import.json"

try {
    $token = docker exec AI env -u PYTHONPATH /opt/assistant-kernel-venv/bin/python -c "from assistant_kernel.config import KernelSettings; print(KernelSettings.from_environment().load_provider_secrets().internal.kernel_api_token.get_secret_value())"
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($token)) {
        throw "Kernel API token is not available"
    }
    $credentialRecords = @(
        @{
            id = "kernelV2ApiAuth"
            name = "Assistant Kernel API"
            type = "httpHeaderAuth"
            data = @{
                name = "Authorization"
                value = "Bearer $token"
            }
        }
    )
    $credential = ConvertTo-Json -InputObject $credentialRecords -Depth 6
    Set-Content -LiteralPath $credentialPath -Value $credential -Encoding utf8
    docker exec -u node n8n n8n import:credentials --input=$credentialContainerPath --projectId=$N8nProjectId
    if ($LASTEXITCODE -ne 0) { throw "n8n credential import failed" }
    docker exec -u node n8n n8n import:workflow --input=$workflowContainerPath --projectId=$N8nProjectId
    if ($LASTEXITCODE -ne 0) { throw "n8n workflow import failed" }
}
finally {
    if (Test-Path -LiteralPath $credentialPath) {
        Remove-Item -LiteralPath $credentialPath -Force
    }
}

Write-Output "N8N_V2_TEMPLATES_IMPORTED"
