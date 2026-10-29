#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pipeline.py —— YouTube → Markdown 五层流水线

    代理层  →  下载层  →  提取层  →  转写层  →  输出层
   (proxy)   (yt-dlp)   (ffmpeg)  (whisper)  (markdown)

每层都是独立函数，单独 try/except，失败时给出可读的中文报错并且【立刻停止】——
不做"这步失败就跳过"的静默降级，那样只会得到一份看不出哪里坏了的产物。

分层的好处是排错时能一眼看出卡在哪：
    downloads/ 有文件、audio/ 空       → 提取层（ffmpeg）出问题
    audio/ 有文件、output/ 的 md 很空   → 转写层（模型/参数）出问题

用法：
    python pipeline.py "https://www.youtube.com/watch?v=XXXX"
    python pipeline.py URL --model small
    python pipeline.py URL --proxy http://127.0.0.1:7890
    python pipeline.py URL --no-proxy
    python pipeline.py URL --force            # 忽略已有文件，全部重来
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import diarize

# ---------------------------------------------------------------------------
# 路径与默认配置
# ---------------------------------------------------------------------------
LAB_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = LAB_DIR / "downloads"
AUDIO_DIR = LAB_DIR / "audio"
OUTPUT_DIR = LAB_DIR / "output"

# 模型缓存目录。
# 为什么不用默认的 ~/.cache/huggingface：
#   · 项目自包含：删掉 lab 目录就是彻底清理，不在用户主目录残留 145 MB
#   · 权限问题：受限环境下写主目录会被拒绝（实测 Windows 上直接 PermissionError）
#   · 阶段三要复用同一个模型做对比，放这里更好找
# 想改成共享缓存： set Y2M_MODEL_DIR=D:\models
_MODEL_DIR_ENV = os.environ.get("Y2M_MODEL_DIR", "").strip()
MODEL_DIR = Path(_MODEL_DIR_ENV) if _MODEL_DIR_ENV else (LAB_DIR / "models")

# 只把 download_root 指到 MODEL_DIR 还不够。
# huggingface_hub 会把【元数据、blob 索引、xet 传输缓存】另外写到 HF_HOME
# （默认 ~/.cache/huggingface），受限环境下那一步同样会 PermissionError。
# 所以这里把 HF_HOME 也默认指到项目内，让整个 lab 真正自包含。
# 用 setdefault：你显式设了 HF_HOME 或 Y2M 之外的路径，就以你的为准。
os.environ.setdefault("HF_HOME", str(MODEL_DIR / ".hf"))
# CLI and GUI should use the same mirror default. Explicit user configuration wins.
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# 同理：Windows 没开"开发者模式"时 huggingface_hub 建不了符号链接，
# 每次都会打一段二十行的警告。功能不受影响（退化成一字一份副本），
# 但会把 CLI 输出淹没，所以关掉这条提示。
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# Some mirrors do not support Hugging Face's Xet CAS endpoints; force the
# regular HTTP blob path unless the user explicitly chooses otherwise.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# 代理：默认本地 7890（Clash / v2ray 常见端口）
DEFAULT_PROXY = "http://127.0.0.1:7890"

# 【代码内硬编码代理】
# 填上非空字符串就强制使用它，优先级高于环境变量 HTTP_PROXY / HTTPS_PROXY。
# 适合"这台机器总是走这个代理"的场景：
#     HARDCODED_PROXY = "http://127.0.0.1:7890"
HARDCODED_PROXY = ""

SAMPLE_RATE = 16000          # Whisper 要求 16 kHz
WHISPER_MODEL = "base"       # tiny / base / small / medium / large-v3
DEVICE_MODES = ("auto", "cpu", "cuda", "npu")
NPU_MODEL_REPO = "OpenVINO/whisper-{model}-int8-ov"
PARAGRAPH_CHARS = 180        # 每段大约多少字（用来把字幕切成可读段落）
PARAGRAPH_GAP = 1.5          # 相邻字幕间隔超过这个秒数就另起一段
CHUNK_SEC = 60               # 分块秒数。
#   为什么是 60 秒而不是 300：实测整段转写时，模型会延续上一段的语言上下文，
#   把一段纯英文念成「中文字幕/中文文字」这种复读幻觉（英文词从 116 掉到 1）。
#   切成 60 秒独立转写、每块单独检测语言后，英文词回到 116、幻觉归零。
CKPT_VERSION = 4             # 检查点版本；转写参数/存档粒度变了都要加 1
CKPT_SAVE_EVERY = 3          # 攒够这么多条字幕就存一次
CKPT_SAVE_SEC = 2.0          # 或者距上次存档超过这么多秒也存（双阈值）
RESUME_OVERLAP = 2.0         # 续跑时往回多转几秒，避免正好切在词中间
#  v2: 加入 multilingual 逐窗口语言检测 + 反幻觉参数，旧结果不可复用
#  v3: 改成【段级】存档（原来按 5 分钟一块存，太粗：不到 40 秒就停止等于白跑）
#  v4: 分块 300s -> 60s，且每块单独检测语言（修混合语言被幻觉吃掉）

# Windows 上不想让子进程弹黑框
# Windows 的英文版控制台默认是 cp1252，打印中文会 UnicodeEncodeError 直接崩溃。
# 保留控制台原有编码，只把无法编码的字符替换掉 —— 中文版 Windows（GBK）不受影响。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# CUDA 上的计算精度。
#
# 为什么不用最"标准"的 float16：在这台机器（RTX 4070 Laptop）上实测，
# float16 会让模型输出崩掉 —— 同一支中英混排视频：
#     float16        英文词 154，中文字 1152，繁体 350   <- 把英文翻成中文还带繁体
#     int8_float16   英文词 765，中文字 263，繁体 38     <- 正常
#     int8           英文词 77， 中文字 245，繁体 75     <- 正常
# 症状很像"语言判定被数值误差带偏"。int8 权重 + float16 计算是 CTranslate2 在
# GPU 上最稳的组合，速度与纯 float16 基本一样，所以默认用 int8_float16。
CUDA_COMPUTE_TYPE = "int8_float16"

# 是否把上一段转写结果作为下一段的上下文。
#
# True（faster-whisper 默认）：上下文连续，但会把复读当上下文继续滚，
#   出现"原影片在 20 分钟…原影片在 20 秒钟…"这种越来越偏的循环。
# False：每段独立解码，复读不容易传染，但对繁体和断句的约束更弱。
CONDITION_ON_PREV = False
_CUDA_DLL_DIR_HANDLES = []

# ---------------------------------------------------------------------------
# 中文/多语言输出质量：initial_prompt + 繁简转换
# ---------------------------------------------------------------------------
# 为什么要 initial_prompt：
#   Whisper 对中文默认会自由输出繁体字、而且经常整段不加标点（实测 60 秒音频：
#   繁体字 82 个、标点 0 个）。给它一段"书面语示例"，它就会对齐成简体 + 带标点。
#   实测加提示词后：繁体 82 -> 0，标点 0 -> 11，而且反而更快（4.1s -> 2.4s）。
LANG_PROMPTS = {
    "zh": "以下是普通话的句子，使用简体中文，并加上标点符号。",
    "ja": "以下是日语的句子，请使用标准日语并加上标点符号。",
    "ko": "다음은 한국어 문장입니다. 표준 한국어로 표기하고 문장 부호를 넣으세요.",
    "en": "The following is a clear English sentence with proper punctuation.",
}

# 繁 -> 简 的兜底转换（提示词偶尔还会漏掉个别繁体字）
_opencc = None


def to_simplified(text: str) -> str:
    """把繁体中文转成简体。opencc 没装就原样返回（不影响主流程）。"""
    global _opencc
    if not text:
        return text
    try:
        if _opencc is None:
            from opencc import OpenCC
            _opencc = OpenCC("t2s")
        return _opencc.convert(text)
    except Exception:
        return text


def prompt_for(lang: str | None) -> str | None:
    """
    按语言挑提示词，并把专有名词表（--vocab）拼进去。

    这就是"词表偏置"：Whisper 把 initial_prompt 当作"上一段文本"，
    里面出现过一次的词，解码时更倾向复现。实测能把"证名"拉回"证明"。

    零依赖、零内存 —— 只是几十个 token 的字符串。YouTube 提升专有名词
    准确率用的就是这个手段（它塞的是标题/频道名/描述）。
    """
    base = None
    if lang:
        base = LANG_PROMPTS.get(lang.split("-")[0].lower())
    terms = [x.strip() for x in VOCAB_TERMS if x and x.strip()]
    if not terms:
        return base
    # 放在最前面：模型对 prompt 开头更敏感
    return f"这段内容涉及{'、'.join(terms)}。" + (base or "")


class PipelineError(RuntimeError):
    """流水线中可预期的失败。用这个类型把"业务错误"和"代码 bug"区分开。"""


# ---------------------------------------------------------------------------
# 计时工具：阶段三要做速度对比，所以每层都记耗时
# ---------------------------------------------------------------------------
@dataclass
class Stage:
    name: str
    seconds: float = 0.0
    detail: str = ""


@dataclass
class Report:
    stages: list = field(default_factory=list)
    started: float = field(default_factory=time.time)

    def add(self, name: str, seconds: float, detail: str = "") -> None:
        self.stages.append(Stage(name, seconds, detail))

    def print_summary(self) -> None:
        total = time.time() - self.started
        print("\n" + "=" * 60)
        print(" 各阶段耗时")
        print("=" * 60)
        for st in self.stages:
            print(f"  {st.name:<10} {st.seconds:7.1f}s   {st.detail}")
        print(f"  {'合计':<10} {total:7.1f}s")
        print("=" * 60)


def probe_duration(path: Path) -> float:
    """用 ffprobe 读媒体时长（秒）。失败返回 0。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
            creationflags=CREATE_NO_WINDOW).stdout.strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def banner(text: str) -> None:
    print(f"\n{'-' * 60}\n {text}\n{'-' * 60}")


# ---------------------------------------------------------------------------
# 1. 代理层
# ---------------------------------------------------------------------------
def resolve_proxy(cli_proxy: str | None = None,
                  no_proxy: bool = False) -> str | None:
    """
    决定这次用什么代理。返回代理 URL；None 表示直连。

    优先级（从高到低）：
      1. 命令行 --no-proxy        → 强制直连
      2. 命令行 --proxy URL       → 用指定的
      3. 代码内 HARDCODED_PROXY   → 改代码就能固定
      4. 环境变量 HTTPS_PROXY / HTTP_PROXY
      5. 默认 DEFAULT_PROXY（127.0.0.1:7890）

    最后还会用 socket 探一下端口通不通。不通不一定就放弃——
    有可能这台机器本身能直连（比如在公司网络里），所以只警告并退回直连，
    同时把话说清楚，免得后面下载失败时看不懂原因。
    """
    banner("第 1 层 / 代理配置")

    if no_proxy:
        print("  命令行指定了 --no-proxy，本次直连。")
        return None

    source = ""
    if cli_proxy:
        proxy, source = cli_proxy, "命令行 --proxy"
    elif HARDCODED_PROXY:
        proxy, source = HARDCODED_PROXY, "代码内 HARDCODED_PROXY"
    elif os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"):
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        source = "环境变量 HTTPS_PROXY/HTTP_PROXY"
    else:
        proxy, source = DEFAULT_PROXY, "默认值"

    print(f"  来源：{source}")
    print(f"  代理：{proxy}")

    # 只有 http://host:port 这种形式才能用 socket 直接探
    host, port = _parse_proxy_hostport(proxy)
    if host is None:
        print(f"  [警告] 无法从 {proxy!r} 解析出 host:port，跳过连通性检测")
        return proxy

    ok, why = _probe_socket(host, port)
    if ok:
        print(f"  [OK] {host}:{port} 端口在监听")
        return proxy

    # 端口不通：可能压根不需要代理，也可能代理软件没开
    print(f"  [警告] {host}:{port} 连不上（{why}）")
    print("         本次改为【直连】继续。如果后续下载失败，请先检查：")
    print(f"           1) 代理软件是否已启动、端口是不是 {port}")
    print("           2) 换端口重试： python pipeline.py URL --proxy http://127.0.0.1:<端口>")
    print("           3) 确认只能走代理时，配好再跑；想强制直连用 --no-proxy")
    return None


def _parse_proxy_hostport(proxy: str):
    """从 http://127.0.0.1:7890 里抠出 (host, port)。"""
    try:
        from urllib.parse import urlparse
        u = urlparse(proxy if "://" in proxy else f"http://{proxy}")
        if not u.hostname:
            return None, None
        return u.hostname, u.port or 7890
    except Exception:
        return None, None


