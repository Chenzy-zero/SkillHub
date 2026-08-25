param(
    [Parameter(Mandatory=$true)]
    [string]$Change,

    [string]$Config = ".\config.json",

    [switch]$VerboseLog,

    [switch]$NoDigest
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Config)) {
    if (Test-Path ".\config.example.json") {
        Copy-Item ".\config.example.json" $Config
        Write-Host "已生成配置文件: $Config" -ForegroundColor Yellow
        Write-Host "请先编辑 Gerrit 地址、账号和 SSH URL 模板，然后重新执行。" -ForegroundColor Yellow
        exit 1
    }
    throw "未找到配置文件: $Config"
}

$argsList = @(".\main.py", "--config", $Config, "--change", $Change)
if ($VerboseLog) { $argsList += "--verbose" }
if ($NoDigest) { $argsList += "--no-digest" }

Write-Host "执行 Gerrit Change Skill Discovery: $Change" -ForegroundColor Cyan
python @argsList
exit $LASTEXITCODE
