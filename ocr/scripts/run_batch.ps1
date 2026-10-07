param(
    [string]$InputDir,
    [string]$OutputDir,
    [double]$KeyframeInterval = 0.5,
    [double]$MinFrameDifference = 1.0,
    [ValidateSet('PP-OCRv5_mobile_rec', 'PP-OCRv5_server_rec')]
    [string]$RecognitionModel = 'PP-OCRv5_mobile_rec',
    [switch]$SaveDebug,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$scriptRoot = $PSScriptRoot
$pipelineRoot = Split-Path -Parent $scriptRoot

if (-not $InputDir) {
    $InputDir = Join-Path $pipelineRoot 'input_videos'
}
if (-not $OutputDir) {
    $OutputDir = Join-Path $pipelineRoot 'outputs'
}

$inputRoot = (Resolve-Path -LiteralPath $InputDir).Path
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$outputRoot = (Resolve-Path -LiteralPath $OutputDir).Path
$videos = Get-ChildItem -LiteralPath $inputRoot -File -Filter '*.mp4' | Sort-Object Name

if (-not $videos) {
    Write-Host "未找到待处理视频：$inputRoot"
    exit 0
}

$completed = 0
$skipped = 0
$failed = 0
foreach ($video in $videos) {
    if ($video.BaseName -notmatch '^(?<city>.+?)__(?<mall>.+?)__(?<date>\d{8})$') {
        Write-Warning "跳过命名不合规的视频：$($video.Name)"
        $skipped += 1
        continue
    }

    $mallName = $Matches.mall
    $mallOutput = [System.IO.Path]::GetFullPath((Join-Path $outputRoot $video.BaseName))
    $outputParent = [System.IO.Directory]::GetParent($mallOutput).FullName
    if ($outputParent -ne $outputRoot) {
        throw "输出目录越界：$mallOutput"
    }

    $resultCsv = Join-Path $mallOutput 'merchant_cards.csv'
    if ((Test-Path -LiteralPath $resultCsv) -and -not $Force) {
        Write-Host "跳过已有结果：$($video.BaseName)"
        $skipped += 1
        continue
    }
    if ((Test-Path -LiteralPath $mallOutput) -and $Force) {
        Remove-Item -LiteralPath $mallOutput -Recurse -Force
    }

    Write-Host "开始处理：$mallName（$($video.Name)）"
    try {
        $singleArgs = @{
            Video = $video.FullName
            MallName = $mallName
            OutputDir = $mallOutput
            KeyframeInterval = $KeyframeInterval
            MinFrameDifference = $MinFrameDifference
            RecognitionModel = $RecognitionModel
        }
        if ($SaveDebug) {
            $singleArgs.SaveDebug = $true
        }
        & (Join-Path $scriptRoot 'run_single.ps1') @singleArgs
        if ($LASTEXITCODE -ne 0) {
            throw "分析脚本退出码：$LASTEXITCODE"
        }
        $completed += 1
    }
    catch {
        Write-Error "处理失败：$($video.Name)：$($_.Exception.Message)" -ErrorAction Continue
        $failed += 1
    }
}

Write-Host "批处理结束：成功 $completed，跳过 $skipped，失败 $failed"
if ($failed -gt 0) {
    exit 1
}