def _probe_socket(host: str, port: int, timeout: float = 3.0):
    """socket 连通性测试。返回 (是否通, 原因)。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except socket.timeout:
        return False, "连接超时"
    except ConnectionRefusedError:
        return False, "连接被拒绝，没有程序监听该端口"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _numbered_path(path: Path) -> Path:
    """Return *path*, or ``name (1)``, ``name (2)`` when it already exists."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    index = 1
    while True:
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _cookie_file(path: Path | None) -> str | None:
    """Validate and normalize a Netscape cookie file for yt-dlp."""
    if path is None:
        return None
    path = Path(path).expanduser()
    if not path.is_file():
        raise PipelineError(f"Cookie 文件不存在：{path}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PipelineError(f"无法读取 Cookie 文件：{path}\n   {exc}") from exc
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Netscape exports use #HttpOnly_ as a real domain prefix, not a comment.
        if stripped.startswith("#") and not stripped.startswith("#HttpOnly_"):
            continue
        rows.append(stripped)
    if not rows or not any(len(row.split("\t")) >= 7 for row in rows):
        raise PipelineError(
            f"Cookie 文件不是 Netscape 格式：{path}\n"
            "   请导出为 Netscape HTTP Cookie File（每行用 Tab 分隔 7 列）。"
        )
    return str(path)


def _browser_cookie_spec(raw: str | None) -> tuple | None:
    """Convert yt-dlp's browser cookie spec into its Python tuple form."""
    if not raw:
        return None
    parts = raw.strip().split(":")
    if not parts[0]:
        raise PipelineError("浏览器 Cookie 参数不能为空")
    if len(parts) > 4:
        raise PipelineError(
            "浏览器 Cookie 参数格式错误：browser[:profile[:keyring[:container]]]" )
    return tuple(parts + [None] * (4 - len(parts)))


def _source_marker(path: Path) -> Path:
    return path.with_name(path.name + ".source.json")


def _source_signature(path: Path) -> dict:
    st = path.stat()
    return {
        "path": str(path.resolve()),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }


def _same_source(target: Path, source: Path) -> bool:
    try:
        marker = _source_marker(target)
        if not marker.exists():
            return False
        return json.loads(marker.read_text(encoding="utf-8")) == _source_signature(source)
    except (OSError, ValueError, TypeError):
        return False


def _target_for_source(base: Path, source: Path, force: bool = False) -> Path:
    """Reuse a target only for the same source; otherwise add ``(n)``."""
    if force:
        return base
    if not base.exists():
        return base
    if _same_source(base, source):
        return base
    return _numbered_path(base)


def _save_source_marker(target: Path, source: Path) -> None:
    try:
        _source_marker(target).write_text(
            json.dumps(_source_signature(source), ensure_ascii=False),
            encoding="utf-8")
    except OSError as exc:
        print(f"  [警告] 来源标记保存失败：{exc}")


# ---------------------------------------------------------------------------
# 2. 下载层
# ---------------------------------------------------------------------------
def download_audio(url: str, proxy: str | None, force: bool = False,
                   cookies: Path | None = None,
                   cookies_from_browser: str | None = None) -> tuple[Path, dict]:
    """
    用 yt-dlp 下载【只有音频】的流。

    优先下载独立音轨；只有没有独立音轨时，才退回最低画质的音视频流。
    若站点没有可用的低画质合并流，才使用默认最佳格式。

    返回 (音频文件路径, yt-dlp 的元信息 dict)。
    """
    banner("第 2 层 / 下载音频")

    # 本地文件直接放行（界面的「选择本地文件…」走这条）
    local = Path(url)
    if local.exists() and local.is_file():
        size_mb = local.stat().st_size / (1024 * 1024)
        dur = probe_duration(local)
        print(f"  本地文件：{local.name}")
        print(f"       大小 {size_mb:.1f} MB，时长 {_fmt_duration(dur)}（跳过下载）")
        return local, {"title": local.stem, "duration": dur}

    print(f"  URL: {url}")

    try:
        import yt_dlp
    except ImportError as exc:
        raise PipelineError(
            "没有装 yt-dlp。\n"
            "   修复：pip install yt-dlp\n"
            f"   （原始错误：{exc}）"
        ) from exc

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 标题最多取 120 字符；同时加入视频 ID，避免不同视频同标题时复用错文件。
    outtmpl = str(DOWNLOAD_DIR / "%(title).120s [%(id)s].%(ext)s")

    cookie_file = _cookie_file(cookies)
    browser_cookie_spec = _browser_cookie_spec(cookies_from_browser)
    if cookie_file and browser_cookie_spec:
        raise PipelineError("--cookies 与 --cookies-from-browser 只能二选一")

    opts = {
        "format": "bestaudio/worst[acodec!=none][vcodec!=none]/best",
        "outtmpl": outtmpl,
        "noplaylist": True,             # 只下这一集，别把整个播放列表拖下来
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,     # 关掉 yt-dlp 自带进度条，用下面自己的 hook 输出
        "socket_timeout": 30,
        "retries": 3,
        # 关键：传空字符串是 yt-dlp 认可的"禁用代理"写法。
        # 不传这个键的话，yt-dlp 会自己去读 Windows 系统代理，
        # 那样"直连"测试就名不副实了。
        "proxy": proxy or "",
        "overwrites": bool(force),
        "progress_hooks": [_progress_printer()],
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    if browser_cookie_spec:
        opts["cookiesfrombrowser"] = browser_cookie_spec

    def run(extra_opts: dict):
        with yt_dlp.YoutubeDL({**opts, **extra_opts}) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise PipelineError("yt-dlp 没有返回视频信息")
            # 播放列表可能返回 entries
            if "entries" in info:
                info = info["entries"][0]
            path = Path(ydl.prepare_filename(info))
            return info, path

    started = time.time()
    try:
        info, path = run({})
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        # 把最常见的几种失败翻译成人话，而不是原样抛 yt-dlp 的英文
        if any(k in msg.lower() for k in ("unable to download api page",
                                          "connection reset", "timed out",
                                          "failed to resolve", "getaddrinfo")):
            raise PipelineError(
                "下载失败：连不上 YouTube。\n"
                "   最常见原因是没有走代理或被网络阻断。请检查：\n"
                "     1) 代理是否在运行： python check_env.py\n"
                "     2) 显式指定代理：   python pipeline.py URL --proxy http://127.0.0.1:7890\n"
                f"   yt-dlp 原始信息：{msg[:300]}"
            ) from exc
        if "private" in msg.lower() or "sign in" in msg.lower():
            raise PipelineError(
                "下载失败：这个视频需要登录或已设为私享/会员可见。\n"
                "   请使用 --cookies <文件路径>，或 --cookies-from-browser <浏览器>。\n"
                f"   yt-dlp 原始信息：{msg[:300]}"
            ) from exc
        raise PipelineError(f"下载失败（yt-dlp）：{msg[:400]}") from exc
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"下载失败：{type(exc).__name__}: {exc}") from exc

    if not path.exists():
        # prepare_filename 在某些带后处理的情况下会算错扩展名，兜底找同名文件
        cands = sorted(DOWNLOAD_DIR.glob(f"{path.stem}.*"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not cands:
            raise PipelineError(f"下载完成但找不到文件：{path}")
        path = cands[0]

    elapsed = time.time() - started
    size_mb = path.stat().st_size / 1024 / 1024
    title = info.get("title") or path.stem
    duration = info.get("duration") or 0
    print(f"  [OK] 已保存：{path.name}")
    print(f"       大小 {size_mb:.1f} MB，时长 {_fmt_duration(duration)}")
    return path, info


def _progress_printer():
    """yt-dlp 的进度回调：每 10% 打一行，避免刷屏。"""
    state = {"last": -1}

    def hook(d):
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        if not total:
            return
        pct = int(done * 100 / total)
        if pct >= state["last"] + 10 or pct == 100:
            state["last"] = pct
            speed = d.get("speed") or 0
            speed_txt = f"{speed / 1024 / 1024:.1f} MB/s" if speed else "…"
            print(f"       下载 {pct:3d}%  {speed_txt}")

    return hook


# ---------------------------------------------------------------------------
# 3. 提取层
# ---------------------------------------------------------------------------
def require_ffmpeg() -> str:
    """
    确认 ffmpeg 可用，返回可执行文件路径。

    ffmpeg-python【不包含】ffmpeg 本体，它只是个命令行参数构造器，
    所以这里必须真的在 PATH 里找到可执行文件，否则到调用时才报错，
    而且报错信息很难看出是环境问题。
    """
    path = shutil.which("ffmpeg")
    if not path:
        raise PipelineError(
            "找不到 ffmpeg（这是硬依赖，音频提取必须要它）。\n"
            "   Windows：winget install Gyan.FFmpeg\n"
            "            或从 https://www.gyan.dev/ffmpeg/builds/ 下载，把 bin 目录加进 PATH\n"
            "   macOS  ：brew install ffmpeg\n"
            "   Linux  ：sudo apt install ffmpeg\n"
            "   装完记得【重开终端】，然后用 python check_env.py 复查。"
        )
    return path


def extract_audio(source: Path, force: bool = False) -> Path:
    """
    把任意音频/视频容器转成 16 kHz 单声道 16-bit WAV。

    为什么必须转：Whisper 系列模型只接受 16 kHz 单声道。
    直接喂 .webm/.m4a 会得到错误结果或直接报错。

    为什么用 pcm_s16le 而不是别的：
      WAV + 16bit 小端是兼容性最好的组合，阶段三拿它去喂别的工具
      （tube2note / whisper.cpp）不会有格式问题。
    """
    banner("第 3 层 / 提取音频")
    ffmpeg = require_ffmpeg()

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    base = AUDIO_DIR / f"{source.stem}.wav"
    target = _target_for_source(base, source, force)

    if target.exists() and target.stat().st_size > 0 and not force:
        print(f"  [跳过] 已存在：{target.name}"
              f"（{target.stat().st_size / 1024 / 1024:.1f} MB，--force 可强制重做）")
        return target

    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        "-i", str(source),
        "-vn",                          # 丢掉视频流
        "-ac", "1",                     # 单声道
        "-ar", str(SAMPLE_RATE),        # 16 kHz
        "-c:a", "pcm_s16le",            # 16-bit PCM
        str(target),
    ]
    print(f"  ffmpeg -vn -ac 1 -ar {SAMPLE_RATE} -c:a pcm_s16le")

    started = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise PipelineError(f"ffmpeg 调用失败（找不到可执行文件）：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineError("ffmpeg 处理超时（600 秒），文件可能损坏或过大") from exc

    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        raise PipelineError(f"ffmpeg 转换失败（退出码 {proc.returncode}）：\n{tail}")

    if not target.exists() or target.stat().st_size == 0:
        raise PipelineError(f"ffmpeg 报告成功但没生成文件：{target}")

    _save_source_marker(target, source)

    elapsed = time.time() - started
    print(f"  [OK] {target.name}  "
          f"{target.stat().st_size / 1024 / 1024:.1f} MB，耗时 {elapsed:.1f}s")
    return target


# ---------------------------------------------------------------------------
# 4. 转写层
# ---------------------------------------------------------------------------
def load_wav_as_array(wav_path: Path):
    """
    把 16 kHz / 单声道 / 16-bit WAV 读成 float32 数组（取值范围 -1.0 ~ 1.0）。

    为什么不用 faster-whisper 自带的解码见调用处的注释（PyAV 19 不兼容）。
    这里用标准库 wave + numpy，不引入任何额外依赖。

    顺带做格式校验：如果 WAV 不是我们提取层产出的标准格式，直接报错比
    让模型拿到错误采样率的数据、输出一段乱码要强得多。
    """
    import wave

    import numpy as np

    try:
        with wave.open(str(wav_path), "rb") as wf:
            channels = wf.getnchannels()
            rate = wf.getframerate()
            width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())
    except Exception as exc:
        raise PipelineError(f"读取 WAV 失败：{wav_path}\n   {type(exc).__name__}: {exc}") from exc

    if (channels, rate, width) != (1, SAMPLE_RATE, 2):
        raise PipelineError(
            f"WAV 格式不对：{wav_path}\n"
            f"   期望 单声道/{SAMPLE_RATE}Hz/16bit，实际 "
            f"{channels}声道/{rate}Hz/{width * 8}bit\n"
            f"   用 --force 重跑，让提取层重新生成。"
        )

    # int16 -> float32，除以 32768 归一化到 [-1, 1)（Whisper 要求的输入范围）
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def _cuda_libs_present() -> bool:
    """
    快速判断 CUDA 运行库到底在不在，避免白试一次 GPU。

    为什么需要：
      ctranslate2.get_supported_compute_types("cuda") 只反映【编译时】支持，
      不代表运行库存在。这台机器上它返回支持，但一推理就报
      "Library cublas64_12.dll is not found"，而且失败前要耗掉几十秒
      （加载 CUDA 运行时、初始化上下文、报错、清理）。
      实测：100 秒音频因为这次无效尝试，转写从 ~12s 拖到 55s。

    做法：直接尝试加载 cuBLAS 的 DLL。加载不到就说明装不了 CUDA 推理，
    直接走 CPU，省掉那次注定失败的尝试。
    """
    if os.name != "nt":
        return True          # 非 Windows 维持原来的逻辑
    import ctypes

    # pip NVIDIA wheels install DLLs under site-packages/nvidia/*/bin, which
    # Windows does not search automatically when loading by DLL name.
    if hasattr(os, "add_dll_directory"):
        for entry in sys.path:
            nvidia_dir = Path(entry) / "nvidia"
            if not nvidia_dir.is_dir():
                continue
            for dll_dir in nvidia_dir.glob("*/bin"):
                try:
                    _CUDA_DLL_DIR_HANDLES.append(
                        os.add_dll_directory(str(dll_dir)))
                except OSError:
                    continue

    cublas_names = ("cublas64_12.dll", "cublas64_11.dll")
    cudnn_names = ("cudnn64_9.dll", "cudnn64_8.dll")
    cublas_ok = False
    cudnn_ok = False
    for name in cublas_names:
        try:
            ctypes.WinDLL(name)
            cublas_ok = True
            break
        except OSError:
            continue
    for name in cudnn_names:
        try:
            ctypes.WinDLL(name)
            cudnn_ok = True
            break
        except OSError:
            continue
    return cublas_ok and cudnn_ok


def _is_cuda_runtime_error(exc: Exception) -> bool:
    """
    判断异常是不是"GPU 能用但 CUDA 运行库缺失"。

    实测 ctranslate2 4.8.3 在没装 CUDA 运行库的机器上会抛：
        RuntimeError: Library cublas64_12.dll is not found or cannot be loaded

    注意：ctranslate2.get_supported_compute_types("cuda") 返回正常【不代表】能跑，
    它只检查库编译时是否带 CUDA 支持，不检查运行时 DLL 在不在。踩过这个坑。
    """
    text = str(exc).lower()
    keys = ("cublas", "cudnn", "cuda", "not found or cannot be loaded",
            "dll", "libcu")
    return any(k in text for k in keys)


def _device_candidates(mode: str) -> list[tuple[str, str]]:
    """Resolve the requested execution mode into CTranslate2 candidates."""
    mode = (mode or "auto").lower()
    if mode not in DEVICE_MODES:
        raise PipelineError(
            f"未知设备模式：{mode}。可选值：auto、cpu、cuda、npu。")
    if mode == "npu":
        return [("npu", "int8")]
    if mode == "cpu":
        return [("cpu", "int8")]
    if mode == "cuda":
        if not _cuda_libs_present():
            raise PipelineError(
                "已强制选择 CUDA，但 cuBLAS/cuDNN 运行库无法加载。\n"
                "   请确认当前 Python 环境已安装 nvidia-cublas-cu12 和 "
                "nvidia-cudnn-cu12，并更新 NVIDIA 显卡驱动。\n"
                "   或改选“自动”，让程序使用 CPU。")
        return [("cuda", CUDA_COMPUTE_TYPE)]
    candidates = [("cuda", CUDA_COMPUTE_TYPE), ("cpu", "int8")]
    if not _cuda_libs_present():
        candidates = [("cpu", "int8")]
        print("  未检测到 CUDA 运行库（cublas64_12.dll），自动使用 CPU")
    return candidates


_NPU_PIPELINE_CACHE: dict = {}


def _npu_model_path(model_size: str) -> Path:
    """Download an OpenVINO Whisper model when it is not already cached."""
    if model_size not in {"tiny", "base", "small", "medium", "large-v3"}:
        raise PipelineError(f"NPU 不支持未知模型：{model_size}")
    repo = NPU_MODEL_REPO.format(model=model_size)
    target = MODEL_DIR / "openvino" / f"whisper-{model_size}-int8-ov"
    required = (
        "openvino_encoder_model.xml", "openvino_encoder_model.bin",
        "openvino_decoder_model.xml", "openvino_decoder_model.bin",
        "openvino_tokenizer.xml", "openvino_tokenizer.bin",
        "openvino_detokenizer.xml", "openvino_detokenizer.bin",
        "generation_config.json")
    if target.is_dir() and all((target / name).exists() for name in required):
        return target

    endpoint = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
    api_url = f"{endpoint}/api/models/{repo}/tree/main?recursive=true"
    try:
        request = urllib.request.Request(
            api_url, headers={"User-Agent": "video2markdown/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            files = json.load(response)
    except Exception as exc:
        raise PipelineError(
            f"无法获取 NPU 模型清单：{repo}\n"
            f"   当前镜像：{endpoint}\n"
            f"   原始错误：{type(exc).__name__}: {exc}") from exc

    try:
        from huggingface_hub import snapshot_download
        target.parent.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo, local_dir=str(target), endpoint=endpoint,
                           max_workers=4)
    except Exception as exc:
        raise PipelineError(
            f"NPU 模型下载失败：{repo}\n"
            f"   原始错误：{type(exc).__name__}: {exc}") from exc
    if not all((target / name).exists() for name in required):
        raise PipelineError(f"NPU 模型下载完成但文件不完整：{target}")
    return target


def _get_npu_pipeline(model_size: str):
    """Create and cache an OpenVINO Whisper pipeline on Intel NPU."""
    if model_size not in _NPU_PIPELINE_CACHE:
        try:
            import openvino_genai as ov_genai
        except ImportError as exc:
            raise PipelineError(
                "没有安装 OpenVINO NPU 后端。\n"
                "   请使用南京大学 PyPI 镜像安装：\n"
                "   pip install -i https://mirrors.nju.edu.cn/pypi/web/simple "
                "openvino openvino-genai") from exc
        model_path = _npu_model_path(model_size)
        try:
            _NPU_PIPELINE_CACHE[model_size] = ov_genai.WhisperPipeline(
                str(model_path), "NPU", word_timestamps=True)
        except Exception as exc:
            raise PipelineError(
                f"Intel NPU 初始化失败：{type(exc).__name__}: {exc}\n"
                "   请确认 Intel NPU 驱动和 OpenVINO NPU 插件已安装。") from exc
    return _NPU_PIPELINE_CACHE[model_size]


# 模型实例缓存。
# 分块转写（CHUNK_SEC=60）后，一块一次 _run_once，如果每次都新建 WhisperModel，
# 24 分钟视频就要重复加载 24 次模型（small 每次约 2.5 秒，白等一分钟）。
# 按 (模型, 设备, 精度) 缓存，同一轮转写只加载一次。
_MODEL_CACHE: dict = {}


# 语言检测专用模型。detect_language 只需要跑一遍编码器，
# 用 base 代替用户选的 medium/large 能快 3~5 倍，判定结果几乎一样
# （检测的是"这段像哪种语言"，不需要大模型的表达能力）。
def _cpu_threads() -> int:
    """
    CPU 推理线程数。faster-whisper 默认 0（=4 线程），这台机器有 20 个物理核，
    明显吃不饱。取物理核数的 3/4 并封顶 16：再多收益递减，还会和 VAD/解码抢线程。
    """
    try:
        import os as _os
        n = _os.cpu_count() or 4
        return max(4, min(16, int(n * 0.75)))
    except Exception:
        return 4


def _get_model(model_cls, model_size: str, device: str, compute_type: str):
    key = (model_size, device, compute_type)
    # 用 CUDA 时必须先调用一次 _cuda_libs_present()：它不只是"检测"，
    # 还会把 pip 装到 site-packages/nvidia/*/bin 的目录注册进 DLL 搜索路径。
    # 少了这一步，模型能加载但第一次推理就报
    # "Library cublas64_12.dll is not found or cannot be loaded"。
    if device == "cuda":
        _cuda_libs_present()

    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = model_cls(
            model_size, device=device, compute_type=compute_type,
            cpu_threads=_cpu_threads(),
            download_root=str(MODEL_DIR))
    return _MODEL_CACHE[key]


def _run_once_npu(model_size: str, audio, lang: str | None = None,
                  prompt: str | None = None, base_offset: float = 0.0,
                  chunk_idx: int = 0, chunk_total: int = 1,
                  on_segment=None, skip_before: float = -1.0,
                  spans: list | None = None):
    """Transcribe one chunk through OpenVINO GenAI on Intel NPU."""
    import numpy as np
    from types import SimpleNamespace

    # OpenVINO WhisperPipeline has no faster-whisper VAD filter. Avoid feeding
    # near-silent chunks to the decoder, which otherwise tends to hallucinate a
    # short word on empty audio.
    if float(np.sqrt(np.mean(np.square(audio)))) < 0.005:
        return [], SimpleNamespace(
            duration=len(audio) / SAMPLE_RATE,
            language=lang or "?",
            language_probability=0.0,
        ), "npu/int8", (lang or "?")

    pipeline = _get_npu_pipeline(model_size)
    kwargs = {
        "return_timestamps": True,
        "task": "transcribe",
        "no_repeat_ngram_size": 3,
    }
    if lang:
        kwargs["language"] = lang
        kwargs["initial_prompt"] = prompt or prompt_for(lang)
    if prompt:
        kwargs["initial_prompt"] = prompt
    try:
        result = pipeline.generate(np.asarray(audio, dtype=np.float32).tolist(),
                                   **kwargs)
    except Exception as exc:
        raise PipelineError(
            f"Intel NPU 转写失败：{type(exc).__name__}: {exc}") from exc

    detected = (getattr(result, "language", None) or lang or "?").lower()
    probability = 0.0
    segments = []
    for chunk in result.chunks:
        text = getattr(chunk, "text", "") or ""
        if text and detected.startswith("zh"):
            text = to_simplified(text)
        start = round(float(chunk.start_ts) + base_offset, 3)
        end = round(float(chunk.end_ts) + base_offset, 3)
        item = {"start": start, "end": end, "text": text}
        if spans:
            item["speaker"] = diarize.best_speaker(spans, start, end)
        if skip_before >= 0 and start < skip_before - 0.05:
            continue
        segments.append(item)
        if on_segment is not None:
            on_segment(item)
    return segments, SimpleNamespace(
        duration=len(audio) / SAMPLE_RATE,
        language=detected,
        language_probability=probability,
    ), "npu/int8", detected


def _run_once(model_cls, model_size: str, device: str, compute_type: str,
              audio, lang: str | None = None, prompt: str | None = None,
              base_offset: float = 0.0,
              chunk_idx: int = 0, chunk_total: int = 1,
              on_segment=None, skip_before: float = -1.0,
              spans: list | None = None):
    """
    在一个音频片段上完整跑一遍识别。

    base_offset  这一块在原音频里的起始秒数。片段内时间戳要加上它，
                 否则每块都从 0 开始，合并后时间轴就乱了。
    lang         None = 在这一块内部自己检测语言并挑提示词。
                 混合语言靠它：整块锁死一个语言时，另一种语言会被"翻译"掉
                 或变成复读幻觉（实测英文词从 116 掉到 1）。

    返回 (segments, info, 用了哪个设备, 最终语言)。
    """
    if device == "npu":
        return _run_once_npu(
            model_size, audio, lang=lang, prompt=prompt,
            base_offset=base_offset, chunk_idx=chunk_idx,
            chunk_total=chunk_total, on_segment=on_segment,
            skip_before=skip_before, spans=spans)

    model = _get_model(model_cls, model_size, device, compute_type)

    # 每块单独检测语言。这一步是混合语言场景的关键：
    # 整段转写时模型会延续上一段的语言上下文，把纯英文段念成"中文字幕"复读。
    # 逐块检测 + 逐块给对应提示词后，英文段能正常识别。
    if lang is None:
        try:
            detected, det_prob, _all = model.detect_language(audio=audio)
            lang = detected
        except Exception as exc:
            print(f"       [警告] 语言检测失败（{type(exc).__name__}），按自动处理")

    if prompt is None:
        prompt = prompt_for(lang)

    raw_segments, info = model.transcribe(
        audio,
        language=lang,
        initial_prompt=prompt,
        beam_size=1,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        # --- 反"捏造/复读"三件套 ---
        # condition_on_previous_text：默认 True —— 缺点是会把上一段的复读当上下文继续滚下去。
# 曾经测试过关掉它会加重繁体和丢句，但那是【加入中文提示词和 OpenCC 之前】的结论。
# 现在两层兜底都在，重新权衡（见 CONDITION_ON_PREV）。
#
# no_repeat_ngram_size：禁止同一个 3-gram 反复出现，专治复读循环
        # hallucination_silence_threshold：静音超过 2 秒时丢弃该段"幻听产物"
        #   （长视频里没人说话的段落，模型会凭空编内容，这是最常见的捏造来源）
        # condition_on_previous_text 保持 True：实测关掉反而让繁体和丢句变严重
        no_repeat_ngram_size=3,
        hallucination_silence_threshold=2.0,
    )

    # info 在生成器消费前就有，直接拿它的语言判断要不要转简体
    final_lang = (lang or getattr(info, "language", "") or "").lower()
    do_simplify = final_lang.startswith("zh")

    # segments 是生成器，必须在这里消费掉，否则计时只算了"开始"的时间。
    # 每 20 条打一次【全局】进度（块内百分比换算到整段音频）。
    total_sec = info.duration or 0
    segs = []
    for seg in raw_segments:
        text = seg.text or ""
        if text and do_simplify:
            text = to_simplified(text)
        abs_start = (seg.start or 0.0) + base_offset
        abs_end = (seg.end or 0.0) + base_offset
        item = {
            "start": round(abs_start, 3),
            "end": round(abs_end, 3),
            "text": text,
        }
        if spans:
            item["speaker"] = diarize.best_speaker(spans, abs_start, abs_end)
        # 续跑时开头会与上次已存的部分重叠，这段丢掉避免重复
        if skip_before >= 0 and item["start"] < skip_before - 0.05:
            continue
        segs.append(item)
        if on_segment is not None:
            on_segment(item)
        if total_sec and len(segs) % 20 == 0:
            local = min(0.99, (seg.end or 0) / total_sec)
            glob = int((chunk_idx + local) / max(1, chunk_total) * 100)
            print(f"       转写进度 {min(glob, 99):3d}%   "
                  f"（第 {chunk_idx + 1}/{chunk_total} 块）", flush=True)
    return segs, info, f"{device}/{compute_type}", final_lang


@dataclass
class Seg:
    """一条字幕。检查点里恢复出来的数据也用它，好让段落合并逻辑统一处理。"""
    start: float
    end: float
    text: str
    speaker: int | None = None      # 说话人编号（0 起），没做分离时为 None


# ---------------------------------------------------------------------------
# 断点续跑：分块 + 检查点
# ---------------------------------------------------------------------------
def _ckpt_path(wav: Path) -> Path:
    return wav.with_suffix(".segments.json")


def _ckpt_signature(wav: Path, model_size: str, lang: str | None,
                    device_mode: str = "auto", diarize_sig: str = "") -> str:
    """
    检查点的"指纹"。音频文件、模型、语言、分块大小任何一个变了，
    旧结果都不能复用（否则会把不同模型/不同参数的结果混在一起）。
    """
    st = wav.stat()
    return (f"v{CKPT_VERSION}|{model_size}|{lang or 'auto+multi'}|{device_mode}"
            f"|{diarize_sig}|{CHUNK_SEC}|{st.st_size}|{int(st.st_mtime)}")


def _load_ckpt(wav: Path, sig: str) -> list:
    """读已存的字幕列表。签名不符则丢弃。"""
    p = _ckpt_path(wav)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [提示] 检查点读不了（{type(exc).__name__}），从头开始")
        return []
    if data.get("signature") != sig:
        print("  [提示] 参数和上次不同（音频/模型/语言），检查点作废，从头开始")
        return []
    segs = data.get("segments") or []
    segs.sort(key=lambda d: d["start"])
    return segs


def _save_ckpt(wav: Path, sig: str, segments: list) -> None:
    """
    原子写：先写 .tmp 再 os.replace 替换。
    直接写目标文件时，中途被强杀会留下半截 JSON，下次读就崩。
    """
    p = _ckpt_path(wav)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"signature": sig, "segments": segments},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:
        print(f"  [警告] 检查点保存失败：{exc}")


# ---------------------------------------------------------------------------
# 语言断点切分：让每一段"语言单一"
# ---------------------------------------------------------------------------
# 为什么必须做（真实现场）：
#   某支视频 0~30 秒是中文片头 + 英文采访。整块检测语言 = zh（置信度 0.86），
#   于是 Whisper 用中文模式去转英文音频 —— 它不会报错，而是【把英文翻译成中文】：
#
#     原文：What are we calling this big event...? Has the event got a name?
#     输出：我们在最初的奖励中，我们在这一开始，我们在评测数十多个评论中，
#           我们有没有一个名字？
#
#   这是最坏的失败方式：内容看着通顺，但已经不是原话，而且没法靠后处理修回来。
#
# 做法：检测置信度不够就把这一块对半切开，各自再检测，直到置信度够高或到达
# 最小长度。切出来的每一段带着自己的语言 token 去转写，就不会互相污染。

# 固定 30 秒起步。
#
# 试过改成 60 秒（想省掉一半检测次数），结果【更慢也更差】：
#   30 秒起步：合计 49.5s，58 条字幕，混排段被正确切开
#   60 秒起步：合计 59.3s，73 条字幕，混排段没切开 -> 英文被当中文转，输出更长更慢
# 原因：60 秒起步时那段的置信度是 0.86 会递归，但递归出来的两段同语言又被合并回
# 60 秒，等于没切。30 秒起步时同样递归，但切完的两段语言不同，能保留。
# 所以这个值不能动，动了切分就失效。
LANG_SPLIT_PIECE = 30.0
LANG_SPLIT_CONF = 0.90      # 置信度低于这个值就继续对半切
LANG_SPLIT_FINE = 8.0       # 最细切到这么短（再短转写质量会下降）
LANG_SPLIT_MIN_SEC = 12.0   # 合并时短于这个值的孤立段不单独成段


def _language_split(model, audio, rate: int, ranges: list,
                    piece_sec: float = LANG_SPLIT_PIECE,
                    conf: float = LANG_SPLIT_CONF,
                    fine_sec: float = LANG_SPLIT_FINE,
                    min_sec: float = LANG_SPLIT_MIN_SEC) -> list:
    """
    按语言把 ranges 切成"语言尽量单一"的小段。

    返回 [(起样本, 止样本, 语言 或 None), ...]，语言 None 表示"切到最细仍不确定"，
    交给 _run_once 自己检测。

    ## 两步走，缺一不可

    第一步【固定切成 30 秒】：保证行为与设备无关。
      实测 CPU(int8) 和 GPU(int8_float16 / float16) 的 detect_language 结果
      几乎完全一致（10 段里只有 1 段真正含糊的会不同），所以固定粒度是安全的。

    第二步【置信度低于 0.90 时继续对半切】：这一步不能省。
      实测某支视频 0~30 秒是中文片头 + 英文采访，检测给 zh（0.86）。
      只切 30 秒的话，0-30 和 30-60 都判成 zh，合并回去等于没切，
      整段被当中文转写 —— 里面的英文会被【翻译成中文】。
      继续往下切到 15 秒后，前半段 zh（0.9x）、后半段 en（0.9x），问题才解决。

    第三步【合并相邻同语言 + 短孤段并入前段】：避免把 60 秒的话切成 4 段，
    转写质量会下降。
    """
    out: list = []
    fine_n = int(fine_sec * rate)

    def rec(a: int, b: int) -> None:
        if b - a < rate * 2:
            return
        lang, prob = None, 0.0
        try:
            lang, prob, _ = model.detect_language(audio=audio[a:b])
        except Exception:
            out.append([a, b, None])
            return
        if prob >= conf or (b - a) <= fine_n:
            out.append([a, b, lang if prob >= conf else None])
            return
        mid = a + (b - a) // 2
        rec(a, mid)
        rec(mid, b)

    for s, e in ranges:
        s, e = int(s), int(e)
        step = max(int(piece_sec * rate), rate)
        cur = s
        while cur < e:
            nxt = min(cur + step, e)
            rec(cur, nxt)
            cur = nxt

    # 合并相邻同语言。上限 2 个块长：不加限制的话，1 小时纯中文视频会被合并成
    # 一整段 —— 进度条没有中间点、断点续跑也失去粒度。
    cap = int(piece_sec * 2 * rate)
    merged: list = []
    for a, b, lang in out:
        if (merged and merged[-1][2] is not None and merged[-1][2] == lang
                and (b - merged[-1][0]) <= cap):
            merged[-1][1] = b
        else:
            merged.append([a, b, lang])

    # 太短的孤段并进前一段
    min_n = int(min_sec * rate)
    fixed: list = []
    for a, b, lang in merged:
        if fixed and (b - a) < min_n:
            fixed[-1][1] = b
        else:
            fixed.append([a, b, lang])

    return [(a, b, lang) for a, b, lang in fixed]


def _chunk_ranges(audio, rate: int, chunk_sec: float) -> list:
    """
    把音频切成若干块，切口尽量落在静音处。

    为什么不硬切在 5 分钟整数倍上：那样正好切断一个词，半个词的后半截
    会跑到下一块开头，变成"…我们来看"+"看结果吧"这种残句。
    做法：在目标点前后 ±2 秒里逐 0.1 秒算平均能量，挑最安静的点切。
    """
    import numpy as np

    total = len(audio)
    win = max(1, int(rate * 0.1))
    tol = int(rate * 2.0)
    target = int(rate * chunk_sec)
    if target <= 0 or total <= target * 1.2:
        return [(0, total)]          # 不够两块，别切了

    edges = [0]
    while True:
        pos = edges[-1] + target
        if pos >= total - win:
            break
        lo = max(edges[-1] + win * 2, pos - tol)
        hi = min(total - win, pos + tol)
        if hi - lo <= win:
            edges.append(pos)
            continue
        idx = np.arange(lo, hi - win, win)
        if len(idx) == 0:
            edges.append(pos)
            continue
        energies = [float(np.abs(audio[i:i + win]).mean()) for i in idx]
        edges.append(int(idx[int(np.argmin(energies))]))
    edges.append(total)
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)
            if edges[i + 1] > edges[i]]


