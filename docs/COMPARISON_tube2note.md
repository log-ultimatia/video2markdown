# tube2note vs 自建流水线 —— 对比报告

> 环境：Windows 11 / Python 3.13.15 / 代理 Clash 127.0.0.1:7890
> 自建方案：`pipeline.py`（yt-dlp + ffmpeg + faster-whisper `base`，CPU int8）
> 对比方案：`tube2note` 1.0.2（`omersusin/tube2note`，★19，MIT）
> 对比日期：2026-10-21

---

## 0. 一句话结论

**tube2note 更快、导出格式更多，但它不做语音识别——只抓 YouTube 已有的字幕。**
因此它只能处理"有字幕的视频"，而无字幕视频会直接跳过（0 字）。
**自建方案对任意视频都能转写，且转写质量更高（带标点、术语更准）。**

---

## 1. 样本选择

任务的对比要求"同一视频"。但首选样本（CS61A 讲座 `zYC7tKfKPtM`）**没有任何字幕轨**
（`subtitles.keys = NA`、`automatic_captions.keys = NA`），tube2note 无法处理。

于是改用两个视频，各承担一个目的：

| 用途 | 视频 | 时长 | 字幕情况 |
|---|---|---|---|
| **主要对比**（两边都能跑） | `q2SGW2VgwAM` — Python Object Oriented Programming in 10 minutes | 10:04 | 有英文自动字幕（157 轨） |
| **能力边界测试**（无字幕） | `zYC7tKfKPtM` — CS61A: Names, Assignment... | 8:46 | **无任何字幕** |

---

## 2. 四维对比

### 2.1 转写准确率 —— 自建胜

同一视频、同一时间段，逐条核对原文（人工确认原话）：

| 原文 | 自建（Whisper base） | tube2note（YouTube 自动字幕） |
|---|---|---|
| `def stop` | ✅ `DEF, stop` | ❌ **`tef stop`** |
| `two underscores again` | ✅ `two underscores again` | ❌ **`200 scores again`** |
| `__init__` 拼读 | ✅ `two underscores, I-N-I-T` | ⚠️ `two underscores i n i t`（无停顿标记） |

**量化对比**（同一份 10 分钟音频）：

| 指标 | 自建 | tube2note |
|---|---|---|
| 总词数 | 1757 | 1742 |
| 大写开头的词 | **144** | 20 |
| 句读标点（. , ! ?） | **230** | 13 |

> 词数几乎相同，说明**内容覆盖度一致**；但 tube2note 拿到的是 YouTube 的原始
> 自动字幕——全小写、无标点、按 40 秒一大块堆在一起。要读或要引用，都得先清洗。

### 2.2 Markdown 结构化程度 —— 各有优势

**自建**：

```markdown
## 00:00:13 ~ 00:00:27

If I could have just a moment of your time, please like, comment, and subscribe.
I would greatly appreciate it. Welcome to today's topic on Python object oriented
programming, also known as P-O-O-P,
```

- 每段约 13 秒，**带起止时间戳**，段落边界按"说话人停顿"切分
- 带标点和大小写，**正文直接可读**
- 顶部有元信息（来源/时长/语言置信度/模型/字幕条数）

**tube2note**：

```markdown
## My Notes
## Transcript
<details>
<summary>Show transcript</summary>

[00:00] hey what's going on everybody it's bro hope you're doing well and in this
video i'm going to explain object oriented programming in python ...（连续 300+ 词）
```

- 时间戳每 40 秒一个（`--ts-every` 可调），无段落切分
- **全小写无标点**，且正文被折叠在 `<details>` 里
- 但胜在**导出格式丰富**：`--srt` `--vtt` `--txt` `--pdf` `--epub` `--anki` `--obsidian`
  `--cite`（APA/MLA/Chicago/BibTeX）`--chapters`（按视频章节分组）`--sponsorblock`（跳过广告段）

> 结构化评分：**阅读体验 自建胜，导出能力 tube2note 胜。**

### 2.3 处理速度 —— tube2note 快 2.2 倍，但比得不公平

| 阶段 | 自建 | tube2note |
|---|---|---|
| 下载 | 9.3 MB 音频（`bestaudio`） | 仅字幕（KB 级） |
| 提取 | ffmpeg → 18.4 MB WAV（1.4s） | 无 |
| 转写 | **Whisper 147 条字幕（约 24s）** | **无（直接取现成字幕）** |
| **合计** | **37.8s** | **16.8s** |

**关键**：tube2note 快，是因为它**跳过了最贵的那一步**（语音识别）。
它并没有做转写，只是把 YouTube 已经算好的字幕下载下来。

所以这个速度对比的正确表述是：
> **"抓字幕" vs "跑语音识别"**，前者天然快一个数量级，不是算法优化的差距。

### 2.4 代理配置便利性 —— 平手

| | 自建 | tube2note |
|---|---|---|
| 参数 | `--proxy http://127.0.0.1:7890` | `--proxy http://127.0.0.1:7890` |
| 环境变量 | `HTTP_PROXY` / `HTTPS_PROXY` | 未发现（靠参数） |
| 代码内硬编码 | 支持（`HARDCODED_PROXY`） | 不支持 |
| 默认值 | 自动退回 `127.0.0.1:7890` | 无默认，不传则直连 |
| 失败提示 | 先 socket 探活 + 退回直连并提示 | 由 yt-dlp 原样抛错 |

> 自建方案多了"默认值 + 端口探活 + 可读报错"三层；tube2note 是标准的单参数设计。
> 日常使用差别不大，**排错时自建更友好**。

---

