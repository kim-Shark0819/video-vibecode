"""바이블 작성자 — 원문 + 타입 + 이미지 분석을 하나의 중간 표현으로 수렴시킨다.

## 이 역할이 존재하는 이유 (지시서 5절)

    "입력 타입 × 결과물을 9개 조합으로 만들지 말 것 — 바이블이 중간 표현이다."

입력이 3종(대본/트리트먼트/로그라인)이고 결과물이 3종(로그라인/콘티/대본)이면 조합은
9가지다. 조합마다 프롬프트를 두면 규칙이 아홉 벌이 되고, 한 곳을 고칠 때 나머지 여덟이
어긋난다. 바이블을 가운데 두면 3 + 3 으로 줄어든다.

그래서 **결과물 작성자는 원문 타입을 모른다.** 바이블만 본다.

## 재생성 조건

지시서 9절 수용 기준: "원문이 안 바뀌면 바이블을 재생성하지 않는다."
`source_hash` 가 그 판단의 근거다. 원문 + 타입 + 분석 결과를 합쳐 해시한다.
분석을 포함하는 이유: 기획자가 분석 카드를 고치면 바이블이 다시 만들어져야 한다
(그것도 수용 기준이다).
"""

from __future__ import annotations

import logging

from .. import prompts_story, story_context, story_models
from . import _json

logger = logging.getLogger("toon-change-ai-pj-app.story.bible")


def source_hash(story_text: str, story_type: str, analyses: list[dict]) -> str:
    """바이블의 입력 신원. 이 값이 같으면 재생성하지 않는다.

    분석에서 해시에 넣는 필드를 고른 이유: `degraded` 나 `note` 처럼 실행 사정에 따라
    달라지는 값이 들어가면, 내용이 같은데도 해시가 달라져 바이블이 매번 재생성된다.
    바이블 입력에 실제로 들어가는 값만 넣는다(story_context.bible_input 이 쓰는 것들).
    """
    digest_parts = []
    for item in analyses:
        digest_parts.append(
            {
                "sha256": item.get("sha256") or "",
                "image_kind": item.get("image_kind") or "",
                "characters": item.get("characters") or [],
                "setting": item.get("setting") or "",
                "mood_lighting": item.get("mood_lighting") or "",
                "art_style": item.get("art_style") or "",
                "text_in_image": item.get("text_in_image") or "",
            }
        )
    # 이미지 순서가 바뀌어도 내용이 같으면 같은 해시여야 한다. 정렬해서 넣는다.
    digest_parts.sort(key=lambda item: item["sha256"] or item["image_kind"])
    return story_context.content_hash(story_text or "", story_type or "", digest_parts)


def build(caller, story_text: str, story_type: str, analyses: list[dict]) -> dict:
    """바이블을 만든다. 실패하면 RoleError.

    폴백을 두지 않는다. 바이블이 없으면 뒤의 결과물이 전부 근거를 잃으므로,
    빈 바이블로 진행하는 것은 잘못된 결과물을 만드는 것과 같다. 여기서 멈추고 알린다.
    """
    if not (story_text or "").strip():
        raise _json.RoleError("실제 스토리를 입력해 주세요. 바이블을 만들 근거가 없습니다.")

    spec = story_models.spec(story_models.ROLE_BIBLE)
    user_text = story_context.bible_input(story_text, story_type, analyses)

    payload, meta = _json.call_json(
        caller,
        prompts_story.BUILD_BIBLE,
        [{"text": user_text}],
        spec["max_tokens"],
        spec["temperature"],
        "bible",
    )

    bible = normalize(payload)
    bible["source_hash"] = source_hash(story_text, story_type, analyses)
    bible["retried"] = bool(meta.get("retried"))
    logger.info(
        "바이블 생성 완료 characters=%s settings=%s beats=%s retried=%s",
        len(bible["characters"]),
        len(bible["settings"]),
        len(bible["plot_beats"]),
        bible["retried"],
    )
    return bible


_ROLE_VALUES = {"주인공", "주요", "조연"}


def normalize(payload: dict) -> dict:
    """모델 응답을 저장 가능한 형태로 다듬는다.

    상한을 두는 이유: 바이블은 결과물 작성자의 입력이 되고 회차마다 반복해서 들어간다.
    인물이 30명이면 회차당 입력 토큰이 그만큼 늘어나고, 12화면 그것이 12배가 된다.
    """
    characters = []
    for item in payload.get("characters") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        role = str(item.get("role") or "").strip()
        if role not in _ROLE_VALUES:
            role = "조연"
        characters.append(
            {
                "name": name[:60],
                "role": role,
                "age": str(item.get("age") or "").strip()[:40],
                "occupation": str(item.get("occupation") or "").strip()[:80],
                "appearance": str(item.get("appearance") or "").strip()[:400],
                "personality": str(item.get("personality") or "").strip()[:400],
                "speech": str(item.get("speech") or "").strip()[:200],
                "goal": str(item.get("goal") or "").strip()[:200],
                "conflict": str(item.get("conflict") or "").strip()[:200],
            }
        )

    relationships = []
    known = {character["name"] for character in characters}
    for item in payload.get("relationships") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("from") or "").strip()
        target = str(item.get("to") or "").strip()
        relation = str(item.get("relation") or "").strip()
        if not (source and target and relation):
            continue
        # 인물 목록에 없는 이름이 관계에 나오면 모델이 이름을 흔든 것이다.
        # 버리지 않고 남긴다 — 검수자가 잡을 수 있어야 한다.
        relationships.append(
            {
                "from": source[:60],
                "to": target[:60],
                "relation": relation[:200],
                "unknown": not ({source, target} <= known),
            }
        )

    settings = []
    for item in payload.get("settings") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        settings.append(
            {
                "name": name[:80],
                "description": str(item.get("description") or "").strip()[:300],
                "mood": str(item.get("mood") or "").strip()[:120],
            }
        )

    beats = []
    for index, item in enumerate(payload.get("plot_beats") or [], start=1):
        if isinstance(item, str):
            text = item.strip()
            if text:
                beats.append({"no": index, "beat": text[:400]})
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("beat") or "").strip()
        if not text:
            continue
        try:
            no = int(item.get("no") or index)
        except (TypeError, ValueError):
            no = index
        beats.append({"no": no, "beat": text[:400]})
    # 번호를 다시 매긴다. 모델이 건너뛰거나 중복하는 경우가 있고, 아웃라인이 이 번호를
    # 참조하므로 연속이어야 한다.
    beats = [{"no": index, "beat": item["beat"]} for index, item in enumerate(beats[:14], start=1)]

    conflicts = [
        str(item).strip()[:300]
        for item in (payload.get("conflicts") or [])
        if str(item or "").strip()
    ]

    return {
        "title": str(payload.get("title") or "").strip()[:200],
        "genre": str(payload.get("genre") or "").strip()[:80],
        "tone": str(payload.get("tone") or "").strip()[:300],
        "logline": str(payload.get("logline") or "").strip()[:500],
        "characters": characters[:12],
        "relationships": relationships[:14],
        "settings": settings[:8],
        "plot_beats": beats,
        "visual_style": str(payload.get("visual_style") or "").strip()[:400],
        "conflicts": conflicts[:8],
        "version": 1,
    }
