"""
Video composer - MoviePy + FFmpeg. STYLE + TRANSITION AWARE version.

Every render gets a "style" dict (modules.style_variation.get_style) that
randomizes caption position, hook card position/tilt/colour, end-question
position, scene rhythm, SFX gains and the final colour grade. Scene joins are
randomized too (modules.transitions) so the video never uses the same
transition palette twice. This makes each video look hand-edited rather than
produced from a fixed template.

Features:
  - Big bold hook card (first 3 s) - position/colour/duration randomized per video
  - Word-by-word captions (via modules.captions) - position/rotation randomized
  - Slow punch-in zoom on every scene (+12% over duration)
  - Pro audio mix (voice EQ + synthesized SFX + ducked BGM + loudnorm)
  - Randomized xfade transitions between scenes (wipe/slide/fade/circle/etc.)
  - Final ffmpeg colour pass (grade / vignette / grain) - parameters randomized
"""

import filecmp
import math
import os
import random
import subprocess
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from moviepy.editor import (
    VideoFileClip, AudioFileClip, CompositeAudioClip,
    concatenate_audioclips, concatenate_videoclips, vfx,
    ImageClip, CompositeVideoClip,
)
import moviepy.audio.fx.all as afx

from modules.audio import build_final_audio, INTER_SCENE_PAUSE, get_duration
from modules import pro_fx
from modules.style_variation import get_style
from modules import transitions as transitions_mod

TARGET_W = 1080
TARGET_H = 1920

BG_MUSIC_VOLUME = 0.15
SCENE_GAP = INTER_SCENE_PAUSE

# Caption settings (PIL based, no ImageMagick needed)
CAPTION_FONT_SIZE = 72
CAPTION_POSITION_RATIO = 0.55
CAPTION_FADE = 0.10
CAPTION_MAX_WIDTH = TARGET_W - 100

# CTA settings
CTA_TEXT = "Follow for more"
CTA_FONT_SIZE = 58
CTA_POSITION_RATIO = 0.85
CTA_START_RATIO = 0.55
CTA_FADE_DURATION = 0.5

# Pacing defaults (real value comes from style["shot_seconds"]).
JUMP_CUT_TARGET = 2.4
MAX_SHOTS = 3
MIN_SHOT = 1.6
PUNCH_STEP = 0.09

# Shot entry effects
ENTRY_POP = 0.14
ENTRY_SHAKE_PX = 14
SHOT_BASE_SCALE = 1.08
SHOT_DRIFT_PX = 36
ENTRY_CHOICES = ["pop", "pop", "flash", "shake", None]

# End question defaults (style overrides per video)
END_Q_SHOW = 1.7
END_Q_Y_RATIO = 0.76
END_Q_FONT_SIZE = 70

# Hook card defaults (style overrides per video)
HOOK_CARD_DURATION = 3.0
HOOK_CARD_Y_RATIO = 0.20
HOOK_CARD_FONT_SIZE = 108


