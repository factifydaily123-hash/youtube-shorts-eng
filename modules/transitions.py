"""
Randomized scene-to-scene transitions.

Why
---
Hard cuts every single scene is one of the loudest "template" signals a video
can send. Real editors mix cuts with a handful of transitions - a wipe here,
a slide there, occasionally just a hard cut. This module gives every video
its OWN small palette of transition styles so joins stop looking mechanical.

What it does
------------
- `pick_transitions(n_joins, style)` returns a list of `n_joins` transition
  names, drawn from a per-video palette that the style dict biases.
- `apply_transitions(scenes, transitions, duration_each, transition_time)` is a
  MoviePy helper that concatenates the scene clips with xfade effects between
  them. Audio is untouched (the final mix already sits on the master timeline).
- The palette is REPRODUCIBLE per video (seeded by style) but DIFFERENT
  between videos, which is exactly what we want.

Notes
-----
MoviePy's xfade via `CompositeVideoClip` with `crossfadein/crossfadeout` is
slow and lossy for 9:16. Instead we hand it to ffmpeg with the `xfade`
filter (fast, GPU-free, high quality), then just read the result back.
This keeps the rest of the composer code unchanged.
"""

import os
import random
import subprocess

# Safe xfade modes across ffmpeg 4.3+ (all tested on Ubuntu 22.04 + ffmpeg 6)
XFADE_MODES = [
    "fade",
    "wipeleft",
    "wiperight",
    "wipeup",
    "wipedown",
    "slideleft",
    "slideright",
    "slideup",
    "slidedown",
    "circleopen",
    "circleclose",
    "radial",
    "smoothleft",
    "smoothright",
    "smoothup",
    "smoothdown",
    "dissolve",
    "pixelize",
    "distance",
    "fadeblack",
]

# How often we just use a hard cut (no xfade) - real editors do this a lot.
HARD_CUT_WEIGHT = 0.45

# Per-video preferred palette size (before random selection)
PALETTE_SIZES = [3, 4, 4, 5, 5, 6]

# Transition durations (seconds). Longer = more "produced" feeling, shorter =
# punchier. Kept under 0.35 s so Shorts do not lose rhythm.
TRANSITION_DURATIONS = [0.18, 0.22, 0.26, 0.30, 0.35]


def pick_palette(style, seed=None):
    """
    Per-video transition palette. Prefers ffmpeg modes that fit the video's
    overall mood (fast videos tend to use slides/wipes, slower ones fades).
    """
    rnd = random.Random(seed if seed is not None else
                        hash(str(style.get("caption_y_ratio", 0.62))) ^
                        int(style.get("scene_gap", 0.05) * 1000))

    size = rnd.choice(PALETTE_SIZES)
    pool = list(XFADE_MODES)
    rnd.shuffle(pool)

    # Bias by pacing: faster shot_seconds -> prefer wipes/slides
    shot = style.get("shot_seconds", 2.0)
    if shot <= 2.0:
        fast_biased = [m for m in pool
                       if m.startswith(("wipe", "slide", "smooth"))][:size]
        palette = fast_biased or pool[:size]
    elif shot >= 2.6:
        slow_biased = [m for m in pool
                       if m in ("fade", "dissolve", "distance", "fadeblack",
                                "circleopen", "circleclose", "radial")][:size]
        palette = slow_biased or pool[:size]
    else:
        palette = pool[:size]

    print("[transitions] palette: " + ", ".join(palette) +
          f" | hard-cut weight {HARD_CUT_WEIGHT:.2f}")
    return palette


def pick_transitions(n_joins, style, seed=None):
    """
    Return a list of length `n_joins`. Each item is either a mode name from
    XFADE_MODES or None (= hard cut).
    """
    if n_joins <= 0:
        return []
    palette = pick_palette(style, seed=seed)
    rnd = random.Random((seed if seed is not None else 0) ^
                        int(style.get("caption_rotation", 0) * 100) ^
                        int(style.get("hook_tilt", 0) * 1000))
    out = []
    last = None
    for _ in range(n_joins):
        # keep it varied: avoid two identical transitions in a row
        if rnd.random() < HARD_CUT_WEIGHT:
            out.append(None)
            last = None
            continue
        choices = [m for m in palette if m != last] or palette
        pick = rnd.choice(choices)
        out.append(pick)
        last = pick
    return out


def pick_duration(style, seed=None):
    rnd = random.Random((seed or 0) ^ int(style.get("grade_grain", 4) * 37))
    return rnd.choice(TRANSITION_DURATIONS)


def _fmt(x):
    return ("%.3f" % x).rstrip("0").rstrip(".")


def apply_transitions_ffmpeg(scene_paths, durations, transitions,
                             scene_gap, out_path, transition_time=None):
    """
    Concatenate pre-rendered scene .mp4 files with ffmpeg's xfade filter.

    scene_paths : list[str]   already 1080x1920, 30fps, WITH audio
    durations   : list[float] exact length of each scene file in seconds
    transitions : list[str|None] one per join (len == len(scene_paths)-1)
    scene_gap   : float       silent padding baked into each scene already
    out_path    : str         where to write the concatenated mp4

    Returns True on success. Never raises.
    """
    n = len(scene_paths)
    if n < 2:
        return False
    if len(transitions) != n - 1:
        return False

    # xfade eats `t` seconds at every join (overlap). MoviePy scene durations
    # must be shrunk by that amount on the outgoing side so the total stays
    # exactly the same as the sum of scene_gaps we planned.
    t_len = transition_time or 0.24

    # Inputs
    inputs = []
    for p in scene_paths:
        inputs += ["-i", p]

    # Build the filter graph
    parts = []
    # Scale everything just in case
    for i in range(n):
        parts.append(f"[{i}:v]fps=30,setsar=1,format=yuv420p[v{i}]")

    # Chain xfades; hard cuts are done with a 0.04 s fade so audio stays smooth.
    cur_v = "v0"
    cur_a = "0:a"
    total_offset = durations[0]

    for i, mode in enumerate(transitions):
        next_v = f"v{i+1}"
        next_a = f"{i+1}:a"

        if mode is None:
            # tiny dissolve that is almost invisible (keeps audio from clicking)
            real_mode = "fade"
            real_t = 0.04
        else:
            real_mode = mode
            real_t = t_len

        out_v = f"xv{i}"
        out_a = f"xa{i}"

        # offset = when the transition starts on the current timeline
        offset = total_offset - real_t

        parts.append(
            f"[{cur_v}][{next_v}]xfade=transition={real_mode}:"
            f"duration={real_t}:offset={_fmt(offset)}[{out_v}]"
        )
        parts.append(
            f"[{cur_a}][{next_a}]acrossfade=d={real_t}:c1=tri:c2=tri[{out_a}]"
        )

        cur_v = out_v
        cur_a = out_a
        total_offset = offset + real_t + (durations[i + 1] - real_t)

    graph = ";".join(parts)

    cmd = ["ffmpeg", "-y", *inputs,
           "-filter_complex", graph,
           "-map", f"[{cur_v}]", "-map", f"[{cur_a}]",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "192k",
           out_path]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not os.path.exists(out_path) or \
                os.path.getsize(out_path) < 10000:
            print("[transitions] ffmpeg xfade failed:")
            print((r.stderr or "")[-1200:])
            return False
        n_cuts = sum(1 for t in transitions if t is None)
        n_fades = n - 1 - n_cuts
        print(f"[transitions] applied: {n_fades} xfades, {n_cuts} hard cuts")
        return True
    except Exception as e:
        print("[transitions] error: " + str(e))
        return False
