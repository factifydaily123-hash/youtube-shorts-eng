"""
Stock-footage fetcher with STRICT keyword discipline + scene-aware fallback.
"""

import os
import random
import re
import subprocess
import shutil
import requests

_USED_IDS = set()

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")


KEYWORD_SYNONYMS = {
    "brain": ["human brain animation", "head closeup person", "person thinking"],
    "neuron": ["neurons firing", "human brain animation", "microscope cells"],
    "neurons": ["neurons firing", "human brain animation", "microscope cells"],
    "nerve": ["neurons firing", "human brain animation", "hand touching skin"],
    "nerves": ["neurons firing", "human brain animation", "hand touching skin"],
    "cerebellum": ["human brain animation", "head closeup person"],
    "memory": ["person thinking", "human brain animation", "old photo closeup"],
    "think": ["person thinking", "person looking window", "head closeup person"],
    "thinking": ["person thinking", "person looking window", "head closeup person"],
    "tickle": ["person laughing", "hand touching skin", "fingers moving closeup"],
    "ticklish": ["person laughing", "hand touching skin", "fingers moving closeup"],
    "laugh": ["person laughing closeup", "smiling face closeup", "friends laughing"],
    "laughing": ["person laughing closeup", "smiling face closeup"],
    "skin": ["skin closeup", "hand touching skin", "face closeup slow motion"],
    "touch": ["hand touching skin", "hands closeup", "fingers moving closeup"],
    "hand": ["hands closeup", "hand touching skin", "fingers moving closeup"],
    "hands": ["hands closeup", "hand touching skin"],
    "finger": ["fingers moving closeup", "hand touching skin"],
    "fingers": ["fingers moving closeup", "hand touching skin"],
    "eye": ["human eye closeup", "person blinking closeup"],
    "eyes": ["human eye closeup", "person blinking closeup"],
    "heart": ["human heart animation", "chest closeup person", "heartbeat monitor"],
    "sleep": ["person sleeping", "bed bedroom night", "person closing eyes"],
    "sleeping": ["person sleeping", "bed bedroom night"],
    "dream": ["dreamy clouds", "night sky stars", "person sleeping"],
    "dreams": ["dreamy clouds", "night sky stars", "person sleeping"],
    "reflex": ["human body animation", "doctor examining patient", "muscle closeup"],
    "space": ["space galaxy stars", "earth from space", "stars night sky"],
    "galaxy": ["space galaxy stars", "stars night sky timelapse"],
    "planet": ["planet space animation", "earth from space"],
    "star": ["stars night sky", "space galaxy stars"],
    "stars": ["stars night sky", "space galaxy stars"],
    "ocean": ["ocean waves underwater", "ocean waves aerial", "underwater blue"],
    "sea": ["ocean waves underwater", "underwater deep sea"],
    "shark": ["shark swimming underwater", "fish underwater"],
    "whale": ["whale ocean underwater", "ocean waves underwater"],
    "fish": ["fish swimming underwater", "aquarium fish closeup"],
    "cat": ["cat looking camera", "cat playing"],
    "dog": ["dog running grass", "dog looking camera"],
    "lion": ["lion running savanna", "lion closeup"],
    "fire": ["fire flames dark", "campfire night", "flame closeup"],
    "flame": ["fire flames dark", "flame closeup"],
    "ice": ["ice glacier arctic", "frozen ice closeup"],
    "snow": ["snow falling", "snow mountain"],
    "lightning": ["lightning storm sky", "storm clouds dramatic"],
    "storm": ["storm clouds dramatic", "rain window moody"],
    "rain": ["rain window moody", "rain falling street"],
    "volcano": ["volcano eruption lava", "lava flowing closeup"],
    "lava": ["volcano eruption lava", "lava flowing closeup"],
    "earthquake": ["earthquake cracked ground", "city building shaking"],
    "money": ["money cash dollars", "coins closeup"],
    "gold": ["gold coins treasure", "gold closeup shiny"],
    "coin": ["gold coins treasure", "coins closeup"],
    "coins": ["gold coins treasure", "coins closeup"],
    "clock": ["clock ticking closeup", "old clock wall"],
    "time": ["clock ticking closeup", "hourglass sand"],
    "computer": ["computer code screen", "laptop closeup hands"],
    "laptop": ["laptop closeup hands", "computer code screen"],
    "phone": ["smartphone closeup hands", "phone scrolling"],
    "smartphone": ["smartphone closeup hands", "phone scrolling"],
    "robot": ["robot machine closeup", "robot factory"],
    "internet": ["server data center", "network cables closeup"],
    "food": ["cooking food closeup", "chef cooking"],
    "water": ["water pouring glass", "water droplet closeup"],
    "pyramid": ["ancient pyramid egypt", "desert pyramid"],
    "temple": ["ancient temple ruins", "old stone temple"],
    "mummy": ["ancient mummy museum", "museum artifact closeup"],
    "tree": ["forest fog morning", "tree branches sky"],
    "trees": ["forest fog morning", "tree branches sky"],
    "forest": ["forest fog morning", "forest sunbeam trees"],
    "plant": ["plant leaves closeup", "green leaves sunlight"],
    "insect": ["insect macro closeup", "ant walking macro"],
    "ant": ["ant walking macro", "insect macro closeup"],
    "bee": ["bee flower closeup", "insect macro closeup"],
    "city": ["city traffic night", "city street aerial"],
    "car": ["car driving road", "car closeup wheel"],
    "rocket": ["rocket launching space", "rocket engine fire"],
    "mountain": ["mountains landscape aerial", "mountain peak clouds"],
    "desert": ["desert sand dunes", "desert sunset"],
    "museum": ["museum artifact closeup", "old manuscript closeup"],
    "history": ["museum artifact closeup", "old manuscript closeup"],
    "ancient": ["ancient temple ruins", "old stone wall"],
    "scientist": ["scientist laboratory", "microscope science lab"],
    "microscope": ["microscope science lab", "scientist laboratory"],
    "lab": ["science laboratory", "microscope science lab"],
    "cells": ["microscope cells", "science laboratory"],
    "cell": ["microscope cells", "science laboratory"],
    "muscle": ["muscle closeup", "athlete training"],
    "body": ["human body animation", "athlete training"],
    "person": ["person closeup", "face closeup slow motion"],
    "face": ["face closeup slow motion", "smiling face closeup"],
    "head": ["head closeup person", "face closeup slow motion"],
    "smile": ["smiling face closeup", "person laughing closeup"],
}

