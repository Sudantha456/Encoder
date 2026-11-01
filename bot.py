"""
bot.py — Telethon Video Encoder Bot
Video එකක් දාන්න → metadata preview → resolution (720p/480p/240p) තෝරන්න →
fast_downloader → FFmpeg (live progress) → fast_uploader
"""
import asyncio
import contextlib
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events, Button
from telethon.errors import MessageNotModifiedError, FloodWaitError

import config
from config import (API_ID, API_HASH, BOT_TOKEN, SESSION_NAME, ALLOWED_USERS,
                    PROGRESS_UPDATE_INTERVAL, DOWNLOAD_DIR, PRESETS, CODECS,
                    PRESET_INFO, CRF, AUDIO_BITRATE, MAX_UPLOAD_BYTES)
from fast_downloader import get_downloader, init_download_engine
from fast_uploader import get_uploader
from media import (VideoMeta, telegram_meta, probe_remote, ffprobe, apply_probe,
                   offered_targets, estimate_size, render_preview, short_side)
from settings_store import SettingsStore
from transcoder import EncState, transcode
