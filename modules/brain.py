"""
Script engine (Gemini) - English edition.

- Narration is plain spoken ENGLISH (the TTS voice is an en-US neural voice).
- On-screen word-by-word captions are the exact same English words.
- Two passes: writer -> strict fact-check/editor.
- Topic history stores the last facts and titles; Gemini is told to avoid them.
- Story structure with a real hook, a payoff, and a loop-back ending.
- Symbols / % / $ / stray characters are cleaned so TTS never stumbles.
- Optimized for US/UK audience retention (psychology/brain facts, strong hooks).
- KEYWORD DISCIPLINE: every scene's search_keyword MUST be footage that
  ACTUALLY EXISTS on Pexels/Pixabay and MUST visually match the spoken line.
  This is what kills the "clips mismatch" problem.
"""

import json
import os
import random
import re
import time
from datetime import datetime

from google.genai import types

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

CATEGORIES = [
    "human body and brain (eyes, heart, hands, sleep, tickle reflex)",
    "psychology and everyday human habits",
    "space, planets, stars and astronauts",
    "deep ocean and sea animals",
    "wild animals and their survival tricks",
    "extreme weather (lightning, storms, ice, rain)",
    "volcanoes, earthquakes and how the Earth works",
    "ancient civilizations, temples and ruins",
    "money, gold and strange facts about wealth",
    "food and cooking science",
    "technology, robots, computers and smartphones",
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
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"[\"\u201c\u201d`*_#\[\]{}()<>/\\|~^=+@]", " ", text)
    text = re.sub(r"\s+([,.?!;:])", r"\1", text)
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
                    _DEAD_MODELS.add(model)
        if rnd < max_rounds:
            time.sleep(base_wait * rnd)
    raise RuntimeError(f"Gemini failed after all retries: {last}")


_LATIN = re.compile(r"[A-Za-z]")
_NON_ENGLISH = re.compile(r"[\u0900-\u097F\u0600-\u06FF]")


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
        # keyword must be 2-4 simple english words, no punctuation
        kw_words = sc["search_keyword"].split()
        if len(kw_words) < 2 or len(kw_words) > 4:
            problems.append(f"scene {i} search_keyword not 2-4 words: '{sc['search_keyword']}'")

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
      "search_keyword": "2-4 english words describing EXACTLY what the camera should show"
    }
  ]
}"""


def _writer_prompt(plan):
    avoid = "\n".join(f"- {a}" for a in plan["avoid"]) or "- (nothing yet)"
    return f"""
You are the head writer of a top English-language YouTube Shorts facts channel
that targets US/UK audiences (Gen Z + young millennials). Your videos average
2M+ views because you know EXACTLY how to stop the scroll in the first 2 seconds.

CATEGORY: {plan['category']}
FORMAT: {plan['format']} -> {FORMATS[plan['format']]}
ENDING STYLE: {plan['cta']}

DO NOT repeat or paraphrase any of these earlier videos:
{avoid}
Also avoid the internet's most overused facts (honey never spoils, octopus has three hearts,
we use only 10% of the brain, banana radiation, Great Wall visible from space, etc).
Pick something a curious person would say "wait, really?" to.

============================================================
HOOK (scene 1) - THIS IS 90% OF THE VIDEO'S SUCCESS
============================================================
- Max 8 words. The FIRST 3 WORDS must create shock, danger, or an open question.
- NEVER start with 'Did you know', 'Have you ever', or any greeting. Start mid-action.
- The hook must feel like the viewer is ALREADY in the middle of a story.
- Use ONE of these 5 proven patterns (styles only, do NOT copy examples):

  1. BOLD TRUE CLAIM that sounds wrong:
     'Your brain lies to you every day.'
  2. WARNING to the viewer (direct 'you'):
     'Never do this before you sleep.'
  3. IMPOSSIBLE THING that grabs attention:
     'This animal comes back to life.'
  4. DIRECT QUESTION that hurts curiosity:
     'Why can't you tickle yourself?'
  5. STAKES / COUNTDOWN:
     'Just three seconds, and everything changes.'

