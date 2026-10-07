import os
import json
import math
import time
import re
import random
import shutil
import requests
from datetime import datetime
from google import genai

from modules.composer import ShortsComposer
from modules.youtube_uploader import (
    upload_video,
    set_thumbnail,
    add_to_playlist,
)
from modules.tiktok_uploader import upload_to_tiktok
from modules.asset_manager import fetch_scene_video
from modules.audio import generate_voiceover, load_word_timings, get_duration
from modules.brain import generate_script, record_history

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")

YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN")
YOUTUBE_PLAYLIST_ID = os.getenv("YOUTUBE_PLAYLIST_ID", "")

TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
TIKTOK_REFRESH_TOKEN = os.getenv("TIKTOK_REFRESH_TOKEN")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

USED_TOPICS_FILE = "used_topics.json"

ENABLE_CAPTIONS = os.getenv("ENABLE_CAPTIONS", "0") == "1"
WORD_CAPTIONS = os.getenv("WORD_CAPTIONS", "1") == "1"
HOOK_CAPTION = os.getenv("HOOK_CAPTION", "1") == "1"
# last scene re-uses the hook footage -> visual loop (replay feels continuous)
LOOP_VISUAL = os.getenv("LOOP_VISUAL", "1") == "1"

client = genai.Client(api_key=GEMINI_API_KEY)

ASSETS_DIR = "assets"
TEMP_VIDEO_DIR = os.path.join(ASSETS_DIR, "video_clips")
TEMP_AUDIO_DIR = os.path.join(ASSETS_DIR, "audio_clips")
SCENE_CLIP_DIR = os.path.join(ASSETS_DIR, "scene_clips")
OUTPUT_DIR = os.path.join(ASSETS_DIR, "final")

for directory in [TEMP_VIDEO_DIR, TEMP_AUDIO_DIR, SCENE_CLIP_DIR, OUTPUT_DIR]:
    os.makedirs(directory, exist_ok=True)

TITLE_POOL = [
    "How Is This Even Possible? \U0001F631",
    "The Biggest Secret On Earth!",
    "Even Scientists Are Confused! \U0001F92F",
    "Is This True Or A Lie?",
    "You Won't Believe This!",
    "This Fact Is Actually Real!",
    "Never Do This Again!",
]

BASE_TAGS = [
    "shorts", "youtubeshorts", "facts", "amazing facts", "mysteries", "viral shorts",
    "science facts", "crazy facts", "mind blowing", "unbelievable", "dangerous facts",
    "what if", "did you know", "fun facts", "interesting facts",
]


def get_youtube_channel():
    return {
        "name": "YouTube",
        "client_id": YOUTUBE_CLIENT_ID,
        "client_secret": YOUTUBE_CLIENT_SECRET,
        "refresh_token": YOUTUBE_REFRESH_TOKEN,
        "playlist_id": YOUTUBE_PLAYLIST_ID,
    }


def notify_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message[:4000]},
            timeout=15,
        )
    except Exception as e:
        print(f"Telegram notify failed: {e}")


def build_scene_voiceovers(scenes, audio_dir):
    """
    1.15x speed (+15%) for every scene, hook a touch faster (+18%) so the first
    second has energy. No SSML breaks -> no dead air at the start; silence is
    trimmed and real word timings are saved next to each mp3 for caption sync.
    """
    paths = []
    for index, scene in enumerate(scenes, start=1):
        path = os.path.join(audio_dir, f"scene_{index:02d}.mp3")
        if os.path.exists(path):
            os.remove(path)

        if index == 1:
            rate, pitch = "+18%", "+2Hz"      # HOOK - energy burst
        else:
            rate, pitch = "+15%", "+0Hz"      # 1.15x narrator speed

        for attempt in range(1, 4):
            try:
                generate_voiceover(scene["narration"], path, rate=rate, pitch=pitch)
                if os.path.exists(path) and os.path.getsize(path) > 1000:
                    break
            except Exception as e:
                print(f"[Voice scene {index}] attempt {attempt} failed: {e}")
                time.sleep(2)
        else:
            raise RuntimeError(f"Scene {index} ki voiceover generate nahi ho saki.")
        paths.append(path)
    return paths


def build_scene_clips(script, clip_dir, voice_paths):
    """
    One clip per scene, ALWAYS tied to the script's subject:
    scene keyword -> fallback keyword -> subject -> visual_domain -> exact-subject AI image.
    """
    scenes = script["scenes"]
    subject = script.get("subject", "")
    domain = script.get("visual_domain", [])

    shutil.rmtree(clip_dir, ignore_errors=True)
    os.makedirs(clip_dir, exist_ok=True)
    paths = []
    last = len(scenes)
    for index, scene in enumerate(scenes, start=1):
        target = os.path.join(clip_dir, f"scene_{index:02d}.mp4")
        keyword = scene.get("search_keyword") or subject
        print(f"Scene {index}: '{keyword}' (subject: '{subject}')")

        # visual loop: closing scene shows the hook subject again
        if LOOP_VISUAL and index == last and last >= 3 and paths:
            shutil.copy(paths[0], target)
            print("Scene %d: re-using hook footage for a visual loop" % index)
            paths.append(target)
            continue

        dur = get_duration(voice_paths[index - 1]) if index - 1 < len(voice_paths) else 3.0
        min_dur = max(3, int(math.ceil(dur)))
        try:
            fetch_scene_video(
                keyword,
                target,
                min_duration=min_dur,
                narration=scene.get("narration", ""),
                subject=subject,
                domain=domain,
                fallback_keyword=scene.get("fallback_keyword", ""),
                hook=(index == 1),
            )
            paths.append(target)
        except Exception as e:
            print(f"Scene {index} ka clip nahi mila: {e}")
            if not paths:
                raise
            paths.append(paths[0])      # hook footage = on-subject by definition
    return paths


