"""Bedrock 호출을 한 곳으로 모으는 게이트웨이.

모든 모델 호출이 이 모듈을 지나가게 해서 두 가지를 보장한다.

1. 사용량 기록: 누가 어떤 모델을 얼마나 썼는지 남긴다. 호출 지점마다 기록을 넣으면
   빠뜨리는 곳이 생기므로, 기록은 호출과 같은 자리에서 한다.
2. 클라이언트 설정 일원화: 긴 텍스트 생성은 read_timeout을 늘려야 하고 재시도를 꺼야 한다.
   botocore 기본값(60초, 재시도 3회)으로는 생성 중인 요청을 끊고 다시 불러 시간과 비용이 배로 든다.

기록 실패가 생성 실패로 번지지 않도록, 사용량 기록은 예외를 삼킨다.
"""

import json
import logging
import os

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from . import graph_store, prompt_layers

logger = logging.getLogger("toon-change-ai-pj-app.gateway")

# 확산 모델(이미지·영상) 프롬프트 상한.
#
# providers 의 어댑터들이 본문을 만들 때 각자 4,000~9,000자로 자른다. 게이트웨이는 어느
# 어댑터가 쓰일지 알지만 그 상한값은 모르므로, **가장 좁은 값**을 쓴다. 이보다 길게 만들면
# 어떤 어댑터에서는 모델이 요청을 거부한다. 실제 프롬프트는 500~2,500자라 닿지 않는 값이고,
# 닿으면 prompt_layers 가 경고를 남긴다.
DIFFUSION_PROMPT_MAX_CHARS = int(os.getenv("DIFFUSION_PROMPT_MAX_CHARS", "4000"))

# 긴 텍스트 생성 타임아웃.
#
# 240초였고 그 값이 실제로 대본 생성을 깨뜨렸다. 대본 회차 하나의 상한이 16,000 출력
# 토큰(story_models.ROLE_SCRIPT_EPISODE)이고, Sonnet 계열이 초당 50~80 토큰을 내므로
# 분량을 다 쓰면 200~320초가 걸린다. 즉 상한에 가까운 회차는 응답을 받기 전에
# ReadTimeoutError 가 났다. 재시도도 없다(max_attempts=1) — 토큰 값을 두 번 물지 않으려는
# 의도이고, 그래서 한 번의 타임아웃이 곧 그 회차의 실패였다.
#
# 상한 토큰을 다 쓰는 경우를 기준으로 잡는다. 모델이 빨리 끝내면 이 값은 쓰이지 않는다.
LONG_READ_TIMEOUT = int(os.getenv("DOC_READ_TIMEOUT_SECONDS", "600"))
SHORT_READ_TIMEOUT = int(os.getenv("BEDROCK_READ_TIMEOUT_SECONDS", "60"))

# 이야기 분석(개요·상세·통합) 전용 읽기 제한.
#
# **왜 별도 값인가.** 이야기 분석은 `std` 프로필(60초)을 쓰고 있었고 개발계 18장 프로젝트의
# 통합 단계가 그 제한을 넘겨 실패했다(실측: job bcd93fb… lastError ReadTimeoutError,
# 저장된 단계 결과는 overview 18 + detail 33, integrate 0 — 통합에서만 끊겼다).
#
# 근거: 통합 단계 출력 상한이 24,000 토큰이었고 Sonnet 계열이 초당 50~80 토큰을 낸다.
# 상한을 다 쓰면 300~480초다.
# 여유를 두어 900초로 잡는다. AWS 는 대형 모델 호출에서 읽기 제한을 넉넉히(수천 초대에서
# 시작해 줄여 가며) 잡으라고 안내한다 — 60초는 그 권고와 정반대였다.
#   https://repost.aws/knowledge-center/bedrock-large-model-read-timeouts
#   https://docs.aws.amazon.com/nova/latest/userguide/using-converse-api.html
#
# ALB·웹 서버 제한을 함께 늘리지 않는다. 이 호출은 웹 요청이 아니라 지속 백그라운드 작업
# 안에서 일어나고, 웹 요청은 작업 id 를 돌려주고 상태만 조회한다.
STORY_READ_TIMEOUT = int(os.getenv("STORY_ANALYSIS_READ_TIMEOUT_SECONDS", "900"))

