"""여러 장의 참고 이미지를 읽어 역할을 판단하고, 부족한 정보를 질문으로 만든다.

## 왜 필요한가

영상 한 컷을 만들려면 기획자가 아는 것이 모델에게도 있어야 한다. 그런데 실제 작업에서는
이미지를 여러 장 올린다. 원본 장면 컷, 캐릭터 시트, 컨셉 아트, 소품 참고가 섞여 있고
그것들이 서로 어떤 관계인지는 파일 이름과 기획자 머릿속에만 있다.

모델은 그 관계를 모른 채 추측한다. 그래서 캐릭터 시트를 배경으로 넣거나, 장면 인물과
시트 인물을 다른 사람으로 그린다. 이 모듈은 생성 **전에** 그 간극을 드러낸다.

두 가지를 만든다.

1. **분석**: 각 이미지가 무엇이고 어떤 역할인지, 어느 것을 첫 프레임으로 쓰면 좋을지
2. **질문**: 지금 정보로는 확정할 수 없는 것들. 기획자가 답하면 프롬프트에 반영하고,
   건너뛰면 모델이 알아서 판단한다

## 파일 이름을 근거로 쓰는 이유

기획자는 파일을 `주인공_김남주_정면_캐릭터시트.png` 처럼 저장한다. 그 이름 안에 인물 이름,
구도, 용도가 이미 들어 있다. 이미지만 보고 추측하는 것보다 훨씬 정확하다.

**주의: S3 키에 든 이름으로는 알 수 없다.** 업로드 경로가 `secure_filename`을 쓰고
그 함수가 한글을 버려서 `주인공_김남주_정면_캐릭터시트.png` 가 `png` 가 된다.
그래서 `server.s3_original_name()` 이 메타데이터에서 원본 이름을 되읽어 여기로 넘긴다.

## 질문을 강제하지 않는 이유

기획 단계에서는 아직 결정되지 않은 것이 많다. 답을 요구하면 사용자는 아무 값이나 적어
넣고, 그 값이 프롬프트에 들어가 결과를 망친다. 그래서 모든 질문은 건너뛸 수 있고,
건너뛴 질문은 프롬프트에 아예 넣지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger("toon-change-ai-pj-app.image_brief")

# 한 번에 읽을 이미지 수 상한. 비전 호출은 이미지 수에 비례해 토큰이 늘어난다.
#
# 20장인 이유는 **Bedrock Converse 가 요청당 이미지 20장까지만 받기 때문**이다(AWS 문서).
# 이 값을 넘기면 호출 자체가 거부된다. 더 늘리려면 분석을 여러 번 호출해 역할 목록과
# 질문을 합쳐야 하는데, 실제 작업에서 20장을 넘기는 경우가 드물어 단일 호출로 둔다.
# 넘으면 앞쪽 20장만 본다.
#
# server.py 가 BRIEF_IMAGE_PIXELS(768px, JPEG)로 축소해서 넘긴다. 1536px PNG 20장이면
# 비전 토큰이 6만 개가 되는데, 역할 판단에는 768px 이면 충분하다.
# 상한의 단일 원본은 config.env 의 VIDEO_MAX_REFERENCE_IMAGES 다.
# **server.py 의 상수를 임포트하지 않는다** — image_brief 는 server 보다 아래 계층이고,
# 임포트하면 순환이 된다. 같은 환경변수를 각자 읽는 편이 안전하고,
# .build/check-reference-images.py 가 두 값과 화면 값이 어긋나지 않는지 대조한다.
MAX_IMAGES = int(os.getenv("VIDEO_MAX_REFERENCE_IMAGES", "20"))

# 이미지 역할. 값이 프롬프트 조립 방식을 바꾸므로 서버·화면이 같은 목록을 써야 한다.
ROLE_SCENE = "scene"
ROLE_CHARACTER = "characterSheet"
ROLE_CONCEPT = "concept"
ROLE_PROP = "prop"
ROLE_UNKNOWN = "unknown"
ROLES = {
    ROLE_SCENE: "원본 장면",
    ROLE_CHARACTER: "캐릭터 시트",
    ROLE_CONCEPT: "컨셉 아트",
    ROLE_PROP: "소품 · 배경 참고",
    ROLE_UNKNOWN: "판단 어려움",
}

# 질문 주제. 화면이 주제별로 묶어 보여줄 수 있게 라벨을 함께 둔다.
TOPICS = {
    "confirm": "분석 확인",
    "character": "인물 세부",
    "relation": "이미지 사이의 관계",
    "scene": "장면 상태",
    "motion": "움직임",
    "style": "화풍",
}

_MAX_QUESTIONS = 6
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

# 분석 대상 영상 모델. 분석 자체는 이미지를 읽는 일이라 같지만, **무엇을 물어야 하는지가 다르다.**
# 두 경로는 받는 모델이 다르고 제약도 다르다(지시서 1-2가 경로를 분리하라고 정한 이유).
#
#   bedrock : Luma Ray2. 첫 프레임이 없어도 되고, 부정 프롬프트가 없다.
#             프롬프트가 숏 크기·피사체·장소를 전부 담아야 해서 장면 정보가 더 중요하다.
#   wan     : 자체 GPU의 Wan 2.2 I2V. 첫 프레임이 필수이고, 원작 화풍 유지가 핵심 요구사항이다.
#             그래서 인물 동일성과 화풍 관련 확인이 더 중요하다.
TARGET_BEDROCK = "bedrock"
TARGET_I2V = "i2v"
TARGET_WAN = "wan"
_TARGET_NOTES = {
    TARGET_BEDROCK: (
        "[이 영상 모델의 특성 - 질문에 반영하세요]\n"
        "- 첫 프레임 이미지 없이 텍스트만으로도 생성할 수 있습니다. 이미지는 참고 자료일 수 있습니다.\n"
        "- 프롬프트가 숏 크기·피사체·장소·조명을 전부 담아야 합니다. 화면에 무엇이 어떻게 보이는지가\n"
        "  덜 정해져 있으면 그것을 우선 물어보세요.\n"
        "- 클립 길이는 5초 또는 9초입니다. 그 안에 담길 하나의 동작을 확인하세요."
    ),
    TARGET_I2V: (
        "[이 제작 흐름의 특성 - 질문에 반영하세요]\n"
        "- 최종 영상은 승인된 기준 이미지 한 장을 반드시 거쳐 I2V로 생성합니다.\n"
        "- 입력 이미지가 있으면 어느 컷/캐릭터 시트가 기준인지, 인물 외형·관계·소품 중 무엇을 유지해야 하는지 확인하세요.\n"
        "- 이미지가 없으면 장면 설명으로 기준 이미지를 먼저 만들므로, 인물·장소·분위기·한 가지 행동 중 빠진 것만 물으세요.\n"
        "- 실제 생성은 모델 길이 단위(5~9초)로 만들고 편집에서 필요한 구간만 씁니다. 행동의 시작과 끝이 보이게 한 가지 행동을 확인하세요."
    ),
    TARGET_WAN: (
        "[이 영상 모델의 특성 - 질문에 반영하세요]\n"
        "- 첫 프레임 이미지가 반드시 필요합니다. 어느 이미지를 첫 프레임으로 쓸지가 결과를 좌우합니다.\n"
        "- 원작의 화풍과 캐릭터 외형을 그대로 유지하는 것이 이 모델의 핵심 목적입니다.\n"
        "  장면 속 인물이 캐릭터 시트의 누구인지, 유지해야 할 외형 특징이 무엇인지 우선 물어보세요.\n"
        "- 첫 프레임이 이미 인물과 배경을 갖고 있으므로, 무엇이 '움직이는지'만 정하면 됩니다.\n"
        "  인물 외형을 새로 정하는 질문보다 동작·표정 변화 확인이 유용합니다.\n"
        "- 클립 길이는 2~5초입니다. 큰 동작은 담기지 않습니다."
    ),
}

_INSTRUCTION = """당신은 드라마 제작사에서 영상 생성을 준비하는 조연출입니다.
기획자가 참고 이미지 여러 장과 장면 설명을 올렸습니다. 각 이미지가 무엇인지 판단하고,
영상을 만들기 전에 기획자에게 확인해야 할 것을 질문으로 정리하세요.

