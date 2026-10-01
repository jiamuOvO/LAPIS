param([int]$Port = 5433)

$project = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $project '.postgres-server\pgsql\bin'
$data = Join-Path $env:LOCALAPPDATA 'LAPIS\postgres-data'
if (-not (Test-Path -LiteralPath (Join-Path $bin 'pg_ctl.exe')) -or
    -not (Test-Path -LiteralPath (Join-Path $data 'PG_VERSION'))) {
    throw 'PostgreSQL binaries or data directory are missing. See README.'
}
& (Join-Path $bin 'pg_isready.exe') -h 127.0.0.1 -p $Port *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Output "PostgreSQL port $Port is already listening."
    return
}
& (Join-Path $bin 'pg_ctl.exe') -D $data -l (Join-Path $data 'server.log') -o "-p $Port -h 127.0.0.1" start
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL failed to start. Check server.log.' }
