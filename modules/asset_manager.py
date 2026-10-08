"""
Stock-footage fetcher - STRICT CONTEXTUAL SEARCH.

Problem this fixes
------------------
Old logic fell back to unrelated generic clips (girls, jets, random buildings)
whenever the exact keyword had no result. That is the #1 cause of
"visual != audio" -> viewers swipe away.

New logic
---------
1. QUERY LADDER (all anchored on the script's subject):
       scene search_keyword -> scene fallback_keyword -> subject
       -> subject + key narration word -> visual_domain terms
   There is NO generic "abstract background" / category fallback anymore.

2. RELEVANCE SCORING: every candidate clip is scored by comparing the query
   with the clip's own title/tags (Pexels slug, Pixabay tags). Clips whose
   metadata does not match the query are rejected - even if the API returned
   them. Portrait clips get a bonus; landscape clips are accepted and
   auto-cropped to 9:16.

3. If NO relevant stock clip exists for any query, we generate an image of the
   exact subject (Pollinations) and animate it (slow zoom) instead of showing
   something unrelated.

4. Every downloaded clip is normalised to exactly 1080x1920 @30fps (center
   crop = auto 9:16) and cut to 10s, which also makes the final render faster.
"""

import math
import os
import random
import re
import shutil
import subprocess

import requests

from modules.textutil import content_stems

_USED_IDS = set()
_SEARCH_CACHE = {}

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY")

TARGET_W, TARGET_H = 1080, 1920
MAX_LADDER = 7


# ------------------------------------------------------------------ video io ----
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
    """Auto-crop to 9:16 (1080x1920), 30fps, max 10s, no audio."""
    temp_output = target_path + ".normalized.mp4"
    if os.path.exists(temp_output):
        os.remove(temp_output)
    vf = (
        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_W}:{TARGET_H},setsar=1,fps=30"
    )
    command = [
        "ffmpeg", "-y", "-t", "10", "-i", source_path,
        "-map", "0:v:0", "-an",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        temp_output,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=240)
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


# ------------------------------------------------------------- query ladder ----
def _clean_query(q):
    q = re.sub(r"[^A-Za-z0-9 ]", " ", str(q or ""))
    return re.sub(r"\s+", " ", q).strip().lower()


_WEAK_WORDS = {
    "inside", "outside", "behind", "still", "these", "those", "around", "through", "almost",
    "often", "always", "never", "something", "nothing", "everything", "called", "because",
    "which", "other", "another", "before", "after", "should", "would", "could", "actually",
    "really", "means", "makes", "means", "while", "where", "their", "there", "years", "times",
    "about", "every", "first", "second", "third", "thing", "things", "people", "found",
    "scientists", "scientist", "nobody", "everyone", "anyone", "recently", "until",
}


def build_query_ladder(keyword, fallback_keyword="", subject="", domain=None, narration=""):
    """
    Ordered list of search queries, ALL tied to the video's subject.
    No generic / unrelated fallbacks.
    """
    ladder = []

    def add(q):
        q = _clean_query(q)
        if q and q not in ladder:
            ladder.append(q)

    add(keyword)
    add(fallback_keyword)
    add(subject)

    # subject head-noun + the most specific narration word
    subj_stems = content_stems(subject)
    head = _clean_query(subject).split()[-1] if _clean_query(subject) else ""
    words = [w for w in re.findall(r"[a-zA-Z]+", (narration or "").lower())
             if len(w) > 4 and w not in _WEAK_WORDS]
    words = [w for w in words if content_stems(w) and not (content_stems(w) & subj_stems)]
    words.sort(key=len, reverse=True)
    if head and words:
        add(f"{head} {words[0]}")

    for d in (domain or []):
        add(d)

    return ladder[:MAX_LADDER]


# ---------------------------------------------------------------- relevance ----
def _slug_text(url):
    """'https://www.pexels.com/video/school-of-fish-3209828/' -> 'school of fish'"""
    if not url:
        return ""
    parts = [p for p in str(url).split("/") if p]
    if not parts:
        return ""
    slug = parts[-1]
    slug = re.sub(r"-?\d{5,}$", "", slug)
    return slug.replace("-", " ").replace("_", " ")


