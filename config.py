"""
config.py — Encoder bot settings
fast_downloader.py / fast_uploader.py දෙකත් මේ PROGRESS_UPDATE_INTERVAL එකම import කරනවා.
"""
import os

# ── Telegram ──────────────────────────────────────────────
API_ID = int(os.getenv("API_ID", ""))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SESSION_NAME = os.getenv("SESSION_NAME", "encoder_bot")

# හිස්නම් හැමෝටම, නැත්නම් "123,456" වගේ user id (comma separated)
ALLOWED_USERS = [int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()]

# ── 🔁 Progress update interval (තත්පර) — මෙතනින් වෙනස් කරන්න ──
PROGRESS_UPDATE_INTERVAL = float(os.getenv("PROGRESS_UPDATE_INTERVAL", "3"))

# ── Paths ─────────────────────────────────────────────────
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
SETTINGS_FILE = os.getenv("SETTINGS_FILE", "user_settings.json")

# ── Encoding ──────────────────────────────────────────────
DEFAULT_PRESET = "ultrafast"
DEFAULT_CODEC = "libx264"

PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
           "medium", "slow", "slower", "veryslow"]
CODECS = {"libx264": "H.264 (libx264)", "libx265": "H.265 (libx265)"}

PRESET_INFO = {
    "ultrafast": "⚡ වේගවත්ම • Quality අඩු • File size ඉතා විශාලයි",
    "superfast": "🚀 ඉතා වේගවත් • size විශාලයි",
    "veryfast":  "🏃 වේගවත් • size ටිකක් විශාලයි",
    "faster":    "💨 වේගවත් • සාමාන්‍ය ට වඩා හොඳයි",
    "fast":      "👍 සාමාන්‍ය ට වඩා වේගවත්",
    "medium":    "⚖️ FFmpeg default setting එක",
    "slow":      "🐌 මන්දගාමී • size අඩුයි • quality හොඳයි",
    "slower":    "🐢 ඉතා මන්දගාමී • size තව අඩුයි",
    "veryslow":  "🦥 මන්දගාමීම • උපරිම quality • අඩුම size",
}

RESOLUTIONS = [720, 480, 240]            # buttons වල පෙන්වන්නේ (short side)
CRF = {"libx264": 23, "libx265": 28}     # quality (අඩු = හොඳයි)
AUDIO_BITRATE = {720: 128, 480: 96, 240: 64}   # kbps

# 🛡 Size guard: output bitrate එක source එකේ bitrate එක (resolution අනුව scale කරලා) ඉක්මවන්නේ නෑ.
# සමහර වෙලාවට (ultrafast වගේ) 20MB file එක 70MB වෙන එක වළක්වනවා. False කළොත් pure CRF.
SIZE_GUARD = os.getenv("SIZE_GUARD", "1") == "1"
SIZE_GUARD_RATIO = float(os.getenv("SIZE_GUARD_RATIO", "0.7"))   # 0.7 = source bitrate එකෙන් 70% (≈30% කුඩා). අඩු කළොත් තවත් කුඩා
MIN_GUARD_KBPS = 120

MAX_PARALLEL_JOBS = int(os.getenv("MAX_PARALLEL_JOBS", "2"))
MAX_UPLOAD_BYTES = 2000 * 1024 * 1024    # bot upload limit ≈ 2GB

# Preview එකට metadata probe කරන bytes (head + tail)
PROBE_HEAD_CHUNKS = 6      # 6 × 512KB = 3MB
PROBE_TAIL_CHUNKS = 12     # 12 × 512KB = 6MB