# 클라이언트 프로필: (connect_timeout, read_timeout, SDK 재시도 횟수)
#
# **재시도 값의 의미를 오해하지 않는다.** botocore 는 `max_attempts` 를 총 시도 N+1 로
# 바꾼다(실측: 3 -> total_max_attempts 4, 1 -> 2, 0 -> 1). 그래서 "재시도 없음" 은 1 이
# 아니라 **0** 이다. 예전 주석은 `max_attempts=1` 을 재시도 없음이라고 적었지만 실제로는
# 한 번 더 불렀고, 긴 생성에서 그 한 번이 토큰 값을 두 번 물게 만든다.
#
# 긴 생성 경로는 SDK 재시도를 쓰지 않는다. 재시도 판단은 호출자 한 곳에만 둔다 —
# 두 계층이 각자 재시도하면 호출 수가 곱해진다.
CLIENT_PROFILES: dict[str, tuple[int, int, int]] = {
    "std": (10, SHORT_READ_TIMEOUT, 3),
    "long": (10, LONG_READ_TIMEOUT, 0),
    "story": (10, STORY_READ_TIMEOUT, 0),
}

_runtime_clients: dict[str, object] = {}


class ImageBlocked(ValueError):
    """이미지 모델이 결과 없이 사유만 준 경우. **원문을 들고 다닌다.**

    `ValueError` 를 그대로 상속한다 — 기존 호출부가 `except ValueError` 로 잡고 있고, 문구로
    사유를 분류하는 코드도 있다. 그 둘을 깨지 않으면서 `finish_reasons` 원문을 잃지 않는 것이
    목적이다. 전에는 원문이 `logger.warning` 에만 남아 파드가 재시작되면 사라졌다.
    """

    def __init__(self, message: str, *, reason: str = "", model_id: str = "",
                 finish_reasons=None) -> None:
        super().__init__(message)
        self.reason = str(reason or "")
        self.model_id = str(model_id or "")
        # 벤더가 준 그대로. 문자열로 접거나 잘라 두지 않는다.
        self.finish_reasons = (
            list(finish_reasons) if isinstance(finish_reasons, (list, tuple))
            else ([finish_reasons] if finish_reasons is not None else [])
        )


def runtime(region: str, long_running: bool = False, *, profile: str = ""):
    """Bedrock 런타임 클라이언트. 용도에 따라 타임아웃과 재시도가 다르다.

    `profile` 을 주면 그것을 쓴다. 주지 않으면 기존 `long_running` 불리언을 따른다 —
    이 함수의 호출부가 이미지·영상 경로까지 여러 곳이라 그 계약을 깨지 않는다.
    """
    name = str(profile or ("long" if long_running else "std"))
    if name not in CLIENT_PROFILES:
        raise ValueError(f"알 수 없는 Bedrock 클라이언트 프로필입니다: {name}")
    cache_key = f"{region}:{name}"
    if cache_key not in _runtime_clients:
        connect_timeout, read_timeout, max_attempts = CLIENT_PROFILES[name]
        config = BotoConfig(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"max_attempts": max_attempts, "mode": "standard"},
        )
        _runtime_clients[cache_key] = boto3.client("bedrock-runtime", region_name=region, config=config)
    return _runtime_clients[cache_key]


def _with_frontend_prompt(purpose: str, body: dict, key: str = "prompt") -> dict:
    """확산 모델 본문의 프롬프트에 프론트엔드 프롬프트를 덧붙인다.

    **왜 게이트웨이에서 하는가.** 지시서 8-1은 "현재 시스템에서 사용 중인 모든 모델"에
    백엔드/프론트엔드 두 계층이 적용되어야 한다고 요구한다. 호출 지점마다 결합을 넣으면
    지금 열 곳이 넘고, 한 곳을 빠뜨려도 아무 오류가 나지 않는다. 그 모델만 조용히
    프론트엔드 프롬프트를 무시하고, 관리자는 화면에서 값을 저장했으니 적용됐다고 믿는다.

    게이트웨이는 **모든 Bedrock 호출의 유일한 통로**이고 이미 `purpose`를 받는다.
    prompt_layers 의 역할 id가 그 `purpose` 문자열과 같게 설계되어 있어, 여기 한 곳에서
    결합하면 빠뜨릴 경로가 구조적으로 없다.

    역할 목록에 없는 purpose 는 fragment() 가 빈 문자열을 돌려주므로 아무 일도 하지 않는다.
    """
    prompt_text = body.get(key)
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        return body
    merged = prompt_layers.compose_body(
        purpose, prompt_text, max_chars=DIFFUSION_PROMPT_MAX_CHARS
    )
    if merged == prompt_text:
        return body
    # 원본 dict 를 고치지 않는다. 호출부가 같은 본문을 재시도에 다시 쓰면 조각이 두 번 붙는다.
    return {**body, key: merged}


