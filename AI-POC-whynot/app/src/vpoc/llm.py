"""vpoc1 Claude 호출 — PRD 5.3 · 부록 D.

도구 1개 + toolChoice 강제로 JSON 을 받고, 코드로 예산·금지어를 검사한다. 위반이면 위반
목록을 붙여 **1회만** 다시 요청한다(E9). 자유 텍스트 JSON 은 쓰지 않는다.

읽기 300초 · 재시도 0 (E10). 60초 기본 읽기 제한에서 긴 호출이 끊긴 사고가 있었다.

설계자 2026-10-13 "분석이 안 되는 에러를 원천 차단": 운영에서 300초 동안 응답이 없어
ReadTimeout 으로 분석이 실패했다. 이제 응답을 스트리밍으로 받고(토큰이 오는 동안 끊기지 않음),
혼잡 · 시간 초과 · 연결 끊김은 같은 모델로 한 번 더, 그래도 안 되면 다음 모델로 바꿔 부른다.
② 분석은 이 계정에서 가장 좋은 Claude(settings.ANALYZE_MODEL)로 한다.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from . import compose, settings
from .errors import VpocError

log = logging.getLogger("vpoc")

_RUNTIME = {"client": None, "broker": None}
_RUNTIME_LOCK = threading.Lock()


def _broker_client():
    """와이랩 계정 역할을 빌린 bedrock-runtime(임시, settings.BROKER_*). 자격 증명은 자동 갱신된다."""
    from botocore.credentials import RefreshableCredentials  # noqa: PLC0415
    from botocore.session import get_session  # noqa: PLC0415

    sts = boto3.client("sts", region_name=settings.AWS_REGION or settings.BROKER_REGION)

    def refresh() -> dict:
        creds = sts.assume_role(
            RoleArn=settings.BROKER_ROLE_ARN,
            RoleSessionName="whynot-vpoc",
            ExternalId=settings.BROKER_EXTERNAL_ID,
            DurationSeconds=3600,
        )["Credentials"]
        return {
            "access_key": creds["AccessKeyId"],
            "secret_key": creds["SecretAccessKey"],
            "token": creds["SessionToken"],
            "expiry_time": creds["Expiration"].isoformat(),
        }

    botocore_session = get_session()
    botocore_session._credentials = RefreshableCredentials.create_from_metadata(  # noqa: SLF001
        metadata=refresh(), refresh_using=refresh, method="sts-assume-role"
    )
    return boto3.Session(botocore_session=botocore_session).client(
        "bedrock-runtime",
        region_name=settings.BROKER_REGION,
        config=Config(read_timeout=settings.READ_TIMEOUT_S, connect_timeout=15, retries={"max_attempts": 0}),
    )


def _uses_broker(model: str) -> bool:
    return bool(settings.BROKER_ROLE_ARN) and any(mark in model for mark in settings.BROKER_MODEL_MARKERS)


def client_for(model: str):
    """이 모델을 부를 클라이언트. 와이낫 계정에서 아직 못 쓰는 모델만 와이랩 역할로 부른다."""
    if not _uses_broker(model):
        return runtime()
    with _RUNTIME_LOCK:
        if _RUNTIME["broker"] is None:
            _RUNTIME["broker"] = _broker_client()
        return _RUNTIME["broker"]


def runtime():
    if _RUNTIME["client"] is None:
        _RUNTIME["client"] = boto3.client(
            "bedrock-runtime",
            region_name=settings.TEXT_REGION,
            config=Config(
                read_timeout=settings.READ_TIMEOUT_S,
                connect_timeout=15,
                retries={"max_attempts": 0},
            ),
        )
    return _RUNTIME["client"]


# ---------------------------------------------------------------- 도구 스키마 (부록 D-3)

EMIT_STORY = {
    "name": "emit_story",
    "description": "Return the analyzed story, characters and up to three clarifying questions.",
    "inputSchema": {
        "json": {
            "type": "object",
            "required": ["story", "characters", "questions"],
            "properties": {
                "story": {
                    "type": "object",
                    "required": [
                        "titleKo",
                        "loglineKo",
                        "genre",
                        "settingKo",
                        "settingEn",
                        "styleSuggestion",
                    ],
                    "properties": {
                        "titleKo": {"type": "string"},
                        "loglineKo": {"type": "string"},
                        "genre": {"type": "array", "items": {"type": "string"}},
                        "settingKo": {"type": "string"},
                        "settingEn": {"type": "string"},
                        "styleSuggestion": {
                            "type": "string",
                            "enum": ["live_action", "anime"],
                        },
                        "notesKo": {"type": "string"},
                    },
                },
                "characters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "id",
                            "nameKo",
                            "source",
                            "appearanceKo",
                            "appearanceEn",
                            "handleEn",
                            "aiFilled",
                        ],
                        "properties": {
                            "id": {"type": "string"},
                            "nameKo": {"type": "string"},
                            "nameEn": {"type": ["string", "null"]},
                            "source": {"type": "string", "enum": ["user", "image", "ai"]},
                            "roleKo": {"type": "string"},
                            "blPosition": {"type": ["string", "null"]},
                            "appearanceKo": {"type": "string"},
                            "appearanceEn": {"type": "string"},
                            "handleEn": {"type": "string"},
                            "bodyKo": {"type": ["string", "null"]},
                            "bodyEn": {"type": ["string", "null"]},
                            "personalityKo": {"type": "string"},
                            "traitsKo": {"type": "string"},
                            "likesKo": {"type": "string"},
                            "aiFilled": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "questionKo", "optionsKo", "defaultIndex"],
                        "properties": {
                            "id": {"type": "string"},
                            "questionKo": {"type": "string"},
                            "optionsKo": {"type": "array", "items": {"type": "string"}},
                            "defaultIndex": {"type": "integer"},
                            "whyKo": {"type": "string"},
                        },
                    },
                },
            },
        }
    },
}

EMIT_SHOTS = {
    "name": "emit_shots",
    "description": "Return the shot plan for one short silent video.",
    "inputSchema": {
        "json": {
            "type": "object",
            "required": ["shots"],
            "properties": {
                "episodeTitleKo": {"type": "string"},
                "shots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "id",
                            "summaryKo",
                            "characters",
                            "locationEn",
                            "actionEn",
                            "cameraEn",
                            "cameraKo",
                        ],
                        "properties": {
                            "id": {"type": "string"},
                            "summaryKo": {"type": "string"},
                            "characters": {"type": "array", "items": {"type": "string"}},
                            "locationEn": {"type": "string"},
                            "actionEn": {"type": "string"},
                            "cameraEn": {"type": "string"},
                            "cameraKo": {"type": "string"},
                        },
                    },
                },
            },
        }
    },
}


ANALYZE_SYSTEM = """당신은 한국 웹툰·웹소설 IP 를 짧은 무음 영상으로 옮기는 기획자다.
사용자가 준 글과 이미지를 읽고 이야기·인물·배경을 정리한다.