def transcribe(wav: Path, model_size: str = WHISPER_MODEL,
               lang: str | None = None, resume: bool = True,
               device_mode: str = "auto",
               with_speakers: bool = False,
               diarize_threshold: float = diarize.DEFAULT_THRESHOLD,
               num_speakers: int = -1,
               proxy: str | None = None):
    """
    用 faster-whisper 做语音识别，返回 (段落列表, 识别信息, 耗时秒)。

    【断点续跑】按【段】存档，不是按块。
    转写占整条流水线 80% 的时间，中途停止后必须能接着跑。最初的实现是
    "每 5 分钟一块，块转完才存"，结果用 small 模型时一块要 ~40 秒，
    用户 30 秒内点停止等于什么都没存 —— 表现为"停止后重启还是从头来"。
    现在改成：每产生 CKPT_SAVE_EVERY 条字幕就存一次（约 5 秒一次），
    续跑点精确到"最后一条字幕结束的时间"，并且从那个时间点往回退
    RESUME_OVERLAP 秒重新转，避免正好切在词中间；重叠部分按时间戳去重。

    其它关键参数：
      vad_filter=True   掐掉静音段（长视频静音多，不掐既慢又容易"幻听"）
      beam_size=1       贪心解码；默认 5 慢 3~5 倍而准确率提升有限
      hybrid           逐块检测语言（CHUNK_SEC=60）。整段一次性转写会让
                        纯英文段变成中文复读幻觉，必须逐块做
      initial_prompt    按语言给"书面语示例"，中文决定简体与标点
    """
    banner("第 4 层 / 语音转文本")
    engine = "OpenVINO Whisper" if device_mode == "npu" else "faster-whisper"
    print(f"  模型：{engine} {model_size}（缓存在 {MODEL_DIR}）")

    WhisperModel = None
    if device_mode != "npu":
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise PipelineError(
                "没有装 faster-whisper。\n"
                "   修复：pip install faster-whisper\n"
                f"   （原始错误：{exc}）"
            ) from exc

    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    audio = load_wav_as_array(wav)
    rate = SAMPLE_RATE
    total_sec = len(audio) / rate
    print(f"  音频 {_fmt_duration(total_sec)}")

    # 说话人分离（可选）：放在转写【之前】做。
    # 这样每条字幕落下时就带着 speaker，断点存档里也有，续跑不用重做分离。
    spans: list = []
    if with_speakers:
        print("  说话人分离…（首次会下载 34MB 模型）")
        try:
            spans, spk_count, spk_sec = diarize.run(
                audio, MODEL_DIR, threshold=diarize_threshold,
                num_speakers=num_speakers, proxy=proxy)
        except diarize.DiarizeError as exc:
            raise PipelineError(f"说话人分离失败：\n{exc}") from exc
        print(f"  [OK] 检测到 {spk_count} 个说话人（{len(spans)} 段），"
              f"分离耗时 {spk_sec:.1f}s")
        if num_speakers <= 0:
            print("       注意：人数是自动估算的，偏多的可能性较大。"
                  "已知人数时用 --num-speakers N 会准确得多。")

    diarize_sig = (f"spk{num_speakers}@{diarize_threshold}" if with_speakers else "spkoff")
    sig = _ckpt_signature(wav, model_size, lang, device_mode, diarize_sig)
    saved = _load_ckpt(wav, sig) if resume else []
    if not resume and _ckpt_path(wav).exists():
        print("  已指定 --no-resume，忽略旧检查点")

    # 续跑点 = 已存最后一条字幕的结束时间
    resume_at = saved[-1]["end"] if saved else 0.0
    if resume_at > 0:
        print(f"  [续跑] 已有 {len(saved)} 条字幕，"
              f"从 {_fmt_duration(resume_at)} 处继续（共 {_fmt_duration(total_sec)}）")
    elif _ckpt_path(wav).exists():
        print("  没有可复用的进度，从头开始")

    # 还没转的区间（从续跑点往回退一点，避免切断词）
    start_sec = max(0.0, resume_at - RESUME_OVERLAP) if resume_at > 0 else 0.0
    remaining = audio[int(start_sec * rate):]
    ranges = [(s + int(start_sec * rate), e + int(start_sec * rate))
              for s, e in _chunk_ranges(remaining, rate, CHUNK_SEC)]

    n = len(ranges)
    print(f"  待转 {_fmt_duration(total_sec - start_sec)}，分 {n} 段处理"
          f"（每段约 {CHUNK_SEC // 60} 分钟，每 {CKPT_SAVE_EVERY} 条字幕存一次）")

    # 语言策略：
    #   · 不指定语言 -> 每一块自己检测语言、自己挑提示词
    #     混合语言必须这样：整段转写会让模型延续上一段语言，
    #     把纯英文段念成"中文字幕/中文文字"复读（实测英文词 116 -> 1）
    #   · 指定语言   -> 全部锁死该语言
    forced_lang = lang
    if forced_lang is None:
        print(f"  语言：逐块自动检测（每块 {CHUNK_SEC} 秒，适合混合语言）")
    else:
        print(f"  语言：锁定 {forced_lang}（提示词已按该语言匹配）")

    candidates = _device_candidates(device_mode)
    print(f"  设备模式：{device_mode}（候选：{', '.join(f'{d}/{t}' for d, t in candidates)}）")
    # 语言断点切分：把"中英混在一块"的区间继续对半切开，让每段语言单一。
    # 不这么做的话，一块里只要中文占多数，Whisper 就会把同一块里的英文
    # 【翻译成中文】—— 实测过，内容看着通顺但已经不是原话。
    pieces = [(s, e, None) for s, e in ranges]
    if forced_lang is None:
        try:
            # 用 base 模型做检测（而不是用户选的 medium/large）：
            # 实测同一支 6 分钟视频，medium 做检测单项要 ~160s（不在阶段明细里，
            # 所以显示成"4·转写 193.7s"但明细只有 33.2s）。base 快 3~5 倍。
            # 检测用和转写同一个模型。
            # 试过换成 base（想更快），实测【更差也更慢】：
            #   medium 检测：合计 49.5s，58 条字幕
            #   base   检测：合计 54.7s，83 条字幕（置信度不同 -> 递归更多 ->
            #               切得更碎 -> 分块变多 -> 转写反而慢）
            # 所以不做这个替换，只保留下面的耗时打印。
            lang_model = _get_model(WhisperModel, model_size, *candidates[0])
            # 检测耗时必须打出来。
            # 之前它不计入任何阶段，用户看到"4·转写 193.7s"但明细只有 33.2s，
            # 中间差的 160s 完全隐形 —— 排查时根本不知道时间去哪了。
            _t_detect = time.time()
            pieces = _language_split(lang_model, audio, rate, ranges)
            print(f"  语言检测耗时 {time.time() - _t_detect:.1f}s"
                  f"（{'GPU' if candidates[0][0] == 'cuda' else 'CPU'}）")
            langs = [p[2] for p in pieces if p[2]]
            if len(pieces) > len(ranges):
                print(f"  语言断点：{len(ranges)} 块 -> {len(pieces)} 段"
                      f"（检测到 {', '.join(sorted(set(langs))) or '不确定'}）")
        except Exception as exc:
            print(f"  [警告] 语言断点切分跳过：{type(exc).__name__}: {exc}")

    n = len(pieces)

    chosen = None
    last_info = None
    last_lang = forced_lang
    pending = [0]                 # 距上次存盘新增的条数

    last_save = [time.time()]

    def persist(item=None):
        """
        每来一条字幕就累加，满足任一条件就落盘：
          · 自上次存档又攒了 CKPT_SAVE_EVERY 条（少写磁盘）
          · 距上次存档超过 CKPT_SAVE_SEC 秒（保证"随便什么时候停都留下了进度"）

        只用条数阈值是不够的：small 模型一段 30 秒音频要算 4 秒左右，
        10 条字幕要 40 秒，用户在前 40 秒内点停止仍然什么都没存 —— 
        这正是"停止后重启还是从头来"的原因。
        """
        if item is not None:
            saved.append(item)
            pending[0] += 1
        now = time.time()
        if pending[0] and (pending[0] >= CKPT_SAVE_EVERY
                           or (now - last_save[0]) >= CKPT_SAVE_SEC):
            _save_ckpt(wav, sig, saved)
            pending[0] = 0
            last_save[0] = now

    started = time.time()
    for idx in range(n):
        s, e, piece_lang = pieces[idx]
        print(f"  --- 第 {idx + 1}/{n} 段："
              f"{_fmt_duration(s / rate)} ~ {_fmt_duration(e / rate)}")
        tries = [chosen] if chosen else candidates
        last_exc = None
        for device, ctype in tries:
            try:
                segs, info, used, last_lang = _run_once(
                    WhisperModel, model_size, device, ctype, audio[s:e],
                    lang=forced_lang or piece_lang, prompt=None,
                    base_offset=s / rate,
                    chunk_idx=idx, chunk_total=n,
                    on_segment=persist,
                    skip_before=resume_at if idx == 0 else -1.0,
                    spans=spans)
                chosen = (device, ctype)
                last_info = info
                if idx == 0:
                    print(f"  [OK] 使用 {used}")
                break
            except Exception as exc:
                last_exc = exc
                if (device != "cuda" or not _is_cuda_runtime_error(exc)
                        or device_mode != "auto"):
                    raise PipelineError(_explain_model_error(exc)) from exc
                print(f"  [警告] CUDA 推理失败：{str(exc)[:88]}")
                print("         缺的是 CUDA 运行库（cuBLAS/cuDNN），不是显卡驱动。")
                print("         本次自动退回 CPU（int8）——结果一样，只是慢一些。")
                chosen = None
        else:
            raise PipelineError(_explain_model_error(last_exc) if last_exc
                                else "模型加载与推理均失败")
        _save_ckpt(wav, sig, saved)
        pending[0] = 0
        last_save[0] = time.time()
        print(f"  [已存] 第 {idx + 1}/{n} 段完成，累计 {len(saved)} 条"
              f" —— 现在停止也不会白跑")

    saved.sort(key=lambda d: d["start"])
    all_segs = [Seg(d["start"], d["end"], d["text"], d.get("speaker")) for d in saved]

    # 混排细分：把缺标点的中文段单独拎出来重转。
    # 放在检查点【之后】做 —— 存档里存的是原始结果，每次运行都重新细分，
    # 续跑和首次运行得到的结果一致。
    if all_segs and not _NO_REFINE and WhisperModel is not None:
        try:
            refine_model = _get_model(WhisperModel, model_size,
                                      *(chosen or ("cpu", "int8")))
            before = len(all_segs)
            all_segs = refine_language_runs(all_segs, audio, rate, refine_model)
            if len(all_segs) != before:
                print(f"       混排细分后共 {len(all_segs)} 条字幕")
        except Exception as exc:
            print(f"       [警告] 混排细分整体跳过：{type(exc).__name__}: {exc}")

    elapsed = time.time() - started
    audio_sec = total_sec or 0
    speed = (audio_sec / max(elapsed, 0.001))

    from types import SimpleNamespace
    info = SimpleNamespace(
        duration=audio_sec,
        language=getattr(last_info, "language", last_lang or "?"),
        language_probability=getattr(last_info, "language_probability", 0.0),
    )

    print(f"  [OK] 共 {len(all_segs)} 条字幕")
    print(f"       语言 {info.language}（置信度 {info.language_probability:.2f}）")
    print(f"       音频 {_fmt_duration(audio_sec)}，本次耗时 {elapsed:.1f}s，"
          f"实时倍率 {speed:.1f}x")
    if resume_at > 0:
        print(f"       本次从 {_fmt_duration(resume_at)} 处接着转，"
              f"省下前面 {_fmt_duration(resume_at)} 音频的转写时间")
    return all_segs, info, elapsed


