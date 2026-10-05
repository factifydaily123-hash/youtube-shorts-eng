"""
Script engine (Gemini) - English edition.

- Narration is plain spoken ENGLISH (the TTS voice is an en-US neural voice).
  On-screen word-by-word captions are the exact same English words, so the
  captions always match the audio.
- Two passes: writer -> strict fact-check/editor.
- Topic history stores the last facts and titles; Gemini is told to avoid them.
- Story structure with a real hook, a payoff, and a loop-back ending.
- Symbols / % / $ / stray characters are cleaned so TTS never stumbles.
"""

import json
import os
import random
import re
import time
from datetime import datetime

from google.genai import types

# Order = priority. Each model has its OWN quota, so a longer list = more free calls per day.
# Dead names (404) are skipped automatically. Override without editing code:
#   GEMINI_MODELS: 'gemini-3.8-flash,gemini-3.6-flash'   (in run.yml)
_DEFAULT_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
]
MODELS = [
    m.strip()
    for m in os.getenv("GEMINI_MODELS", ",".join(_DEFAULT_MODELS)).split(",")
    if m.strip()
]
_DEAD_MODELS = set()

MIN_SCENES = 8
MAX_SCENES = 12
HISTORY_KEY = "_recent"
HISTORY_LIMIT = 90

# Every category maps to footage that really exists on Pexels/Pixabay.
CATEGORIES = [
    "human body and brain (eyes, heart, hands, sleep)",
    "space, planets, stars and astronauts",
    "deep ocean and sea animals",
    "wild animals and their survival tricks",
    "extreme weather (lightning, storms, ice, rain)",
    "volcanoes, earthquakes and how the Earth works",
    "ancient civilizations, temples and ruins",
    "money, gold and strange facts about wealth",
    "food and cooking science",
    "technology, robots, computers and smartphones",
    "psychology and everyday human habits",
    "time, clocks and calendars",
    "deserts, mountains and extreme places on Earth",
    "insects and tiny creatures",
    "dreams and sleep",
    "science labs, experiments and discoveries",
    "trees, plants and forests",
    "fire, ice and extreme temperatures",
    "sports and the limits of the human body",
    "history's strangest records and museum objects",
]

FORMATS = {
    "shocking_fact": (
        "ONE surprising, verifiable fact, explained step by step. "
        "Hook = the surprising result, explanation comes after."
    ),
    "personal_what_if": (
        "A 'what if this happened to YOU' scenario grounded in real science. "
        "Speak directly to the viewer (you). Only real, well-established consequences - "
        "no invented numbers, no fake 'X will kill you' claims."
    ),
    "myth_vs_truth": (
        "Start with a very common belief, then reveal the truth with a real reason. "
        "Hook = the belief stated as if it were true, then flip it."
    ),
    "mystery_explained": (
        "Open with a strange real phenomenon that seems impossible, "
        "then explain how it actually works."
    ),
    "top3": (
        "Three real, related facts, each in 2-3 short scenes, escalating so that the "
        "third is the most shocking. Hook promises three things."
    ),
}

CTA_STYLES = [
    "a loop line whose last words complete the hook sentence, so the video replays naturally",
    "a loop line whose last words complete the hook sentence, so the video replays naturally",
    "a soft question that invites a comment (e.g. which one surprised you most)",
    "a curiosity teaser for the next video, no begging for likes",
]


