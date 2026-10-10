"""
Audio engine v5 - style-aware SFX + human-sounding voice.

Changes in this version
-----------------------
- build_sfx_track / build_final_audio accept a `style` dict so whoosh/pop/click
  gains are randomized per video (no metronomic sound design).
- Everything else is the same as v4 (Kokoro primary + Edge-TTS fallback,
  humanize chain, broadcast-grade master).
"""

import asyncio
import os
import random
import re
import subprocess
import time
import wave

import numpy as np
import edge_tts

SR = 44100

# ---------------------------------------------------------------- voice ----
VOICE_CHAIN = [
    os.getenv("TTS_VOICE_PRIMARY",   "en-US-AndrewMultilingualNeural"),
    os.getenv("TTS_VOICE_FALLBACK1", "en-US-BrianMultilingualNeural"),
    os.getenv("TTS_VOICE_FALLBACK2", "en-US-GuyNeural"),
    os.getenv("TTS_VOICE_FALLBACK3", "en-GB-RyanNeural"),
]
TTS_RETRIES_PER_VOICE = 2
VOICE_VOLUME = "+0%"

TTS_ENGINE = os.getenv("TTS_ENGINE", "auto").lower()
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "af_heart")
KOKORO_LANG = os.getenv("KOKORO_LANG", "a")
KOKORO_SR = 24000
KOKORO_RETRIES = 2

VOICE_RATE = os.getenv("TTS_RATE", "+7%")
VOICE_PITCH = os.getenv("TTS_PITCH", "+0Hz")

LAST_ENGINE = None

SILENCE_TRIM_DB = "-45dB"
SILENCE_KEEP = 0.05

INTER_SCENE_PAUSE = 0.05

POWER_WORDS = {
    "never", "always", "secret", "truth", "lies", "lie", "dead", "die",
    "deadly", "impossible", "shocking", "alive", "brain", "heart", "money",
    "gold", "world", "first", "last", "only", "fastest", "biggest",
    "strongest", "hidden", "crazy", "insane", "million", "billion",
    "nobody", "everyone", "stop", "warning", "real", "fake", "myth", "kill",
    "killed", "fire", "ice", "space", "ocean", "speed", "power", "time",
    "twist", "wrong", "mystery", "forever", "zero", "tickle", "ticklish",
    "laugh", "touch", "nerve", "reflex", "yourself", "actually", "really",
    "warning", "danger", "dangerous", "help", "worst", "best",
}

DRAMATIC_PAUSE_WORDS = {"but", "however", "except", "suddenly", "wait"}


# ------------------------------------------------------------- SSML layer ----
def _build_ssml(text, voice, rate, pitch):
    esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    tokens = re.findall(r"\S+", esc)
    out = []
    for i, tok in enumerate(tokens):
        clean = re.sub(r"[^\w']", "", tok).lower()
        if (i > 0
                and clean in POWER_WORDS
                and not out[-1].endswith((",", ".", "!", "?", ";", ":"))):
            out[-1] = out[-1] + ","
        if (i > 0
                and clean in DRAMATIC_PAUSE_WORDS
                and not out[-1].endswith((",", ".", "!", "?", ";", ":"))):
            out[-1] = out[-1] + ","
        out.append(tok)
    processed = " ".join(out)

    ssml = (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="en-US">'
        f'<voice name="{voice}">'
        f'<mstts:express-as style="newscast-casual" styledegree="1.2">'
        f'<prosody rate="{rate}" pitch="{pitch}" volume="{VOICE_VOLUME}">'
        f'<break time="80ms"/>'
        f'{processed}'
        f'<break time="80ms"/>'
        f'</prosody>'
        f'</mstts:express-as>'
        f'</voice>'
        f'</speak>'
    )
    return ssml