# ---------------------------------------------------------------------------
# 混排细分：把"缺标点的中文段"单独重转
# ---------------------------------------------------------------------------
# 为什么需要（真实现场，一段中英混排视频的 60 秒块）：
#   整块转 -> 这段陶哲宣的访谈是数学频道NumberDales在10月15号放出来的。上次我们
#            检查是9月初他说数学家要的其实有两样答案和理解以前这两样是绑在一起的
#   单独转 -> 这段陶哲轩的访谈是数学频道 Numberphile 在 10 月 15 号放出来的。
#
# 同一个模型、同一段音频，差别只在于"有没有和英文抢上下文"。
# 混排块里模型会放弃断句，人名和数字也跟着错。
#
# 做法：转完之后扫一遍字幕，把【中文占多数、且几乎没有标点】的连续片段
# 单独拎出来、按中文重新转一遍（带中文提示词）。

REFINE_MIN_SEC = 2.5        # 短于这个长度的片段不值得重转
REFINE_PAD = 0.4            # 前后各留 0.4 秒，避免把词切在边界上
REFINE_MAX_RATIO = 0.5      # 需要重转的总时长超过音频一半就放弃，交给后处理
_NO_REFINE = False          # 由 --no-refine 置为 True


def _seg_text(seg) -> str:
    return seg["text"] if isinstance(seg, dict) else (seg.text or "")


