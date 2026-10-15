"""Luma Ray2 (Bedrock 비동기). 제출 · 조회 · 출력 찾기.

실측 교훈 (vpoc1 run-log):
  - clientRequestToken 은 [a-zA-Z0-9](-*[a-zA-Z0-9])* — 밑줄 금지
  - 빈 프롬프트는 API 검증을 통과해 과금 작업이 된다 → 제출 전에 막는다
  - 출력 파일명을 가정하지 않는다 → 출력 접두사를 나열해 .mp4 를 찾는다
"""
from __future__ import annotations

import re
import threading
from typing import Any

from . import settings, store
from .errors import Vpoc2Error, from_client_error

_client = None
_lock = threading.Lock()

THROTTLE_CODES = {"ThrottlingException", "ServiceQuotaExceededException", "TooManyRequestsException"}


def _bedrock():
    global _client
    with _lock:
        if _client is None:
            import boto3
            from botocore.config import Config

            _client = boto3.client("bedrock-runtime", region_name=settings.VIDEO_REGION,
                                   config=Config(read_timeout=60, retries={"max_attempts": 0}))
        return _client


def client_token(run_id: str, shot_id: str, attempt: int) -> str:
    raw = f"{run_id}-{shot_id}-{attempt}"
    token = re.sub(r"[^a-zA-Z0-9]+", "-", raw).strip("-")
    return token[:256]


def output_prefix(owner: str, run_id: str, shot_id: str, attempt: int) -> str:
    return f"{store.run_prefix(owner, run_id)}/shots/{shot_id}-{attempt}/"


class Throttled(Exception):
    pass


def submit(prompt: str, *, resolution: str, token: str, out_prefix: str) -> str:
    """invocationArn 을 돌려준다. 스로틀이면 Throttled (과금 없음 → 대기열로)."""
    if not prompt or not prompt.strip():
        raise Vpoc2Error("EmptyPrompt", "빈 프롬프트는 제출하지 않는다", where="luma.start_async_invoke", http=400)
    if resolution not in settings.USD_PER_SEC:
        raise Vpoc2Error("BadResolution", resolution, where="luma.start_async_invoke", http=400)
    bucket, _region = store.get_store().buckets[store.VIDEO]
    try:
        resp = _bedrock().start_async_invoke(
            modelId=settings.VIDEO_MODEL,
            clientRequestToken=token,
            modelInput={
                "prompt": prompt,
                "aspect_ratio": settings.ASPECT,
                "duration": f"{settings.SHOT_SECONDS}s",
                "resolution": resolution,
                "loop": False,
            },
            outputDataConfig={"s3OutputDataConfig": {"s3Uri": f"s3://{bucket}/{out_prefix}"}},
        )
    except Exception as exc:  # noqa: BLE001
        code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code")
        if code in THROTTLE_CODES:
            raise Throttled(code) from exc
        raise from_client_error(exc, "luma.start_async_invoke") from exc
    return resp["invocationArn"]


def poll(arn: str) -> dict[str, Any]:
    """{status: InProgress|Completed|Failed, failureMessage}"""
    try:
        resp = _bedrock().get_async_invoke(invocationArn=arn)
    except Exception as exc:  # noqa: BLE001
        raise from_client_error(exc, "luma.get_async_invoke") from exc
    return {"status": resp.get("status"), "failureMessage": resp.get("failureMessage")}


def find_output(out_prefix: str) -> str | None:
    keys = [k for k in store.get_store().list_keys(store.VIDEO, out_prefix) if k.lower().endswith(".mp4")]
    return keys[0] if keys else None
