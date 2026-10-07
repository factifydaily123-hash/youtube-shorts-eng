"""
Audio engine v3 — "ElevenLabs Killer" Edge-TTS edition.

Ye file Edge-TTS ki MAXIMUM free quality nikalne ke liye tuned hai:

  1. VOICE CHAIN: AndrewMultilingual (best 2024 male voice) → Brian → Guy → Ryan
     Andrew female se better male voice hai, aur GuyNeural se 2 generations naya.

  2. PER-SCENE PROSODY: rate/pitch curve jo har scene ko alag energy deta hai
     (hook = fast+high, twist = slow+low, loop = medium).

  3. EMPHASIS COMMAS: power words ke aage comma insert karte hain, Edge-TTS
     naturally ruk jaata hai — ye "SSML emphasis" ka lightweight version hai.

  4. SSML BREAKS: sentence ke start/end pe 80ms break — click-free transitions.

  5. TIGHT SILENCE TRIM: -45dB / 0.05s keep = no dead air.

  6. POST-EQ: presence boost (3.2kHz) + de-esser (6.5kHz cut) = broadcast clarity.

  7. SYNTH SFX: pure numpy, no copyright risk, never fails.
     - boom on hook, whoosh on cuts, riser+boom on twist, ding on loop.

  8. FINAL MASTER: compressor + sidechain-ducked BGM + loudnorm -14 LUFS.
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
# Tried in ORDER. If one fails N times, move to next.
# Andrew = 2024 flagship male voice, most natural for narration.
VOICE_CHAIN = [
    os.getenv("TTS_VOICE_PRIMARY",   "en-US-AndrewMultilingualNeural"),
    os.getenv("TTS_VOICE_FALLBACK1", "en-US-BrianMultilingualNeural"),
    os.getenv("TTS_VOICE_FALLBACK2", "en-US-GuyNeural"),
    os.getenv("TTS_VOICE_FALLBACK3", "en-GB-RyanNeural"),
]
TTS_RETRIES_PER_VOICE = 2
VOICE_VOLUME = "+0%"

# Default rate/pitch — per-scene overrides come from main.py
VOICE_RATE = os.getenv("TTS_RATE", "+15%")
VOICE_PITCH = os.getenv("TTS_PITCH", "+0Hz")

# Silence trim — aggressive
SILENCE_TRIM_DB = "-45dB"
SILENCE_KEEP = 0.05

# Scene gap — tight (was 0.12)
INTER_SCENE_PAUSE = 0.05

# Power words: comma inserted before them -> micro-pause -> perceived emphasis
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

# Words that get a slight slowdown via comma + short pause
DRAMATIC_PAUSE_WORDS = {"but", "however", "except", "suddenly", "wait"}


# ------------------------------------------------------------- SSML layer ----
def _build_ssml(text, voice, rate, pitch):
    """
    Wrap plain text in SSML with:
      - 80ms break at start and end (click-free)
      - emphasis commas before power words
      - 60ms break before dramatic words (but/however/suddenly)
      - 100ms break after exclamation/question marks (built into text)
    """
    # Escape for XML
    esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    # Add comma before power words (only if not already at sentence start)
    tokens = re.findall(r"\S+", esc)
    out = []
    for i, tok in enumerate(tokens):
        clean = re.sub(r"[^\w']", "", tok).lower()
        # comma before power word
        if (i > 0
                and clean in POWER_WORDS
                and not out[-1].endswith((",", ".", "!", "?", ";", ":"))):
            out[-1] = out[-1] + ","
        # comma before dramatic word (heavier pause)
        if (i > 0
                and clean in DRAMATIC_PAUSE_WORDS
                and not out[-1].endswith((",", ".", "!", "?", ";", ":"))):
            out[-1] = out[-1] + ","
        out.append(tok)
    processed = " ".join(out)

    # Wrap in SSML
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


async def _tts_async(text, output_path, voice, rate, pitch, use_ssml=True):
    """Generate TTS. Tries SSML first (better prosody), falls back to plain."""
    if use_ssml:
        try:
            ssml = _build_ssml(text, voice, rate, pitch)
            communicate = edge_tts.Communicate(ssml, voice)
            await communicate.save(output_path)
            return
        except Exception as e:
            print(f"SSML failed for {voice}, falling back to plain: {e}")

    # Plain fallback (no SSML)
    communicate = edge_tts.Communicate(
        text=text, voice=voice, rate=rate, pitch=pitch, volume=VOICE_VOLUME
    )
    await communicate.save(output_path)


def _run(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _trim_silence(path):
    """
    Aggressive silence trim + de-rumble + click-free fades.
    - highpass 85Hz removes rumble
    - -45dB threshold removes breath noises
    - 6ms fade-in kills click
    """
    tmp = path + ".trim.mp3"
    af = (
        f"silenceremove=start_periods=1:"
        f"start_threshold={SILENCE_TRIM_DB}:start_silence={SILENCE_KEEP},"
        f"areverse,"
        f"silenceremove=start_periods=1:"
        f"start_threshold={SILENCE_TRIM_DB}:start_silence={SILENCE_KEEP},"
        f"areverse,"
        f"highpass=f=85,"
        f"afade=t=in:d=0.006"
    )
    cmd = ["ffmpeg", "-y", "-i", path, "-af", af, "-ar", str(SR), "-b:a", "192k", tmp]
    try:
        r = _run(cmd, 60)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 500:
            os.replace(tmp, path)
        elif os.path.exists(tmp):
            os.remove(tmp)
    except Exception as e:
        print(f"Silence trim skipped for {path}: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)


def generate_voiceover(text, output_path, rate=None, pitch=None):
    """
    One scene of narration -> mp3 (trimmed).
    Tries the voice chain in order. Fails only if ALL voices fail.
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    clean = " ".join(str(text).split())
    if not clean:
        raise ValueError("Voiceover text empty hai.")

    last_err = None
    for voice in VOICE_CHAIN:
        for attempt in range(1, TTS_RETRIES_PER_VOICE + 1):
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)

                asyncio.run(_tts_async(
                    clean, output_path, voice,
                    rate or VOICE_RATE,
                    pitch or VOICE_PITCH,
                    use_ssml=True,
                ))

                if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                    _trim_silence(output_path)
                    return output_path

                raise RuntimeError("Edge TTS ne valid audio nahi di.")
            except Exception as e:
                last_err = e
                print(f"Edge TTS ({voice}) attempt {attempt}/{TTS_RETRIES_PER_VOICE} failed: {e}")
                time.sleep(1.0 * attempt)

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


