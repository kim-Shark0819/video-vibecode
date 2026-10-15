"""vpoc1 이미지 — 업로드 정규화 · 분석용 타일 · SD3.5 캐릭터 이미지.

PRD 5.2 · 5.3 · 10장 E8 · E13 · E14.

사용자가 캐릭터 이미지를 올렸으면 **생성하지 않고 그 이미지를 쓴다**(source=user).
웹툰 컷과 사용자 이미지는 영상 키프레임에 쓰지 않는다(PRD 5.1 필터 실측).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import secrets
import time

import boto3
from botocore.config import Config
from PIL import Image, ImageOps

from . import settings
from .errors import VpocError
from .store import STORE, character_key, upload_key, upload_meta_key

log = logging.getLogger("vpoc")

_RUNTIME = {"client": None}

# Converse 이미지 제한: 장당 3.75MB. 여유를 두고 3.2MB 아래로 다시 쓴다.
_TILE_MAX_BYTES = 3_200_000


def runtime():
    if _RUNTIME["client"] is None:
        _RUNTIME["client"] = boto3.client(
            "bedrock-runtime",
            region_name=settings.IMAGE_REGION,
            config=Config(
                read_timeout=settings.READ_TIMEOUT_S,
                connect_timeout=15,
                retries={"max_attempts": 1},
            ),
        )
    return _RUNTIME["client"]


# ---------------------------------------------------------------- 업로드


def normalize_upload(raw: bytes, name: str) -> tuple[bytes, int, int]:
    """EXIF 회전을 적용하고 RGB JPEG 로 다시 쓴다. 원래 이름은 따로 보관한다."""
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        raise VpocError(
            "images.normalize_upload",
            "FileTooLarge",
            f"파일이 너무 큽니다 ({len(raw) / 1024 / 1024:.1f}MB)."
            f" {settings.MAX_UPLOAD_BYTES // 1024 // 1024}MB 이하로 줄여 주세요.",
            413,
        )
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode not in ("RGB",):
                image = image.convert("RGB")
            # Converse 는 긴 변 8,000px 이하를 요구한다.
            if max(image.size) > 8000:
                ratio = 8000 / max(image.size)
                image = image.resize(
                    (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                    Image.LANCZOS,
                )
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=88, optimize=True)
            return buffer.getvalue(), image.width, image.height
    except VpocError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise VpocError(
            "images.normalize_upload",
            "UnreadableImage",
            f"이미지를 읽을 수 없습니다: {exc}",
            400,
        ) from exc


def save_upload(owner: str, pid: str, raw: bytes, *, name: str, kind: str, char_index: int) -> dict:
    jpeg, width, height = normalize_upload(raw, name)
    file_id = f"f_{secrets.token_hex(4)}"
    STORE.put_bytes(upload_key(owner, pid, file_id), jpeg, "image/jpeg")
    meta = {
        "fileId": file_id,
        "name": name,
        "kind": kind,
        "charIndex": char_index,
        "bytes": len(jpeg),
        "width": width,
        "height": height,
    }
    STORE.write_json(upload_meta_key(owner, pid, file_id), meta)
    return meta


# ---------------------------------------------------------------- 분석용 타일


def _fit_bytes(image: Image.Image) -> bytes:
    quality = 88
    while True:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= _TILE_MAX_BYTES or quality <= 45:
            return data
        quality -= 12


def tiles_for_analysis(jpeg: bytes) -> list[bytes]:
    """세로로 긴 웹툰 스트립을 높이 1,568px 단위 타일로 자른다(E14).

    모델이 긴 변 1,568px 로 줄여 보기 때문에, 세로 스트립을 통째로 보내면 얼굴·글씨가 뭉개진다.
    """
    with Image.open(io.BytesIO(jpeg)) as image:
        image = image.convert("RGB")
        if image.height <= settings.TILE_HEIGHT * 1.5:
            return [_fit_bytes(image)]
        out: list[bytes] = []
        top = 0
        while top < image.height:
            bottom = min(image.height, top + settings.TILE_HEIGHT)
            out.append(_fit_bytes(image.crop((0, top, image.width, bottom))))
            top = bottom
        return out


def panel_count(height: int) -> int:
    """tiles_for_analysis 가 이 높이의 이미지를 몇 장으로 자르는지(이미지를 읽지 않고 계산)."""
    if height <= settings.TILE_HEIGHT * 1.5:
        return 1
    return -(-int(height) // settings.TILE_HEIGHT)


def panel_jpeg(owner: str, pid: str, file_id: str, index: int, _cache: dict | None = None) -> bytes:
    """업로드 이미지의 index 번째 장면 조각(JPEG). 세로 웹툰은 1,568px 단위로 자른 조각이다."""
    cache = _cache if _cache is not None else {}
    tiles = cache.get(file_id)
    if tiles is None:
        tiles = tiles_for_analysis(STORE.get_bytes(upload_key(owner, pid, file_id)))
        cache[file_id] = tiles
    return tiles[min(max(0, index), len(tiles) - 1)]


def jpeg_for_reference(jpeg: bytes) -> str:
    """참고 이미지용 base64 — 짧은 변 512px 이상, 긴 변 4096px 이하(keyframe_base64 와 같은 규칙)."""
    with Image.open(io.BytesIO(jpeg)) as image:
        image = image.convert("RGB")
        if min(image.size) < 512:
            ratio = 512 / min(image.size)
            image = image.resize(
                (int(image.width * ratio) + 1, int(image.height * ratio) + 1), Image.LANCZOS
            )
        if max(image.size) > 4096:
            ratio = 4096 / max(image.size)
            image = image.resize((int(image.width * ratio), int(image.height * ratio)), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        return base64.b64encode(buffer.getvalue()).decode("ascii")


def pick_for_analysis(items: list[bytes]) -> tuple[list[bytes], int]:
    """12장을 넘으면 균등하게 골라낸다. 몇 장 중 몇 장을 봤는지 함께 돌려준다."""
    total = len(items)
    limit = settings.MAX_ANALYSIS_IMAGES
    if total <= limit:
        return items, total
    step = total / limit
    picked = [items[min(total - 1, int(index * step))] for index in range(limit)]
    return picked, total


def load_analysis_images(owner: str, pid: str, file_ids: list[str]) -> tuple[list[bytes], int]:
    collected: list[bytes] = []
    for file_id in file_ids:
        try:
            jpeg = STORE.get_bytes(upload_key(owner, pid, file_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("vpoc.images.missing file=%s err=%s", file_id, exc)
            continue
        collected.extend(tiles_for_analysis(jpeg))
    return pick_for_analysis(collected)


# ---------------------------------------------------------------- SD3.5


def generate_character_image(
    *,
    owner: str,
    pid: str,
    char_id: str,
    prompt: str,
    negative_prompt: str,
    seed: int,
) -> dict:
    """SD3.5 Large 로 캐릭터 이미지 1장. finish_reasons 가 null 이 아니면 저장하지 않는다."""
    body = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "mode": "text-to-image",
        "aspect_ratio": "16:9",
        "output_format": "jpeg",
        "seed": int(seed),
    }
    started = time.time()
    response = runtime().invoke_model(modelId=settings.IMAGE_MODEL, body=json.dumps(body))
    payload = json.loads(response["body"].read())
    elapsed = int((time.time() - started) * 1000)

    reasons = payload.get("finish_reasons") or [None]
    seeds = payload.get("seeds") or [seed]
    images = payload.get("images") or []
    if reasons[0] is not None or not images:
        log.info(
            "vpoc.call where=sd35 plan=- char=%s ms=%d ok=false err=%s",
            char_id,
            elapsed,
            reasons[0],
        )
        raise VpocError(
            "stability.invoke_model",
            "ImageFiltered",
            str(reasons[0] or "이미지가 반환되지 않았습니다."),
            502,
        )

    raw = base64.b64decode(images[0])
    image_id = f"img_{char_id}_{secrets.token_hex(3)}"
    key = character_key(owner, pid, char_id, image_id)
    STORE.put_bytes(key, raw, "image/jpeg")
    log.info("vpoc.call where=sd35 char=%s ms=%d ok=true bytes=%d", char_id, elapsed, len(raw))
    return {
        "imageId": image_id,
        "key": key,
        "seed": int(seeds[0]),
        "bytes": len(raw),
        "prompt": prompt,
    }


def keyframe_base64(key: str) -> str:
    """키프레임용 JPEG base64. Luma 입력은 최소 512x512 · 최대 4096x4096."""
    raw = STORE.get_bytes(key)
    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert("RGB")
        if min(image.size) < 512:
            ratio = 512 / min(image.size)
            image = image.resize(
                (int(image.width * ratio) + 1, int(image.height * ratio) + 1), Image.LANCZOS
            )
        if max(image.size) > 4096:
            ratio = 4096 / max(image.size)
            image = image.resize((int(image.width * ratio), int(image.height * ratio)), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=92)
        return base64.b64encode(buffer.getvalue()).decode("ascii")
