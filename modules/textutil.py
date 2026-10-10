"""
Small shared text helpers (no external deps).

Used by:
  - brain.py          -> topic de-duplication + keyword/narration relevance check
  - asset_manager.py  -> scoring stock clips against the search keyword
"""

import re

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "at", "and", "or",
    "but", "it", "its", "this", "that", "for", "with", "as", "by", "you", "your", "i", "we",
    "they", "he", "she", "has", "have", "had", "do", "does", "did", "be", "been", "will",
    "would", "can", "could", "should", "may", "might", "not", "no", "so", "if", "when",
    "than", "then", "there", "their", "them", "us", "our", "my", "me", "just", "very",
    "from", "into", "over", "under", "about", "what", "which", "who", "why", "how", "up",
    "out", "all", "any", "some", "one", "more", "most", "also", "even", "ever", "every",
    "his", "her", "him", "these", "those", "while", "because", "after", "before",
}

# words that describe HOW something is filmed, not WHAT is filmed
GENERIC_VISUAL = {
    "closeup", "close", "macro", "slow", "motion", "footage", "video", "view", "aerial",
    "shot", "shots", "background", "stock", "hd", "cinematic", "timelapse", "lapse",
    "moving", "vertical", "real", "natural", "beautiful", "amazing", "dark", "moody",
}


def stem(word):
    """Very small suffix stemmer: fishes->fish, swimming->swim, studies->study."""
    w = (word or "").lower()
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            base = w[: -len(suf)]
            if suf in ("ing", "ed") and len(base) >= 3 and base[-1] == base[-2] \
                    and base[-1] not in "aeiouls":
                base = base[:-1]          # swimm -> swim
            return base
    return w


def content_stems(text, drop_generic=True, min_len=3):
    """Set of stemmed meaningful words in `text`."""
    out = set()
    for w in re.findall(r"[a-zA-Z]+", (text or "").lower()):
        if len(w) < min_len or w in STOPWORDS:
            continue
        if drop_generic and w in GENERIC_VISUAL:
            continue
        out.add(stem(w))
    return out
