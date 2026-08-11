param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$ManifestUrl,
    [int]$LauncherPid = 0,
    [string]$LauncherPath = '',
    [switch]$NoUi,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$PinnedManifestUrl = 'http://47.95.121.98:19280/updates/nya-local/latest.json'
$PinnedReleasePrefix = 'http://47.95.121.98:19280/updates/nya-local/releases/'
$mutex = $null
$downloadRoot = $null
$rootStage = $null
$launcherStopped = $false
$progressPath = $null

function Write-ProgressState(
    [string]$Stage,
    [int]$Percent,
    [string]$Message,
    [int]$Current = 0,
    [int]$Total = 0
) {
    if ([string]::IsNullOrWhiteSpace($script:progressPath)) {
        return
    }
    try {
        $parent = Split-Path -Parent $script:progressPath
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        $payload = [ordered]@{
            stage = $Stage
            percent = [Math]::Max(0, [Math]::Min(100, $Percent))
            message = $Message
            current = $Current
            total = $Total
            updatedAt = [DateTimeOffset]::Now.ToString('o')
        } | ConvertTo-Json -Compress
        $temporary = $script:progressPath + '.tmp'
        [IO.File]::WriteAllText(
            $temporary,
            $payload,
            (New-Object Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $temporary -Destination $script:progressPath -Force
    } catch {
        # Progress reporting must never make an otherwise valid update fail.
    }
}

function Write-VersionState(
    [string]$ResolvedRoot,
    [string]$ReleaseId,
    [long]$VersionCode,
    [int]$FileCount
) {
    $payload = [ordered]@{
        releaseId = $ReleaseId
        versionCode = $VersionCode
        updatedAt = [DateTimeOffset]::Now.ToString('o')
        fileCount = $FileCount
    } | ConvertTo-Json
    [IO.File]::WriteAllText(
        (Join-Path $ResolvedRoot '.nya-version.json'),
        $payload,
        (New-Object Text.UTF8Encoding($false))
    )
}

function Get-VersionCode([object]$Source, [string]$ReleaseId) {
    $explicit = 0L
    if ($null -ne $Source.versionCode -and
        [long]::TryParse(([string]$Source.versionCode), [ref]$explicit) -and
        $explicit -gt 0) {
        return $explicit
    }
    if ($ReleaseId -match '(?i)(?:^|[-_.])v([0-9]+)$') {
        $legacy = 0L
        if ([long]::TryParse($Matches[1], [ref]$legacy) -and $legacy -gt 0) {
            return $legacy
        }
    }
    throw '版本清单缺少有效的数值版本号。'
}

function Read-InstalledVersionCode([string]$ResolvedRoot) {
    $statePath = Join-Path $ResolvedRoot '.nya-version.json'
    if (-not (Test-Path -LiteralPath $statePath)) {
        return 0L
    }
    try {
        $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        return Get-VersionCode $state ([string]$state.releaseId)
    } catch {
        return 0L
    }
}

function Show-Result([string]$Message, [bool]$ErrorResult = $false) {
    if ($NoUi) {
        Write-Output $Message
        return
    }
    Add-Type -AssemblyName PresentationFramework
    $icon = if ($ErrorResult) { 'Error' } else { 'Information' }
    [System.Windows.MessageBox]::Show(
        $Message,
        'Nya 本地端更新',
        'OK',
        $icon
    ) | Out-Null
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-SafeRelativePath([object]$RawPath) {
    $relative = ([string]$RawPath).Trim().Replace('\', '/')
    if ([string]::IsNullOrWhiteSpace($relative) -or [IO.Path]::IsPathRooted($relative)) {
        throw "更新清单包含无效路径: $relative"
    }
    $segments = @($relative.Split('/') | Where-Object { $_ -ne '' })
    if ($segments.Count -eq 0 -or $segments -contains '..' -or $segments -contains '.') {
        throw "更新清单包含越界路径: $relative"
    }
    if ($relative.Contains(':') -or $relative.StartsWith('.nya-update/', 'OrdinalIgnoreCase')) {
        throw "更新清单包含禁止路径: $relative"
    }

    $rootFiles = @(
        '00_一键启动本地端.bat',
        '01_启动服务器.bat',
        '02_打开Flash.bat',
        '03_打开Ruffle.bat',
        '04_打开GM后台.bat',
        '05_打开Web后台.bat',
        '06_手动检查更新.bat',
        '99_关闭本地端.bat',
        'Nya本地登录器.exe',
        'PACKAGE_MANIFEST.json',
        '使用说明.txt',
        '启动Nya登录器.bat'
    )
    $isRootFile = $rootFiles -contains $relative
    $isProgramTree = $relative.StartsWith('tools/', 'OrdinalIgnoreCase') -or
        $relative.StartsWith('www/', 'OrdinalIgnoreCase')
    if (-not $isRootFile -and -not $isProgramTree) {
        throw "更新清单试图修改受保护目录: $relative"
    }
    if ($relative.StartsWith('www/updates/', 'OrdinalIgnoreCase')) {
        throw "更新清单不能递归包含云端更新仓库: $relative"
    }
    return ($segments -join '/')
}

function Join-DownloadUrl([string]$BaseUrl, [string]$RelativePath) {
    $encoded = @($RelativePath.Split('/') | ForEach-Object {
        [Uri]::EscapeDataString($_)
    }) -join '/'
    return $BaseUrl.TrimEnd('/') + '/' + $encoded
}

function Stop-PackageProcesses([string]$ResolvedRoot) {
    $control = Join-Path $ResolvedRoot 'tools\local_package_server.ps1'
    if (Test-Path -LiteralPath $control) {
        try {
            & $control -Action Stop -Root $ResolvedRoot | Out-Null
        } catch {
            # Continue with the PID/process ownership checks below.
        }
    }

    $pidFile = Join-Path $ResolvedRoot 'local-server.pid'
    if (Test-Path -LiteralPath $pidFile) {
        try {
            $serverPid = [int](Get-Content -LiteralPath $pidFile -Raw)
            Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $serverPid -Timeout 8 -ErrorAction SilentlyContinue
        } catch {
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }

    foreach ($name in @('CefFlashBrowser', 'native_flash_host')) {
        foreach ($process in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
            try {
                $processPath = $process.Path
                if ($processPath -and $processPath.StartsWith($ResolvedRoot, 'OrdinalIgnoreCase')) {
                    Stop-Process -Id $process.Id -Force -ErrorAction Stop
                    Wait-Process -Id $process.Id -Timeout 8 -ErrorAction SilentlyContinue
                }
            } catch {
            }
        }
    }
}

function Start-LauncherIfAvailable {
    if (-not [string]::IsNullOrWhiteSpace($LauncherPath) -and
        (Test-Path -LiteralPath $LauncherPath)) {
        Start-Process -FilePath $LauncherPath -WorkingDirectory (Split-Path -Parent $LauncherPath)
    }
}

try {
    if ($ManifestUrl -ne $PinnedManifestUrl) {
        throw '更新地址不受信任，已拒绝联网。'
    }
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if (-not (Test-Path -LiteralPath (Join-Path $resolvedRoot 'tools\start_nya_208_isolated.py'))) {
        throw '无法确认 Nya 本地端根目录，未执行更新。'
    }
    $mutex = New-Object Threading.Mutex($false, 'Global\NyaLocalPackageUpdateV1')
    if (-not $mutex.WaitOne(0)) {
        Show-Result '已有更新任务正在运行。'
        exit 3
    }

    try {
        $manifest = Invoke-RestMethod -UseBasicParsing -Uri $ManifestUrl -TimeoutSec 8
    } catch {
        Show-Result '阿里云更新服务当前不可用，本地文件和正在运行的服务均未改动。' $true
        exit 10
    }

    $script:progressPath = Join-Path $resolvedRoot '.nya-update\progress.json'
    Write-ProgressState 'checking' 1 '已取得发布清单，正在验证版本信息。'

    if ([int]$manifest.schemaVersion -ne 1 -or $manifest.published -ne $true) {
        Write-ProgressState 'unavailable' 100 '当前没有已标记的同步版本。'
        Show-Result '云端没有标记可同步的发布版本，本地文件未改动。'
        exit 0
    }
    $releaseId = ([string]$manifest.releaseId).Trim()
    if ($releaseId -notmatch '^[0-9A-Za-z._-]{1,80}$') {
        throw '云端发布版本号无效。'
    }
    $versionCode = Get-VersionCode $manifest $releaseId
    $installedVersionCode = Read-InstalledVersionCode $resolvedRoot
    if ($installedVersionCode -gt $versionCode) {
        Write-ProgressState 'completed' 100 "本地 v$installedVersionCode 高于云端 v$versionCode，已阻止降级。"
        Show-Result "本地版本 v$installedVersionCode 高于云端版本 v$versionCode，未执行降级更新。"
        exit 0
    }
    $baseUrl = ([string]$manifest.baseUrl).Trim()
    if (-not $baseUrl.StartsWith($PinnedReleasePrefix, 'OrdinalIgnoreCase')) {
        throw '云端文件地址不受信任。'
    }
    $files = @($manifest.files)
    if ($files.Count -eq 0 -or $files.Count -gt 12000) {
        throw '云端更新文件数量无效。'
    }

    $downloadRoot = Join-Path ([IO.Path]::GetTempPath()) ("NyaUpdate-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    $changes = New-Object Collections.Generic.List[object]
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $processedFiles = 0
    Write-ProgressState 'downloading' 2 '正在检查本地文件并下载差异。' 0 $files.Count

    foreach ($row in $files) {
        $relative = Get-SafeRelativePath $row.path
        if (-not $seen.Add($relative)) {
            throw "云端更新清单包含重复路径: $relative"
        }
        $expectedHash = ([string]$row.sha256).ToLowerInvariant()
        $expectedSize = [long]$row.size
        if ($expectedHash -notmatch '^[0-9a-f]{64}$' -or $expectedSize -lt 0 -or
            $expectedSize -gt 2147483648) {
            throw "云端更新文件校验信息无效: $relative"
        }
        $target = Join-Path $resolvedRoot ($relative.Replace('/', '\'))
        $targetFull = [IO.Path]::GetFullPath($target)
        if (-not $targetFull.StartsWith($resolvedRoot + '\', 'OrdinalIgnoreCase')) {
            throw "更新目标越界: $relative"
        }
        if ((Test-Path -LiteralPath $targetFull) -and
            (Get-Item -LiteralPath $targetFull).Length -eq $expectedSize -and
            (Get-Sha256 $targetFull) -eq $expectedHash) {
            $processedFiles += 1
            $scanPercent = 2 + [int](78 * $processedFiles / $files.Count)
            Write-ProgressState 'downloading' $scanPercent '正在检查本地文件并下载差异。' $processedFiles $files.Count
            continue
        }

        $download = Join-Path $downloadRoot ($relative.Replace('/', '\'))
        New-Item -ItemType Directory -Path (Split-Path -Parent $download) -Force | Out-Null
        $url = Join-DownloadUrl $baseUrl $relative
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $download -TimeoutSec 180
        } catch {
            throw "下载失败，未修改任何本地文件: $relative"
        }
        if ((Get-Item -LiteralPath $download).Length -ne $expectedSize -or
            (Get-Sha256 $download) -ne $expectedHash) {
            throw "下载文件校验失败，未修改任何本地文件: $relative"
        }
        $changes.Add([pscustomobject]@{
            Relative = $relative
            Target = $targetFull
            Download = $download
        })
        $processedFiles += 1
        $scanPercent = 2 + [int](78 * $processedFiles / $files.Count)
        Write-ProgressState 'downloading' $scanPercent "已下载并校验：$relative" $processedFiles $files.Count
    }

    if ($changes.Count -eq 0) {
        Write-VersionState $resolvedRoot $releaseId $versionCode 0
        Write-ProgressState 'completed' 100 '当前已经是最新版本。' $files.Count $files.Count
        Show-Result "当前已经是已标记的同步版本：$releaseId"
        exit 0
    }
    if ($DryRun) {
        Write-ProgressState 'completed' 100 "更新预检通过，共 $($changes.Count) 个文件需要同步。" $files.Count $files.Count
        Show-Result "更新预检通过：$releaseId，共 $($changes.Count) 个文件需要同步。"
        exit 0
    }

    # Move verified downloads onto the package volume before stopping anything.
    $updateRoot = Join-Path $resolvedRoot '.nya-update'
    $rootStage = Join-Path $updateRoot ("staging-" + [Guid]::NewGuid().ToString('N'))
    $backupRoot = Join-Path $updateRoot ("backup-" + $releaseId)
    New-Item -ItemType Directory -Path $rootStage -Force | Out-Null
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    foreach ($change in $changes) {
        $staged = Join-Path $rootStage ($change.Relative.Replace('/', '\'))
        New-Item -ItemType Directory -Path (Split-Path -Parent $staged) -Force | Out-Null
        Copy-Item -LiteralPath $change.Download -Destination $staged -Force
        $change | Add-Member -NotePropertyName Staged -NotePropertyValue $staged
    }

    Write-ProgressState 'applying' 90 '下载校验完成，正在应用更新。' 0 $changes.Count
    Stop-PackageProcesses $resolvedRoot
    if ($LauncherPid -gt 0) {
        Stop-Process -Id $LauncherPid -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $LauncherPid -Timeout 10 -ErrorAction SilentlyContinue
        $launcherStopped = $true
    } elseif (-not [string]::IsNullOrWhiteSpace($LauncherPath)) {
        foreach ($process in @(Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($LauncherPath)) -ErrorAction SilentlyContinue)) {
            try {
                if ($process.Path -and
                    [IO.Path]::GetFullPath($process.Path) -eq [IO.Path]::GetFullPath($LauncherPath)) {
                    Stop-Process -Id $process.Id -Force -ErrorAction Stop
                    Wait-Process -Id $process.Id -Timeout 10 -ErrorAction SilentlyContinue
                    $launcherStopped = $true
                }
            } catch {
            }
        }
    }

    $applied = New-Object Collections.Generic.List[object]
    try {
        $appliedCount = 0
        foreach ($change in $changes) {
            $targetDirectory = Split-Path -Parent $change.Target
            New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
            $backup = Join-Path $backupRoot ($change.Relative.Replace('/', '\'))
            $existed = Test-Path -LiteralPath $change.Target
            if ($existed) {
                New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
                Move-Item -LiteralPath $change.Target -Destination $backup -Force
            }
            Move-Item -LiteralPath $change.Staged -Destination $change.Target -Force
            $applied.Add([pscustomobject]@{
                Target = $change.Target
                Backup = $backup
                Existed = $existed
            })
            $appliedCount += 1
            $applyPercent = 90 + [int](9 * $appliedCount / $changes.Count)
            Write-ProgressState 'applying' $applyPercent "正在替换：$($change.Relative)" $appliedCount $changes.Count
        }
    } catch {
        $rollback = @($applied)
        [Array]::Reverse($rollback)
        foreach ($entry in $rollback) {
            Remove-Item -LiteralPath $entry.Target -Force -ErrorAction SilentlyContinue
            if ($entry.Existed -and (Test-Path -LiteralPath $entry.Backup)) {
                New-Item -ItemType Directory -Path (Split-Path -Parent $entry.Target) -Force | Out-Null
                Move-Item -LiteralPath $entry.Backup -Destination $entry.Target -Force
            }
        }
        throw "应用更新失败，已回滚原文件：$($_.Exception.Message)"
    }

    Write-VersionState $resolvedRoot $releaseId $versionCode $changes.Count
    Remove-Item -LiteralPath $rootStage -Recurse -Force -ErrorAction SilentlyContinue
    Write-ProgressState 'completed' 100 '更新完成，当前已是最新版本。' $changes.Count $changes.Count
    Show-Result "已同步明确发布的版本：$releaseId`n更新文件：$($changes.Count) 个`n存档未参与更新。"
    Start-LauncherIfAvailable
    exit 0
} catch {
    Write-ProgressState 'failed' 0 $_.Exception.Message
    Show-Result $_.Exception.Message $true
    if ($launcherStopped) {
        Start-LauncherIfAvailable
    }
    exit 20
} finally {
    if ($downloadRoot -and (Test-Path -LiteralPath $downloadRoot)) {
        Remove-Item -LiteralPath $downloadRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($rootStage -and (Test-Path -LiteralPath $rootStage)) {
        Remove-Item -LiteralPath $rootStage -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($mutex) {
        try { $mutex.ReleaseMutex() } catch {}
        $mutex.Dispose()
    }
}