async def _tts_async(text, output_path, voice, rate, pitch, words_out=None):
    tokens = re.findall(r"\S+", text)
    out = []
    for i, tok in enumerate(tokens):
        clean = re.sub(r"[^\w']", "", tok).lower()
        if (i > 0 and (clean in POWER_WORDS or clean in DRAMATIC_PAUSE_WORDS)
                and not out[-1].endswith((",", ".", "!", "?", ";", ":"))):
            out[-1] = out[-1] + ","
        out.append(tok)
    spoken = " ".join(out)

    kwargs = dict(text=spoken, voice=voice, rate=rate, pitch=pitch, volume=VOICE_VOLUME)
    try:
        communicate = edge_tts.Communicate(**kwargs, boundary="WordBoundary")
    except TypeError:
        communicate = edge_tts.Communicate(**kwargs)

    audio = bytearray()
    async for chunk in communicate.stream():
        ctype = chunk.get("type")
        if ctype == "audio":
            audio.extend(chunk["data"])
        elif ctype == "WordBoundary" and words_out is not None:
            off = chunk.get("offset", 0) / 1e7
            dur = chunk.get("duration", 0) / 1e7
            words_out.append({"text": chunk.get("text", ""), "start": off, "end": off + dur})
    if not audio:
        raise RuntimeError("Edge TTS returned no audio")
    with open(output_path, "wb") as f:
        f.write(bytes(audio))


