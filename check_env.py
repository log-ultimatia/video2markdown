#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_env.py —— YouTube → Markdown 流水线的环境自检

设计要点：**只用标准库**。
这样即使依赖没装、甚至虚拟环境是坏的，它也能跑起来告诉你哪里出了问题。
所以这里检查依赖用的是 find_spec（只找不导入），而不是 import。

检查项：
  1. Python 版本（3.9+ 才能装新版 faster-whisper）
  2. FFmpeg 是否在 PATH —— 硬依赖，缺失则整条流水线跑不了
  3. 代理端口（默认 7890）能否用 socket 连上
  4. 核心依赖包是否可导入
  5. 目录骨架是否就绪
  6. 【提示性】代理是否真的能转发 HTTPS 请求
     —— 端口开着不等于代理能用（可能软件在跑但没选节点/规则不对），
        所以额外发一个真实请求验证。这一项失败只警告、不判定整体失败。

用法：
    python check_env.py
    python check_env.py --proxy-port 1080
    python check_env.py --no-net          # 跳过第 6 项的网络探测

退出码：0 = 关键项全部通过；1 = 有致命问题
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Windows 的英文版控制台默认是 cp1252，打印中文会 UnicodeEncodeError 直接崩溃。
# 保留控制台原有编码，只把无法编码的字符替换掉 —— 中文版 Windows（GBK）不受影响。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


# 项目根目录（本文件所在目录）
LAB_DIR = Path(__file__).resolve().parent

# 阶段二要用到的目录
SUBDIRS = ("downloads", "audio", "output")

# 依赖包：导入名 -> 安装名（用于报错时给出 pip 命令）
PACKAGES = {
    "yt_dlp": "yt-dlp",
    "faster_whisper": "faster-whisper",
    "ffmpeg": "ffmpeg-python",
}

MIN_PYTHON = (3, 9)
RECOMMENDED_PYTHON = (3, 10)

FFMPEG_INSTALL_HINTS = {
    "Windows": (
        "winget install Gyan.FFmpeg\n"
        "        或从 https://www.gyan.dev/ffmpeg/builds/ 下载 essentials 包，\n"
        "        解压后把其中的 bin 目录加入系统 PATH，再重开终端。"
    ),
    "Darwin": "brew install ffmpeg",
    "Linux": "sudo apt install ffmpeg     # Debian/Ubuntu\n"
             "        sudo dnf install ffmpeg     # Fedora",
}

# 用来验证代理能否真正转发请求的探针。
# 选 gstatic 的 generate_204：返回 204 且无响应体，最小、最快。
PROBE_URL = "https://www.gstatic.com/generate_204"

OK, BAD, WARN, INFO = "✓", "✗", "!", "·"


class Report:
    """收集检查结果，最后统一汇总。"""

    def __init__(self) -> None:
        self.critical_failed = 0
        self.warnings = 0

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  [{OK}] {label}" + (f"  {detail}" if detail else ""))

    def fail(self, label: str, detail: str = "", hint: str = "") -> None:
        self.critical_failed += 1
        print(f"  [{BAD}] {label}" + (f"  {detail}" if detail else ""))
        if hint:
            for line in hint.splitlines():
                print(f"        → {line}")

    def warn(self, label: str, detail: str = "", hint: str = "") -> None:
        self.warnings += 1
        print(f"  [{WARN}] {label}" + (f"  {detail}" if detail else ""))
        if hint:
            for line in hint.splitlines():
                print(f"        → {line}")

    def info(self, text: str) -> None:
        print(f"  [{INFO}] {text}")


