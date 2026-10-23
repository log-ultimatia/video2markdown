# 三方案 Markdown 输出对照（同一段内容）

样本：`q2SGW2VgwAM` — Python Object Oriented Programming in 10 minutes（604 秒）

三个片段取自**同一段音频**（约 02:36–03:44，讲 `def stop` 和 `__init__` 的地方），
可以直接看出三种完全不同的 Markdown 结构。

| | 字符数 | 二级标题 | 时间戳 | 文件 |
|---|---|---|---|---|
| **A 自建** | 10119 | **41** | **84** | `Python Object Oriented Programming in 10 minutes 🐍.md` |
| **B tube2note** | 9170 | 3 | 15 | `tube2note.md` |
| **C video-to-notes** | 10268 | 2 | 9 | `2020-12-28 - youtube - q2SGW2VgwAM - Python …` |

---

## A. 自建流水线

**头部**

````markdown
# Python Object Oriented Programming in 10 minutes 🐍

- **来源**：https://www.youtube.com/watch?v=q2SGW2VgwAM
- **时长**：10:04
- **语言**：en（置信度 1.00）
- **转写**：faster-whisper（base，VAD 开，beam=1）
- **生成时间**：2026-10-21 19:35:00
- **字幕条数**：147，合并为 41 段

---
````

**同一段内容 —— 每段独立成二级标题，带起止时间戳**

````markdown
iables. For the time being, I'm just going to use none as a placeholder. cars can have a model, a year, and a color.

## 00:02:36 ~ 00:02:48

Now objects can also have methods. What kinds of methods could cars perform? Perhaps a drive method and a stop method. So let's define those DEF, let's say drive and we'll have one argument.

## 00:02:48 ~ 00:03:08

Self refers to the object that is using this method. Now what do we want to do when we call this method? Let's say this car is driving. Let's create a stop method as well. DEF, stop and we will print.

## 00:03:08 ~ 00:03:19

This car is stopped. We now have all of the different attributes and methods. We would like our car objects to have, but there is one more thing that we need. It is a special method called the init method

## 00:03:19 ~ 00:03:31

that will construct objects for us. In other programming languages, this is known as the constructor. So we need a special method that will create objects for us. So we need to define this method, DEF.

## 00:03:31 ~ 00:03:44

And the syntax on this is somewhat strange. It is two underscores, I-N-I-T. It's short for initialize, two underscores again, and then we need at least self as an argument. And then we can actually assign our car objects,

## 00:03:44 ~ 00:03:58

unique variables. So take all of these attributes that we have, and we're going to place them within this init method. Now, we can receive arguments when we create car objects, but we need to pass them in as arguments to our init method.

## 00:03:58 ~ 00:04:12

So we need to set up some parameters. Let's say in order to create a car object, we need a make, a model, a year, and a color. Then when we receive these arguments, we can actually assign them to each car's

## 00:04:12 ~ 00:04:25

specific attributes, but we need to precede each of these with self. Self is referring to the object that we're currently working on or creating. So self.make, self.model, self.ir, and self.color.
````

## B. tube2note

**头部**

````markdown
# YouTube Research Notes

- Date: 2026-10-21
- Videos: target 1
- Languages: en

Feed this file to NotebookLM as a source.

---

## 1. Python Object Oriented Programming in 10 minutes 🐍

- Source: https://www.youtube.com/watch?v=q2SGW2VgwAM
- Video ID: q2SGW2VgwAM
- Subtitle lang: en (auto)
````

**同一段内容 —— 连续文字，40 秒才一个 [MM:SS]，无标点全小写**

````markdown
t have they might have a make so let's create a few variables for the time being i'm just going to use none as a placeholder cars can have a model a year and a color

[02:35] now objects can also have methods what kinds of methods could cars perform perhaps a drive method and a stop method so let's define those def let's say drive and we'll have one argument self refers to the object that is using this method now what do we want to do when we call this method let's say this car is driving let's create a stop method as well tef stop and we will print this car is stopped we now have all of the different attributes and methods we would like our car objects to have but there is one more thing that we need it is a special method called the init

[03:18] method that will construct objects for us in other programming languages this is known as the constructor so we need a special method that will create objects for us so we need to define this method def and the syntax on this is somewhat strange it is two underscores i n i t it's short for initialize 200 scores again and then we need at least self as an argument and then we can actually assign our car objects unique variables so take all of these attributes that we have and we're going to place them within this init method now we can receive arguments when we create car

