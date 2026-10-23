# 三方案对比报告：自建 vs tube2note vs video-to-notes

> 环境：Windows 11 / Python 3.13.15 / RTX 4070 8GB / 代理 Clash `127.0.0.1:7890`
> 日期：2026-10-21
> 样本：`q2SGW2VgwAM`（Python OOP in 10 minutes，604 秒，有英文自动字幕）
> 补充样本：`zYC7tKfKPtM`（CS61A 讲座，526 秒，**无任何字幕**）

| 代号 | 方案 | 版本 | Stars | 转写方式 |
|---|---|---|---|---|
| **A** | 自建流水线 `pipeline.py` | — | — | faster-whisper `base`，beam=1，VAD 开 |
| **B** | `tube2note`（`omersusin`） | 1.0.2 | ★19 | **不做 ASR**，只下 YouTube 已有字幕 |
| **C** | `video-to-notes`（`KIRVO-REPORTING`） | 0.2.0 | ★106 | faster-whisper `base`，**beam=5**，无 VAD |

---

## 0. 一句话结论

**只有 A 和 C 做了真正的语音识别**，B 本质是"字幕下载器"。
C 的转写质量与 A 相当（同引擎），但**部署摩擦显著更高**（4 个障碍，见第 4 节），
且本地模式**不输出 Markdown**。
**A 在"任意视频 → 带时间戳的 Markdown"这个目标上最省事、最可靠。**

---

## 1. 三方定位差异（这决定了后面所有对比）

| | A 自建 | B tube2note | C video-to-notes |
|---|---|---|---|
| 定位 | 单视频转写工具 | **批量收集频道/播放列表** → NotebookLM 知识源 | 单视频 → 报告/笔记（Notion/Obsidian） |
| 转写来源 | 本地 ASR（唯一路径） | 官方字幕（唯一路径，无兜底） | 字幕优先 → 本地 Whisper 兜底 |
| 无字幕视频 | ✅ 正常处理 | ❌ 直接跳过 | ✅ 走兜底（需先配好） |

> C 的策略其实是三者中最"聪明"的：**官方字幕 → 自动字幕 → 本地 Whisper** 三级降级。
> 但它把这套能力藏在多层配置之后，默认状态不可用（见第 4 节）。

---

## 2. 四维对比

### 2.1 转写准确率 —— A ≈ C >> B

同一段音频（`q2SGW2VgwAM`），逐条核对原话：

| 原文 | A 自建 | B tube2note | C video-to-notes |
|---|---|---|---|
| `def stop` | ✅ `DEF stop` | ❌ **`tef stop`** | ✅ `DEF stop` |
| `two underscores again` | ✅ 正确 | ❌ **`200 scores again`** | ✅ 正确 |
| `I-N-I-T`（字母拼读） | ✅ `I-N-I-T` | ⚠️ `i n i t` | ⚠️ `I and I T` |
| `new Python file` | ✅ `new Python file` | ⚠️ `new python file` | ❌ **`new pipe on file` / `fight on file`** |
| `self.year` | ❌ **`self.ir`** | ❌ **`self.ear`** | ✅ `self dot year` |
| 标点 | ✅ 有 | ❌ 全无 | ✅ 有 |
| 大小写 | ✅ 有 | ❌ 全小写 | ✅ 有 |

**量化（同一份 10 分钟音频）**：

| 指标 | A 自建 | B tube2note | C video-to-notes |
|---|---|---|---|
| 总词数 | 1757 | 1742 | 1742 |
| 大写开头的词 | **144** | 20 | ~140 |
| 句读标点 | **230** | 13 | ~220 |
| 明显错词（抽查 4 处） | 1 | **3** | 1 |

> **A 和 C 都用同一个模型（faster-whisper base），准确率处于同一档**：
> 各在 4 个抽查点里错 1 处，且错的不是同一处（A 错 `self.year`，C 错 `new Python file`）。
> 说明 **beam=1 vs beam=5 对准确率没有决定性影响**，却带来 2.4 倍速度差。
>
> **B 明显差一档**：4 个抽查点错 3 处，且它拿的是 YouTube 原始自动字幕，
> 无标点、全小写、术语错误率最高。

### 2.2 Markdown 结构化程度 —— 各有取舍

**A 自建**（10.1 KB，41 个二级标题，84 处时间戳）