## 3. 能力边界（本次最重要的发现）

### 3.1 无字幕视频：tube2note 完全无能为力

```
$ tube2note "https://www.youtube.com/watch?v=zYC7tKfKPtM" --proxy ... --lang en
1 videos found
0/1 videos · 0 words
│ 0 videos -> tube2note.md (1 skipped)
## Skipped
- `Names, Assignment, and User-Defined Functions` — subtitle too short
```

注意它的提示是 **"subtitle too short"（字幕太短）**，但实际是**根本没有字幕**——
这个措辞会误导用户去猜是不是视频太短。

同一视频，自建方案：

```
[OK] 识别出 119 条字幕
     语言 en（置信度 1.00）
     音频 8:46，耗时 24.3s，实时倍率 21.7x
[OK] 31 段，7.7 KB
```

### 3.2 它的本地转写引擎在 1.0.2 里"广告了却没发货"

`--help` 明确写着：

```
--engine ENGINE   transcribe engine: api or local (whisper.cpp extra)
--whisper-model {tiny,base}   whisper.cpp model for --engine local
```

但实际执行：

```
$ tube2note ... --transcribe --engine local --whisper-model base
This needs the 'whisper' extra: Offline transcription via whisper.cpp binary + tiny model (~75MB)
'whisper' still not available after install.
local transcription unavailable — stopping before any work.

$ pip install "tube2note[whisper]"
WARNING: tube2note 1.0.2 does not provide the extra 'whisper'
```

**发布包里没有声明 `whisper` 这个 optional dependency**，所以整条本地转写路径是断的。
无字幕视频唯一的出路是 `--transcribe`（走 Gemini API，需 `GEMINI_API_KEY`，本次未测试）。

### 3.3 其它观察

- 它的默认字幕语言优先级是 `tr,en`（作者是土耳其人）。中文/其它语种视频要显式 `--lang`
- 它的定位是 **"批量收集频道/播放列表并合并成一个 Markdown 喂给 NotebookLM"**，
  不是"单视频精细转写"。这解释了它为什么重视 `--max` `--since` `--watch` `--dedupe`
  这类**批量管理**参数，而不是转写质量

---

## 4. 汇总对照表

| 维度 | 自建流水线 | tube2note 1.0.2 | 胜方 |
|---|---|---|---|
| 转写方式 | 本地 Whisper ASR | 下载 YouTube 已有字幕 | — |
| 处理无字幕视频 | ✅ 可以 | ❌ 直接跳过 | **自建** |
| 转写准确率（术语） | ✅ 正确 | ❌ `tef stop` / `200 scores` | **自建** |
| 标点与大小写 | ✅ 144 大写 / 230 标点 | ❌ 20 大写 / 13 标点 | **自建** |
| 段落切分 | ✅ 13 秒/段，带起止时间 | ⚠️ 40 秒/块，无切分 | **自建** |
| 正文可读性 | ✅ 直接可读 | ⚠️ 折叠在 `<details>` 中 | **自建** |
| 处理速度（10 分钟视频） | 37.8s | **16.8s** | **tube2note** |
| 导出格式 | md + 可选 PDF | **md/srt/vtt/txt/pdf/epub/anki/obsidian** | **tube2note** |
| 章节/引用/跳过广告 | ❌ 无 | ✅ 有 | **tube2note** |
| 批量与断点续传 | ❌ 无（单视频） | ✅ `--watch` `--resume` `--dedupe` | **tube2note** |
| 代理参数 | ✅ `--proxy` + 环境变量 + 默认值 | ✅ `--proxy` | 平手 |
| 代理排错提示 | ✅ 探活 + 可读报错 | ⚠️ 原样抛错 | **自建** |
| 依赖体量 | 较重（faster-whisper + ctranslate2，~300 MB） | **极轻（yt-dlp + certifi）** | **tube2note** |
| 项目成熟度 | 自建，可控 | ★19，个人项目 | — |

---

## 5. 推荐

### 结论：**两者不是替代关系，而是互补关系**

**如果目标是"任意视频 → 转写文本"，推荐自建方案。** 理由：

1. **能力边界是决定性的**：tube2note 只能处理有字幕的视频。实测两个样本里就有一个
   （CS61A 讲座）完全处理不了，而这类"UP 主关闭字幕/YouTube 未生成字幕"的视频非常常见
2. **转写质量确实更好**：带标点、大小写正确、术语准确（`def` / `two underscores` 都对）
3. **它的本地转写兜底在当前版本是坏的**（`whisper` extra 未发布），意味着能力边界无法扩展

**如果目标明确是"批量把有字幕的频道/播放列表转成 NotebookLM 的知识源"，推荐 tube2note。**
它的批量管理（`--watch` `--resume` `--dedupe` `--max` `--since`）、多格式导出、
章节与引用生成，都是自建方案没做的，而且依赖极轻、装完即用。

### 如果要融合（推荐做法）

把 tube2note 的**"字幕优先"策略**移植到自建方案，形成三级降级：

```
官方字幕 → YouTube 自动字幕 → 本地 Whisper ASR
```

- 前两级秒级完成，命中率高
- 第三级保证"任何视频都能处理"
- 实测收益：在本次的 Python 视频上，前两级就能命中，**耗时可以从 37.8s 降到约 17s**
- 代价：字幕轨没有标点，需要额外做一次标点恢复（可交给小模型，或干脆保留 Whisper 输出）

这也正好回答了本作业的核心问题：**"转写"这件事，取现成字幕和质量保证之间要权衡**——
tube2note 选了取巧的那条，自建选了稳妥的那条。
