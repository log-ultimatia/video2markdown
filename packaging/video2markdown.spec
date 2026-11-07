# -*- mode: python ; coding: utf-8 -*-
"""
video2markdown.spec —— PyInstaller 打包配置

## 为什么是两个 exe

PyInstaller 的 `--windowed`（无控制台）应用在 Windows 上 **没有 stdout/stderr**。
而 GUI 是靠"捕获子进程输出"来显示运行日志的，所以流水线不能跑在 windowed 的 exe 里。

于是打成两个 exe，共享同一份依赖（只打一次包，体积不会翻倍）：

    video2markdown.exe   无控制台   GUI（用户双击这个）
    v2m-cli.exe          有控制台   流水线 / 环境自检（由 GUI 以子进程启动）

## 为什么排除 nvidia

venv 里 `nvidia`（cublas + cudnn）占 2 GB，而它只在有 NVIDIA 显卡且用户想加速时才需要。
默认不打进包 —— 用户想要 GPU 加速，可以自己 `pip install` 到 exe 旁边，
或者用发布页提供的 GPU 版附件。这样主包能控制在几百 MB。

## 模型

不打包进 exe。首次运行时流水线会自动下载到 exe 旁边的 `models/`（HF 镜像，国内可用）。
发布页另提供"带模型"的压缩包，解压即用、完全离线。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# SPECPATH 是 spec 文件所在目录（packaging/），项目根是它的上一级。
LAB = Path(SPECPATH).parent  # noqa: F821  (PyInstaller 注入)

# ---------------------------------------------------------------------------
# 依赖收集
# ---------------------------------------------------------------------------
# yt_dlp 的提取器是动态导入的，静态分析扫不到，必须显式收集全部子模块，
# 否则打包后一解析链接就报 "No suitable extractor"。
hiddenimports = (
    collect_submodules("yt_dlp")
    + collect_submodules("faster_whisper")
    + collect_submodules("sherpa_onnx")
    + collect_submodules("onnxruntime")
    + ["opencc", "ctranslate2", "av", "pipeline", "check_env", "diarize"]
)

# OpenCC 依赖 JSON 配置（t2s.json 等），漏了会静默退回不转换。
datas = (
    # faster_whisper 自带 VAD 模型（assets/silero_vad_v6.onnx）。
    # 漏了它，打包版一转写就报 ONNXRuntimeError NO_SUCHFILE。
    collect_data_files("faster_whisper")
    + collect_data_files("opencc")
    + collect_data_files("yt_dlp")
    + collect_data_files("sherpa_onnx")
    + [(str(LAB / "assets"), "assets")]
)

# 各原生库的 DLL 必须显式收集。
binaries = (
    collect_dynamic_libs("ctranslate2")
    + collect_dynamic_libs("av")
    + collect_dynamic_libs("onnxruntime")
    + collect_dynamic_libs("sherpa_onnx")
)

# 这些要么体积巨大、要么本项目用不到。
excludes = [
    "nvidia",          # 2 GB 的 CUDA 运行库，见文件头说明
    "torch", "torchaudio", "torchvision",
    "matplotlib", "scipy", "pandas", "sklearn",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "jupyter", "notebook", "pytest",
    "tensorflow", "transformers",
]

a = Analysis(
    [str(LAB / "main.py")],
    pathex=[str(LAB)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# --- 1) GUI：无控制台 -------------------------------------------------------
exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="video2markdown",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                    # 不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(LAB / "assets" / "youtube2md.ico"),
)

# --- 2) CLI：有控制台，GUI 以子进程调用它跑流水线 ---------------------------
exe_cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="v2m-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                     # 必须有 stdout，否则 GUI 收不到日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(LAB / "assets" / "youtube2md.ico"),
)

coll = COLLECT(
    exe_gui,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="video2markdown",
)
