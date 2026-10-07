"""
Script engine (Gemini) - English edition, RETENTION-OPTIMIZED.

Key upgrades for 80%+ retention:
  1. Hook = max 7 words, first 3 words MUST create shock/question.
  2. Last sentence = LOOP LINE that completes the hook -> seamless replay.
  3. Every scene's search_keyword must be FILMABLE on Pexels/Pixabay.
  4. Concept-fingerprint dedup (not just title dedup).
  5. Self-check pass by editor model for visual continuity.
"""

import json
import os
import random
import re
import time
from datetime import datetime

from google.genai import types

_DEFAULT_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]
MODELS = [
    m.strip()
    for m in os.getenv("GEMINI_MODELS", ",".join(_DEFAULT_MODELS)).split(",")
    if m.strip()
]
_DEAD_MODELS = set()

MIN_SCENES = 8
MAX_SCENES = 11
HISTORY_KEY = "_recent"
HISTORY_LIMIT = 120

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
    "shocking_fact": "ONE surprising verifiable fact. Hook = result, explanation after.",
    "personal_what_if": "A 'what if this happened to YOU' scenario in real science.",
    "myth_vs_truth": "Common belief stated first, then truth with real reason.",
    "mystery_explained": "Strange real phenomenon, then how it works.",
    "top3": "Three related facts, third is most shocking.",
}


FORBIDDEN_KEYWORD_WORDS = {
    "bullet", "paraponera", "clavata", "tarantula", "komodo",
    "cancer", "tumor", "alzheimer", "parkinson", "epilepsy",
    "dopamine", "serotonin", "melatonin", "cortisol", "insulin", "adrenaline",
    "einstein", "newton", "tesla", "edison", "darwin", "hawking",
    "nasa", "spacex", "google", "apple", "microsoft",
    "cerebellum", "hippocampus", "amygdala", "neuron", "synapse",
    "quetzal", "siphonophore", "apolemia", "hura", "crepitans",
}


def _keyword_is_filmable(keyword):
    if not keyword or len(keyword.split()) < 2:
        return False
    kw = keyword.lower()
    for bad in FORBIDDEN_KEYWORD_WORDS:
        if re.search(rf"\b{re.escape(bad)}\b", kw):
            return False
    return True


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


def _fingerprint(text):
    STOP = {"the","a","an","is","are","was","were","of","to","in","on","at","and","or",
            "but","it","its","this","that","for","with","as","by","you","your","i",
            "we","they","he","she","has","have","had","do","does","did","be","been"}
    words = re.findall(r"[a-z]+", (text or "").lower())
    content = sorted(set(w for w in words if w not in STOP and len(w) > 2))
    return " ".join(content[:12])


def record_history(path, script):
    data = load_history(path)
    recent = data.get(HISTORY_KEY, [])
    recent.append({
        "date": datetime.utcnow().isoformat(timespec="seconds"),
        "category": script.get("category", ""),
        "format": script.get("format", ""),
        "title": script.get("title", ""),
        "fact": script.get("core_fact", ""),
        "fingerprint": _fingerprint(script.get("core_fact", "") + " " + script.get("title", "")),
    })
    data[HISTORY_KEY] = recent[-HISTORY_LIMIT:]
    save_history(path, data)


def _is_duplicate(script, recent):
    fp_new = set(_fingerprint(script.get("core_fact", "") + " " + script.get("title", "")).split())
    if not fp_new:
        return False
    for r in recent[-60:]:
        fp_old = set((r.get("fingerprint") or "").split())
        if not fp_old:
            continue
        overlap = len(fp_new & fp_old) / max(1, min(len(fp_new), len(fp_old)))
        if overlap >= 0.6:
            print(f"Duplicate concept ({overlap:.0%}) with: {r.get('title')}")
            return True
    return False


def pick_plan(history_path):
    recent = load_history(history_path).get(HISTORY_KEY, [])
    recent_cats = [r.get("category") for r in recent[-8:]]
    recent_fmts = [r.get("format") for r in recent[-2:]]
    cats = [c for c in CATEGORIES if c not in recent_cats] or CATEGORIES
    fmts = [f for f in FORMATS if f not in recent_fmts] or list(FORMATS)
    return {
        "category": random.choice(cats),
        "format": random.choice(fmts),
        "avoid": [r.get("fact") or r.get("title") for r in recent[-40:] if (r.get("fact") or r.get("title"))],
    }


