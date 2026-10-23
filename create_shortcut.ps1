# create_shortcut.ps1 —— 在桌面创建「YouTube 转 Markdown」快捷方式
#
# 为什么用 pythonw.exe 而不是 python.exe：
#   python.exe 是控制台程序，双击会先弹一个黑窗口；pythonw.exe 是无窗口版本，
#   界面直接出来，看起来才像个正常的桌面软件。
#
# 用法：
#   右键「使用 PowerShell 运行」，或者在 PowerShell 里：
#     cd youtube2md_lab
#     powershell -ExecutionPolicy Bypass -File .\create_shortcut.ps1
#
# 想删掉：删桌面那个 .lnk 即可，本脚本不写注册表、不留残留。

$ErrorActionPreference = "Stop"

# 脚本所在目录 = 项目根目录（不依赖当前工作目录）
$lab = Split-Path -Parent $MyInvocation.MyCommand.Path
$app = Join-Path $lab "app.py"
$ico = Join-Path $lab "assets\youtube2md.ico"
$venv = Join-Path $lab ".venv"

Write-Host "项目目录: $lab"

# --- 前置检查 -------------------------------------------------------------
if (-not (Test-Path $app)) {
    throw "找不到 app.py：$app"
}

# 优先 pythonw.exe；没有就退回 python.exe（会短暂闪一个控制台）
$pythonw = Join-Path $venv "Scripts\pythonw.exe"
$python  = Join-Path $venv "Scripts\python.exe"
if (Test-Path $pythonw) {
    $target = $pythonw
    Write-Host "使用无窗口解释器: pythonw.exe"
} elseif (Test-Path $python) {
    $target = $python
    Write-Warning "没找到 pythonw.exe，改用 python.exe（启动时会闪一下控制台）"
} else {
    throw "找不到虚拟环境：$venv`n请先在项目目录执行： python -m venv .venv 并安装依赖。"
}

if (-not (Test-Path $ico)) {
    Write-Warning "找不到图标 $ico，快捷方式会用默认图标。"
    Write-Warning "生成图标： <任意含 Pillow 的 python> make_icon.py"
}

# --- 创建快捷方式 ---------------------------------------------------------
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "YouTube 转 Markdown.lnk"

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnkPath)
$sc.TargetPath       = $target
# 参数里带空格，必须整体加引号
$sc.Arguments        = '"' + $app + '"'
$sc.WorkingDirectory = $lab
$sc.Description      = "YouTube 视频 → 带时间戳的 Markdown 转写"
if (Test-Path $ico) {
    # IconLocation 格式为 "路径,索引"，索引 0 = 第一个图标
    $sc.IconLocation = "$ico,0"
}
$sc.WindowStyle = 1
$sc.Save()

if (Test-Path $lnkPath) {
    Write-Host ""
    Write-Host "✓ 快捷方式已创建：" -ForegroundColor Green
    Write-Host "  $lnkPath"
    Write-Host ""
    Write-Host "  目标      : $target"
    Write-Host "  参数      : app.py"
    Write-Host "  起始位置  : $lab"
} else {
    throw "快捷方式创建失败：$lnkPath"
}
