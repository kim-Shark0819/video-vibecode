"""목 모드 공급자. 실제 모듈(llm · images · video)과 같은 함수 모양을 가진다.
모델을 부르지 않고, 비용 0, 검사를 통과하는 결과를 만든다. 로컬 개발 · 테스트 전용.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile
import time
from typing import Any

from . import compose, settings, store
from .errors import Vpoc2Error

# --- llm ---------------------------------------------------------------------


def _split_elements(command: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[,.!?\n]|그리고|하지만|그런데", command) if p.strip()]
    return parts[:8] or ["(명령 없음)"]


def interpret(command: str, user_characters: list[dict[str, Any]]) -> tuple[dict[str, Any], float, list[str]]:
    elements = [{"id": f"e{i}", "kind": "other", "textKo": t} for i, t in enumerate(_split_elements(command), 1)]
    given = [c for c in user_characters if c.get("name") or c.get("description")][:2]
    if not given:
        given = [{"name": "", "description": ""}]
    handles = ["the first figure", "the second figure"]
    chars = []
    for i, c in enumerate(given, 1):
        chars.append({
            "id": f"c{i}", "nameKo": c.get("name") or f"인물 {i}", "nameEn": None,
            "roleKo": "주인공" if i == 1 else "상대역",
            "kind": "human", "speciesEn": "person", "mustKeepEn": [],
            "appearanceKo": c.get("description") or "짧은 검은 머리, 단순한 재킷",
            "appearanceEn": "A person with short dark hair wearing a simple navy jacket and white sneakers",
            "handleEn": handles[i - 1],
        })
    cids = [c["id"] for c in chars]
    shots = []
    beats = [
        "The scene starts still, then the first figure slowly turns toward the camera",
        "The first figure starts walking forward, then pauses and looks up at the sky",
        "The first figure starts to smile softly, then lifts one hand in a small wave",
    ]
    for i in range(settings.SHOTS):
        shots.append({
            "id": f"s{i + 1}", "summaryKo": f"목 샷 {i + 1}",
            "elementIds": [e["id"] for j, e in enumerate(elements) if j % settings.SHOTS == i] or [elements[0]["id"]],
            "characters": cids[:1] if i != 1 or len(cids) == 1 else cids[:2],
            "framingEn": "Medium shot with the figure centered and soft background depth",
            "locationEn": "A quiet city street corner at dusk",
            "beatsEn": beats[i],
            "lightEn": "Warm streetlight mixed with blue dusk ambience",
            "detailEn": "Light mist drifting past the lamps",
            "cameraEn": "Slow push-in at eye level",
            "cameraKo": "천천히 다가가기(푸시 인)",
        })
    data = {
        "titleKo": (command.strip()[:20] or "목 영상"), "summaryKo": "목 모드 해석 결과",
        "style": "live_action", "settingKo": "해 질 녘 조용한 도시 골목",
        "settingEn": "A quiet city street corner at dusk with warm streetlights",
        "lookEn": "Muted teal and amber palette, soft contrast, light film grain, calm mood",
        "elements": elements, "characters": chars, "shots": shots,
    }
    return data, 0.0, compose.check_interpretation(data)


def revise_character(character: dict[str, Any], feedback_ko: str) -> tuple[dict[str, Any], float]:
    updated = dict(character)
    updated["appearanceKo"] = f"{character.get('appearanceKo', '')} ({feedback_ko})"
    return updated, 0.0


def appearance_from_image(image_jpeg: bytes, character: dict[str, Any]) -> tuple[str, float, list[str]]:
    return "A person with short dark hair in a navy jacket and white sneakers", 0.0, []


# --- images ------------------------------------------------------------------

def generate(prompt: str, negative: str) -> tuple[bytes, int, int]:
    from PIL import Image, ImageDraw

    seed = abs(hash(prompt + str(time.time()))) % 4294967294
    color = ((seed >> 16) % 200 + 30, (seed >> 8) % 200 + 30, seed % 200 + 30)
    img = Image.new("RGB", (512, 768), color)
    draw = ImageDraw.Draw(img)
    draw.ellipse((156, 120, 356, 320), fill=(235, 210, 190))
    draw.rectangle((136, 340, 376, 700), fill=(40, 50, 90))
    draw.text((20, 20), "MOCK", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue(), seed, 1


# --- video -------------------------------------------------------------------

class Throttled(Exception):
    pass


_MP4: dict[int, bytes] = {}


def _mock_mp4(index: int) -> bytes:
    if index in _MP4:
        return _MP4[index]
    import imageio_ffmpeg

    hue = (index * 120) % 360
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "o.mp4")
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi",
               "-i", f"testsrc2=size=960x540:rate=24:duration={settings.SHOT_SECONDS}",
               "-vf", f"hue=h={hue}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise Vpoc2Error("MockVideoFailed", proc.stderr[-500:], where="mock.video", http=500)
        with open(out, "rb") as f:
            _MP4[index] = f.read()
    return _MP4[index]


def submit(prompt: str, *, resolution: str, token: str, out_prefix: str) -> str:
    if not prompt.strip():
        raise Vpoc2Error("EmptyPrompt", "빈 프롬프트는 제출하지 않는다", where="luma.start_async_invoke", http=400)
    return f"mock|{time.time():.3f}|{out_prefix}"


def poll(arn: str) -> dict[str, Any]:
    _tag, started, prefix = arn.split("|", 2)
    if time.time() - float(started) < settings.MOCK_SHOT_SECONDS:
        return {"status": "InProgress", "failureMessage": None}
    s = store.get_store()
    key = prefix + "mockjob/output.mp4"
    if s.get_bytes(store.VIDEO, key) is None:
        m = re.search(r"/shots/s(\d+)-", prefix)
        s.put_bytes(store.VIDEO, key, _mock_mp4(int(m.group(1)) if m else 0), "video/mp4")
    return {"status": "Completed", "failureMessage": None}


def find_output(out_prefix: str) -> str | None:
    keys = [k for k in store.get_store().list_keys(store.VIDEO, out_prefix) if k.endswith(".mp4")]
    return keys[0] if keys else None