반드시 지킬 것:
1. 사용자가 쓴 이름·외형·관계·사건은 바꾸지 않는다. 비어 있는 곳만 채우고, 채운 필드 이름을
   그 캐릭터의 aiFilled 배열에 적는다.
2. 입력에 이름이 없으면 지어내지 않는다. nameKo 는 "남자 1" 같은 표시명으로 두고 nameEn 은 null 로 둔다.
3. 이미지에서 본 외형을 쓰되, 글과 충돌하면 글이 우선한다. source 는 글에서 온 인물이 "user",
   이미지에서만 나온 인물이 "image", 당신이 새로 만든 인물이 "ai" 다.
4. appearanceEn 은 영어로만 쓴다. **사용자가 쓴 외형 표현(종, 색, 길이, 무늬 등)을 글자 그대로
   번역해 맨 앞에 두고**, 무엇인지(man, woman, cat, dog ...)로 시작한다 — 예: 사용자 "털이 짧고
   까만 고양이" → "A black short-haired cat with ...". 사람이면 머리(색·길이·모양), 얼굴 인상(사용자
   표현 그대로 — 예: Western features, East Asian features), 옷(색·종류), 눈에 띄는 소품을 담는다.
   동물·생물이면 털·깃털·피부의 색과 무늬, 털 길이, 몸집, 눈 색을 담고 사람용 표현(머리·옷)을 쓰지
   않는다. 사용자가 쓴 색은 단색 그대로 둔다(까만 고양이는 온몸이 black). 이름·상표·
   실존 인물·부정어(no/not/without 등)를 쓰지 않는다. 200자 이하.
5. handleEn 은 샷 문장에서 그 인물을 부를 짧은 영어 지칭이다(예: the white-haired racer).
   캐릭터끼리 겹치지 않게 한다. 60자 이하.
6. bodyKo·bodyEn 은 체형이다. 캐릭터 이미지에만 쓰고 영상 프롬프트에는 넣지 않는다. 80자 이하.
7. settingEn 은 장소·시간대·날씨·빛을 담은 영어 한 덩어리다. 이 계획의 모든 샷이 이 안에서
   장소를 고른다. 한 영상 안에서 배경을 바꾸지 않는다. 200자 이하.
8. 장르가 BL 이면 genre 에 넣는다. 공/수가 입력에 드러나면 blPosition 에 "공" 또는 "수" 로 적고,
   드러나지 않으면 null 로 둔다. 추측하지 않는다.
9. 질문은 영상 결과를 크게 바꾸는데 입력에 없는 것만, 최대 3개. 선택지는 2~4개이고 defaultIndex 로
   기본값을 정한다. 우선순위는 (1) 이번 영상에서 보여줄 장면 (2) 장소·시간대 (3) 인물 관계의
   현재 상태 순이다. 화풍은 묻지 않는다.
10. 입력이 실사·애니를 말하지 않으면 styleSuggestion 은 "live_action" 이다.
11. 글자 예산: titleKo 30 · loglineKo 120 · settingKo/settingEn 200 · personalityKo/traitsKo/likesKo
    각 120 · summaryKo 80. 캐릭터는 1~4명이다.

emit_story 도구로만 답한다."""


PLAN_SYSTEM = """당신은 짧은 무음 영상의 샷 계획을 쓰는 연출자다.

반드시 지킬 것:
1. 샷은 기본 4개(3~6개), 각 5초다. 한 샷에 한 동작만 담고, 고른 장면을 처음부터 끝까지 이어 준다.
2. 샷마다 인물은 0~2명이다. characters 에는 캐릭터 id 를 넣는다(없는 id 를 쓰면 안 된다).
3. actionEn 은 영어로만 쓴다. 인물은 이름이 아니라 handleEn 으로 지칭한다. 한글·캐릭터 이름·
   부정어(no/not/without/never/don't/doesn't)를 쓰지 않는다. 260자 이하.
4. locationEn 은 settingEn 안에서 고른 장소다. 배경을 바꾸지 않는다. 120자 이하.
5. cameraEn 은 긍정형 영어 카메라 지시다(예: static camera, slow push-in, tracking shot).
   80자 이하. cameraKo 는 쉬운 한국말이고 괄호 안에 용어를 넣는다
   (얼굴을 크게=클로즈업 / 허리 위까지=미디엄 샷 / 온몸과 배경까지=롱 샷 / 두 사람을 한 화면에=투 샷 /
    한 사람 어깨 너머로 상대를=오버 더 숄더 / 천천히 다가가기=푸시 인 / 천천히 멀어지기=풀 아웃 /
    옆으로 훑기=팬 / 인물을 따라가기=트래킹 / 카메라를 고정=고정 샷). 40자 이하.
6. summaryKo 는 사용자에게 보일 한 문장이다. 80자 이하.
7. 키스·노출·폭력은 사용자가 명시하지 않으면 넣지 않는다. 신체 접촉은 손·어깨·포옹까지다.
8. 실존 팀·선수·상표·방송사 이름을 쓰지 않는다.

