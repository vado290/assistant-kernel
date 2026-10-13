param(
    [string]$WorkspaceRoot = "H:\AI"
)

$ErrorActionPreference = "Stop"
$projectFile = Join-Path $WorkspaceRoot "assistant-kernel\pyproject.toml"
$match = Select-String -LiteralPath $projectFile -Pattern '^version = "([0-9]+\.[0-9]+\.[0-9]+)"$'
if (-not $match) { throw "Assistant Kernel version was not found" }
$version = $match.Matches[0].Groups[1].Value
if ($version -like '0.31.*') {
    & (Join-Path $WorkspaceRoot 'assistant-kernel\scripts\Deploy-SchedulerRelease.ps1') -WorkspaceRoot $WorkspaceRoot
    if ($LASTEXITCODE -ne 0) { throw 'Scheduler release deployment failed' }
    return
}
$runtimeRoot = Join-Path $WorkspaceRoot "assistant-data\runtime"
$versionRoot = Join-Path $runtimeRoot "kernel-venvs\$version"
$pointerPath = Join-Path $runtimeRoot "kernel-current.txt"
$oldVersion = if (Test-Path -LiteralPath $pointerPath) {
    (Get-Content -Raw -LiteralPath $pointerPath).Trim()
} else { "" }
$preflightPython = "/home/v/AI/assistant-data/runtime/kernel-venvs/$oldVersion/bin/python"
if ($oldVersion -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
    throw "Cannot run restart preflight because the active Kernel version is invalid"
}
$preflight = docker exec AI /usr/bin/env -u PYTHONPATH $preflightPython `
    /home/v/AI/assistant-kernel/scripts/restart_preflight.py
$preflightExit = $LASTEXITCODE
$preflight | Write-Output
if ($preflightExit -ne 0) {
    throw "Kernel restart preflight refused deployment (exit=$preflightExit)"
}
$supervisorBackup = "/tmp/assistant-supervisor-pre-kernel-deploy.conf"
docker exec AI cp /etc/supervisor/conf.d/assistant.conf $supervisorBackup
if ($LASTEXITCODE -ne 0) { throw "Supervisor config backup failed" }

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $versionRoot) | Out-Null
if (-not (Test-Path -LiteralPath $versionRoot)) {
    docker exec AI /home/v/miniconda3/bin/python -m venv "/home/v/AI/assistant-data/runtime/kernel-venvs/$version"
    if ($LASTEXITCODE -ne 0) { throw "Kernel venv creation failed" }
}
if (-not (Test-Path -LiteralPath (Join-Path $versionRoot ".complete"))) {
    docker exec AI /usr/bin/env -u PYTHONPATH `
        "/home/v/AI/assistant-data/runtime/kernel-venvs/$version/bin/python" `
        -m pip install --no-cache-dir --no-compile --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Kernel venv pip upgrade failed" }
    docker exec AI /usr/bin/env -u PYTHONPATH `
        "/home/v/AI/assistant-data/runtime/kernel-venvs/$version/bin/python" `
        -m pip install --no-cache-dir --no-compile -r /home/v/AI/assistant-kernel/requirements/kernel.lock
    if ($LASTEXITCODE -ne 0) { throw "Kernel dependency install failed" }
    docker exec AI /usr/bin/env -u PYTHONPATH `
        "/home/v/AI/assistant-data/runtime/kernel-venvs/$version/bin/python" `
        -m pip install --no-cache-dir --no-compile --no-deps /home/v/AI/assistant-kernel
    if ($LASTEXITCODE -ne 0) { throw "Kernel package install failed" }
    docker exec AI /usr/bin/env -u PYTHONPATH `
        "/home/v/AI/assistant-data/runtime/kernel-venvs/$version/bin/python" -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Kernel pip check failed" }
    docker exec AI /usr/bin/env -u PYTHONPATH `
        "/home/v/AI/assistant-data/runtime/kernel-venvs/$version/bin/python" -c `
        "import importlib.metadata as m; import assistant_kernel; import assistant_kernel.api.main, assistant_kernel.services.discord, assistant_kernel.services.feishu, assistant_kernel.worker.run_main, assistant_kernel.worker.feishu_main; expected='$version'; assert assistant_kernel.__version__ == expected, (assistant_kernel.__version__, expected); assert m.version('assistant-kernel') == expected, (m.version('assistant-kernel'), expected); assert m.version('arkruntime') == '0.8.0', m.version('arkruntime')"
    if ($LASTEXITCODE -ne 0) { throw "Kernel import prewarm failed" }
    docker exec AI touch "/home/v/AI/assistant-data/runtime/kernel-venvs/$version/.complete"
    if ($LASTEXITCODE -ne 0) { throw "Kernel venv completion marker failed" }
}

$newPython = "/home/v/AI/assistant-data/runtime/kernel-venvs/$version/bin/python"
docker exec AI /usr/bin/env -u PYTHONPATH $newPython /home/v/AI/assistant-kernel/scripts/verify_release.py
if ($LASTEXITCODE -ne 0) { throw "Installed release does not match the current source" }
$schemaBefore = (docker exec AI /usr/bin/env -u PYTHONPATH $newPython -c "import asyncio; from assistant_kernel.config import KernelSettings; from assistant_kernel.repositories import CoreRepository; print(asyncio.run(CoreRepository(KernelSettings.from_environment()).schema_version()))").Trim()
if ($LASTEXITCODE -ne 0 -or $schemaBefore -notin @('core5.v6','core5.v7')) { throw "Cannot determine compatible source schema" }
$runtimeWorkerPresent = [bool]((docker exec AI supervisorctl -c /etc/supervisor/conf.d/assistant.conf status) -match 'kernel-runtime-worker:')
$stopTargets = @('kernel-api','kernel-mcp','kernel-discord','kernel-feishu','kernel-maintenance','kernel-run-worker:*')
if ($runtimeWorkerPresent) { $stopTargets += 'kernel-runtime-worker:*' }
docker exec AI supervisorctl -c /etc/supervisor/conf.d/assistant.conf stop @stopTargets
if ($LASTEXITCODE -ne 0) { throw "Kernel services could not be drained" }
$temporaryPointer = "$pointerPath.tmp"
$auxRetirementStarted = $false

