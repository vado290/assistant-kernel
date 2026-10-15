<#
Rebuilt-core test environment (phase 3). Production services are not touched.

  .\scripts\core_test_env.ps1 init     # install branch code into the test venv, create/migrate assistant_core_test,
                                       # register the two test programs with supervisord (not started)
  .\scripts\core_test_env.ps1 start    # start core-test-dispatcher and core-test-gateway
  .\scripts\core_test_env.ps1 stop
  .\scripts\core_test_env.ps1 status
#>
param([Parameter(Mandatory)][ValidateSet("init", "start", "stop", "status")][string]$Action)

$ErrorActionPreference = "Stop"
$Conf = "/etc/supervisor/conf.d/assistant.conf"
$Programs = "/home/v/AI/assistant-data/runtime/kernel-services/programs"
$TestPython = "/home/v/AI/assistant-data/runtime/kernel-venvs/test/bin/python"
function Supervisor { docker exec AI supervisorctl -c $Conf @args }

switch ($Action) {
    "init" {
        & (Join-Path $PSScriptRoot "deploy.ps1") -TestOnly
        if (-not $?) { throw "test venv install failed" }
        docker exec AI /usr/bin/env -u PYTHONPATH POSTGRES_DB=assistant_core_test $TestPython -c `
            "import psycopg; from psycopg import sql; from assistant_kernel.config import KernelSettings; from assistant_kernel.core import migrate; s=KernelSettings.from_environment(); a={**migrate.connection_args(s), 'dbname':'rag'}; c=psycopg.connect(**a, autocommit=True); exists=c.execute('SELECT 1 FROM pg_database WHERE datname=%s',('assistant_core_test',)).fetchone(); exists or c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier('assistant_core_test'))); print('MIGRATED', migrate.apply(migrate.connection_args(s)))"
        if ($LASTEXITCODE -ne 0) { throw "test database setup failed" }
        docker exec AI mkdir -p $Programs
        docker exec AI cp /home/v/AI/assistant-kernel/deploy/core-test.supervisord.conf "$Programs/core-test.conf"
        Supervisor reread | Out-Host
        Supervisor update | Out-Host
        Write-Output "CORE_TEST_ENV_READY (programs registered, not started)"
    }
    "start" { Supervisor start core-test-dispatcher core-test-gateway | Out-Host }
    "stop" { Supervisor stop core-test-gateway core-test-dispatcher | Out-Host }
    "status" { Supervisor status core-test-dispatcher core-test-gateway | Out-Host }
}