# ----------------------------------------------------------- history ----
def load_history(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_history(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"history save failed: {e}")


def record_history(path, script):
    data = load_history(path)
    recent = data.get(HISTORY_KEY, [])
    recent.append({
        "date": datetime.utcnow().isoformat(timespec="seconds"),
        "category": script.get("category", ""),
        "format": script.get("format", ""),
        "title": script.get("title", ""),
        "fact": script.get("core_fact", ""),
    })
    data[HISTORY_KEY] = recent[-HISTORY_LIMIT:]
    save_history(path, data)


def pick_plan(history_path):
    recent = load_history(history_path).get(HISTORY_KEY, [])
    recent_cats = [r.get("category") for r in recent[-8:]]
    recent_fmts = [r.get("format") for r in recent[-2:]]

    cats = [c for c in CATEGORIES if c not in recent_cats] or CATEGORIES
    fmts = [f for f in FORMATS if f not in recent_fmts] or list(FORMATS)

    return {
        "category": random.choice(cats),
        "format": random.choice(fmts),
        "cta": random.choice(CTA_STYLES),
        "avoid": [r.get("fact") or r.get("title") for r in recent[-40:] if (r.get("fact") or r.get("title"))],
    }


# ------------------------------------------------- text sanitising ----
def sanitize_narration(text):
    """Make a line safe and natural for English TTS."""
    text = str(text)
    text = re.sub(r"\$\s?(\d[\d,\.]*)", r"\1 dollars", text)
    text = text.replace("%", " percent")
    text = text.replace("&", " and ")
    text = text.replace("\u2014", ", ").replace("\u2013", ", ").replace(" - ", ", ")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)          # 1,000 -> 1000
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"[\"\u201c\u201d`*_#\[\]{}()<>/\\|~^=+@]", " ", text)
    text = re.sub(r"\s+([,.?!;:])", r"\1", text)             # no space before punctuation
    text = re.sub(r"\s+", " ", text).strip()
    return text


# -------------------------------------------------------------- Gemini ----
def _extract_json(raw):
    raw = re.sub(r"```(?:json)?", "", raw or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(raw[start:end + 1])


def _ask(client, prompt, temperature=1.0, max_rounds=3, base_wait=10):
    last = None
    for rnd in range(1, max_rounds + 1):
        live = [m for m in MODELS if m not in _DEAD_MODELS]
        if not live:
            break
        for model in live:
            try:
                print(f"[Gemini round {rnd}/{max_rounds}] {model}")
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        top_p=0.95,
                        response_mime_type="application/json",
                    ),
                )
                return _extract_json(resp.text)
            except Exception as e:
                last = e
                msg = str(e)
                print(f"   {model} failed: {msg[:160]}")
                if "404" in msg or "NOT_FOUND" in msg:
                    _DEAD_MODELS.add(model)  # never retry a removed model in this run
        if rnd < max_rounds:
            time.sleep(base_wait * rnd)
    raise RuntimeError(f"Gemini failed after all retries: {last}")


_LATIN = re.compile(r"[A-Za-z]")
_NON_ENGLISH = re.compile(r"[\u0900-\u097F\u0600-\u06FF]")   # Devanagari / Arabic-Urdu script


def normalize_and_validate(data):
    """Return (script, problems). Script is cleaned; problems is a list of strings."""
    problems = []
    if not isinstance(data, dict):
        return None, ["not a dict"]

    scenes = []
    for sc in data.get("scenes") or []:
        if not isinstance(sc, dict):
            continue
        narration = sanitize_narration(sc.get("narration", ""))
        if not narration:
            continue
        keyword = str(sc.get("search_keyword") or sc.get("visual_keyword") or "").strip()
        caption = str(sc.get("caption") or "").strip()
        # Captions on screen = the spoken English words, so audio and text always match.
        scenes.append({"narration": narration, "caption": caption,
                       "search_keyword": keyword, "roman": narration})

    if not (MIN_SCENES <= len(scenes) <= MAX_SCENES):
        problems.append(f"scene count {len(scenes)} not in {MIN_SCENES}-{MAX_SCENES}")

    for i, sc in enumerate(scenes, 1):
        words = sc["narration"].split()
        if _NON_ENGLISH.search(sc["narration"]):
            problems.append(f"scene {i} has non-English script")
        if not _LATIN.search(sc["narration"]):
            problems.append(f"scene {i} has no English words")
        if len(words) > 16:
            problems.append(f"scene {i} too long ({len(words)} words)")
        if not sc["search_keyword"]:
            problems.append(f"scene {i} missing search_keyword")

    if scenes and len(scenes[0]["narration"].split()) > 9:
        problems.append("hook longer than 9 words")

    total_words = sum(len(s["narration"].split()) for s in scenes)
    if scenes and not (65 <= total_words <= 115):
        problems.append(f"total words {total_words} outside 65-115")

    tags = data.get("tags") or []
    script = {
        "title": str(data.get("title", "")).strip(),
        "description": str(data.get("description", "")).strip(),
        "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
        "core_fact": str(data.get("core_fact", "")).strip(),
        "scenes": scenes,
    }
    if not script["title"]:
        problems.append("missing title")
    return script, problems


