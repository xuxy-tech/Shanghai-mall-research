param(
    [Parameter(Mandatory = $true)]
    [string]$Video,

    [Parameter(Mandatory = $true)]
    [string]$MallName,

    [string]$OutputDir,
    [double]$MaxSeconds = 0,
    [double]$KeyframeInterval = 0.5,
    [double]$MinFrameDifference = 1.0,
    [string]$ReuseRawOcrDir,
    [ValidateSet('PP-OCRv5_mobile_rec', 'PP-OCRv5_server_rec')]
    [string]$RecognitionModel = 'PP-OCRv5_mobile_rec',
    [switch]$SaveDebug
)

$ErrorActionPreference = 'Stop'
$scriptRoot = $PSScriptRoot
$pipelineRoot = Split-Path -Parent $scriptRoot
$workspaceRoot = Split-Path -Parent $pipelineRoot
$condaPath = (Get-Command conda -ErrorAction Stop).Source
$condaPython = @('run', '--no-capture-output', '-n', 'mall-analysis', 'python')
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $condaPath @condaPython -c "import paddle,sys; print('GPU' if paddle.device.cuda.device_count() else 'CPU')" 2>&1 | ForEach-Object {
    if ($_ -eq 'GPU' -or $_ -eq 'CPU') { Write-Host "计算设备：$_" }
}
$probe = $LASTEXITCODE
$ErrorActionPreference = $previousPreference
if ($probe -ne 0) {
    throw 'Conda 环境 mall-analysis 中的 Paddle 无法启动。'
}
$modelTarget = Join-Path $pipelineRoot 'models\cache\paddlex'
$cacheAlias = Join-Path $env:TEMP 'codex_dianping_video_ocr_cache'
$videoPath = (Resolve-Path -LiteralPath $Video).Path

if (-not $OutputDir) {
    $videoStem = [System.IO.Path]::GetFileNameWithoutExtension($videoPath)
    $OutputDir = Join-Path (Join-Path $pipelineRoot 'outputs') $videoStem
}

if (-not (Test-Path -LiteralPath $modelTarget)) {
    New-Item -ItemType Directory -Force -Path $modelTarget | Out-Null
}
if (-not (Test-Path -LiteralPath $cacheAlias)) {
    New-Item -ItemType Junction -Path $cacheAlias -Target $modelTarget | Out-Null
}

$env:PADDLE_PDX_CACHE_HOME = $cacheAlias
$env:PADDLE_PDX_MODEL_SOURCE = 'modelscope'
$env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = 'True'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$arguments = @(
    (Join-Path $scriptRoot 'analyze_video.py'),
    '--video', $videoPath,
    '--mall-name', $MallName,
    '--recognition-model', $RecognitionModel,
    '--keyframe-interval', $KeyframeInterval,
    '--min-frame-difference', $MinFrameDifference
)
if ($OutputDir) {
    $arguments += @('--output-dir', $OutputDir)
}
if ($MaxSeconds -gt 0) {
    $arguments += @('--max-seconds', $MaxSeconds)
}
if ($SaveDebug) {
    $arguments += '--save-debug'
}
if ($ReuseRawOcrDir) {
    $arguments += @('--reuse-raw-ocr-dir', (Resolve-Path -LiteralPath $ReuseRawOcrDir).Path)
}

& $condaPath @condaPython @arguments
exit $LASTEXITCODE
