---
name: bilibili-content
description: "Use when the user shares a Bilibili link, wants a video summarized, or needs transcripts/study notes. Extract subtitles (AI + uploader + ASR), danmaku, and comments. 当用户分享B站链接、需要视频总结或学习笔记时使用。提取字幕（AI生成+投稿者上传+ASR兜底）、弹幕分析和热评。"
version: 1.0.0
author: Gridel (gridel-li)
license: MIT
metadata:
  hermes:
    tags: [bilibili, 哔哩哔哩, video, subtitle, transcript, danmaku, chinese, asr, whisper]
    related_skills: [youtube-content]
---

# Bilibili Content Tool · B站内容提取工具

提取 B站视频字幕、弹幕、评论，输出为摘要、学习笔记、博客等格式。对标 `youtube-content`，专为中文视频优化。

Extract Bilibili video subtitles, danmaku analysis, and top comments. Output as study notes, summaries, threads, or blog posts. The B站 equivalent of `youtube-content`.

## 功能亮点 / Features

- **三策略字幕获取** — AI 自动生成 (`ai-zh`, 覆盖率 ~90%) → 投稿者上传 (`zh`) → 本地 Whisper ASR 兜底
- **弹幕分析** — 高频词统计 + 弹幕密度热点时间段
- **热评提取** — 按赞数排序的优质评论
- **多 P 支持** — 自动识别 URL 中的 `p=` 参数
- **多格式输出** — 教程笔记、章节摘要、博客、时间线

## 适用场景 / When to Use

用户发 B站链接、要总结视频、做学习笔记、提取字幕全文时使用。支持 `bilibili.com/video/`、`b23.tv` 短链、BV 号、AV 号。

Use when the user shares a Bilibili URL, asks for a video summary, study notes, or full transcripts.

## 安装 / Setup

```bash
# 核心依赖
pip install bilibili-api-python httpx

# 可选：本地 ASR 兜底（字幕全无时使用）
pip install faster-whisper
# macOS: brew install ffmpeg
```

### B站 AI 字幕（需 Cookie）

B站 AI 自动生成字幕覆盖率约 90%，但需要登录态：

```bash
# 1. 浏览器登录 B站
# 2. F12 → Application → Cookies → bilibili.com → 复制 SESSDATA 的值
# 3. 设置环境变量
export BILIBILI_SESSDATA="你的SESSDATA值"
```

不设 Cookie 时只能抓投稿者字幕（极少视频有）。

### 本地 ASR 兜底

当 AI 字幕和投稿者字幕都不可用时：

```bash
python3 SKILL_DIR/scripts/fetch_content.py "URL" --asr-fallback
python3 SKILL_DIR/scripts/fetch_content.py "URL" --asr-fallback --asr-model small  # 更准
```

模型大小：`tiny`（最快）< `base`（默认）< `small` < `medium` < `large`（最准）。

## 使用方法 / Usage

`SKILL_DIR` 为本 SKILL.md 所在目录。

```bash
# 完整提取（字幕 + 弹幕 + 评论，JSON 格式）
python3 SKILL_DIR/scripts/fetch_content.py "https://www.bilibili.com/video/BV1xx411c7m2"

# 只取字幕纯文本
python3 SKILL_DIR/scripts/fetch_content.py "BV1xx411c7m2" --subtitle-only

# 带时间戳字幕
python3 SKILL_DIR/scripts/fetch_content.py "URL" --subtitle-only --timestamps

# 跳过弹幕和评论（最快）
python3 SKILL_DIR/scripts/fetch_content.py "URL" --no-danmaku --no-comments

# 指定分 P
python3 SKILL_DIR/scripts/fetch_content.py "URL" --page 17  # 0-based, 即 P18

# 语言偏好
python3 SKILL_DIR/scripts/fetch_content.py "URL" --language zh-CN
```

URL 中的 `?p=18` 会自动识别，无需 `--page`。

## 输出格式 / Output Formats

根据视频类型和用户需求选择：

- **学习笔记**（教程类）— 分层知识点、代码块、⚠️ 注意事项、💡 小技巧
- **章节摘要** — 按内容变化分章，每章配说明
- **全文摘要** — 5-10 句概括
- **博客文章** — 标题、分段、核心要点
- **时间线** — 推文风格，每条 ≤280 字
- **弹幕报告** — 高频词、热点段、观众情绪
- **评论精华** — 高赞补充观点

教程/课程类视频默认输出 **学习笔记**，其他类型默认 **全文摘要**。

## 字幕获取策略（优先级）/ Subtitle Strategy

```
1. get_player_info(cid) → AI 自动生成 (ai-zh)   覆盖率: ~90%  需要: Cookie
2. get_subtitle(cid)    → 投稿者上传 (zh)       覆盖率: <5%   需要: 无
3. fetch_subtitle_asr() → 本地 Whisper 转写     覆盖率: 100%  需要: ffmpeg + faster-whisper
```

策略 3 仅在指定 `--asr-fallback` 时触发。

## JSON 输出结构

```json
{
  "video_id": "BV1xx411c7m2",
  "title": "视频标题",
  "description": "视频简介",
  "duration": "12:34",
  "pages": 1,
  "current_page": 1,
  "subtitle": {
    "source": "ai",          // ai | uploader | whisper
    "language": "ai-zh",
    "segment_count": 519,
    "full_text": "完整字幕...",
    "timestamped_text": "0:00 文本\n0:05 ...",
    "segments": [{"text": "...", "start": 0.0, "duration": 2.5}]
  },
  "danmaku": {
    "count": 380,
    "top_keywords": [{"text": "太细了", "count": 6}],
    "hot_segments": [{"time_range": "5:30-6:00", "count": 22, "samples": ["..."]}]
  },
  "comments": {
    "total_ac": 1234,
    "top_comments": [{"user": "用户名", "content": "...", "likes": 233}]
  }
}
```

## 工作流 / Workflow

1. **抓取** — 运行 `fetch_content.py` 获取 JSON
2. **验证** — 确认非空。字幕为空时注明，但弹幕评论仍可用
3. **分块**（如需要）— 字幕超 ~50K 字时分块处理，重叠 2K 字
4. **转换** — 按视频类型格式化。教程 → 学习笔记，纪录片 → 博客，以此类推
5. **复查** — 检查时间戳、连贯性、完整性

## 错误处理 / Error Handling

| 情况 | 处理 |
|------|------|
| 无字幕 | 告知用户，弹幕和评论仍可用 |
| 无 Cookie / AI 字幕不可用 | 建议设置 `BILIBILI_SESSDATA` 或加 `--asr-fallback` |
| 私有/已删除视频 | 转达 API 错误信息 |
| 依赖缺失 | `pip install bilibili-api-python httpx` |
| 弹幕量巨大 | 脚本采样前 20 段（120 分钟），足够分析 |

## 文件结构 / Files

```
bilibili-content/
├── SKILL.md                        # 本文件
├── scripts/
│   └── fetch_content.py            # 核心抓取脚本
└── references/
    └── output-formats.md           # 输出格式详细示例
```
