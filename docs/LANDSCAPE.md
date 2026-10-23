# 同类方案调研与性能对比（v2）

> 本文回答三个问题：
> 1. 除了 tube2note / video-to-notes，还有哪些开源项目做类似的事？
> 2. 市面现成软件有哪些？
> 3. 性能怎么比？我实测了什么、你还需要手动验证什么？
>
> **修正了 v1 的两个偏见**：不只看 YouTube→Markdown，也不按星标筛选。

---

## 0. 一句话结论

| 你的需求 | 推荐 |
|---|---|
| 只要**本地、免费、可批量**，输出带时间戳的逐段文本 | **本项目**（纯本地，无 LLM，无 key） |
| 要**总结式笔记**（AI 提炼要点、分章节） | **BiliNote**（★7.6k，支持 B站/YouTube/抖音/快手/本地，需配 LLM key） |
| 要**图形界面、拖文件就用** | **Buzz**（★22k，桌面离线）或 **Vibe**（★7.9k） |
| 要**云端省事、不折腾环境** | 通义听悟 / 飞书妙记（中文场景）或 BiliNote Pro 在线版 |

---

## 1. 开源项目全览

### 1.1 视频 → 笔记/文本（和本项目同类）

| 项目 | ★ | 许可 | 来源覆盖 | ASR 引擎 | 输出 | 需 LLM？ | 部署 |
|---|---|---|---|---|---|---|---|
| **JefferyHcool/BiliNote** | 7,595 | MIT | B站/YouTube/抖音/快手/**本地** | Fast-Whisper 等（本地） | **结构化 Markdown 笔记**（含截图/跳转） | **是**（OpenAI/DeepSeek/Qwen） | Docker / 桌面客户端 / 在线版 |
| **pluja/whishper** | 3,094 | AGPL-3.0 | **任意音频**（本地文件/URL） | Whisper（本地） | 字幕（SRT/VTT）+ 翻译编辑 | 否 | Docker + Web UI |
| **jhj0517/Whisper-WebUI** | 2,895 | Apache-2.0 | 本地音视频 + YouTube | Whisper（本地） | 字幕 + 辅助编辑 | 否 | Python Web UI |
| **tube2note**（v1 已测） | 19 | — | 仅 YouTube，**只读字幕** | 无（声称的 whisper 未发货） | Markdown（字幕原文） | 可选 | pip |
| **video-to-notes**（v1 已测） | 106 | — | 仅 YouTube，只读字幕 | 无 | txt/json/html（Markdown 需 publish-obsidian） | 是 | Node 22+ |
| **本项目（youtube2md_lab）** | — | — | YouTube + **本地文件** | faster-whisper（本地） | **带时间戳的逐段 Markdown** | **否** | Python + tkinter GUI |

### 1.2 桌面/通用转写（不做"笔记"，但能转任何视频）

| 项目 | ★ | 许可 | 特点 |
|---|---|---|---|
| **chidiwilliams/buzz** | 22,151 | MIT | 桌面离线转录/翻译，拖文件即用，跨平台，Whisper 系 |
| **thewh1teagle/vibe** | 7,873 | MIT | 跨平台桌面转写，支持任意音视频 |
| **Makememo/MemoAI** | 1,076 | 无声明 | 视频→翻译文本/字幕/笔记，桌面应用，有商用版 |

### 1.3 底层引擎（如果你要自己做，值得复用）

| 项目 | ★ | 许可 | 为什么值得看 |
|---|---|---|---|
| **modelscope/FunASR** | 20,798 | MIT | 中文 ASR 强项，自带 **VAD + 标点恢复（punc）**，可根治"中文没标点" |
| **k2-fsa/sherpa-onnx** | 15,456 | Apache-2.0 | 高效运行时，手机/嵌入式也能跑 |
| **SYSTRAN/faster-whisper** | 26,067 | MIT | 本项目用的引擎，CTranslate2 加速 |

---

## 2. 市面现成软件

| 产品 | 类型 | 来源 | 输出 | 中文 | 备注 |
|---|---|---|---|---|---|
| **通义听悟**（阿里） | 云端 SaaS | 上传音视频/链接 | 转写稿 + AI 摘要 | ✅ 强 | 免费额度，导出 Markdown 需手动整理 |
| **飞书妙记** | 云端 SaaS | 上传/会议 | 文稿 + 纪要 | ✅ 强 | 强在会议场景，长视频慢 |
| **Notta** | SaaS/App | 上传/链接 | 转写 + 摘要 | ✅ | 多语言识别好，免费额度有限 |
| **BibiGPT** | 云端 | B站/YouTube 链接 | 总结式笔记 | ✅ | 总结式，不是逐段转录 |
| **Eightify** | 浏览器插件 | YouTube | 要点摘要 | 一般 | 不做完整转录 |
| **Glasp** | 浏览器插件 | 网页/YouTube | 高亮 + 摘要 | 一般 | 偏阅读标注 |
| **Otter.ai** | SaaS | 会议 | 转写 + 摘要 | ❌ 弱 | 英文会议场景最佳 |
| **MacWhisper** | 桌面（macOS） | 任意文件 | 转写/字幕 | ✅ | 付费，仅 macOS |
| **剪映 / CapCut** | 桌面/移动 | 任意视频 | 自动字幕（可导出 SRT） | ✅ | 不做笔记，字幕质量够用 |
| **BiliNote Pro**（在线版） | 云端 | 多平台链接 | Markdown 笔记 | ✅ | 就是上面开源项目的托管版 |

**关键分野**（决定"能不能比"）：

| 维度 | 本项目 | BiliNote / BibiGPT 类 | 通义听悟 / 妙记 类 |
|---|---|---|---|
| 输出形态 | **逐段时间戳文本** | **AI 总结的笔记** | 转写稿 + 摘要 |
| 是否需 LLM key | **不需要** | 需要（要花钱） | 不需要（在云里） |
| 数据出不出本机 | **不出** | 总结阶段会出去 | 全部上传 |
| 能否离线 | **能** | 转写能，总结不能 | 不能 |

> ⚠️ 这三类**不是同一个产品**。拿"总结质量"比"转录准确率"没有意义。
> 建议分两栏评：**转录保真度** 和 **笔记可用性**。

---

## 3. 我实测了什么

### 3.1 测试素材

`bench/bilingual.mp4` / `bench/bilingual.wav`（150 秒，**中英混排**）

```
0-60s     中文（iPhone Duo 评测）
60-105s   英文（CS61A 课程）
105-150s  中文（iPhone Duo 评测）
```

可以直接拿去上传商业软件验证。

### 3.2 本项目（faster-whisper small，CPU int8）

| 指标 | 数值 |
|---|---|
| 耗时 | **33.3s / 150s 音频 = 4.5x 实时** |
| 英文词 | **133**（修复前 1~17） |
| 幻觉复读（"中文字幕"类） | **0**（修复前 13 处） |
| 繁体字 | **0** |
| 中文标点 | 18 |
| 含完整英文句子 | ✅ |

### 3.3 已测过的其它方案（v1 数据，同一英文视频 604s）

| 方案 | 耗时 | 字幕来源 | 结构化 | 错误率（4 点抽查） |
|---|---|---|---|---|
| 本项目（base） | 37.8s（21.7x） | 本地 ASR | 41 个时间戳段 | 1/4 |
| tube2note | 16.8s | YouTube 字幕 | 3 段 | 3/4 |
| video-to-notes | 59.1s | YouTube 字幕 | 2 段 | 1/4 |

> tube2note / video-to-notes **只读字幕**，无字幕视频直接跳过 —— 这是能力边界，不是速度差异。

### 3.4 我这轮顺手修掉的一个真 bug

**症状**：中英混排视频里，英文段整段丢失，且出现"中文字幕。中文文字。中文語字幕."的复读幻觉。

**根因**：整段一次性转写时，模型延续上一段的语言上下文，把纯英文段念成中文复读。

**修法**：分块从 300s 缩短到 **60s**，**每块单独检测语言 + 单独挑提示词**。

| 做法 | 英文词 | 幻觉 |
|---|---|---|
| 整段一次性 | 1 | 13 |
| 逐块 120s | 16 | 0 |
| **逐块 60s** | **116** | **0** |
| 逐块 45s | 21 | 0 |

---

## 4. 你要手动验证的（模板）

### 4.1 建议的测试对象

| 优先级 | 对象 | 怎么拿 |
|---|---|---|
| 1 | **BiliNote**（★7.6k） | `github.com/JefferyHcool/BiliNote` → Releases 下载 `BiliNote_2.4.5_x64-setup.exe`（98MB），或在线版 bilinote.app |
| 2 | **Buzz**（★22k） | `github.com/chidiwilliams/buzz` → Releases 桌面版 |
| 3 | **通义听悟** | tingwu.aliyun.com 上传 `bench/bilingual.mp4` |
| 4 | 飞书妙记 / Notta | 二选一即可 |

> BiliNote 和 Buzz 都需要**首次下载模型**（几百 MB），第一次跑会慢，别算进性能。

### 4.2 填这张表

| 项目 | 部署耗时 | 150s 音频处理耗时 | 英文段是否保留 | 中文是否简体 | 中文有标点 | 有无复读幻觉 | 输出是否 Markdown | 需不需要 API key | 主观评分（1-5） |
|---|---|---|---|---|---|---|---|---|---|
| 本项目 | 已装好 | 33.3s | ✅ 133 词 | ✅ | ✅ 18 个 | ✅ 0 | ✅ | ❌ 不需要 | |
| BiliNote | | | | | | | | 需要 | |
| Buzz | | | | | | | | | |
| 通义听悟 | | | | | | | | | |
| 其它： | | | | | | | | | |

### 4.3 验证时看这 5 处（最容易暴露问题）

1. **60s 处**（中文→英文切换点）：英文有没有变成中文，或者变成"中文字幕"复读
2. **105s 处**（英文→中文切换点）：有没有漏掉半句
3. **"iPhone Duo"** 有没有被写成 "iPhone 2" / "iPhone Do"（小模型常见错）
4. **"500次折叠"** 有没有被写成 "500字折跌"（中文数字量词错误）
5. 中文部分有没有**整段没有标点**

---

## 5. 参考链接

- [JefferyHcool/BiliNote](https://github.com/JefferyHcool/BiliNote) — AI 视频笔记，多平台，★7.6k
- [pluja/whishper](https://github.com/pluja/whishper) — 本地 Web UI 转写，★3.1k
- [jhj0517/Whisper-WebUI](https://github.com/jhj0517/Whisper-WebUI) — 字幕 Web UI，★2.9k
- [chidiwilliams/buzz](https://github.com/chidiwilliams/buzz) — 桌面离线转写，★22k
- [thewh1teagle/vibe](https://github.com/thewh1teagle/vibe) — 跨平台桌面转写，★7.9k
- [modelscope/FunASR](https://github.com/modelscope/FunASR) — 中文 ASR + 标点恢复，★20.8k
- [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) — 高效运行时，★15.5k
- [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 本项目引擎，★26k
- [BiliNote Pro 在线版](https://www.bilinote.app/) — 开箱即用
- [通义听悟](https://tingwu.aliyun.com/)、[飞书妙记](https://www.feishu.cn/product/minutes)