# ---------------------------------------------------------------------------
# 1. Python 版本
# ---------------------------------------------------------------------------
def check_python(rep: Report) -> None:
    print("\n[1/6] Python 版本")
    ver = sys.version_info
    text = f"{ver.major}.{ver.minor}.{ver.micro}"

    if ver[:2] < MIN_PYTHON:
        rep.fail(
            f"Python {text} 太旧（需要 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）",
            hint="新版 faster-whisper 的依赖没有这么老的轮子。\n"
                 "去 https://www.python.org/downloads/ 装 3.11 或 3.12。",
        )
        return

    rep.ok(f"Python {text}", f"({sys.executable})")
    if ver[:2] < RECOMMENDED_PYTHON:
        rep.warn("建议用 3.10 及以上", "3.9 能跑，但部分依赖的轮子更新更慢")

    # 虚拟环境里跑才算隔离干净
    in_venv = sys.prefix != sys.base_prefix
    if in_venv:
        rep.ok("运行在虚拟环境中", f"({sys.prefix})")
    else:
        rep.warn(
            "当前不是虚拟环境",
            hint="直接装在系统 Python 里，依赖冲突时会很麻烦。\n"
                 "先用 .venv\\Scripts\\Activate.ps1（Windows）或 source .venv/bin/activate 激活。",
        )


# ---------------------------------------------------------------------------
# 2. FFmpeg（硬依赖）
# ---------------------------------------------------------------------------
def check_ffmpeg(rep: Report) -> None:
    print("\n[2/6] FFmpeg（硬依赖：音频提取全靠它）")
    system = platform.system()
    hint = FFMPEG_INSTALL_HINTS.get(system, FFMPEG_INSTALL_HINTS["Linux"])

    path = shutil.which("ffmpeg")
    if not path:
        rep.fail(
            "PATH 里找不到 ffmpeg",
            hint="装好后记得【重开终端】，PATH 才会生效。\n" + hint,
        )
        return

    rep.ok("ffmpeg 已找到", f"({path})")

    # 光有文件还不够：可能是坏的、或缺动态库，实际跑一下才算数
    try:
        proc = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        rep.fail("ffmpeg 无法执行", f"{type(exc).__name__}: {exc}",
                 hint="文件在但跑不起来，通常是缺动态库或下载不完整。\n" + hint)
        return

    if proc.returncode != 0:
        rep.fail("ffmpeg -version 返回非零", f"退出码 {proc.returncode}",
                 hint=(proc.stderr or "")[-300:] + "\n" + hint)
        return

    first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
    rep.ok("ffmpeg 可正常执行", first[:78])

    # 顺带确认 ffprobe 也在（阶段二想读时长时会用到）
    if shutil.which("ffprobe"):
        rep.ok("ffprobe 也在 PATH 中")
    else:
        rep.warn("ffprobe 不在 PATH", "不影响转写，但读音频时长会不方便")


# ---------------------------------------------------------------------------
# 3. 代理端口（socket 连通性测试，不发数据包）
# ---------------------------------------------------------------------------
def check_proxy(rep: Report, port: int) -> bool:
    print(f"\n[3/6] 代理端口 127.0.0.1:{port}（socket 连通性）")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass
    except socket.timeout:
        rep.fail(f"连接 127.0.0.1:{port} 超时",
                 hint="端口没响应。确认代理软件已启动、端口号没写错。")
        return False
    except ConnectionRefusedError:
        rep.fail(f"127.0.0.1:{port} 拒绝连接",
                 hint="没有程序监听这个端口：代理软件没开，或端口不是 " + str(port) +
                      "。\n换个端口试试：python check_env.py --proxy-port 1080")
        return False
    except OSError as exc:
        rep.fail(f"连接 127.0.0.1:{port} 失败", f"{type(exc).__name__}: {exc}")
        return False

    rep.ok(f"127.0.0.1:{port} 可以连上", "端口在监听")
    return True


# ---------------------------------------------------------------------------
# 4. 核心依赖
# ---------------------------------------------------------------------------
def check_packages(rep: Report) -> None:
    print("\n[4/6] 核心依赖包")
    missing = []

    for module, dist in PACKAGES.items():
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if found:
            version = _dist_version(dist)
            rep.ok(f"{dist}", version)
        else:
            missing.append(dist)
            rep.fail(f"{dist} 未安装")

    if missing:
        rep.info("安装命令：pip install " + " ".join(missing))

    # faster-whisper 的实际算力：CPU / CUDA
    if "faster_whisper" not in missing:
        try:
            import ctranslate2
            cpu = ctranslate2.get_supported_compute_types("cpu")
            rep.ok("ctranslate2", f"{ctranslate2.__version__}  CPU: {sorted(cpu)}")
            try:
                ctranslate2.get_supported_compute_types("cuda")
                rep.ok("CUDA 可用", "转写会自动用 GPU")
            except Exception:
                rep.warn("CUDA 不可用，将用 CPU 转写",
                         "base 模型在 CPU 上也够快，不影响流程跑通")
        except Exception as exc:
            rep.warn("无法探测算力", f"{type(exc).__name__}: {exc}")