def _run(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


HUMANIZE_AF = (
    "highpass=f=80,"
    "equalizer=f=140:t=q:w=0.9:g=1.8,"
    "equalizer=f=320:t=q:w=1.0:g=-1.2,"
    "equalizer=f=2800:t=q:w=1.1:g=1.6,"
    "equalizer=f=7200:t=q:w=1.8:g=-2.4,"
    "acompressor=threshold=0.12:ratio=2.2:attack=12:release=110:makeup=1.4,"
    "aecho=0.92:0.9:23:0.07"
)


def _trim_silence(path, src=None, humanize=True):
    src = src or path
    tmp = path + ".trim.mp3"
    af = (
        f"silenceremove=start_periods=1:"
        f"start_threshold={SILENCE_TRIM_DB}:start_silence={SILENCE_KEEP},"
        f"areverse,"
        f"silenceremove=start_periods=1:"
        f"start_threshold={SILENCE_TRIM_DB}:start_silence={SILENCE_KEEP},"
        f"areverse,"
        + (HUMANIZE_AF if humanize else "highpass=f=85")
        + ",afade=t=in:d=0.006"
    )
    cmd = ["ffmpeg", "-y", "-i", src, "-af", af, "-ar", str(SR), "-b:a", "192k", tmp]
    try:
        r = _run(cmd, 60)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 500:
            os.replace(tmp, path)
        else:
            if os.path.exists(tmp):
                os.remove(tmp)
            if humanize:
                print("Humanize chain failed, retrying plain: " + (r.stderr or "")[-300:])
                return _trim_silence(path, src=src, humanize=False)
            if src != path:
                _run(["ffmpeg", "-y", "-i", src, "-ar", str(SR), "-b:a", "192k", path], 60)
    except Exception as e:
        print(f"Silence trim skipped for {path}: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)


def get_duration(path):
    try:
        pr = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", path], 30)
        return float(pr.stdout.strip())
    except Exception:
        return 0.0


def _estimate_words(text, trimmed_len):
    toks = text.split()
    if not toks or trimmed_len <= 0.2:
        return []
    lo, hi = SILENCE_KEEP, max(SILENCE_KEEP + 0.2, trimmed_len - 0.03)
    weights = [len(re.sub(r"\W", "", t)) + 2 for t in toks]
    total, t, out = float(sum(weights)), lo, []
    for tok, w in zip(toks, weights):
        d = (hi - lo) * w / total
        out.append({"text": tok, "start": t, "end": t + d})
        t += d
    return out


def _save_word_timings(words, mp3_path, trimmed_len):
    import json
    if not words:
        return None
    shift = max(0.0, words[0]["start"] - SILENCE_KEEP)
    fixed = []
    for w in words:
        t0 = max(0.0, w["start"] - shift)
        t1 = max(t0 + 0.04, w["end"] - shift)
        if trimmed_len > 0:
            t0 = min(t0, trimmed_len)
            t1 = min(t1, trimmed_len)
        if w["text"].strip():
            fixed.append({"text": w["text"], "start": round(t0, 3), "end": round(t1, 3)})
    path = mp3_path + ".words.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fixed, f)
    return path


def load_word_timings(mp3_path):
    import json
    path = mp3_path + ".words.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) and data else None
    except Exception:
        return None


def _rate_pct(rate):
    try:
        return float(str(rate).replace("%", "").replace("+", ""))
    except Exception:
        return 7.0


_KPIPE = None


def kokoro_available():
    try:
        import kokoro      # noqa: F401
        import soundfile   # noqa: F401
        return True
    except Exception:
        return False


def _kokoro_tts(text, raw_wav, speed, words_out):
    global _KPIPE
    import soundfile as sf
    if _KPIPE is None:
        from kokoro import KPipeline
        _KPIPE = KPipeline(lang_code=KOKORO_LANG)

    chunks, offset = [], 0.0
    for res in _KPIPE(text, voice=KOKORO_VOICE, speed=speed, split_pattern=r"\n+"):
        audio = getattr(res, "audio", None)
        if audio is None and isinstance(res, tuple) and len(res) >= 3:
            audio = res[2]
        if audio is None:
            continue
        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        for tok in (getattr(res, "tokens", None) or []):
            t0 = getattr(tok, "start_ts", None)
            t1 = getattr(tok, "end_ts", None)
            txt = (getattr(tok, "text", "") or "").strip()
            if t0 is None or t1 is None or not re.search(r"\w", txt):
                continue
            words_out.append({"text": txt, "start": offset + float(t0), "end": offset + float(t1)})
        chunks.append(audio)
        offset += len(audio) / KOKORO_SR
    if not chunks:
        raise RuntimeError("Kokoro returned no audio")
    sf.write(raw_wav, np.concatenate(chunks), KOKORO_SR)


def _finish(raw_path, output_path, clean, words):
    _trim_silence(output_path, src=raw_path)
    if not (os.path.exists(output_path) and os.path.getsize(output_path) > 1000):
        raise RuntimeError("voice file missing after post-processing")
    dur = get_duration(output_path)
    n_words = len(clean.split())
    max_ok = max(4.0, n_words * 0.9 + 2.0)
    if dur and dur > max_ok:
        raise RuntimeError(f"voice too long ({dur:.1f}s for {n_words} words)")
    if len(words) >= 0.7 * n_words:
        _save_word_timings(words, output_path, dur)
    else:
        est = _estimate_words(clean, dur)
        if est:
            _save_word_timings(est, output_path, dur)
        print(f"WordBoundary incomplete ({len(words)}/{n_words}) - using estimated caption timing")
    return True


def generate_voiceover(text, output_path, rate=None, pitch=None, engine=None):
    global LAST_ENGINE
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    clean = " ".join(str(text).split())
    if not clean:
        raise ValueError("Voiceover text empty hai.")

    sidecar = output_path + ".words.json"
    if os.path.exists(sidecar):
        os.remove(sidecar)

    mode = (engine or TTS_ENGINE or "auto").lower()
    last_err = None

    if mode in ("auto", "kokoro") and kokoro_available():
        raw = output_path + ".raw.wav"
        speed = max(0.85, min(1.25, 1.0 + _rate_pct(rate or VOICE_RATE) / 100.0))
        for attempt in range(1, KOKORO_RETRIES + 1):
            try:
                words = []
                _kokoro_tts(clean, raw, speed, words)
                _finish(raw, output_path, clean, words)
                LAST_ENGINE = "kokoro"
                return output_path
            except Exception as e:
                last_err = e
                print(f"Kokoro attempt {attempt}/{KOKORO_RETRIES} failed: {e}")
            finally:
                if os.path.exists(raw):
                    os.remove(raw)
        if mode == "kokoro":
            raise RuntimeError(f"Kokoro failed: {last_err}")
    elif mode == "kokoro":
        raise RuntimeError("Kokoro not installed (pip install kokoro soundfile + espeak-ng)")

    for voice in VOICE_CHAIN:
        for attempt in range(1, TTS_RETRIES_PER_VOICE + 1):
            raw = output_path + ".raw.mp3"
            try:
                words = []
                asyncio.run(_tts_async(
                    clean, raw, voice,
                    rate or VOICE_RATE,
                    pitch or VOICE_PITCH,
                    words_out=words,
                ))
                if os.path.exists(raw) and os.path.getsize(raw) > 1000:
                    _finish(raw, output_path, clean, words)
                    LAST_ENGINE = "edge"
                    return output_path
                raise RuntimeError("Edge TTS ne valid audio nahi di.")
            except Exception as e:
                last_err = e
                print(f"Edge TTS ({voice}) attempt {attempt}/{TTS_RETRIES_PER_VOICE} failed: {e}")
                time.sleep(1.0 * attempt)
            finally:
                if os.path.exists(raw):
                    os.remove(raw)

    raise RuntimeError(f"All voices failed. Last error: {last_err}")


# ------------------------------------------------------------ synth SFX ----
def _norm(x, peak=0.9):
    m = float(np.max(np.abs(x))) or 1.0
    return (x / m * peak).astype(np.float32)


def _t(dur):
    return np.arange(int(SR * dur), dtype=np.float64) / SR


def _svf_bandpass_sweep(noise, f_start, f_end, q=2.0):
    n = len(noise)
    freqs = np.geomspace(f_start, f_end, n)
    f = 2.0 * np.sin(np.pi * freqs / SR)
    damp = 1.0 / q
    low = band = 0.0
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        low += f[i] * band
        high = noise[i] - low - damp * band
        band += f[i] * high
        out[i] = band
    return out


def synth_whoosh(dur=0.42, rising=True, rng=None):
    rng = rng or np.random.default_rng()
    n = int(SR * dur)
    noise = rng.standard_normal(n)
    lo, hi = (350, 4200) if rising else (4200, 350)
    sig = _svf_bandpass_sweep(noise, lo, hi, q=1.6)
    t = np.linspace(0, 1, n)
    env = np.sin(np.pi * np.clip(t ** 0.8, 0, 1)) ** 2
    return _norm(sig * env, 0.85)


def synth_riser(dur=1.0, rng=None):
    rng = rng or np.random.default_rng()
    n = int(SR * dur)
    noise = rng.standard_normal(n)
    sig = _svf_bandpass_sweep(noise, 250, 7000, q=1.2)
    t = np.linspace(0, 1, n)
    env = t ** 2.2
    tone = np.sin(2 * np.pi * np.cumsum(np.geomspace(180, 900, n)) / SR) * 0.25
    return _norm((sig * 0.9 + tone) * env, 0.8)


def synth_boom(dur=1.0, rng=None):
    rng = rng or np.random.default_rng()
    t = _t(dur)
    freq = 38 + (110 - 38) * np.exp(-t * 9)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * np.exp(-t * 3.6)
    thump = rng.standard_normal(len(t)) * np.exp(-t * 55)
    thump = np.convolve(thump, np.ones(24) / 24, mode="same")
    sig = np.tanh(1.8 * (body + 0.55 * thump))
    fade = np.minimum(1, (len(t) - np.arange(len(t))) / (SR * 0.05))
    return _norm(sig * fade, 0.95)


def synth_pop(dur=0.16):
    t = _t(dur)
    freq = 900 * np.exp(-t * 18) + 180
    sig = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 26)
    return _norm(sig, 0.8)


def synth_click(dur=0.06, rng=None):
    rng = rng or np.random.default_rng()
    t = _t(dur)
    tick = np.sin(2 * np.pi * 2400 * t) * np.exp(-t * 90)
    burst = rng.standard_normal(len(t)) * np.exp(-t * 140)
    burst = burst - np.convolve(burst, np.ones(8) / 8, mode="same")
    return _norm(tick * 0.7 + burst * 0.5, 0.7)


def synth_ding(dur=0.9):
    t = _t(dur)
    sig = (
        np.sin(2 * np.pi * 1318.5 * t) * 0.6
        + np.sin(2 * np.pi * 1976.0 * t) * 0.3
        + np.sin(2 * np.pi * 2637.0 * t) * 0.12
    ) * np.exp(-t * 6.5)
    sig *= np.minimum(1, t / 0.004)
    return _norm(sig, 0.75)


def _place(track, sample, start_sec, gain):
    s = int(start_sec * SR)
    if s < 0:
        sample = sample[-s:]
        s = 0
    if s >= len(track) or len(sample) == 0:
        return
    e = min(len(track), s + len(sample))
    track[s:e] += sample[: e - s] * gain


def build_sfx_track(scene_timings, total_duration, out_path, seed=None,
                    events=None, style=None):
    """
    SFX aligned to voice. Gains come from `style` so each video has its own
    sound density (no metronomic template).
    """
    style = style or {}
    rng = np.random.default_rng(seed if seed is not None else random.randrange(1 << 30))
    n = int(SR * (total_duration + 1.5))
    track = np.zeros(n, dtype=np.float32)

    whoosh_gain = style.get("sfx_whoosh_gain", 0.48)
    pop_gain = style.get("sfx_pop_gain", 0.18)
    click_gain = style.get("sfx_click_gain", 0.33)

    whoosh_a = synth_whoosh(0.42, True, rng)
    whoosh_b = synth_whoosh(0.42, False, rng)
    boom = synth_boom(1.0, rng)
    riser = synth_riser(1.0, rng)
    pop = synth_pop()

    hook_start = scene_timings[0][0] if scene_timings else 0.0
    _place(track, boom, hook_start, 0.9)

    count = len(scene_timings)
    for i, (start, _dur) in enumerate(scene_timings):
        if i == 0:
            continue
        if i == count - 2 and count >= 4:
            _place(track, riser, max(0.0, start - 1.0), 0.6)
            _place(track, boom, start, 0.65)
            continue
        if i == count - 1:
            _place(track, pop, max(0.0, start - 0.04), 0.3)
            continue
        w = whoosh_a if i % 2 else whoosh_b
        _place(track, w, start - 0.10, whoosh_gain)

    if events:
        click = synth_click(rng=rng)
        short_a = synth_whoosh(0.20, True, rng)
        short_b = synth_whoosh(0.20, False, rng)
        last_pop = -9.0
        for k, (t_ev, kind) in enumerate(sorted(events)):
            if t_ev < 0 or t_ev > total_duration:
                continue
            if kind == "cut":
                _place(track, short_a if k % 2 else short_b, t_ev - 0.07, whoosh_gain * 0.62)
                _place(track, click, t_ev, click_gain * 0.66)
            elif kind == "pop":
                if t_ev < 0.20 or t_ev - last_pop < 0.45:
                    continue
                _place(track, pop, t_ev, pop_gain)
                last_pop = t_ev
            elif kind == "click":
                _place(track, click, t_ev, click_gain)

    track = np.tanh(track * 1.15)
    pcm = (np.clip(track, -1, 1) * 32767).astype(np.int16)
    stereo = np.repeat(pcm[:, None], 2, axis=1)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(stereo.tobytes())
    return out_path


# ------------------------------------------------------------------ mix ----
def _build_voice_track(voice_paths, scene_timings, total_duration, out_path):
    cmd = ["ffmpeg", "-y"]
    for p in voice_paths:
        cmd += ["-i", p]
    parts, labels = [], []
    for i, (start, _d) in enumerate(scene_timings):
        ms = max(0, int(start * 1000))
        parts.append(
            f"[{i}:a]aresample={SR},aformat=channel_layouts=stereo,adelay={ms}|{ms}[v{i}]"
        )
        labels.append(f"[v{i}]")
    graph = (
        ";".join(parts)
        + ";"
        + "".join(labels)
        + f"amix=inputs={len(labels)}:normalize=0:duration=longest,"
        + f"apad=whole_dur={total_duration:.3f},atrim=0:{total_duration:.3f}[out]"
    )
    cmd += ["-filter_complex", graph, "-map", "[out]", "-ar", str(SR), out_path]
    r = _run(cmd)
    if r.returncode != 0:
        raise RuntimeError("voice track build failed:\n" + r.stderr[-1200:])
    return out_path


def build_final_audio(voice_paths, scene_timings, total_duration, bg_music_path,
                      out_dir="assets/mix", out_name="final_audio.wav",
                      bgm_level=0.55, sfx_level=0.9, sfx_events=None, style=None):
    """
    Master chain: voice EQ + de-esser + compressor; BGM ducked by voice;
    synthesized SFX (gains from `style`); loudnorm -14 LUFS.
    """
    os.makedirs(out_dir, exist_ok=True)
    voice_wav = os.path.join(out_dir, "voice_track.wav")
    sfx_wav = os.path.join(out_dir, "sfx_track.wav")
    out_path = os.path.join(out_dir, out_name)

    _build_voice_track(voice_paths, scene_timings, total_duration, voice_wav)
    build_sfx_track(scene_timings, total_duration, sfx_wav, events=sfx_events, style=style)

    inputs = ["-i", voice_wav, "-i", sfx_wav]
    has_bgm = bool(bg_music_path and os.path.exists(bg_music_path)
                   and os.path.getsize(bg_music_path) > 1000)

    voice_chain = (
        "[0:a]highpass=f=90,"
        "equalizer=f=250:t=q:w=1.0:g=-2.5,"
        "equalizer=f=800:t=q:w=1.2:g=-1.0,"
        "equalizer=f=3200:t=q:w=1.0:g=3.5,"
        "equalizer=f=6500:t=q:w=1.5:g=-2.0,"
        "acompressor=threshold=0.085:ratio=3.8:attack=5:release=85:makeup=2.8,"
        "alimiter=limit=0.95"
    )

    if has_bgm:
        try:
            probe = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", bg_music_path], 30)
            bgm_len = float(probe.stdout.strip())
        except Exception:
            bgm_len = 0.0
        start = (random.uniform(0, bgm_len - total_duration - 1)
                 if bgm_len > total_duration + 2 else 0.0)
        inputs += ["-stream_loop", "-1", "-ss", f"{start:.2f}", "-i", bg_music_path]

        graph = (
            f"{voice_chain}[vc];"
            "[vc]asplit=2[vmix][vside];"
            f"[2:a]atrim=0:{total_duration:.3f},asetpts=N/SR/TB,"
            "aformat=channel_layouts=stereo,"
            "highpass=f=60,lowpass=f=9000,"
            f"volume={bgm_level},"
            f"afade=t=in:d=0.02,afade=t=out:st={max(0.0, total_duration - 0.04):.2f}:d=0.04[bgm];"
            "[bgm][vside]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350:makeup=1[bgduck];"
            f"[1:a]volume={sfx_level}[sfx];"
            "[vmix][bgduck][sfx]amix=inputs=3:normalize=0:duration=first[mix];"
        )
    else:
        print("BG music missing - voice + SFX only")
        graph = (
            f"{voice_chain}[vmix];"
            f"[1:a]volume={sfx_level}[sfx];"
            "[vmix][sfx]amix=inputs=2:normalize=0:duration=first[mix];"
        )

    graph += (
        "[mix]loudnorm=I=-14:TP=-1.5:LRA=9,"
        f"atrim=0:{total_duration:.3f},"
        "alimiter=limit=0.97[master]"
    )

    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", graph, "-map", "[master]",
           "-ar", str(SR), "-c:a", "pcm_s16le", out_path]
    r = _run(cmd, 300)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("final audio mix failed:\n" + r.stderr[-1500:])
    print(f"Final audio ready: {out_path}")
    return out_path
