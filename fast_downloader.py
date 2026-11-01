"""
fast_downloader.py — Telethon SMART PARALLEL downloader
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Premium Rate Limits මගහැරීමට Global Flood Lock සහ 
වේගය යළි පිහිටුවන (Speed Auto-Recovery) බුද්ධිමත් වේග පාලකයක් ඇතුළත් කර ඇත.
"""
import os
import time
import logging
import asyncio
import random
import re
from typing import Optional, Callable, Dict

from telethon import TelegramClient
from telethon.tl.functions.upload import GetFileRequest
from telethon.tl.types import InputDocumentFileLocation
from telethon.errors import FloodWaitError, FileMigrateError

from config import PROGRESS_UPDATE_INTERVAL

logger = logging.getLogger("FastDL")

CHUNK_SIZE = 512 * 1024 
MAX_CONCURRENT_CHUNKS = 1  

class FastDownloader:

    def __init__(self):
        self._cancel_flags: Dict[str, bool] = {}

    def cancel(self, download_id: str):
        self._cancel_flags[download_id] = True

    def is_cancelled(self, download_id: str) -> bool:
        return self._cancel_flags.get(download_id, False)

    async def cleanup(self, download_id: str):
        self._cancel_flags.pop(download_id, None)

    def force_delete_file(self, file_path: str) -> bool:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                return True
            except Exception:
                return False
        return False

    def _get_file_location(self, message):
        if getattr(message, 'document', None):
            return InputDocumentFileLocation(
                id=message.document.id,
                access_hash=message.document.access_hash,
                file_reference=message.document.file_reference,
                thumb_size=""
            ), message.document.size
        return None, 0

    async def download(
        self,
        client: TelegramClient,
        message,
        save_path: str,
        download_id: str,
        progress_callback: Optional[Callable] = None,
        max_retries: int = 15,
    ) -> str:
        
        self._cancel_flags[download_id] = False

        doc_size = getattr(message.document, 'size', 0) if getattr(message, 'document', None) else 0
        
        # 5MB සීමාව 1MB දක්වා අඩු කර ඇත. දැන් 1MB ට වැඩි සියලුම files වේගවත් Parallel ක්‍රමයට යයි!
        if getattr(message, 'photo', None) or (doc_size > 0 and doc_size < 1 * 1024 * 1024):
            return await self._download_native(client, message, save_path, download_id, progress_callback, max_retries)

        location, total_size = self._get_file_location(message)

        if not location or total_size == 0:
            return await self._download_native(client, message, save_path, download_id, progress_callback, max_retries)

        last_update = [0.0]
        downloaded_bytes = 0

        def _prog(current_add: int):
            nonlocal downloaded_bytes
            downloaded_bytes += current_add
            if self.is_cancelled(download_id):
                raise asyncio.CancelledError("Cancelled")
            
            now = time.monotonic()
            if now - last_update[0] >= PROGRESS_UPDATE_INTERVAL or downloaded_bytes >= total_size:
                last_update[0] = now
                if progress_callback:
                    asyncio.create_task(progress_callback(downloaded_bytes, total_size))

        with open(save_path, "wb") as f:
            pass 

        file_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHUNKS)
        
        # ── Smart Flood Protection Variables ──
        global_flood_lock = asyncio.Lock()
        dynamic_pacing = [0.0]

        correct_dc = None
        first_chunk_data = None
        
        try:
            req = GetFileRequest(location=location, offset=0, limit=CHUNK_SIZE)
            first_chunk = await asyncio.wait_for(client(req), timeout=45.0)
            first_chunk_data = first_chunk.bytes
        except FileMigrateError as e:
            correct_dc = e.new_dc
        except Exception:
            return await self._download_native(client, message, save_path, download_id, progress_callback, max_retries)

        sender = None
        if correct_dc:
            try:
                sender = await asyncio.wait_for(client._borrow_exported_sender(correct_dc), timeout=45.0)
            except Exception:
                return await self._download_native(client, message, save_path, download_id, progress_callback, max_retries)

        async def fetch_chunk(offset: int):
            for attempt in range(max_retries):
                if self.is_cancelled(download_id):
                    raise asyncio.CancelledError("Cancelled")
                
                # Apply Dynamic Pacing
                if dynamic_pacing[0] > 0:
                    await asyncio.sleep(dynamic_pacing[0])

                if global_flood_lock.locked():
                    async with global_flood_lock:
                        pass
                    await asyncio.sleep(random.uniform(0.1, 0.4))

                try:
                    async with semaphore:
                        req = GetFileRequest(location=location, offset=offset, limit=CHUNK_SIZE)
                        # Timeout එක 60s දක්වා වැඩි කර ඇත
                        if sender:
                            result = await asyncio.wait_for(sender.send(req), timeout=60.0)
                        else:
                            result = await asyncio.wait_for(client(req), timeout=60.0)
                    
                    if not result.bytes:
                        return 
                        
                    async with file_lock:
                        with open(save_path, "r+b") as f:
                            f.seek(offset)
                            f.write(result.bytes)
                    
                    _prog(len(result.bytes))
                    
                    # Auto-Recovery: සාර්ථක නම් Speed එක ආයෙත් ටිකෙන් ටික වැඩි කරයි
                    if dynamic_pacing[0] > 0:
                        dynamic_pacing[0] = max(0.0, dynamic_pacing[0] - 0.02)

                    return

                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    if attempt == max_retries - 1:
                        raise RuntimeError(f"Chunk at {offset} failed after timeouts")
                    await asyncio.sleep(2 ** attempt)
                except Exception as e:
                    err_str = str(e).lower()
                    is_flood = isinstance(e, FloodWaitError) or "premium" in err_str or "wait of" in err_str
                    
                    if is_flood:
                        wait_secs = 12
                        if isinstance(e, FloodWaitError):
                            wait_secs = e.seconds
                        else:
                            m = re.search(r'wait of (\d+)', err_str)
                            if m: wait_secs = int(m.group(1))
                        
                        if not global_flood_lock.locked():
                            async with global_flood_lock:
                                total_wait = wait_secs + random.uniform(1.0, 2.5)
                                logger.warning(f"Rate Limit Hit! Pausing all chunks for {total_wait:.1f}s...")
                                await asyncio.sleep(total_wait)
                                # වේගය අඩු කරයි (උපරිම 2.0s)
                                dynamic_pacing[0] = min(2.0, dynamic_pacing[0] + 0.15)
                        else:
                            await asyncio.sleep(1)
                        continue

                    if attempt == max_retries - 1:
                        raise e
                    await asyncio.sleep(2 ** attempt)

        try:
            tasks = []
            start_offset = 0
            
            if first_chunk_data is not None:
                async with file_lock:
                    with open(save_path, "r+b") as f:
                        f.seek(0)
                        f.write(first_chunk_data)
                _prog(len(first_chunk_data))
                start_offset = CHUNK_SIZE

            if start_offset < total_size:
                for offset in range(start_offset, total_size, CHUNK_SIZE):
                    tasks.append(asyncio.create_task(fetch_chunk(offset)))

                if tasks:
                    await asyncio.gather(*tasks)
            
            return save_path
            
        except asyncio.CancelledError:
            self.force_delete_file(save_path)
            raise
        except Exception as e:
            self.force_delete_file(save_path)
            raise RuntimeError(f"Download failed: {e}")
        finally:
            if sender:
                await client._return_exported_sender(sender)

    async def _download_native(self, client, message, save_path, download_id, progress_callback, max_retries=10):
        last_update = [0.0]

        def _prog(current: int, total: int):
            if self.is_cancelled(download_id):
                raise asyncio.CancelledError("Cancelled")
            now = time.monotonic()
            if now - last_update[0] < PROGRESS_UPDATE_INTERVAL and current < total:
                return
            last_update[0] = now
            if progress_callback:
                asyncio.create_task(progress_callback(current, total))

        for attempt in range(max_retries):
            if self.is_cancelled(download_id):
                raise asyncio.CancelledError("Cancelled")
            try:
                path = await client.download_media(message, file=save_path, progress_callback=_prog)
                if not path:
                    raise RuntimeError("Native download_media returned no path")
                return path
            except asyncio.CancelledError:
                self.force_delete_file(save_path)
                raise
            except Exception as e:
                err_str = str(e).lower()
                if isinstance(e, FloodWaitError) or "premium" in err_str or "wait of" in err_str:
                    wait_secs = 15
                    if isinstance(e, FloodWaitError): wait_secs = e.seconds
                    else:
                        m = re.search(r'wait of (\d+)', err_str)
                        if m: wait_secs = int(m.group(1))
                    
                    wait_time = wait_secs + random.uniform(1.0, 3.0)
                    logger.warning(f"Native Rate Limit. Waiting {wait_time:.1f}s")
                    await asyncio.sleep(wait_time)
                    continue

                if attempt >= max_retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"Native Download failed after {max_retries} attempts")

_downloader: Optional[FastDownloader] = None

def get_downloader() -> FastDownloader:
    global _downloader
    if _downloader is None:
        _downloader = FastDownloader()
    return _downloader

async def init_download_engine() -> FastDownloader:
    dl = get_downloader()
    logger.info("FastDownloader (Smart Auto-Pacing & Recovery Edition) initialized!")
    return dl

async def close_download_engine():
    global _downloader
    _downloader = None
