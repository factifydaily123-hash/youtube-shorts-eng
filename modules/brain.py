"""
Script engine (Gemini) - English edition, RETENTION + RELEVANCE optimized.

What is new in this version
---------------------------
1. SUBJECT-ANCHORED VISUALS
   The writer must return a precise `subject` (e.g. "barreleye fish") and a
   `visual_domain` (e.g. ["deep sea fish", "underwater ocean"]). Every scene
   gets a `search_keyword` (what the camera shows) and a `fallback_keyword`
   (broader, but still the same world). asset_manager walks this ladder and
   NEVER falls back to unrelated stock (girls, jets, random buildings).

2. HOOK STYLE ROTATION  -> no more identical template every video.
   A hook style + loop style are picked per video and remembered in history.

3. SEAMLESS LOOP
   Last sentence re-uses the key words of the hook so the replay feels like
   one continuous sentence.

4. ON-SCREEN HOOK TITLE (`hook_title`) for the first 3 seconds.

5. STRONG DUPLICATE CHECK
   - same subject (stem-based, over the whole history)  -> rejected
   - overlapping concept fingerprint                    -> rejected
   - near-identical title                               -> rejected
"""

import difflib
import json
import os
import random
import re
import time
from datetime import datetime

from google.genai import types

from modules.textutil import content_stems

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

MIN_SCENES = 6
MAX_SCENES = 9
MIN_WORDS = 55
MAX_WORDS = 82
HISTORY_KEY = "_recent"
HISTORY_LIMIT = 300

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

# Rotating hook styles -> avoids the "same template every video" penalty.
HOOK_STYLES = {
    "impossible_claim": (
        "State something that sounds impossible but is true.",
        ["This fish has a see-through head.", "Some trees are older than writing."],
    ),
    "direct_question": (
        "Ask a question the viewer cannot answer instantly.",
        ["Why can't you tickle yourself?", "Why does the ocean glow at night?"],
    ),
    "warning": (
        "A warning or 'never do this' that creates tension.",
        ["Never swim near this glowing jellyfish.", "Stop holding your breath like that."],
    ),
    "number_shock": (
        "Open with ONE shocking specific number.",
        ["Four hundred years old. Still alive.", "Ninety percent of this ocean is unseen."],
    ),
    "contradiction": (
        "Attack a common belief in the first words.",
        ["Goldfish do not forget in three seconds.", "Lightning does strike twice."],
    ),
    "you_scenario": (
        "Put the viewer inside the situation.",
        ["Your eyes are lying to you right now.", "Your brain deletes things while you sleep."],
    ),
}

LOOP_STYLES = {
    "answer_loop": (
        "Last sentence ANSWERS / completes the hook and re-uses its key words, "
        "so the replay sounds like: ANSWER -> same hook again."
    ),
    "mirror_loop": (
        "Last sentence ends on the hook's key phrase as a final twist "
        "(e.g. hook 'This fish has a see-through head.' -> last "
        "'...and that is why this fish has a see-through head.')."
    ),
}

# Stock sites cannot give us these -> keep them out of keywords.
FORBIDDEN_KEYWORD_WORDS = {
    "einstein", "newton", "tesla", "edison", "darwin", "hawking",
    "nasa", "spacex", "google", "apple", "microsoft", "tiktok", "youtube",
}

# Words that should never be what the camera shows unless narration is about them
OFF_TOPIC_RISK = {"girl", "woman", "man", "boy", "model", "fashion", "jet", "airplane",
                  "business", "office", "meeting", "handshake", "building", "skyscraper"}


# ---------------------------------------------------------------- history ----
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


def _fingerprint(text, limit=20):
    """Most distinctive stems (longer words first) -> stable concept fingerprint."""
    stems = sorted(content_stems(text, drop_generic=False, min_len=4), key=lambda w: (-len(w), w))
    return " ".join(stems[:limit])


def _subject_key(subject):
    return " ".join(sorted(content_stems(subject, drop_generic=False)))


def _script_fingerprint(script):
    narr = " ".join(s.get("narration", "") for s in script.get("scenes", []))
    return _fingerprint(
        f"{script.get('core_fact', '')} {script.get('title', '')} "
        f"{script.get('subject', '')} {narr}"
    )