CATEGORY_SAFE_FALLBACKS = {
    "human body": ["human brain animation", "hand touching skin", "person thinking"],
    "psychology": ["person thinking", "person looking window", "human brain animation"],
    "space": ["space galaxy stars", "stars night sky"],
    "ocean": ["ocean waves underwater", "underwater blue"],
    "wild animals": ["lion running savanna", "cat looking camera"],
    "weather": ["lightning storm sky", "storm clouds dramatic"],
    "volcanoes": ["volcano eruption lava", "lava flowing closeup"],
    "ancient": ["ancient temple ruins", "old stone wall"],
    "money": ["money cash dollars", "gold coins treasure"],
    "food": ["cooking food closeup", "chef cooking"],
    "technology": ["computer code screen", "smartphone closeup hands"],
    "time": ["clock ticking closeup", "hourglass sand"],
    "deserts": ["desert sand dunes", "desert sunset"],
    "insects": ["insect macro closeup", "ant walking macro"],
    "dreams": ["dreamy clouds", "person sleeping"],
    "science": ["microscope science lab", "scientist laboratory"],
    "trees": ["forest fog morning", "tree branches sky"],
    "fire": ["fire flames dark", "ice glacier arctic"],
    "sports": ["runner running track", "athlete training"],
    "history": ["museum artifact closeup", "old manuscript closeup"],
}

GENERIC_SAFE_FALLBACK = "abstract background dark"