def refine_language_runs(segs, audio, rate: int, model, verbose: bool = True):
    """
    把缺标点的中文连续片段单独重转，返回新的片段列表。

    触发条件（同时满足）：
      · 片段里中文占多数（_cjk_ratio >= 0.35）
      · 连续长度 >= REFINE_MIN_SEC
      · 标点密度 < 0.03（即每 30 多个字才 1 个标点，基本等于没断句）

    不触发的情况一律原样保留：纯英文、已有标点的中文、很短的插话。
    """
    if not segs or model is None:
        return segs

    def ratio(s):
        return _cjk_ratio(_seg_text(s))

    def density(s):
        return _punct_density(_seg_text(s))

    total_audio = len(audio) / rate if rate else 0.0
    budget = total_audio * REFINE_MAX_RATIO
    used = 0.0
    out, refined, skipped = [], 0, 0
    i, n = 0, len(segs)

    while i < n:
        if ratio(segs[i]) < 0.35:
            out.append(segs[i])
            i += 1
            continue

        j = i
        while j < n and ratio(segs[j]) >= 0.35:
            j += 1
        run = segs[i:j]
        span = run[-1].end - run[0].start
        text = "".join(_seg_text(s) for s in run)

        trigger = (span >= REFINE_MIN_SEC
                   and _punct_density(text) < 0.03
                   and span <= max(budget - used, 0.0))
        if trigger:
            # 先确认这段【音频】真的是中文。否则可能是"英文被当中文转成了中文"，
            # 那种情况下再按中文重转只会越走越偏。
            try:
                seg_lang, seg_prob, _ = model.detect_language(
                    audio=audio[int(run[0].start * rate):int(run[-1].end * rate)])
                if seg_lang != "zh":
                    if verbose:
                        print(f"       [混排细分] {_fmt_clock(run[0].start)} 起这段"
                              f"音频其实是 {seg_lang}（{seg_prob:.2f}），跳过细分")
                    trigger = False
            except Exception:
                pass
        if not trigger:
            if span >= REFINE_MIN_SEC and _punct_density(text) < 0.03:
                skipped += 1
            out.extend(run)
            i = j
            continue

        start = max(0.0, run[0].start - REFINE_PAD)
        end = min(total_audio, run[-1].end + REFINE_PAD)
        try:
            got, _info = model.transcribe(
                audio[int(start * rate):int(end * rate)],
                language="zh",
                initial_prompt=prompt_for("zh"),
                beam_size=1,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                no_repeat_ngram_size=3,
                hallucination_silence_threshold=2.0,
                condition_on_previous_text=CONDITION_ON_PREV,
            )
            fresh = []
            for s in got:
                txt = (s.text or "").strip()
                if not txt:
                    continue
                fresh.append(Seg(round(start + s.start, 3), round(start + s.end, 3), txt,
                                 getattr(run[0], "speaker", None)))
            if not fresh:                       # 重转没结果就别乱改
                out.extend(run)
                i = j
                continue
            used += span
            refined += 1
            if verbose:
                before = _punct_density(text)
                after = _punct_density("".join(s.text for s in fresh))
                print(f"       [混排细分] {_fmt_clock(start)}~{_fmt_clock(end)} "
                      f"中文段重转：{len(run)} 条 -> {len(fresh)} 条，"
                      f"标点密度 {before:.3f} -> {after:.3f}")
            out.extend(fresh)
        except Exception as exc:
            if verbose:
                print(f"       [警告] 混排细分失败（保留原结果）："
                      f"{type(exc).__name__}: {exc}")
            out.extend(run)
        i = j

    if verbose and skipped:
        print(f"       [混排细分] 另有 {skipped} 段达到触发条件但超出预算，"
              f"交给后面标点恢复处理")
    return out


def _explain_model_error(exc: Exception) -> str:
    """把模型加载/推理阶段的异常翻译成可操作的提示。"""
    text = str(exc)
    if any(k in text.lower() for k in ("connect", "timeout", "http", "resolve",
                                       "couldn't connect")):
        return (
            "模型下载失败（huggingface.co 在国内经常连不上）。\n"
            "   设置镜像后重试：\n"
            "     PowerShell:  $env:HF_ENDPOINT='https://hf-mirror.com'\n"
            "     cmd:         set HF_ENDPOINT=https://hf-mirror.com\n"
            "     bash:        export HF_ENDPOINT=https://hf-mirror.com\n"
            f"   原始错误：{type(exc).__name__}: {text[:200]}"
        )
    if _is_cuda_runtime_error(exc):
        return (
            "CUDA 推理失败，且退回 CPU 也失败了。\n"
            "   请确认当前 Python 环境中的 nvidia-cublas-cu12 / "
            "nvidia-cudnn-cu12 DLL 可加载，并更新 NVIDIA 显卡驱动。\n"
            f"   原始错误：{type(exc).__name__}: {text[:200]}"
        )
    return f"转写失败：{type(exc).__name__}: {text[:400]}"


