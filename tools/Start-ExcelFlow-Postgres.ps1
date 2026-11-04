[CmdletBinding()]
param([switch]$NoBrowser)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$dataRoot = Join-Path $projectRoot 'var'
$logRoot = Join-Path $dataRoot 'logs'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$vite = Join-Path $projectRoot 'apps\web\node_modules\vite\bin\vite.js'
$infra = Join-Path $projectRoot 'deploy\docker-compose.local-infra.yaml'
$secrets = Join-Path $dataRoot 'local-infra.env'
$statePath = Join-Path $dataRoot 'local-launcher.json'
$pageUrl = 'http://127.0.0.1:5173/'
$mutex = $null
$ownsMutex = $false

function Get-Listener([int]$Port) {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Get-ProjectProcess([string]$Pattern) {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('python.exe', 'node.exe') -and $_.CommandLine -and
            $_.CommandLine.IndexOf($projectRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $_.CommandLine -match $Pattern } | Select-Object -First 1
}

function Start-HiddenService([string]$Executable, [string[]]$Arguments, [string]$Directory, [string]$Name) {
    $argumentLine = ($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    Start-Process -FilePath $Executable -ArgumentList $argumentLine -WorkingDirectory $Directory `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot "$Name-$stamp.stdout.log") `
        -RedirectStandardError (Join-Path $logRoot "$Name-$stamp.stderr.log")
}

function Wait-Http([string]$Url, [string]$Name, [int]$ProcessId = 0) {
    for ($i = 0; $i -lt 60; $i++) {
        try {
            if ((Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { return }
        } catch { }
        if ($ProcessId -and -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            throw "$Name 提前退出，请查看 $logRoot。"
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name 未能就绪，请查看 $logRoot。"
}

function Get-SavedState {
    if (-not (Test-Path -LiteralPath $statePath)) { return $null }
    try { return Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { return $null }
}

function Assert-PostgresData {
    & $python (Join-Path $projectRoot 'tools\check_local_postgres.py') $dataRoot
    if ($LASTEXITCODE -ne 0) { throw '数据库或迁移数据检查失败；不会退回 SQLite。' }
}

try {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $key = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant()))).Replace('-', '').Substring(0, 16) }
    finally { $sha.Dispose() }
    $mutex = [Threading.Mutex]::new($false, "Local\ExcelFlow-$key")
    try { $ownsMutex = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { Write-Host '另一个启动流程正在运行，请稍候。'; exit 0 }

    foreach ($path in @($python, $vite, $infra, $secrets)) {
        if (-not (Test-Path -LiteralPath $path)) { throw "缺少启动依赖：$path" }
    }
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($nodeCommand) { $node = $nodeCommand.Source }
    else {
        $node = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
        if (-not (Test-Path -LiteralPath $node)) { throw '未找到 Node.js。' }
    }
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $secretLine = Get-Content -LiteralPath $secrets -Encoding UTF8 | Where-Object { $_ -match '^EXCELFLOW_LOCAL_PG_PASSWORD=' } | Select-Object -First 1
    if (-not $secretLine) { throw '本地 PostgreSQL 凭据文件无效。' }
    $password = $secretLine.Substring('EXCELFLOW_LOCAL_PG_PASSWORD='.Length)
    if ($password -notmatch '^[0-9a-f]{48}$') { throw '本地 PostgreSQL 凭据文件格式无效。' }

    $env:EXCEL_AUDITOR_ENV = 'development'
    $env:EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP = '1'
    $env:EXCEL_AUDITOR_ALLOW_INSECURE_PLATFORM_HTTP = '1'
    $env:EXCEL_AUDITOR_ALLOW_PRIVATE_PLATFORM = '1'
    $env:EXCEL_AUDITOR_DATA = $dataRoot
    $env:EXCEL_AUDITOR_RECOVERY_INTERVAL = '1'
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:VITE_API_TARGET = 'http://127.0.0.1:8000'
    $env:DATABASE_URL = "postgresql+psycopg://excel_auditor:$password@127.0.0.1:55432/excel_auditor"
    $env:REDIS_URL = 'redis://127.0.0.1:56379/0'
    $platformConnections = Join-Path $dataRoot 'connections.json'
    $platformSecrets = Join-Path $dataRoot 'secrets'
    if (Test-Path -LiteralPath $platformConnections) { $env:EXCEL_AUDITOR_CONNECTIONS = $platformConnections }
    if (Test-Path -LiteralPath $platformSecrets -PathType Container) { $env:EXCEL_AUDITOR_SECRET_DIR = $platformSecrets }

    $docker = Get-Command docker.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $docker) { throw '未安装 Docker Desktop，无法启动本地 PostgreSQL 和 Redis。' }
    & $docker.Source info --format '{{.ServerVersion}}' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $desktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
        if (-not (Test-Path -LiteralPath $desktop)) { throw 'Docker 引擎未运行，请启动 Docker Desktop。' }
        Start-Process -FilePath $desktop -WindowStyle Hidden | Out-Null
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 2
            & $docker.Source info --format '{{.ServerVersion}}' 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { break }
        }
        if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop 未能启动。' }
    }
    & $docker.Source compose --env-file $secrets -f $infra up -d --wait --quiet-pull
    if ($LASTEXITCODE -ne 0) { throw '本地 PostgreSQL 或 Redis 启动失败。' }
    & $python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL 数据库迁移失败。' }
    Assert-PostgresData

    $apiListener = Get-Listener 8000
    $webListener = Get-Listener 5173
    $saved = Get-SavedState
    if ($apiListener) {
        $startedAt = (Get-Process -Id $apiListener.OwningProcess).StartTime.ToUniversalTime().ToString('o')
        if (-not $saved -or $saved.backend -ne 'postgres-rq' -or $saved.api_pid -ne $apiListener.OwningProcess -or
            $saved.api_started_at -ne $startedAt) {
            throw '8000 端口已有其他后端；为避免连接错数据库，已停止启动。'
        }
    }
    if ($webListener -and -not (Get-ProjectProcess 'vite[\\/]bin[\\/]vite\.js')) {
        throw '5173 端口被其他程序占用。'
    }
    if (-not $apiListener) {
        $api = Start-HiddenService $python @('-m','uvicorn','excel_auditor.api:app','--app-dir',(Join-Path $projectRoot 'src'),'--host','127.0.0.1','--port','8000') $projectRoot 'api-postgres'
        Wait-Http 'http://127.0.0.1:8000/health/live' 'PostgreSQL 版 API' $api.Id
        $apiListener = Get-Listener 8000
        @{ backend = 'postgres-rq'; project_root = $projectRoot; data_root = $dataRoot;
           api_pid = $apiListener.OwningProcess;
           api_started_at = (Get-Process -Id $apiListener.OwningProcess).StartTime.ToUniversalTime().ToString('o') } |
            ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    }
    $recovery = Get-ProjectProcess 'excel_auditor\.workflow\.execution'
    if (-not $recovery) {
        $recovery = Start-HiddenService $python @('-m','excel_auditor.workflow.execution','--data-root',$dataRoot) $projectRoot 'workflow-recovery'
    }
    foreach ($queue in @('workflow','workflow-export')) {
        $pattern = 'rq\.cli.*' + [regex]::Escape($queue) + '"?\s*$'
        $worker = Get-ProjectProcess $pattern
        if (-not $worker) {
            $workerName = "excelflow-local-$queue-$((Get-Date).ToString('yyyyMMddHHmmssfff'))"
            $worker = Start-HiddenService $python @('-c','from rq.cli import main; main()','worker','--url',$env:REDIS_URL,
                '--worker-class','rq.worker.SimpleWorker','--name',$workerName,$queue) $projectRoot "rq-$queue"
            $workerId = $worker.Id
            $registered = $false
            for ($i = 0; $i -lt 40; $i++) {
                if (-not (Get-Process -Id $workerId -ErrorAction SilentlyContinue)) { break }
                & $python (Join-Path $projectRoot 'tools\check_local_rq.py') $workerName $queue
                if ($LASTEXITCODE -eq 0) { $registered = $true; break }
                Start-Sleep -Milliseconds 250
            }
            if (-not $registered) { throw "$queue Worker 未能注册，请查看 $logRoot。" }
        }
        $workerId = if ($worker.PSObject.Properties['ProcessId']) { $worker.ProcessId } else { $worker.Id }
        if (-not (Get-Process -Id $workerId -ErrorAction SilentlyContinue)) {
            throw "$queue Worker 提前退出，请查看 $logRoot。"
        }
    }
    if (-not $webListener) {
        $web = Start-HiddenService $node @($vite,'--host','127.0.0.1','--port','5173','--strictPort') (Join-Path $projectRoot 'apps\web') 'web'
        Wait-Http $pageUrl '前端' $web.Id
    }
    Wait-Http 'http://127.0.0.1:5173/health/live' '前后端连接'
    $apiListener = Get-Listener 8000
    $webListener = Get-Listener 5173
    @{ backend = 'postgres-rq'; project_root = $projectRoot; data_root = $dataRoot;
       api_pid = $apiListener.OwningProcess; web_pid = $webListener.OwningProcess;
       api_started_at = (Get-Process -Id $apiListener.OwningProcess).StartTime.ToUniversalTime().ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    Write-Host "启动完成：$pageUrl（PostgreSQL + Redis/RQ）"
    if (-not $NoBrowser) { Start-Process -FilePath $pageUrl }
} catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