```markdown
# Python Object Oriented Programming in 10 minutes 🐍

- **来源**：https://www.youtube.com/watch?v=q2SGW2VgwAM
- **时长**：10:04
- **语言**：en（置信度 1.00）
- **转写**：faster-whisper（base，VAD 开，beam=1）
- **字幕条数**：147，合并为 41 段

---

## 00:00:13 ~ 00:00:27

If I could have just a moment of your time, please like, comment, and subscribe.
```

- ✅ **每段都有起止时间戳**，可直接当目录用、可定位
- ✅ 段落按说话停顿切分，正文直接可读
- ❌ 元信息较简陋（无频道、发布日期、frontmatter）

**C video-to-notes**（10.1 KB，**2 个**二级标题，9 处时间戳）

```markdown
---
video_to_notes_id: "q2SGW2VgwAM"
title: "Python Object Oriented Programming in 10 minutes 🐍"
platform: "youtube"
channel: "Bro Code"
published: "2020-12-28T21:27:28+00:00"
duration_seconds: 604
transcript_source: "local_whisper"
---
# Python Object Oriented Programming in 10 minutes 🐍

## Video Details
- Source: ...
- Channel: Bro Code
...

Summary pending.

## Transcript

（整篇约 6000 字一口气塞进一个 text 代码块，内部无任何时间戳）
```

- ✅ **YAML frontmatter 极其丰富**（频道、发布日期、时长、转写来源、路径），
  配合 `dashboard.html` / `index.json` 做知识库管理很顺
- ❌ **逐字稿是单一巨型块，零时间戳** → 无法定位、无法当目录
- ❌ 本地模式**根本不产 Markdown**，必须再跑 `publish-obsidian` 才有（见第 4 节）

**B tube2note**（9 KB）

```markdown
## Transcript
<details>
<summary>Show transcript</summary>

[00:00] hey what's going on everybody it's bro hope you're doing well ...（连续 300+ 词）
```

- ✅ 有 `<details>` 折叠、有 srt 副产物、导出格式最多
- ❌ 全小写无标点、40 秒一大块、正文被折叠

> **结论**：要"能读、能定位" → A 明显最好；要"能进知识库" → C 的 frontmatter 最强。

### 2.3 处理速度 —— B 最快，但只有它跳过了 ASR

| 方案 | 下载 | 提取 | 转写 | 合计 | 是否做 ASR |
|---|---|---|---|---|---|
| **B tube2note** | 字幕（KB） | 无 | **无** | **16.8s** | ❌ 否 |
| **A 自建** | 9.3 MB 音频，2.9s | 1.4s | **约 24s**（beam=1） | **37.8s** | ✅ 是 |
| **C video-to-notes** | 9.3 MB 音频（已缓存） | 内部 | **约 55s**（beam=5） | **59.1s** | ✅ 是 |

**关键解读**：
- B 的 16.8s **不含语音识别**，和 A/C 不是同一件事，不能直接比
- A vs C 才是公平对比：**A 快 2.4 倍**，主要因为 `beam_size=1` vs `beam_size=5`，
  另外 A 用 VAD 掐掉了静音段（C 没开 VAD）
- A 还省了 C 的一步：C 要写 `report.html` / `dashboard.html` / `index.json`，A 只写一个 md

### 2.4 代理配置便利性 —— A > B > C

| | A 自建 | B tube2note | C video-to-notes |
|---|---|---|---|
| 命令行参数 | ✅ `--proxy URL` | ✅ `--proxy URL` | ❌ **没有该参数** |
| 环境变量 | ✅ `HTTP_PROXY`/`HTTPS_PROXY` | ❌ 未发现 | ✅ 实测 `HTTPS_PROXY` **有效** |
| 默认值 | ✅ 自动用 `127.0.0.1:7890` | ❌ 不传就直连 | ❌ 无 |
| 代码内硬编码 | ✅ `HARDCODED_PROXY` | ❌ | ❌ |
| 端口探活 + 可读报错 | ✅ 有 | ⚠️ 原样抛错 | ⚠️ 原样抛错 |

> **C 最出人意料**：它是个主打 YouTube/Bilibili 的中文项目，
> 但 `process --help` 里**完全没有代理选项**，只能靠环境变量。
> 国内用户第一次跑大概率卡在下载失败，且报错不会提示"你可能需要代理"。

---

## 3. 能力边界（重要）

### 3.1 无字幕视频