# ---------------------------------------------------------------------------
# 5. 输出层
# ---------------------------------------------------------------------------
def group_paragraphs(segments, max_chars: int = PARAGRAPH_CHARS,
                     gap: float = PARAGRAPH_GAP):
    """
    把逐条字幕合并成可读段落，返回 [(起始秒, 结束秒, 文本, 说话人), ...]。

    为什么不能一条字幕一个标题：
        一节 40 分钟的课有 1000+ 条字幕，每条都 `## 时间` 会得到一份
        没法阅读、也没法当目录用的 Markdown。

    分段规则（满足任一就另起一段）：
        · 当前段字数达到 max_chars
        · 与上一条字幕的间隔超过 gap 秒（说话人停顿，是天然段落边界）
        · 说话人变了 —— 做过说话人分离时必须切，否则两个人混在同一段里读不出来

    说话人为 None（没开分离）时，行为与之前完全一致。
    """
    blocks = []
    buf: list[str] = []
    start = prev_end = None
    chars = 0
    speaker = None

    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        spk = getattr(seg, "speaker", None)
        switched = buf and spk != speaker
        if buf and (chars >= max_chars or (seg.start - prev_end) > gap or switched):
            blocks.append((start, prev_end, _join_text(buf), speaker))
            buf, chars, start = [], 0, None
        if start is None:
            start = seg.start
            speaker = spk
        buf.append(text)
        chars += len(text)
        prev_end = seg.end

    if buf and start is not None:
        blocks.append((start, prev_end, _join_text(buf), speaker))
    return blocks




# ---------------------------------------------------------------------------
# 文本后处理：中文标点、中英间距
# ---------------------------------------------------------------------------
# 问题现场（真实输出，一段中英混排视频）：
#   ## 00:00:24 ~ 00:00:37
#   这段陶哲宣的访谈是数学频道Numberphile在10月15号放出来的。上次我们检查是9月初
#   他说数学家要的其实有两样答案和理解以前这两样是绑在一起的这次答案先到了
#
# 三个毛病：
#   1. 140 个中文字里只有 1 个标点 —— 读起来是一整块，断不开
#   2. 中英文直接贴着：`数学频道Numberphile在10月15号`
#   3. 把同一段音频【单独】拿出来转，标点就有了（4 个），连人名都从
#      "陶哲宣"修回"陶哲轩" —— 说明问题出在"整块混排"，不是提示词
#
# 修法分两层：
#   · 纯文本层（总是生效，无依赖）：中英之间补空格、中文语境的半角标点转全角
#   · 模型层（可选，首次用会下 62MB）：CT-Transformer 标点恢复，补出 `，。？`

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CJK_RANGE = "\u4e00-\u9fff"
_CN_PUNCT = "，。！？、；：""''（）《》"
# 只在【紧邻中文】时才转全角，否则会破坏英文句子（实测模型会把
# "papers, and they vary" 变成 "papers，and they vary"）
_HALF_TO_FULL = {",": "，", ".": "。", "?": "？", "!": "！", ";": "；",
                 ":": "：", "(": "（", ")": "）"}

# 专有名词表（--vocab 设置）。为空时不改提示词。
VOCAB_TERMS: list[str] = []

_PUNCT_MODEL_CACHE = None
_PUNCT_MODEL_FIXED = False


def _cjk_ratio(text: str) -> float:
    """中文字占"有意义的字符"的比例。空白和标点不算。"""
    chars = [c for c in text if not c.isspace() and c not in _CN_PUNCT and c not in ",.!?;:()"]
    if not chars:
        return 0.0
    return sum(1 for c in chars if _CJK_RE.match(c)) / len(chars)


def _normalize_compat(text: str) -> str:
    """
    把「兼容形式」标点归一成标准字符。

    为什么需要：CT-Transformer 补标点时，输出的不是 `，。？！`，
    而是 U+FE50~FE6F 这些兼容形式（﹑ ﹐ ﹔ ﹒ ﹗ ﹖ ﹕ ﹞ ﹝ ﹛ ﹘）。
    它们看着像标点，但：

      · 我的去重/全角转换规则匹配的是 `[，。！？、；：]` —— 一个都匹配不上
      · Markdown 渲染、搜索、复制到别处都会出问题
      · 用户看到的就是"标点很怪、可读性差"

    NFKC 归一正好把这类兼容字符折回标准形式（﹑->、 ﹐->, ﹒->. ﹗->!）。
    折成半角之后再交给 _fullwidth_punct 统一转全角。
    """
    import unicodedata
    return unicodedata.normalize("NFKC", text)


def _longest_common(a: str, b: str) -> int:
    """a 和 b 的最长【连续】公共片段长度。"""
    import difflib
    if not a or not b:
        return 0
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(
        0, len(a), 0, len(b))
    return m.size


def _is_repeat(sentence: str, earlier: str, ratio: float = 0.45,
               min_block: int = 6) -> bool:
    """
    判断一句话是不是前面内容的重复。

    ## 为什么不用固定 n-gram 重叠率（上一版的做法）

    实测失败的例子：

      前文：另一件事AI公司算不算敌人。他说大公司追AGI的竞赛已经开始牺牲爱好。
      重复：另一件是AI公司算不算敌人。

    两条只差一个字（事/是），但把句子拆成 4-gram 再算重叠率——
    开头几个 4-gram 全不匹配，重叠率被稀释到 0.57 左右，卡在阈值边缘。

    改成"最长连续公共片段"：这两句的公共片段是 `公司算不算敌人。`（8 字），
    占短句 14 字的一半以上 —— 一眼就是重复，不受开头差异影响。

    参考做法：whisper 复读清理的常见实现都是找最长重复片段而不是按窗口打散，
    见 VoiceStudio PR #356、whisper-guard。

    ## 判据

      · 最长公共片段 >= min_block（6 字，太短会误伤"他说"这种正常重复）
      · 该片段占这句话的比例 >= ratio（默认 0.45）
    6 字 / 0.45 是实测调出来的：真实输出里那条残句的公共片段是
    `公司算不算敌人`（7 字，前一句用破折号接下文所以只对上 7 字），
    占 14 字的 50%；阈值取 8 字时刚好漏掉。
    """
    if len(sentence) < min_block:
        return False
    return _longest_common(sentence, earlier) >= max(min_block, len(sentence) * ratio)


def _drop_duplicate_sentences(text: str, verbose: bool = True) -> str:
    """
    去掉与前面重复的句子（Whisper 退化复读）。

    ## 设计原则：判定只看原文，删除只打标记

    之前改了三轮都没修干净，根本原因是"边判边改"：
    第一轮正序扫描删掉几句之后，第二轮回删时的参照文本已经不是原文了，
    被删掉的句子里恰好含有残句的完整版本，导致最长公共片段变短、判不出来。

    现在：
      · 所有判定都基于【原始切句 + 原文拼接】，不受前面删了什么影响
      · 删除只往 drop[] 里打标记，最后统一拼接
    两轮因此互相独立。

    ## 两轮做什么

    第一轮（正序）：每句只和"原文中它前面的内容"比 —— 抓重复出现的整句。
    第二轮（尾部回删）：从最后一句往前，每句和"原文里除它自己以外的全部"比 ——
      抓堆在末尾的残句。参照用全文而不是前文，是因为复读的第二次出现
      可能被打散成几句，用全文比对才能凑够公共片段长度。

    ## 判据见 _is_repeat：最长连续公共片段 >= 6 字且占该句 >= 45%
    """
    parts = [x for x in re.split(r"(?<=[。！？!?])", text) if x.strip()]
    if len(parts) < 2:
        return text

    norms = [re.sub(r"\s+", "", p) for p in parts]
    total = "".join(norms)
    offsets = [0]
    for n in norms:
        offsets.append(offsets[-1] + len(n))

    drop = [False] * len(parts)

    # 第一轮：正序，每句和前文比
    for i in range(len(parts)):
        if _is_repeat(norms[i], total[:offsets[i]]):
            drop[i] = True

    # 第二轮：尾部回删，每句和"原文去掉它自己"比
    guard = 0
    while sum(1 for d in drop if not d) > 2 and guard < len(parts):
        guard += 1
        idx = max((i for i in range(len(parts)) if not drop[i]), default=-1)
        if idx < 0:
            break
        ref = total[:offsets[idx]] + total[offsets[idx + 1]:]
        if not _is_repeat(norms[idx], ref):
            break
        drop[idx] = True

    removed = sum(1 for d in drop if d)
    if removed and verbose:
        print(f"       [反复读] 去掉 {removed} 句重复内容")
    if not removed:
        return text
    return "".join(p_ for i, p_ in enumerate(parts) if not drop[i])


def _normalize_compat(text: str) -> str:
    """
    把「兼容形式」标点归一成标准字符。

    为什么需要：CT-Transformer 补标点时，输出的不是 `，。？！`，
    而是 U+FE50~FE6F 这些兼容形式（﹑ ﹐ ﹔ ﹒ ﹗ ﹖ ﹕ ﹞ ﹝ ﹛ ﹘）。
    它们看着像标点，但：

      · 我的去重/全角转换规则匹配的是 `[，。！？、；：]` —— 一个都匹配不上
      · Markdown 渲染、搜索、复制到别处都会出问题
      · 用户看到的就是"标点很怪、可读性差"

    NFKC 归一正好把这类兼容字符折回标准形式（﹑->、 ﹐->, ﹒->. ﹗->!）。
    折成半角之后再交给 _fullwidth_punct 统一转全角。
    """
    import unicodedata
    return unicodedata.normalize("NFKC", text)


def _longest_common(a: str, b: str) -> int:
    """a 和 b 的最长【连续】公共片段长度。"""
    import difflib
    if not a or not b:
        return 0
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(
        0, len(a), 0, len(b))
    return m.size


def _is_repeat(sentence: str, earlier: str, ratio: float = 0.45,
               min_block: int = 6) -> bool:
    """
    判断一句话是不是前面内容的重复。

    ## 为什么不用固定 n-gram 重叠率（上一版的做法）

    实测失败的例子：

      前文：另一件事AI公司算不算敌人。他说大公司追AGI的竞赛已经开始牺牲爱好。
      重复：另一件是AI公司算不算敌人。

    两条只差一个字（事/是），但把句子拆成 4-gram 再算重叠率——
    开头几个 4-gram 全不匹配，重叠率被稀释到 0.57 左右，卡在阈值边缘。

    改成"最长连续公共片段"：这两句的公共片段是 `公司算不算敌人。`（8 字），
    占短句 14 字的一半以上 —— 一眼就是重复，不受开头差异影响。

    参考做法：whisper 复读清理的常见实现都是找最长重复片段而不是按窗口打散，
    见 VoiceStudio PR #356、whisper-guard。

    ## 判据

      · 最长公共片段 >= min_block（6 字，太短会误伤"他说"这种正常重复）
      · 该片段占这句话的比例 >= ratio（默认 0.45）
    6 字 / 0.45 是实测调出来的：真实输出里那条残句的公共片段是
    `公司算不算敌人`（7 字，前一句用破折号接下文所以只对上 7 字），
    占 14 字的 50%；阈值取 8 字时刚好漏掉。
    """
    if len(sentence) < min_block:
        return False
    return _longest_common(sentence, earlier) >= max(min_block, len(sentence) * ratio)


def _drop_duplicate_sentences(text: str, verbose: bool = True) -> str:
    """
    去掉与前面重复的句子（Whisper 退化复读）。

    两轮：
      1. 正序扫一遍，每句和前文比，重复就丢
      2. 从尾部逐句回删，直到尾部不再重复
         （复读几乎总堆在末尾；尾部单独跑一轮能处理断句被打乱的情况）
    """
    parts = [x for x in re.split(r"(?<=[。！？!?])", text) if x.strip()]
    if len(parts) < 2:
        return text

    kept: list[str] = []
    history = ""
    dropped = 0
    for part in parts:
        norm = re.sub(r"\s+", "", part)
        if _is_repeat(norm, re.sub(r"\s+", "", history)):
            dropped += 1
            continue
        kept.append(part)
        history += part

    # 第二轮：尾部回删
    #
    # 参照文本必须是【原始全文】，不能是"已保留内容"。
    # 上一版用 "".join(kept[:-1])：第一遍正序扫描已经删掉了几句，
    # 被删句里正好含有那句残句的完整版本，于是参照里只剩半截，
    # 最长公共片段从 7 字掉到不够长，循环就停了 —— 实测
    # "另一件是 AI 公司算不算敌人。" 单独判定是 True，循环里却不删。
    #
    # 做法：整块原文去掉末尾这句自己，剩下的都当参照。
    # 这样残句在原文任何位置出现过都能对上。
    tail_dropped = 0
    full_raw = re.sub(r"\s+", "", "".join(parts))
    while len(kept) >= 3:
        last = re.sub(r"\s+", "", kept[-1])
        ref = full_raw[:-len(last)] if len(last) < len(full_raw) else ""
        if not _is_repeat(last, ref):
            break
        kept.pop()
        tail_dropped += 1

    if (dropped or tail_dropped) and verbose:
        print(f"       [反复读] 去掉 {dropped} 句重复整句、{tail_dropped} 句尾部残句")
    return "".join(kept)


