param(
    [Parameter(Mandatory=$true)]
    [string]$Change,

    [string]$Config = ".\config.json",

    [switch]$VerboseLog,

    [switch]$NoDigest,

    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path $Config)) {
    if (Test-Path ".\config.example.json") {
        Copy-Item ".\config.example.json" $Config
        Write-Host "已生成配置文件: $Config" -ForegroundColor Yellow
        Write-Host "请先编辑 Gerrit 和 database 配置，然后重新执行。" -ForegroundColor Yellow
        exit 1
    }
    throw "未找到配置文件: $Config"
}

$argsList = @(".\main.py", "--config", $Config, "--change", $Change)
if ($VerboseLog) { $argsList += "--verbose" }
if ($NoDigest) { $argsList += "--no-digest" }

Write-Host "执行 Gerrit Change Skill Discovery: $Change" -ForegroundColor Cyan
python @argsList
$code = $LASTEXITCODE

if ($code -eq 0) {
    Write-Host "执行完成：JSON + 配置数据库 + HTML Dashboard 已更新。" -ForegroundColor Green
    if ($OpenDashboard -and (Test-Path ".\output\dashboard\index.html")) {
        Start-Process (Resolve-Path ".\output\dashboard\index.html")
    }
}

exit $code
