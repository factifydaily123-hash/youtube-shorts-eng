"""
Pro FX stage (pure ffmpeg - fast, deterministic, no MoviePy per-frame python).

Runs on the BASE video (stock clips + audio) BEFORE captions / hook card are
composited on top, so text stays rock-steady and sharp while the picture "moves".

What it adds (all synced to the real cut times):
  HOOK (first ~0.4 s)  fast push-in (1.37x -> 1.05x), RGB-split glitch, camera shake
                       -> the very first frames already feel edited, not a static stock clip
  EVERY CUT            punch-in kick that settles, short camera shake, 2-frame whip blur,
                       RGB-split flicker, and a soft flash on every 2nd cut
  LOOK                 soft bloom/glow on highlights, thin progress bar on top
                       (the colour grade / vignette / grain is applied later in composer._grade_pass)

Everything is controlled by env vars (see bottom constants) so it can be dialled up/down
from the GitHub workflow without touching code.
"""

import os
import subprocess

ENABLED = os.getenv("PRO_FX", "1") == "1"

W, H, FPS = 1080, 1920, 30

BASE_ZOOM = float(os.getenv("FX_BASE_ZOOM", "1.05"))     # constant headroom for shake/pan
HOOK_PUSH = float(os.getenv("FX_HOOK_PUSH", "0.32"))     # extra zoom at t=0, decays in ~0.2 s
CUT_KICK = float(os.getenv("FX_CUT_KICK", "0.09"))       # extra zoom at every cut
SHAKE_PX = float(os.getenv("FX_SHAKE_PX", "14"))         # camera-shake amplitude (px)
PROGRESS_BAR = os.getenv("FX_BAR", "1") == "1"


def _fmt(x):
    return ("%.3f" % x).rstrip("0").rstrip(".")


def _between_sum(windows):
    """ffmpeg 'enable' expression that is >0 inside any (a, b) window."""
    if not windows:
        return "0"
    return "+".join("between(t,%s,%s)" % (_fmt(a), _fmt(b)) for a, b in windows)


def build_filtergraph(cuts, duration, hook=True):
    """
    cuts: sorted absolute times (s) where the picture changes.
    Returns the filter_complex string (input [0:v] -> output [v]).
    """
    cuts = [c for c in sorted(set(round(c, 3) for c in cuts)) if 0.25 < c < duration - 0.15]

    # ---- zoom + shake expressions (zoompan: `it` = input timestamp) ----------
    z = [_fmt(BASE_ZOOM)]
    sx, sy = [], []
    if hook:
        z.append("%s*exp(-it/0.2)" % _fmt(HOOK_PUSH))
        sx.append("if(lt(it,0.5),%s*exp(-it/0.1)*sin(70*it),0)" % _fmt(SHAKE_PX * 0.8))
        sy.append("if(lt(it,0.5),%s*exp(-it/0.1)*cos(83*it),0)" % _fmt(SHAKE_PX * 0.8))
    for c in cuts:
        cc = _fmt(c)
        z.append("if(gte(it,%s),%s*exp(-(it-%s)/0.14),0)" % (cc, _fmt(CUT_KICK), cc))
        sx.append("if(between(it,%s,%s+0.4),%s*exp(-(it-%s)/0.09)*sin(62*(it-%s)),0)"
                  % (cc, cc, _fmt(SHAKE_PX), cc, cc))
        sy.append("if(between(it,%s,%s+0.4),%s*exp(-(it-%s)/0.09)*cos(75*(it-%s)),0)"
                  % (cc, cc, _fmt(SHAKE_PX * 0.7), cc, cc))
    z_expr = "+".join(z)
    x_expr = "iw/2-(iw/zoom/2)" + "".join("+" + t for t in sx)
    y_expr = "ih/2-(ih/zoom/2)" + "".join("+" + t for t in sy)

    # ---- timed windows -------------------------------------------------------
    glitch = ([(0.04, 0.30)] if hook else []) + [(c, c + 0.10) for c in cuts]
    blur = [(max(0.0, c - 0.035), c + 0.045) for c in cuts]
    flash = [(c + 0.0, c + 0.06) for i, c in enumerate(cuts) if i % 2 == 1]

    g = []
    g.append("[0:v]zoompan=z='%s':x='%s':y='%s':d=1:s=%dx%d:fps=%d,setsar=1[z]"
             % (z_expr, x_expr, y_expr, W, H, FPS))
    last = "z"
    if glitch:
        g.append("[%s]rgbashift=rh=-9:bh=9:gv=3:edge=smear:enable='%s'[g]"
                 % (last, _between_sum(glitch)))
        last = "g"
    if blur:
        g.append("[%s]gblur=sigma=12:enable='%s'[b]" % (last, _between_sum(blur)))
        last = "b"
    if flash:
        g.append("[%s]eq=brightness=0.20:enable='%s'[f]" % (last, _between_sum(flash)))
        last = "f"
    # soft bloom: blurred copy screened over the image at low opacity
    g.append("[%s]split[bl1][bl2];[bl2]gblur=sigma=24[bl3];"
             "[bl1][bl3]blend=all_mode=screen:all_opacity=0.14[bloom]" % last)
    last = "bloom"
    if PROGRESS_BAR:
        g.append("color=c=0xFFD400:s=%dx8:r=%d:d=%s[bar]" % (W, FPS, _fmt(duration + 0.5)))
        g.append("[%s][bar]overlay=x='-W+W*t/%s':y=0:shortest=1[v]" % (last, _fmt(duration)))
    else:
        g.append("[%s]null[v]" % last)
    return ";".join(g)


def apply_pro_fx(src, dst, cuts, duration, hook=True):
    """
    src -> dst (.mov/.mp4). Returns True on success. Never raises: on any problem the
    caller just keeps the un-effected base video.
    """
    if not ENABLED:
        return False
    script = dst + ".filter.txt"
    try:
        with open(script, "w") as f:
            f.write(build_filtergraph(cuts, duration, hook=hook))
        cmd = ["ffmpeg", "-y", "-i", src, "-filter_complex_script", script,
               "-map", "[v]", "-map", "0:a?",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
               "-pix_fmt", "yuv420p", "-c:a", "copy", dst]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 10000:
            print("Pro FX failed: " + (r.stderr or "")[-800:])
            return False
        print("Pro FX applied (%d cut effects)" % len(cuts))
        return True
    except Exception as e:
        print("Pro FX error: " + str(e))
        return False
    finally:
        if os.path.exists(script):
            try:
                os.remove(script)
            except Exception:
                pass
