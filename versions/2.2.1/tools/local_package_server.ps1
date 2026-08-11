param(
    [ValidateSet('Start', 'Stop')][string]$Action,
    [string]$Root
)
$ErrorActionPreference = 'Stop'
$rootPath = [IO.Path]::GetFullPath($Root)
$pythonPath = Join-Path $rootPath 'runtime\python\python.exe'
$entryPath = Join-Path $rootPath 'tools\start_nya_208_isolated.py'
$logPath = Join-Path $rootPath 'logs'
$pidPath = Join-Path $rootPath 'local-server.pid'

if ($Action -eq 'Stop') {
    if (-not (Test-Path -LiteralPath $pidPath)) {
        Write-Host 'Local PID file was not found; the server may already be stopped.'
        exit 0
    }
    $serverPid = [int](Get-Content -LiteralPath $pidPath -Raw)
    $process = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
    if ($null -ne $process) {
        Stop-Process -Id $serverPid -Force
        Write-Host "Local server stopped (PID $serverPid)."
    } else {
        Write-Host 'The process referenced by the PID file no longer exists.'
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    exit 0
}

if (Test-Path -LiteralPath $pidPath) {
    $oldPid = [int](Get-Content -LiteralPath $pidPath -Raw)
    $oldProcess = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    if ($null -ne $oldProcess) {
        Write-Host "Local server is already running (PID $oldPid)."
        exit 0
    }
    Remove-Item -LiteralPath $pidPath -Force
}

$ports = @(19280, 19281, 19284, 19285, 19286)
$occupied = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in $ports } |
    Select-Object -ExpandProperty LocalPort -Unique | Sort-Object)
if ($occupied.Count -gt 0) {
    Write-Host "Startup failed; these ports are already in use: $($occupied -join ', ')" -ForegroundColor Red
    exit 2
}
if (-not (Test-Path -LiteralPath $pythonPath) -or -not (Test-Path -LiteralPath $entryPath)) {
    Write-Host 'Startup failed: the bundled Python runtime or server entry point is missing.' -ForegroundColor Red
    exit 3
}

New-Item -ItemType Directory -Path $logPath -Force | Out-Null
$stdoutPath = Join-Path $logPath 'local-server.out.log'
$stderrPath = Join-Path $logPath 'local-server.err.log'
# Some launch environments expose both PATH and Path. Start-Process treats
# those case-insensitively and otherwise aborts before Python is created.
$processEnvironment = [Environment]::GetEnvironmentVariables()
$pathValue = [string]$processEnvironment['Path']
if ([string]::IsNullOrWhiteSpace($pathValue)) {
    $pathValue = [string]$processEnvironment['PATH']
}
if (-not [string]::IsNullOrWhiteSpace($pathValue)) {
    [Environment]::SetEnvironmentVariable('PATH', $null, 'Process')
    [Environment]::SetEnvironmentVariable('Path', $pathValue, 'Process')
}
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-u', $entryPath) `
    -WorkingDirectory $rootPath -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath -PassThru -WindowStyle Hidden
Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ascii
Write-Host "Local server is starting (PID $($process.Id))."
Write-Host 'URL: http://127.0.0.1:19280/local-control.html'
Write-Host 'GM password: NyaLocal#208'
Write-Host 'Logs: logs\local-server.out.log / local-server.err.log'