# ------------------------------------------------------------- prompts ----
_SCHEMA = """{
  "core_fact": "one English sentence stating the main fact (used to avoid repeats)",
  "title": "English title, max 60 chars, one emoji, no hashtags",
  "description": "2-3 short English lines + one line of English search keywords. No hashtags.",
  "tags": ["15-20 lowercase English tags"],
  "scenes": [
    {
      "narration": "ONE spoken sentence in natural English",
      "caption": "2-4 word on-screen hook text in English, UPPERCASE-friendly",
      "search_keyword": "2-4 english words for stock footage"
    }
  ]
}"""


def _writer_prompt(plan):
    avoid = "\n".join(f"- {a}" for a in plan["avoid"]) or "- (nothing yet)"
    return f"""
You are the head writer of a top English-language YouTube Shorts facts channel.
Write ONE fresh 28-34 second Short. Retention is everything: most viewers decide
to swipe in the first 2-3 seconds.

CATEGORY: {plan['category']}
FORMAT: {plan['format']} -> {FORMATS[plan['format']]}
ENDING STYLE: {plan['cta']}

DO NOT repeat or paraphrase any of these earlier videos:
{avoid}
Also avoid the internet's most overused facts (honey never spoils, octopus has three hearts,
we use only 10% of the brain, banana radiation, Great Wall visible from space, etc).
Pick something a curious person would say "wait, really?" to.

HOOK (scene 1) - THE MOST IMPORTANT LINE
- Max 8 words. The first 3 words must already create shock, danger, or an open question.
- No greeting, no intro, never start with 'Did you know'. Start mid-action, like the
  story is already happening.
- Use ONE of these patterns (styles only, do NOT copy the examples):
  1. Bold true claim that sounds wrong: 'Your brain lies to you every day.'
  2. Warning to the viewer: 'Never do this before you sleep.'
  3. Impossible thing: 'This animal comes back to life.'
  4. Direct 'you' question: 'You do this daily, but why?'
  5. Countdown/stakes: 'Just three seconds, and everything changes.'
- The hook must promise something the LAST scenes actually deliver. No clickbait lies.
- Scene 1 caption = the 2-3 most shocking words, UPPERCASE-friendly.

ACCURACY (non-negotiable)
- Only real, well-established facts. If you are not sure, choose a different fact.
- No invented statistics. No "X will kill you" style medical fear-mongering.
- Use round, defensible numbers ("about", "roughly", "nearly" are fine).

LANGUAGE
- Narration: natural spoken American English, like a friend telling a story - NOT a textbook.
- SUPER EASY WORDS: a 10-year-old must understand every word on first hearing. No bookish or
  technical words when a simple one exists ('use' not 'utilize', 'big' not 'enormous',
  'begin' not 'commence'). Contractions are good (it's, you're, doesn't).
- Plain text only: no emojis, no hashtags, no symbols like % $ & inside narration.
  Write 'percent' and 'dollars' as words. Small numbers as words ('three', 'fifty');
  big or year numbers may use digits ('1969', '8000').
- Use commas and full stops naturally so the voice gets rhythm and breath.
- Each scene = EXACTLY ONE short sentence, 6-12 words.

STRUCTURE (8 to 11 scenes, 75-95 words total)
1. HOOK (see above).
2. One line of context - why should I care? Zero filler, go straight into the story.
3-4. Concrete detail, a real number, then the WHY in simple words.
5. RE-HOOK: a line that flips or escalates ('But here's the real twist.' style, in your own
   words) and still adds NEW information. This stops the mid-video drop-off.
6-7. The story continues. Every scene adds new info and ends on a small open loop.
Second-last: the twist / most surprising part.
Last scene: {plan['cta']}. Max 10 words. No 'like/subscribe' begging.

VISUALS
- search_keyword: English, 2-4 words, BROAD footage that certainly exists on free stock sites
  (space galaxy, ocean waves, lion running, human eye closeup, city traffic night, gold coins,
  ancient temple ruins, scientist microscope, lightning storm, sleeping person, clock ticking...).
- Scene 1 keyword must be the most dramatic, eye-catching footage (fast motion, closeup, dark
  and moody), because it is the first frame the viewer sees.
- Never a brand, a person's name, or a rare species. Every scene must have a DIFFERENT keyword.
- Make the keyword match what is SAID in that scene, so picture and voice agree.

Return ONLY valid JSON, exactly this shape:
{_SCHEMA}
"""