def build_sfx_track(scene_timings, total_duration, out_path, seed=None):
    """SFX aligned to voice (hook_start, not fixed 0.0)."""
    rng = np.random.default_rng(seed if seed is not None else random.randrange(1 << 30))
    n = int(SR * (total_duration + 1.5))
    track = np.zeros(n, dtype=np.float32)

    whoosh_a = synth_whoosh(0.42, True, rng)
    whoosh_b = synth_whoosh(0.42, False, rng)
    boom = synth_boom(1.0, rng)
    riser = synth_riser(1.0, rng)
    ding = synth_ding()
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
            _place(track, ding, start + 0.02, 0.5)
            _place(track, pop, max(0.0, start - 0.04), 0.4)
            continue
        w = whoosh_a if i % 2 else whoosh_b
        _place(track, w, start - 0.10, 0.48)

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
                      bgm_level=0.55, sfx_level=0.9):
    """
    Master chain (broadcast-grade):
      voice -> highpass 90 -> mid-cut 250Hz -> presence boost 3.2kHz
             -> DE-ESSER 6.5kHz (new!) -> compressor -> limiter
      bgm   -> looped, ducked by voice (sidechain)
      sfx   -> synthesized
      mix   -> loudnorm -14 LUFS + final limiter
    """
    os.makedirs(out_dir, exist_ok=True)
    voice_wav = os.path.join(out_dir, "voice_track.wav")
    sfx_wav = os.path.join(out_dir, "sfx_track.wav")
    out_path = os.path.join(out_dir, out_name)

    _build_voice_track(voice_paths, scene_timings, total_duration, voice_wav)
    build_sfx_track(scene_timings, total_duration, sfx_wav)

    inputs = ["-i", voice_wav, "-i", sfx_wav]
    has_bgm = bool(bg_music_path and os.path.exists(bg_music_path)
                   and os.path.getsize(bg_music_path) > 1000)

    # VOICE CHAIN — "broadcast-grade":
    #   1. Highpass 90Hz removes rumble
    #   2. 250Hz cut = removes muddiness
    #   3. 3.2kHz boost = presence/clarity
    #   4. 6.5kHz cut = DE-ESSER (removes harsh "s" sounds)
    #   5. compressor = consistent loudness
    #   6. limiter = peak safety
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
            f"afade=t=in:d=0.6,afade=t=out:st={max(0.0, total_duration - 1.2):.2f}:d=1.2[bgm];"
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
