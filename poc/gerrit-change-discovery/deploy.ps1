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

Write-Host "[1/8] 检查运行环境..." -ForegroundColor Yellow
Require-Command python
Require-Command git
python --version
git --version

Write-Host "[2/8] 安装 Python 依赖..." -ForegroundColor Yellow
python -m pip install -r .\requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Python 依赖安装失败" }

Write-Host "[3/8] 校验 Python 模块语法..." -ForegroundColor Yellow
python -m py_compile .\main.py .\gerrit_client.py .\inventory.py .\change_analyzer.py .\skill_digest.py .\database.py .\report_generator.py
if ($LASTEXITCODE -ne 0) { throw "Python 模块语法校验失败" }

Write-Host "[4/8] 准备配置文件..." -ForegroundColor Yellow
if (-not (Test-Path $Config)) {
    Copy-Item ".\config.example.json" $Config
    Write-Host "已生成 $Config" -ForegroundColor Green
    Write-Host "请先编辑 Gerrit 和 database 配置，然后重新执行 deploy.ps1。" -ForegroundColor Yellow
    exit 1
} else {
    Write-Host "保留已有配置: $Config" -ForegroundColor Green
}

Write-Host "[5/8] 创建运行目录..." -ForegroundColor Yellow
@(".\data", ".\output", ".\output\dashboard", ".\workspace") | ForEach-Object {
    New-Item -ItemType Directory -Force -Path $_ | Out-Null
}

Write-Host "[6/8] 检查数据库连接并初始化表结构..." -ForegroundColor Yellow
python .\database.py --config $Config --check --init --summary
if ($LASTEXITCODE -ne 0) {
    Write-Host "" -ForegroundColor Red
    Write-Host "数据库初始化失败。若 database.type=mysql，请确认：" -ForegroundColor Red
    Write-Host "1. config.json 中 host/port/database/username/password 正确" -ForegroundColor Yellow
    Write-Host "2. MySQL 中目标数据库已创建，例如 skillhub_security" -ForegroundColor Yellow
    Write-Host "3. 当前数据库账号对目标库至少具有 CREATE/ALTER/INDEX/SELECT/INSERT/UPDATE/DELETE 权限" -ForegroundColor Yellow
    throw "数据库初始化失败"
}

Write-Host "[7/8] 生成初始 HTML Dashboard..." -ForegroundColor Yellow
python .\report_generator.py --config $Config
if ($LASTEXITCODE -ne 0) { throw "Dashboard 生成失败" }

Write-Host "[8/8] 部署完成" -ForegroundColor Green
Write-Host ""
Write-Host "接下来只需要：" -ForegroundColor Cyan
Write-Host "1. 执行：.\run_change.ps1 -Change <Gerrit单据号> -VerboseLog" -ForegroundColor White
Write-Host "2. Dashboard：.\output\dashboard\index.html" -ForegroundColor White
Write-Host "3. 原始 JSON：.\output\change-*-patchset-*.json" -ForegroundColor White
Write-Host "4. 事实数据：写入 config.json 中配置的数据库" -ForegroundColor White

if ($OpenDashboard) {
    $dashboard = Resolve-Path ".\output\dashboard\index.html"
    Start-Process $dashboard
}
