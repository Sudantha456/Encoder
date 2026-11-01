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
                   offered_targets, estimate_size, render_preview, short_side, guard_kbps)
from settings_store import SettingsStore
from transcoder import EncState, transcode
from ui import (SpeedTracker, render_transfer, render_encode, human_size,
                human_size_short, human_time, human_speed, esc)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
logger = logging.getLogger("EncoderBot")

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
client.parse_mode = "html"
store = SettingsStore()
job_sem: Optional[asyncio.Semaphore] = None


@dataclass
class Job:
    id: str
    user_id: int
    chat_id: int
    message: object
    meta: VideoMeta
    status: object
    cancelled: bool = False
    started: bool = False
    proc: object = None


jobs = {}


# ───────────────────────── helpers ─────────────────────────
def allowed(uid: int) -> bool:
    return not ALLOWED_USERS or uid in ALLOWED_USERS


def cb(*parts) -> bytes:
    return ":".join(str(p) for p in parts).encode()


def safe_stem(name: str) -> str:
    stem = re.sub(r"[^\w\-. ]+", "_", os.path.splitext(name)[0]).strip(" ._") or "video"
    return stem[:80]


class Live:
    """Message එක throttle කරලා edit කරන helper (FloodWait / same-text safe)."""

    def __init__(self, msg, buttons=None):
        self.msg, self.buttons = msg, buttons
        self._busy, self._last = False, None

    async def set(self, text: str, force: bool = False):
        if text == self._last or (self._busy and not force):
            return
        self._busy = True
        try:
            await self.msg.edit(text, buttons=self.buttons)
            self._last = text
        except MessageNotModifiedError:
            self._last = text
        except FloodWaitError as e:
            logger.warning(f"edit FloodWait {e.seconds}s — skipped")
        except Exception as e:
            logger.warning(f"edit failed: {e}")
        finally:
            self._busy = False


# ───────────────────────── preview / settings panels ─────────────────────────
def preview_buttons(job: Job):
    cfg = store.get(job.user_id)
    row = []
    for t in offered_targets(job.meta):
        est = estimate_size(job.meta, t, cfg["preset"], cfg["codec"])
        label = f"{t}p ≈{human_size_short(est)}" if est else f"{t}p"
        row.append(Button.inline(label, cb("res", job.id, t)))
    return [row, [Button.inline("⚙️ Settings", cb("cfg", job.id)),
                  Button.inline("❌ Cancel", cb("x", job.id))]]


async def show_preview(job: Job):
    cfg = store.get(job.user_id)
    await job.status.edit(render_preview(job.meta, cfg["preset"], cfg["codec"]),
                          buttons=preview_buttons(job))


def settings_text(uid: int) -> str:
    s = store.get(uid)
    return "\n".join([
        "⚙️ <b>Encoding Settings</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🎚 Preset: <b>{s['preset']}</b>",
        f"   <i>{PRESET_INFO[s['preset']]}</i>",
        f"🎞 Codec: <b>{s['codec']}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "ultrafast → වේගවත්ම  |  veryslow → අඩුම size",
        "H.265 → size අඩුයි, encode වෙන්න වැඩි වෙලාවක් යනවා",
    ])


def settings_buttons(uid: int, job_id: str):
    s = store.get(uid)
    rows, row = [], []
    for p in PRESETS:
        row.append(Button.inline(("✅ " if p == s["preset"] else "") + p, cb("pre", job_id, p)))
        if len(row) == 3:
            rows.append(row)
            row = []
    rows.append([Button.inline(("✅ " if c == s["codec"] else "") + n, cb("cod", job_id, c))
                 for c, n in CODECS.items()])
    rows.append([Button.inline("🔙 Back" if job_id != "0" else "✖ Close", cb("back", job_id))])
    return rows


# ───────────────────────── commands ─────────────────────────
@client.on(events.NewMessage(pattern=r"^/(start|help)", incoming=True))
async def cmd_start(event):
    if not allowed(event.sender_id):
        return
    await event.reply(
        "👋 <b>Video Encoder Bot</b>\n\n"
        "🎬 Video එකක් එවන්න → metadata preview එක පෙනෙයි\n"
        "📐 720p / 480p / 240p තෝරන්න (≈ size පෙන්වනවා)\n"
        "⚡ High-speed download → FFmpeg encode → High-speed upload\n\n"
        "⚙️ Preset / Codec වෙනස් කරන්න: /settings"
    )


@client.on(events.NewMessage(pattern=r"^/settings", incoming=True))
async def cmd_settings(event):
    if not allowed(event.sender_id):
        return
    await event.reply(settings_text(event.sender_id), buttons=settings_buttons(event.sender_id, "0"))