{target_notes}

[이미지 파일 이름을 근거로 쓰세요]
기획자는 파일 이름에 정보를 담아 둡니다(인물 이름, 구도, 용도 등).
이름과 그림이 맞으면 이름의 정보를 그대로 신뢰하세요.
이름과 그림이 어긋나면 그림을 우선하고, 그 사실을 질문으로 확인하세요.

[역할 분류]
- scene: 실제로 움직이게 할 장면. 인물이 배경 안에 놓여 있고 구도가 정해져 있습니다.
- characterSheet: 인물의 외형 참고. 여러 각도나 표정이 나열되어 있거나 배경이 비어 있습니다.
- concept: 분위기·미술 참고. 특정 장면이 아니라 톤을 보여줍니다.
- prop: 소품이나 배경만 있는 참고.
- unknown: 위 어디에도 넣기 어려운 경우.

[질문을 만드는 기준]
지금 정보로 확정할 수 없어서 모델이 추측하게 되는 것만 묻습니다. 최대 {max_questions}개.

**첫 번째 질문은 반드시 topic이 confirm이어야 합니다.** 위 summary와 각 이미지의 역할 판단이
기획자 의도와 같은지 확인하는 질문입니다. 이 판단이 틀리면 나머지가 모두 어긋나므로,
명확해 보여도 건너뛰지 않고 반드시 넣습니다.

