"""vpoc1 영어 프롬프트 조립 — PRD 6.5. 모델을 부르지 않는 순수 함수만 둔다.

규칙 R4: **자르지 않는다.** 길이는 필드 예산으로 보장하고, 그래도 넘으면 제출하지 않고
'검토 필요'로 둔다. 조용한 절단이 지난 실패의 원인이었다.
"""

from __future__ import annotations

import re

# 화풍 문자열 (PRD 6.5)
STYLE = {
    "live_action": (
        "Cinematic live-action film footage, photorealistic, natural skin texture, "
        "35mm lens, soft natural light"
    ),
    "anime": "2D anime-style animation, clean line art, cel shading, soft colors",
    # 설계자 2026-10-14: 웹툰 원작 그대로 — 화풍 · 인물을 올린 웹툰 그림에서 가져온다.
    "webtoon": (
        "Korean webtoon art style exactly as in the uploaded webtoon images: the same line work, "
        "coloring, shading and character designs"
    ),
}

STYLE_IMAGE = {
    "live_action": "Cinematic photograph, photorealistic, 35mm lens, soft natural light",
    "anime": "2D anime-style character illustration, clean line art, cel shading",
    "webtoon": "Korean webtoon character illustration in exactly the art style of the uploaded webtoon",
}

IMAGE_NEGATIVE = (
    "text, watermark, logo, extra people, extra fingers, deformed hands, "
    "blurry face, cropped head"
)

# 조각별 예산 (PRD 6.5 표)
BUDGET = {
    "STYLE": 120,
    "LOCATION": 120,
    "APPEARANCE": 200,
    "ACTION": 260,
    "CAMERA": 80,
}
MAX_PROMPT_CHARS = 1200
MAX_CHARACTERS_PER_SHOT = 2

HANGUL = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")
NEGATION = re.compile(r"\b(no|not|without|never|don't|doesn't)\b", re.IGNORECASE)


def style_of(value: str) -> str:
    return value if value in STYLE else "live_action"


def _name_pattern(name: str) -> re.Pattern | None:
    """전체 이름을 단어 경계로 찾는다. "Seo Won" 은 잡고 "won the race" 는 통과시킨다."""
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        return None
    parts = [re.escape(part) for part in cleaned.split(" ")]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


def character_names(characters: list[dict]) -> list[str]:
    names: list[str] = []
    for item in characters:
        for field in ("nameKo", "nameEn"):
            value = item.get(field)
            if value:
                names.append(str(value))
    return names


def build_prompt(shot: dict, characters: list[dict], style: str) -> str:
    """`{STYLE}. {LOCATION}. {APPEARANCE}. {ACTION}. {CAMERA}.`"""
    by_id = {item.get("id"): item for item in characters}
    appearances = []
    for char_id in (shot.get("characters") or [])[:MAX_CHARACTERS_PER_SHOT]:
        item = by_id.get(char_id) or {}
        text = (item.get("appearanceEn") or "").strip().rstrip(".")
        if text:
            appearances.append(text)

    pieces = [
        STYLE[style_of(style)],
        (shot.get("locationEn") or "").strip().rstrip("."),
        "; ".join(appearances),
        (shot.get("actionEn") or "").strip().rstrip("."),
        (shot.get("cameraEn") or "").strip().rstrip("."),
    ]
    return ". ".join(piece for piece in pieces if piece) + "."


# 이미지 안전 필터(Stability)가 다른 뜻으로 읽어 거절하는 평범한 표현 → 같은 뜻의 무난한 표현.
# 실측 2026-10-13: "orange hair tied up in a bun" 이 매번 "Filter reason: prompt" 로 거절됐다
# ("tied up" = 묶인). 목록은 1차 방어일 뿐이다 — 걸리면 Claude 가 문장을 다시 쓴다(routes._draw_character).
SAFE_PHRASES = [
    (r"\btied\s+up\b", "gathered up"),
    (r"\btied\s+back\b", "pulled back"),
    (r"\btied\b", "gathered"),
    (r"\bbound\b", "wrapped"),
    (r"\btight(?:ly)?\b", "fitted"),
    (r"\bshirtless\b", "in a casual top"),
    (r"\bbloody\b|\bblood(?:stained)?\b", ""),
    (r"\bwounds?\b|\binjur(?:y|ies|ed)\b|\bscars?\b|\bbruis(?:e|es|ed)\b", ""),
    (r"\bsexy\b|\bseductive\b|\bsensual\b", "elegant"),
    (r"\bnaked\b|\bnude\b|\bbare\b", ""),
    (r"\bcleavage\b", "neckline"),
    (r"\bgun\b|\bknife\b|\bweapon\b|\bcigarette\b|\bsmoking\b", ""),
]


