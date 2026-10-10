"""
Pro FX stage (pure ffmpeg - fast, deterministic, no MoviePy per-frame python).
STYLE-AWARE version.

Runs on the BASE video (stock clips + audio) BEFORE captions / hook card are
composited on top, so text stays rock-steady and sharp while the picture "moves".

Every amount (hook push, cut kick, shake) is randomized via the style dict so
each video feels hand-edited rather than generated from a fixed template.
"""

import os
import subprocess

ENABLED = os.getenv("PRO_FX", "1") == "1"

W, H, FPS = 1080, 1920, 30

# defaults (overridden per video by style dict)
DEFAULT_BASE_ZOOM = float(os.getenv("FX_BASE_ZOOM", "1.05"))
DEFAULT_HOOK_PUSH = float(os.getenv("FX_HOOK_PUSH", "0.32"))
DEFAULT_CUT_KICK = float(os.getenv("FX_CUT_KICK", "0.09"))
DEFAULT_SHAKE_PX = float(os.getenv("FX_SHAKE_PX", "14"))
PROGRESS_BAR = os.getenv("FX_BAR", "1") == "1"


def _fmt(x):
    return ("%.3f" % x).rstrip("0").rstrip(".")


def _between_sum(windows):
    if not windows:
        return "0"
    return "+".join("between(t,%s,%s)" % (_fmt(a), _fmt(b)) for a, b in windows)


def build_filtergraph(cuts, duration, hook=True, style=None):
    style = style or {}
    base_zoom = style.get("fx_base_zoom", DEFAULT_BASE_ZOOM)
    hook_push = style.get("fx_hook_push", DEFAULT_HOOK_PUSH)
    cut_kick = style.get("fx_cut_kick", DEFAULT_CUT_KICK)
    shake_px = style.get("fx_shake_px", DEFAULT_SHAKE_PX)

    cuts = [c for c in sorted(set(round(c, 3) for c in cuts)) if 0.25 < c < duration - 0.15]

    z = [_fmt(base_zoom)]
    sx, sy = [], []
    if hook:
        z.append("%s*exp(-it/0.2)" % _fmt(hook_push))
        sx.append("if(lt(it,0.5),%s*exp(-it/0.1)*sin(70*it),0)" % _fmt(shake_px * 0.8))
        sy.append("if(lt(it,0.5),%s*exp(-it/0.1)*cos(83*it),0)" % _fmt(shake_px * 0.8))
    for c in cuts:
        cc = _fmt(c)
        z.append("if(gte(it,%s),%s*exp(-(it-%s)/0.14),0)" % (cc, _fmt(cut_kick), cc))
        sx.append("if(between(it,%s,%s+0.4),%s*exp(-(it-%s)/0.09)*sin(62*(it-%s)),0)"
                  % (cc, cc, _fmt(shake_px), cc, cc))
        sy.append("if(between(it,%s,%s+0.4),%s*exp(-(it-%s)/0.09)*cos(75*(it-%s)),0)"
                  % (cc, cc, _fmt(shake_px * 0.7), cc, cc))
    z_expr = "+".join(z)
    x_expr = "iw/2-(iw/zoom/2)" + "".join("+" + t for t in sx)
    y_expr = "ih/2-(ih/zoom/2)" + "".join("+" + t for t in sy)

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
    g.append("[%s]split[bl1][bl2];[bl2]gblur=sigma=24[bl3];"
             "[bl1][bl3]blend=all_mode=screen:all_opacity=0.14[bloom]" % last)
    last = "bloom"
    if PROGRESS_BAR:
        g.append("color=c=0xFFD400:s=%dx8:r=%d:d=%s[bar]" % (W, FPS, _fmt(duration + 0.5)))
        g.append("[%s][bar]overlay=x='-W+W*t/%s':y=0:shortest=1[v]" % (last, _fmt(duration)))
    else:
        g.append("[%s]null[v]" % last)
    return ";".join(g)


def apply_pro_fx(src, dst, cuts, duration, hook=True, style=None):
    """
    src -> dst (.mov/.mp4). Returns True on success. Never raises.
    `style` may include fx_base_zoom, fx_hook_push, fx_cut_kick, fx_shake_px.
    """
    if not ENABLED:
        return False
    script = dst + ".filter.txt"
    try:
        with open(script, "w") as f:
            f.write(build_filtergraph(cuts, duration, hook=hook, style=style))
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