def _record(
    actor: str,
    model_id: str,
    kind: str,
    purpose: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    units: float = 0.0,
    region: str = "",
    ok: bool = True,
) -> None:
    if not actor:
        return
    graph_store.record_usage(
        actor,
        model_id,
        kind,
        purpose,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        units=units,
        region=region,
        ok=ok,
    )


# --------------------------------------------------------------------------- #
# 텍스트 (Converse)
# --------------------------------------------------------------------------- #
def _rejects_temperature(error: ClientError) -> bool:
    """이 오류가 'temperature를 받지 않는 모델'이라는 뜻인지 판단한다."""
    if error.response.get("Error", {}).get("Code") != "ValidationException":
        return False
    message = error.response.get("Error", {}).get("Message", "").lower()
    return "temperature" in message and ("deprecated" in message or "not supported" in message)


def converse(
    model_id: str,
    region: str,
    system_text: str,
    user_content: list[dict],
    max_tokens: int,
    temperature: float,
    actor: str = "",
    purpose: str = "text",
    long_running: bool = True,
    profile: str = "",
) -> tuple[str, bool]:
    """Converse 호출. (본문, 잘렸는지)를 돌려주고 토큰 사용량을 기록한다.

    user_content는 Converse의 content 블록 목록이라 텍스트와 이미지를 함께 넣을 수 있다.
    `profile` 로 클라이언트 프로필(타임아웃·SDK 재시도)을 고른다 — `CLIENT_PROFILES` 참고.
    """
    client = runtime(region, long_running=long_running, profile=profile)
    # 지시서 5-4 / 6-3 의 결합 순서를 여기 한 곳에서 지킨다.
    #   백엔드(system_text) -> 프론트엔드(관리자 설정) -> 사용자 입력(messages)
    # 사용자 입력은 messages 로 들어가므로 항상 마지막이다. compose_system 이 우선순위
    # 문장까지 붙여, 뒤에 온 지시를 더 강하게 따르는 경향을 상쇄한다.
    system_text = prompt_layers.compose_system(purpose, system_text)
    request = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": user_content}],
        "system": [{"text": system_text}],
    }
    try:
        response = client.converse(
            **request, inferenceConfig={"maxTokens": max_tokens, "temperature": temperature}
        )
    except ClientError as error:
        # 최신 모델은 temperature를 아예 받지 않는다. 실측으로 확인했다.
        #   global.anthropic.claude-opus-5   -> ValidationException
        #   global.anthropic.claude-opus-4-8 -> ValidationException
        #   global.anthropic.claude-sonnet-5 -> ValidationException
        #   "The model returned the following errors: `temperature` is deprecated for this model."
        # 모델 목록을 코드에 박아 두면 새 모델이 나올 때마다 또 고쳐야 하므로,
        # 이 오류만 잡아서 temperature 없이 한 번 다시 부른다.
        if not _rejects_temperature(error):
            _record(actor, model_id, "text", purpose, region=region, ok=False)
            raise
        logger.info("Model %s rejects temperature; retrying without it", model_id)
        try:
            response = client.converse(**request, inferenceConfig={"maxTokens": max_tokens})
        except (BotoCoreError, ClientError):
            _record(actor, model_id, "text", purpose, region=region, ok=False)
            raise
    except BotoCoreError:
        _record(actor, model_id, "text", purpose, region=region, ok=False)
        raise

    parts = response.get("output", {}).get("message", {}).get("content", [])
    text = "".join(part.get("text", "") for part in parts).strip()
    usage = response.get("usage") or {}
    truncated = response.get("stopReason") == "max_tokens"

    _record(
        actor,
        model_id,
        "text",
        purpose,
        input_tokens=usage.get("inputTokens") or 0,
        output_tokens=usage.get("outputTokens") or 0,
        region=region,
    )
    logger.info(
        "Converse model=%s purpose=%s in=%s out=%s stop=%s",
        model_id,
        purpose,
        usage.get("inputTokens"),
        usage.get("outputTokens"),
        response.get("stopReason"),
    )
    return text, truncated