# ───────────────────────── video received ─────────────────────────
def is_video(e) -> bool:
    m = e.message
    if not m.document or m.gif:
        return False
    return (m.document.mime_type or "").startswith("video/") or bool(m.video)


@client.on(events.NewMessage(incoming=True, func=is_video))
async def on_video(event):
    if not allowed(event.sender_id):
        return
    msg = event.message
    status = await event.reply("🔎 <b>Analyzing video…</b>")
    job = Job(id=uuid.uuid4().hex[:8], user_id=event.sender_id, chat_id=event.chat_id,
              message=msg, meta=telegram_meta(msg), status=status)
    jobs[job.id] = job

    workdir = Path(DOWNLOAD_DIR) / job.id
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        await probe_remote(client, msg, job.meta, str(workdir))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    await show_preview(job)


# ───────────────────────── callbacks ─────────────────────────
@client.on(events.CallbackQuery())
async def on_callback(event):
    try:
        action, job_id, arg = (event.data.decode().split(":", 2) + [""])[:3]
    except Exception:
        return
    job = jobs.get(job_id)
    uid = event.sender_id

    if job_id != "0" and not job:
        await event.answer("Job එක අවසන් / expire වෙලා.", alert=True)
        return
    if job and uid != job.user_id:
        await event.answer("මේක ඔයාගේ job එකක් නෙවෙයි.", alert=True)
        return

    if action == "res":
        if job.started:
            await event.answer("Already running…")
            return
        job.started = True
        await event.answer("Starting…")
        asyncio.create_task(run_job(job, int(arg)))

    elif action == "x":
        job.cancelled = True
        get_downloader().cancel(job.id)
        get_uploader().cancel(job.id)
        if job.proc:
            with contextlib.suppress(Exception):
                job.proc.kill()
        await event.answer("Cancelling…")
        if not job.started:
            jobs.pop(job.id, None)
            with contextlib.suppress(Exception):
                await job.status.edit("❌ <b>Cancelled</b>", buttons=None)

    elif action == "cfg":
        await event.answer()
        await event.edit(settings_text(uid), buttons=settings_buttons(uid, job_id))

    elif action in ("pre", "cod"):
        store.set(uid, **({"preset": arg} if action == "pre" else {"codec": arg}))
        await event.answer(f"{arg} ✔")
        with contextlib.suppress(MessageNotModifiedError):
            await event.edit(settings_text(uid), buttons=settings_buttons(uid, job_id))

    elif action == "back":
        await event.answer()
        if job and not job.started:
            await show_preview(job)
        else:
            with contextlib.suppress(Exception):
                await event.delete()