emit_shots 도구로만 답한다."""


# 같은 모델로 한 번 더 해 볼 오류(일시적). 그 밖의 오류는 바로 다음 모델로 넘어간다.
_TRANSIENT_CODES = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelStreamErrorException",
    "ModelNotReadyException",
    "ModelTimeoutException",
    "ModelErrorException",
    "TooManyRequestsException",
}


# 이 계정에서 거부된 모델(AccessDenied · 미허용)은 10분 동안 건너뛴다 — 매 호출마다 실패를 겪지 않게.
_DENIED: dict[str, float] = {}
_DENY_TTL_S = 600


def _model_chain(primary: str) -> list[str]:
    chain: list[str] = []
    for model in [primary, *settings.TEXT_FALLBACK_MODELS, settings.TEXT_MODEL]:
        if model and model not in chain:
            chain.append(model)
    now = time.time()
    usable = [model for model in chain if now - _DENIED.get(model, 0) > _DENY_TTL_S]
    return usable or chain


# 스트리밍 권한(bedrock:InvokeModelWithResponseStream)이 없는 역할이 있다(운영 앱 역할, 2026-10-13).
# 한 번 거부되면 이 프로세스에서는 보통 호출(converse)로 바꾼다.
_STREAM = {"ok": True, "broker": True}


# 모델마다 받지 않는 설정이 있다(2026-10-14 실측): Opus 5.5 · Fable 5.x 는 toolChoice tool/any 를,
# GPT-6 Astra 는 temperature 를 거절한다. 거절되면 그 설정을 빼고 다시 부르고, 프로세스 동안 기억한다.
_NO_FORCED_TOOL: set[str] = set()
_NO_TEMPERATURE: set[str] = set()


def _request(model: str, system_text: str, blocks: list[dict], tool: dict, max_tokens: int) -> dict:
    inference: dict = {"maxTokens": max_tokens}
    if model not in _NO_TEMPERATURE:
        inference["temperature"] = 0.6
    choice = {"auto": {}} if model in _NO_FORCED_TOOL else {"tool": {"name": tool["name"]}}
    system = system_text
    if model in _NO_FORCED_TOOL:
        system += f"\n\nYou MUST answer by calling the {tool['name']} tool exactly once. No plain text."
    return {
        "modelId": model,
        "system": [{"text": system}],
        "messages": [{"role": "user", "content": blocks}],
        "inferenceConfig": inference,
        "toolConfig": {"tools": [{"toolSpec": tool}], "toolChoice": choice},
    }


def _adapt(model: str, exc: ClientError) -> bool:
    """설정 거절이면 기억하고 True(같은 모델로 다시). 아니면 False."""
    text = str(exc)
    if "tool_choice" in text or "toolChoice" in text:
        if model not in _NO_FORCED_TOOL:
            _NO_FORCED_TOOL.add(model)
            return True
    if "temperature" in text and model not in _NO_TEMPERATURE:
        _NO_TEMPERATURE.add(model)
        return True
    return False


def _converse_once(model: str, system_text: str, blocks: list[dict], tool: dict, max_tokens: int):
    """보통 호출 한 번(스트리밍을 못 쓸 때). (도구 입력 dict 또는 None, 텍스트, usage)."""
    response = client_for(model).converse(**_request(model, system_text, blocks, tool, max_tokens))
    payload = None
    text_dump = ""
    for block in (response.get("output", {}).get("message", {}) or {}).get("content", []) or []:
        if "toolUse" in block:
            payload = block["toolUse"].get("input")
        elif "text" in block:
            text_dump += block["text"]
    return payload, text_dump, response.get("usage", {}) or {}


def _stream_once(model: str, system_text: str, blocks: list[dict], tool: dict, max_tokens: int):
    """converse_stream 한 번. (도구 입력 dict 또는 None, 텍스트, usage)."""
    kind = "broker" if _uses_broker(model) else "ok"
    if not _STREAM[kind]:
        return _converse_once(model, system_text, blocks, tool, max_tokens)
    try:
        response = client_for(model).converse_stream(**_request(model, system_text, blocks, tool, max_tokens))
    except ClientError as exc:
        if "InvokeModelWithResponseStream" not in str(exc):
            raise
        log.warning("vpoc.call.no_stream_permission — 보통 호출로 바꾼다")
        _STREAM[kind] = False
        return _converse_once(model, system_text, blocks, tool, max_tokens)
    return _read_stream(response)


def _read_stream(response):
    """converse_stream 응답을 끝까지 읽는다. (도구 입력 dict 또는 None, 텍스트, usage)."""
    tool_json = ""
    text_dump = ""
    usage: dict = {}
    for event in response["stream"]:
        delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
        if "toolUse" in delta:
            tool_json += delta["toolUse"].get("input") or ""
        elif "text" in delta:
            text_dump += delta["text"]
        elif "metadata" in event:
            usage = event["metadata"].get("usage") or {}
    payload = None
    if tool_json:
        try:
            payload = json.loads(tool_json)
        except ValueError:
            text_dump = "(도구 입력 JSON 이 깨짐) " + tool_json[:1500]
    return payload, text_dump, usage


def _coerce(value, schema: dict):
    """도구 입력을 스키마 형태로 되살린다(설계자 2026-10-14 PlanMissing).

    모델이 배열 · 객체를 JSON 문자열로 돌려줄 때가 있다(예: shots 의 원소가 문자열). 그대로 쓰면
    shot.get(...) 에서 터졌다. 스키마가 array/object 인데 문자열이면 json 으로 풀고, 풀리지 않는 원소는 버린다.
    """
    kind = (schema or {}).get("type")
    if kind in ("array", "object") and isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [] if kind == "array" else {}
    if kind == "array":
        if not isinstance(value, list):
            return []
        item_schema = schema.get("items") or {}
        out = [_coerce(item, item_schema) for item in value]
        if item_schema.get("type") == "object":
            out = [item for item in out if isinstance(item, dict) and item]
        return out
    if kind == "object":
        if not isinstance(value, dict):
            return {}
        props = schema.get("properties") or {}
        return {key: (_coerce(val, props[key]) if key in props else val) for key, val in value.items()}
    return value


def _converse(
    *,
    system_text: str,
    blocks: list[dict],
    tool: dict,
    max_tokens: int,
    where: str,
    model: str = "",
) -> tuple[dict, dict]:
    """도구 하나로 JSON 을 받는다.

    모델마다: 설정 거절(toolChoice · temperature)은 그 설정을 빼고 바로 다시, 일시 오류(혼잡 · 시간 초과)는
    한 번 쉬고 다시, 그 밖의 오류(미허용 · 입력 오류 · 도구 미사용)는 다음 모델로 넘어간다.
    """
    last: BaseException | None = None
    for candidate in _model_chain(model or settings.TEXT_MODEL):
        transient_left = 1
        adapt_left = 2
        while True:
            started = time.time()
            try:
                payload, text_dump, usage = _stream_once(
                    candidate, system_text, blocks, tool, max_tokens
                )
            except ClientError as exc:
                code = str((exc.response.get("Error") or {}).get("Code") or type(exc).__name__)
                last = exc
                if code == "ValidationException" and adapt_left and _adapt(candidate, exc):
                    adapt_left -= 1
                    log.info("vpoc.call.adapt where=%s model=%s msg=%s", where, candidate, str(exc)[:200])
                    continue
                log.warning(
                    "vpoc.call.retry where=%s model=%s code=%s msg=%s",
                    where, candidate, code, str(exc)[:300],
                )
                if code in _TRANSIENT_CODES and transient_left:
                    transient_left -= 1
                    time.sleep(3)
                    continue
                if code in ("AccessDeniedException", "ResourceNotFoundException") and (
                    "InvokeModelWithResponseStream" not in str(exc)
                ):
                    _DENIED[candidate] = time.time()
                break  # 다음 모델로
            except (ReadTimeoutError, ConnectionClosedError, EndpointConnectionError) as exc:
                last = exc
                log.warning("vpoc.call.retry where=%s model=%s err=%s", where, candidate, type(exc).__name__)
                if transient_left:
                    transient_left -= 1
                    time.sleep(3)
                    continue
                break
            log.info(
                "vpoc.call where=%s model=%s ms=%d ok=%s in=%s out=%s",
                where,
                candidate,
                int((time.time() - started) * 1000),
                payload is not None,
                usage.get("inputTokens"),
                usage.get("outputTokens"),
            )
            if payload is None:
                last = VpocError(
                    where,
                    "SchemaViolation",
                    "모델이 도구를 쓰지 않았습니다. 출력 앞부분: " + text_dump[:2000],
                    502,
                )
                break
            payload = _coerce(payload, (tool.get("inputSchema") or {}).get("json") or {})
            return payload, {**usage, "model": candidate}
    if last is not None:
        raise last
    raise VpocError(where, "NoModel", "호출할 모델이 없습니다.", 500)


EMIT_IMAGE_PROMPT = {
    "name": "emit_prompt",
    "description": "Return the rewritten image prompt.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        }
    },
}

SAFE_PROMPT_SYSTEM = """You rewrite image-generation prompts that a strict automatic safety filter blocked by mistake.
Keep the same art style sentence, the same character (age range, gender, hair color, hairstyle, clothing,
colors, body type, expression) and the same setting. Replace every word or phrase that a filter could misread
as sexual, bondage, violence, injury, blood, nudity, minors, drugs, smoking or weapons with plainly neutral
wording (example: "hair tied up in a bun" -> "hair in a neat bun"; "slim" stays). No names of real people.
English only, at most 600 characters. Answer only with the emit_prompt tool."""


def safe_image_prompt(prompt: str) -> tuple[str, dict]:
    """필터에 걸린 그림 문장을 같은 내용의 무난한 문장으로 다시 쓴다(설계자 2026-10-13)."""
    payload, usage = _converse(
        system_text=SAFE_PROMPT_SYSTEM,
        blocks=[{"text": "Blocked prompt:\n" + prompt}],
        tool=EMIT_IMAGE_PROMPT,
        max_tokens=600,
        where="claude.safe_image_prompt",
    )
    text = str((payload or {}).get("prompt") or "").strip()
    if not text:
        raise VpocError("claude.safe_image_prompt", "SchemaViolation", "빈 문장", 502)
    return text[:900], usage


EMIT_BRIEF = {
    "name": "emit_brief",
    "description": "Return the scene notes for one video shot.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "mood": {"type": "string"},
                "background": {"type": "string"},
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "character": {"type": "string"},
                            "action": {"type": "string"},
                        },
                        "required": ["character", "action"],
                    },
                },
                "situation": {"type": "string"},
                "motion": {"type": "string"},
            },
            "required": ["mood", "background", "actions", "situation", "motion"],
        }
    },
}

BRIEF_SYSTEM = """You turn consecutive webtoon panels into scene notes for ONE short video shot.
The panels are in story order. The video must play through what they show from the first panel to
the last, as continuous motion — not a still image with blinking. Write English only.
- mood: overall atmosphere and emotional tone (<= 200 chars)
- background: where it happens — place, time of day, lighting, key props (<= 240 chars)
- actions: for EACH listed character in the shot, what they physically do and how it changes across
  the panels (<= 200 chars each). Use exactly the given character handles.