- For PSYCHOLOGY / BRAIN facts: start with the weirdest symptom or result first.
- The hook must be TRUTHFULLY paid off in the last scenes. No clickbait lies.
- Scene 1 caption = the 2-3 most shocking words, UPPERCASE-friendly.

============================================================
ACCURACY (non-negotiable)
============================================================
- Only real, well-established facts. If unsure, choose a different fact.
- No invented statistics. No 'X will kill you' style medical fear-mongering.
- Use round, defensible numbers ('about', 'roughly', 'nearly' are fine).

============================================================
LANGUAGE (US/UK audience optimized)
============================================================
- Narration: natural spoken American English, like a friend telling a story.
- SUPER EASY WORDS: a 10-year-old must understand every word on first hearing.
- Plain text only: no emojis, no hashtags, no symbols like % $ & inside narration.
  Write 'percent' and 'dollars' as words.
- Use commas and full stops naturally so the voice gets rhythm and breath.
- Each scene = EXACTLY ONE short sentence, 6-12 words.

============================================================
STRUCTURE (8 to 11 scenes, 75-95 words total)
============================================================
1. HOOK (see above). Max 8 words. First 3 words = shock/question.
2. One line of context - why should I care? Zero filler.
3-4. Concrete detail, a real number, then the WHY in simple words.
5. RE-HOOK: a line that flips or escalates and still adds NEW information.
6-7. Story continues. Every scene adds new info and ends on a small open loop.
Second-last: the twist / most surprising part.
Last scene: {plan['cta']}. Max 10 words. No 'like/subscribe' begging.

============================================================
VISUAL-KEYWORD DISCIPLINE (THE MOST IMPORTANT RULE)
============================================================
The stock-footage search_keyword is the difference between a video that looks
PROFESSIONAL and one that looks like a random clip-dump. Follow these rules
WITHOUT EXCEPTION:

RULE 1 - KEYWORD MUST SHOW WHAT THE SCENE SAYS.
   If the scene says 'your brain predicts your own touch', the keyword must be
   'hand touching skin' or 'person arm closeup' - NOT 'space galaxy'.
   The viewer must feel the picture matches the voice word-for-word.

RULE 2 - ONLY USE FOOTAGE THAT REALLY EXISTS ON PEXELS/PIXABAY.
   These sites have HUGE libraries of: humans (hands, faces, eyes, walking,
   laughing, sleeping, exercising), nature (forests, oceans, mountains, storms,
   lightning, fire, ice, deserts), animals (cats, dogs, lions, birds, fish,
   insects), space (galaxy, stars, earth from space, moon, rockets), city
   (traffic, night lights, crowds), science (microscope, lab, liquid, smoke,
   fire experiments), food (cooking, fruit, water pouring, coffee), objects
   (clocks, coins, books, phones, computers).
   They DO NOT have: specific brain neurons firing, specific named diseases,
   microscopic cells labelled, historical figures, fake 3D medical animations,
   or named brands.
   -> NEVER use keywords like 'cerebellum animation', 'dopamine synapse',
      'Neuron 3D render', 'Albert Einstein portrait'. They will fail and force
      a random fallback clip (which is the EXACT mismatch you must avoid).

RULE 3 - CONVERT ABSTRACT IDEAS INTO FILMABLE SHOTS.
   Bad: 'your brain ignores you' -> keyword 'brain ignoring'
   Good: 'your brain ignores you' -> keyword 'hand touching arm'
   Bad: 'cerebellum cancels the signal' -> keyword 'cerebellum signal'
   Good: 'cerebellum cancels the signal' -> keyword 'human brain animation'
        (only if you can be sure 'human brain animation' footage exists - it does)
   Bad: 'you feel ticklish' -> keyword 'ticklish feeling'
   Good: 'you feel ticklish' -> keyword 'person laughing closeup'

