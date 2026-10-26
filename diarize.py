# -*- coding: utf-8 -*-
"""
diarize.py —— 说话人分离（Speaker Diarization）

给转写结果标上"谁在说"。解决的是访谈、播客、多人讨论这类视频：
一整段文字读下来分不清哪句是主持人、哪句是嘉宾。

## 为什么用 sherpa-onnx 而不是 Buzz 那套 pyannote

Buzz 用 pyannote.audio + whisper-diarization，效果确实好，但有两个硬门槛：

  1. 需要 PyTorch（CPU 版约 124 MB，装完实际占 2 GB 左右）
  2. 需要 HuggingFace token —— `pyannote/speaker-diarization-3.1` 是 gated 模型，
     要在网页上接受条款、生成 token、再配环境变量

一个"本地、免费、不要 key"的工具带上这两个门槛，等于把大部分用户挡在门外。

sherpa-onnx 走 ONNX Runtime，只需要：
  · sherpa-onnx 包本体：约 2 MB
  · 模型：pyannote 分割 6.6 MB + 3D-Speaker 中英双语说话人嵌入 27 MB = 共 34 MB

不需要 torch、不需要 token。实测在同一条 150 秒中英混排素材上：
  单次分离耗时约 28 秒（5.3x 实时），阈值 0.9 时得到 3 个说话人
  （素材实际就是 中文旁白 + 英文访谈 + 旁白）。

## 精度说明

分离只回答"哪段话是谁"，不回答"是谁"。说话人编号 S1/S2/S3 是聚类结果，
不保证跨视频一致。想要真实姓名得自己做映射。

## 模型来源

  · 分割：https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-segmentation-models
  · 嵌入：https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models
    （"recongition" 是上游 release tag 的原始拼写，不是笔误）
"""

from __future__ import annotations

import tarfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# 阈值。越大越"倾向于合并成同一个说话人"。
# 实测同一段素材：0.5~0.7 会把 2~3 人拆成 6 人，0.8 -> 4 人，0.9 -> 3 人。
DEFAULT_THRESHOLD = 0.9

SEGMENTATION_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
EMBEDDING_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/"
    "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx")

SEGMENTATION_DIR = "sherpa-onnx-pyannote-segmentation-3-0"
EMBEDDING_FILE = "3dspeaker_campplus_zh_en.onnx"


class DiarizeError(RuntimeError):
    """分离失败（模型下载失败、sherpa-onnx 未安装、模型文件损坏等）。"""


@dataclass
class Span:
    """一段被判定为同一说话人的时间区间。"""
    start: float
    end: float
    speaker: int

    def overlap(self, start: float, end: float) -> float:
        """与 [start, end] 的重叠秒数。用来给字幕分配说话人。"""
        return max(0.0, min(self.end, end) - max(self.start, start))