- situation: what is going on in the story at this moment and why it matters (<= 240 chars)
- motion: the shot as a timeline over the given seconds, e.g. "0-2s ..., 2-4s ..., 4-5s ...",
  with clear body movement, gestures and camera movement that follow the panels in order (<= 400 chars)
Never describe faces, hair, skin or clothing (reference images define how characters look).
No dialogue text, no names other than the handles. Answer only with the emit_brief tool."""


def scene_brief(
    panel_jpegs: list[bytes],
    character_jpegs: list[tuple[str, bytes]],
    *,
    shot_text: str,
    story_text: str,
    seconds: int,
) -> tuple[dict, dict]:
    """샷 하나가 맡은 웹툰 장면들을 보고 4가지(분위기 · 배경 · 인물별 행동 · 상황) + 움직임 순서를 쓴다."""
    blocks: list[dict] = [
        {
            "text": (
                f"Story so far: {story_text[:1500]}\n"
                f"Planned shot ({seconds}s): {shot_text[:1200]}\n"
                "Character handles in this shot: "
                + (", ".join(handle for handle, _ in character_jpegs) or "(none)")
                + f"\nThe next {len(panel_jpegs)} images are the webtoon panels for this shot, in order."
            )
        }
    ]
    for jpeg in panel_jpegs:
        blocks.append({"image": {"format": "jpeg", "source": {"bytes": _shrink(jpeg)}}})
    for handle, jpeg in character_jpegs:
        blocks.append({"text": f"Reference image of {handle} (for recognizing who is who only):"})
        blocks.append({"image": {"format": "jpeg", "source": {"bytes": _shrink(jpeg)}}})
    payload, usage = _converse(
        system_text=BRIEF_SYSTEM,
        blocks=blocks,
        tool=EMIT_BRIEF,
        max_tokens=1500,
        where="claude.scene_brief",
        model=settings.BRIEF_MODEL,
    )
    return payload or {}, usage


def brief_text(brief: dict) -> str:
    """scene_brief 결과를 Seedance 문장 뒤에 붙일 한 단락으로."""
    if not brief:
        return ""
    actions = "; ".join(
        f"{str(item.get('character') or '').strip()}: {str(item.get('action') or '').strip()}"
        for item in brief.get("actions") or []
        if isinstance(item, dict) and item.get("action")
    )
    parts = [
        ("Mood", brief.get("mood")),
        ("Background", brief.get("background")),
        ("Actions", actions),
        ("Situation", brief.get("situation")),
        ("Motion in order", brief.get("motion")),
    ]
    body = " ".join(f"{label}: {str(value).strip().rstrip('.')}." for label, value in parts if value)
    return (
        "Scene notes (from the webtoon panels; follow them over the plan text if they differ): "
        + body
        + " Keep everything moving continuously through the whole shot; do not hold a still frame."
    )


EMIT_BOARD = {
    "name": "emit_board",
    "description": "Return the keyframe storyboard for the whole video.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "keyframes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "summaryKo": {"type": "string"},
                            "sceneEn": {"type": "string"},
                            "characters": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["summaryKo", "sceneEn", "characters"],
                    },
                },
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "summaryKo": {"type": "string"},
                            "motionEn": {"type": "string"},
                        },
                        "required": ["summaryKo", "motionEn"],
                    },
                },
            },
            "required": ["keyframes", "segments"],
        }
    },
}

BOARD_SYSTEM = """You are a storyboard artist turning a webtoon into a short animated video.
Like a real animation team, first decide KEYFRAMES: one every {interval} seconds from 0s to {total}s
({count} keyframes), then the motion BETWEEN each pair ({segments} segments of {interval}s).
The webtoon panels are given in story order; the video must follow the story from the first panel to the
last, spreading the story evenly across the keyframes.
- keyframes[i].sceneEn (English, <= 420 chars): one frozen 16:9 frame — where each character stands, pose,
  gesture, facing direction, facial expression, background/setting, lighting, camera framing and angle.
  Refer to characters ONLY by their handles. Never describe faces, hair, skin or clothes.