# ---- NEW: scene-aware keyword from narration ----
_NARRATION_STOPWORDS = {
    "the","a","an","is","are","was","were","of","to","in","on","at","and","or",
    "but","it","its","this","that","for","with","as","by","you","your","i","we",
    "they","he","she","has","have","had","do","does","did","be","been","will",
    "would","can","could","should","may","might","not","no","so","if","when",
    "than","then","there","their","them","us","our","my","me","just","very",
    "really","only","even","also","ever","never","every","each","more","most",
    "some","any","all","one","two","three","about","around","like","feel",
    "feels","felt","makes","make","made","gets","get","got","goes","went","come",
    "comes","came","see","sees","saw","seen","know","knows","knew","think",
    "thinks","thought","because","while","during","after","before","between",
    "through","against","into","over","under","above","below","here","where",
    "what","which","who","whom","whose","why","how",
}


def _extract_sentence_keyword(narration):
    """Pick the most concrete noun-phrase from a narration sentence."""
    words = re.findall(r"[a-zA-Z]+", (narration or "").lower())
    content = [w for w in words if w not in _NARRATION_STOPWORDS and len(w) > 3]
    if not content:
        return ""
    for w in content:
        if w in KEYWORD_SYNONYMS:
            return KEYWORD_SYNONYMS[w][0]
    content.sort(key=len, reverse=True)
    return " ".join(content[:2])


def validate_video(video_path):
    if not os.path.exists(video_path):
        return False
    if os.path.getsize(video_path) < 50000:
        return False
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height",
             "-of", "default=noprint_wrappers=1", video_path],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            return False
        decode = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", video_path,
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        return decode.returncode == 0
    except Exception:
        return False


