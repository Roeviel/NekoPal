<#
  在桌面创建「蕴 · 猫娘伴友」快捷方式。

  单独一个脚本而不是塞进 .bat 里：批处理里的中文和内联 PowerShell 引号
  太容易在代码页变化时被解析坏（这个项目已经踩过两次），
  放到 .ps1 里（UTF-8 带 BOM）稳定得多，也可以随时手动重跑。

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\make_shortcut.ps1
#>
[CmdletBinding()]
param(
    [string]$Name = '蕴 · 猫娘伴友'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$vbs  = Join-Path $root '启动蕴.vbs'
$ico  = Join-Path $root 'web\favicon.ico'

if (-not (Test-Path $vbs)) { Write-Host "找不到启动脚本：$vbs" -ForegroundColor Red; exit 1 }

$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop "$Name.lnk"

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
# 目标用 wscript.exe + 参数，比直接指向 .vbs 更明确、也更不容易被杀软拦
$lnk.TargetPath       = Join-Path $env:SystemRoot 'System32\wscript.exe'
$lnk.Arguments        = '"' + $vbs + '"'
$lnk.WorkingDirectory = $root
$lnk.Description      = "$Name —— 双击启动"
if (Test-Path $ico) { $lnk.IconLocation = "$ico,0" }
$lnk.Save()

if (Test-Path $lnkPath) {
    Write-Host "已在桌面创建快捷方式：$lnkPath" -ForegroundColor Green
    Write-Host "  目标 : $($shell.CreateShortcut($lnkPath).TargetPath)"
    exit 0
}
Write-Host "快捷方式创建失败" -ForegroundColor Red
exit 1
