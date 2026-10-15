"""vpoc1 영상 경로 — PRD 5.1 · 6.9.

provider 경계는 `submit(job, caps)` 와 `poll(job)` 두 함수뿐이다. 2단계(Seedance 2.0)는
`CAPS` 에 항목을 더하고 같은 두 함수를 구현한다. 1단계에서 이 이상 추상화하지 않는다.

Luma Ray2 는 **비동기 전용**이다. 백그라운드 작업자를 두지 않고 화면 조회 때 상태를 묻는다.
파드가 재시작돼도 작업 상태는 Bedrock 이 들고 있으므로 이어진다.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from . import settings
from .errors import VpocError, now_iso
from .store import STORE, video_output_prefix

log = logging.getLogger("vpoc")

CAPS = {
    "luma-ray2": {
        "model": "luma.ray-v2:0",
        "region": "us-west-2",
        "maxPromptChars": 1200,
        "durations": [5, 9],
        "resolutions": ["540p", "720p"],
        "aspectRatios": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "9:21"],
        "keyframes": ["frame0", "frame1"],
        "maxRefImages": 0,
        "output": "s3",
        "usdPerSec": {"540p": 0.75, "720p": 1.5},
    },
}

PROVIDER = "luma-ray2"

# Throttling 과 실패로 보는 상태를 구분한다.
_THROTTLE_CODES = (
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceQuotaExceededException",
    "LimitExceededException",
)

_RUNTIME = {"client": None}
_RUNTIME_LOCK = threading.Lock()


def caps() -> dict:
    return CAPS[PROVIDER]


def runtime():
    with _RUNTIME_LOCK:
        if _RUNTIME["client"] is None:
            _RUNTIME["client"] = boto3.client(
                "bedrock-runtime",
                region_name=settings.VIDEO_REGION,
                # start/get_async_invoke 는 1초 안팎의 짧은 호출이다. 300초를 기다리면 끊긴 연결 하나에
                # "생성 시작" 요청이 5분 묶여 화면이 HTTP 504 를 받았다(운영 2026-10-13, ms=301352).
                # 짧게 끊고 다시 보낸다 — clientRequestToken 이 같아 두 번 만들어지지 않는다.
                config=Config(
                    read_timeout=20,
                    connect_timeout=10,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        return _RUNTIME["client"]


# StartAsyncInvoke 의 clientRequestToken 은 이 패턴을 만족해야 한다. 밑줄을 받지 않는다.
# 우리 planId(`pl_20260922_de38`)와 shotId(`shot_01`)는 밑줄을 쓰므로 그대로 넣으면
# 제출 전에 ValidationException 이 난다(2026-09-23 dev 에서 4샷 전부 실패).
TOKEN_PATTERN = re.compile(r"^[a-zA-Z0-9](-*[a-zA-Z0-9])*$")
_TOKEN_UNSAFE = re.compile(r"[^a-zA-Z0-9]+")


def client_token(plan_id: str, shot_id: str, attempt: int, gen_id: str = "") -> str:
    """중복 제출 멱등 키. 원장도 이 키로 한 번만 계상한다.

    같은 (planId, genId, shotId, attempt) 는 항상 같은 값이 나와야 한다 — 두 조회가 동시에
    같은 샷을 제출해도 Bedrock 쪽 작업이 하나로 합쳐지고 원장도 한 번만 계상된다.

    **genId 가 없으면 회차를 구분할 수 없다.** ③을 고쳐 다시 승인했을 때 1차와 같은 토큰이
    나와 Bedrock 이 1차 invocation 을 돌려주고 옛 영상이 다시 붙었다
    (2026-09-23, docs/vpoc1/fix-regen-reload.md). genId 는 generate 가 회차마다 새로 만든다.
    """
    raw = f"{plan_id}-{gen_id}-{shot_id}-{attempt}" if gen_id else f"{plan_id}-{shot_id}-{attempt}"
    token = _TOKEN_UNSAFE.sub("-", raw).strip("-")[:256].rstrip("-")
    if not TOKEN_PATTERN.match(token):
        raise VpocError(
            "luma.client_token",
            "BadClientRequestToken",
            f"멱등 키를 만들 수 없습니다: {raw!r} → {token!r}",
            500,
        )
    return token


def model_input(prompt: str, *, keyframe_jpeg_b64: str = "", resolution: str = "") -> dict:
    # 화질은 ③에서 사용자가 고른다(540p · 720p). 길이는 Luma 상한 5초로 고정(설계자 2026-10-07).
    body: dict = {
        "prompt": prompt,
        "aspect_ratio": settings.ASPECT,
        "duration": settings.DURATION,
        "resolution": resolution or settings.RESOLUTION,
        "loop": False,
    }
    if keyframe_jpeg_b64:
        body["keyframes"] = {
            "frame0": {
                "type": "image",
                "source": {"bytes": keyframe_jpeg_b64},
            }
        }
    return body


def submit(
    *,
    owner: str,
    project_id: str,
    plan_id: str,
    shot_id: str,
    attempt: int,
    prompt: str,
    keyframe_jpeg_b64: str = "",
    gen_id: str = "",
    resolution: str = "",
) -> dict:
    """StartAsyncInvoke. 즉시 반환한다. 제출 성공 시점이 과금 시점이다."""
    limit = caps()["maxPromptChars"]
    if not prompt.strip():
        # 이 가드를 빼면 안 된다. 빈 프롬프트는 Bedrock API 검증을 **통과**하고 비동기 작업으로
        # 들어간다. 1초 뒤 failureMessage "prompt is required" 로 Failed 가 되는데, 그 시점에는
        # 이미 제출된 작업이라 우리 원장은 보수적으로 계상한다. 로컬에서 먼저 막는다.
        # (2026-09-23 실측: invocationArn mm0oexhd8y31)
        raise VpocError(
            "luma.start_async_invoke",
            "EmptyPrompt",
            "프롬프트가 비어 있습니다. 빈 프롬프트는 AWS 가 거절하지 않고 작업으로 받아들이므로"
            " 제출하기 전에 막았습니다.",
            400,
        )
    if len(prompt) > limit:
        # R4: 자르지 않는다.
        raise VpocError(
            "luma.start_async_invoke",
            "PromptOverBudget",
            f"프롬프트가 {limit}자를 넘습니다 ({len(prompt)}자). 자르지 않고 제출을 멈췄습니다.",
            400,
        )

    prefix = video_output_prefix(owner, project_id, plan_id, shot_id, attempt, gen_id)
    token = client_token(plan_id, shot_id, attempt, gen_id)
    started = time.time()
    try:
        response = runtime().start_async_invoke(
            clientRequestToken=token,
            modelId=settings.VIDEO_MODEL,
            modelInput=model_input(prompt, keyframe_jpeg_b64=keyframe_jpeg_b64, resolution=resolution),
            outputDataConfig={
                "s3OutputDataConfig": {"s3Uri": f"s3://{settings.VIDEO_BUCKET}/{prefix}"}
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        log.info(
            "vpoc.call where=luma.start plan=%s shot=%s ms=%d ok=false err=%s",
            plan_id,
            shot_id,
            int((time.time() - started) * 1000),
            code,
        )
        if code in _THROTTLE_CODES:
            # E4: 대기로 두고 다음 조회 때 재제출한다. 과금 없음.
            raise Throttled(code, str(exc.response.get("Error", {}).get("Message") or exc)) from exc
        raise

    log.info(
        "vpoc.call where=luma.start plan=%s shot=%s ms=%d ok=true",
        plan_id,
        shot_id,
        int((time.time() - started) * 1000),
    )
    return {
        "provider": PROVIDER,
        "invocationArn": response["invocationArn"],
        "clientRequestToken": token,
        "outputPrefix": prefix,
        "submittedAt": now_iso(),
        "promptChars": len(prompt),
    }


class Throttled(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def error_probe() -> None:
    """AWS 원문 오류를 **과금 없이** 한 번 받아 본다 (테스트 페이지의 오류 시험).

    일부러 패턴을 어기는 멱등 키를 보낸다. Bedrock 은 이것을 API 검증 계층에서 거절하므로
    비동기 작업이 만들어지지 않는다 — 2026-09-23 dev 에서 같은 오류로 4건이 거절됐고
    ListAsyncInvokes 에 작업이 하나도 남지 않은 것으로 확인했다.

    빈 프롬프트를 쓰지 않는 이유: 그것은 검증을 통과해 작업으로 들어가고 1초 뒤 Failed 가 된다.
    즉 과금 위험이 있다.
    """
    runtime().start_async_invoke(
        clientRequestToken="vpoc-error-probe_invalid",
        modelId=settings.VIDEO_MODEL,
        modelInput=model_input(
            "Cinematic live-action film footage. A static camera watches an empty road at dawn."
        ),
        outputDataConfig={
            "s3OutputDataConfig": {
                "s3Uri": f"s3://{settings.VIDEO_BUCKET}/users/_vpoc1/error-probe"
            }
        },
    )


def poll(invocation_arn: str) -> dict:
    """GetAsyncInvoke. 상태와 실패 원문을 그대로 돌려준다."""
    response = runtime().get_async_invoke(invocationArn=invocation_arn)
    return {
        "status": response.get("status", ""),
        "failureMessage": response.get("failureMessage") or "",
        "submitTime": response.get("submitTime"),
        "endTime": response.get("endTime"),
    }


def invocation_id(invocation_arn: str) -> str:
    return str(invocation_arn or "").rsplit("/", 1)[-1]


def find_output_mp4(prefix: str, invocation_arn: str = "") -> dict | None:
    """그 작업의 출력 `.mp4` 를 찾는다. 파일명을 가정하지 않는다(PRD 5.1).

    Luma 는 `{prefix}/{invocationId}/output.mp4` 로 쓴다. **상위 접두사를 통째로 나열하지
    않는다** — 같은 접두사에 이전 회차의 mp4 가 남아 있으면 그것을 집어 옛 영상이 붙는다
    (2026-09-23, docs/vpoc1/fix-regen-reload.md). arn 을 주면 그 invocation 폴더만 본다.
    """
    scope = prefix
    ident = invocation_id(invocation_arn)
    if ident:
        scope = f"{prefix.rstrip('/')}/{ident}/"
    for item in STORE.list_keys(scope, bucket=settings.VIDEO_BUCKET):
        if item["key"].lower().endswith(".mp4"):
            return item
    return None


def video_url(key: str) -> str:
    # Luma 출력의 Content-Type 은 application/octet-stream 이다. 재생되도록 덮어쓴다.
    return STORE.presign(key, bucket=settings.VIDEO_BUCKET, content_type="video/mp4")