def _editor_prompt(draft_json):
    return f"""
You are a strict fact-checker and retention editor for an English YouTube Shorts facts channel.
Below is a draft script (JSON). Improve it and return the FINAL JSON in the exact same schema.

CHECKLIST
1. Fact-check every claim. If any claim is wrong, exaggerated or unverifiable, replace it with a
   verified detail (or rewrite the scene) so the whole video is true. Remove invented numbers.
2. Hook: max 8 words. First 3 words must create shock, danger or a burning question. Rewrite it
   if it sounds like an intro, a greeting or a textbook line. The hook must be truthfully paid off.
3. Every scene: one sentence, 6-12 words, natural spoken English, simple everyday words a
   10-year-old knows. No emojis or symbols (% $ & etc) inside narration - write 'percent', 'dollars'.
4. Cut filler. Each scene must add new info. Keep 8-11 scenes, 75-95 words total. Scene 5 must
   work as a re-hook (a flip or escalation).
5. Last scene: short, natural, no begging for likes. It should make the viewer want to replay or comment.
6. Keep search_keyword broad, English, 2-4 words, different for every scene, matching the spoken line.
7. Title: English, max 60 chars, one emoji, honest (no false promise).

Return ONLY the corrected JSON.

DRAFT:
{draft_json}
"""


# ---------------------------------------------------------- public API ----
def generate_script(client, history_path, attempts=3):
    plan = pick_plan(history_path)
    print(f"Category: {plan['category']}")
    print(f"Format:   {plan['format']}")

    best = None
    for attempt in range(1, attempts + 1):
        try:
            draft = _ask(client, _writer_prompt(plan), temperature=1.0)
        except Exception as e:
            print(f"writer failed: {e}")
            continue

        draft_script, draft_problems = normalize_and_validate(draft)
        final_script, final_problems = None, ["editor not run"]

        try:
            edited = _ask(client, _editor_prompt(json.dumps(draft, ensure_ascii=False)), temperature=0.4, max_rounds=2)
            final_script, final_problems = normalize_and_validate(edited)
        except Exception as e:
            print(f"editor failed (using draft if valid): {e}")

        if final_script and not final_problems:
            chosen = final_script
        elif draft_script and not draft_problems:
            print(f"editor output rejected: {final_problems}")
            chosen = draft_script
        else:
            print(f"attempt {attempt} problems: draft={draft_problems} final={final_problems}")
            best = best or final_script or draft_script
            continue

        chosen["category"] = plan["category"]
        chosen["format"] = plan["format"]
        print(f"Script OK | {chosen['title']} | scenes={len(chosen['scenes'])}")
        return chosen

    if best and len(best.get("scenes", [])) >= MIN_SCENES:
        print("Using best-effort script despite validation warnings")
        best["category"] = plan["category"]
        best["format"] = plan["format"]
        return best
    return None
