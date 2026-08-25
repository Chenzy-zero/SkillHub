$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$Config = Join-Path $ScriptDir "scan_config.json"
$Example = Join-Path $ScriptDir "scan_config.example.json"

if (-not (Test-Path $Config)) {
    Copy-Item $Example $Config
    Write-Host "[SkillHub POC] 已生成配置文件: $Config" -ForegroundColor Yellow
    Write-Host "请先编辑 scan_config.json，填写 Gerrit/Git SSH 仓库地址，然后再次执行 .\run_batch.ps1" -ForegroundColor Yellow
    exit 0
}

Write-Host "[SkillHub POC] 使用配置: $Config" -ForegroundColor Cyan
python .\batch_scan.py --config $Config
$Code = $LASTEXITCODE

if ($Code -ne 0) {
    Write-Host "[SkillHub POC] 扫描结束，但存在失败仓库。请查看 output\batch_scan.log" -ForegroundColor Yellow
} else {
    Write-Host "[SkillHub POC] 扫描全部完成。结果位于 output 目录。" -ForegroundColor Green
}

exit $Code