def _relevance(query, text):
    """Returns (score 0..1, accepted?). Compares query stems with clip metadata stems."""
    q = content_stems(query)
    if not q:
        return 0.0, False
    t = content_stems(text)
    matched = len(q & t)
    need = max(1, math.ceil(len(q) * 0.5))
    return matched / len(q), matched >= need


# ----------------------------------------------------------------- searching ----
def _pexels_search(query):
    key = ("pexels", query)
    if key in _SEARCH_CACHE:
        return _SEARCH_CACHE[key]
    results = []
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "per_page": 40},   # all orientations; we auto-crop
            timeout=30,
        )
        r.raise_for_status()
        for v in r.json().get("videos", []):
            files = [
                {"w": f.get("width") or 0, "h": f.get("height") or 0, "link": f.get("link")}
                for f in v.get("video_files", [])
                if f.get("file_type") == "video/mp4" and f.get("link")
            ]
            if not files:
                continue
            results.append({
                "src": "pexels",
                "id": f"px{v.get('id')}",
                "duration": v.get("duration") or 0,
                "w": v.get("width") or 0,
                "h": v.get("height") or 0,
                "text": _slug_text(v.get("url")),
                "files": files,
            })
    except Exception as e:
        print(f"Pexels request failed for '{query}': {e}")
    _SEARCH_CACHE[key] = results
    return results


def _pixabay_search(query):
    key = ("pixabay", query)
    if key in _SEARCH_CACHE:
        return _SEARCH_CACHE[key]
    results = []
    try:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": PIXABAY_API_KEY, "q": query, "per_page": 30, "safesearch": "true"},
            timeout=30,
        )
        r.raise_for_status()
        for h in r.json().get("hits", []):
            vids = h.get("videos", {}) or {}
            files = []
            for size in ("large", "medium", "small"):
                v = vids.get(size) or {}
                if v.get("url"):
                    files.append({"w": v.get("width") or 0, "h": v.get("height") or 0,
                                  "link": v["url"]})
            if not files:
                continue
            big = files[0]
            results.append({
                "src": "pixabay",
                "id": f"pb{h.get('id')}",
                "duration": h.get("duration") or 0,
                "w": big["w"], "h": big["h"],
                "text": (h.get("tags") or "") + " " + _slug_text(h.get("pageURL")),
                "files": files,
            })
    except Exception as e:
        print(f"Pixabay request failed for '{query}': {e}")
    _SEARCH_CACHE[key] = results
    return results


def _rank(cands, query, min_duration, strict):
    """Relevant + unused + long enough clips, best first (small random shuffle for variety)."""
    scored = []
    for c in cands:
        if c["id"] in _USED_IDS or c["duration"] < min_duration:
            continue
        score, ok = _relevance(query, c["text"])
        if strict and score < 0.6:
            ok = False
        if not ok:
            continue
        portrait = c["h"] >= c["w"]
        scored.append((score + (0.3 if portrait else 0.0), c))
    if not scored:
        return []
    scored.sort(key=lambda x: -x[0])
    best = scored[0][0]
    band = [c for s, c in scored if s >= best - 0.1][:4]
    rest = [c for s, c in scored if c not in band][:4]
    random.shuffle(band)
    return band + rest


def _pick_file(c):
    """Choose a file giving enough resolution for a clean 1080x1920 center-crop."""
    files = sorted(c["files"], key=lambda f: (f["h"], f["w"]))
    portrait = c["h"] >= c["w"]
    need_h = 1280 if portrait else 1920          # landscape crop needs height >= 1920
    for f in files:
        if f["h"] >= need_h:
            return f
    return files[-1]