RULE 4 - 2-4 WORDS, PLAIN ENGLISH, NO PUNCTUATION, NO NAMES.
   Format: '<subject> <action>' or '<subject> <closeup>'.
   Examples that ALWAYS work:
     'human eye closeup', 'hand touching skin', 'person laughing',
     'woman sleeping', 'human brain animation', 'neurons firing',
     'ocean waves underwater', 'lightning storm sky', 'space galaxy stars',
     'city traffic night', 'gold coins closeup', 'clock ticking closeup',
     'fire flames dark', 'microscope science lab', 'water pouring glass',
     'cat looking camera', 'lion running savanna', 'rocket launching space',
     'smartphone closeup hands', 'forest fog morning', 'person thinking window'.

RULE 5 - EVERY SCENE GETS A DIFFERENT KEYWORD. Never repeat the same keyword twice.
   But every keyword must STILL match its own scene's narration.

RULE 6 - SCENE 1 (HOOK) = MOST DRAMATIC, EYE-CATCHING FOOTAGE.
   Use fast motion, closeup, dark and moody, or a striking image. Something that
   stops the thumb mid-scroll (fire, lightning, extreme closeup of an eye,
   running animal, dark city street at night). NEVER a calm landscape as scene 1.

RULE 7 - DO NOT CHANGE THE FACT TO FIT THE CLIP.
   Keep the fact accurate and interesting. If a fact is hard to film (e.g. it
   happens inside a cell), pick a metaphor shot the viewer will accept as a
   stand-in (microscope, liquid, slow-motion human skin closeup, scientist at
   microscope). Never invent a fake visual claim.

SELF-CHECK before you return the JSON: for each scene, ask yourself
'If a viewer heard this exact sentence and saw ONLY this exact keyword's clip,
would they nod and think yes, this matches?' If NO, change the keyword.

Return ONLY valid JSON, exactly this shape:
{_SCHEMA}
"""


def _editor_prompt(draft_json):
    return f"""
You are a strict fact-checker, retention editor AND visual-continuity editor for
an English YouTube Shorts facts channel. Below is a draft script (JSON).
Return the FINAL JSON in the exact same schema.

CHECKLIST
1. Fact-check every claim. If any claim is wrong, exaggerated or unverifiable,
   replace it with a verified detail (or rewrite the scene) so the whole video
   is true. Remove invented numbers.
2. Hook: max 8 words. First 3 words must create shock, danger or a burning
   question. Rewrite it if it sounds like an intro, a greeting or a textbook line.
   The hook must be truthfully paid off.
3. Every scene: one sentence, 6-12 words, natural spoken English, simple everyday
   words a 10-year-old knows. No emojis or symbols inside narration.
4. Cut filler. Each scene must add new info. Keep 8-11 scenes, 75-95 words total.
   Scene 5 must work as a re-hook.
5. Last scene: short, natural, no begging for likes.
6. Title: English, max 60 chars, one emoji, honest.

7. VISUAL CONTINUITY (MOST IMPORTANT - do this scene by scene):
   For EVERY scene, read the narration and the search_keyword together.
   If a viewer hearing the narration and seeing only that keyword's clip would
   feel a mismatch, REWRITE the search_keyword so it matches the spoken line.
   Replace abstract / non-filmable keywords ('cerebellum signal', 'dopamine
   spike', 'brain ignoring self') with filmable, common footage keywords
   ('human brain animation', 'hand touching skin', 'person laughing closeup').
   - Keyword = 2-4 plain english words, no punctuation, no names, no brands.
   - Every scene must use a DIFFERENT keyword.
   - Scene 1 must be the most dramatic footage (closeup / fast / dark / striking).
   - Never invent a keyword for something stock sites don't have
     (no 'named historical figure', no 'specific labelled cell', no 'brand logo').

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
            edited = _ask(client, _editor_prompt(json.dumps(draft, ensure_ascii=False)),
                          temperature=0.4, max_rounds=2)
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