class ShortsComposer:
    def __init__(self, output_dir="output"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    @staticmethod
    def _get_font_file():
        font_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "assets/fonts/DejaVuSans-Bold.ttf",
            "assets/fonts/NotoSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
        for f in font_candidates:
            if os.path.exists(f):
                print("Font found: " + f)
                return f
        try:
            import subprocess
            result = subprocess.run(
                ["fc-match", "-f", "%{file}", "sans:bold"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                path = result.stdout.strip()
                if os.path.exists(path):
                    print("Font via fc-match: " + path)
                    return path
        except Exception as e:
            print("fc-match failed: " + str(e))
        print("Koi bhi font nahi mila!")
        return None

    @staticmethod
    def _fit_vertical(clip):
        if clip.w / clip.h > TARGET_W / TARGET_H:
            clip = clip.resize(height=TARGET_H)
            clip = clip.crop(x_center=clip.w / 2, width=TARGET_W)
        else:
            clip = clip.resize(width=TARGET_W)
            clip = clip.crop(y_center=clip.h / 2, height=TARGET_H)
        if (clip.w, clip.h) != (TARGET_W, TARGET_H):
            clip = clip.resize((TARGET_W, TARGET_H))
        return clip

    @staticmethod
    def _shock_grade(clip):
        """High-contrast, saturated look for the very first scene (visual hook)."""
        from PIL import ImageEnhance

        def grade(frame):
            img = Image.fromarray(frame)
            img = ImageEnhance.Contrast(img).enhance(1.22)
            img = ImageEnhance.Color(img).enhance(1.28)
            img = ImageEnhance.Brightness(img).enhance(1.04)
            return np.array(img)

        return clip.fl_image(grade)

    @staticmethod
    def _cut_points(duration):
        """Times (inside a scene) where a jump cut happens."""
        n = int(round(duration / JUMP_CUT_TARGET))
        n = max(1, min(MAX_SHOTS, n))
        while n > 1 and duration / n < MIN_SHOT:
            n -= 1
        return [round(duration * i / n, 3) for i in range(1, n)]

    @staticmethod
    def _pick_entry():
        if pro_fx.ENABLED:
            return None
        return random.choice(ENTRY_CHOICES)

    @staticmethod
    def _zoomed(fitted, duration, gain, zoom_out=False, start_scale=1.0,
                cuts=None, punch_from_end=False, entry=None, drift=0.0):
        d = max(duration, 0.5)
        cuts = sorted(cuts or [])
        n_seg = len(cuts) + 1
        has_room = start_scale >= 1.07 and not zoom_out
        if entry == "shake" and not has_room:
            entry = "pop"
        if not has_room:
            drift = 0.0

        def snap(t):
            if not cuts:
                return 0.0
            k = sum(1 for c in cuts if t >= c)
            if punch_from_end:
                on = (k % 2) != ((n_seg - 1) % 2)
            else:
                on = (k % 2) == 1
            return PUNCH_STEP if on else 0.0

        def scale(t):
            if zoom_out:
                s = start_scale + gain * (1.0 - min(t, d) / d) + snap(t)
            else:
                s = start_scale + gain * min(t, d) / d + snap(t)
            if entry == "pop":
                s += ENTRY_POP * math.exp(-t / 0.09)
            return s

        zoomed = fitted.resize(scale)

        if entry is None and not drift:
            zoomed = zoomed.set_position("center")
        else:
            sign = random.choice([-1, 1])

            def pos(t):
                s = scale(t)
                dx = sign * drift * (min(t, d) / d - 0.5)
                dy = 0.0
                if entry == "shake" and t < 0.45:
                    e = math.exp(-t / 0.09)
                    dx += ENTRY_SHAKE_PX * e * math.sin(2 * math.pi * 26 * t)
                    dy += ENTRY_SHAKE_PX * e * math.cos(2 * math.pi * 31 * t)
                return (int(round(TARGET_W * (1 - s) / 2 + dx)),
                        int(round(TARGET_H * (1 - s) / 2 + dy)))

            zoomed = zoomed.set_position(pos)

        composed = CompositeVideoClip([zoomed], size=(TARGET_W, TARGET_H))

        if entry == "flash":
            def _flash(gf, t):
                f = gf(t)
                if t > 0.3:
                    return f
                a = 0.6 * math.exp(-t / 0.06)
                return (f + (255.0 - f.astype(np.float32)) * a).astype(np.uint8)
            composed = composed.fl(_flash)

        return composed.set_duration(duration)

    @staticmethod
    def _prepare_scene_video(path, duration, shock=False, in_point=None,
                             zoom_out=False, split=False, cut_log=None,
                             punch_from_end=False, entry=None, drift=0.0,
                             base_scale=1.0):
        try:
            clip = VideoFileClip(path, audio=False)
        except Exception as e:
            raise RuntimeError("VideoFileClip fail: " + path + ": " + str(e))

        if clip.duration is None or clip.duration <= 0:
            clip.close()
            raise RuntimeError("Clip duration invalid: " + path)

        src = float(clip.duration)
        pinned = False
        multi_shots = None
        cuts = ShortsComposer._cut_points(duration)

        if in_point is not None and src >= in_point + duration + 0.03:
            clip = clip.subclip(in_point, in_point + duration)
            pinned = True
        elif src < duration + 0.2:
            try:
                clip = clip.fx(vfx.loop, duration=duration + 0.5)
            except Exception as e:
                clip.close()
                raise RuntimeError("Loop fail: " + path + ": " + str(e))
        elif split and not shock and duration >= 2.4 and src >= duration + 0.3 and cuts:
            bounds = [0.0] + list(cuts) + [duration]
            lens = [bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1)]
            try:
                seg = src / len(lens)
                shots = []
                for i, ln in enumerate(lens):
                    lo = i * seg
                    hi = min(src - ln - 0.02, (i + 1) * seg - ln)
                    if hi > lo:
                        st = random.uniform(lo, hi)
                    else:
                        st = max(0.0, min(lo, src - ln - 0.02))
                    shots.append((clip.subclip(st, min(src, st + ln)), ln))
                multi_shots = shots
            except Exception as e:
                print("Split skipped: " + str(e))
                multi_shots = None
            if multi_shots is None:
                spare = max(0.0, src - duration - 0.2)
                start = random.uniform(0, spare) if spare > 0.1 else 0.0
                clip = clip.subclip(start, min(src, start + duration))
        else:
            spare = max(0.0, src - duration - 0.2)
            start = random.uniform(0, spare) if spare > 0.1 else 0.0
            end = start + duration
            if end > src:
                end = src
                start = max(0.0, end - duration)
            try:
                clip = clip.subclip(start, end)
            except Exception as e:
                clip.close()
                raise RuntimeError("Subclip fail: " + path + ": " + str(e))

        if multi_shots is not None:
            try:
                parts = []
                for i, (sub, ln) in enumerate(multi_shots):
                    fit = ShortsComposer._fit_vertical(sub)
                    if i % 2 == 1:
                        try:
                            fit = fit.fx(vfx.mirror_x)
                        except Exception:
                            pass
                    parts.append(ShortsComposer._zoomed(
                        fit, ln, 0.10, start_scale=(1.0 if i % 2 == 0 else 1.06),
                        entry=(entry if i == 0 else "pop")))
                both = concatenate_videoclips(parts, method="chain")
                if cut_log is not None:
                    cut_log.extend(cuts)
                return both.set_duration(duration)
            except Exception as e:
                print("Multi-shot build failed, using single shot: " + str(e))
                spare = max(0.0, src - duration - 0.2)
                start = random.uniform(0, spare) if spare > 0.1 else 0.0
                clip = VideoFileClip(path, audio=False).subclip(start, min(src, start + duration))

        fitted = ShortsComposer._fit_vertical(clip)

        zoom_gain = 0.12 if base_scale <= 1.0 else 0.08
        if shock:
            try:
                fitted = ShortsComposer._shock_grade(fitted)
                zoom_gain = 0.20
            except Exception as e:
                print("Shock grade skipped: " + str(e))

        try:
            out = ShortsComposer._zoomed(fitted, duration, zoom_gain, zoom_out=zoom_out,
                                         start_scale=base_scale, cuts=cuts,
                                         punch_from_end=punch_from_end,
                                         entry=entry, drift=drift)
            if cut_log is not None:
                cut_log.extend(cuts)
            return out
        except Exception:
            return fitted.set_duration(duration)

    def _build_multi_clip_scene(self, paths, duration, cut_log=None):
        n = len(paths)
        each = duration / n
        parts = []
        for i, p in enumerate(paths):
            ln = duration - each * (n - 1) if i == n - 1 else each
            try:
                part = self._prepare_scene_video(
                    p, ln, split=False, entry=self._pick_entry(),
                    drift=SHOT_DRIFT_PX, base_scale=SHOT_BASE_SCALE)
            except Exception as e:
                print("Shot %d failed (%s) - re-using the scene's first clip" % (i + 1, e))
                part = self._prepare_scene_video(
                    paths[0], ln, split=False, entry="pop",
                    drift=SHOT_DRIFT_PX, base_scale=SHOT_BASE_SCALE)
            parts.append(part)
            if i > 0 and cut_log is not None:
                cut_log.append(round(each * i, 3))
        return concatenate_videoclips(parts, method="chain").set_duration(duration)

    # ============================================================
    # Hook card (STYLE-AWARE)
    # ============================================================
    def _make_hook_card(self, hook_text, duration=None, style=None):
        style = style or {}
        duration = duration or style.get("hook_duration", HOOK_CARD_DURATION)
        try:
            font_file = self._get_font_file()
            if not font_file or not hook_text:
                return None

            text = " ".join(str(hook_text).split()).upper()
            if not text:
                return None
            stroke = 10
            max_w = TARGET_W - 150
            dummy = ImageDraw.Draw(Image.new("RGBA", (10, 10)))

            def wrap(font):
                lines, cur = [], ""
                for word in text.split():
                    trial = (cur + " " + word).strip()
                    w = dummy.textbbox((0, 0), trial, font=font, stroke_width=stroke)
                    if (w[2] - w[0]) <= max_w or not cur:
                        cur = trial
                    else:
                        lines.append(cur)
                        cur = word
                if cur:
                    lines.append(cur)
                return lines

            font_size = style.get("hook_font_size", HOOK_CARD_FONT_SIZE)
            font, lines = None, []
            for size in range(int(font_size), 55, -8):
                try:
                    f = ImageFont.truetype(font_file, size)
                except Exception:
                    return None
                ls = wrap(f)
                if len(ls) <= 3:
                    font, lines = f, ls
                    break
            if font is None:
                font = ImageFont.truetype(font_file, 60)
                lines = wrap(font)[:3]

            boxes = [dummy.textbbox((0, 0), ln, font=font, stroke_width=stroke) for ln in lines]
            widths = [b[2] - b[0] for b in boxes]
            heights = [b[3] - b[1] for b in boxes]
            gap, pad = 26, 56
            img_w = min(TARGET_W - 40, max(widths) + pad * 2)
            img_h = sum(heights) + gap * (len(lines) - 1) + pad * 2

            fill = style.get("hook_fill", (0, 0, 0, 200))
            text_color = style.get("hook_text_color", (255, 235, 59, 255))
            radius = style.get("hook_radius", 36)

            img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            if fill[3] > 0:
                draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1],
                                       radius=radius, fill=fill)
            else:
                # border-only card variant
                draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1],
                                       radius=radius, outline=(255, 255, 255, 230),
                                       width=6)
            y = pad
            for i, ln in enumerate(lines):
                x = (img_w - widths[i]) // 2 - boxes[i][0]
                draw.text((x, y - boxes[i][1]), ln, font=font,
                          fill=text_color, stroke_width=stroke,
                          stroke_fill=(0, 0, 0, 255))
                y += heights[i] + gap

            y0 = int(TARGET_H * style.get("hook_y_ratio", HOOK_CARD_Y_RATIO))
            clip = ImageClip(np.array(img), transparent=True).set_duration(duration).set_start(0)
            clip = clip.set_position(("center", y0))
            tilt = style.get("hook_tilt", 0)
            if tilt:
                try:
                    clip = clip.rotate(tilt, resample="bilinear", expand=False)
                except Exception:
                    pass
            try:
                clip = clip.crossfadeout(0.25)
            except Exception:
                pass
            return clip
        except Exception as e:
            print("Hook card build failed: " + str(e))
            return None

    # ============================================================
    # End question (STYLE-AWARE)
    # ============================================================
    def _make_end_question_overlay(self, text, total_duration, font_file, style=None):
        style = style or {}
        try:
            text = " ".join(str(text or "").split()).upper()
            if not text or not font_file or total_duration < 3:
                return None
            stroke = 6
            max_w = TARGET_W - 200
            dummy = ImageDraw.Draw(Image.new("RGBA", (10, 10)))

            font, lines = None, []
            for size in range(END_Q_FONT_SIZE, 41, -6):
                f = ImageFont.truetype(font_file, size)
                cur, ls = "", []
                for word in text.split():
                    trial = (cur + " " + word).strip()
                    b = dummy.textbbox((0, 0), trial, font=f, stroke_width=stroke)
                    if (b[2] - b[0]) <= max_w or not cur:
                        cur = trial
                    else:
                        ls.append(cur)
                        cur = word
                if cur:
                    ls.append(cur)
                if len(ls) <= 2:
                    font, lines = f, ls
                    break
            if font is None:
                return None

            boxes = [dummy.textbbox((0, 0), ln, font=font, stroke_width=stroke) for ln in lines]
            widths = [b[2] - b[0] for b in boxes]
            heights = [b[3] - b[1] for b in boxes]
            gap, pad_x, pad_y = 18, 48, 34
            img_w = min(TARGET_W - 40, max(widths) + pad_x * 2)
            img_h = sum(heights) + gap * (len(lines) - 1) + pad_y * 2

            img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=40, fill=(0, 0, 0, 205))
            y = pad_y
            for i, ln in enumerate(lines):
                x = (img_w - widths[i]) // 2 - boxes[i][0]
                draw.text((x, y - boxes[i][1]), ln, font=font,
                          fill=(255, 221, 0, 255), stroke_width=stroke,
                          stroke_fill=(0, 0, 0, 255))
                y += heights[i] + gap

            show = style.get("end_q_duration", END_Q_SHOW)
            start = max(0.0, total_duration - show)
            y_ratio = style.get("end_q_y_ratio", END_Q_Y_RATIO)

            clip = ImageClip(np.array(img), transparent=True)
            clip = clip.set_start(start).set_duration(total_duration - start)
            clip = clip.set_position(("center", int(TARGET_H * y_ratio)))
            try:
                clip = clip.crossfadein(0.15)
            except Exception:
                pass
            print("End question added: " + text)
            return clip
        except Exception as e:
            print("End question overlay failed: " + str(e))
            return None

    # ============================================================
    # Legacy caption/CTA helpers
    # ============================================================
    @staticmethod
    def _make_caption_png(text, font_file, font_size=CAPTION_FONT_SIZE):
        try:
            if not text or not text.strip() or not font_file:
                return None

            display_text = text.strip().upper()
            if len(display_text) > 55:
                display_text = display_text[:52] + "..."

            words = display_text.split()
            if len(words) > 5:
                mid = len(words) // 2
                line1 = " ".join(words[:mid])
                line2 = " ".join(words[mid:])
                lines = [line1, line2]
            else:
                lines = [display_text]

            try:
                font = ImageFont.truetype(font_file, font_size)
            except Exception as e:
                print("PIL font load fail: " + str(e))
                font = ImageFont.load_default()

            dummy_img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
            dummy_draw = ImageDraw.Draw(dummy_img)
            line_heights, line_widths = [], []
            for line in lines:
                bbox = dummy_draw.textbbox((0, 0), line, font=font, stroke_width=6)
                line_widths.append(bbox[2] - bbox[0])
                line_heights.append(bbox[3] - bbox[1])

            max_width = max(line_widths) if line_widths else 0
            total_height = sum(line_heights) + (len(lines) - 1) * 15

            pad_x, pad_y = 40, 30
            img_w = max_width + pad_x * 2
            img_h = total_height + pad_y * 2

            img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            y_offset = pad_y
            for i, line in enumerate(lines):
                bbox = draw.textbbox((0, 0), line, font=font, stroke_width=6)
                line_w = bbox[2] - bbox[0]
                x = (img_w - line_w) // 2
                draw.text(
                    (x, y_offset), line, font=font,
                    fill=(255, 255, 255, 255),
                    stroke_width=6,
                    stroke_fill=(0, 0, 0, 255),
                )
                y_offset += line_heights[i] + 15

            return img
        except Exception as e:
            print("Caption PNG creation error: " + str(e))
            return None

    def _make_caption_overlay(self, text, start_time, duration, font_file):
        try:
            png_img = self._make_caption_png(text, font_file)
            if png_img is None:
                return None
            img_array = np.array(png_img)
            caption_clip = ImageClip(img_array, transparent=True)
            caption_clip = caption_clip.set_duration(duration).set_start(start_time)
            caption_clip = caption_clip.set_position(
                ("center", int(TARGET_H * CAPTION_POSITION_RATIO))
            )
            caption_clip = caption_clip.crossfadein(CAPTION_FADE).crossfadeout(CAPTION_FADE)
            caption_clip = caption_clip.set_opacity(1.0)
            return caption_clip
        except Exception as e:
            print("Caption overlay error: " + str(e))
            return None

    def _make_cta_overlay(self, total_duration, font_file):
        try:
            if not font_file:
                return None
            png_img = self._make_caption_png(CTA_TEXT, font_file, font_size=CTA_FONT_SIZE)
            if png_img is None:
                return None
            img_array = np.array(png_img)
            cta_clip = ImageClip(img_array, transparent=True)
            cta_clip = cta_clip.set_duration(total_duration)
            cta_clip = cta_clip.set_position(
                ("center", int(TARGET_H * CTA_POSITION_RATIO))
            )
            start_time = total_duration * CTA_START_RATIO
            cta_clip = cta_clip.set_start(start_time)
            cta_clip = cta_clip.crossfadein(CTA_FADE_DURATION).set_opacity(1.0)
            return cta_clip
        except Exception as e:
            print("CTA error: " + str(e))
            return None

    # ============================================================
    # Fallback audio mixer
    # ============================================================
    def _add_background_music(self, voice_audio, total_duration, bg_music_path):
        audio_tracks = [voice_audio]
        bg_music = None
        try:
            if bg_music_path and os.path.exists(bg_music_path):
                bg_music_raw = AudioFileClip(bg_music_path)
                if bg_music_raw.duration is None or bg_music_raw.duration <= 0:
                    bg_music_raw.close()
                else:
                    bg_music = bg_music_raw
                    if bg_music.duration < total_duration:
                        loop_count = int(total_duration // bg_music.duration) + 2
                        bg_music = concatenate_audioclips([bg_music] * loop_count)
                    if bg_music.duration > total_duration:
                        bg_music = bg_music.subclip(0, total_duration)
                    bg_music = bg_music.volumex(BG_MUSIC_VOLUME)
                    bg_music = (
                        bg_music.fx(afx.audio_fadein, 0.5)
                        .fx(afx.audio_fadeout, 1.0)
                    )
                    audio_tracks.append(bg_music)
        except Exception as e:
            print("BG music error: " + str(e))
            bg_music = None
        return CompositeAudioClip(audio_tracks), bg_music

    def _grade_pass(self, src, dst, style=None):
        """Final ffmpeg pass: colour grade + vignette + light grain. STYLE-AWARE."""
        style = style or {}
        vf = (
            f"eq=contrast={style.get('grade_contrast', 1.06)}:"
            f"saturation={style.get('grade_saturation', 1.12)}:brightness=0.01,"
            "colorbalance=rs=0.02:bs=-0.02:rh=0.02:bh=-0.015,"
            "unsharp=5:5:0.5:5:5:0.0,"
            f"vignette=angle={style.get('vignette_angle', 'PI/6')},"
            f"noise=alls={style.get('grain_strength', 4)}:allf=t"
        )
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart",
               "-c:a", "copy", dst]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 10000:
            raise RuntimeError("grade pass failed: " + (r.stderr or "")[-600:])

    def _export(self, video_clip, output_filename, style=None):
        output_path = os.path.join(self.output_dir, output_filename)
        pre_path = output_path[:-4] + ".pre.mp4"
        video_clip.write_videofile(
            pre_path,
            codec="libx264",
            audio_codec="aac",
            audio_bitrate="192k",
            fps=30,
            preset="ultrafast",
            ffmpeg_params=["-pix_fmt", "yuv420p", "-crf", "17"],
            threads=4,
            temp_audiofile=os.path.join(self.output_dir, "temp_audio.m4a"),
            remove_temp=True,
        )
        try:
            self._grade_pass(pre_path, output_path, style=style)
            os.remove(pre_path)
            print("Colour grade applied")
        except Exception as e:
            print("Grade pass skipped: " + str(e))
            os.replace(pre_path, output_path)
        return output_path

    # ============================================================
    # MAIN - multi-scene Short builder (STYLE + TRANSITION AWARE)
    # ============================================================
    def create_multi_scene_short(self, clip_paths, voiceover_paths,
                                  output_filename="final_short.mp4",
                                  bg_music_path="bg_music.mp3",
                                  add_cta=True,
                                  scene_narrations=None,
                                  word_scenes=None,
                                  hook_text=None,
                                  word_timings=None,
                                  loop_visual=False,
                                  end_question=None,
                                  extra_clip_paths=None,
                                  style=None):
        # ---- style fingerprint for this video ----
        style = style or get_style()
        global SCENE_GAP
        SCENE_GAP = style.get("scene_gap", SCENE_GAP)

        print("Multi-scene composition START (style + transitions applied)")

        if not clip_paths or not voiceover_paths:
            raise ValueError("clip_paths ya voiceover_paths empty hain")

        count = min(len(clip_paths), len(voiceover_paths))
        print("Scenes: " + str(count))

        font_file = self._get_font_file()
        if not font_file:
            print("FONT NAHI MILA - Captions skip ho jayengi")
        else:
            print("Font ready: " + font_file)

        voice_clips = []
        video_scenes = []
        opened_audio = []
        opened_video = []
        timeline = 0.0
        scene_timings = []
        abs_cut_times = []

        # ---- SEAMLESS VISUAL LOOP PLAN ----
        loop_ok = False
        hook_in = None
        try:
            if loop_visual and count >= 3 \
                    and filecmp.cmp(clip_paths[0], clip_paths[count - 1], shallow=False):
                d_hook = get_duration(voiceover_paths[0]) + SCENE_GAP
                d_last = get_duration(voiceover_paths[count - 1]) + SCENE_GAP
                hook_in = d_last + 0.1
                src_len = get_duration(clip_paths[0])
                if d_hook > 0.5 and d_last > 0.5 and src_len >= hook_in + d_hook + 0.25:
                    loop_ok = True
        except Exception as e:
            print("Loop plan skipped: " + str(e))
        print("Visual loop: " + ("seamless (reverse lead-in)" if loop_ok else "plain re-use / off"))

        try:
            for index in range(count):
                clip_path = clip_paths[index]
                voice_path = voiceover_paths[index]

                try:
                    voice = AudioFileClip(voice_path)
                except Exception as e:
                    raise RuntimeError("Scene voice load fail: " + str(e))

                opened_audio.append(voice)

                if voice.duration is None or voice.duration <= 0.1:
                    raise RuntimeError("Scene voice invalid")

                scene_duration = voice.duration + SCENE_GAP
                scene_timings.append((timeline, voice.duration))

                is_last = (index == count - 1)
                cut_log = []
                extras = []
                if extra_clip_paths and index < len(extra_clip_paths):
                    extras = [x for x in (extra_clip_paths[index] or [])
                              if x and os.path.exists(x)]
                try:
                    if loop_ok and index == 0:
                        scene_video = self._prepare_scene_video(
                            clip_path, scene_duration, shock=True, in_point=hook_in,
                            cut_log=cut_log
                        )
                    elif loop_ok and is_last:
                        scene_video = self._prepare_scene_video(
                            clip_path, scene_duration, shock=True,
                            in_point=max(0.0, hook_in - scene_duration), zoom_out=True,
                            cut_log=cut_log, punch_from_end=True
                        )
                    elif extras and 0 < index < count - 1:
                        scene_video = self._build_multi_clip_scene(
                            [clip_path] + extras, scene_duration, cut_log=cut_log)
                    else:
                        scene_video = self._prepare_scene_video(
                            clip_path, scene_duration, shock=(index == 0),
                            split=(0 < index < count - 1), cut_log=cut_log,
                            entry=(None if index == 0 else self._pick_entry()),
                            drift=(0.0 if index == 0 else SHOT_DRIFT_PX),
                            base_scale=(1.0 if index == 0 else SHOT_BASE_SCALE),
                        )
                except Exception as e:
                    raise RuntimeError("Scene video fail: " + str(e))
                abs_cut_times.extend(timeline + c for c in cut_log)

                opened_video.append(scene_video)
                video_scenes.append(scene_video)
                voice_clips.append(voice.set_start(timeline))
                timeline += scene_duration

                print(f"Scene {index + 1} ready - dur={scene_duration:.2f}s")

            total_duration = timeline
            last_start, last_voice = scene_timings[-1]
            total_duration = min(timeline, last_start + last_voice + 0.06)
            print(f"TOTAL TIMELINE: {total_duration:.2f}s")
            if total_duration > 40:
                raise RuntimeError(
                    f"Timeline {total_duration:.0f}s too long for a Short "
                    "(voiceover bug?) - aborting before slow render")

            scene_sum = sum(float(s.duration or 0) for s in video_scenes)
            print(f"Safety: video_scenes sum = {scene_sum:.2f}s "
                  f"(expected {total_duration:.2f}s)")
            if abs(scene_sum - total_duration) > total_duration * 0.3:
                print("DURATION MISMATCH! Trimming scenes to exact length...")
                trimmed = []
                for i, (clip, (start_t, voice_d)) in enumerate(
                        zip(video_scenes, scene_timings)):
                    expected = voice_d + SCENE_GAP
                    if clip.duration and clip.duration > expected + 0.3:
                        print(f"   Scene {i+1}: {clip.duration:.2f}s "
                              f"-> {expected:.2f}s")
                        clip = clip.subclip(0, expected)
                    trimmed.append(clip)
                video_scenes = trimmed

            if not voice_clips:
                raise RuntimeError("No voice clips ready")

            # ---- SFX EVENTS (style-aware gains) ----
            sfx_events = [(t, "cut") for t in abs_cut_times]
            if hook_text or word_scenes:
                sfx_events.append((0.03, "click"))
            if end_question and total_duration >= 3:
                sfx_events.append((max(0.0, total_duration -
                                       style.get("end_q_duration", END_Q_SHOW)), "click"))
            if word_scenes:
                try:
                    from modules.captions import hero_word_times
                    sfx_events += [(t, "pop") for t in hero_word_times(
                        word_scenes, scene_timings, total_duration, word_timings,
                        style=style)]
                except Exception as e:
                    print("Caption pop SFX skipped: " + str(e))
            print("SFX events: " + str(len(sfx_events)) +
                  " (" + str(len(abs_cut_times)) + " jump cuts)")

            # ---- AUDIO MASTER ----
            try:
                master_path = build_final_audio(
                    voiceover_paths[:count],
                    scene_timings,
                    total_duration,
                    bg_music_path,
                    out_dir=os.path.join(self.output_dir, "mix"),
                    sfx_events=sfx_events,
                    style=style,
                )
                master_clip = AudioFileClip(master_path)
                opened_audio.append(master_clip)

                real_len = float(master_clip.duration or 0)
                if real_len > 1.0:
                    total_duration = min(total_duration, real_len - 0.02)
                final_audio = master_clip
            except Exception as e:
                print("Pro audio mix failed, using simple mix: " + str(e))
                voice_track = CompositeAudioClip(voice_clips).set_duration(total_duration)
                final_audio, bg_music = self._add_background_music(
                    voice_track, total_duration, bg_music_path
                )
                if bg_music is not None:
                    opened_audio.append(bg_music)

            # ============================================================
            # ---- CONCATENATE (with randomized xfade transitions) ----
            # ============================================================
            video = None
            try:
                # compute each scene's exact length on the master timeline
                scene_lens = []
                for i, (start_t, voice_d) in enumerate(scene_timings):
                    if i + 1 < len(scene_timings):
                        scene_lens.append(scene_timings[i + 1][0] - start_t)
                    else:
                        scene_lens.append(total_duration - start_t)

                tmp_scene_dir = os.path.join(self.output_dir, "scene_tmp")
                os.makedirs(tmp_scene_dir, exist_ok=True)
                scene_files = []
                for i, scene_clip in enumerate(video_scenes):
                    p = os.path.join(tmp_scene_dir, f"scene_{i:02d}.mp4")
                    scene_clip.write_videofile(
                        p,
                        codec="libx264",
                        audio_codec="aac",
                        audio_bitrate="192k",
                        fps=30,
                        preset="ultrafast",
                        ffmpeg_params=["-pix_fmt", "yuv420p", "-crf", "17"],
                        threads=4,
                        temp_audiofile=os.path.join(tmp_scene_dir, f"ta_{i}.m4a"),
                        remove_temp=True,
                        verbose=False,
                        logger=None,
                    )
                    scene_files.append(p)

                if len(scene_files) >= 2:
                    join_modes = transitions_mod.pick_transitions(
                        len(scene_files) - 1, style)
                    xfade_out = os.path.join(self.output_dir, "joined_xfade.mp4")
                    ok = transitions_mod.apply_transitions_ffmpeg(
                        scene_files, scene_lens, join_modes,
                        SCENE_GAP, xfade_out,
                        transition_time=transitions_mod.pick_duration(style),
                    )
                    if ok:
                        joined = VideoFileClip(xfade_out)
                        opened_video.append(joined)
                        video = joined
                        print("Scenes joined with randomized xfade transitions")
                    else:
                        print("xfade stage failed - falling back to plain concat")
            except Exception as e:
                print("xfade pipeline error: " + str(e))

            if video is None:
                print("Using plain concatenate (no transitions)")
                video = concatenate_videoclips(video_scenes, method="chain")

            # ---- DURATION SAFETY AFTER XFADE ----
            if video.duration and video.duration > total_duration + 0.05:
                print(f"Trim after xfade: {video.duration:.2f}s -> {total_duration:.2f}s")
                video = video.subclip(0, total_duration)
            elif video.duration and video.duration < total_duration - 0.05:
                # xfade ate too much - pad with the last frame
                try:
                    last_t = max(0.0, video.duration - 0.05)
                    last_frame = video.get_frame(last_t)
                    pad_dur = total_duration - video.duration
                    pad = ImageClip(last_frame).set_duration(pad_dur)
                    video = concatenate_videoclips([video, pad], method="chain")
                    print(f"Padded after xfade by {pad_dur:.2f}s")
                except Exception as e:
                    print("Pad after xfade failed: " + str(e))

            video = video.set_audio(final_audio).set_duration(total_duration)
            print(f"Final video duration: {video.duration:.2f}s")

            # ---- PRO FX stage ----
            if pro_fx.ENABLED:
                try:
                    base_tmp = os.path.join(self.output_dir, "base_nofx.mov")
                    fx_tmp = os.path.join(self.output_dir, "base_fx.mov")
                    video.write_videofile(
                        base_tmp, codec="libx264", audio_codec="pcm_s16le", fps=30,
                        preset="ultrafast", ffmpeg_params=["-pix_fmt", "yuv420p", "-crf", "14"],
                        threads=4,
                        temp_audiofile=os.path.join(self.output_dir, "temp_audio_fx.wav"),
                        remove_temp=True,
                    )
                    cut_times = [t0 for t0, _d in scene_timings[1:]] + list(abs_cut_times)
                    if pro_fx.apply_pro_fx(base_tmp, fx_tmp, cut_times, total_duration,
                                           hook=True, style=style):
                        fx_clip = VideoFileClip(fx_tmp)
                        opened_video.append(fx_clip)
                        video = fx_clip.set_duration(total_duration)
                    else:
                        print("Pro FX skipped - using plain base video")
                except Exception as e:
                    print("Pro FX stage failed (continuing without): " + str(e))

            overlays = []

            # ---- HOOK CARD ----
            try:
                hook_source = (hook_text or "").strip()
                if not hook_source and word_scenes:
                    hook_source = word_scenes[0].get("caption") or ""
                    if not hook_source:
                        hook_source = " ".join(
                            word_scenes[0].get("narration", "").split()[:6]
                        )
                if hook_source:
                    hook_card = self._make_hook_card(hook_source, style=style)
                    if hook_card is not None:
                        overlays.append(hook_card)
                        print("Hook card added: " + hook_source)
            except Exception as e:
                print("Hook card failed: " + str(e))

            # ---- WORD-BY-WORD CAPTIONS (style-aware) ----
            if word_scenes:
                try:
                    from modules.captions import build_word_caption_clips
                    overlays.extend(build_word_caption_clips(
                        word_scenes, scene_timings, total_duration,
                        word_timings=word_timings,
                        style=style,
                    ))
                except Exception as e:
                    print("Word captions failed: " + str(e))
            elif scene_narrations and font_file:
                for i, (start_t, dur_t) in enumerate(scene_timings):
                    if i >= len(scene_narrations):
                        break
                    caption = self._make_caption_overlay(
                        scene_narrations[i], start_t, dur_t, font_file
                    )
                    if caption is not None:
                        overlays.append(caption)

            # ---- END QUESTION ----
            if end_question and font_file:
                eq = self._make_end_question_overlay(end_question, total_duration,
                                                     font_file, style=style)
                if eq is not None:
                    overlays.append(eq)

            # ---- CTA ----
            if add_cta and font_file:
                cta_overlay = self._make_cta_overlay(total_duration, font_file)
                if cta_overlay is not None:
                    overlays.append(cta_overlay)

            # ---- COMPOSITE OVERLAYS ----
            if overlays:
                try:
                    video = CompositeVideoClip(
                        [video] + overlays, size=(TARGET_W, TARGET_H)
                    ).set_duration(total_duration)
                except Exception as e:
                    print("Overlay compose fail: " + str(e))

            output_path = self._export(video, output_filename, style=style)

            try:
                video.close()
            except Exception:
                pass

        finally:
            for clip in opened_audio:
                try:
                    clip.close()
                except Exception:
                    pass
            for clip in opened_video:
                try:
                    clip.close()
                except Exception:
                    pass

        print("Video ready: " + output_path)
        return output_path

    # ============================================================
    # Single-clip Short builder (kept for backward compatibility)
    # ============================================================
    def create_short(self, video_path, voiceover_path,
                     output_filename="final_short.mp4",
                     bg_music_path="bg_music.mp3"):
        print("Single-clip composition")

        if not os.path.exists(video_path):
            raise FileNotFoundError("Video nahi mili: " + video_path)
        if not os.path.exists(voiceover_path):
            raise FileNotFoundError("Voiceover nahi mili: " + voiceover_path)

        voiceover_clip = AudioFileClip(voiceover_path)
        final_duration = voiceover_clip.duration
        video_clip = VideoFileClip(video_path)

        if video_clip.duration < final_duration:
            video_clip = video_clip.fx(vfx.loop, duration=final_duration)
        else:
            video_clip = video_clip.subclip(0, final_duration)

        final_audio, bg_music = self._add_background_music(
            voiceover_clip, final_duration, bg_music_path
        )
        video_clip = video_clip.set_audio(final_audio)

        font_file = self._get_font_file()
        if font_file:
            cta_overlay = self._make_cta_overlay(final_duration, font_file)
            if cta_overlay is not None:
                try:
                    video_clip = CompositeVideoClip(
                        [video_clip, cta_overlay],
                        size=(TARGET_W, TARGET_H)
                    ).set_duration(final_duration)
                except Exception as e:
                    print("CTA overlay fail: " + str(e))

        output_path = self._export(video_clip, output_filename)

        video_clip.close()
        voiceover_clip.close()
        if bg_music is not None:
            try:
                bg_music.close()
            except Exception:
                pass

        print("Video ready: " + output_path)
        return output_path