- keyframes[i].summaryKo (Korean, <= 60 chars): what this frame shows.
- keyframes[i].characters: ids of characters visible in the frame.
- segments[i].motionEn (English, <= 420 chars): how everything moves from keyframe i to keyframe i+1 in
  {interval}s — body movement, gestures, camera move — as a short timeline. Continuous motion, no cuts.
- segments[i].summaryKo (Korean, <= 60 chars).
No dialogue text, no speech bubbles, no captions. Answer only with the emit_board tool."""


def storyboard(
    panel_jpegs: list[bytes],
    characters: list[dict],
    *,
    story_text: str,
    total_s: int,
    interval_s: int,
    count: int | None = None,
) -> tuple[dict, dict]:
    """웹툰 전체를 보고 키 장면(interval 초마다)과 그 사이 움직임을 정한다(설계자 2026-10-14).

    설계자 2026-10-19: 영상 1개(single) 방식은 키 장면 수를 직접 정한다(간격이 실수라 total//interval
    로 셀 수 없다). count 를 주면 그 수를 그대로 쓰고, 안 주면 기존대로 total_s // interval_s + 1.
    """
    if count is None:
        count = total_s // interval_s + 1
    count = max(2, int(count))
    system = BOARD_SYSTEM.format(
        interval=interval_s, total=total_s, count=count, segments=count - 1
    )
    cast = "\n".join(
        f"- id={item.get('id')} handle=\"{item.get('handleEn')}\" ({item.get('nameKo') or ''})"
        for item in characters
    ) or "(none)"
    blocks: list[dict] = [
        {
            "text": (
                f"Story: {story_text[:2500]}\nCharacters:\n{cast}\n"
                f"Video length {total_s}s, keyframe every {interval_s}s -> exactly {count} keyframes and "
                f"{count - 1} segments.\nThe next {len(panel_jpegs)} images are the webtoon panels in order."
            )
        }
    ]
    for jpeg in panel_jpegs:
        blocks.append({"image": {"format": "jpeg", "source": {"bytes": _shrink(jpeg)}}})
    payload, usage = _converse(
        system_text=system,
        blocks=blocks,
        tool=EMIT_BOARD,
        max_tokens=8000,
        where="claude.storyboard",
        model=settings.ANALYZE_MODEL,
    )
    keyframes = list(payload.get("keyframes") or [])
    segments = list(payload.get("segments") or [])
    if len(keyframes) != count or len(segments) != count - 1:
        fix = _retry_block([
            f"keyframes 는 정확히 {count}개, segments 는 정확히 {count - 1}개여야 합니다 "
            f"(받은 값 {len(keyframes)} / {len(segments)})."
        ])
        payload2, usage2 = _converse(
            system_text=system,
            blocks=blocks + [fix],
            tool=EMIT_BOARD,
            max_tokens=8000,
            where="claude.storyboard.retry",
            model=settings.ANALYZE_MODEL,
        )
        usage = _add_usage(usage, usage2)
        keyframes = list(payload2.get("keyframes") or keyframes)
        segments = list(payload2.get("segments") or segments)
    # 개수가 그래도 어긋나면 맞춘다(모자라면 마지막을 반복, 넘치면 자른다).
    while keyframes and len(keyframes) < count:
        keyframes.append(dict(keyframes[-1]))
    keyframes = keyframes[:count]
    while len(segments) < count - 1:
        segments.append({"summaryKo": "이어지는 움직임", "motionEn": "Smooth continuous motion to the next frame."})
    segments = segments[: count - 1]
    return {"keyframes": keyframes, "segments": segments}, usage


def _add_usage(first: dict, second: dict) -> dict:
    return {
        "inputTokens": (first.get("inputTokens") or 0) + (second.get("inputTokens") or 0),
        "outputTokens": (first.get("outputTokens") or 0) + (second.get("outputTokens") or 0),
        "model": second.get("model") or first.get("model") or "",
    }


def _retry_block(problems: list[str]) -> dict:
    return {
        "text": (
            "이전 응답이 규칙을 어겼습니다. 아래 위반을 모두 고쳐 같은 도구로 다시 답하세요.\n"
            + "\n".join(f"- {item}" for item in problems)
        )
    }


# ---------------------------------------------------------------- analyze


def _character_card_text(cards: list[dict]) -> str:
    if not cards:
        return "(사용자가 적은 캐릭터 카드 없음)"
    lines = []
    for index, card in enumerate(cards, 1):
        name = (card.get("name") or "").strip() or "(이름 없음)"
        desc = (card.get("description") or "").strip() or "(설명 없음)"
        images = len(card.get("files") or [])
        lines.append(f"{index}. 이름: {name} / 설명: {desc} / 첨부 이미지 {images}장")
    return "\n".join(lines)


def analyze(
    step1: dict, image_jpegs: list[bytes], *, total_images: int, _shrunk: int = 0
) -> tuple[dict, dict]:
    blocks: list[dict] = [
        {
            "text": (
                "다음은 사용자가 입력한 내용이다.\n\n"
                "[글]\n"
                + ((step1.get("text") or "").strip() or "(글 없음)")
                + "\n\n[사용자가 적은 캐릭터 카드]\n"
                + _character_card_text(step1.get("characters") or [])
                + (
                    f"\n\n[이미지] 첨부된 이미지 타일 {total_images}장 중 {len(image_jpegs)}장을 첨부한다."
                    if image_jpegs
                    else "\n\n[이미지] 없음"
                )
            )
        }
    ]
    for jpeg in image_jpegs:
        blocks.append({"image": {"format": "jpeg", "source": {"bytes": jpeg}}})

    try:
        payload, usage = _converse(
            system_text=ANALYZE_SYSTEM,
            blocks=blocks,
            tool=EMIT_STORY,
            max_tokens=8000,
            where="claude.analyze",
            model=settings.ANALYZE_MODEL,
        )
    except ClientError as exc:
        # 이미지가 모델 제한(크기 · 개수)에 걸린 경우: 작게 줄이고 장수를 반으로 해 한 번 더.
        if (
            (exc.response.get("Error") or {}).get("Code") != "ValidationException"
            or not image_jpegs
            or _shrunk >= 2
        ):
            raise
        log.warning("vpoc.analyze.shrink images=%d err=%s", len(image_jpegs), str(exc)[:300])
        smaller = [_shrink(jpeg) for jpeg in image_jpegs[: max(1, len(image_jpegs) // 2)]]
        return analyze(step1, smaller, total_images=total_images, _shrunk=_shrunk + 1)
    problems = check_story(payload)
    if problems:
        payload, usage2 = _converse(
            system_text=ANALYZE_SYSTEM,
            blocks=blocks + [_retry_block(problems)],
            tool=EMIT_STORY,
            max_tokens=8000,
            where="claude.analyze.retry",
            model=settings.ANALYZE_MODEL,
        )
        usage = _add_usage(usage, usage2)
        problems = check_story(payload)
        if problems:
            raise VpocError(
                "claude.analyze",
                "SchemaViolation",
                "모델 출력이 규칙을 두 번 어겼습니다:\n" + "\n".join(problems[:20]),
                502,
            )
    return normalize_story(payload), usage


def _shrink(jpeg: bytes) -> bytes:
    """긴 변 1,568px · 품질 80 으로 줄인다(모델 제한에 걸린 이미지용)."""
    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    with Image.open(io.BytesIO(jpeg)) as image:
        image = image.convert("RGB")
        image.thumbnail((1568, 1568))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=80, optimize=True)
        return buffer.getvalue()


def check_story(payload: dict) -> list[str]:
    problems: list[str] = []
    story = payload.get("story") or {}
    for field, limit in (
        ("titleKo", 30),
        ("loglineKo", 120),
        ("settingKo", 200),
        ("settingEn", 200),
    ):
        value = str(story.get(field) or "").strip()
        if not value:
            problems.append(f"story.{field} 가 비어 있습니다")
        elif len(value) > limit:
            problems.append(f"story.{field} 가 {limit}자를 넘습니다 ({len(value)}자)")
    setting_en = str(story.get("settingEn") or "")
    if compose.HANGUL.search(setting_en):
        problems.append("story.settingEn 에 한글이 있습니다")
    if story.get("styleSuggestion") not in ("live_action", "anime"):
        problems.append("story.styleSuggestion 은 live_action 또는 anime 여야 합니다")

    characters = payload.get("characters") or []
    if not 1 <= len(characters) <= 4:
        problems.append(f"characters 는 1~4명이어야 합니다 (현재 {len(characters)}명)")
    seen_ids: set[str] = set()
    handles: set[str] = set()
    for item in characters:
        char_id = str(item.get("id") or "").strip()
        if not char_id:
            problems.append("character.id 가 비어 있습니다")
        elif char_id in seen_ids:
            problems.append(f"character.id '{char_id}' 가 중복입니다")
        seen_ids.add(char_id)
        if item.get("source") not in ("user", "image", "ai"):
            problems.append(f"character {char_id}: source 는 user/image/ai 중 하나여야 합니다")
        if item.get("blPosition") not in ("공", "수", None, ""):
            problems.append(f"character {char_id}: blPosition 은 공/수/null 만 됩니다")
        handle = str(item.get("handleEn") or "").strip().lower()
        if handle and handle in handles:
            problems.append(f"character {char_id}: handleEn 이 다른 캐릭터와 겹칩니다")
        handles.add(handle)
        for field in ("personalityKo", "traitsKo", "likesKo"):
            value = str(item.get(field) or "")
            if len(value) > 120:
                problems.append(f"character {char_id}: {field} 가 120자를 넘습니다")
        problems.extend(compose.check_character(item))

    questions = payload.get("questions") or []
    if len(questions) > 3:
        problems.append(f"questions 는 최대 3개입니다 (현재 {len(questions)}개)")
    for question in questions:
        options = question.get("optionsKo") or []
        if not 2 <= len(options) <= 4:
            problems.append(
                f"question {question.get('id')}: 선택지는 2~4개여야 합니다 (현재 {len(options)}개)"
            )
        index = question.get("defaultIndex")
        if not isinstance(index, int) or not 0 <= index < max(1, len(options)):
            problems.append(f"question {question.get('id')}: defaultIndex 가 선택지 범위를 벗어납니다")
    return problems


def normalize_story(payload: dict) -> dict:
    story = dict(payload.get("story") or {})
    story.setdefault("notesKo", "")
    story["style"] = story.get("styleSuggestion") or "live_action"
    characters = []
    for item in payload.get("characters") or []:
        row = dict(item)
        row.setdefault("nameEn", None)
        row.setdefault("roleKo", "")
        row.setdefault("blPosition", None)
        row.setdefault("bodyKo", "")
        row.setdefault("bodyEn", "")
        row.setdefault("personalityKo", "")
        row.setdefault("traitsKo", "")
        row.setdefault("likesKo", "")
        row.setdefault("aiFilled", [])
        characters.append(row)
    questions = []
    answers = {}
    for item in payload.get("questions") or []:
        row = dict(item)
        row.setdefault("whyKo", "")
        questions.append(row)
        answers[row["id"]] = {"index": int(row.get("defaultIndex") or 0), "text": None}
    return {"story": story, "characters": characters, "questions": questions, "answers": answers}


# ---------------------------------------------------------------- plan


def _plan_blocks(step2: dict) -> list[dict]:
    story = step2.get("story") or {}
    characters = step2.get("characters") or []
    questions = step2.get("questions") or []
    answers = step2.get("answers") or {}

    lines = [
        "[이야기]",
        f"제목: {story.get('titleKo', '')}",
        f"줄거리: {story.get('loglineKo', '')}",
        f"장르: {', '.join(story.get('genre') or [])}",
        f"배경(한국어): {story.get('settingKo', '')}",
        f"배경(영어, 모든 샷이 이 안에서 장소를 고른다): {story.get('settingEn', '')}",
        f"화풍: {story.get('style', 'live_action')}",
        "",
        "[캐릭터]",
    ]
    for item in characters:
        lines.append(
            f"- id={item.get('id')} / 이름={item.get('nameKo')} / 역할={item.get('roleKo', '')}"
            f" / handleEn={item.get('handleEn')} / appearanceEn={item.get('appearanceEn')}"
            f" / 성격={item.get('personalityKo', '')} / 특징={item.get('traitsKo', '')}"
        )
    lines.append("")
    lines.append("[사용자 답]")
    if questions:
        for question in questions:
            answer = answers.get(question.get("id")) or {}
            options = question.get("optionsKo") or []
            if answer.get("text"):
                chosen = str(answer["text"])
            else:
                index = int(answer.get("index") or 0)
                chosen = options[index] if 0 <= index < len(options) else "(미선택)"
            lines.append(f"- {question.get('questionKo')} → {chosen}")
    else:
        lines.append("(질문 없음)")
    extra = (step2.get("extraKo") or "").strip()
    lines.append("")
    lines.append("[추가 요청]")
    lines.append(extra or "(없음)")
    lines.append("")
    wanted = requested_shot_count(step2)
    if wanted:
        # ② "몇 개의 샷으로 나눌까요?"에서 사용자가 고른 수(설계자 2026-10-08). 정확히 지킨다.
        lines.append(
            f"위 내용으로 무음 샷 정확히 {wanted}개의 계획을 쓰세요(사용자가 샷 수를 직접 골랐습니다)."
            " 사용자가 고른 장면을 처음부터 끝까지 담습니다."
        )
    else:
        lines.append(
            "위 내용으로 5초짜리 무음 샷 4개(3~6개 허용)의 계획을 쓰세요."
            " 사용자가 고른 장면을 처음부터 끝까지 담습니다."
        )
    return [{"text": "\n".join(lines)}]


def requested_shot_count(step2: dict) -> int:
    """② 에서 고른 샷 수. 0 이면 자동(AI 가 3~6개 중 정한다)."""
    try:
        count = int(step2.get("shotCount") or 0)
    except (TypeError, ValueError):
        return 0
    return count if settings.MIN_SHOTS <= count <= settings.MAX_SHOTS else 0


def plan(step2: dict) -> tuple[dict, dict]:
    blocks = _plan_blocks(step2)
    characters = step2.get("characters") or []
    style = (step2.get("story") or {}).get("style") or "live_action"

    payload, usage = _converse(
        system_text=PLAN_SYSTEM,
        blocks=blocks,
        tool=EMIT_SHOTS,
        max_tokens=6000,
        where="claude.plan",
    )
    problems = check_shots(payload, characters, style)
    wanted = requested_shot_count(step2)
    if wanted and len(payload.get("shots") or []) != wanted:
        problems.append(
            f"사용자가 샷 {wanted}개를 골랐습니다. shots 를 정확히 {wanted}개로 다시 쓰세요"
            f" (현재 {len(payload.get('shots') or [])}개)"
        )
    if problems:
        payload, usage2 = _converse(
            system_text=PLAN_SYSTEM,
            blocks=blocks + [_retry_block(problems)],
            tool=EMIT_SHOTS,
            max_tokens=6000,
            where="claude.plan.retry",
        )
        usage = {
            "inputTokens": (usage.get("inputTokens") or 0) + (usage2.get("inputTokens") or 0),
            "outputTokens": (usage.get("outputTokens") or 0) + (usage2.get("outputTokens") or 0),
        }
    # 설계자 2026-10-13: 몇 글자 초과로 승인이 막히지 않게 길이는 경계에서 줄인다(compose.fit_shot).
    # 길이 외 위반이 남으면 한 번 더 고쳐 달라고 한다. 그래도 남은 것만 샷별 '검토 필요'가 된다.
    payload["shots"] = [compose.fit_shot(shot) for shot in payload.get("shots") or []]
    problems = check_shots(payload, characters, style)
    if problems:
        payload3, usage3 = _converse(
            system_text=PLAN_SYSTEM,
            blocks=blocks + [_retry_block(problems)],
            tool=EMIT_SHOTS,
            max_tokens=6000,
            where="claude.plan.retry2",
        )
        payload3["shots"] = [compose.fit_shot(shot) for shot in payload3.get("shots") or []]
        if len(check_shots(payload3, characters, style)) < len(problems):
            payload = payload3
        usage = _add_usage(usage, usage3)
    return payload, usage


def check_shots(payload: dict, characters: list[dict], style: str) -> list[str]:
    problems: list[str] = []
    shots = payload.get("shots") or []
    if not settings.MIN_SHOTS <= len(shots) <= settings.MAX_SHOTS:
        problems.append(
            f"shots 는 {settings.MIN_SHOTS}~{settings.MAX_SHOTS}개여야 합니다 (현재 {len(shots)}개)"
        )
    seen: set[str] = set()
    for shot in shots:
        shot_id = str(shot.get("id") or "").strip()
        if not shot_id:
            problems.append("shot.id 가 비어 있습니다")
        elif shot_id in seen:
            problems.append(f"shot.id '{shot_id}' 가 중복입니다")
        seen.add(shot_id)
        camera_ko = str(shot.get("cameraKo") or "")
        if len(camera_ko) > 40:
            problems.append(f"shot {shot_id}: cameraKo 가 40자를 넘습니다")
        problems.extend(compose.check_shot(shot, characters, style))
    return problems



# ---------------------------------------------------------------- 마스터 계획(영상 1개)

# 영상 1개(single, 설계자 2026-10-19): 샷/구간 콘티 대신 "마스터 프롬프트 1개 + 고정 배경 + 고정 인물"
# 을 만든다. Claude 가 웹툰 패널 + 인물 + 총 길이를 보고 배경(영어/한국어) · 비트(2~4개, 0~총길이를
# 빈틈없이 덮음) · 인물별 외모 보강 문장을 돌려준다. 기존 _converse/tool 방식과 모델 체인을 그대로 쓴다.
EMIT_MASTER = {
    "name": "emit_master",
    "description": "Return the master plan for one continuous video.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "backgroundEn": {"type": "string"},
                "backgroundKo": {"type": "string"},
                "beats": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "t0": {"type": "number"},
                            "t1": {"type": "number"},
                            "actionEn": {"type": "string"},
                            "cameraEn": {"type": "string"},
                            "descKo": {"type": "string"},
                            "characters": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["t0", "t1", "actionEn", "cameraEn", "descKo", "characters"],
                    },
                },
                "characters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "appearanceEn": {"type": "string"},
                        },
                        "required": ["id", "appearanceEn"],
                    },
                },
            },
            "required": ["backgroundEn", "beats"],
        }
    },
}

MASTER_SYSTEM = """You plan ONE short continuous video (a single take, no cuts) made from a webtoon.
The whole video keeps ONE fixed background and the SAME characters throughout. The webtoon panels are in
story order; spread the story evenly across the beats. Write English for every *En field, Korean for *Ko.
- backgroundEn (<= 300 chars): the single setting shared by the whole video — place, time of day, lighting,
  key props. One empty location only, with NO people in it (a background plate). Never mention characters.