def normalize_video(source_path, target_path):
    temp_output = target_path + ".normalized.mp4"
    if os.path.exists(temp_output):
        os.remove(temp_output)
    command = [
        "ffmpeg", "-y", "-i", source_path,
        "-map", "0:v:0", "-an",
        "-vf", "scale='min(1080,iw)':-2",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        temp_output,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError("FFmpeg video normalization failed:\n" + result.stderr[-1500:])
    if not validate_video(temp_output):
        if os.path.exists(temp_output):
            os.remove(temp_output)
        raise RuntimeError("Normalized video is still invalid.")
    if os.path.exists(target_path):
        os.remove(target_path)
    shutil.move(temp_output, target_path)
    return target_path


def download_file(url, target_path, extra_headers=None, asset_type="video"):
    temp_path = target_path + ".download"
    if os.path.exists(temp_path):
        os.remove(temp_path)
    print(f"Downloading asset: {target_path}...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
    }
    if extra_headers:
        headers.update(extra_headers)
    try:
        with requests.get(url, stream=True, headers=headers,
                          timeout=(20, 180), allow_redirects=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if asset_type == "video":
                if "video" not in content_type and "octet-stream" not in content_type:
                    raise ValueError(f"Unexpected video content type: {content_type}")
            elif asset_type == "audio":
                if ("audio" not in content_type and "octet-stream" not in content_type
                        and "mpeg" not in content_type and "ogg" not in content_type):
                    print(f"Warning: Unexpected audio content type: {content_type}")
            with open(temp_path, "wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file.write(chunk)
        if not os.path.exists(temp_path):
            raise RuntimeError("Downloaded file was not created.")
        min_size = 2000 if asset_type == "audio" else 50000
        if os.path.getsize(temp_path) < min_size:
            raise ValueError(f"Downloaded file is too small ({os.path.getsize(temp_path)} bytes)")
        if asset_type == "video" and not validate_video(temp_path):
            raise ValueError("Downloaded MP4 is corrupt or cannot be decoded.")
        if os.path.exists(target_path):
            os.remove(target_path)
        shutil.move(temp_path, target_path)
        return target_path
    except Exception as error:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        if os.path.exists(target_path):
            os.remove(target_path)
        raise RuntimeError(f"Asset download failed ({target_path}): {error}") from error


def _theme_fallbacks(keyword):
    kw = (keyword or "").lower().strip()
    words = kw.split()
    fallbacks = []
    if len(words) > 2:
        fallbacks.append(" ".join(words[-2:]))
        fallbacks.append(" ".join(words[:2]))
    if len(words) > 1:
        fallbacks.append(words[0])
        fallbacks.append(words[-1])
    for w in words:
        for syn in KEYWORD_SYNONYMS.get(w, []):
            if syn not in fallbacks:
                fallbacks.append(syn)
    for cat, items in CATEGORY_SAFE_FALLBACKS.items():
        if any(w in cat or w in " ".join(items) for w in words):
            for item in items:
                if item not in fallbacks:
                    fallbacks.append(item)
    fallbacks.append(GENERIC_SAFE_FALLBACK)
    seen = set()
    ordered = []
    for f in fallbacks:
        if f and f not in seen:
            seen.add(f)
            ordered.append(f)
    return ordered


def fetch_pexels_clip(keyword, target_path, min_duration=3):
    if validate_video(target_path):
        return target_path
    if os.path.exists(target_path):
        os.remove(target_path)
    if not PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY set nahi hai.")
    headers = {"Authorization": PEXELS_API_KEY}
    search_url = "https://api.pexels.com/videos/search"
    queries_to_try = _theme_fallbacks(keyword)
    for query in queries_to_try:
        print(f"Searching Pexels for '{query}'...")
        params = {"query": query, "orientation": "portrait", "per_page": 30}
        try:
            response = requests.get(search_url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"Pexels request failed for '{query}': {e}")
            continue
        videos = [
            v for v in data.get("videos", [])
            if v.get("duration", 0) >= min_duration
            and v.get("id") not in _USED_IDS
        ]
        if not videos:
            continue
        videos = videos[:10]
        random.shuffle(videos)
        for video in videos:
            files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"]
            if not files:
                continue
            files.sort(key=lambda f: abs((f.get("width", 1080) or 1080) - 1080))
            for f in files:
                raw_path = target_path + ".raw.mp4"
                try:
                    if os.path.exists(raw_path):
                        os.remove(raw_path)
                    download_file(f["link"], raw_path, asset_type="video")
                    normalize_video(raw_path, target_path)
                    if os.path.exists(raw_path):
                        os.remove(raw_path)
                    _USED_IDS.add(video.get("id"))
                    print(f"Valid Pexels clip ready: {target_path}")
                    return target_path
                except Exception as error:
                    print(f"Pexels clip failed: {error}")
                    if os.path.exists(raw_path):
                        os.remove(raw_path)
                    if os.path.exists(target_path):
                        os.remove(target_path)
    raise RuntimeError(f"No valid Pexels video found for '{keyword}'.")


def fetch_pixabay_clip(keyword, target_path):
    if validate_video(target_path):
        return target_path
    if os.path.exists(target_path):
        os.remove(target_path)
    if not PIXABAY_API_KEY:
        raise RuntimeError("PIXABAY_API_KEY set nahi hai.")
    queries_to_try = _theme_fallbacks(keyword)
    hits = []
    for query in queries_to_try:
        url = (
            "https://pixabay.com/api/videos/"
            f"?key={PIXABAY_API_KEY}"
            f"&q={requests.utils.quote(query)}"
            "&per_page=20"
        )
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            hits = response.json().get("hits", [])
        except Exception as e:
            print(f"Pixabay request failed for '{query}': {e}")
            hits = []
        if hits:
            break
    if not hits:
        raise RuntimeError(f"No Pixabay video found for '{keyword}'.")
    hits = [h for h in hits if h.get("id") not in _USED_IDS] or hits
    hits = hits[:10]
    random.shuffle(hits)
    last_error = None
    for hit in hits:
        try:
            videos = hit.get("videos", {})
            video_url = (
                videos.get("medium", {}).get("url")
                or videos.get("small", {}).get("url")
                or videos.get("large", {}).get("url")
            )
            if not video_url:
                continue
            raw_path = target_path + ".raw.mp4"
            download_file(video_url, raw_path, asset_type="video")
            normalize_video(raw_path, target_path)
            if os.path.exists(raw_path):
                os.remove(raw_path)
            _USED_IDS.add(hit.get("id"))
            return target_path
        except Exception as error:
            last_error = error
            raw_path = target_path + ".raw.mp4"
            if os.path.exists(raw_path):
                os.remove(raw_path)
    raise RuntimeError(f"All Pixabay clips failed for '{keyword}': {last_error}")


def fetch_fallback_ai_clip(keyword, target_path, duration=6):
    if os.path.exists(target_path):
        os.remove(target_path)
    img_prompt = requests.utils.quote(f"{keyword}, cinematic background, vertical 9:16")
    img_url = (
        "https://image.pollinations.ai/prompt/"
        f"{img_prompt}?width=1080&height=1920&nologo=true"
    )
    img_file = target_path + ".jpg"
    try:
        response = requests.get(img_url, timeout=90)
        response.raise_for_status()
        with open(img_file, "wb") as file:
            file.write(response.content)
        command = [
            "ffmpeg", "-y", "-loop", "1", "-i", img_file,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,"
                   "crop=1080:1920,"
                   "zoompan=z='min(zoom+0.0008,1.15)':d=150:s=1080x1920:fps=30",
            "-t", str(max(duration, 5)),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
            target_path,
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=180)
        if os.path.exists(img_file):
            os.remove(img_file)
        if result.returncode != 0:
            raise RuntimeError("AI fallback FFmpeg failed:\n" + result.stderr[-1500:])
        if not validate_video(target_path):
            raise RuntimeError("AI fallback generated an invalid video.")
        return target_path
    except Exception:
        if os.path.exists(img_file):
            os.remove(img_file)
        raise


def fetch_scene_video(keyword, target_path, min_duration=3, narration=""):
    """
    Main entry. Order:
      1) keyword -> Pexels
      2) narration-derived keyword -> Pexels
      3) keyword -> Pixabay
      4) AI fallback
    """
    if validate_video(target_path):
        return target_path
    if os.path.exists(target_path):
        os.remove(target_path)

    errors = []

    try:
        return fetch_pexels_clip(keyword, target_path, min_duration=min_duration)
    except Exception as e:
        errors.append(f"Pexels('{keyword}'): {e}")
        if os.path.exists(target_path):
            os.remove(target_path)

    derived = _extract_sentence_keyword(narration) if narration else ""
    if derived and derived != keyword:
        print(f"Retrying Pexels with narration-derived keyword: '{derived}'")
        try:
            return fetch_pexels_clip(derived, target_path, min_duration=min_duration)
        except Exception as e:
            errors.append(f"Pexels(derived '{derived}'): {e}")
            if os.path.exists(target_path):
                os.remove(target_path)

    try:
        return fetch_pixabay_clip(keyword, target_path)
    except Exception as e:
        errors.append(f"Pixabay: {e}")
        if os.path.exists(target_path):
            os.remove(target_path)

    try:
        return fetch_fallback_ai_clip(keyword, target_path, duration=max(min_duration, 4))
    except Exception as e:
        errors.append(f"AI fallback: {e}")

    raise RuntimeError(f"'{keyword}' -> no visual found:\n" + "\n".join(errors))


def fetch_scene_clips(scenes, scene_durations, output_dir="assets/scene_clips"):
    os.makedirs(output_dir, exist_ok=True)
    paths = []
    for index, (scene, duration) in enumerate(zip(scenes, scene_durations)):
        target_path = os.path.join(output_dir, f"scene_{index}.mp4")
        keyword = (
            scene.get("search_keyword", "").strip()
            or scene.get("visual_keyword", "").strip()
            or GENERIC_SAFE_FALLBACK
        )
        print(f"\nFetching scene {index + 1}: {keyword}")
        fetch_scene_video(
            keyword, target_path,
            min_duration=max(2, int(duration)),
            narration=scene.get("narration", ""),
        )
        if not validate_video(target_path):
            raise RuntimeError(f"Scene {index + 1} video invalid: {target_path}")
        paths.append(target_path)
    return paths