def motion_score(video_path):
    """
    How 'alive' a clip is: mean frame-to-frame change + contrast (0..~1).
    Static skies / still objects score low, action shots score high.
    Used to pick the most eye-catching hook clip. Returns 0.0 on any failure.
    """
    try:
        import numpy as np
        w, h = 72, 128
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", video_path, "-t", "4",
             "-vf", f"fps=6,scale={w}:{h},format=gray",
             "-f", "rawvideo", "-"],
            capture_output=True, timeout=60,
        )
        raw = proc.stdout
        n = len(raw) // (w * h)
        if proc.returncode != 0 or n < 3:
            return 0.0
        frames = np.frombuffer(raw[: n * w * h], dtype=np.uint8).reshape(n, h, w).astype(np.float32)
        motion = float(np.mean(np.abs(np.diff(frames, axis=0)))) / 255.0
        contrast = float(np.mean(np.std(frames, axis=(1, 2)))) / 128.0
        return motion * 6.0 + contrast
    except Exception:
        return 0.0


def _try_source(searcher, label, ladder, target_path, min_duration, strict, pick_best=False):
    """
    Walk the ladder; first query with usable clips wins.
    pick_best=True (hook): download up to 3 relevant candidates and keep the
    one with the most motion/contrast instead of the first one.
    """
    for query in ladder:
        cands = searcher(query)
        ranked = _rank(cands, query, min_duration, strict)
        print(f"[{label}] '{query}': {len(cands)} results, {len(ranked)} relevant")

        if pick_best and ranked:
            scored = []
            for k, c in enumerate(ranked[:3]):
                f = _pick_file(c)
                raw_path = target_path + f".raw{k}.mp4"
                cand_path = target_path + f".cand{k}.mp4"
                try:
                    download_file(f["link"], raw_path, asset_type="video")
                    normalize_video(raw_path, cand_path)
                    sc = motion_score(cand_path)
                    print(f"[{label}] hook candidate {c['id']} motion score {sc:.3f}")
                    scored.append((sc, c, cand_path))
                except Exception as error:
                    print(f"[{label}] hook candidate {c['id']} failed: {error}")
                    if os.path.exists(cand_path):
                        os.remove(cand_path)
                finally:
                    if os.path.exists(raw_path):
                        os.remove(raw_path)
            if scored:
                scored.sort(key=lambda x: -x[0])
                best_sc, best_c, best_path = scored[0]
                if os.path.exists(target_path):
                    os.remove(target_path)
                shutil.move(best_path, target_path)
                for _sc, _c, pth in scored[1:]:
                    if os.path.exists(pth):
                        os.remove(pth)
                _USED_IDS.add(best_c["id"])
                print(f"[{label}] HOOK USING {best_c['id']} ('{best_c['text'].strip()[:60]}') "
                      f"score {best_sc:.3f}")
                return target_path
            continue

        for c in ranked:
            f = _pick_file(c)
            raw_path = target_path + ".raw.mp4"
            try:
                download_file(f["link"], raw_path, asset_type="video")
                normalize_video(raw_path, target_path)
                _USED_IDS.add(c["id"])
                print(f"[{label}] USING {c['id']} ('{c['text'].strip()[:60]}') for '{query}'")
                return target_path
            except Exception as error:
                print(f"[{label}] clip {c['id']} failed: {error}")
            finally:
                if os.path.exists(raw_path):
                    os.remove(raw_path)
    raise RuntimeError(f"No relevant {label} clip for ladder {ladder}")