- backgroundKo (<= 120 chars): the same, in Korean, for the planner to read.
- beats: {min_beats}-{max_beats} beats whose [t0,t1) seconds tile 0..{total} with NO gaps and NO overlaps
  (first t0=0, last t1={total}, each t0 equals the previous t1). For each beat:
    - actionEn (<= 240 chars): what the characters physically do in this time span, as continuous motion.
    - cameraEn (<= 80 chars): one camera movement for this span (e.g. "slow push-in", "static wide").
    - descKo (<= 70 chars): what happens, in Korean.
    - characters: ids of the characters on screen in this beat.
  Refer to characters ONLY by their handles. Never describe faces, hair, skin or clothes inside beats.
- characters: for EACH given character, appearanceEn (<= 200 chars) — a clean English look description
  (hair, build, clothing, colors). If the character already has an appearance, keep its meaning.
No dialogue text, no speech bubbles, no captions. Answer only with the emit_master tool."""


def master_plan(
    panel_jpegs: list[bytes],
    characters: list[dict],
    *,
    story_text: str,
    total_s: int,
    min_beats: int,
    max_beats: int,
) -> tuple[dict, dict]:
    """웹툰 전체 + 인물 + 총 길이 → 배경 · 비트(2~4) · 인물별 외모(설계자 2026-10-19).

    기존 appearanceEn 이 있으면 그 뜻을 유지하도록 프롬프트로 지시한다. 비트의 시간 구간이
    0~total 을 빈틈없이 덮게 하고, 어긋나면 호출부(routes)가 정규화한다.
    """
    system = MASTER_SYSTEM.format(total=total_s, min_beats=min_beats, max_beats=max_beats)
    cast = "\n".join(
        f"- id={item.get('id')} handle=\"{item.get('handleEn')}\" ({item.get('nameKo') or ''}) "
        f"appearance=\"{str(item.get('appearanceEn') or '').strip()}\""
        for item in characters
    ) or "(none)"
    blocks: list[dict] = [
        {
            "text": (
                f"Story: {story_text[:2500]}\nCharacters:\n{cast}\n"
                f"Video length {total_s}s. Use {min_beats}-{max_beats} beats that tile 0..{total_s}s.\n"
                f"The next {len(panel_jpegs)} images are the webtoon panels in order."
            )
        }
    ]
    for jpeg in panel_jpegs:
        blocks.append({"image": {"format": "jpeg", "source": {"bytes": _shrink(jpeg)}}})
    payload, usage = _converse(
        system_text=system,
        blocks=blocks,
        tool=EMIT_MASTER,
        max_tokens=4000,
        where="claude.master_plan",
        model=settings.ANALYZE_MODEL,
    )
    return payload or {}, usage