def record_history(path, script):
    data = load_history(path)
    recent = data.get(HISTORY_KEY, [])
    recent.append({
        "date": datetime.utcnow().isoformat(timespec="seconds"),
        "category": script.get("category", ""),
        "format": script.get("format", ""),
        "hook_style": script.get("hook_style", ""),
        "loop_style": script.get("loop_style", ""),
        "title": script.get("title", ""),
        "subject": script.get("subject", ""),
        "subject_key": _subject_key(script.get("subject", "")),
        "fact": script.get("core_fact", ""),
        "fingerprint": _script_fingerprint(script),
    })
    data[HISTORY_KEY] = recent[-HISTORY_LIMIT:]
    save_history(path, data)


def _is_duplicate(script, recent):
    """True if this video is the same / too similar a concept as an earlier one."""
    subj_new = set(_subject_key(script.get("subject", "")).split())
    fp_new = set(_script_fingerprint(script).split())
    title_new = (script.get("title") or "").lower()

    for r in recent:
        # 1) same subject (e.g. 'barreleye' vs 'barreleye fish')
        subj_old = set((r.get("subject_key") or "").split())
        if subj_new and subj_old and (subj_new <= subj_old or subj_old <= subj_new):
            print(f"Duplicate SUBJECT with: {r.get('title')} [{r.get('subject')}]")
            return True

        # 2) concept fingerprint overlap
        fp_old = set((r.get("fingerprint") or "").split())
        if fp_new and fp_old:
            overlap = len(fp_new & fp_old) / max(1, min(len(fp_new), len(fp_old)))
            if overlap >= 0.5:
                print(f"Duplicate CONCEPT ({overlap:.0%}) with: {r.get('title')}")
                return True

        # 3) near-identical title
        t_old = (r.get("title") or "").lower()
        if title_new and t_old and difflib.SequenceMatcher(None, title_new, t_old).ratio() >= 0.75:
            print(f"Duplicate TITLE with: {r.get('title')}")
            return True
    return False


def pick_plan(history_path):
    recent = load_history(history_path).get(HISTORY_KEY, [])
    recent_cats = [r.get("category") for r in recent[-8:]]
    recent_fmts = [r.get("format") for r in recent[-2:]]
    recent_hooks = [r.get("hook_style") for r in recent[-3:]]
    recent_loops = [r.get("loop_style") for r in recent[-1:]]

    cats = [c for c in CATEGORIES if c not in recent_cats] or CATEGORIES
    fmts = [f for f in FORMATS if f not in recent_fmts] or list(FORMATS)
    hooks = [h for h in HOOK_STYLES if h not in recent_hooks] or list(HOOK_STYLES)
    loops = [l for l in LOOP_STYLES if l not in recent_loops] or list(LOOP_STYLES)

    subjects, seen = [], set()
    for r in reversed(recent):
        s = (r.get("subject") or "").strip().lower()
        if s and s not in seen:
            seen.add(s)
            subjects.append(s)
        if len(subjects) >= 150:
            break

    return {
        "category": random.choice(cats),
        "format": random.choice(fmts),
        "hook_style": random.choice(hooks),
        "loop_style": random.choice(loops),
        "avoid": [r.get("fact") or r.get("title") for r in recent[-40:]
                  if (r.get("fact") or r.get("title"))],
        "avoid_subjects": subjects,
    }


# ------------------------------------------------------------- sanitizing ----
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