이어서 다음을 확인하세요.
1. 분석이 맞는지 (confirm): 각 이미지의 역할과 장면 해석이 기획자 의도와 같은지
2. 인물 세부 (character): 장면 속 인물의 나이대, 표정, 시선, 의상처럼 그림만으로 애매한 것
3. 이미지 사이의 관계 (relation): 장면 속 인물이 캐릭터 시트의 누구인지, 여러 인물이면 각각 누구인지
4. 장면 상태 (scene): 시간대, 날씨, 장소가 어디인지, 사건의 앞뒤
5. 움직임 (motion): 무엇이 어떻게 움직여야 하는지가 설명에 없을 때

[질문 작성 규칙]
- 한국어로, 한 문장으로 짧게 묻습니다.
- 기획자가 바로 고를 수 있는 선택지를 options에 2~4개 넣습니다. 자유 입력이 나은 질문은 빈 배열로 둡니다.
- why에는 이 답이 결과의 무엇을 바꾸는지 한 문장으로 적습니다.
- 이미 명확한 것은 묻지 않습니다. 질문이 적어도 됩니다.
- 답을 강요하는 표현을 쓰지 않습니다. 기획자는 모든 질문을 건너뛸 수 있습니다.

[출력 형식] 아래 JSON 하나만 출력합니다. 설명이나 코드펜스를 붙이지 마세요.
{{
  "summary": "이미지들을 합쳐 이해한 내용 2~3문장(한국어)",
  "images": [
    {{
      "index": "입력에서 몇 번째 이미지인지 정수(1부터)",
      "role": "scene | characterSheet | concept | prop | unknown 중 하나",
      "description": "이 이미지에 보이는 것 한두 문장(한국어)",
      "nameMatchesImage": "파일 이름과 그림이 일치하면 true, 어긋나면 false",
      "englishHint": "영상 프롬프트에 쓸 영어 묘사 8~25단어. 인물이면 외형, 장면이면 구도와 배경."
    }}
  ],
  "suggestedPrimary": "첫 프레임으로 쓸 이미지의 index 정수. scene 역할을 우선한다.",
  "primaryReason": "그 이미지를 고른 이유 한 문장(한국어)",
  "questions": [
    {{
      "id": "q1",
      "topic": "confirm | character | relation | scene | motion | style 중 하나",
      "question": "기획자에게 물을 한 문장(한국어)",
      "why": "이 답이 무엇을 바꾸는지 한 문장(한국어)",
      "options": ["고를 수 있는 답 1", "고를 수 있는 답 2"]
    }}
  ]
}}"""


def _extract_json(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(raw)
        if not match:
            raise ValueError("분석 응답에서 JSON을 찾지 못했습니다.")
        return json.loads(match.group(0))


def _fallback(images: list[dict], target: str = TARGET_WAN) -> dict:
    """분석이 실패했을 때의 결과. 생성을 막지 않는다.

    첫 번째 이미지를 첫 프레임으로 보고 질문은 만들지 않는다. 질문 없이도 생성은 된다.
    """
    return {
        "summary": "이미지 분석에 실패했습니다. 첫 번째 이미지를 첫 프레임으로 사용합니다.",
        "images": [
            {
                "index": index,
                "name": item.get("name") or "",
                "role": ROLE_SCENE if index == 1 else ROLE_UNKNOWN,
                "roleLabel": ROLES[ROLE_SCENE if index == 1 else ROLE_UNKNOWN],
                "description": "",
                "nameMatchesImage": True,
                "englishHint": "",
            }
            for index, item in enumerate(images, start=1)
        ],
        "suggestedPrimary": 1,
        "primaryReason": "분석에 실패해 첫 번째 이미지를 사용합니다.",
        "questions": [],
        "target": target,
        "degraded": True,
    }


def analyze(
    converse, images: list[dict], user_text: str = "", target: str = TARGET_WAN
) -> dict:
    """이미지들을 읽어 역할·분석·확인 질문을 만든다.

    Args:
        converse: (system, content, max_tokens) -> str. gateway를 감싼 호출자.
                  이 모듈은 boto3를 직접 만지지 않는다(사용량 기록을 우회하지 않기 위해).
        images: [{"name": 원본 파일명, "bytes": 이미지 바이트, "format": "png"}] 목록.
                name은 S3 키가 아니라 **업로드 당시의 원본 이름**이어야 한다.
        user_text: 기획자가 적은 장면 설명. 없어도 된다.
        target: 이 분석 결과를 받을 영상 모델. `bedrock` 또는 `wan`.
                무엇을 물어야 하는지가 달라진다(_TARGET_NOTES 참고).

    Returns:
        {"summary", "images"[], "suggestedPrimary", "primaryReason", "questions"[],
         "target", "degraded"}
    """
    usable = [item for item in images if item.get("bytes")][:MAX_IMAGES]
    if not usable:
        return _fallback(images, target)

    lines = []
    content: list[dict] = []
    for index, item in enumerate(usable, start=1):
        name = (item.get("name") or "").strip() or "(파일 이름 없음)"
        lines.append(f"{index}. {name}")
        content.append({"text": f"[이미지 {index}] 파일 이름: {name}"})
        fmt = item.get("format") or "png"
        content.append(
            {
                "image": {
                    "format": fmt if fmt in {"png", "jpeg", "gif", "webp"} else "png",
                    "source": {"bytes": item["bytes"]},
                }
            }
        )

    header = "올린 이미지 목록 (순서대로):\n" + "\n".join(lines)
    if (user_text or "").strip():
        header += f"\n\n기획자가 적은 장면 설명:\n{user_text.strip()[:4000]}"
    else:
        header += "\n\n기획자가 적은 장면 설명: (아직 없음)"
    content.insert(0, {"text": header})

    notes = _TARGET_NOTES.get(target) or _TARGET_NOTES[TARGET_WAN]
    system = _INSTRUCTION.format(max_questions=_MAX_QUESTIONS, target_notes=notes)
    try:
        # 이미지 수에 비례해 출력이 늘어난다. 이미지당 약 400토큰 + 질문 분량.
        raw = converse(system, content, min(6000, 1200 + 500 * len(usable)))
        parsed = _extract_json(raw)
    except Exception as error:  # noqa: BLE001 - 분석 실패가 생성을 막아서는 안 된다
        logger.warning("이미지 분석에 실패해 기본값을 사용합니다: %s", error)
        return _fallback(usable, target)

    brief = _normalize(parsed, usable)
    brief["target"] = target
    return brief


def _normalize(parsed: dict, images: list[dict]) -> dict:
    """모델 응답을 화면과 프롬프트가 바로 쓸 수 있는 형태로 다듬는다.

    모델이 index를 빠뜨리거나 범위를 벗어나는 경우가 있어 입력 순서를 기준으로 다시 맞춘다.
    """
    by_index: dict[int, dict] = {}
    for item in parsed.get("images") or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if 1 <= index <= len(images):
            by_index[index] = item

    normalized_images = []
    for index, source in enumerate(images, start=1):
        found = by_index.get(index) or {}
        role = str(found.get("role") or "").strip()
        if role not in ROLES:
            role = ROLE_UNKNOWN
        normalized_images.append(
            {
                "index": index,
                # 이름은 서버가 아는 값을 쓴다. 모델이 옮겨 적다 틀리는 경우가 있다.
                "name": (source.get("name") or "").strip(),
                "role": role,
                "roleLabel": ROLES[role],
                "description": str(found.get("description") or "").strip()[:400],
                "nameMatchesImage": found.get("nameMatchesImage") is not False,
                "englishHint": str(found.get("englishHint") or "").strip()[:300],
            }
        )

    # 첫 프레임 추천. 범위를 벗어나면 scene 역할 중 첫 번째, 그것도 없으면 1번.
    try:
        primary = int(parsed.get("suggestedPrimary"))
    except (TypeError, ValueError):
        primary = 0
    if not 1 <= primary <= len(images):
        scenes = [item["index"] for item in normalized_images if item["role"] == ROLE_SCENE]
        primary = scenes[0] if scenes else 1

    raw_questions = []
    for item in parsed.get("questions") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("question") or "").strip()
        if not text:
            continue
        topic = str(item.get("topic") or "").strip()
        if topic not in TOPICS:
            topic = "confirm"
        raw_questions.append(
            {
                "topic": topic,
                "question": text[:300],
                "why": str(item.get("why") or "").strip()[:200],
                "options": [
                    str(option).strip()[:120]
                    for option in (item.get("options") or [])
                    if str(option or "").strip()
                ][:4],
            }
        )

    # 분석이 맞는지 확인하는 질문은 반드시 하나 있어야 한다. 이 판단이 틀리면 나머지가
    # 모두 어긋나기 때문이다. 모델이 "명확하다"고 보고 생략하는 경우가 실제로 있었다.
    if not any(item["topic"] == "confirm" for item in raw_questions):
        labels = []
        for item in normalized_images:
            shown = item["name"] or f"{item['index']}번 이미지"
            labels.append(f"{shown}={item['roleLabel']}")
        roles = ", ".join(labels)
        raw_questions.insert(
            0,
            {
                "topic": "confirm",
                "question": f"이미지 역할을 이렇게 봤습니다 ({roles}). 맞나요?",
                "why": "역할 판단이 틀리면 첫 프레임과 참고 자료가 뒤바뀝니다.",
                "options": ["맞습니다", "역할이 다릅니다"],
            },
        )
    # confirm 질문을 맨 앞으로 올린다. 먼저 확인하고 세부로 들어가는 순서가 자연스럽다.
    raw_questions.sort(key=lambda item: 0 if item["topic"] == "confirm" else 1)

    questions = []
    for order, item in enumerate(raw_questions[:_MAX_QUESTIONS], start=1):
        questions.append(
            {
                # id는 서버가 부여한다. 모델이 중복 id를 주면 답변이 섞인다.
                "id": f"q{order}",
                "topic": item["topic"],
                "topicLabel": TOPICS[item["topic"]],
                "question": item["question"],
                "why": item["why"],
                "options": item["options"],
                # 모든 질문은 건너뛸 수 있다. 기획 단계에서 아직 정해지지 않은 것이 많다.
                "optional": True,
            }
        )

    return {
        "summary": str(parsed.get("summary") or "").strip()[:800],
        "images": normalized_images,
        "suggestedPrimary": primary,
        "primaryReason": str(parsed.get("primaryReason") or "").strip()[:200],
        "questions": questions,
        # 호출자가 analyze()에서 실제 target으로 덮어쓴다. 여기서는 기본값만 둔다.
        "target": TARGET_WAN,
        "degraded": False,
    }


def prompt_context(brief: dict | None, answers: list[dict] | None, primary_index: int = 0) -> str:
    """분석 결과와 기획자 답변을 프롬프트에 넣을 한국어 블록으로 만든다.

    첫 프레임 이미지는 모델이 직접 보므로 다시 묘사하지 않는다. 참고 이미지들만 글로 넣는다.
    건너뛴 질문은 아예 넣지 않는다. "미정"이라고 적어 보내면 모델이 그 단어를 반영하려 한다.
    """
    parts: list[str] = []
    brief = brief or {}

    summary = str(brief.get("summary") or "").strip()
    if summary:
        parts.append(f"[이미지 분석 요약]\n{summary[:800]}")

    references = []
    for item in brief.get("images") or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if index == primary_index:
            # 첫 프레임은 모델이 이미지로 본다. 글로 다시 묘사하면 지시가 둘이 되어 충돌한다.
            continue
        label = ROLES.get(str(item.get("role")), ROLES[ROLE_UNKNOWN])
        detail = str(item.get("englishHint") or item.get("description") or "").strip()
        if not detail:
            continue
        name = str(item.get("name") or "").strip()
        references.append(f"- {label}{f' ({name})' if name else ''}: {detail[:300]}")
    if references:
        parts.append(
            "[참고 이미지] 아래는 첫 프레임이 아닌 참고 자료입니다. "
            "인물의 외형과 화풍을 맞추는 근거로만 쓰고, 화면에 새로 등장시키지 마세요.\n"
            + "\n".join(references[:6])
        )

    answered = []
    for item in answers or []:
        if not isinstance(item, dict):
            continue
        answer = str(item.get("answer") or "").strip()
        if not answer:
            continue  # 건너뛴 질문
        question = str(item.get("question") or "").strip()
        answered.append(f"- {question[:200]} -> {answer[:300]}")
    if answered:
        parts.append(
            "[기획자가 확인해 준 내용] 아래는 기획자가 직접 답한 것입니다. 추측보다 우선합니다.\n"
            + "\n".join(answered[:10])
        )

    return "\n\n".join(parts)
