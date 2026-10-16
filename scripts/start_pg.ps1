param([ValidateRange(1,65535)][int]$Port = 5433)

$ErrorActionPreference = 'Stop'

$project = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $project '.postgres-server\pgsql\bin'
$data = Join-Path $env:LOCALAPPDATA 'LAPIS\postgres-data'
$control = Join-Path $bin 'pg_ctl.exe'
$ready = Join-Path $bin 'pg_isready.exe'
$log = Join-Path (Split-Path -Parent $data) 'postgres-server.log'
foreach ($requiredFile in @($control, $ready, (Join-Path $data 'PG_VERSION'))) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "PostgreSQL file is missing or inaccessible: $requiredFile. See README; do not reinitialize existing data."
    }
}
& $ready -h 127.0.0.1 -p $Port -t 2 -q
if ($LASTEXITCODE -eq 2) {
    & $control -D $data status *> $null
    if ($LASTEXITCODE -ne 0) {
        # Keep the active log outside PGDATA to avoid recovery-time file sharing violations.
        & $control -D $data -l $log -o "-p $Port -h 127.0.0.1" -w -t 60 start
        if ($LASTEXITCODE -ne 0) { throw "PostgreSQL failed to start. Check $log." }
    }
}
$deadline = (Get-Date).AddSeconds(60)
do {
    & $ready -h 127.0.0.1 -p $Port -t 2 -q
    if ($LASTEXITCODE -eq 0) {
        Write-Output "PostgreSQL is ready at 127.0.0.1:$Port."
        return
    }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)
throw "PostgreSQL is not ready at port $Port. Check $log and $data\server.log."