def _dist_version(dist: str) -> str:
    """取包的版本号；取不到就返回空串（不影响判定）。"""
    try:
        import importlib.metadata as md
        return md.version(dist)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 5. 目录骨架
# ---------------------------------------------------------------------------
def check_dirs(rep: Report) -> None:
    print("\n[5/6] 目录骨架")
    for name in SUBDIRS:
        target = LAB_DIR / name
        try:
            target.mkdir(parents=True, exist_ok=True)
            rep.ok(f"{name}/", "就绪")
        except OSError as exc:
            rep.fail(f"{name}/ 无法创建", f"{type(exc).__name__}: {exc}",
                     hint="确认当前用户对该目录有写权限。")


# ---------------------------------------------------------------------------
# 6. 代理能否真正转发（提示性）
# ---------------------------------------------------------------------------
def check_proxy_forwarding(rep: Report, port: int) -> None:
    print(f"\n[6/6] 代理能否真正转发 HTTPS 请求（提示性）")
    proxy = f"http://127.0.0.1:{port}"

    # 关键点：urllib 默认会读系统代理，所以测试"直连"时必须显式清空，
    # 否则测出来的还是代理通道，结论会骗人。
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    via_proxy = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))

    for label, opener in (("直连", direct), ("走代理", via_proxy)):
        try:
            resp = opener.open(PROBE_URL, timeout=10)
            rep.ok(f"{label} {PROBE_URL}", f"HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            # 能拿到 HTTP 响应就说明通道是通的（状态码不是关键）
            rep.ok(f"{label} {PROBE_URL}", f"HTTP {exc.code}")
        except Exception as exc:
            rep.warn(f"{label} 探测失败", f"{type(exc).__name__}: {str(exc)[:70]}",
                     hint=("如果直连失败、走代理成功 —— 这正是需要代理的原因，"
                           "阶段二会默认走代理。" if label == "直连" else ""))
        finally:
            if label == "走代理":
                rep.info("端口开着≠代理可用：软件在跑但没选节点/规则不对，请求照样失败。"
                         "所以这一项才要真发一个请求。")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="YouTube → Markdown 流水线的环境自检（只用标准库）")
    parser.add_argument("--proxy-port", type=int,
                        default=int(os.environ.get("PROXY_PORT", "7890")),
                        help="本地代理端口，默认 7890（也可用环境变量 PROXY_PORT）")
    parser.add_argument("--no-net", action="store_true",
                        help="跳过第 6 项的网络探测")
    args = parser.parse_args()

    # Windows 控制台默认不是 UTF-8，先修一下，否则下面的 ✓/✗ 会乱码甚至报错
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 66)
    print(" YouTube → Markdown 流水线  环境自检")
    print(f" 目录：{LAB_DIR}")
    print(f" 系统：{platform.system()} {platform.release()}  ({platform.machine()})")
    print("=" * 66)

    rep = Report()
    check_python(rep)
    check_ffmpeg(rep)
    proxy_open = check_proxy(rep, args.proxy_port)
    check_packages(rep)
    check_dirs(rep)

    if args.no_net:
        print("\n[6/6] 代理转发测试  已跳过（--no-net）")
    elif proxy_open:
        check_proxy_forwarding(rep, args.proxy_port)
    else:
        print("\n[6/6] 代理转发测试  跳过（端口不可用）")

    # 汇总
    print("\n" + "=" * 66)
    if rep.critical_failed == 0:
        print(f" 结论：关键项全部通过"
              + (f"（{rep.warnings} 个提醒）" if rep.warnings else ""))
        print(" 可以进入阶段二了。")
        code = 0
    else:
        print(f" 结论：{rep.critical_failed} 个关键项未通过，先按上面的提示修好。")
        code = 1
    print("=" * 66)
    return code


if __name__ == "__main__":
    sys.exit(main())
