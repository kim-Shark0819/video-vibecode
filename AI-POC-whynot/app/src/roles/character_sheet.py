"""캐릭터 시트 — 레퍼런스 분석 여러 건을 인물 외모 텍스트 1벌로 압축한다.

## 왜 텍스트로 고정하는가 (요구사항 R-03.1)

레퍼런스를 100장 모아도 추론에 다 넣을 수 없다. 이미지 모델이 받는 참조는 보통 1~3장이다.
100장은 **분석 대상**이고, 그 이해를 두 가지로 압축해 추론에 쓴다.

  1. 이 텍스트 시트 (`appearance_en` 이 확산 모델 프롬프트에 직접 들어간다)
  2. 큐레이션된 대표 3장 (호출자가 고른다)

그래서 **장수가 늘어도 추론 비용과 컨텍스트가 늘지 않는다.** 100장을 다 넣는 설계는
30장에서 컨텍스트가 터지고 100장에서는 불가능하다.

## 이 역할은 `vision` 을 직접 부르지 않는다

역할끼리 서로를 부르지 않는다(`roles/__init__` 규칙 2). 레퍼런스 장당 분석은
`vision` 이 하고, 그 결과를 라우터가 이 역할에 넘긴다.

## 실패 비용이 크다

이 시트가 **모든 샷의 프롬프트에 같은 문장으로 삽입된다.** 여기에 레퍼런스에 없던
특징이 한 줄 들어가면 그것이 전 샷에 퍼지고, 사용자는 자기 캐릭터가 아닌 인물을 본다.
그래서 모델을 값싼 것으로 내리지 않고 temperature 를 낮게 둔다.
"""

from __future__ import annotations

import logging

from .. import prompts_story, story_context, story_models
from . import _json

logger = logging.getLogger("toon-change-ai-pj-app.video.character_sheet")

# 시트 1벌을 만들 때 모델에 넣는 분석 건수 상한.
#
# 큐레이션 통과분이 30장이어도 전부 넣지 않는다. 같은 인물의 분석은 내용이 크게
# 겹쳐서, 12건을 넘기면 입력 토큰만 늘고 결과가 달라지지 않는다. 대표 각도가
# 포함되도록 큐레이션이 정렬해서 넘긴다.
MAX_ANALYSES_IN_PROMPT = 12

# appearance_en 길이 하한·상한. 프롬프트에도 같은 값을 적었다.
# 짧으면 인물이 흔들리고, 길면 샷별 지시(구도·조명)를 밀어낸다.
MIN_APPEARANCE_WORDS = 8
MAX_APPEARANCE_CHARS = 400


def _analysis_lines(analyses: list[dict]) -> list[str]:
    """비전 분석 결과에서 인물 외모에 쓸 수 있는 부분만 뽑는다.

    `setting` 과 `mood_lighting` 은 넣지 않는다. 장면마다 달라지는 값이라
    모델이 그것을 외모로 오인해 시트에 넣으면 모든 샷의 배경이 고정된다.
    """
    lines: list[str] = []
    for index, item in enumerate(analyses[:MAX_ANALYSES_IN_PROMPT], start=1):
        bits: list[str] = []
        for character in item.get("characters") or []:
            descriptor = str(character.get("descriptor") or "").strip()
            if descriptor:
                who = str(character.get("name_if_visible") or "").strip()
                bits.append(f"인물: {f'{who} — ' if who else ''}{descriptor}")
        art_style = str(item.get("art_style") or "").strip()
        if art_style:
            bits.append(f"화풍: {art_style}")
        if not bits:
            continue
        angle = str(item.get("angle") or "").strip()
        header = f"--- 레퍼런스 {index}" + (f" (각도: {angle})" if angle else "") + " ---"
        lines.append(header)
        lines.extend(bits)
    return lines


def _user_content(name: str, analyses: list[dict], bible_note: str, notes=None) -> list[dict]:
    parts = [f"[인물 이름] {name}", ""]

    lines = _analysis_lines(analyses)
    if lines:
        parts += ["[레퍼런스 이미지 분석]"] + lines
    else:
        # 분석에서 인물 묘사를 못 얻은 경우다. 바이블만으로 만들게 한다.
        # 빈 프롬프트를 보내면 모델이 자유롭게 창작해 레퍼런스와 무관한 인물이 나온다.
        parts += ["[레퍼런스 이미지 분석] 사용할 수 있는 인물 묘사가 없습니다."]

    if bible_note.strip():
        parts += ["", "[작품 바이블의 이 인물 설정]", bible_note.strip()[:1500]]

    if notes:
        parts += ["", "[직전 결과의 문제 — 고쳐 주세요]"] + [f"- {note}" for note in notes]

    return [{"text": "\n".join(parts)}]


