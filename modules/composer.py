"""
Video composer — MoviePy + FFmpeg.

BUG FIX (this version):
  Previous code used `.set_duration(fitted.duration)` in _prepare_scene_video().
  `fitted.duration` returned the ORIGINAL Pexels clip length (15-60s), not the
  trimmed scene length. Result: 9 scenes × ~62s = ~9 min video instead of ~30s.

  FIX: use `.set_duration(duration)` — the function argument (exact scene length).

Retention features in this version:
  - Big bold hook card (first 3 seconds)
  - Word-by-word captions (via modules.captions)
  - Slow punch-in zoom on every scene (+12% over duration)
  - Pro audio mix (voice EQ + synthesized SFX + ducked BGM + loudnorm)
"""

import os
import random
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from moviepy.editor import (
    VideoFileClip, AudioFileClip, CompositeAudioClip,
    concatenate_audioclips, concatenate_videoclips, vfx,
    ImageClip, CompositeVideoClip,
)
import moviepy.audio.fx.all as afx

from modules.audio import build_final_audio, INTER_SCENE_PAUSE

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

# Hook card settings (first 3 seconds)
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
    def _prepare_scene_video(path, duration, shock=False):
        """
        Returns a clip with EXACTLY `duration` seconds.

        BUG FIX: previously `.set_duration(fitted.duration)` was used, which
        returned the ORIGINAL source clip's duration (15-60s from Pexels),
        NOT the trimmed length. This made every scene 15-60s long instead of
        the intended 3-5s, producing a 9-minute video.

        Now: `.set_duration(duration)` uses the function argument directly —
        guaranteed exact scene length.
        """
        try:
            clip = VideoFileClip(path, audio=False)
        except Exception as e:
            raise RuntimeError("VideoFileClip fail: " + path + ": " + str(e))

        if clip.duration is None or clip.duration <= 0:
            clip.close()
            raise RuntimeError("Clip duration invalid: " + path)

        # If source clip shorter than needed -> loop
        if clip.duration < duration + 0.2:
            try:
                clip = clip.fx(vfx.loop, duration=duration + 0.5)
            except Exception as e:
                clip.close()
                raise RuntimeError("Loop fail: " + path + ": " + str(e))
        else:
            # Trim from a random in-point to keep things fresh
            spare = max(0.0, clip.duration - duration - 0.2)
            start = random.uniform(0, spare) if spare > 0.1 else 0.0
            end = start + duration
            if end > clip.duration:
                end = clip.duration
                start = max(0.0, end - duration)
            try:
                clip = clip.subclip(start, end)
            except Exception as e:
                clip.close()
                raise RuntimeError("Subclip fail: " + path + ": " + str(e))

        fitted = ShortsComposer._fit_vertical(clip)

        # FIRST scene = visual hook: punchier colours + faster punch-in zoom
        zoom_gain = 0.12
        if shock:
            try:
                fitted = ShortsComposer._shock_grade(fitted)
                zoom_gain = 0.20
            except Exception as e:
                print("Shock grade skipped: " + str(e))

        # Slow punch-in zoom = constant motion, keeps eyes on screen
        try:
            d = max(duration, 0.5)
            zoomed = fitted.resize(lambda t: 1 + zoom_gain * min(t, d) / d)
            composed = CompositeVideoClip(
                [zoomed.set_position("center")], size=(TARGET_W, TARGET_H)
            )
            # ✅ CRITICAL FIX: use `duration` (the exact arg), not fitted.duration
            return composed.set_duration(duration)
        except Exception:
            # Fallback: hard-force exact duration
            return fitted.set_duration(duration)

    # ============================================================
    # Big bold hook card for the FIRST 3 SECONDS
    # ============================================================
    def _make_hook_card(self, hook_text, duration=HOOK_CARD_DURATION):
        """
        Bold yellow title card (e.g. 'THIS FISH HAS A TRANSPARENT HEAD?!').
        - word-wraps into max 3 lines, shrinking the font until it fits
        - slides down + fades in (0.18s), fades out at the end
        """
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

            font, lines = None, []
            for size in range(HOOK_CARD_FONT_SIZE, 59, -8):
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

            img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=36, fill=(0, 0, 0, 200))
            y = pad
            for i, ln in enumerate(lines):
                x = (img_w - widths[i]) // 2 - boxes[i][0]
                draw.text((x, y - boxes[i][1]), ln, font=font,
                          fill=(255, 235, 59, 255), stroke_width=stroke,
                          stroke_fill=(0, 0, 0, 255))
                y += heights[i] + gap

            y0 = int(TARGET_H * HOOK_CARD_Y_RATIO)
            clip = ImageClip(np.array(img), transparent=True).set_duration(duration).set_start(0)
            clip = clip.set_position(
                lambda t: ("center", y0 - int(46 * max(0.0, 1.0 - t / 0.18)))
            )
            try:
                clip = clip.crossfadein(0.12).crossfadeout(0.25)
            except Exception:
                pass
            return clip
        except Exception as e:
            print("Hook card build failed: " + str(e))
            return None

    # ============================================================
    # Caption rendering (PIL)
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
    # Fallback audio mixer (used only if build_final_audio fails)
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

    def _export(self, video_clip, output_filename):
        output_path = os.path.join(self.output_dir, output_filename)
        video_clip.write_videofile(
            output_path,
            codec="libx264",
            audio_codec="aac",
            audio_bitrate="192k",
            fps=30,
            preset="ultrafast",
            ffmpeg_params=["-pix_fmt", "yuv420p", "-crf", "23"],
            threads=4,
            temp_audiofile=os.path.join(self.output_dir, "temp_audio.m4a"),
            remove_temp=True,
        )
        return output_path

    # ============================================================
    # MAIN — multi-scene Short builder
    # ============================================================
    def create_multi_scene_short(self, clip_paths, voiceover_paths,
                                  output_filename="final_short.mp4",
                                  bg_music_path="bg_music.mp3",
                                  add_cta=True,
                                  scene_narrations=None,
                                  word_scenes=None,
                                  hook_text=None,
                                  word_timings=None):
        print("Multi-scene composition START")

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

                try:
                    scene_video = self._prepare_scene_video(
                        clip_path, scene_duration, shock=(index == 0)
                    )
                except Exception as e:
                    raise RuntimeError("Scene video fail: " + str(e))

                opened_video.append(scene_video)
                video_scenes.append(scene_video)
                voice_clips.append(voice.set_start(timeline))
                timeline += scene_duration

                print(f"Scene {index + 1} ready — dur={scene_duration:.2f}s")

            total_duration = timeline
            print(f"TOTAL TIMELINE: {total_duration:.2f}s")
            if total_duration > 40:
                raise RuntimeError(
                    f"Timeline {total_duration:.0f}s too long for a Short "
                    "(voiceover bug?) - aborting before slow render")

            # SAFETY CHECK: verify video_scenes sum matches total_duration
            scene_sum = sum(float(s.duration or 0) for s in video_scenes)
            print(f"Safety: video_scenes sum = {scene_sum:.2f}s "
                  f"(expected {total_duration:.2f}s)")
            if abs(scene_sum - total_duration) > total_duration * 0.3:
                print("⚠️ DURATION MISMATCH! Trimming scenes to exact length...")
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

            # ---- AUDIO MASTER ----
            try:
                master_path = build_final_audio(
                    voiceover_paths[:count],
                    scene_timings,
                    total_duration,
                    bg_music_path,
                    out_dir=os.path.join(self.output_dir, "mix"),
                )
                master_clip = AudioFileClip(master_path)
                opened_audio.append(master_clip)

                real_len = float(master_clip.duration or 0)
                if real_len > 1.0:
                    total_duration = min(total_duration, real_len - 0.15)
                final_audio = master_clip
            except Exception as e:
                print("Pro audio mix failed, using simple mix: " + str(e))
                voice_track = CompositeAudioClip(voice_clips).set_duration(total_duration)
                final_audio, bg_music = self._add_background_music(
                    voice_track, total_duration, bg_music_path
                )
                if bg_music is not None:
                    opened_audio.append(bg_music)

            # ---- CONCATENATE ----
            video = concatenate_videoclips(video_scenes, method="chain")

            # FORCE final duration = total_duration (safety)
            if video.duration and video.duration > total_duration + 0.5:
                print(f"⚠️ Final video too long ({video.duration:.2f}s) "
                      f"— trimming to {total_duration:.2f}s")
                video = video.subclip(0, total_duration)

            video = video.set_audio(final_audio).set_duration(total_duration)
            print(f"Final video duration: {video.duration:.2f}s")

            overlays = []

            # ---- HOOK CARD (first 3 seconds) ----
            try:
                hook_source = (hook_text or "").strip()
                if not hook_source and word_scenes:
                    hook_source = word_scenes[0].get("caption") or ""
                    if not hook_source:
                        hook_source = " ".join(
                            word_scenes[0].get("narration", "").split()[:6]
                        )
                if hook_source:
                    hook_card = self._make_hook_card(hook_source, HOOK_CARD_DURATION)
                    if hook_card is not None:
                        overlays.append(hook_card)
                        print("Hook card added (first 3s): " + hook_source)
            except Exception as e:
                print("Hook card failed: " + str(e))

            # ---- WORD-BY-WORD CAPTIONS ----
            if word_scenes:
                try:
                    from modules.captions import build_word_caption_clips
                    overlays.extend(build_word_caption_clips(
                        word_scenes, scene_timings, total_duration,
                        word_timings=word_timings,
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

            output_path = self._export(video, output_filename)

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
