"""
Stock-footage fetcher with STRICT keyword discipline.

The old version's fallback was 'if scene keyword finds nothing, use a random
PEXELS_SEARCH_TERMS entry'. That is exactly what caused the "clips mismatch"
problem: a scene about brain cells suddenly got a mountain landscape.

New behaviour:
  - Fallback is now HIERARCHICAL and STAYS ON-TOPIC.
    1. Try the exact keyword.
    2. Try a shortened version of the SAME keyword (last 2 words, then first 2).
    3. Try a synonym/nearby keyword from KEYWORD_SYNONYMS (same theme).
    4. Try the SAME CATEGORY's safe fallback keyword.
    5. Only as a very last resort use a generic safe clip, and log a warning.
  - No random 'PEXELS_SEARCH_TERMS' drift.
  - Every accepted clip must match the theme keyword list.
"""

import os
import random
import subprocess
import shutil
import requests


# Clip IDs already used in this run - no same footage twice in one video
_USED_IDS = set()

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")


# ---------------------------------------------------------------------
# THEME-ANCHORED FALLBACKS
# ---------------------------------------------------------------------
# If the exact scene keyword fails, we try a synonym of the SAME theme first.
# This is what keeps the clip visually aligned with the narration.
KEYWORD_SYNONYMS = {
    "brain": ["human brain animation", "head closeup person", "person thinking"],
    "neuron": ["neurons firing", "human brain animation", "microscope cells"],
    "nerve": ["neurons firing", "human brain animation", "hand touching skin"],
    "cerebellum": ["human brain animation", "head closeup person"],
    "memory": ["person thinking", "human brain animation", "old photo closeup"],
    "think": ["person thinking", "person looking away window", "head closeup person"],
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
    "dream": ["dreamy clouds", "night sky stars", "person sleeping"],
    "reflex": ["human body animation", "doctor examining patient", "muscle closeup"],
    "space": ["space galaxy stars", "earth from space", "stars night sky"],
    "galaxy": ["space galaxy stars", "stars night sky timelapse"],
    "planet": ["planet space animation", "earth from space"],
    "star": ["stars night sky", "space galaxy stars"],
    "ocean": ["ocean waves underwater", "ocean waves aerial", "underwater blue"],
    "sea": ["ocean waves underwater", "underwater deep sea"],
    "shark": ["shark swimming underwater", "fish underwater"],
    "whale": ["whale ocean underwater", "ocean waves underwater"],
    "fish": ["fish swimming underwater", "aquarium fish closeup"],
    "cat": ["cat looking camera", "cat playing"],
    "dog": ["dog running grass", "dog looking camera"],
    "lion": ["lion running savanna", "lion closeup"],
    "fire": ["fire flames dark", "campfire night", "flame closeup"],
    "ice": ["ice glacier arctic", "frozen ice closeup"],
    "snow": ["snow falling", "snow mountain"],
    "lightning": ["lightning storm sky", "storm clouds dramatic"],
    "storm": ["storm clouds dramatic", "rain window moody"],
    "rain": ["rain window moody", "rain falling street"],
    "volcano": ["volcano eruption lava", "lava flowing closeup"],
    "earthquake": ["earthquake cracked ground", "city building shaking"],
    "money": ["money cash dollars", "coins closeup"],
    "gold": ["gold coins treasure", "gold closeup shiny"],
    "coin": ["gold coins treasure", "coins closeup"],
    "clock": ["clock ticking closeup", "old clock wall"],
    "time": ["clock ticking closeup", "hourglass sand"],
    "computer": ["computer code screen", "laptop closeup hands"],
    "phone": ["smartphone closeup hands", "phone scrolling"],
    "robot": ["robot machine closeup", "robot factory"],
    "internet": ["server data center", "network cables closeup"],
    "food": ["cooking food closeup", "chef cooking"],
    "water": ["water pouring glass", "water droplet closeup"],
    "pyramid": ["ancient pyramid egypt", "desert pyramid"],
    "temple": ["ancient temple ruins", "old stone temple"],
    "mummy": ["ancient mummy museum", "museum artifact closeup"],
    "tree": ["forest fog morning", "tree branches sky"],
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
    "snow": ["snow falling", "snow mountain"],
    "storm": ["storm clouds dramatic", "rain window moody"],
}

# Category-level safety net - only used when keyword AND synonyms AND
# shortened keyword all fail. Still theme-appropriate.
CATEGORY_SAFE_FALLBACKS = {
    "human body and brain": ["human brain animation", "hand touching skin", "person thinking"],
    "psychology": ["person thinking", "person looking window", "human brain animation"],
    "space": ["space galaxy stars", "stars night sky"],
    "ocean": ["ocean waves underwater", "underwater blue"],
    "wild animals": ["lion running savanna", "cat looking camera"],
    "extreme weather": ["lightning storm sky", "storm clouds dramatic"],
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

# Very last resort: a safe generic clip that never looks 'wrong' even if
# it's not a perfect match (better than crashing). This should rarely fire.
GENERIC_SAFE_FALLBACK = "abstract background dark"


# ---------------------------------------------------------------------
# Validation + normalisation
# ---------------------------------------------------------------------
def validate_video(video_path):
    if not os.path.exists(video_path):
        return False

    if os.path.getsize(video_path) < 50000:
        return False

    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height",
                "-of", "default=noprint_wrappers=1",
                video_path
            ],
            capture_output=True, text=True, timeout=30
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            return False

        decode = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", video_path,
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30
        )
        return decode.returncode == 0
    except Exception:
        return False


def normalize_video(source_path, target_path):
    temp_output = target_path + ".normalized.mp4"

    if os.path.exists(temp_output):
        os.remove(temp_output)

    command = [
        "ffmpeg", "-y",
        "-i", source_path,
        "-map", "0:v:0",
        "-an",
        "-vf", "scale='min(1080,iw)':-2",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        temp_output
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
