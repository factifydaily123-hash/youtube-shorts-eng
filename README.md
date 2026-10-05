# 🎬 AutoShorts AI: The Automated Faceless Video Generator

![Views](https://komarev.com/ghpvc/?username=SaarD00-AI-Youtube-Shorts-Generator&style=for-the-badge&color=blue)


**AutoShorts AI** is a Python pipeline that creates viral-style "Faceless" English YouTube Shorts (single channel) with English voiceover and English word-by-word captions, fully automatic. It handles the production chain: AI topic/script generation, voiceover generation, stock footage sourcing, and FFmpeg editing with transitions and avatar injection.

---

## ✨ Key Features

- **🧠 Intelligent Scriptwriting:** Uses **Google Gemini 2.0 Flash** to write engaging, "Edutainment" style scripts (Vox/Kurzgesagt style) with strict storytelling structures (Hook → Context → Mechanism → Twist).
- **🗣️ Voiceovers:** Generates English narration with `edge-tts` (default voice `en-US-GuyNeural`, change with `TTS_VOICE`).
- **💬 Captions:** Word-by-word English captions that match the audio exactly.
- **🎞️ Dual-Visual System:** Automatically searches and downloads **two distinct stock videos** per scene from **Pexels**, creating a dynamic "A/B Split" visual style to maximize viewer retention.
- **✂️ Advanced FFmpeg Editing:**
- **Smart Trimming:** Syncs video perfectly to audio duration.
- **A/B Splitting:** Cuts every scene in half, switching visuals mid-sentence.
- **Pro Transitions:** Randomly applies `xfade` (fade, slide, wipes) between scenes.
- **Silence Removal:** Automatically trims dead air from AI voice generation.

- **🤖 Random Avatar Injection:** Automatically inserts a custom "Avatar/Mascot" video into a random middle scene to build channel brand identity.
- **🪟 Windows Ready:** Includes specific FFmpeg flags (`yuv420p`, `faststart`) to prevent corruption errors (`0x80004005`) on Windows Media Player.

---

## 📂 Project Structure

```text
Automated-YT-Shorts-AI/
│
├── assets/                  # Stores all media files
│   ├── audio_clips/         # Generated voiceovers (.wav)
│   ├── video_clips/         # Downloaded stock footage (.mp4)
│   ├── temp/                # Intermediate processing files
│   ├── final/               # 🏆 The Final Output Video lives here
│   └── avatar/              # ⚠️ PUT YOUR AVATAR VIDEO HERE
│       └── Professional_Girl_Animation_Video_Generation.mp4
│
├── modules/                 # Core Logic Modules
│   ├── brain.py             # AI Scriptwriter (Gemini)
│   ├── audio.py             # Voice generator (edge-tts)
│   ├── asset_manager.py     # Pexels Downloader (Dual-Visual logic)
│   └── composer.py          # FFmpeg Video Editor (Stitching & Transitions)
│
├── main.py                  # Entry point (Orchestrator)
└── requirements.txt         # Python dependencies

```

---

## 🛠️ Prerequisites

1. **Python 3.10+** installed.
2. **FFmpeg** installed and added to your system PATH.

- _Windows:_ `winget install ffmpeg` (or download from [ffmpeg.org](https://ffmpeg.org/download.html)).
- _Verify:_ Type `ffmpeg -version` in your terminal.

3. **API Keys:**

- **Google Gemini API Key** (Free tier available).
- **Pexels API Key** (Free).
- No Ngrok token is required for the default voiceover path. The current pipeline uses `edge-tts`.

---

## 🚀 Installation

### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/AutoShorts-AI.git
cd AutoShorts-AI

```

### 2. Install Dependencies

```bash
pip install -r requirements.txt

```

### 3. Environment Setup

Create the required folders and add your avatar:

1. Create folder: `assets/avatar`
2. Place your avatar video inside and name it: `avatars.mp4`

### 4. Configure API Keys

Copy `.env.example` to `.env` and fill in your key:

```bash
cp .env.example .env
```

Required:

- `GEMINI_API_KEY` for script generation
- `PEXELS_API_KEY` for stock video search/download

YouTube (single channel):

- `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`
- `YOUTUBE_PLAYLIST_ID` (optional)

Optional:

- `GEMINI_MODELS` to override the Gemini model list
- `TTS_VOICE` / `TTS_RATE` to change the English voice and speed

---

## 🎮 How to Run

### Generate Video

Run the main script:

```bash
python main.py

```

1. Enter a topic (e.g., _"The Mystery of the Pyramids"_).
2. Wait for the AI to write the script, generate audio, download stock footage, and edit the video.
3. The final video will be saved in `assets/final/final_short.mp4`.

---

## 🧩 Module Breakdown

### `brain.py` ( The Writer)

- **Input:** Topic string.
- **Logic:** Prompts Gemini to create an 8-9 scene JSON script. It asks for **two** visual keywords per scene (`visual_1`, `visual_2`) to enable the A/B split effect.

### `audio.py` (The Voice)

- **Input:** Text script.
- **Logic:** Generates MP3 voice clips with `edge-tts`.
- **Post-Processing:** Reads durations with `mutagen` so scenes can be synced to audio length.

### `asset_manager.py` (The Librarian)

- **Input:** Visual keywords + (optionally) local actor photos.
- **Cat-only clips:** the bot generates funny/cute cat scenarios (occasionally with one other animal like a duck/dog for comedy — the cat is always the main character). Every Pexels query is built from that scene's own script text and is force-checked to contain "cat", so clips stay on-topic scene-by-scene instead of drifting off-script.
- **Background music:** drop royalty-free playful/upbeat instrumental tracks into `assets/bgm/` — one is picked at random and mixed quietly under the whole final video.
- **Cat sound effects:** drop short royalty-free cat clips (meow, purr, etc) into `assets/sfx/` — one is picked at random per scene and mixed quietly under the voiceover, so the video has real cat audio, not just narration + music. Leave the folder empty to skip this (nothing breaks).

### `composer.py` (The Editor)

- **Input:** Audio files + Video files.
- **Logic:**
- **Scene Processing:** Cuts the scene duration in half. Plays Video A for the first half, Video B for the second half.
- **Avatar Injection:** Identifies a random "middle" scene (not hook/outro) and replaces the stock footage with your Avatar loop.
- **Stitching:** Merges all scenes using `xfade` transitions (wipes, slides).
- **Rendering:** Exports as `yuv420p` H.264 MP4 with `faststart` flags for maximum compatibility.

---

## ⚠️ Troubleshooting

**Q: The video is black or corrupt (0x80004005 error).**

- **Fix:** This is usually a Windows codec issue. The updated `composer.py` forces `pix_fmt='yuv420p'`. Try opening the file with VLC Media Player.

**Q: "Avatar file missing" error.**

- **Fix:** Ensure your folder structure is exactly `assets/avatar/avatars.mp4`.

**Q: The audio is silent or fails.**

- **Fix:** Check your internet connection and that `edge-tts` is installed from `requirements.txt`.

**Q: FFmpeg error "Exec format error" or "not found".**

- **Fix:** Ensure FFmpeg is installed and accessible from your command line.

---

## 📜 License

This project is open-source. Feel free to modify and build your own automation empire!