# ---------------------------------------------------------------------------
# 模型下载
# ---------------------------------------------------------------------------
def _download(url: str, dest: Path, proxy: str | None = None, timeout: int = 180) -> Path:
    """下载到 dest。已存在且非空就跳过。"""
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)

    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"       下载 {dest.name} …")
    t0 = time.time()
    try:
        with opener.open(url, timeout=timeout) as resp, open(tmp, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                block = resp.read(1024 * 256)
                if not block:
                    break
                fh.write(block)
                got += len(block)
                if total and got % (1024 * 1024 * 8) < 1024 * 256:
                    print(f"         {got / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB")
        tmp.replace(dest)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise DiarizeError(
            f"下载失败：{dest.name}\n"
            f"   地址：{url}\n"
            f"   原始错误：{type(exc).__name__}: {exc}\n"
            f"   国内网络需要代理，用 --proxy 指定，例如 --proxy http://127.0.0.1:7890"
        ) from exc
    print(f"       完成 {dest.name}  {dest.stat().st_size / 1024 / 1024:.1f} MB"
          f"（{time.time() - t0:.1f}s）")
    return dest


def ensure_models(model_root: Path, proxy: str | None = None) -> tuple[Path, Path]:
    """确保分割模型和嵌入模型都在本地，返回 (分割模型, 嵌入模型) 路径。"""
    root = Path(model_root) / "diarization"

    seg_dir = root / SEGMENTATION_DIR
    seg_model = seg_dir / "model.onnx"
    if not seg_model.exists():
        tar_path = _download(SEGMENTATION_URL, root / f"{SEGMENTATION_DIR}.tar.bz2", proxy)
        try:
            # filter="data" 是 Python 3.12+ 的推荐写法，避免路径穿越并消除弃用警告
            with tarfile.open(tar_path, "r:bz2") as tf:
                tf.extractall(root, filter="data")
        except TypeError:                       # 老版本 Python 没有 filter 参数
            with tarfile.open(tar_path, "r:bz2") as tf:
                tf.extractall(root)
        except Exception as exc:
            raise DiarizeError(f"解压分割模型失败：{exc}") from exc
        if not seg_model.exists():
            raise DiarizeError(f"解压后仍找不到 {seg_model}")

    emb_model = _download(EMBEDDING_URL, root / EMBEDDING_FILE, proxy)
    return seg_model, emb_model


# ---------------------------------------------------------------------------
# 分离
# ---------------------------------------------------------------------------
def _build(model_root: Path, threshold: float, num_speakers: int, proxy: str | None):
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise DiarizeError(
            "没有安装 sherpa-onnx。\n"
            "   修复：pip install sherpa-onnx\n"
            "   （约 2 MB，不需要 PyTorch）"
        ) from exc

    seg_model, emb_model = ensure_models(model_root, proxy)

    try:
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(seg_model)),
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb_model)),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=num_speakers, threshold=threshold),
            min_duration_on=0.5,      # 短于 0.5 秒的片段丢掉，避免碎段
            min_duration_off=0.5,     # 间隔短于 0.5 秒的两段合并
        )
        return sherpa_onnx.OfflineSpeakerDiarization(config)
    except Exception as exc:
        raise DiarizeError(f"初始化分离器失败：{type(exc).__name__}: {exc}") from exc


def run(audio, model_root: Path, threshold: float = DEFAULT_THRESHOLD,
        num_speakers: int = -1, proxy: str | None = None) -> tuple[list[Span], int, float]:
    """
    对 16 kHz 单声道 float32 数组做说话人分离。

    num_speakers = -1 表示让算法自己判断人数；已知人数时指定更准。

    返回 (说话人片段列表, 人数, 耗时秒)。
    """
    sd = _build(model_root, threshold, num_speakers, proxy)
    expected = getattr(sd, "sample_rate", 16000)
    if expected != 16000:
        raise DiarizeError(f"分离器要求 {expected} Hz，但音频是 16000 Hz")

    started = time.time()
    try:
        result = sd.process(audio)
    except Exception as exc:
        raise DiarizeError(f"分离过程失败：{type(exc).__name__}: {exc}") from exc
    elapsed = time.time() - started

    spans = [Span(float(s.start), float(s.end), int(s.speaker))
             for s in result.sort_by_start_time()]
    return spans, int(result.num_speakers), elapsed


def best_speaker(spans: list[Span], start: float, end: float) -> int | None:
    """
    给一个时间区间挑说话人：取重叠最多的那段。

    比"按起点判断"稳 —— 换人常常正好落在句中，起点还落在上一句的尾音里。
    """
    best, best_overlap = None, 0.0
    for sp in spans:
        ov = sp.overlap(start, end)
        if ov > best_overlap:
            best, best_overlap = sp, ov
    return best.speaker if best is not None else None


def assign(segments, spans: list[Span]) -> None:
    """
    给每条字幕就地打上 speaker 字段。

    规则：取与该字幕时间重叠最多的那段说话人区间。
    一条字幕可能横跨两个说话人（换人正好落在句中），这时取重叠多的那个 ——
    比"按起点判断"稳，因为起点常常在上一句的尾音里。
    """
    if not spans:
        return
    for seg in segments:
        seg.speaker = best_speaker(spans, seg.start, seg.end)


def label(speaker: int | None) -> str:
    """说话人编号转成可读标签。None 表示没分离。"""
    if speaker is None:
        return ""
    return f"S{speaker + 1}"