def safe_image_text(text: str) -> str:
    """SAFE_PHRASES 를 적용하고 빈 쉼표 · 공백을 정리한다."""
    out = text or ""
    for pattern, replacement in SAFE_PHRASES:
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    out = re.sub(r"\s+,", ",", out)
    out = re.sub(r",\s*,+", ",", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip()


def minimal_image_prompt(character: dict, style: str) -> tuple[str, str]:
    """마지막 수단: 화풍 + 사람/생물 + 단순 배경만. 외모 세부를 빼서 필터에 걸릴 말이 없다."""
    appearance = (character.get("appearanceEn") or "").strip()
    who = "a person" if is_human(appearance) else "a friendly creature"
    head = (STYLE_IMAGE.get(style_of(style)) or STYLE[style_of(style)]).strip().rstrip(".")
    prompt = (
        f"{head}. Waist-up portrait of {who}, neutral friendly expression, "
        "plain light background, facing the camera, sharp focus on the face."
    )
    return prompt, "text, watermark, logo, extra people, blurry face"


IMAGE_NEGATIVE_NONHUMAN = (
    "text, watermark, logo, people, humans, extra animals, extra legs, deformed paws, "
    "blurry, cropped body"
)

# appearanceEn 은 "무엇인지"로 시작한다(llm ANALYZE 규칙 4). 앞 여섯 단어에 사람 명사가 없으면
# 동물·생물로 본다. 사람용 틀(허리 위 · 표정 · 손가락)에 동물을 넣으면 SD3.5 가 사용자가 쓴
# 특징(예: 까만 단모)을 버리고 흔한 모습(예: 삼색 고양이)으로 그린다 (2026-09-30 dev 보고).
HUMAN_NOUN = re.compile(
    r"\b(man|woman|men|women|boy|girl|person|people|guy|lady|gentleman|child|kid|teen|teenager|"
    r"student|racer|soldier|knight|prince|princess|king|queen|human|male|female)\b",
    re.IGNORECASE,
)


def is_human(appearance: str) -> bool:
    head = " ".join((appearance or "").split()[:6])
    return bool(HUMAN_NOUN.search(head)) or not head


def image_prompt(character: dict, story: dict, style: str) -> tuple[str, str]:
    appearance = (character.get("appearanceEn") or "").strip().rstrip(".")
    body = (character.get("bodyEn") or "").strip().rstrip(".")
    setting = (story.get("settingEn") or "").strip().rstrip(".")
    who = appearance + (f", {body}" if body else "")
    if not is_human(appearance):
        who = re.sub(r"^(a|an|the)\s+", "", who, flags=re.IGNORECASE)
        where = f", in {setting}" if setting else ""
        prompt = (
            f"{STYLE_IMAGE[style_of(style)]}. Full-body portrait of a single {who}{where}. "
            "The whole body is visible, looking at the camera, sharp focus on the face and fur texture."
        )
        return prompt, IMAGE_NEGATIVE_NONHUMAN
    where = f", standing in {setting}" if setting else ""
    prompt = (
        f"{STYLE_IMAGE[style_of(style)]}. Waist-up portrait of {who}{where}, "
        "facing the camera, calm expression, sharp focus on the face."
    )
    return prompt, IMAGE_NEGATIVE


# ---------------------------------------------------------------- 코드 검사


def check_shot(shot: dict, characters: list[dict], style: str) -> list[str]:
    """조립 전 검사. 위반 목록을 돌려준다. 빈 목록이면 통과다."""
    problems: list[str] = []
    known = {item.get("id") for item in characters}

    english_fields = {
        "locationEn": BUDGET["LOCATION"],
        "actionEn": BUDGET["ACTION"],
        "cameraEn": BUDGET["CAMERA"],
    }
    for field, limit in english_fields.items():
        value = str(shot.get(field) or "").strip()
        if not value:
            problems.append(f"shot {shot.get('id')}: {field} 가 비어 있습니다")
            continue
        if HANGUL.search(value):
            problems.append(f"shot {shot.get('id')}: {field} 에 한글이 있습니다")
        if len(value) > limit:
            problems.append(
                f"shot {shot.get('id')}: {field} 가 {limit}자를 넘습니다 ({len(value)}자)"
            )
        if NEGATION.search(value):
            problems.append(f"shot {shot.get('id')}: {field} 에 부정어가 있습니다")

    for name in character_names(characters):
        pattern = _name_pattern(name)
        if not pattern:
            continue
        for field in english_fields:
            if pattern.search(str(shot.get(field) or "")):
                problems.append(
                    f"shot {shot.get('id')}: {field} 에 캐릭터 이름 '{name}' 이 있습니다"
                    " (이름 대신 외형으로 지칭해야 합니다)"
                )

    shot_chars = shot.get("characters") or []
    if len(shot_chars) > MAX_CHARACTERS_PER_SHOT:
        problems.append(f"shot {shot.get('id')}: 인물이 {len(shot_chars)}명입니다 (최대 2명)")
    for char_id in shot_chars:
        if char_id not in known:
            problems.append(f"shot {shot.get('id')}: 없는 캐릭터 id '{char_id}' 를 참조합니다")

    summary = str(shot.get("summaryKo") or "").strip()
    if not summary:
        problems.append(f"shot {shot.get('id')}: summaryKo 가 비어 있습니다")
    elif len(summary) > 80:
        problems.append(f"shot {shot.get('id')}: summaryKo 가 80자를 넘습니다")

    if not problems:
        prompt = build_prompt(shot, characters, style)
        if len(prompt) > MAX_PROMPT_CHARS:
            problems.append(
                f"shot {shot.get('id')}: 조립 결과가 {MAX_PROMPT_CHARS}자를 넘습니다"
                f" ({len(prompt)}자). 자르지 않고 검토 필요로 둡니다"
            )
    return problems


def _fit_text(text: str, limit: int) -> str:
    """limit 자 안으로 줄인다. 문장 · 쉼표 · 단어 경계에서 자르고 끝의 접속어를 정리한다."""
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    head = value[:limit]
    cut = -1
    for mark in (". ", "; ", ", "):
        position = head.rfind(mark)
        if position >= int(limit * 0.55):
            cut = position
            break
    if cut < 0:
        cut = head.rfind(" ")
    out = head[: cut if cut > 0 else limit].rstrip(" ,;:-")
    out = re.sub(r"\s+(and|with|while|as|of|in|on|at|to|the|a|an)$", "", out, flags=re.IGNORECASE)
    return out.rstrip(" ,;:-")


def fit_shot(shot: dict) -> dict:
    """길이 예산을 넘은 칸만 경계에서 줄인 사본(설계자 2026-10-13).

    몇 글자 넘었다고 "검토 필요"로 막혀 승인이 안 됐다(예: locationEn 123자 > 120자).
    길이 외 위반(한글 · 이름 · 부정어 · 인물 수)은 그대로 둔다 — check_shot 이 잡는다.
    """
    if not isinstance(shot, dict):
        return shot
    fitted = dict(shot)
    for field, limit in (
        ("locationEn", BUDGET["LOCATION"]),
        ("actionEn", BUDGET["ACTION"]),
        ("cameraEn", BUDGET["CAMERA"]),
        ("summaryKo", 80),
    ):
        if fitted.get(field):
            fitted[field] = _fit_text(fitted[field], limit)
    return fitted


def check_character(character: dict) -> list[str]:
    problems: list[str] = []
    for field, limit in (("appearanceEn", BUDGET["APPEARANCE"]), ("handleEn", 60), ("bodyEn", 80)):
        value = str(character.get(field) or "").strip()
        if field == "appearanceEn" and not value:
            problems.append(f"character {character.get('id')}: appearanceEn 이 비어 있습니다")
        if not value:
            continue
        if HANGUL.search(value):
            problems.append(f"character {character.get('id')}: {field} 에 한글이 있습니다")
        if len(value) > limit:
            problems.append(
                f"character {character.get('id')}: {field} 가 {limit}자를 넘습니다 ({len(value)}자)"
            )
    return problems