def sanitize_narration(text):
    text = str(text)
    text = re.sub(r"\$\s?(\d[\d,\.]*)", r"\1 dollars", text)
    text = text.replace("%", " percent").replace("&", " and ")
    text = text.replace("\u2014", ", ").replace("\u2013", ", ").replace(" - ", ", ")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"[\"\u201c\u201d`*_#\[\]{}()<>/\\|~^=+@]", " ", text)
    text = re.sub(r"\s+([,.?!;:])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_json(raw):
    raw = re.sub(r"```(?:json)?", "", raw or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON in response")
    return json.loads(raw[start:end + 1])


def _ask(client, prompt, temperature=1.0, max_rounds=3, base_wait=8):
    last = None
    for rnd in range(1, max_rounds + 1):
        live = [m for m in MODELS if m not in _DEAD_MODELS]
        if not live:
            break
        for model in live:
            try:
                print(f"[Gemini r{rnd}] {model}")
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
                print(f"   {model} failed: {msg[:150]}")
                if "404" in msg or "NOT_FOUND" in msg:
                    _DEAD_MODELS.add(model)
        if rnd < max_rounds:
            time.sleep(base_wait * rnd)
    raise RuntimeError(f"Gemini failed: {last}")


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
        scenes.append({"narration": narration, "caption": caption, "search_keyword": keyword})

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
        if not (2 <= len(kw_words) <= 4):
            problems.append(f"scene {i} search_keyword not 2-4 words")
        if not _keyword_is_filmable(sc["search_keyword"]):
            problems.append(f"scene {i} keyword '{sc['search_keyword']}' NOT filmable")

    if scenes and len(scenes[0]["narration"].split()) > 7:
        problems.append("hook longer than 7 words")

    total_words = sum(len(s["narration"].split()) for s in scenes)
    if scenes and not (70 <= total_words <= 110):
        problems.append(f"total words {total_words} outside 70-110")

    if len(scenes) >= 3:
        hook_words = set(re.findall(r"[a-z]{4,}", scenes[0]["narration"].lower()))
        last_words = set(re.findall(r"[a-z]{4,}", scenes[-1]["narration"].lower()))
        if not (hook_words & last_words):
            problems.append("last scene does not loop back to hook")

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


_SCHEMA = """{
  "core_fact": "one English sentence stating the main fact",
  "title": "English title, max 60 chars, one emoji, no hashtags",
  "description": "2-3 short English lines + one line of English search keywords.",
  "tags": ["15-20 lowercase English tags"],
  "scenes": [
    {
      "narration": "ONE short spoken sentence in natural English",
      "caption": "2-4 word on-screen hook text, UPPERCASE-friendly",
      "search_keyword": "2-4 english words describing EXACTLY what camera shows"
    }
  ]
}"""


def _writer_prompt(plan):
    avoid = "\n".join(f"- {a}" for a in plan["avoid"]) or "- (nothing yet)"
    return f"""
You are the head writer of a top English YouTube Shorts facts channel (US/UK Gen Z + young millennials).
Your videos average 2M+ views because you nail the FIRST 3 SECONDS and the LOOP ENDING.

CATEGORY: {plan['category']}
FORMAT: {plan['format']} -> {FORMATS[plan['format']]}

DO NOT repeat or paraphrase these earlier videos:
{avoid}

============================================================
HOOK (scene 1) - THIS IS 90% OF THE VIDEO'S SUCCESS
============================================================
- MAX 7 WORDS. First 3 words MUST create shock, danger, or burning question.
- NEVER start with 'Did you know', 'Have you ever', or any greeting.
- Patterns (pick one):
  1. Bold true claim: 'Your brain lies to you.'
  2. Warning: 'Never do this before sleep.'
  3. Impossible: 'This animal comes back to life.'
  4. Direct question: "Why can't you tickle yourself?"
  5. Countdown: 'Three seconds changes everything.'

============================================================
LOOP ENDING (scene LAST) - CRITICAL FOR 100%+ RETENTION
============================================================
The LAST sentence MUST complete or answer the HOOK sentence, so that when the
video loops back to scene 1, the viewer hears a continuous story.

Examples:
  - Hook: 'Your brain lies to you every day.'
  - Last: '...and that is how your brain lies to you.'  <- loop closes
  - Hook: "Why can't you tickle yourself?"
  - Last: '...that is why you cannot tickle yourself.'  <- loop closes

DO NOT end with 'follow for more', 'like and subscribe', or any CTA.
The last sentence must SHARE at least one content word with the hook.

============================================================
ACCURACY (non-negotiable)
============================================================
- Only real, well-established facts. No invented statistics.
- Round numbers are fine ('about', 'roughly', 'nearly').

============================================================
LANGUAGE
============================================================
- Natural spoken American English. SUPER EASY words, 10-year-old level.
- Plain text only: no emojis, hashtags, symbols inside narration.
- Each scene = EXACTLY ONE short sentence, 6-12 words.

============================================================
STRUCTURE (8 to 11 scenes, 80-100 words total)
============================================================
1. HOOK (max 7 words). First 3 words = shock/question.
2. One line of context. Zero filler.
3-4. Concrete detail, one real number, then the WHY.
5. RE-HOOK: flips or escalates, adds NEW info.
6-7. Story continues. Every scene ends on a small open loop.
Second-last: the twist / most surprising part.
Last: LOOP LINE that closes the hook (see above). Max 12 words.

============================================================
VISUAL-KEYWORD DISCIPLINE (READ TWICE)
============================================================
RULE 1 - KEYWORD MUST MATCH NARRATION.
   Scene says 'your brain predicts your touch' -> keyword 'hand touching skin'.
   NOT 'space galaxy'.

RULE 2 - ONLY FILMABLE, GENERIC KEYWORDS. Allowed vocabulary:
     human: human brain animation, hand touching skin, face closeup,
            person thinking, person laughing, person sleeping,
            human eye closeup, head closeup person, fingers moving,
            muscle closeup, athlete training
     animals: ant macro closeup, insect macro closeup, bee flower closeup,
              spider web macro, cat looking camera, dog running grass,
              lion running savanna, bird flying sky, fish underwater
     nature: ocean waves underwater, lightning storm sky, volcano eruption lava,
             forest fog morning, desert sand dunes, snow falling,
             rain window moody, fire flames dark, ice glacier arctic
     space: space galaxy stars, earth from space, moon closeup,
            rocket launching space, stars night sky
     city: city traffic night, city street aerial, car driving road,
           crowd people walking, street at night
     objects: money cash dollars, gold coins treasure, clock ticking closeup,
              hourglass sand, water pouring glass, food cooking closeup
     tech: computer code screen, laptop closeup hands, smartphone closeup hands,
           robot machine closeup, server data center
     ancient: ancient temple ruins, ancient pyramid egypt,
              museum artifact closeup, old stone wall
     science: microscope science lab, scientist laboratory,
              liquid pouring closeup, smoke slow motion
     abstract: abstract background dark, particles floating light

   FORBIDDEN (won't find clips for these):
     - Named species, molecules, diseases, brain parts, people, brands.
   Use METAPHOR shots:
     - 'cerebellum' -> 'human brain animation'
     - 'dopamine'   -> 'person smiling closeup'
     - 'bullet ant' -> 'ant macro closeup'

RULE 3 - 2-4 WORDS, PLAIN ENGLISH, NO PUNCTUATION, NO NAMES.

RULE 4 - EVERY SCENE GETS A DIFFERENT KEYWORD.

RULE 5 - SCENE 1 = MOST DRAMATIC, EYE-CATCHING FOOTAGE.
   Fast motion, closeup, dark/moody, or striking (fire, lightning,
   extreme eye closeup, running animal). NEVER a calm landscape.

SELF-CHECK before returning: for each scene, 'If a viewer heard this
sentence and saw ONLY this keyword's clip, would it match?' If NO,
change the keyword OR rewrite the sentence (keep the FACT accurate).

Return ONLY valid JSON:
{_SCHEMA}
"""


def _editor_prompt(draft_json):
    return f"""
You are a strict fact-checker, retention editor AND visual-continuity editor.
Return the FINAL JSON in the exact same schema.

CHECKLIST
1. Fact-check every claim. Replace wrong/exaggerated claims.
2. Hook: MAX 7 words. First 3 words create shock/question.
3. Every scene: one sentence, 6-12 words, natural spoken English.
4. Each scene adds new info. Keep 8-11 scenes, 80-100 words total.
5. LAST SCENE = LOOP LINE that completes/answers the HOOK and shares at
   least one content word with it. NO 'follow for more'. NO CTA.
6. Title: English, max 60 chars, one emoji, honest.

7. VISUAL CONTINUITY: For EVERY scene, if the keyword doesn't match the
   narration, REWRITE the keyword (2-4 plain words, no punctuation,
   different from other scenes). Scene 1 must be most dramatic footage.

8. HARD FILMABILITY FILTER:
   - NO named species/molecules/diseases/brain-parts/people/brands.
   - Replace with metaphors: cerebellum->human brain animation,
     dopamine->person smiling closeup, bullet ant->ant macro closeup.

Return ONLY the corrected JSON.

DRAFT:
{draft_json}
"""


def generate_script(client, history_path, attempts=4):
    plan = pick_plan(history_path)
    print(f"Category: {plan['category']}")
    print(f"Format:   {plan['format']}")

    recent = load_history(history_path).get(HISTORY_KEY, [])
    best = None

    for attempt in range(1, attempts + 1):
        try:
            draft = _ask(client, _writer_prompt(plan), temperature=1.0)
        except Exception as e:
            print(f"writer failed: {e}")
            continue

        try:
            edited = _ask(client, _editor_prompt(json.dumps(draft, ensure_ascii=False)),
                          temperature=0.4, max_rounds=2)
            final_script, final_problems = normalize_and_validate(edited)
        except Exception as e:
            print(f"editor failed: {e}")
            final_script, final_problems = None, ["editor failed"]

        draft_script, draft_problems = normalize_and_validate(draft)

        chosen = None
        if final_script and not final_problems:
            chosen = final_script
        elif draft_script and not draft_problems:
            chosen = draft_script
        else:
            print(f"attempt {attempt}: draft={draft_problems} | final={final_problems}")
            best = best or final_script or draft_script
            continue

        if _is_duplicate(chosen, recent):
            print(f"attempt {attempt}: concept duplicate, retrying")
            continue

        chosen["category"] = plan["category"]
        chosen["format"] = plan["format"]
        print(f"Script OK | {chosen['title']} | scenes={len(chosen['scenes'])}")
        return chosen

    if best and len(best.get("scenes", [])) >= MIN_SCENES:
        print("Using best-effort script despite warnings")
        best["category"] = plan["category"]
        best["format"] = plan["format"]
        return best
    return None
