param(
    [string]$WorkspaceRoot = "H:\AI"
)

$ErrorActionPreference = "Stop"
$pointerPath = Join-Path $WorkspaceRoot "assistant-data\runtime\kernel-current.txt"
if (-not (Test-Path -LiteralPath $pointerPath)) {
    throw "Kernel version pointer is missing: $pointerPath"
}
$version = (Get-Content -Raw -LiteralPath $pointerPath).Trim()
if ($version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
    throw "Kernel version pointer is invalid"
}
$python = "/home/v/AI/assistant-data/runtime/kernel-venvs/$version/bin/python"
$script = "/home/v/AI/assistant-kernel/scripts/restart_preflight.py"
docker exec AI $python $script
exit $LASTEXITCODE