def build_metadata(script, full_narration):
    title_core = re.sub(r"#\S+", "", script.get("title", "")).strip()
    if not title_core:
        title_core = random.choice(TITLE_POOL)
    title = f"{title_core[:75].strip()} #Shorts"[:95]
    tags, seen, total_chars = [], set(), 0
    for tag in script.get("tags", []) + BASE_TAGS:
        tag = re.sub(r"[#,<>]", "", tag).strip().lower()
        if not tag or tag in seen:
            continue
        if total_chars + len(tag) + 1 > 450:
            break
        seen.add(tag)
        tags.append(tag)
        total_chars += len(tag) + 1
    hashtags, seen_h = [], set()
    candidates = ["#Shorts", "#Facts", "#AmazingFacts", "#FunFacts",
                  "#CrazyFacts", "#DidYouKnow", "#WhatIf", "#MindBlown"]
    candidates += ["#" + re.sub(r"[^0-9a-zA-Z]", "", t) for t in script.get("tags", [])]
    for candidate in candidates:
        key = candidate.lower()
        if len(candidate) < 3 or key in seen_h:
            continue
        seen_h.add(key)
        hashtags.append(candidate)
        if len(hashtags) >= 10:
            break
    body = script.get("description") or full_narration
    description = f"{body}\n\n{' '.join(hashtags)}"[:4900]
    return title, description, tags


