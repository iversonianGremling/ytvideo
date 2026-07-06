import asyncio
import logging
import signal

logger = logging.getLogger("mpv")


class MpvStreamer:
    """Single mpv encoding process - one instance for the whole app.
    
    mpv handles yt-dlp URL resolution, format selection, and transcoding
    into browser-friendly h264+aac fMP4 in a single process.
    """

    def __init__(self):
        self._proc = None
        self._video_id = None
        self._lock = asyncio.Lock()

    async def start(self, video_id: str, quality: int = 720, start: float = 0):
        async with self._lock:
            await self._kill()
            self._video_id = video_id

            url = f"https://www.youtube.com/watch?v={video_id}"
            ytdl_fmt = (
                f"bestvideo[height<={quality}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
                f"bestvideo[height<={quality}]+bestaudio/"
                f"best[height<={quality}]/best"
            )

            cmd = [
                "mpv", url,
                "--no-terminal",
                "--no-config",
                "--really-quiet",
                f"--ytdl-format={ytdl_fmt}",
                "--ytdl-raw-options=format-sort=lang",
                "--o=-",
                "--of=mp4",
                "--ofopts=movflags=+frag_keyframe+empty_moov+default_base_moof",
                "--ovc=libx264",
                "--ovcopts=preset=ultrafast,tune=zerolatency,crf=23",
                "--oac=aac",
            ]
            if start > 0:
                cmd.append(f"--start={start}")

            logger.info(f"[mpv] start {video_id} q={quality} start={start}")
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            return self._proc

    async def _kill(self):
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                    await self._proc.wait()
                except ProcessLookupError:
                    pass
        self._proc = None

    @property
    def current_video(self):
        return self._video_id


mpv = MpvStreamer()
