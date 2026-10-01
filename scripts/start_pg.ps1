param([int]$Port = 5433)

$project = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $project '.postgres-server\pgsql\bin'
$data = Join-Path $env:LOCALAPPDATA 'LAPIS\postgres-data'
$client = $null
try {
    $client = [System.Net.Sockets.TcpClient]::new('127.0.0.1', $Port)
    Write-Output "PostgreSQL port $Port is already listening."
    return
} catch [System.Net.Sockets.SocketException] {
    # No local listener; start the bundled server below.
} finally {
    if ($client) { $client.Dispose() }
}
if (-not (Test-Path -LiteralPath (Join-Path $bin 'pg_ctl.exe')) -or
    -not (Test-Path -LiteralPath (Join-Path $data 'PG_VERSION'))) {
    throw "PostgreSQL binaries or data directory are missing: $bin ; $data"
}
& (Join-Path $bin 'pg_ctl.exe') -D $data -l (Join-Path $data 'server.log') -o "-p $Port -h 127.0.0.1" start
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL failed to start. Check server.log.' }