def sanitize_keyword(text):
    text = re.sub(r"[^A-Za-z0-9 ]", " ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip().lower()


def sanitize_hook_title(text):
    """On-screen title: letters/digits/basic punctuation, max 8 words."""
    text = str(text or "")
    text = re.sub(r"[^A-Za-z0-9 ?!'.,%$-]", " ", text)
    words = re.sub(r"\s+", " ", text).strip().split()
    return " ".join(words[:8])


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


# -------------------------------------------------------------- validation ----
def normalize_and_validate(data):
    problems = []
    if not isinstance(data, dict):
        return None, ["not a dict"]

    subject = sanitize_keyword(data.get("subject"))
    domain = []
    raw_domain = data.get("visual_domain") or []
    if isinstance(raw_domain, str):
        raw_domain = [raw_domain]
    for d in raw_domain:
        d = sanitize_keyword(d)
        if d and d not in domain:
            domain.append(d)
    domain = domain[:3]

    if not subject or len(subject.split()) > 4:
        problems.append("missing/too long subject (1-3 words needed)")
    if not domain:
        problems.append("missing visual_domain")

    subject_ctx = content_stems(subject + " " + " ".join(domain))

    scenes = []
    for sc in data.get("scenes") or []:
        if not isinstance(sc, dict):
            continue
        narration = sanitize_narration(sc.get("narration", ""))
        if not narration:
            continue
        keyword = sanitize_keyword(sc.get("search_keyword") or sc.get("visual_keyword"))
        fallback = sanitize_keyword(sc.get("fallback_keyword"))
        caption = str(sc.get("caption") or "").strip()
        scenes.append({
            "narration": narration,
            "caption": caption,
            "search_keyword": keyword,
            "fallback_keyword": fallback,
        })

    if not (MIN_SCENES <= len(scenes) <= MAX_SCENES):
        problems.append(f"scene count {len(scenes)} not in {MIN_SCENES}-{MAX_SCENES}")

    off_topic = 0
    off_topic_kw = []
    for i, sc in enumerate(scenes, 1):
        words = sc["narration"].split()
        if _NON_ENGLISH.search(sc["narration"]):
            problems.append(f"scene {i} has non-English script")
        if not _LATIN.search(sc["narration"]):
            problems.append(f"scene {i} has no English words")
        if len(words) > 13:
            problems.append(f"scene {i} too long ({len(words)} words)")

        kw = sc["search_keyword"]
        if not kw:
            problems.append(f"scene {i} missing search_keyword")
            continue
        if not (2 <= len(kw.split()) <= 4):
            problems.append(f"scene {i} search_keyword not 2-4 words")
        if any(re.search(rf"\b{re.escape(b)}\b", kw) for b in FORBIDDEN_KEYWORD_WORDS):
            problems.append(f"scene {i} keyword '{kw}' has a name stock sites can't film")

        # keyword must be tied to the subject world OR to this sentence
        kw_stems = content_stems(kw)
        narr_stems = content_stems(sc["narration"])
        if not (kw_stems & (subject_ctx | narr_stems)):
            off_topic += 1
            off_topic_kw.append(f"scene {i} '{kw}'")
        # risky generic people/objects only allowed if the narration mentions them
        risky = [w for w in kw.split() if w in OFF_TOPIC_RISK and w not in sc["narration"].lower()]
        if risky:
            problems.append(f"scene {i} keyword '{kw}' is off-topic stock ({risky[0]})")

        if not sc["fallback_keyword"]:
            sc["fallback_keyword"] = domain[0] if domain else subject

    # one loosely-related scene is fine (metaphor shot); two or more = drifting visuals
    if off_topic >= 2:
        problems.append("keywords not tied to subject/narration: " + "; ".join(off_topic_kw))

    if scenes and len(scenes[0]["narration"].split()) > 7:
        problems.append("hook longer than 7 words")

    total_words = sum(len(s["narration"].split()) for s in scenes)
    if scenes and not (MIN_WORDS <= total_words <= MAX_WORDS):
        problems.append(f"total words {total_words} outside {MIN_WORDS}-{MAX_WORDS}")

    if len(scenes) >= 3:
        hook_words = content_stems(scenes[0]["narration"], drop_generic=False, min_len=4)
        last_words = content_stems(scenes[-1]["narration"], drop_generic=False, min_len=4)
        if not (hook_words & last_words):
            problems.append("last scene does not loop back to hook")

    hook_title = sanitize_hook_title(data.get("hook_title"))
    if not hook_title and scenes:
        hook_title = sanitize_hook_title(scenes[0]["narration"])
    if len(hook_title.split()) < 2:
        problems.append("hook_title too short")

    tags = data.get("tags") or []
    script = {
        "title": str(data.get("title", "")).strip(),
        "description": str(data.get("description", "")).strip(),
        "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
        "core_fact": str(data.get("core_fact", "")).strip(),
        "subject": subject,
        "visual_domain": domain,
        "hook_title": hook_title,
        "scenes": scenes,
    }
    if not script["title"]:
        problems.append("missing title")
    return script, problems


# ----------------------------------------------------------------- prompts ----
_SCHEMA = """{
  "core_fact": "one English sentence stating the main fact",
  "subject": "the exact main thing of the video, 1-3 words (e.g. 'barreleye fish', 'lightning', 'human eye')",
  "visual_domain": ["2-3 broad stock-footage search terms for the subject's WORLD, e.g. 'deep sea fish', 'underwater ocean'"],
  "hook_title": "3-7 word BOLD on-screen title for the first 3 seconds, may end with ?! (e.g. 'This Fish Has A Transparent Head?!')",
  "title": "English YouTube title, max 60 chars, one emoji, no hashtags",
  "description": "2-3 short English lines + one line of English search keywords.",
  "tags": ["15-20 lowercase English tags"],
  "scenes": [
    {
      "narration": "ONE short spoken sentence in natural English",
      "caption": "2-4 word on-screen text",
      "search_keyword": "2-4 plain words = what the camera shows for THIS sentence (must be about the subject or its world)",
      "fallback_keyword": "1-3 plain words, broader but still the same subject world"
    }
  ]
}"""


def _writer_prompt(plan):
    avoid = "\n".join(f"- {a}" for a in plan["avoid"]) or "- (nothing yet)"
    banned = ", ".join(plan["avoid_subjects"]) or "(none yet)"
    hook_desc, hook_examples = HOOK_STYLES[plan["hook_style"]]
    hook_ex = " | ".join(hook_examples)
    return f"""
You are the head writer of a top English YouTube Shorts facts channel (US/UK Gen Z + young millennials).
Viewers decide in 2 seconds whether to swipe. Your job: stop the swipe, keep the eyes, force the replay.

CATEGORY: {plan['category']}
FORMAT: {plan['format']} -> {FORMATS[plan['format']]}
HOOK STYLE (use THIS style only): {plan['hook_style']} -> {hook_desc}
   style examples (do NOT copy): {hook_ex}
LOOP STYLE (use THIS style only): {plan['loop_style']} -> {LOOP_STYLES[plan['loop_style']]}

BANNED SUBJECTS - already covered, pick something clearly different:
{banned}

DO NOT repeat or paraphrase these earlier videos:
{avoid}

============================================================
VIDEO LENGTH = 25-30 SECONDS
============================================================
- 6 to 9 scenes, 60-78 words TOTAL. One short sentence per scene (6-11 words).
- A new piece of information or a visual change every ~3-4 seconds.

============================================================
HOOK (scene 1) - 90% OF THE VIDEO'S SUCCESS
============================================================
- MAX 7 WORDS. The first 3 words create shock, danger or a burning question.
- Name the SUBJECT or its strangest property in the hook, so the first visual
  can show exactly that thing.
- NEVER start with 'Did you know', 'Have you ever', 'Today', 'In this video', any greeting.
- Then add `hook_title`: the same idea as bold on-screen text, 3-7 words, can end with ?!

============================================================
SEAMLESS LOOP (last scene)
============================================================
- The LAST sentence must flow straight back into the FIRST one when the video replays.
- It MUST re-use at least one key content word of the hook.
- NO 'follow', 'subscribe', 'like', 'comment', 'part 2'. NO CTA.
  Good pair:  last '...and that is why your brain lies to you.'  ->  hook 'Your brain lies to you every day.'

============================================================
ACCURACY (non-negotiable)
============================================================
- Only real, well-established facts. No invented statistics. Round numbers are fine.

============================================================
LANGUAGE
============================================================
- Natural spoken American English, 10-year-old vocabulary. Short punchy sentences.
- Plain text only: no emojis, hashtags or symbols inside narration.

============================================================
VISUALS - EVERY SHOT MUST SHOW WHAT THE VOICE SAYS
============================================================
`subject`: the precise main thing (1-3 words). If the video is about a barreleye fish -> "barreleye fish".
`visual_domain`: 2-3 broader stock-footage terms for the subject's world ("deep sea fish", "underwater ocean").

For each scene:
  search_keyword (2-4 words) = what a camera would film DURING THIS SENTENCE.
     - It MUST be about the subject or its world. You MAY use the real name
       (animal, planet, food, landmark, body part, phenomenon) - the search system
       automatically falls back to `fallback_keyword` when the exact name has no footage.
     - Prefer concrete filmable nouns + shot type: 'deep sea fish', 'jellyfish dark water',
       'human eye closeup', 'lightning storm sky', 'lava flowing closeup'.
     - FORBIDDEN: unrelated people or objects (girl, woman, man, jet, office, building, handshake)
       unless the sentence is literally about them.
     - No people's names, brands, abstract ideas ('curiosity', 'danger').
  fallback_keyword (1-3 words) = broader term of the SAME world ('fish underwater').
  All scenes must use DIFFERENT search_keywords, but all stay inside the subject's world.
  Scene 1 = the exact subject, as dramatic as possible (closeup, motion, high contrast).

SELF-CHECK: if a viewer heard each sentence and saw ONLY that keyword's footage,
would it match? If not, change the keyword.

Return ONLY valid JSON:
{_SCHEMA}
"""


def _editor_prompt(draft_json, plan):
    return f"""
You are a strict fact-checker, retention editor AND visual-continuity editor.
Return the FINAL JSON in the exact same schema (keep `subject`, `visual_domain`, `hook_title`).

CHECKLIST
1. Fact-check every claim. Replace wrong or exaggerated claims.
2. Hook (scene 1): MAX 7 words, first 3 words shock/question, names the subject or its strangest property.
   Keep the hook style: {plan['hook_style']}.
3. Every scene: one sentence, 6-11 words, natural spoken English. 6-9 scenes, 60-78 words total
   (video MUST be 25-30 seconds).
4. SEAMLESS LOOP: last scene re-uses a key content word of the hook and flows back into it.
   No CTA, no 'follow for more'.
5. `hook_title`: 3-7 words, bold, curiosity-driven, matches the hook.
6. Title: English, max 60 chars, one emoji, honest.
7. VISUAL MATCH: for every scene the search_keyword must show what the sentence says AND belong
   to the subject's world. Rewrite keywords that drift (no girls, jets, offices, buildings or other
   unrelated stock unless the sentence is about them). 2-4 plain words, all different.
   fallback_keyword = broader term of the same world.
8. Do not change `subject` to something else.

Return ONLY the corrected JSON.

DRAFT:
{draft_json}
"""


# --------------------------------------------------------------- generation ----
def generate_script(client, history_path, attempts=5):
    plan = pick_plan(history_path)
    print(f"Category:   {plan['category']}")
    print(f"Format:     {plan['format']}")
    print(f"Hook style: {plan['hook_style']} | Loop: {plan['loop_style']}")

    recent = load_history(history_path).get(HISTORY_KEY, [])
    best = None

    for attempt in range(1, attempts + 1):
        try:
            draft = _ask(client, _writer_prompt(plan), temperature=1.0)
        except Exception as e:
            print(f"writer failed: {e}")
            continue

        try:
            edited = _ask(client, _editor_prompt(json.dumps(draft, ensure_ascii=False), plan),
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
            cand = final_script or draft_script
            if cand and not _is_duplicate(cand, recent):
                best = best or cand
            continue

        if _is_duplicate(chosen, recent):
            print(f"attempt {attempt}: duplicate topic, retrying")
            continue

        chosen["category"] = plan["category"]
        chosen["format"] = plan["format"]
        chosen["hook_style"] = plan["hook_style"]
        chosen["loop_style"] = plan["loop_style"]
        print(f"Script OK | {chosen['title']} | subject={chosen['subject']} "
              f"| scenes={len(chosen['scenes'])}")
        return chosen

    if best and len(best.get("scenes", [])) >= MIN_SCENES:
        print("Using best-effort script despite warnings (non-duplicate)")
        best["category"] = plan["category"]
        best["format"] = plan["format"]
        best["hook_style"] = plan["hook_style"]
        best["loop_style"] = plan["loop_style"]
        return best
    return None
