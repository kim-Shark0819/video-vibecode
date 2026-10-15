"""BytePlus Seedream 그림 생성 — 키 장면(러프 콘티 → 완성 그림)용 (설계자 2026-10-14).

Seedance 와 같은 ModelArk 키(settings.ARK_API_KEY)를 쓴다. 참고 이미지는 URL 이 아니라 본문 base64
(data URL)로 보낸다 — BytePlus 쪽에 이미지를 남기지 않는다. 결과도 b64 로 받아 우리 버킷에 둔다.

실측(2026-10-14): Seedream 5.0 Flash 1920x1080 약 11초, Pro 약 60~100초. 둘 다 참고 이미지(웹툰 컷)의
인물 · 화풍을 잘 따른다. `sequential_image_generation` 은 이 모델들이 받지 않는다.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import time
import urllib.error
import urllib.request

from PIL import Image

from . import settings
from .errors import VpocError

log = logging.getLogger("vpoc")

SIZE = "1920x1080"  # 16:9 — Seedance 의 시작 · 끝 장면으로 그대로 쓴다.


def _data_url(jpeg: bytes, longest: int = 1536) -> str:
    with Image.open(io.BytesIO(jpeg)) as image:
        image = image.convert("RGB")
        image.thumbnail((longest, longest))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def generate(prompt: str, references: list[bytes] | None = None, *, model: str, where: str = "seedream") -> bytes:
    """그림 1장(JPEG bytes). references 는 순서대로 "Image 1, 2, …" 가 된다(최대 10장)."""
    if not settings.ARK_API_KEY:
        raise VpocError(where, "NoApiKey", "Seedream 키(ARK_API_KEY)가 설정되지 않았습니다.", 400)
    body: dict = {
        "model": model,
        "prompt": prompt[:3000],
        "size": SIZE,
        "response_format": "b64_json",
        "watermark": False,
    }
    if references:
        body["image"] = [_data_url(item) for item in references[:10]]
    request = urllib.request.Request(
        settings.ARK_BASE_URL + "/images/generations",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + settings.ARK_API_KEY},
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = (json.loads(raw).get("error") or {}).get("message") or raw
        except ValueError:
            detail = raw
        raise VpocError(where, f"HTTP{exc.code}", str(detail)[:500], 502) from exc
    data = (payload.get("data") or [{}])[0]
    b64 = data.get("b64_json") or ""
    if not b64:
        raise VpocError(where, "NoImage", f"그림이 오지 않았습니다: {str(payload)[:300]}", 502)
    log.info(
        "vpoc.call where=%s model=%s ms=%d refs=%d",
        where,
        model,
        int((time.time() - started) * 1000),
        len(references or []),
    )
    raw_bytes = base64.b64decode(b64)
    with Image.open(io.BytesIO(raw_bytes)) as image:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=92)
        return buffer.getvalue()


def _filtered(exc: Exception) -> bool:
    text = str(getattr(exc, "message", "") or exc).lower()
    return "sensitive" in text or "safety" in text or "content" in text and "policy" in text


def generate_safe(prompt: str, references: list[bytes] | None = None, *, model: str, where: str = "seedream") -> bytes:
    """generate + 안전 필터 오탐 대응(설계자 2026-10-14 "에러가 다시는 나지 않도록").

    1) 그대로 2) 한 번 더(결과가 매번 달라 통과하기도 한다) 3) Claude 가 같은 장면을 무난한 말로 다시 쓴 문장
    4) 행동 묘사를 뺀 최소 문장(구도 · 인물 · 화풍 참고 이미지는 그대로). 필터가 아닌 오류는 바로 올린다.
    """
    from . import llm  # noqa: PLC0415 — 순환 import 피하기

    attempts = [prompt, prompt]
    last: Exception | None = None
    index = 0
    rewritten = False
    while index < len(attempts):
        try:
            return generate(attempts[index], references, model=model, where=where)
        except VpocError as exc:
            if not _filtered(exc):
                raise
            last = exc
            log.warning("vpoc.seedream.filtered where=%s step=%d", where, index + 1)
            if index == 1 and not rewritten:
                rewritten = True
                try:
                    better, _usage = llm.safe_image_prompt(prompt)
                    attempts.append(better)
                except Exception as rewrite_exc:  # noqa: BLE001
                    log.warning("vpoc.seedream.rewrite_failed err=%s", rewrite_exc)
                head = prompt.split("Scene:")[0].strip()
                attempts.append(head + " Scene: a calm, friendly moment; characters stand naturally.")
            index += 1
    raise last or VpocError(where, "Filtered", "그림을 만들지 못했습니다.", 502)
