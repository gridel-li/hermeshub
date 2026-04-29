#!/usr/bin/env python3
"""
Fetch Bilibili video subtitles, danmaku, and top comments as structured JSON.

对标 youtube-content 的 fetch_transcript.py，增加了弹幕和高质量评论。

Usage:
    python fetch_content.py <url_or_bvid> [--language zh-CN] [--no-danmaku] [--no-comments]

Output (JSON):
    {
        "video_id": "BV1xx411c7m2",
        "title": "...",
        "description": "...",
        "duration": "12:34",
        "subtitle": {
            "language": "zh-CN",
            "segments": [...],
            "full_text": "...",
            "timestamped_text": "00:00 ..."
        },
        "danmaku": {
            "count": 1234,
            "top_keywords": [...],
            "hot_segments": [...]
        },
        "comments": {
            "top_comments": [...]
        }
    }

Install dependency: pip install bilibili-api-python httpx
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from typing import Optional


def extract_bvid(url_or_id: str) -> str:
    """从 B站各种 URL 格式中提取 BV 号。"""
    url_or_id = url_or_id.strip()
    # 标准 BV 号: 以 BV 开头，10 位字母数字
    if re.match(r'^BV[a-zA-Z0-9]{10}$', url_or_id):
        return url_or_id
    # 从 URL 中提取
    match = re.search(r'(BV[a-zA-Z0-9]{10})', url_or_id)
    if match:
        return match.group(1)
    # AV 号也支持
    match = re.search(r'av(\d+)', url_or_id, re.IGNORECASE)
    if match:
        return match.group(0)
    return url_or_id


def format_timestamp(seconds: float) -> str:
    """秒数 → HH:MM:SS 或 MM:SS"""
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


async def fetch_subtitle(v, cid: int, preferred_lang: Optional[str] = None) -> Optional[dict]:
    """获取视频字幕（AI自动生成 + 投稿者上传），返回标准化的 segments + full_text + timestamped_text。

    B站字幕分两层：
    1. AI自动生成 (lan=ai-zh) — 覆盖率最高，通过 player/wbi/v2 获取，需 Cookie
    2. 投稿者上传 (lan=zh) — 通过 get_subtitle() 获取，无需 Cookie
    """
    subtitle_url = None
    language = "unknown"
    language_display = ""

    # 策略 1: player/wbi/v2（含 AI 字幕，优先）
    try:
        player_info = await v.get_player_info(cid=cid)
        sub_data = player_info.get("subtitle", {}) or {}
        subtitles = sub_data.get("subtitles", [])
        if subtitles:
            # 优先 AI 中文，其次普通中文，再次指定语言
            tiers = []
            if preferred_lang:
                tiers.append(preferred_lang)
            tiers.extend(["ai-zh", "zh", "ai-en", "en"])
            chosen = None
            for tier in tiers:
                for sub in subtitles:
                    lan = sub.get("lan", "")
                    if lan == tier or lan.startswith(tier):
                        chosen = sub
                        break
                if chosen:
                    break
            if not chosen:
                chosen = subtitles[0]
            subtitle_url = chosen.get("subtitle_url", "")
            language = chosen.get("lan", "unknown")
            language_display = chosen.get("lan_doc", "")
    except Exception:
        pass

    # 策略 2: get_subtitle()（投稿者字幕，无 Cookie 也能用，作为 fallback）
    if not subtitle_url:
        try:
            subtitle_info = await v.get_subtitle(cid)
            subtitles = subtitle_info.get("subtitles", [])
            if subtitles:
                chosen = subtitles[0]
                if preferred_lang:
                    for sub in subtitles:
                        if sub.get("lan", "").startswith(preferred_lang):
                            chosen = sub
                            break
                subtitle_url = chosen.get("subtitle_url", "")
                language = chosen.get("lan", "unknown")
                language_display = chosen.get("lan_doc", "")
        except Exception:
            pass

    if not subtitle_url:
        return None

    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url

    # 下载字幕 JSON
    raw = None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(subtitle_url)
            resp.raise_for_status()
            raw = resp.json()
    except ImportError:
        import urllib.request
        import ssl
        ctx = ssl.create_default_context()
        req = urllib.request.Request(subtitle_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    if not raw:
        return None

    body = raw.get("body", [])
    if not body:
        return None

    segments = []
    for item in body:
        segments.append({
            "text": item.get("content", ""),
            "start": float(item.get("from", 0)),
            "duration": float(item.get("to", 0)) - float(item.get("from", 0)),
        })

    full_text = " ".join(seg["text"] for seg in segments)
    timestamped = "\n".join(
        f"{format_timestamp(seg['start'])} {seg['text']}" for seg in segments
    )

    return {
        "language": language,
        "language_display": language_display,
        "source": "ai" if language.startswith("ai-") else "uploader",
        "segment_count": len(segments),
        "segments": segments,
        "full_text": full_text,
        "timestamped_text": timestamped,
    }


async def fetch_danmaku(v, cid: int, page_index: int = 0) -> Optional[dict]:
    """获取弹幕，统计高频词和热点时间段。"""
    try:
        # 先获取分段信息
        view = await v.get_danmaku_view(page_index=page_index, cid=cid)
    except Exception:
        return None

    seg_count = 1
    dm_segs = view.get("dm_segs", [])
    if dm_segs:
        seg_count = len(dm_segs)

    all_danmaku = []
    # 按段获取弹幕（每段 6 分钟），取前 20 段（120分钟），避免超时
    max_segs = min(seg_count, 20)
    for i in range(max_segs):
        try:
            dms = await v.get_danmakus(
                page_index=page_index,
                cid=cid,
                from_seg=i,
                to_seg=i + 1,
            )
            all_danmaku.extend(dms)
        except Exception:
            continue

    if not all_danmaku:
        return {"count": 0, "top_keywords": [], "hot_segments": []}

    texts = [dm.text for dm in all_danmaku if len(dm.text.strip()) >= 2]
    times = [dm.dm_time for dm in all_danmaku]

    # 高频词统计（去重，过滤纯数字/符号）
    word_counter = Counter()
    for text in texts:
        # 过滤低质量弹幕
        cleaned = text.strip()
        if re.match(r'^[\d\W_]+$', cleaned):  # 纯数字/符号跳过
            continue
        if cleaned in ("打卡", "来了", "第一", "第二", "前排", "？？？", "哈哈哈", "hhh"):
            continue
        word_counter[cleaned] += 1

    top_keywords = [{"text": w, "count": c} for w, c in word_counter.most_common(30)]

    # 热点时间段：按 30 秒窗口统计弹幕密度
    if times:
        max_time = max(times)
        window = 30  # 30秒窗口
        buckets = Counter()
        for t in times:
            bucket = int(t // window) * window
            buckets[bucket] += 1

        hot_segments = []
        for start, count in buckets.most_common(10):
            samples = [dm.text for dm in all_danmaku
                       if start <= dm.dm_time < start + window][:8]
            hot_segments.append({
                "time_range": f"{format_timestamp(start)}-{format_timestamp(start + window)}",
                "start": start,
                "count": count,
                "samples": samples,
            })
        hot_segments.sort(key=lambda x: x["start"])
    else:
        hot_segments = []

    return {
        "count": len(all_danmaku),
        "top_keywords": top_keywords,
        "hot_segments": hot_segments,
    }


async def fetch_comments(oid: int) -> Optional[dict]:
    """获取按赞数排序的高质量评论。"""
    try:
        from bilibili_api.comment import get_comments_lazy, OrderType, CommentResourceType

        result = await get_comments_lazy(
            oid=oid,
            type_=CommentResourceType.VIDEO,
            order=OrderType.LIKE,
        )
    except Exception:
        return None

    replies = result.get("replies", [])
    top_comments = []
    for reply in replies[:20]:  # 最多 20 条
        top_comments.append({
            "user": reply.get("member", {}).get("uname", "未知"),
            "content": reply.get("content", {}).get("message", ""),
            "likes": reply.get("like", 0),
            "timestamp": reply.get("ctime", 0),
        })

    return {"top_comments": top_comments, "total_ac": result.get("page", {}).get("ac_count", 0)}


async def main_async(args):
    """异步主逻辑。"""
    from bilibili_api import video, Credential
    import os

    bvid = extract_bvid(args.url)

    # Cookie 支持：环境变量 BILIBILI_SESSDATA（AI 字幕需要）
    credential = None
    sessdata = os.environ.get("BILIBILI_SESSDATA", "")
    if sessdata:
        credential = Credential(sessdata=sessdata)

    v = video.Video(bvid=bvid, credential=credential)

    # 基础信息
    info = await v.get_info()
    pages = await v.get_pages()

    duration_sec = info.get("duration", 0)
    # 分 P 视频
    if pages:
        duration_sec = sum(p.get("duration", 0) for p in pages)

    result = {
        "video_id": bvid,
        "title": info.get("title", ""),
        "description": info.get("desc", ""),
        "duration": format_timestamp(duration_sec),
        "duration_seconds": duration_sec,
        "pages": len(pages) if pages else 1,
        "url": f"https://www.bilibili.com/video/{bvid}",
    }

    # 取指定 P 的 cid（字幕/弹幕都是按分 P 来的）
    page_index = args.page if hasattr(args, 'page') and args.page is not None else 0
    cid = await v.get_cid(page_index=page_index)
    result["current_page"] = page_index + 1

    # 字幕（策略 1+2）
    subtitle = await fetch_subtitle(v, cid, args.language)
    if subtitle:
        result["subtitle"] = subtitle
    elif getattr(args, 'asr_fallback', False):
        # 策略 3: ASR 兜底 — 下载音频 + whisper 转文字
        import sys as _sys
        model_name = getattr(args, 'asr_model', 'base')
        _sys.stderr.write(f"[ASR] No subtitle found, falling back to whisper ({model_name})...\n")
        _sys.stderr.flush()
        subtitle = await fetch_subtitle_asr(v, cid, page_index, credential, model_name, args.language)
        if subtitle:
            _sys.stderr.write(f"[ASR] Done — {subtitle['segment_count']} segments\n")
            _sys.stderr.flush()
            result["subtitle"] = subtitle
        else:
            result["subtitle"] = None
    else:
        result["subtitle"] = None

    # 弹幕
    if not args.no_danmaku:
        danmaku = await fetch_danmaku(v, cid, page_index=page_index)
        if danmaku:
            result["danmaku"] = danmaku
        else:
            result["danmaku"] = None

    # 评论（需要 AV 号作为 oid）
    if not args.no_comments:
        aid = info.get("aid", 0)
        comments = await fetch_comments(aid)
        if comments:
            result["comments"] = comments
        else:
            result["comments"] = None

    return result


async def fetch_subtitle_asr(v, cid: int, page_index: int, credential, model_name: str = "base", preferred_lang: Optional[str] = None) -> Optional[dict]:
    """策略 3: 下载音频流 → 本地 Whisper 转文字。
    
    当策略 1 (AI字幕) 和策略 2 (投稿者字幕) 都失败时使用。
    需要: ffmpeg + faster-whisper
    """
    import os
    import tempfile
    import subprocess
    import shutil

    # 1. 获取音频流 URL
    audio_url = None
    try:
        url_info = await v.get_download_url(page_index=page_index, cid=cid)
        dash = url_info.get("dash", {}) if isinstance(url_info, dict) else {}
        audios = dash.get("audio", [])
        if audios:
            # 取最低码率音频（64K 足够 ASR 用）
            audio_url = audios[0].get("base_url") or audios[0].get("baseUrl", "")
    except Exception:
        pass

    if not audio_url:
        return None

    # 2. 下载音频到临时文件
    tmpdir = tempfile.mkdtemp(prefix="bilibili_asr_")
    audio_path = os.path.join(tmpdir, "audio.m4a")
    wav_path = os.path.join(tmpdir, "audio.wav")

    try:
        import httpx
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com/",
        }
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            resp = await client.get(audio_url, headers=headers)
            resp.raise_for_status()
            with open(audio_path, "wb") as f:
                f.write(resp.content)
    except ImportError:
        import urllib.request
        req = urllib.request.Request(audio_url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            with open(audio_path, "wb") as f:
                f.write(resp.read())
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None

    # 3. ffmpeg 转 WAV (16kHz mono, whisper 最优格式)
    if shutil.which("ffmpeg"):
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
                capture_output=True, timeout=120
            )
            if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
                transcribe_path = wav_path
            else:
                transcribe_path = audio_path
        except Exception:
            transcribe_path = audio_path
    else:
        transcribe_path = audio_path

    # 4. Whisper 转文字
    try:
        from faster_whisper import WhisperModel

        device = "cpu"
        compute_type = "int8"
        # macOS Apple Silicon 优先用 CoreML
        if hasattr(subprocess, "check_output"):
            try:
                uname = subprocess.check_output(["uname", "-m"], text=True).strip()
                if uname == "arm64":
                    compute_type = "auto"
            except Exception:
                pass

        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        lang = preferred_lang if preferred_lang else None
        segments_raw, info = model.transcribe(transcribe_path, language=lang, beam_size=5)

        segments = []
        for seg in segments_raw:
            segments.append({
                "text": seg.text.strip(),
                "start": seg.start,
                "duration": seg.end - seg.start,
            })

        if not segments:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None

        full_text = " ".join(seg["text"] for seg in segments)
        timestamped = "\n".join(
            f"{format_timestamp(seg['start'])} {seg['text']}" for seg in segments
        )

        shutil.rmtree(tmpdir, ignore_errors=True)
        return {
            "language": info.language,
            "language_display": f"ASR ({model_name})",
            "source": "whisper",
            "model": model_name,
            "segment_count": len(segments),
            "segments": segments,
            "full_text": full_text,
            "timestamped_text": timestamped,
        }

    except ImportError:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Bilibili video subtitles, danmaku, and comments as JSON"
    )
    parser.add_argument("url", help="Bilibili URL or BV/AV ID")
    parser.add_argument("--language", "-l", default=None,
                        help="Preferred subtitle language (e.g., zh-CN, en). Default: first available")
    parser.add_argument("--no-danmaku", action="store_true",
                        help="Skip danmaku fetching")
    parser.add_argument("--no-comments", action="store_true",
                        help="Skip comment fetching")
    parser.add_argument("--subtitle-only", action="store_true",
                        help="Output subtitle plain text only (for piping)")
    parser.add_argument("--timestamps", "-t", action="store_true",
                        help="Include timestamps in --subtitle-only output")
    parser.add_argument("--page", "-p", type=int, default=None,
                        help="Page index for multi-P videos (0-based). Overrides p= in URL.")
    parser.add_argument("--asr-fallback", action="store_true",
                        help="Use local Whisper ASR when no subtitle available (needs faster-whisper + ffmpeg)")
    parser.add_argument("--asr-model", default="base",
                        help="Whisper model size: tiny, base, small, medium, large (default: base)")
    args = parser.parse_args()

    # 如果没显式指定 --page，从 URL 中提取 p= 参数
    if args.page is None:
        match = re.search(r'[?&]p=(\d+)', args.url)
        if match:
            args.page = int(match.group(1)) - 1  # URL 中 p= 是 1-based

    try:
        result = asyncio.run(main_async(args))
    except ImportError as e:
        print(json.dumps({
            "error": f"Missing dependency: {e}. Run: pip install bilibili-api-python httpx"
        }, ensure_ascii=False))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)

    if args.subtitle_only:
        sub = result.get("subtitle")
        if not sub:
            print("(no subtitle available)")
            sys.exit(1)
        if args.timestamps:
            print(sub.get("timestamped_text", ""))
        else:
            print(sub.get("full_text", ""))
        return

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
