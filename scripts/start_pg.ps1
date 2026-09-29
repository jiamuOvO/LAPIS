param([int]$Port = 5433)

$project = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $project '.postgres-server\pgsql\bin'
$data = Join-Path $env:LOCALAPPDATA 'LAPIS\postgres-data'
if (-not (Test-Path -LiteralPath (Join-Path $bin 'pg_ctl.exe')) -or
    -not (Test-Path -LiteralPath (Join-Path $data 'PG_VERSION'))) {
    throw 'PostgreSQL 程序或数据目录不存在，请先按 README 初始化。'
}
& (Join-Path $bin 'pg_isready.exe') -h 127.0.0.1 -p $Port *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Output "PostgreSQL 端口 $Port 已在监听。"
    return
}
& (Join-Path $bin 'pg_ctl.exe') -D $data -l (Join-Path $data 'server.log') -o "-p $Port -h 127.0.0.1" start
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL 启动失败，检查 server.log。' }