try {
    if ($version -eq '0.24.9') {
        $auxRetirementStarted = $true
        docker exec AI /usr/bin/env -u PYTHONPATH $newPython /home/v/AI/assistant-kernel/scripts/retire_aux_agents.py
        if ($LASTEXITCODE -ne 0) { throw "Child/Operator retirement refused or failed" }
    }
    if ($version -eq '0.24.8') {
        docker exec AI /usr/bin/env -u PYTHONPATH $newPython /home/v/AI/assistant-kernel/scripts/migrate_core5_v7.py --receipt /home/v/AI/assistant-data/runtime/core5-v7-migration.json
        if ($LASTEXITCODE -ne 0) { throw "core5.v7 migration refused or failed" }
    }
    $version | Set-Content -LiteralPath $temporaryPointer -Encoding ascii -NoNewline
    Move-Item -LiteralPath $temporaryPointer -Destination $pointerPath -Force
    docker exec AI install -m 0644 /home/v/AI/assistant-kernel/deploy/supervisord.conf /etc/supervisor/conf.d/assistant.conf
    if ($LASTEXITCODE -ne 0) { throw "Supervisor config install failed" }
    docker exec AI supervisorctl -c /etc/supervisor/conf.d/assistant.conf reread
    if ($LASTEXITCODE -ne 0) { throw "Supervisor reread failed" }
    docker exec AI supervisorctl -c /etc/supervisor/conf.d/assistant.conf update
    if ($LASTEXITCODE -ne 0) { throw "Supervisor update failed" }
    docker exec AI sh -lc "supervisorctl -c /etc/supervisor/conf.d/assistant.conf restart kernel-api kernel-mcp kernel-discord kernel-feishu kernel-maintenance 'kernel-run-worker:*' 'kernel-runtime-worker:*'"
    if ($LASTEXITCODE -ne 0) { throw "Kernel process restart failed" }
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $response = Invoke-RestMethod -Uri "http://127.0.0.1:18001/health/ready" -TimeoutSec 5
            if ($response.status -eq "ready" -and $response.interactive_ready -and $response.runtime_ready) { $ready = $true; break }
        } catch {}
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw "Kernel readiness did not recover" }
    $deployed = Invoke-RestMethod -Uri "http://127.0.0.1:18001/version" -TimeoutSec 5
    if ($deployed.version -ne $version) {
        throw "Kernel version endpoint mismatch: expected $version, got $($deployed.version)"
    }
}
catch {
    $deployFailure = $_
    docker exec AI supervisorctl -c /etc/supervisor/conf.d/assistant.conf stop kernel-api kernel-mcp kernel-discord kernel-feishu kernel-maintenance 'kernel-run-worker:*' 'kernel-runtime-worker:*' | Out-Null
    $schemaAfter = (docker exec AI /usr/bin/env -u PYTHONPATH $newPython -c "import asyncio; from assistant_kernel.config import KernelSettings; from assistant_kernel.repositories import CoreRepository; print(asyncio.run(CoreRepository(KernelSettings.from_environment()).schema_version()))").Trim()
    if ($LASTEXITCODE -ne 0) { throw "Rollback schema probe failed; keep services stopped. $deployFailure" }
    if ($schemaAfter -ne $schemaBefore) {
        if ($schemaBefore -ne 'core5.v6' -or $schemaAfter -ne 'core5.v7') { throw "Unexpected schema transition; keep services stopped. $deployFailure" }
        docker exec AI /usr/bin/env -u PYTHONPATH $newPython /home/v/AI/assistant-kernel/scripts/migrate_core5_v7.py --downgrade
        if ($LASTEXITCODE -ne 0) { throw "Rollback refused; services remain stopped. $deployFailure" }
    }
    if ($auxRetirementStarted) {
        docker exec AI /usr/bin/env -u PYTHONPATH $newPython /home/v/AI/assistant-kernel/scripts/retire_aux_agents.py --restore
        if ($LASTEXITCODE -ne 0) { throw "Agent configuration restore failed; keep services stopped. $deployFailure" }
    }
    $oldVersion | Set-Content -LiteralPath $temporaryPointer -Encoding ascii -NoNewline
    Move-Item -LiteralPath $temporaryPointer -Destination $pointerPath -Force
    docker exec AI cp $supervisorBackup /etc/supervisor/conf.d/assistant.conf | Out-Null
    docker exec AI supervisorctl -c /etc/supervisor/conf.d/assistant.conf reread | Out-Null
    docker exec AI supervisorctl -c /etc/supervisor/conf.d/assistant.conf update | Out-Null
    docker exec AI supervisorctl -c /etc/supervisor/conf.d/assistant.conf start @stopTargets | Out-Null
    throw $deployFailure
}

Write-Output "ASSISTANT_KERNEL_DEPLOYED; version=$version; previous=$oldVersion; container_recreated=false"
