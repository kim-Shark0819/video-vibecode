"""SD3.5 Large 캐릭터 이미지. finish_reasons 가 null 이 아니면 필터 → 시드만 바꿔 1회 재시도."""
from __future__ import annotations

import base64
import json
import random
import threading

from . import settings
from .errors import Vpoc2Error, from_client_error

_client = None
_lock = threading.Lock()


def _bedrock():
    global _client
    with _lock:
        if _client is None:
            import boto3
            from botocore.config import Config

            _client = boto3.client("bedrock-runtime", region_name=settings.IMAGE_REGION,
                                   config=Config(read_timeout=120, retries={"max_attempts": 0}))
        return _client


def _once(prompt: str, negative: str, seed: int) -> tuple[bytes | None, str | None]:
    body = {
        "prompt": prompt,
        "negative_prompt": negative,
        "mode": "text-to-image",
        "aspect_ratio": "2:3",
        "output_format": "jpeg",
        "seed": seed,
    }
    try:
        resp = _bedrock().invoke_model(modelId=settings.IMAGE_MODEL, body=json.dumps(body),
                                       contentType="application/json", accept="application/json")
    except Exception as exc:  # noqa: BLE001
        raise from_client_error(exc, "sd35.invoke_model") from exc
    out = json.loads(resp["body"].read())
    reasons = out.get("finish_reasons") or [None]
    if reasons[0] is not None:
        return None, str(reasons[0])
    return base64.b64decode(out["images"][0]), None


def generate(prompt: str, negative: str) -> tuple[bytes, int, int]:
    """(jpeg, seed, 호출 횟수). 두 번 다 필터에 걸리면 ImageFiltered."""
    calls = 0
    reason = None
    for _ in range(2):
        seed = random.randint(0, 4294967294)
        calls += 1
        data, reason = _once(prompt, negative, seed)
        if data is not None:
            return data, seed, calls
    raise Vpoc2Error("ImageFiltered", f"finish_reasons: {reason}", where="sd35.invoke_model", http=502)