def _normalize_compat(text: str) -> str:
    """
    把「兼容形式」标点归一成标准字符。

    为什么需要：CT-Transformer 补标点时，输出的不是 `，。？！`，
    而是 U+FE50~FE6F 这些兼容形式（﹑ ﹐ ﹔ ﹒ ﹗ ﹖ ﹕ ﹞ ﹝ ﹛ ﹘）。
    它们看着像标点，但：

      · 我的去重/全角转换规则匹配的是 `[，。！？、；：]` —— 一个都匹配不上
      · Markdown 渲染、搜索、复制到别处都会出问题
      · 用户看到的就是"标点很怪、可读性差"

    NFKC 归一正好把这类兼容字符折回标准形式（﹑->、 ﹐->, ﹒->. ﹗->!）。
    折成半角之后再交给 _fullwidth_punct 统一转全角。
    """
    import unicodedata
    return unicodedata.normalize("NFKC", text)


def _ngrams(s: str, n: int = 4) -> set:
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _drop_duplicate_sentences(text: str, verbose: bool = True) -> str:
    """
    去掉与前面高度重复的句子（退化复读）。

    ## 为什么不用"整句相等"或"difflib 相似度"

    实测失败的例子：

      第一次：另一件事 AI 公司算不算敌人—他说…已经开始牺牲爱好。
      第二次：另一件是 AI 公司算不算敌人。他说…已经开始牺牲安全。

    两次重复杂一两个字，而且【断句位置不同】（第一次是一整句，第二次被切成两句）。
    按句切开再逐句比较，因为断句位置变了就匹配不上，实测完全失效。

    ## 改用字符 n-gram 重叠率

    把句子和"已保留的全部内容"都拆成 4 字片段，看这句的 4-gram 有多少
    在已保留内容里出现过。>= 0.80 判为重复。

    好处：不受断句位置和个别字差异影响。4-gram 对中文足够区分，
    正常并列句（"他喜欢苹果。他喜欢香蕉。"）重叠率远低于阈值，不会误删。
    """
    parts = re.split(r"(?<=[。！？!?])", text)
    kept: list[str] = []
    kept_grams: set = set()
    dropped = 0
    for part in parts:
        norm = re.sub(r"\s+", "", part)
        if len(norm) < 8:
            kept.append(part)
            continue
        # 统一用 4-gram。试过"短句改用 3-gram"，因为 kept_grams 里混进了不同
        # 长度的片段，比对反而失效（实测一条都没删掉），所以回退。
        # 已知残留：十几字的重复短句（"另一件是AI公司算不算敌人。"）重叠率
        # 约 0.7，达不到阈值，会留下来 —— 但重复的长句主体已经能删掉。
        grams = _ngrams(norm)
        overlap = (len(grams & kept_grams) / len(grams)) if grams else 0.0
        if overlap >= 0.75:
            dropped += 1
            continue
        kept.append(part)
        kept_grams |= grams
    result = "".join(kept)

    # 第二轮：从【尾部】逐句回删。
    # 第一轮漏掉的是"重复片段被切成了几句"的情况，比如
    #   …已经开始牺牲爱好。原影片连接在说明栏。有兴趣可以去听更多。
    #   另一件是 AI 公司算不算敌人。            <- 只有 14 字，4-gram 重叠率约 0.7
    # 达不到 0.80。但它在【尾部】，而退化复读几乎总是堆在末尾，
    # 所以对尾部用更低的阈值 0.55 单独处理，不会误伤正文中段。
    tail_dropped = 0
    while True:
        parts = re.split(r"(?<=[。！？!?])", result)
        parts = [x for x in parts if x.strip()]
        if len(parts) < 3:
            break
        last = re.sub(r"\s+", "", parts[-1])
        if len(last) < 6:
            break
        head_grams = _ngrams(re.sub(r"\s+", "", "".join(parts[:-1])))
        last_grams = _ngrams(last)
        if not last_grams:
            break
        if len(last_grams & head_grams) / len(last_grams) < 0.55:
            break
        result = "".join(parts[:-1])
        tail_dropped += 1

    if (dropped or tail_dropped) and verbose:
        print(f"       [反复读] 去掉 {dropped} 句重复整句、{tail_dropped} 句尾部残句")
    return result


def _strip_cn_punct(text: str) -> str:
    """去掉中文标点，给标点恢复模型一个干净的输入。"""
    return re.sub(r"[，。！？、；：""''（）《》]", "", text)


# ---------------------------------------------------------------------------
# 复读截断
# ---------------------------------------------------------------------------
# 真实现场（medium 模型，某支视频最后一段）：
#
#   原影片将近 20 分钟…原影片是在 20 分…原影片在 20 秒钟…原影片，在 20 时…
#   原影片的时间，在 30 分钟…原影片中，我还是在想？…
#
# 这是 Whisper 的退化解码：每句话都换了说法，所以 no_repeat_ngram_size=3
# 抓不住（它只禁完全相同的 3-gram）；condition_on_previous_text 开/关实测
# 对这段输出【一字不差】；单独把这 26 秒拿出来转则完全正常 —— 说明复读是
# 上下文/分块引入的，模型本身的参数压不住。
#
# 能做的是在文本层兜一道：同一个短串在【句子/分句开头】反复出现，就是退化信号。

LOOP_PREFIX_MIN = 3        # 至少重复 3 次才判定
LOOP_PREFIX_LEN = 3        # 短串长度（中文 3 字，如"原影片"）
LOOP_MAX_CJK = 600         # 只对"短块 + 高重复"动手，长的正常文本不碰


def collapse_loops(text: str, verbose: bool = True) -> str:
    """
    把退化复读的尾巴截掉，只保留第一次重复之前的内容。

    判定条件（同时满足才动手）：
      · 中文字数 <= LOOP_MAX_CJK（长文本正常重复更多，不能误伤）
      · 某个 3 字串在【开头或标点之后】出现 >= LOOP_PREFIX_MIN 次
      · 该串至少占句子开头总数的 1/3（说明整段都在复读它，而不是偶尔提到）

    宁可少删：触发时只截到第 3 次出现的分句之前，不重写内容。
    """
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    if cjk < 20 or cjk > LOOP_MAX_CJK:
        return text

    boundaries = set("。！？，、；：\n")
    best = None
    counts: dict[str, list[int]] = {}
    for i in range(len(text) - LOOP_PREFIX_LEN + 1):
        piece = text[i:i + LOOP_PREFIX_LEN]
        if not re.fullmatch(r"[\u4e00-\u9fff]{%d}" % LOOP_PREFIX_LEN, piece):
            continue
        if i > 0 and text[i - 1] not in boundaries:
            continue                      # 只统计"分句开头"的出现
        counts.setdefault(piece, []).append(i)

    for piece, positions in counts.items():
        if len(positions) < LOOP_PREFIX_MIN:
            continue
        # 关键判据：重复要【一直持续到文本后半段】才算退化。
        # 正常文本提到同一个人名两三次，通常集中在前半（介绍完之后就不再说），
        # 而退化复读会一路重复到最后。
        if positions[-1] < len(text) * 0.5:
            continue
        if best is None or len(positions) > len(best[1]):
            best = (piece, positions)

    if best is None:
        # 前缀法没命中时，退化复读更常见的形态是【整句重复】——
        # 实测："…另一件事 AI 公司算不算敌人…牺牲爱好。原影片连接在说明栏…"
        #       紧接着又出现 "另一件是 AI 公司算不算敌人…牺牲安全。"
        # 每次措辞略有不同，所以用相似度而不是完全相等来判。
        return _drop_duplicate_sentences(text, verbose)

    return _finish_loop_cut(text, best, verbose)


def _finish_loop_cut(text: str, best, verbose: bool) -> str:
    """按"3 字串重复"的判定结果截断。

    注意：boundaries 必须在这里定义。上次把 collapse_loops 拆成两个函数时
    漏了这一步，导致每次有中文输出都在 write_markdown 崩掉：
        NameError: name 'boundaries' is not defined
    """
    boundaries = set("。！？，、；：\n")
    piece, positions = best
    cut = positions[LOOP_PREFIX_MIN - 1]
    # 退到上一个分句边界，避免截出半句
    while cut > 0 and text[cut - 1] not in boundaries:
        cut -= 1
    head = text[:cut].rstrip("，、；： ")
    if len(head) < 10:
        return text
    if verbose:
        print(f"       [反复读] 检测到「{piece}」在分句开头重复 {len(positions)} 次，"
              f"已截断最后 {len(text) - len(head)} 字")
    return head


def _dedupe_punct(text: str) -> str:
    """
    清理重复标点。三种情况：
      · 同一个标点连着出现   "，，" -> "，"
      · 句子结束符后面又跟逗号 "。，" -> "。"
      · 空标点串           "，，，" -> "，"
    标点模型偶尔还会留下这种痕迹，兜一道。
    """
    text = re.sub(r"([，。！？、；：])\1+", r"\1", text)
    text = re.sub(r"[。！？][，、；：]+", lambda m: m.group(0)[0], text)
    return text


def _punct_density(text: str) -> float:
    """每个中文字摊到几个中文标点。正常中文书面语大约 0.06~0.10。"""
    cjk = len(_CJK_RE.findall(text))
    if cjk == 0:
        return 1.0
    return sum(1 for c in text if c in _CN_PUNCT) / cjk


def _space_cjk_latin(text: str) -> str:
    """中英文之间补空格：`数学频道Numberphile在10月15号` -> `数学频道 Numberphile 在 10 月 15 号`。"""
    text = re.sub(rf"([{_CJK_RANGE}])([A-Za-z0-9])", r"\1 \2", text)
    text = re.sub(rf"([A-Za-z0-9])([{_CJK_RANGE}])", r"\1 \2", text)
    return text


def _fullwidth_punct(text: str) -> str:
    """中文语境里的半角标点转全角。只处理紧邻中文的，英文句子保持原样。"""
    for half, full in _HALF_TO_FULL.items():
        h = re.escape(half)
        text = re.sub(rf"([{_CJK_RANGE}]){h}", lambda m: m.group(1) + full, text)
        text = re.sub(rf"{h}([{_CJK_RANGE}])", lambda m: full + m.group(1), text)
        # 中文标点后面紧跟的中文，前面的半角也要换
        text = re.sub(rf"([{_CJK_RANGE}]{h})\s*([{_CJK_RANGE}])",
                      lambda m: m.group(1).replace(half, full) + m.group(2), text)
    return text


def _restore_punctuation(text: str, proxy: str | None = None) -> str:
    """
    给缺少标点的中文补标点。用 sherpa-onnx 的 CT-Transformer（中英双语 int8，62MB）。

    为什么不是无脑调用：
      实测它对英文是破坏性的 —— 会把 `papers, and they vary` 改成
      `papers，and they vary`。所以调用方必须先判断"这段是不是中文、是不是真的没标点"。

    模型没装 / 下载失败时安静跳过，不影响主流程。
    """
    global _PUNCT_MODEL_CACHE, _PUNCT_MODEL_FIXED
    if _PUNCT_MODEL_FIXED:
        return text
    try:
        import sherpa_onnx
    except ImportError:
        _PUNCT_MODEL_FIXED = True        # 没装 sherpa-onnx，以后不再尝试
        return text

    try:
        if _PUNCT_MODEL_CACHE is None:
            model = _ensure_punct_model(proxy)
            if model is None:
                _PUNCT_MODEL_FIXED = True
                return text
            cfg = sherpa_onnx.OfflinePunctuationConfig(
                model=sherpa_onnx.OfflinePunctuationModelConfig(
                    ct_transformer=str(model)))
            _PUNCT_MODEL_CACHE = sherpa_onnx.OfflinePunctuation(cfg)
        return _PUNCT_MODEL_CACHE.add_punctuation(text)
    except Exception as exc:
        print(f"       [警告] 标点恢复失败，跳过：{type(exc).__name__}: {exc}")
        _PUNCT_MODEL_FIXED = True
        return text


def _ensure_punct_model(proxy: str | None = None) -> Path | None:
    """标点模型不存在就下载（62MB）。失败返回 None，不抛异常。"""
    root = MODEL_DIR / "punct"
    found = list(root.glob("*/model.int8.onnx")) if root.exists() else []
    if found:
        return found[0]

    url = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/"
           "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8.tar.bz2")
    print("       这段中文缺少标点，正在下载标点恢复模型（62MB，只需一次）…")
    try:
        import tarfile
        import urllib.request
        root.mkdir(parents=True, exist_ok=True)
        tar_path = root / "punct.tar.bz2"
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(url, timeout=180) as resp, open(tar_path, "wb") as fh:
            while True:
                block = resp.read(1024 * 256)
                if not block:
                    break
                fh.write(block)
        with tarfile.open(tar_path, "r:bz2") as tf:
            try:
                tf.extractall(root, filter="data")
            except TypeError:
                tf.extractall(root)
        tar_path.unlink(missing_ok=True)
        found = list(root.glob("*/model.int8.onnx"))
        if found:
            print(f"       [OK] 标点模型就绪：{found[0].parent.name}")
            return found[0]
    except Exception as exc:
        print(f"       [警告] 标点模型下载失败，中文标点可能仍有缺失："
              f"{type(exc).__name__}: {exc}")
    return None