[03:53] objects but we need to pass them in as arguments to our init method so we need to set up some parameters let's say in order to create a car object we need a make a model a year and a color then when we receive these arguments we can actually assign them to each car's specific attributes but we need to precede each of these with self is referring to the object that we're currently working on or creating so self.make self.model self.ear and self.color and then when we assign these we're going to say self.make equals whatever make that we receive when it's passed in as an argument self.model equals model and continue
````

## C. video-to-notes

**头部（YAML frontmatter —— 这个最丰富）**

````markdown
---
video_to_notes_id: "q2SGW2VgwAM"
title: "Python Object Oriented Programming in 10 minutes 🐍"
platform: "youtube"
source_url: "https://www.youtube.com/watch?v=q2SGW2VgwAM"
channel: "Bro Code"
published: "2020-12-28T21:27:28+00:00"
processed: "2026-10-21T12:48:41.912508+00:00"
duration_seconds: 604
transcript_source: "local_whisper"
local_report: "output_v2n\\processed\\2020-12-28_youtube_q2SGW2VgwAM_python-object-oriented-programming-in-10-minutes\\report.html"
local_folder: "output_v2n\\processed\\2020-12-28_youtube_q2SGW2VgwAM_python-object-oriented-programming-in-10-minutes"
workspace: "output_v2n"
---
# Python Object Oriented Programming in 10 minutes 🐍

## Video Details
- Source: [https://www.youtube.com/watch?v=q2SGW2VgwAM](https://www.youtube.com/watch?v=q2SGW2VgwAM)
- Platform: youtube
- Channel: Bro Code
- Published: 2020-12-28T21:27:28+00:00
- Duration: 10:04
- Transcript source: local_whisper
- Processed: 2026-10-21T12:48:41.912508+00:00
- Local report: output_v2n\processed\2020-12-28_youtube_q2SGW2VgwAM_python-object-oriented-programming-in-10-minutes\report.html
- Workspace: output_v2n

Summary pending.
````

**同一段内容 —— 整篇逐字稿塞在一个 text 代码块里，内部零时间戳**

````markdown
might have they might have a make so let's create a few variables for the time being I'm just going to use none as a placeholder cars can have a model a year and a color now objects can also have methods what kinds of methods could cars perform perhaps a drive method and a stop method so let's define those DEF let's say drive and we'll have one argument self self refers to the object that is using this method now what do we want to do when we call this method let's say this car is driving let's create a stop method as well DEF stop and we will print this car is stopped we now have all of the different attributes and methods we would like our car objects to have but there is one more thing that we need it is a special method called the init method that will construct objects for us another programming languages this is known as the constructor so we need a special method that will create objects for us so we need to define this map that DEF and the syntax on this is somewhat strange it is two underscores I and I T it's short for initialize two underscores again and then we need at least self as an argument and then we can actually assign our car objects unique variables so take all of these attributes that we have and we're going to place them within this init method now we can receive arguments when we create car objects but we need to pass them in as arguments to our init method so we need to set up some parameters let's say in order to create a car object we need a make a model a year and color then when we receive these arguments we can actually assign them to each car's specific attributes but we need to precede each of these with self self is referring to the object that we're currently working on or creating so self dot make self dot model self dot year and self dot color and then when we assign these we're going to say self dot make equals whatever make that we receive when it's passed in as an argument self dot model equals model and continue that pattern for a year and color all right that is all we need for our class car
````

---

## 对照结论

| 维度 | A 自建 | B tube2note | C video-to-notes |
|---|---|---|---|
| 每段时间戳 | ✅ 起止都标 | ⚠️ 40 秒一个 | ❌ 无 |
| 段落切分 | ✅ 按停顿切 41 段 | ❌ 一大块 | ❌ 整篇一块 |
| 标点与大小写 | ✅ 有 | ❌ 无 | ✅ 有 |
| 可直接当目录 | ✅ | ❌ | ❌ |
| 元信息 | 简单列表 | 简短 | ✅ YAML frontmatter |
| 本地直接出 Markdown | ✅ | ✅ | ❌ 需 publish-obsidian |
