<#
Deploy the current assistant-kernel source: build one wheel, verify it in the
`test` venv, then install the same wheel into the `prod` venv and restart.
On a failed health check, prod is reinstalled from the previous wheel.

  .\scripts\deploy.ps1                 # full deploy
  .\scripts\deploy.ps1 -SkipTests      # skip the test-venv pytest step
  .\scripts\deploy.ps1 -TestOnly       # build + test venv only, prod untouched
#>
param(
    [string]$TestPath = "tests/core",
    [switch]$SkipTests,
    [switch]$TestOnly
)

$ErrorActionPreference = "Stop"
$Kernel = "/home/v/AI/assistant-kernel"
$Runtime = "/home/v/AI/assistant-data/runtime"
$Venvs = "$Runtime/kernel-venvs"
$Wheels = "$Runtime/wheels"
$BasePython = "/home/v/miniconda3/bin/python"
$SupervisorConf = "/etc/supervisor/conf.d/assistant.conf"
$HealthUrl = "http://127.0.0.1:18001"

# Progress goes to the host stream: PowerShell functions return everything written to output.
function Step($text) { Write-Host "== $text" }
function InContainer {
    # Arguments are passed as separate tokens; never embed double quotes (PowerShell 5.1 strips them).
    docker exec AI /usr/bin/env -u PYTHONPATH -u PYTHONHOME @args | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "container command failed: $($args -join ' ')" }
}
function Supervisor { docker exec AI supervisorctl -c $SupervisorConf @args }
function SupervisorDo { Supervisor @args | Out-Host }

function Ensure-Venv($name) {
    $python = "$Venvs/$name/bin/python"
    docker exec AI test -x $python
    if ($LASTEXITCODE -ne 0) { InContainer $BasePython -m venv "$Venvs/$name" }
    InContainer $python -m pip install --quiet --upgrade pip
    InContainer $python -m pip install --quiet -r "$Kernel/requirements/kernel.lock"
    return $python
}

function Install-Wheel($python, $wheel) {
    InContainer $python -m pip install --quiet --no-deps --force-reinstall $wheel
    InContainer $python -m pip check
}

function Kernel-Programs {
    $names = Supervisor status | ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ -like 'kernel-*' }
    $names | ForEach-Object { if ($_ -like '*:*') { ($_ -split ':')[0] + ':*' } else { $_ } } | Select-Object -Unique
}

function Wait-Ready($expectedVersion) {
    for ($i = 0; $i -lt 45; $i++) {
        try {
            $ready = Invoke-RestMethod -Uri "$HealthUrl/health/ready" -TimeoutSec 5
            $version = Invoke-RestMethod -Uri "$HealthUrl/version" -TimeoutSec 5
            if ($ready.status -eq "ready" -and $version.version -eq $expectedVersion) { return $true }
        } catch {}
        Start-Sleep -Seconds 2
    }
    return $false
}


# 1. build one wheel from the checked-out source
$version = (Select-String -LiteralPath (Join-Path $PSScriptRoot "..\pyproject.toml") -Pattern '^version = "(.+)"$').Matches[0].Groups[1].Value
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Step "build wheel $version ($stamp)"
InContainer $BasePython -m pip wheel --quiet --no-deps -w "$Wheels/$stamp" $Kernel
$wheel = "$(docker exec AI sh -c "ls $Wheels/$stamp/*.whl")".Trim()
if (-not $wheel) { throw "wheel build produced no file" }
# Only wheels that reached prod (marked .deployed) are rollback targets.
$previous = "$(docker exec AI sh -c "ls -1d $Wheels/*/.deployed 2>/dev/null | sort | tail -n 1 | xargs -r dirname")".Trim()
if ($previous) { $previous += "/" }

# 2. verify in the test venv
Step "install into test venv"
$testPython = Ensure-Venv "test"
InContainer $testPython -m pip install --quiet -r "$Kernel/requirements/test.lock"
Install-Wheel $testPython $wheel
if (-not $SkipTests) {
    Step "pytest $TestPath (temporary databases)"
    InContainer sh -c "cd $Kernel && $testPython -m pytest $TestPath -q -p no:cacheprovider"
}
if ($TestOnly) {
    docker exec AI rm -rf "$Wheels/$stamp"
    Write-Output "TEST_ONLY_OK; version=$version (test venv only; wheel discarded)"
    return
}

# 3. promote the same wheel to prod
$prodPython = Ensure-Venv "prod"
$programs = @(Kernel-Programs)
Step "stop: $($programs -join ' ') (running turns drain first)"
SupervisorDo stop @programs
try {
    Step "install into prod venv"
    Install-Wheel $prodPython $wheel
    Step "migrate"
    InContainer $prodPython -m assistant_kernel.core.migrate
    Step "install supervisor config and start"
    InContainer install -m 0644 "$Kernel/deploy/supervisord.conf" $SupervisorConf
    SupervisorDo reread
    SupervisorDo update
    $programs = @(Kernel-Programs)
    SupervisorDo restart @programs
    if (-not (Wait-Ready $version)) { throw "health check failed for $version" }
}
catch {
    $failure = $_
    if (-not $previous) { throw "deploy failed and no previous wheel exists; services may be down: $failure" }
    $old = "$(docker exec AI sh -c "ls ${previous}*.whl")".Trim()
    $oldVersion = ($old -replace '.*assistant_kernel-([0-9.]+)-.*', '$1')
    Step "ROLLBACK to $old"
    SupervisorDo stop @programs
    Install-Wheel $prodPython $old
    SupervisorDo restart @programs
    if (-not (Wait-Ready $oldVersion)) { throw "rollback also failed; services need attention: $failure" }
    throw "deploy failed, rolled back to $oldVersion (migrations are not reverted): $failure"
}

# 4. mark this wheel as deployed; keep the two newest deployed wheels, drop everything else
docker exec AI touch "$Wheels/$stamp/.deployed"
docker exec AI sh -c "cd $Wheels && for d in */; do [ -f `$d.deployed ] || rm -rf `$d; done; ls -1d */ | sort | head -n -2 | xargs -r rm -rf"
Write-Output "ASSISTANT_KERNEL_DEPLOYED; version=$version; wheel=$wheel; previous=$previous"
