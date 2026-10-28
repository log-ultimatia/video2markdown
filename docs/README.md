# 文档索引

| 文档 | 内容 |
|---|---|
| [VALIDATION_noise_and_diarization.md](VALIDATION_noise_and_diarization.md) | **验证报告** —— 噪声对识别的影响、降噪是否值得做、说话人分离的实测短板 |
| [TECHNICAL_NOTES.md](TECHNICAL_NOTES.md) | **实现笔记** —— 设计取舍、关键参数理由、踩过的坑、全部实测数据 |
| [LANDSCAPE.md](LANDSCAPE.md) | **同类方案调研** —— 开源项目（BiliNote / Buzz / whishper / Whisper-WebUI 等）与商业软件对比 |
| [COMPARISON_buzz.md](COMPARISON_buzz.md) | 与 **Buzz** 的双语视频实测对比（含"为什么它会丢 75% 内容"） |
| [COMPARISON_3way.md](COMPARISON_3way.md) | 与 **tube2note**、**video-to-notes** 的三方对比 |
| [COMPARISON_tube2note.md](COMPARISON_tube2note.md) | 与 **tube2note** 1.0.2 的详细对比 |
| [OUTPUT_SAMPLES.md](OUTPUT_SAMPLES.md) | 三方案的实际输出样例（Markdown 对照） |
| [buzz_raw_segments.json](buzz_raw_segments.json) | Buzz 的原始转录数据（毫秒时间戳），上面那份对比的证据 |
| `preview-*.png` | 界面与预览窗口截图，主 README 引用 |

---

## 读的顺序建议

1. 只想用 → 回 [主 README](../README.md)
2. 想知道**为什么这么设计** → [TECHNICAL_NOTES.md](TECHNICAL_NOTES.md)
3. 想选型、看别人怎么做的 → [LANDSCAPE.md](LANDSCAPE.md)
4. 想验证"中英混排"这个说法 → [COMPARISON_buzz.md](COMPARISON_buzz.md)

---

## 关于这些对比数据

所有对比都是**同一台机器、同一模型、同一段素材**跑出来的，命令行都写在对应文档里，可以复现。

需要说明的两点：

- `COMPARISON_*.md` 里的第三方工具版本是当时的版本，之后可能已经改进了
- 速度数字受机器负载影响很大，**只适合相对比较**，不适合当绝对基准