def _bible_note(bible: dict, name: str) -> str:
    """바이블에서 이 인물의 설정을 찾는다. 없으면 빈 문자열.

    바이블을 함께 넣는 이유: 레퍼런스가 3장뿐일 때 분석만으로는 대표 의상을 못 정한다.
    바이블의 `appearance` 가 그 공백을 메운다. 다만 **레퍼런스가 우선**이라고
    프롬프트에서 못 박았다 — 바이블은 글이고 레퍼런스는 실제 그림이다.
    """
    target = (name or "").strip()
    if not target:
        return ""
    for character in bible.get("characters") or []:
        if str(character.get("name") or "").strip() != target:
            continue
        bits = []
        for key, label in (
            ("appearance", "외형"),
            ("age", "나이"),
            ("occupation", "직업"),
            ("personality", "성격"),
        ):
            value = str(character.get(key) or "").strip()
            if value:
                bits.append(f"{label}: {value}")
        return "\n".join(bits)
    return ""


def normalize(payload: dict, name: str, ref_count: int) -> dict:
    """모델 응답을 다듬는다. `appearance_en` 이 쓸 만한지 여기서 판정한다."""
    appearance_en = " ".join(str(payload.get("appearance_en") or "").split())

    degraded = False
    note = ""
    if len(appearance_en.split()) < MIN_APPEARANCE_WORDS:
        # 너무 짧으면 인물이 고정되지 않는다. 실패로 표시해 호출부가 알 수 있게 한다.
        # 조용히 통과시키면 모든 샷에서 인물이 흔들리는데 원인을 찾기 어렵다.
        degraded = True
        note = f"appearance_en 이 {MIN_APPEARANCE_WORDS} 단어보다 짧습니다."
        logger.warning("캐릭터 시트가 너무 짧습니다 name=%s value=%r", name, appearance_en)

    return {
        "name": (str(payload.get("name") or "").strip() or name)[:60],
        "appearance_ko": str(payload.get("appearance_ko") or "").strip()[:800],
        "appearance_en": appearance_en[:MAX_APPEARANCE_CHARS],
        "signature_items": [
            str(item or "").strip()[:80]
            for item in (payload.get("signature_items") or [])
            if str(item or "").strip()
        ][:6],
        "uncertain": [
            str(item or "").strip()[:120]
            for item in (payload.get("uncertain") or [])
            if str(item or "").strip()
        ][:8],
        "ref_count": int(ref_count),
        "degraded": degraded,
        "note": note,
        # 기획자가 화면에서 고쳤는지. 고친 시트는 refSetHash 가 바뀌어도 덮어쓰지 않는다.
        "edited_by_user": False,
    }


def build(
    caller,
    name: str,
    analyses: list[dict],
    bible: dict | None = None,
    notes=None,
) -> dict:
    """인물 한 명의 캐릭터 시트를 만든다.

    Args:
        caller: (system, content, max_tokens, temperature) -> (text, truncated)
        name: 인물 이름. 바이블·샷 목록과 같은 표기여야 한다.
        analyses: 큐레이션을 통과한 레퍼런스의 비전 분석 결과.
                  대표 각도가 앞에 오도록 정렬해서 넘긴다.
        bible: 레퍼런스가 적을 때 공백을 메운다. 레퍼런스가 우선이다.

    Raises:
        _json.RoleError: 두 번 시도해도 규격에 맞지 않을 때.
    """
    target = (name or "").strip()
    if not target:
        raise _json.RoleError("인물 이름이 없습니다.")

    spec = story_models.spec(story_models.ROLE_CHARACTER_SHEET)
    payload, _meta = _json.call_json(
        caller,
        prompts_story.CHARACTER_SHEET,
        _user_content(target, analyses or [], _bible_note(bible or {}, target), notes),
        spec["max_tokens"],
        spec["temperature"],
        "character_sheet",
    )

    sheet = normalize(payload, target, len(analyses or []))
    logger.info(
        "캐릭터 시트 완료 name=%s refs=%s words=%s degraded=%s",
        sheet["name"],
        sheet["ref_count"],
        len(sheet["appearance_en"].split()),
        sheet["degraded"],
    )
    return sheet


def set_hash(curated_sha256: list[str]) -> str:
    """큐레이션 통과 레퍼런스 집합의 지문.

    이 값이 바뀔 때만 시트를 다시 만든다(요구사항 R-05.3). 정렬해서 해시하는 이유는
    같은 집합이 다른 순서로 오면 해시가 달라져 불필요하게 재생성되기 때문이다.
    """
    return story_context.content_hash(sorted(set(curated_sha256 or [])))[:32]


def prompt_fragment(sheet: dict) -> str:
    """키프레임 프롬프트에 삽입할 조각. 영어다.

    `signature_items` 를 함께 넣는다. 의상 요소가 빠지면 인물이 같아 보이지 않는
    경우가 많다 — 얼굴보다 옷이 먼저 눈에 들어온다.
    """
    if not isinstance(sheet, dict):
        return ""
    base = str(sheet.get("appearance_en") or "").strip()
    if not base:
        return ""
    items = [str(item).strip() for item in (sheet.get("signature_items") or []) if str(item).strip()]
    if items:
        return f"{base}, {', '.join(items[:3])}"
    return base