def footage_preflight(script, hook_min_duration=6, min_scene_ratio=0.75):
    """
    Cheap check BEFORE we spend time on voice/render: does stock footage really
    exist for this topic?  (Searches are cached, so the real download step
    re-uses them for free.)

    Returns (ok, report). ok=False -> pick another topic instead of making a
    video whose pictures do not match the words (= swipe).
    """
    searchers = []
    if PEXELS_API_KEY:
        searchers.append(_pexels_search)
    if PIXABAY_API_KEY:
        searchers.append(_pixabay_search)
    if not searchers:
        return True, "no stock API keys - preflight skipped"

    scenes = script.get("scenes", [])
    subject = script.get("subject", "")
    domain = script.get("visual_domain", [])
    covered = 0
    hook_hits = 0
    for i, sc in enumerate(scenes):
        ladder = build_query_ladder(sc.get("search_keyword", ""), sc.get("fallback_keyword", ""),
                                    subject, domain, sc.get("narration", ""))[:3]
        found = 0
        for q in ladder:
            for fn in searchers:
                try:
                    found += len(_rank(fn(q), q, hook_min_duration if i == 0 else 3,
                                       strict=(i == 0)))
                except Exception:
                    pass
            if found:
                break
        if i == 0:
            hook_hits = found
        if found:
            covered += 1
    ratio = covered / max(1, len(scenes))
    ok = hook_hits >= 2 and ratio >= min_scene_ratio
    return ok, f"hook candidates={hook_hits}, scenes with footage={covered}/{len(scenes)}"


# ----------------------------------------------------------------- AI image ----
def fetch_ai_image_clip(prompt_text, target_path, duration=5):
    """Exact-subject generated image + slow zoom (used only if no relevant stock exists)."""
    if os.path.exists(target_path):
        os.remove(target_path)
    img_prompt = requests.utils.quote(
        f"{prompt_text}, photorealistic, cinematic lighting, high contrast, vertical 9:16"
    )
    img_url = f"https://image.pollinations.ai/prompt/{img_prompt}?width=1080&height=1920&nologo=true"
    img_file = target_path + ".jpg"
    try:
        response = requests.get(img_url, timeout=90)
        response.raise_for_status()
        with open(img_file, "wb") as file:
            file.write(response.content)
        frames = int(max(duration, 5) * 30)
        command = [
            "ffmpeg", "-y", "-loop", "1", "-i", img_file,
            "-vf", f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
                   f"crop={TARGET_W}:{TARGET_H},"
                   f"zoompan=z='min(zoom+0.0008,1.15)':d={frames}:s={TARGET_W}x{TARGET_H}:fps=30",
            "-t", str(max(duration, 5)),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
            target_path,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=240)
        if result.returncode != 0:
            raise RuntimeError("AI image FFmpeg failed:\n" + result.stderr[-1500:])
        if not validate_video(target_path):
            raise RuntimeError("AI image clip is invalid.")
        return target_path
    finally:
        if os.path.exists(img_file):
            os.remove(img_file)


# ---------------------------------------------------------------- main entry ----
def fetch_scene_video(keyword, target_path, min_duration=3, narration="",
                      subject="", domain=None, fallback_keyword="", hook=False):
    """
    Order: Pexels ladder -> Pixabay ladder -> AI image of the exact subject.
    `hook=True` (scene 1) uses a stricter relevance threshold so the first
    frame shows the real subject.
    """
    if validate_video(target_path):
        return target_path
    if os.path.exists(target_path):
        os.remove(target_path)

    ladder = build_query_ladder(keyword, fallback_keyword, subject, domain, narration)
    print(f"Search ladder: {ladder}")
    errors = []

    for strict in ([True, False] if hook else [False]):
        if PEXELS_API_KEY:
            try:
                return _try_source(_pexels_search, "pexels", ladder, target_path,
                                   min_duration, strict, pick_best=hook)
            except Exception as e:
                errors.append(str(e))
        if PIXABAY_API_KEY:
            try:
                return _try_source(_pixabay_search, "pixabay", ladder, target_path,
                                   min_duration, strict, pick_best=hook)
            except Exception as e:
                errors.append(str(e))
        for p in (target_path,):
            if os.path.exists(p):
                os.remove(p)

    try:
        prompt = ", ".join(x for x in [subject, keyword or fallback_keyword] if x)
        print(f"No relevant stock clip - generating exact-subject visual: '{prompt}'")
        return fetch_ai_image_clip(prompt or keyword, target_path,
                                   duration=max(min_duration, 5))
    except Exception as e:
        errors.append(f"AI image: {e}")

    raise RuntimeError(f"'{keyword}' -> no relevant visual found:\n" + "\n".join(errors))