def polish_text(text: str, fix_punct: bool = True, proxy: str | None = None) -> str:
    """
    收尾打磨一段文字。顺序很重要：

      1. 先补标点（模型需要"没有标点"的输入才好判断该加在哪）
      2. 再补中英空格
      3. 最后统一全角/半角
    """
    if not text:
        return text
    text = _normalize_compat(text)
    cjk_ratio = _cjk_ratio(text)
    if cjk_ratio >= 0.35 and fix_punct and _punct_density(text) < 0.03:
        # CT-Transformer 是"给【没有标点】的文本补标点"的模型。
        # 直接喂带标点的文本，它会在原标点旁边再加一个，叠出
        #   "变得多了。，"   "看得懂。，"   "敌人？，"   "，，"
        # 这类重复 —— 实测真实输出里成片出现，可读性反而更差。
        # 正确用法：先把标点剥干净，让它整体重打一遍。
        text = _restore_punctuation(_strip_cn_punct(text), proxy)
    text = _space_cjk_latin(text)
    if cjk_ratio >= 0.20:
        text = _fullwidth_punct(text)
    # 反向修正：紧跟在拉丁字母/数字后面的全角标点改回半角。
    # 标点模型看不到"语言边界"，会把英文分句也断成全角，例如
    # "we asked。" -> 应该是 "we asked."
    for full, half in {v: k for k, v in _HALF_TO_FULL.items()}.items():
        text = re.sub(rf"([A-Za-z0-9]){re.escape(full)}", lambda m, h=half: m.group(1) + h, text)
    text = _dedupe_punct(text)
    return collapse_loops(text)


def write_markdown(url: str, info: dict, segments, tr_info,
                   wav: Path, out_path: Path,
                   model_size: str = WHISPER_MODEL,
                   fix_punct: bool = True,
                   proxy: str | None = None) -> Path:
    """把转写结果写成 Markdown。"""
    banner("第 5 层 / 生成 Markdown")

    blocks = group_paragraphs(segments)
    duration = tr_info.duration or info.get("duration") or 0

    # 原始聚类编号是稀疏的（可能是 0 和 3），直接输出会变成 S1/S4 让人以为漏了人。
    # 这里按【实际出现顺序】重排成连续的 0,1,2…，输出就是 S1/S2/S3。
    present = []
    for _s, _e, _t, _spk in blocks:
        if _spk is not None and _spk not in present:
            present.append(_spk)
    speaker_map = {raw: idx for idx, raw in enumerate(present)}
    blocks = [(s, e, tx, None if spk is None else speaker_map[spk])
              for s, e, tx, spk in blocks]
    speakers = list(speaker_map.values())

    # 各说话人占了多少时长。自动判定的人数并不可靠（实测 8 个用例只对 5 个），
    # 把占比写进头部，用户一眼就能看出"63% / 30% / 6%"这种可疑分布，
    # 从而知道该用 --num-speakers 指定人数。
    share: dict[int, float] = {}
    for _s, _e, _t, _spk in blocks:
        if _spk is not None:
            share[_spk] = share.get(_spk, 0.0) + (_e - _s)
    share_total = sum(share.values()) or 1.0

    lines = [
        f"# {info.get('title') or wav.stem}",
        "",
        f"- **来源**：{url}",
        f"- **时长**：{_fmt_duration(duration)}",
        f"- **语言**：{tr_info.language}"
        f"（置信度 {tr_info.language_probability:.2f}）",
        f"- **转写**：faster-whisper（{model_size}，VAD 开，beam=1）",
        f"- **生成时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **字幕条数**：{len(segments)}，合并为 {len(blocks)} 段",
        *([f"- **说话人**：{len(speakers)} 人"
           f"（{', '.join(f'{diarize.label(s)} {share.get(s, 0) / share_total * 100:.0f}%' for s in speakers)}）"]
          if speakers else []),
        "",
        "---",
        "",
    ]

    for start, end, text, speaker in blocks:
        # 后处理是"锦上添花"，绝不能因为它出错把整次转写白费。
        # 之前 _finish_loop_cut 漏了 boundaries，就是在这一步崩的 ——
        # 用户跑完 6 分钟音频（甚至几小时）才发现最后一步炸掉。
        try:
            text = polish_text(text, fix_punct=fix_punct, proxy=proxy)
        except Exception as exc:
            print(f"       [警告] 文本打磨跳过（保留原始转写）："
                  f"{type(exc).__name__}: {exc}")
        head = f"## {_fmt_clock(start)} ~ {_fmt_clock(end)}"
        if speaker is not None:
            head += f"  [{diarize.label(speaker)}]"
        lines.append(head)
        lines.append("")
        lines.append(text)
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    _save_source_marker(out_path, wav)

    size_kb = out_path.stat().st_size / 1024
    print(f"  [OK] {out_path}")
    print(f"       {len(blocks)} 段，{size_kb:.1f} KB")
    return out_path


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _join_text(parts: list[str]) -> str:
    """
    把若干字幕片段拼成一段文字。

    为什么要单独写：
      Whisper 返回的每个片段首尾没有统一约定 —— 英文有时带前导空格、有时不带，
      直接 "".join 会拼出 "work.So let me show you" 这种缺空格的句子。

    规则：
      · 中文字符之间【不加】空格（中文本来就不用空格断句）
      · 其余情况（英文/数字/标点）之间加一个空格
    """
    out = ""
    for raw in parts:
        piece = raw.strip()
        if not piece:
            continue
        if out and not _is_cjk(out[-1]) and not _is_cjk(piece[0]):
            out += " "
        out += piece
    return out


def _is_cjk(ch: str) -> bool:
    """判断是不是中日韩文字（这类字符之间不需要空格）。"""
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF      # CJK 统一表意文字
        or 0x3400 <= code <= 0x4DBF   # 扩展 A
        or 0x3000 <= code <= 0x303F   # CJK 标点
        or 0xFF00 <= code <= 0xFFEF   # 全角字符
    )

def _fmt_duration(seconds: float) -> str:
    total = max(0, int(round(seconds or 0)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_clock(seconds: float) -> str:
    total = max(0, int(round(seconds or 0)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(url: str, proxy_arg: str | None, no_proxy: bool,
        model: str, force: bool, out: Path | None,
        lang: str | None = None, resume: bool = True,
        cookies: Path | None = None, device_mode: str = "auto",
        cookies_from_browser: str | None = None,
        with_speakers: bool = False,
        diarize_threshold: float = diarize.DEFAULT_THRESHOLD,
        num_speakers: int = -1,
        fix_punct: bool = True) -> int:
    rep = Report()

    # 先查硬依赖：ffmpeg 缺失时没必要先花时间下载
    try:
        require_ffmpeg()
    except PipelineError as exc:
        print(f"\n[环境错误] {exc}")
        return 1

    try:
        # 1. 代理
        t0 = time.time()
        proxy = resolve_proxy(proxy_arg, no_proxy)
        if proxy:
            # yt-dlp receives the proxy explicitly; Hugging Face/model downloads
            # use the standard proxy environment variables.
            os.environ["HTTP_PROXY"] = proxy
            os.environ["HTTPS_PROXY"] = proxy
        rep.add("代理配置", time.time() - t0,
                "直连" if not proxy else proxy)

        # 2. 下载
        t0 = time.time()
        src, info = download_audio(
            url, proxy, force, cookies=cookies,
            cookies_from_browser=cookies_from_browser)
        rep.add("下载音频", time.time() - t0, src.name)

        # 3. 提取
        t0 = time.time()
        wav = extract_audio(src, force)
        rep.add("提取音频", time.time() - t0, wav.name)

        # 4. 转写
        segments, tr_info, tr_sec = transcribe(
            wav, model, lang, resume, device_mode=device_mode,
            with_speakers=with_speakers,
            diarize_threshold=diarize_threshold,
            num_speakers=num_speakers,
            proxy=proxy)
        rep.add("语音转写", tr_sec, f"{len(segments)} 条字幕")
        if not segments:
            raise PipelineError(
                "转写结果为空。可能是音频里没有人声，或音频提取有问题。\n"
                f"   可以听一下 {wav} 确认有声音。"
            )

        # 5. 输出
        t0 = time.time()
        base = out or (OUTPUT_DIR / f"{wav.stem}.md")
        target = _target_for_source(base, wav, force)
        md = write_markdown(url, info, segments, tr_info, wav, target,
                        model_size=model, fix_punct=fix_punct, proxy=proxy)
        rep.add("生成 Markdown", time.time() - t0, md.name)

    except PipelineError as exc:
        print(f"\n[失败] {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n[中断] 用户取消")
        return 130

    rep.print_summary()
    print(f"\n完成：{md}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="YouTube → Markdown 五层流水线（代理/下载/提取/转写/输出）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python pipeline.py "https://www.youtube.com/watch?v=XXXX"
  python pipeline.py URL --model small
  python pipeline.py URL --proxy http://127.0.0.1:7890
  python pipeline.py URL --no-proxy
""")
    parser.add_argument("url", help="YouTube 视频链接")
    parser.add_argument("--proxy", help=f"代理地址（默认读环境变量，再退回 {DEFAULT_PROXY}）")
    parser.add_argument("--no-proxy", action="store_true", help="强制直连")
    cookie_group = parser.add_mutually_exclusive_group()
    cookie_group.add_argument(
        "--cookies", type=Path,
        help="Netscape HTTP Cookie File 路径（用于需要登录的视频）")
    cookie_group.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER[:PROFILE[:KEYRING[:CONTAINER]]]",
        help="从浏览器读取 Cookie，例如 chrome、edge 或 firefox:default")
    parser.add_argument(
        "--device", choices=DEVICE_MODES, default="auto",
        help="推理设备：auto（自动）、cpu、cuda、npu（OpenVINO Intel NPU）")
    parser.add_argument("--lang", default=None,
                        help="强制语言（如 zh / en / ja）；不填则自动检测")
    parser.add_argument("--no-resume", action="store_true",
                        help="忽略断点检查点，从头转写（默认会从上次停下的块继续）")
    parser.add_argument("--model", default=WHISPER_MODEL,
                        help=f"Whisper 模型大小（默认 {WHISPER_MODEL}）："
                             "tiny/base/small/medium/large-v3")
    parser.add_argument("--force", action="store_true",
                        help="忽略已下载/已提取的文件，全部重做")
    parser.add_argument("--diarize", dest="speakers_enabled", action="store_true",
                        help="开启说话人分离（需 pip install sherpa-onnx；"
                             "首次下载 34MB 模型；总耗时大约翻倍）")
    parser.add_argument("--num-speakers", type=int, default=-1, metavar="N",
                        help="已知说话人数量时指定（如 2）；默认 -1 = 自动判断")
    parser.add_argument("--diarize-threshold", type=float,
                        default=diarize.DEFAULT_THRESHOLD, metavar="X",
                        help="说话人聚类阈值：调大更倾向合并为同一人")
    parser.add_argument("--vocab", default=None, metavar="词1,词2",
                        help="专有名词表，逗号分隔。会把这些人名/术语拼进提示词，"
                             "提升识别准确率（如 陶哲轩,Numberphile,OpenAI）")
    parser.add_argument("--no-refine", action="store_true",
                        help="不做混排细分（默认会做：把缺标点的中文段单独重转）")
    parser.add_argument("--no-fix-punctuation", dest="fix_punct",
                        action="store_false",
                        help="不做中文标点恢复（默认会做：中文缺标点时自动下载 62MB 模型补标点）")
    parser.add_argument("-o", "--out", type=Path, help="输出 Markdown 路径")
    args = parser.parse_args()
    global _NO_REFINE
    if getattr(args, "no_refine", False):
        _NO_REFINE = True
    if getattr(args, "vocab", None):
        VOCAB_TERMS.extend(x.strip() for x in args.vocab.split(",") if x.strip())
        print(f"[词表] 已启用 {len(VOCAB_TERMS)} 个专有名词：{'、'.join(VOCAB_TERMS)}")

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 60)
    print(" YouTube → Markdown 流水线")
    print(f" 工作目录：{LAB_DIR}")
    print("=" * 60)

    return run(args.url, args.proxy, args.no_proxy,
               args.model, args.force, args.out, lang=args.lang,
               resume=not args.no_resume, cookies=args.cookies,
               device_mode=args.device,
               cookies_from_browser=args.cookies_from_browser,
               with_speakers=args.speakers_enabled,
               diarize_threshold=args.diarize_threshold,
               num_speakers=args.num_speakers,
               fix_punct=args.fix_punct)


if __name__ == "__main__":
    sys.exit(main())