def text_caller(model_id: str, region: str, actor: str, purpose: str):
    """documents 모듈이 쓰는 호출자. 모듈이 boto3를 직접 다루지 않게 한다."""

    def call(system_text: str, user_text: str, max_tokens: int, temperature: float) -> tuple[str, bool]:
        return converse(
            model_id,
            region,
            system_text,
            [{"text": user_text}],
            max_tokens,
            temperature,
            actor=actor,
            purpose=purpose,
        )

    return call


# --------------------------------------------------------------------------- #
# 이미지 (동기 invoke_model)
# --------------------------------------------------------------------------- #
def invoke_image(
    model: dict,
    body: dict,
    actor: str = "",
    purpose: str = "image",
    image_count: int = 1,
) -> list[bytes]:
    """이미지 모델을 호출해 PNG 바이트 목록을 돌려준다."""
    region = model.get("region") or "us-west-2"
    model_id = model["modelId"]
    client = runtime(region)
    body = _with_frontend_prompt(purpose, body)
    try:
        response = client.invoke_model(modelId=model_id, body=json.dumps(body))
        parsed = json.loads(response["body"].read())
    except (BotoCoreError, ClientError):
        _record(actor, model_id, "image", purpose, region=region, ok=False)
        raise

    images = parsed.get("images") or []
    if not images:
        _record(actor, model_id, "image", purpose, region=region, ok=False)
        # Stability 계열은 콘텐츠 필터에 걸리면 images 없이 finish_reason만 준다.
        # 원인별로 사용자가 무엇을 바꿔야 하는지 알려준다.
        raw_reasons = parsed.get("finish_reasons")
        if raw_reasons is None:
            raw_reasons = parsed.get("finishReason")
        reason = str(raw_reasons or "unknown")
        logger.warning("Image model returned no image model=%s reason=%s", model_id, reason)
        # 원문을 예외에 싣는다. 전에는 `logger.warning` 에만 남아 파드가 재시작되면 사라졌고,
        # 무엇이 막았는지 코드로 재구성해야 했다 (실측: 기준 이미지 15건 차단 조사).
        # 문구는 그대로 둔다 — 호출부가 이 문장으로 사유를 분류한다.
        if "input image" in reason:
            raise ImageBlocked(
                "참조 이미지가 안전 필터에 걸렸습니다. 다른 이미지를 사용해 주세요.",
                reason=reason, model_id=model_id, finish_reasons=raw_reasons)
        if "output image" in reason:
            raise ImageBlocked(
                "생성된 이미지가 안전 필터에 걸렸습니다. 같은 설명으로 다시 시도하면 다른 결과가 나올 수 있습니다.",
                reason=reason, model_id=model_id, finish_reasons=raw_reasons)
        if "prompt" in reason:
            raise ImageBlocked(
                "안전 필터가 프롬프트를 차단했습니다. 인물 묘사나 장면 설명을 다르게 표현해 주세요. "
                "나이·신체 묘사를 구체적으로 적으면 차단되는 경우가 있습니다.",
                reason=reason, model_id=model_id, finish_reasons=raw_reasons)
        raise ImageBlocked(
            f"이미지 모델이 결과를 반환하지 않았습니다(사유: {reason}).",
            reason=reason, model_id=model_id, finish_reasons=raw_reasons)

    _record(actor, model_id, "image", purpose, units=float(len(images)), region=region)
    import base64

    return [base64.b64decode(item) for item in images]


