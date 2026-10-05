"""
Audio engine for the Shorts pipeline.

- Voice: edge-tts (English MALE neural voice only, retried - no female fallback),
  tight silence trim, light EQ + de-click fades.
- SFX: generated locally with numpy (no downloads, never fails, no copyright).
- Mix: ffmpeg only. BGM is ducked under the voice (sidechain), voice is
  compressed for clarity, and the master is loudness-normalised for Shorts.
"""

import asyncio
import os
import random
import subprocess
import time
import wave

import numpy as np
import edge_tts

SR = 44100

# ---------------------------------------------------------------- voice ----
# Male voice is LOCKED for every scene. gTTS fallback was removed because gTTS
# only has a female voice, which made the gender flip between scenes whenever
# Edge TTS failed once. If Edge TTS fails we retry the SAME voice instead.
VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural")
TTS_RETRIES = 5
VOICE_RATE = os.getenv("TTS_RATE", "+6%")
VOICE_PITCH = "+0Hz"
VOICE_VOLUME = "+0%"

# Old value was 0.3s, which KEPT 0.3s of silence at both ends of every scene
# (~0.6s dead air between sentences). Keep only a tiny natural breath now.
SILENCE_TRIM_DB = "-42dB"
SILENCE_KEEP = 0.06
INTER_SCENE_PAUSE = 0.12


async def _tts_async(text, output_path, rate, pitch):
    communicate = edge_tts.Communicate(
        text=text, voice=VOICE, rate=rate, pitch=pitch, volume=VOICE_VOLUME
    )
    await communicate.save(output_path)


def _run(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _trim_silence(path):
    """Trim head/tail silence, remove rumble, add a tiny fade-in (no clicks)."""
    tmp = path + ".trim.mp3"
    k = SILENCE_KEEP
    af = (
        f"silenceremove=start_periods=1:start_threshold={SILENCE_TRIM_DB}:start_silence={k},"
        f"areverse,"
        f"silenceremove=start_periods=1:start_threshold={SILENCE_TRIM_DB}:start_silence={k},"
        f"areverse,"
        f"highpass=f=80,"
        f"afade=t=in:d=0.005"
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
    """One scene of narration -> mp3 (trimmed). Always the same male Edge voice."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    clean = " ".join(str(text).split())
    if not clean:
        raise ValueError("Voiceover text empty hai.")

    last_err = None
    for attempt in range(1, TTS_RETRIES + 1):
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
            asyncio.run(_tts_async(clean, output_path, rate or VOICE_RATE, pitch or VOICE_PITCH))
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                _trim_silence(output_path)
                return output_path
            raise RuntimeError("Edge TTS ne valid audio nahi di.")
        except Exception as e:
            last_err = e
            print(f"Edge TTS ({VOICE}) attempt {attempt}/{TTS_RETRIES} failed: {e}")
            time.sleep(1.5 * attempt)

    # Do NOT fall back to another (female) voice - fail loudly instead.
    raise RuntimeError(f"Male voice ({VOICE}) generate nahi hui: {last_err}")


# ------------------------------------------------------------ synth SFX ----
def _norm(x, peak=0.9):
    m = float(np.max(np.abs(x))) or 1.0
    return (x / m * peak).astype(np.float32)


def _t(dur):
    return np.arange(int(SR * dur), dtype=np.float64) / SR


def _svf_bandpass_sweep(noise, f_start, f_end, q=2.0):
    """State-variable band-pass with a moving centre frequency (pure numpy loop)."""
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


def synth_whoosh(dur=0.45, rising=True, rng=None):
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
    """Cinematic low hit: falling sine + short noise thump, soft-clipped."""
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
    """
    scene_timings: [(start_sec, voice_duration), ...]
    Placement:
      - hook: deep boom at 0.0
      - every cut: whoosh (alternating direction for variety)
      - twist scene (second-last): riser leading in + boom on the reveal
      - last scene (CTA): soft ding + pop
    """
    rng = np.random.default_rng(seed if seed is not None else random.randrange(1 << 30))
    n = int(SR * (total_duration + 1.5))
    track = np.zeros(n, dtype=np.float32)

    whoosh_a = synth_whoosh(0.42, True, rng)
    whoosh_b = synth_whoosh(0.42, False, rng)
    boom = synth_boom(1.0, rng)
    riser = synth_riser(1.0, rng)
    ding = synth_ding()
    pop = synth_pop()

    _place(track, boom, 0.0, 0.85)

    count = len(scene_timings)
    for i, (start, _dur) in enumerate(scene_timings):
        if i == 0:
            continue
        if i == count - 2 and count >= 4:
            _place(track, riser, max(0.0, start - 1.0), 0.55)
            _place(track, boom, start, 0.6)
            continue
        if i == count - 1:
            _place(track, ding, start + 0.02, 0.5)
            _place(track, pop, max(0.0, start - 0.04), 0.4)
            continue
        w = whoosh_a if i % 2 else whoosh_b
        _place(track, w, start - 0.12, 0.42)

    track = np.tanh(track * 1.1)
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
    Returns the path of the finished master (WAV; moviepy encodes AAC once).
    Chain:
      voice -> EQ + compressor (clear, forward)      \
      bgm   -> looped, ducked by the voice (sidechain) }-> amix -> loudnorm -14 LUFS -> limiter
      sfx   -> synthesized track                     /
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

    voice_chain = (
        "[0:a]highpass=f=90,"
        "equalizer=f=250:t=q:w=1:g=-2,"
        "equalizer=f=3200:t=q:w=1:g=3,"
        "acompressor=threshold=0.09:ratio=3.5:attack=6:release=90:makeup=2.5,"
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
