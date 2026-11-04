[CmdletBinding()]
param([switch]$NoBrowser)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$dataRoot = Join-Path $projectRoot 'var'
$logRoot = Join-Path $dataRoot 'logs'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$vite = Join-Path $projectRoot 'apps\web\node_modules\vite\bin\vite.js'
$webRoot = Join-Path $projectRoot 'apps\web'
$pageUrl = 'http://127.0.0.1:5173/'
$mutex = $null
$ownsMutex = $false

function Get-Listener([int]$Port) {
    return Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Test-ProjectProcess([int]$ProcessId, [string]$Kind) {
    $current = $ProcessId
    for ($depth = 0; $depth -lt 5 -and $current -gt 0; $depth++) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$current" -ErrorAction SilentlyContinue
        if ($null -eq $process) { return $false }
        $command = [string]$process.CommandLine
        if ($command.IndexOf($projectRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            if ($Kind -eq 'api' -and $command -match 'uvicorn"?\s+"?excel_auditor\.api:app') { return $true }
            if ($Kind -eq 'web' -and $command -match 'vite[\\/]bin[\\/]vite\.js') { return $true }
            if ($Kind -eq 'worker' -and $command -match 'excel_auditor\.workflow\.execution') { return $true }
        }
        $current = [int]$process.ParentProcessId
    }
    return $false
}

function Get-WorkflowWorker {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^python(?:w)?\.exe$' -and $_.CommandLine -match 'excel_auditor\.workflow\.execution' -and
            $_.CommandLine.IndexOf($dataRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0 } |
        Where-Object { Test-ProjectProcess $_.ProcessId 'worker' } |
        Select-Object -First 1
}

function Test-ExternalWorkflowConfigured {
    $statePath = Join-Path $dataRoot 'local-launcher.json'
    if (-not (Test-Path -LiteralPath $statePath)) { return $false }
    try {
        $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        return $state.workflow_external -eq $true
    } catch { return $false }
}

function Get-HttpCompatibility {
    try {
        $settings = Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/workflow-settings' -TimeoutSec 3
        return $settings.ai.http_allowed -eq $true
    } catch {
        # A script-started authenticated server can be reused without reading credentials.
        $statePath = Join-Path $dataRoot 'local-launcher.json'
        if (Test-Path -LiteralPath $statePath) {
            try {
                $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
                $listener = Get-Listener 8000
                if ($null -ne $listener -and $state.api_pid -eq $listener.OwningProcess) {
                    $process = Get-Process -Id $listener.OwningProcess -ErrorAction Stop
                    return $state.api_started_at -eq $process.StartTime.ToUniversalTime().ToString('o')
                }
            } catch { return $false }
        }
        return $false
    }
}

function Assert-NoActiveTasks {
    $database = Join-Path $dataRoot 'workflow\workflow.sqlite3'
    if (Test-Path -LiteralPath $database) {
        $code = 'import pathlib,sqlite3,sys; con=sqlite3.connect(pathlib.Path(sys.argv[1]).as_uri()+"?mode=ro",uri=True); print(con.execute("SELECT COUNT(*) FROM workflow_objects WHERE status IN (''processing'',''running'',''queued'')").fetchone()[0]); con.close()'
        $count = $code | & $python '-' $database
        if ($LASTEXITCODE -ne 0) { throw '无法确认后台任务状态，已保留现有服务，请查看控制台错误。' }
        if ([int]$count -gt 0) { throw '后端存在处理中的任务。请等待完成后再次双击启动，当前任务未中断。' }
    }
}

function Wait-Service([string]$Url, [string]$Name, [int]$ProcessId = 0) {
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return }
        } catch { }
        if ($ProcessId -gt 0 -and $null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            throw "$Name 提前退出，请检查日志：$logRoot"
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name 未能就绪，请检查日志：$logRoot"
}

