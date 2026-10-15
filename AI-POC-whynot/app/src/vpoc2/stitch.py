"""샷 3개를 이어 15초 합본을 만든다. 스트림 복사 → 실패하면 재인코딩 1회 (vpoc1 E15)."""
from __future__ import annotations

import os
import subprocess
import tempfile

from . import store
from .errors import Vpoc2Error


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def concat(video_keys: list[str]) -> bytes:
    """영상 버킷의 MP4 키들을 순서대로 이어 붙인 MP4 바이트."""
    s = store.get_store()
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, key in enumerate(video_keys):
            got = s.get_bytes(store.VIDEO, key)
            if got is None:
                raise Vpoc2Error("ShotMissing", key, where="stitch", http=500)
            path = os.path.join(tmp, f"{i}.mp4")
            with open(path, "wb") as f:
                f.write(got[0])
            paths.append(path)
        listing = os.path.join(tmp, "list.txt")
        with open(listing, "w", encoding="utf-8") as f:
            for p in paths:
                f.write(f"file '{p}'\n")
        out = os.path.join(tmp, "final.mp4")
        base = [_ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing]
        attempts = [
            base + ["-c", "copy", "-movflags", "+faststart", out],
            base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-movflags", "+faststart", out],
        ]
        last = ""
        for cmd in attempts:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                with open(out, "rb") as f:
                    return f.read()
            last = proc.stderr[-1500:]
        raise Vpoc2Error("StitchFailed", last or "ffmpeg 실패", where="stitch", http=500)