def generate_thumbnail(video_path: str, output_path: str, title_text: str):
    import subprocess
    if not os.path.exists(video_path):
        return None
    frame_path = output_path + ".frame.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-ss", "1", "-i", video_path,
         "-frames:v", "1", "-q:v", "2", frame_path],
        capture_output=True,
    )
    if not os.path.exists(frame_path):
        return None
    safe_title = re.sub(r"[^\x20-\x7E]", "", title_text)
    safe_title = re.sub(r'[":\'\\\n\r%]', "", safe_title)[:40].strip()
    if not safe_title:
        safe_title = "Amazing Fact"
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    font_file = next((f for f in font_candidates if os.path.exists(f)), None)
    vf_parts = [
        "scale=1080:1920:force_original_aspect_ratio=increase",
        "crop=1080:1920",
    ]
    if font_file:
        vf_parts.append(
            f"drawtext=text='{safe_title}':"
            f"fontcolor=white:fontsize=72:"
            f"box=1:boxcolor=black@0.7:boxborderw=20:"
            f"x=(w-text_w)/2:y=h*0.75:"
            f"fontfile={font_file}"
        )
    cmd = [
        "ffmpeg", "-y", "-i", frame_path,
        "-vf", ",".join(vf_parts),
        "-frames:v", "1", "-q:v", "2", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if os.path.exists(frame_path):
        os.remove(frame_path)
    if result.returncode == 0 and os.path.exists(output_path):
        return output_path
    return None


def run_pipeline(channel: dict) -> bool:
    name = channel["name"]
    print(f"\n{'='*60}")
    print(f"  Starting pipeline for {name}")
    print(f"{'='*60}\n")

    ch_audio_dir = TEMP_AUDIO_DIR
    ch_clip_dir = SCENE_CLIP_DIR
    ch_output_dir = OUTPUT_DIR
    for d in (ch_audio_dir, ch_clip_dir, ch_output_dir):
        os.makedirs(d, exist_ok=True)

    start_time = time.time()

    print(f"\n[{name}] Generating fresh script...")
    script = generate_script(client, USED_TOPICS_FILE)
    if not script:
        msg = f"[{name}] Script generation failed."
        print(msg)
        notify_telegram(msg)
        return False

    record_history(USED_TOPICS_FILE, script)
    scenes = script["scenes"]
    full_narration = " ".join(s["narration"] for s in scenes)
    print(f"[{name}] {len(scenes)} scenes | subject: {script.get('subject')} | "
          f"Hook: {scenes[0]['narration']} | Title card: {script.get('hook_title')}")

    print(f"\n[{name}] Generating voiceovers...")
    try:
        voice_paths = build_scene_voiceovers(scenes, ch_audio_dir)
    except Exception as e:
        msg = f"[{name}] Voiceover failed: {e}"
        print(msg)
        notify_telegram(msg)
        return False

    print(f"\n[{name}] Downloading stock clips...")
    try:
        clip_paths = build_scene_clips(script, ch_clip_dir, voice_paths)
    except Exception as e:
        msg = f"[{name}] Video download failed: {e}"
        print(msg)
        notify_telegram(msg)
        return False

    print(f"\n[{name}] Composing final video...")
    composer = ShortsComposer(output_dir=ch_output_dir)

    bg_music_path = None
    for candidate in [
        os.path.join("assets", "bgm"),
        os.path.join("modules", "bg_music.mp3"),
    ]:
        if os.path.isdir(candidate):
            files = [f for f in os.listdir(candidate) if f.lower().endswith(".mp3")]
            if files:
                bg_music_path = os.path.join(candidate, random.choice(files))
                break
        elif os.path.isfile(candidate):
            bg_music_path = candidate
            break

    if WORD_CAPTIONS:
        captions = None
    elif ENABLE_CAPTIONS:
        captions = [s.get("caption", "") for s in scenes]
    elif HOOK_CAPTION:
        captions = [""] * len(scenes)
        captions[0] = scenes[0].get("caption", "")
        if len(scenes) > 4:
            captions[-2] = scenes[-2].get("caption", "")
    else:
        captions = None

    output_filename = "final_short.mp4"
    try:
        final_video_path = composer.create_multi_scene_short(
            clip_paths=clip_paths,
            voiceover_paths=voice_paths,
            output_filename=output_filename,
            bg_music_path=bg_music_path,
            add_cta=False,
            scene_narrations=captions,
            word_scenes=scenes if WORD_CAPTIONS else None,
            hook_text=script.get("hook_title"),
            word_timings=[load_word_timings(p) for p in voice_paths],
        )
    except Exception as e:
        msg = f"[{name}] Composition failed: {e}"
        print(msg)
        notify_telegram(msg)
        return False

    if not os.path.exists(final_video_path):
        msg = f"[{name}] Final video file not created."
        print(msg)
        notify_telegram(msg)
        return False

    print(f"\n[{name}] Generating thumbnail...")
    thumb_path = os.path.join(ch_output_dir, "thumbnail.jpg")
    generate_thumbnail(final_video_path, thumb_path, script.get("title", "Amazing Fact"))

    print(f"\n[{name}] Uploading to YouTube...")
    title, description, tags = build_metadata(script, full_narration)
    print(f"[{name}] Title: {title}")

    try:
        video_id = upload_video(
            video_path=final_video_path,
            title=title,
            description=description,
            tags=tags,
            privacy_status="public",
            client_id=channel["client_id"],
            client_secret=channel["client_secret"],
            refresh_token=channel["refresh_token"],
        )
        print(f"[{name}] Video uploaded! ID: {video_id}")
        print(f"https://youtube.com/shorts/{video_id}")

        if os.path.exists(thumb_path):
            set_thumbnail(
                video_id, thumb_path,
                channel["client_id"], channel["client_secret"], channel["refresh_token"],
            )

        if channel["playlist_id"]:
            add_to_playlist(
                video_id, channel["playlist_id"],
                channel["client_id"], channel["client_secret"], channel["refresh_token"],
            )

        elapsed = time.time() - start_time
        notify_telegram(
            f"[{name}] Video uploaded!\n{title}\n"
            f"https://youtube.com/shorts/{video_id}\n{elapsed:.0f}s"
        )
    except Exception as e:
        msg = f"[{name}] YouTube Upload Failed: {e}"
        print(msg)
        notify_telegram(msg)
        return False

    if TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET and TIKTOK_REFRESH_TOKEN:
        try:
            tiktok_publish_id = upload_to_tiktok(
                video_path=final_video_path,
                title=title,
                client_key=TIKTOK_CLIENT_KEY,
                client_secret=TIKTOK_CLIENT_SECRET,
                refresh_token=TIKTOK_REFRESH_TOKEN,
            )
            notify_telegram(
                f"[{name}] TikTok draft uploaded!\n{title}\n"
                f"Publish ID: {tiktok_publish_id}"
            )
        except Exception as e:
            print(f"[{name}] TikTok Upload Failed: {e}")
    else:
        print(f"\n[{name}] TikTok credentials missing - skipping.")

    elapsed = time.time() - start_time
    print(f"\n[{name}] Pipeline complete in {elapsed:.0f}s")
    return True


def main():
    print("Starting Automated Short Pipeline (single channel, English)...")
    channel = get_youtube_channel()
    if not (channel["client_id"] and channel["client_secret"] and channel["refresh_token"]):
        msg = "YouTube credentials missing."
        print(msg)
        notify_telegram(msg)
        raise SystemExit(1)
    try:
        ok = run_pipeline(channel)
    except Exception as e:
        print(f"Pipeline crashed: {e}")
        notify_telegram(f"Pipeline crashed: {e}")
        ok = False
    print("\n" + "=" * 60)
    print("  " + ("SUCCESS" if ok else "FAILED"))
    print("=" * 60)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
