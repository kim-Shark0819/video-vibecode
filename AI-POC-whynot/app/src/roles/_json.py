"""JSON 응답 파싱과 1차 스키마 검사. 모든 역할이 공유한다.

## 왜 공유 모듈인가

이 코드베이스에는 같은 파싱 로직이 **세 벌** 있다.
  image_brief._extract_json / documents.extract_json / styles.route 안의 정규식
세 곳이 조금씩 다르고, 한 곳만 고쳐진 흔적이 있다. 역할이 일곱 개인 이 파이프라인에서
같은 실수를 반복하지 않으려고 한 곳에 둔다.

## 코드펜스를 반드시 벗겨야 하는 이유 (실측)

프롬프트에서 "코드펜스를 붙이지 마세요"라고 명시했는데도 모델이 붙인다.
2026-09-07 실측:
  global.anthropic.claude-sonnet-4-6                -> 펜스 없음
  global.anthropic.claude-sonnet-4-5-20250929-v1:0  -> ```json 붙임
  global.anthropic.claude-haiku-4-5-20251001-v1:0   -> ```json 붙임
  amazon.nova-lite / nova-pro                       -> ```json 붙임
검수자가 haiku 이므로 **검수 응답은 거의 항상 펜스가 붙는다.** 벗기지 않으면 검수가
매번 파싱 실패로 떨어지고, 그러면 모든 결과물이 needs_review 가 된다.
"""

from __future__ import annotations

import json
import logging
import re

from .. import prompts_story

logger = logging.getLogger("toon-change-ai-pj-app.story.json")

_FENCE_HEAD = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*", re.MULTILINE)
_FENCE_TAIL = re.compile(r"\s*```\s*$")
# 앞뒤에 설명을 붙인 경우의 마지막 수단. 가장 바깥 중괄호 덩어리를 집는다.
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class RoleError(RuntimeError):
    """역할 실행이 실패했을 때. 사용자에게 보여줄 수 있는 한국어 메시지를 담는다."""


def extract(text: str) -> dict:
    """모델 응답에서 JSON 객체를 꺼낸다. 실패하면 RoleError."""
    raw = (text or "").strip()
    if not raw:
        raise RoleError("모델이 빈 응답을 반환했습니다.")

    candidates = [raw]

    # 1) 코드펜스 제거
    if "```" in raw:
        stripped = _FENCE_TAIL.sub("", _FENCE_HEAD.sub("", raw)).strip()
        candidates.append(stripped)

    # 2) 가장 바깥 객체만
    match = _OBJECT.search(raw)
    if match:
        candidates.append(match.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
        # 배열을 돌려주는 모델도 있다. 스키마가 객체를 요구하므로 감싸지 않고 거부한다 —
        # 조용히 감싸면 필수 키 검사가 통과해 버려 더 나쁘다.
        logger.warning("모델이 객체가 아닌 %s 를 반환했습니다.", type(parsed).__name__)

    preview = raw[:200].replace("\n", " ")
    raise RoleError(f"모델 응답에서 JSON을 찾지 못했습니다. 앞부분: {preview}")


def check_schema(payload: dict, schema_name: str) -> list[str]:
    """1차 스키마 검사. **무료다.** 문제 목록을 돌려준다(빈 목록이면 통과).

    지시서 1절: "1차 코드 스키마 검사(무료) -> 2차 Haiku 일관성 검사".
    형식이 깨진 결과에 "일관성"을 물어도 의미가 없고, Haiku 호출이 낭비다.
    """
    schema = prompts_story.SCHEMAS.get(schema_name)
    if not schema:
        return [f"'{schema_name}' 스키마 정의가 없습니다."]

    problems: list[str] = []
    for key in schema.get("required", []):
        if key not in payload:
            problems.append(f"필수 항목 '{key}' 이(가) 없습니다.")
        elif payload[key] in (None, ""):
            # 0 과 False 는 유효한 값이다. `not payload[key]` 로 검사하면
            # episode_no=0 이나 pass=False 를 "없음"으로 잘못 판정한다.
            problems.append(f"필수 항목 '{key}' 이(가) 비어 있습니다.")

    for key in schema.get("arrays", []):
        if key in payload and not isinstance(payload[key], list):
            problems.append(f"'{key}' 은(는) 배열이어야 합니다. 지금 {type(payload[key]).__name__}.")

    return problems


def call_json(
    caller,
    system: str,
    content: list[dict],
    max_tokens: int,
    temperature: float,
    schema_name: str,
    retry_once: bool = True,
) -> tuple[dict, dict]:
    """JSON 을 요구하는 역할 호출 한 건. (payload, meta) 를 돌려준다.

    meta = {"truncated": bool, "retried": bool}

    파싱이나 스키마 검사가 실패하면 **한 번** 다시 요청한다(지시서 6절).
    재요청 때는 무엇이 틀렸는지 붙여 준다. 같은 요청을 그대로 반복하면 같은 실패가 온다.

    두 번째도 실패하면 RoleError 를 올린다. 라우터가 그것을 받아 결과물을 failed 로 남긴다.
    조용히 빈 dict 를 돌려주지 않는다 — 그러면 빈 결과물이 저장되고 사용자는 이유를 모른다.
    """
    attempt_content = content
    last_problem = ""

    for attempt in (1, 2):
        text, truncated = caller(system, attempt_content, max_tokens, temperature)

        if truncated:
            # 잘린 JSON 은 반드시 파싱에 실패한다. 파싱을 시도하지 않고 바로 알린다.
            # max_tokens 를 올려야 하는 상황이므로 사용자에게 그것을 알려야 한다.
            raise RoleError(
                f"응답이 출력 한도({max_tokens:,} 토큰)에서 잘렸습니다. "
                "분량을 줄이거나 회차 범위를 좁혀 주세요."
            )

        try:
            payload = extract(text)
            problems = check_schema(payload, schema_name)
            if not problems:
                return payload, {"truncated": False, "retried": attempt == 2}
            last_problem = " ".join(problems)
        except RoleError as error:
            last_problem = str(error)

        if attempt == 2 or not retry_once:
            break

        logger.warning("역할 응답이 규격에 맞지 않아 1회 재요청합니다: %s", last_problem)
        attempt_content = list(content) + [
            {
                "text": (
                    "직전 응답이 규격에 맞지 않았습니다. 아래 문제를 고쳐 "
                    "**JSON 하나만** 다시 출력하세요. 코드펜스와 설명을 붙이지 마세요.\n"
                    f"문제: {last_problem}"
                )
            }
        ]

    raise RoleError(f"모델 응답이 규격에 맞지 않습니다. {last_problem}")
