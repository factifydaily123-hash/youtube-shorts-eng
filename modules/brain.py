"""
Script engine (Gemini) - English edition.

IMPORTANT: Ye version Gemini ko kabhi aise keywords use karne hi nahi dega
jinki REAL stock footage (Pexels/Pixabay par) maujood nahi hoti.
Isliye ab yahi rule hai: agar scene ko film karna mushkil hai
(named species, specific cell, specific historical figure, labeled animation),
to writer us SCENE KO REWRITE karega taake footage match kar sake - fact ki
quality kam NAHI hogi, sirf shot simple ho jayega.

Yehi 'bullet ant' problem ka asli hal hai:
  - Writer ek aise angle pe story likhega jise REAL footage se film kiya ja sake.
  - Ya wo 'ant macro closeup' jaisa common keyword use karega aur narration bhi
    usi hisaab se likhega, taake screen aur voice match karein.
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
        "Speak directly to the viewer (you). Only real, well-established consequences."
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


# ---------------------------------------------------------------------
# HARD FILMABILITY FILTER
# ---------------------------------------------------------------------
# Ye words scene keyword mein allowed NAHI hain kyunki:
#   - ya to footage unki exist hi nahi karti
#   - ya jo milti hai, wo scene se match nahi karti
# Agar writer ne aisa keyword diya, to scene ko rewrite karna padega.
FORBIDDEN_KEYWORD_WORDS = {
    # named species that stock sites don't tag specifically
    "bullet", "paraponera", "clavata", "tarantula", "hawk", "moth",
    "cobra", "python", "anaconda", "komodo", "dragon",
    # named diseases / conditions
    "cancer", "tumor", "alzheimer", "parkinson", "epilepsy", "autism",
    "adhd", "depression", "anxiety", "ptsd", "ocd",
    # named chemicals / molecules
    "dopamine", "serotonin", "melatonin", "cortisol", "insulin",
    "adrenaline", "histamine", "oxytocin",
    # named historical people / brands
    "einstein", "newton", "tesla", "edison", "darwin", "hawking",
    "nasa", "spacex", "google", "apple", "microsoft", "tesla",
    # specific numbers/anatomy that can't be filmed distinctly
    "cerebellum", "hippocampus", "amygdala", "neuron", "synapse",
    # specific objects that stock won't have
    "quetzal", "siphonophore", "apolemia", "hura", "crepitans",
}

# Words that ARE always safe and filmable
SAFE_KEYWORD_WHITELIST = {
    "human", "brain", "eye", "eyes", "hand", "hands", "finger", "fingers",
    "skin", "face", "head", "person", "people", "man", "woman", "child",
    "sleeping", "sleep", "dream", "thinking", "laugh", "laughing", "smile",
    "touch", "touching", "tickle", "walking", "running", "sitting",
    "ocean", "sea", "wave", "waves", "underwater", "fish", "shark", "whale",
    "space", "galaxy", "star", "stars", "planet", "earth", "moon", "rocket",
    "fire", "flame", "flames", "ice", "snow", "rain", "storm", "lightning",
    "volcano", "lava", "earthquake", "desert", "mountain", "forest", "tree",
    "trees", "plant", "leaf", "leaves", "flower", "ant", "bee", "insect",
    "spider", "cat", "dog", "lion", "tiger", "bird", "city", "traffic",
    "night", "street", "car", "clock", "time", "hourglass", "money", "gold",
    "coin", "coins", "computer", "laptop", "phone", "smartphone", "robot",
    "food", "cooking", "water", "glass", "kitchen", "pyramid", "temple",
    "museum", "scientist", "lab", "microscope", "cells", "muscle", "body",
    "abstract", "background", "dark", "closeup", "macro", "slow", "motion",
}


def _keyword_is_filmable(keyword):
    """
    True agar keyword mein koi forbidden word nahi, aur kam se kam ek
    word whitelist ya plain english noun ho.
    """
    if not keyword or len(keyword.split()) < 2:
        return False
    kw_lower = keyword.lower()
    for bad in FORBIDDEN_KEYWORD_WORDS:
        if re.search(rf"\b{re.escape(bad)}\b", kw_lower):
            return False
    return True


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
        kw_words = sc["search_keyword"].split()
        if len(kw_words) < 2 or len(kw_words) > 4:
            problems.append(f"scene {i} search_keyword not 2-4 words: '{sc['search_keyword']}'")
        # HARD FILMABILITY CHECK
        if not _keyword_is_filmable(sc["search_keyword"]):
            problems.append(
                f"scene {i} keyword '{sc['search_keyword']}' is NOT filmable on stock sites "
                f"(named species/molecule/person/etc)"
            )

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
- Use ONE of these 5 patterns:
  1. Bold true claim that sounds wrong: 'Your brain lies to you every day.'
  2. Warning to the viewer: 'Never do this before you sleep.'
  3. Impossible thing: 'This animal comes back to life.'
  4. Direct question: 'Why can't you tickle yourself?'
  5. Countdown/stakes: 'Just three seconds, and everything changes.'
- The hook must be TRUTHFULLY paid off in the last scenes. No clickbait lies.

============================================================
ACCURACY (non-negotiable)
============================================================
- Only real, well-established facts. If unsure, pick another fact.
- No invented statistics. No 'X will kill you' style fear-mongering.
- Round, defensible numbers ('about', 'roughly', 'nearly' are fine).

============================================================
LANGUAGE
============================================================
- Natural spoken American English, like a friend telling a story.
- SUPER EASY WORDS: a 10-year-old must understand every word on first hearing.
- Plain text only: no emojis, no hashtags, no symbols like % $ & inside narration.
- Each scene = EXACTLY ONE short sentence, 6-12 words.

============================================================
STRUCTURE (8 to 11 scenes, 75-95 words total)
============================================================
1. HOOK (see above). Max 8 words. First 3 words = shock/question.
2. One line of context. Zero filler.
3-4. Concrete detail, a real number, then the WHY in simple words.
5. RE-HOOK: a line that flips or escalates and still adds NEW information.
6-7. Story continues. Every scene adds new info and ends on a small open loop.
Second-last: the twist / most surprising part.
Last scene: {plan['cta']}. Max 10 words. No 'like/subscribe' begging.

============================================================
VISUAL-KEYWORD DISCIPLINE (READ THIS TWICE)
============================================================
The stock-footage search_keyword is the difference between a video that looks
PROFESSIONAL and one that looks like a random clip-dump. Follow these rules
WITHOUT EXCEPTION:

RULE 1 - KEYWORD MUST SHOW WHAT THE SCENE SAYS.
   If the scene says 'your brain predicts your own touch', the keyword must be
   'hand touching skin' - NOT 'space galaxy'.
   If the scene says 'this ant's sting feels like a gunshot', the keyword must
   be something like 'ant macro closeup' or 'insect closeup macro' - NOT
   'bullet ant' (Pexels/Pixabay don't tag that species).

RULE 2 - ONLY USE FILMABLE, GENERIC KEYWORDS.
   Allowed vocabulary (use ONLY words from this list plus basic adjectives
   like 'closeup', 'slow', 'dark', 'macro', 'night', 'underwater'):
     human body: human brain animation, hand touching skin, face closeup,
                 person thinking, person laughing, person sleeping,
                 human eye closeup, head closeup person, fingers moving,
                 muscle closeup, athlete training, doctor examining patient
     animals:    ant macro closeup, insect macro closeup, bee flower closeup,
                 spider web macro, cat looking camera, dog running grass,
                 lion running savanna, bird flying sky, fish underwater
     nature:     ocean waves underwater, lightning storm sky, volcano eruption lava,
                 forest fog morning, desert sand dunes, snow falling,
                 rain window moody, fire flames dark, ice glacier arctic
     space:      space galaxy stars, earth from space, moon closeup, rocket launching space,
                 stars night sky
     city:       city traffic night, city street aerial, car driving road,
                 crowd people walking, street at night
     objects:    money cash dollars, gold coins treasure, clock ticking closeup,
                 hourglass sand, water pouring glass, food cooking closeup
     tech:       computer code screen, laptop closeup hands, smartphone closeup hands,
                 robot machine closeup, server data center
     ancient:    ancient temple ruins, ancient pyramid egypt, museum artifact closeup,
                 old stone wall, old manuscript closeup
     science:    microscope science lab, scientist laboratory, liquid pouring closeup,
                 smoke slow motion
     abstract:   abstract background dark, particles floating light

   FORBIDDEN (stock sites DON'T tag these, and if forced you'll get a
   random clip that mismatches the narration):
     - Named species (bullet ant, tarantula hawk, komodo dragon, quetzal...)
     - Named molecules (dopamine, serotonin, insulin, adrenaline...)
     - Named diseases (cancer, alzheimer, parkinson...)
     - Named brain parts (cerebellum, hippocampus, amygdala...)
     - Named people/brands (Einstein, Tesla, NASA, Apple...)
     - Anything like 'cerebellum cancels the signal' or 'dopamine spike'.
   Instead, use a METAPHOR SHOT that the viewer will accept:
     - 'cerebellum' -> 'human brain animation' or 'head closeup person'
     - 'dopamine'   -> 'person smiling closeup' or 'human brain animation'
     - 'bullet ant' -> 'ant macro closeup' or 'insect macro closeup'
     - 'quetzal'    -> 'colorful bird flying' or 'bird closeup'

RULE 3 - 2-4 WORDS, PLAIN ENGLISH, NO PUNCTUATION, NO NAMES.
   Format: '<subject> <action>' or '<subject> <closeup>'.

RULE 4 - EVERY SCENE GETS A DIFFERENT KEYWORD.
   But every keyword must STILL match its own scene's narration.

RULE 5 - SCENE 1 (HOOK) = MOST DRAMATIC, EYE-CATCHING FOOTAGE.
   Fast motion, closeup, dark and moody, or striking (fire, lightning,
   extreme eye closeup, running animal, dark city street at night).
   NEVER a calm landscape as scene 1.

RULE 6 - DO NOT CHANGE THE FACT TO FIT THE CLIP.
   Keep the fact accurate. If it's hard to film (e.g. happens inside a cell),
   use a metaphor shot ('microscope science lab' or 'human brain animation'
   or 'liquid pouring closeup'). Never invent a fake visual claim.

SELF-CHECK before returning the JSON: for each scene, ask yourself
'If a viewer heard this exact sentence and saw ONLY this exact keyword's clip,
would they nod and think yes, this matches?'
If NO, change the keyword OR rewrite the scene's narration to match a filmable keyword.
The narration is what you can flex on - the FACT must stay accurate.

Return ONLY valid JSON, exactly this shape:
{_SCHEMA}
"""