function Start-HiddenService([string]$Executable, [string[]]$Arguments, [string]$Directory, [string]$Name) {
    # Start-Process joins argument arrays, so explicitly quote each path for Windows.
    $argumentLine = ($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    return Start-Process -FilePath $Executable -ArgumentList $argumentLine -WorkingDirectory $Directory `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot "$Name-$stamp.stdout.log") `
        -RedirectStandardError (Join-Path $logRoot "$Name-$stamp.stderr.log")
}

try {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $key = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant())))).Replace('-', '').Substring(0, 16) }
    finally { $sha.Dispose() }
    $mutex = New-Object Threading.Mutex($false, "Local\ExcelFlow-$key")
    try { $ownsMutex = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { Write-Host '另一个启动流程正在运行，请稍候。'; exit 0 }

    if (-not (Test-Path -LiteralPath $python)) { throw '缺少项目虚拟环境 .venv，请先安装项目 Python 依赖。' }
    if (-not (Test-Path -LiteralPath $vite)) { throw '缺少前端依赖，请先执行 pnpm --dir apps/web install --frozen-lockfile。' }
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $nodeCommand) { $node = $nodeCommand.Source }
    else {
        $node = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
        if (-not (Test-Path -LiteralPath $node)) { throw '未找到 Node.js，请先安装兼容项目的 Node.js。' }
    }
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

    # Fixed local-development settings. Model keys stay in the existing data directory.
    $env:EXCEL_AUDITOR_ENV = 'development'
    $env:EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP = '1'
    $env:EXCEL_AUDITOR_ALLOW_INSECURE_PLATFORM_HTTP = '1'
    $env:EXCEL_AUDITOR_ALLOW_PRIVATE_PLATFORM = '1'
    $env:EXCEL_AUDITOR_DATA = $dataRoot
    $env:EXCEL_AUDITOR_RECOVERY_INTERVAL = '1'
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:VITE_API_TARGET = 'http://127.0.0.1:8000'
    $platformConnections = Join-Path $dataRoot 'connections.json'
    $platformSecrets = Join-Path $dataRoot 'secrets'
    if (Test-Path -LiteralPath $platformConnections) { $env:EXCEL_AUDITOR_CONNECTIONS = $platformConnections }
    if (Test-Path -LiteralPath $platformSecrets -PathType Container) { $env:EXCEL_AUDITOR_SECRET_DIR = $platformSecrets }

    Write-Host "数据目录：$dataRoot"
    Write-Host 'HTTP 模型开发兼容：每次启动自动开启'

    # Check both ports before starting anything; never terminate unrelated applications.
    $apiListener = Get-Listener 8000
    $webListener = Get-Listener 5173
    if ($null -ne $apiListener -and -not (Test-ProjectProcess $apiListener.OwningProcess 'api')) {
        throw '8000 端口被其他程序占用，请先关闭该程序。'
    }
    if ($null -ne $webListener -and -not (Test-ProjectProcess $webListener.OwningProcess 'web')) {
        throw '5173 端口被其他程序占用，请先关闭该程序。'
    }

    if ($null -ne $apiListener -and (-not (Get-HttpCompatibility) -or -not (Test-ExternalWorkflowConfigured))) {
        Assert-NoActiveTasks
        Write-Host '正在重新启动本项目后端，使独立 Worker 工作流生效…'
        Stop-Process -Id $apiListener.OwningProcess -ErrorAction Stop
        for ($i = 0; $i -lt 20 -and $null -ne (Get-Listener 8000); $i++) { Start-Sleep -Milliseconds 250 }
        if ($null -ne (Get-Listener 8000)) { throw '后端端口尚未释放，请稍后重试。' }
        $apiListener = $null
    }
    if ($null -eq $apiListener) {
        $apiProcess = Start-HiddenService $python @('-m', 'uvicorn', 'excel_auditor.api:app', '--app-dir', (Join-Path $projectRoot 'src'), '--host', '127.0.0.1', '--port', '8000') $projectRoot 'api'
        Wait-Service 'http://127.0.0.1:8000/health/live' '后端' $apiProcess.Id
        Write-Host '后端已启动。'
    } else {
        Wait-Service 'http://127.0.0.1:8000/health/live' '后端'
        Write-Host '后端已运行，继续使用现有服务。'
    }
    $workflowWorker = Get-WorkflowWorker
    if ($null -eq $workflowWorker) {
        $workerStarted = Get-Date
        $workerProcess = Start-HiddenService $python @('-m', 'excel_auditor.workflow.execution', '--data-root', $dataRoot) $projectRoot 'workflow-worker'
        $heartbeat = Join-Path $dataRoot 'workflow\recovery.heartbeat'
        $workerReady = $false
        for ($i = 0; $i -lt 40; $i++) {
            if ($null -eq (Get-Process -Id $workerProcess.Id -ErrorAction SilentlyContinue)) {
                throw "独立 Worker 提前退出，请检查日志：$logRoot"
            }
            if ((Test-Path -LiteralPath $heartbeat) -and
                (Get-Item -LiteralPath $heartbeat).LastWriteTimeUtc -ge $workerStarted.ToUniversalTime()) {
                $workerReady = $true
                break
            }
            Start-Sleep -Milliseconds 250
        }
        if (-not $workerReady) { throw "独立 Worker 未能就绪，请检查日志：$logRoot" }
        Write-Host '独立 Worker 已启动。'
    } else {
        $workerProcess = $workflowWorker
        Write-Host '独立 Worker 已运行。'
    }
    $workerProcess = Get-WorkflowWorker
    if ($null -eq $workerProcess) { throw '无法确认独立 Worker 进程。' }
    if ($null -eq $webListener) {
        $webProcess = Start-HiddenService $node @($vite, '--host', '127.0.0.1', '--port', '5173', '--strictPort') $webRoot 'web'
        Wait-Service $pageUrl '前端' $webProcess.Id
        Write-Host '前端已启动。'
    } else {
        Wait-Service $pageUrl '前端'
        Write-Host '前端已运行，继续使用现有服务。'
    }
    Wait-Service 'http://127.0.0.1:5173/health/live' '前后端连接'
    $apiListener = Get-Listener 8000
    $webListener = Get-Listener 5173
    $apiProcess = Get-Process -Id $apiListener.OwningProcess
    @{ project_root = $projectRoot; data_root = $dataRoot; http_compatible = $true; workflow_external = $true;
       api_pid = $apiListener.OwningProcess; web_pid = $webListener.OwningProcess;
       worker_pid = $workerProcess.ProcessId;
       api_started_at = $apiProcess.StartTime.ToUniversalTime().ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dataRoot 'local-launcher.json') -Encoding UTF8
    Write-Host "启动完成：$pageUrl"
    if (-not $NoBrowser) { Start-Process -FilePath $pageUrl }
} catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