# --------------------------------------------------------------------------- #
# 영상 (비동기 start_async_invoke)
# --------------------------------------------------------------------------- #
def start_video(
    model: dict,
    model_input: dict,
    output_uri: str,
    actor: str = "",
    purpose: str = "video",
    seconds: float = 0.0,
) -> str:
    """영상 생성을 시작하고 invocationArn을 돌려준다.

    비동기라 응답 시점에는 토큰/과금 정보가 없다. 요청한 영상 길이를 단위로 기록한다.
    """
    region = model["region"]
    model_id = model["modelId"]
    client = runtime(region)
    # 영상 모델도 같은 결합을 받는다. Luma 는 부정 프롬프트가 없고 긍정형만 받으므로
    # 관리자 조각도 긍정형으로 적혀야 한다(그 안내는 화면의 frontendHint 에 있다).
    model_input = _with_frontend_prompt(purpose, model_input)
    try:
        response = client.start_async_invoke(
            modelId=model_id,
            modelInput=model_input,
            outputDataConfig={"s3OutputDataConfig": {"s3Uri": output_uri}},
        )
    except (BotoCoreError, ClientError):
        _record(actor, model_id, "video", purpose, region=region, ok=False)
        raise

    _record(actor, model_id, "video", purpose, units=float(seconds or 0.0), region=region)
    return response.get("invocationArn")


# --------------------------------------------------------------------------- #
# 집계
# --------------------------------------------------------------------------- #
def summarize_usage(since_iso: str | None = None, username: str = "") -> dict:
    """사용자별 / 모델별 사용량 요약.

    `username`을 주면 그 사용자의 행만 센다. 일반 사용자가 자기 사용량을 보는 화면에서
    쓴다. 필터를 호출부에 맡기지 않고 여기서 하는 이유는, byUser·byModel·totals·recent를
    모두 같은 행 집합으로 만들어야 하기 때문이다. 호출부가 응답에서 byUser만 골라내는
    방식이면 totals와 recent에는 다른 사람의 수치가 그대로 남는다.
    """
    rows = graph_store.usage_rows(since_iso)
    if username:
        rows = [row for row in rows if row.get("username") == username]

    by_user: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    totals = {"calls": 0, "inputTokens": 0, "outputTokens": 0, "videoSeconds": 0.0, "images": 0.0, "failed": 0}

    for row in rows:
        totals["calls"] += 1
        totals["inputTokens"] += row["inputTokens"]
        totals["outputTokens"] += row["outputTokens"]
        if row["kind"] == "video":
            totals["videoSeconds"] += row["units"]
        elif row["kind"] == "image":
            totals["images"] += row["units"]
        if not row["ok"]:
            totals["failed"] += 1

        user = by_user.setdefault(
            row["username"] or "(알 수 없음)",
            {
                "username": row["username"] or "(알 수 없음)",
                "calls": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "videoSeconds": 0.0,
                "images": 0.0,
                "failed": 0,
                "lastUsedAt": None,
                "models": {},
            },
        )
        model = by_model.setdefault(
            row["modelId"] or "(알 수 없음)",
            {
                "modelId": row["modelId"] or "(알 수 없음)",
                "kind": row["kind"],
                "calls": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "videoSeconds": 0.0,
                "images": 0.0,
                "failed": 0,
            },
        )
        for bucket in (user, model):
            bucket["calls"] += 1
            bucket["inputTokens"] += row["inputTokens"]
            bucket["outputTokens"] += row["outputTokens"]
            if row["kind"] == "video":
                bucket["videoSeconds"] += row["units"]
            elif row["kind"] == "image":
                bucket["images"] += row["units"]
            if not row["ok"]:
                bucket["failed"] += 1
        user["models"][row["modelId"]] = user["models"].get(row["modelId"], 0) + 1
        if not user["lastUsedAt"] or (row["at"] or "") > user["lastUsedAt"]:
            user["lastUsedAt"] = row["at"]

    for entry in by_user.values():
        entry["models"] = [
            {"modelId": model_id, "calls": calls}
            for model_id, calls in sorted(entry["models"].items(), key=lambda item: -item[1])
        ]

    return {
        "totals": totals,
        "byUser": sorted(by_user.values(), key=lambda item: -(item["inputTokens"] + item["outputTokens"])),
        "byModel": sorted(by_model.values(), key=lambda item: -item["calls"]),
        "recent": rows[:50],
        "rowCount": len(rows),
    }