def _editor_prompt(draft_json):
    return f"""
You are a strict fact-checker, retention editor AND visual-continuity editor
for an English YouTube Shorts facts channel. Below is a draft script (JSON).
Return the FINAL JSON in the exact same schema.

CHECKLIST
1. Fact-check every claim. If any claim is wrong, exaggerated or unverifiable,
   replace it with a verified detail (or rewrite the scene) so the whole video
   is true. Remove invented numbers.
2. Hook: max 8 words. First 3 words must create shock, danger or a burning
   question. Rewrite it if it sounds like an intro, a greeting or a textbook line.
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
   - Keyword = 2-4 plain english words, no punctuation, no names, no brands.
   - Every scene must use a DIFFERENT keyword.
   - Scene 1 must be the most dramatic footage (closeup / fast / dark / striking).

8. HARD FILMABILITY FILTER (reject these keywords):
   - NO named species (bullet ant, tarantula hawk, komodo dragon, quetzal)
   - NO named molecules (dopamine, serotonin, insulin, adrenaline)
   - NO named diseases (cancer, alzheimer, parkinson)
   - NO named brain parts (cerebellum, hippocampus, amygdala)
   - NO named people/brands (Einstein, Tesla, NASA, Apple)
   - NO abstract things that can't be filmed ('cerebellum signal',
     'dopamine spike', 'brain ignoring self')
   If the writer's keyword has any of these, REPLACE it with a filmable metaphor:
     - cerebellum  -> 'human brain animation' or 'head closeup person'
     - dopamine    -> 'person smiling closeup' or 'human brain animation'
     - bullet ant  -> 'ant macro closeup' or 'insect macro closeup'
     - quetzal     -> 'colorful bird flying' or 'bird closeup'
     - cancer cell -> 'microscope science lab' or 'liquid pouring closeup'
   Then, if needed, lightly rewrite the scene's narration so the metaphor shot
   feels natural. DO NOT change the fact itself.

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
