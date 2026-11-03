#requires -Version 5.1
<#
.SYNOPSIS
    把开发仓库里的插件部署到机器人项目的 plugins/ 下。

.DESCRIPTION
    开发在 D:\Dev\whale_lexicon（独立仓库、独立发版），部署到
    D:\Dev\qq-bot-lite\plugins\whale_lexicon（机器人实际加载的那份）。

    机器人只加载项目内 plugins/ 下的目录，所以这份副本是「安装产物」：
    不带 .git，不进主仓库（主仓库 .gitignore 已经忽略它）。

    同步用 robocopy /MIR：目标多出来的文件会被删掉，保证两边一致。
    排除开发用的东西（.git、测试、工具脚本、缓存、文档截图）。

.PARAMETER BotRoot
    机器人项目根目录，默认 D:\Dev\qq-bot-lite

.PARAMETER DryRun
    只打印 robocopy 会做什么，不实际改文件。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\deploy-to-bot.ps1
    powershell -ExecutionPolicy Bypass -File tools\deploy-to-bot.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [string]$BotRoot = 'D:\Dev\qq-bot-lite',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$Source = Split-Path -Parent $PSScriptRoot      # 仓库根
$Target = Join-Path $BotRoot 'plugins\whale_lexicon'

if (-not (Test-Path (Join-Path $BotRoot 'app'))) {
    throw "BotRoot 不像 qq-bot-lite 项目：$BotRoot"
}

Write-Host "源: $Source"
Write-Host "目标: $Target"

$dirty = @(git -C $Source status --porcelain)
$head = (git -C $Source rev-parse --short HEAD)
$branch = (git -C $Source branch --show-current)
if ($dirty.Count -gt 0) {
    Write-Host ("注意：工作区有 {0} 项未提交改动，部署的是当前文件状态（HEAD {1}）" -f $dirty.Count, $head) -ForegroundColor DarkYellow
} else {
    Write-Host ("工作区干净，HEAD {0} @ {1}" -f $head, $branch)
}

$version = (Select-String -Path (Join-Path $Source 'plugin.toml') -Pattern '^version\s*=\s*"([^"]+)"' |
    Select-Object -First 1).Matches.Groups[1].Value

$ExcludeDirs = @('.git', '__pycache__', '.pytest_cache', '.ruff_cache', 'tests', 'tools', 'docs', '.github', '.venv')
$ExcludeFiles = @('*.pyc', 'pytest.ini', 'ruff.toml', '.editorconfig', '.gitignore')

$roboArgs = @(
    $Source, $Target,
    '/MIR',
    '/NFL', '/NDL', '/NJH', '/NJS', '/NP',
    '/XD'
) + $ExcludeDirs + @('/XF') + $ExcludeFiles

if ($DryRun) {
    Write-Host '--- 预演（robocopy /L）---' -ForegroundColor Cyan
    robocopy @roboArgs '/L' | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy 失败，退出码 $LASTEXITCODE" }
    Write-Host '预演结束，未改动文件。' -ForegroundColor Cyan
    exit 0
}

robocopy @roboArgs | Out-Null
$code = $LASTEXITCODE
# robocopy：0=无变化 1=有复制 2=有删除 3=两者都有，>=8 才是真失败
if ($code -ge 8) { throw "robocopy 失败，退出码 $code" }

# 留个部署标记，排查时能对上版本
$stamp = [ordered]@{
    version    = $version
    commit     = $head
    branch     = $branch
    dirty      = ($dirty.Count -gt 0)
    deployedAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
}
($stamp | ConvertTo-Json) | Set-Content (Join-Path $Target '.deployed.json') -Encoding UTF8

$suffix = ''
if ($dirty.Count -gt 0) { $suffix = '（含未提交改动）' }
Write-Host ("部署完成：{0} -> {1}，提交 {2}{3}" -f $version, $Target, $head, $suffix) -ForegroundColor Green
Write-Host '机器人改动插件代码后需要重启才生效。'