# ───────────────────────── main pipeline ─────────────────────────
async def run_job(job: Job, height: int):
    cancel_btn = [[Button.inline("❌ Cancel", cb("x", job.id))]]
    live = Live(job.status, cancel_btn)
    meta = job.meta
    cfg = store.get(job.user_id)
    preset, codec = cfg["preset"], cfg["codec"]
    est_size = estimate_size(meta, height, preset, codec)

    workdir = Path(DOWNLOAD_DIR) / job.id
    workdir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(meta.name)
    ext = os.path.splitext(meta.name)[1] or ".mp4"
    src, dst = workdir / f"in_{stem}{ext}", workdir / f"{stem}_{height}p.mp4"

    phase = ["dl"]
    t_start = time.monotonic()
    stats = {}
    dl, ul = get_downloader(), get_uploader()

    try:
        if job_sem.locked():
            await live.set("⏳ <b>Queued…</b>\nවෙන job ඉවර වෙනකම් ඉන්නවා.", force=True)
        async with job_sem:
            if job.cancelled:
                raise asyncio.CancelledError()

            # 1) DOWNLOAD ─────────────────────────────
            tr = SpeedTracker(total=meta.size)
            await live.set(render_transfer("dl", tr, meta.name), force=True)

            async def dl_cb(cur, tot):
                if phase[0] != "dl":
                    return
                tr.update(cur, tot)
                await live.set(render_transfer("dl", tr, meta.name))

            await dl.download(client, job.message, str(src), job.id, dl_cb)
            stats["dl_avg"], stats["dl_time"] = tr.avg_speed, tr.elapsed
            if job.cancelled:
                raise asyncio.CancelledError()

            # 2) REAL PROBE ───────────────────────────
            phase[0] = "probe"
            await live.set("🔍 <b>Analyzing downloaded file…</b>", force=True)
            if not apply_probe(meta, await ffprobe(str(src))):
                raise RuntimeError("Valid video stream එකක් හමු වුණේ නෑ.")

            # 3) ENCODE ───────────────────────────────
            phase[0] = "enc"
            st = EncState(duration=meta.duration, src_fps=meta.fps)

            async def enc_ticker():
                while True:
                    await live.set(render_encode(st, meta.name, height, preset, codec))
                    await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)

            # Audio: source එකේ bitrate එකට වඩා වැඩි කරන්නේ නෑ; AAC & අඩු නම් copy
            ab = AUDIO_BITRATE.get(height, 96)
            src_ab = meta.abitrate // 1000
            ac = meta.acodec == "aac" and 0 < src_ab <= ab
            if src_ab and not ac:
                ab = min(ab, src_ab)
            ticker = asyncio.create_task(enc_ticker())
            try:
                await transcode(
                    str(src), str(dst), st, job,
                    target=height, scale=short_side(meta) > height,
                    portrait=meta.height > meta.width,
                    preset=preset, codec=codec, crf=CRF[codec],
                    abitrate=ab, audio_copy=ac,
                    maxrate_kbps=guard_kbps(meta, height),
                )
            finally:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker
            stats["enc_time"], stats["enc_speed"] = st.elapsed, st.avg_speed
            with contextlib.suppress(OSError):
                os.remove(src)

            out_size = os.path.getsize(dst)
            if out_size > MAX_UPLOAD_BYTES:
                raise RuntimeError(f"Output {human_size(out_size)} — Telegram bot limit (≈2GB) ඉක්මවනවා.")

            # 4) UPLOAD ───────────────────────────────
            phase[0] = "ul"
            tr2 = SpeedTracker(total=out_size)
            await live.set(render_transfer("ul", tr2, dst.name), force=True)

            async def ul_cb(cur, tot):
                if phase[0] != "ul":
                    return
                tr2.update(cur, tot)
                await live.set(render_transfer("ul", tr2, dst.name))

            sent = await ul.upload_file(client, job.chat_id, str(dst), job.id, ul_cb)
            stats["ul_avg"], stats["ul_time"] = tr2.avg_speed, tr2.elapsed
            phase[0] = "done"

            caption = (f"🎬 <b>{esc(stem)}</b>\n"
                       f"📐 {height}p • 🎞 {codec} • ⚙️ {preset}\n"
                       f"📦 {human_size(out_size)} (original {human_size(meta.size)})")
            with contextlib.suppress(Exception):
                await client.edit_message(sent, caption)

            saved = (1 - out_size / meta.size) * 100 if meta.size else 0
            live.buttons = None
            await live.set("\n".join([
                "✅ <b>Done!</b>",
                f"<code>{esc(dst.name)}</code>",
                "━━━━━━━━━━━━━━━━━━━━",
                f"📦 <code>{human_size(meta.size)}</code> → <code>{human_size(out_size)}</code> "
                f"({'−' if saved >= 0 else '+'}{abs(saved):.0f}%)"
                + (f"  •  est ≈{human_size(est_size)}" if est_size else ""),
                f"📥 DL: <code>{human_speed(stats['dl_avg'])}</code> • <code>{human_time(stats['dl_time'])}</code>",
                f"🎬 Encode: <code>{stats['enc_speed']:.2f}x</code> • <code>{human_time(stats['enc_time'])}</code>",
                f"📤 UL: <code>{human_speed(stats['ul_avg'])}</code> • <code>{human_time(stats['ul_time'])}</code>",
                f"⏱ Total: <code>{human_time(time.monotonic() - t_start)}</code>",
            ]), force=True)

    except asyncio.CancelledError:
        phase[0] = "stop"
        live.buttons = None
        await live.set("❌ <b>Cancelled</b>", force=True)
    except Exception as e:
        phase[0] = "stop"
        logger.exception("job failed")
        live.buttons = None
        await live.set(f"⚠️ <b>Failed</b>\n<code>{esc(str(e)[-900:])}</code>", force=True)
    finally:
        await dl.cleanup(job.id)
        await ul.cleanup(job.id)
        shutil.rmtree(workdir, ignore_errors=True)
        jobs.pop(job.id, None)


async def main():
    global job_sem
    job_sem = asyncio.Semaphore(config.MAX_PARALLEL_JOBS)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    await client.start(bot_token=BOT_TOKEN)
    await init_download_engine()
    me = await client.get_me()
    logger.info(f"✅ @{me.username} running • update interval {PROGRESS_UPDATE_INTERVAL}s")
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