```
样本 zYC7tKfKPtM（CS61A 讲座，无任何字幕轨）

A 自建          : [OK] 识别出 119 条字幕，实时倍率 21.7x → 31 段 Markdown
B tube2note     : 0 videos -> (1 skipped)  "subtitle too short"  ← 措辞误导，实际是没字幕
C video-to-notes: 可以（走 Whisper 兜底），但需先正确配置
```

### 3.2 B 的本地转写引擎在当前版本是坏的

```
$ tube2note --transcribe --engine local --whisper-model base
'whisper' still not available after install.

$ pip install "tube2note[whisper]"
WARNING: tube2note 1.0.2 does not provide the extra 'whisper'
```

`--help` 宣传了 `--engine local`，但**发布包没有声明这个 extra**，整条路径是断的。

---

## 4. 部署摩擦对比（本次最有价值的部分）

我在同一台机器上真实部署三者，记录遇到的每个障碍：

| 障碍 | A 自建 | B tube2note | C video-to-notes |
|---|---|---|---|
| 安装方式 | `pip install yt-dlp faster-whisper ffmpeg-python` | `pip install tube2note`（一行） | **`git clone` + `pip install -e .`**（未上 PyPI） |
| 依赖体量 | ~300 MB | **~10 MB** | ~300 MB |
| **需要 JS 运行时** | ❌ 不需要（yt-dlp 只警告） | ❌ 不需要 | ✅ **硬性要求 Node 22+/Deno 2.3+，否则直接拒跑** |
| **PyAV 19 冲突** | ✅ 已规避（自己读 WAV 成数组） | — 不涉及 | ❌ **崩溃**，需 `sitecustomize.py` 垫片 |
| **CUDA 运行库缺失** | ✅ 自动退回 CPU | — 不涉及 | ❌ **崩溃**，需手改 `config.json` 设 `device=cpu` |
| 配置交互 | 无（参数直给） | 无 | ⚠️ `--model` 与 `--model-choice none` 冲突，报错绕 |
| **本地产出 Markdown** | ✅ 直接输出 | ✅ 直接输出 | ❌ **本地只有 txt/json/html**，Markdown 需再跑 `publish-obsidian` |
| ffmpeg | 需系统安装（显式检测 + 平台安装提示） | 不需要 | `imageio-ffmpeg` 自带，但 doctor 仍读系统 ffmpeg |

### 实际踩坑记录（C）

按时间顺序，我为了让 C 跑起来做的事：

1. **`RuntimeError: YouTube requires Deno 2.3+ or Node.js 22+`**
   → 用环境里已有的 Node v24.21.0 加进 PATH 才通过

2. **`configure` 报错**：`Recommended/custom Whisper setup was selected but not installed`
   → 发现 `--model base` 和 `--model-choice none` 冲突，只能二选一
   → 用 `--model-choice none` 通过，但**这导致 backend 被设为 `none`（禁用转写）**

3. **`process` 报错**：`No usable captions found and local Whisper fallback is disabled in config.json`
   → 命令行 `--backend faster-whisper --model base` **覆盖不了 config.json**
   → 只能手工编辑 `config.json` 把 `profile.backend` 改成 `faster-whisper`

4. **`TypeError: open() got an unexpected keyword argument 'metadata_errors'`**
   → faster-whisper 1.2.1 + PyAV 19 不兼容（**和我方案里遇到的是同一个 bug**）
   → 降级 `av==14.4.0` **失败**（无 cp313 轮子，源码编译报错）
   → 最终写 `sitecustomize.py` 垫片，把 `metadata_errors` 参数吞掉

5. **`RuntimeError: Library cublas64_12.dll is not found`**
   → 它的配置 `device=auto` 选了 CUDA 但缺运行库，**且没有 CPU 兜底**
   → 手工改 `config.json` 设 `device=cpu, compute_type=int8`

6. **本地模式不产 Markdown**
   → 额外跑 `publish-obsidian --obsidian-vault <path>` 才拿到 `.md`

**对比 A**：A 只需 `pip install` 三件套 + 设一个 `HF_ENDPOINT` 镜像，
上述第 4、5 两个坑我在**开发阶段就已内建修复**（自己读 WAV + CUDA 自动降级），
用户跑的时候不会遇到。

---

## 5. 汇总评分

