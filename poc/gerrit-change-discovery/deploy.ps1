param(
    [string]$Config = ".\config.json",
    [switch]$OpenDashboard
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " SkillHub Gerrit Change Discovery POC - One Click Deploy" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

function Require-Command($Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "未找到命令: $Name"
    }
}

Write-Host "[1/7] 检查运行环境..." -ForegroundColor Yellow
Require-Command python
Require-Command git
python --version
git --version

Write-Host "[2/7] 校验 Python 模块语法..." -ForegroundColor Yellow
python -m py_compile .\main.py .\gerrit_client.py .\inventory.py .\change_analyzer.py .\skill_digest.py .\database.py .\report_generator.py
if ($LASTEXITCODE -ne 0) { throw "Python 模块语法校验失败" }

Write-Host "[3/7] 准备配置文件..." -ForegroundColor Yellow
if (-not (Test-Path $Config)) {
    Copy-Item ".\config.example.json" $Config
    Write-Host "已生成 $Config" -ForegroundColor Green
    Write-Host "注意：请部署完成后编辑 Gerrit 地址、username、http_password、ssh_url_template。" -ForegroundColor Yellow
} else {
    Write-Host "保留已有配置: $Config" -ForegroundColor Green
}

Write-Host "[4/7] 创建运行目录..." -ForegroundColor Yellow
@(".\data", ".\output", ".\output\dashboard", ".\workspace") | ForEach-Object {
    New-Item -ItemType Directory -Force -Path $_ | Out-Null
}

Write-Host "[5/7] 初始化 SQLite 数据库..." -ForegroundColor Yellow
python .\database.py --config $Config --init --summary
if ($LASTEXITCODE -ne 0) { throw "数据库初始化失败" }

Write-Host "[6/7] 生成初始 HTML Dashboard..." -ForegroundColor Yellow
python .\report_generator.py --config $Config
if ($LASTEXITCODE -ne 0) { throw "Dashboard 生成失败" }

Write-Host "[7/7] 部署完成" -ForegroundColor Green
Write-Host ""
Write-Host "接下来只需要：" -ForegroundColor Cyan
Write-Host "1. 编辑 $Config，填写 Gerrit 配置" -ForegroundColor White
Write-Host "2. 执行：.\run_change.ps1 -Change <Gerrit单据号> -VerboseLog" -ForegroundColor White
Write-Host "3. Dashboard：.\output\dashboard\index.html" -ForegroundColor White
Write-Host "4. SQLite：.\data\skillhub-poc.db" -ForegroundColor White
Write-Host "5. 原始 JSON：.\output\change-*-patchset-*.json" -ForegroundColor White

if ($OpenDashboard) {
    $dashboard = Resolve-Path ".\output\dashboard\index.html"
    Start-Process $dashboard
}