| 维度 | A 自建 | B tube2note | C video-to-notes |
|---|---|---|---|
| 转写准确率 | ★★★★☆ | ★★☆☆☆ | ★★★★☆ |
| Markdown 结构化（可定位） | ★★★★★ | ★★☆☆☆ | ★★☆☆☆ |
| Markdown 元数据（知识库） | ★★☆☆☆ | ★★★☆☆ | ★★★★★ |
| 处理速度 | ★★★☆☆ | ★★★★★（未做 ASR） | ★★☆☆☆ |
| 代理配置 | ★★★★★ | ★★★☆☆ | ★★☆☆☆ |
| 部署省心度 | ★★★★☆ | ★★★★★ | ★☆☆☆☆ |
| 无字幕视频支持 | ★★★★★ | ☆☆☆☆☆ | ★★★★☆ |
| 额外能力（批量/导出/摘要） | ★☆☆☆☆ | ★★★★★ | ★★★★★ |

---

## 6. 推荐

### 就本任务（"给一个 YouTube，返回转写 Markdown"）而言：**推荐 A 自建方案**

理由按重要性排序：

1. **能力完整**：唯一一个**默认状态下**就能处理任意视频（含无字幕）、并用一条命令出 Markdown 的
2. **结构最实用**：41 个带时间戳的段落，可直接当目录、可定位，这正是"转写文本"该有的样子
3. **部署最省心**：JS 运行时 / PyAV / CUDA 三个障碍在 A 里都不存在或已内建修复
4. **代理最友好**：有默认值、有端口探活、有可读报错

### 什么时候该选 B 或 C

- **选 B tube2note**：目标明确是"批量把有字幕的频道转成 NotebookLM 知识源"。
  它的批量管理（`--watch` `--resume` `--dedupe`）、多格式导出、极轻依赖（10 MB）无可替代。
- **选 C video-to-notes**：需要 YAML frontmatter、HTML 报告、Notion/Obsidian 同步、
  多视频 dashboard 这类"知识库工作流"。它的元数据管理是三者中最强的。
  但要接受：**部署 4 个坑 + 本地不出 Markdown**。

### 如果做融合（推荐给生产环境）

三者各有一块 A 缺的能力，值得吸收：

| 从谁 | 吸收什么 | 收益 |
|---|---|---|
| **C** | **字幕优先 → ASR 兜底** 的三级降级 | 有字幕的视频从 24s 降到约 2s，省掉最贵的一步 |
| **C** | YAML frontmatter（频道/日期/来源/转写方式） | 方便检索和归档 |
| **B** | `--ts-every` 控制时间戳密度 | 长视频只留每 30 秒一个，文件体积可控 |
| **B** | `--cite` 生成引用（APA/MLA/BibTeX） | 学术用途 |

**最关键的融合点是"字幕优先"**：本次的 Python 视频有自动字幕，
若采用三级降级，耗时可从 37.8s 降到约 17s，质量损失可用标点恢复弥补。

---

## 附：本次产出文件位置

```
youtube2md_lab/
├── pipeline.py                   A 方案（五层流水线）
├── output/                       A 的 Markdown 产物
├── tube2note_env/                B 的独立环境
├── output_tube2note/             B 的产物（md + srt）
├── v2n_env/                      C 的独立环境（含 sitecustomize.py 垫片）
├── vendor/video-to-notes/        C 的源码（git clone）
├── output_v2n/                   C 的本地产物（txt/json/html + config.json）
├── obsidian_vault/               C 的 Markdown 产物（publish-obsidian 生成）
└── COMPARISON_v1_tube2note.md    早期两方对比（保留存档）
```

---

## 补记：第 4 个抽查点（自我修正）

初版报告只抽查了 3 处，结论写成「A 0 错 / C 2 错」。后来补抽第 4 处 `self.year`，结果推翻了这个印象：

| 原文 | A 自建 | B tube2note | C video-to-notes |
|---|---|---|---|
| `self.year` | ❌ `self.ir` | ❌ `self.ear` | ✅ `self dot year` |

**修正后的结论**：
- **A ≈ C**，两者各错 1 处，且错的位置不同 —— 同模型下 beam 大小不决定准确率
- **B 最差**（错 3 处），因为它不跑 ASR
- 没有哪个方案是"零错误"的，**声学相近的词（year/ir/ear）是所有方案的共同弱点**

这个更正本身也说明一件事：**小样本抽查很容易得出过于乐观的结论**，
如果要严谨对比，应该在多个视频、多个位置上统计词错率（WER），而不是抽 3~4 个点。
