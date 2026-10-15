"""영상 프롬프트 조립과 검사. 모델을 부르지 않는다.

조립 틀 (연출 단계 — prompt-quality-plan 2장):
  {STYLE}, {LOOK}. {FRAMING}. {LOCATION}. {C1}[; {C2}]. {BEATS}. {LIGHT}. {DETAIL}. {CAMERA}.

규칙 (vpoc1 실측 교훈):
  - 영어만. 캐릭터 이름 대신 handle 로 지칭. 부정어 금지 (Luma 가 부정을 제대로 못 읽는다)
  - 인물 외형 문장은 모든 샷에서 글자 하나 다르지 않게 같은 문자열 (일관성 장치)
  - 넘치면 자르지 않는다. 위반 목록을 돌려주고 샷을 '검토 필요' 로 둔다
"""
from __future__ import annotations

import re
from typing import Any

from . import settings

STYLE = {
    "live_action": "Cinematic live-action film footage, photorealistic, natural skin texture",
    "anime": "2D anime-style animation, clean line art, cel shading",
}
STYLE_IMAGE = {
    "live_action": "Cinematic photograph, photorealistic, 35mm lens, soft natural light",
    "anime": "2D anime-style character illustration, clean line art, cel shading",
}

# 필드별 예산 (글자 수)
BUDGET = {
    "lookEn": 160,
    "settingEn": 200,
    "framingEn": 110,
    "locationEn": 130,
    "appearanceShortEn": 120,
    "handleEn": 60,
    "beatsEn": 320,
    "lightEn": 100,
    "detailEn": 120,
    "cameraEn": 100,
}
SHOT_REQUIRED = ("framingEn", "locationEn", "beatsEn", "cameraEn")
SHOT_OPTIONAL = ("lightEn", "detailEn")

_HANGUL = re.compile(r"[ㄱ-ㆎ가-힣]")
_NEGATION = re.compile(r"\b(no|not|without|never|don't|doesn't|isn't|aren't|won't|cannot|can't|nobody|nothing)\b",
                       re.IGNORECASE)


def _name_patterns(characters: list[dict[str, Any]]) -> list[tuple[str, re.Pattern]]:
    pats = []
    for c in characters:
        for field in ("nameKo", "nameEn"):
            name = (c.get(field) or "").strip()
            if len(name) >= 2:
                pats.append((name, re.compile(r"(?<![\w가-힣])" + re.escape(name) + r"(?![\w가-힣])", re.IGNORECASE)))
    return pats


def check_text(field: str, text: str, characters: list[dict[str, Any]], *, required: bool = True) -> list[str]:
    """영어 필드 하나를 검사해 위반 목록(한국어)을 돌려준다."""
    out: list[str] = []
    text = (text or "").strip()
    if not text:
        if required:
            out.append(f"{field}: 비어 있다")
        return out
    if _HANGUL.search(text):
        out.append(f"{field}: 영어 필드에 한글이 있다")
    neg = _NEGATION.search(text)
    if neg:
        out.append(f"{field}: 부정어 '{neg.group(0)}' 를 쓰지 않는다 (긍정형으로)")
    for name, pat in _name_patterns(characters):
        if pat.search(text):
            out.append(f"{field}: 캐릭터 이름 '{name}' 대신 지칭(handle)을 쓴다")
    limit = BUDGET.get(field)
    if limit and len(text) > limit:
        out.append(f"{field}: {len(text)}자 — 예산 {limit}자를 넘었다")
    return out


def check_interpretation(interp: dict[str, Any]) -> list[str]:
    """AI 해석 결과 전체 검사. 비어 있으면 통과."""
    v: list[str] = []
    chars = interp.get("characters") or []
    elements = interp.get("elements") or []
    shots = interp.get("shots") or []
    if not elements:
        v.append("elements: 명령 요소가 하나도 없다")
    if len(chars) > 2:
        v.append(f"characters: {len(chars)}명 — 1단계는 최대 2명")
    if len(shots) != settings.SHOTS:
        v.append(f"shots: {len(shots)}개 — 정확히 {settings.SHOTS}개여야 한다 (15초 = 5초 × {settings.SHOTS})")
    if interp.get("style") not in STYLE:
        v.append(f"style: '{interp.get('style')}' 는 live_action / anime 중 하나여야 한다")
    v += check_text("lookEn", interp.get("lookEn", ""), chars)
    v += check_text("settingEn", interp.get("settingEn", ""), chars)
    handles = set()
    char_ids = {c.get("id") for c in chars}
    for c in chars:
        v += [f"{c.get('id')}.{x}" for x in check_text("handleEn", c.get("handleEn", ""), chars)]
        v += [f"{c.get('id')}.{x}" for x in check_must_keep(c.get("appearanceEn", ""), c)]
        h = (c.get("handleEn") or "").strip().lower()
        if h in handles:
            v.append(f"{c.get('id')}.handleEn: 다른 캐릭터와 지칭이 같다")
        handles.add(h)
    element_ids = {e.get("id") for e in elements}
    covered: set[str] = set()
    for s in shots:
        sid = s.get("id")
        for f in SHOT_REQUIRED:
            v += [f"{sid}.{x}" for x in check_text(f, s.get(f, ""), chars)]
        for f in SHOT_OPTIONAL:
            v += [f"{sid}.{x}" for x in check_text(f, s.get(f, ""), chars, required=False)]
        if len(s.get("characters") or []) > 2:
            v.append(f"{sid}.characters: 한 샷에 최대 2명")
        for cid in s.get("characters") or []:
            if cid not in char_ids:
                v.append(f"{sid}.characters: 없는 캐릭터 '{cid}'")
        for eid in s.get("elementIds") or []:
            if eid not in element_ids:
                v.append(f"{sid}.elementIds: 없는 요소 '{eid}'")
            covered.add(eid)
    for e in elements:
        if e.get("id") not in covered:
            v.append(f"{e.get('id')}: 명령 요소 '{e.get('textKo')}' 가 어떤 샷에도 배정되지 않았다")
    return v


def assemble(interp: dict[str, Any], shot: dict[str, Any], appearances: dict[str, str]) -> str:
    """샷 하나의 영어 프롬프트. appearances = {charId: appearanceShortEn} (모든 샷에서 같은 문자열)."""
    style = interp.get("style", "live_action")
    parts = [STYLE[style] + (", " + interp["lookEn"].strip().rstrip(".") if interp.get("lookEn") else "")]
    for field in ("framingEn", "locationEn"):
        if shot.get(field):
            parts.append(shot[field].strip().rstrip("."))
    looks = [appearances[cid].strip().rstrip(".") for cid in shot.get("characters") or [] if appearances.get(cid)]
    if looks:
        parts.append("; ".join(looks))
    for field in ("beatsEn", "lightEn", "detailEn", "cameraEn"):
        if shot.get(field):
            parts.append(shot[field].strip().rstrip("."))
    return ". ".join(p for p in parts if p) + "."


def check_prompt(prompt: str, characters: list[dict[str, Any]]) -> list[str]:
    """조립 결과 검사. 자르지 않는다 — 넘으면 위반."""
    v = []
    if not prompt.strip():
        v.append("prompt: 비어 있다 (빈 프롬프트는 과금 작업이 된다)")
    if len(prompt) > settings.MAX_PROMPT_CHARS:
        v.append(f"prompt: {len(prompt)}자 — 상한 {settings.MAX_PROMPT_CHARS}자")
    if _HANGUL.search(prompt):
        v.append("prompt: 한글이 있다")
    neg = _NEGATION.search(prompt)
    if neg:
        v.append(f"prompt: 부정어 '{neg.group(0)}'")
    for name, pat in _name_patterns(characters):
        if pat.search(prompt):
            v.append(f"prompt: 캐릭터 이름 '{name}'")
    return v


def check_must_keep(text: str, character: dict[str, Any]) -> list[str]:
    """사용자가 직접 쓴 외형 특징(mustKeepEn)이 문장에 그대로 들어 있는지 본다 (명령 반영의 최소 조건)."""
    low = (text or "").lower()
    missing = [t for t in (character.get("mustKeepEn") or []) if t and t.strip().lower() not in low]
    if missing:
        return [f"appearance: 사용자가 쓴 특징 {missing} 이 외형 문장에 없다"]
    return []


def character_image_prompt(style: str, character: dict[str, Any], setting_en: str,
                           note_en: str = "") -> tuple[str, str]:
    """캐릭터 이미지 프롬프트. 사용자가 쓴 특징을 맨 앞에 둔다 (SD3.5 는 앞쪽 단어를 강하게 따른다).
    사람이 아니면 사람용 틀(허리 위 · 표정 · 손가락)을 쓰지 않는다."""
    kind = character.get("kind") or "human"
    appearance = (character.get("appearanceEn") or "").strip().rstrip(".")
    must = ", ".join(t.strip() for t in (character.get("mustKeepEn") or []) if t.strip())
    species = (character.get("speciesEn") or "").strip()
    setting = setting_en.strip().rstrip(".")
    style_text = STYLE_IMAGE.get(style, STYLE_IMAGE["live_action"])
    note = f" {note_en.strip().rstrip('.')}." if note_en else ""
    if kind == "human":
        prompt = (f"{style_text}. Waist-up portrait of {appearance}, standing in {setting}, "
                  f"facing the camera, calm expression, sharp focus on the face.{note}")
        negative = "text, watermark, logo, extra people, extra fingers, deformed hands, blurry face, cropped head"
    else:
        lead = f"{must} {species}".strip() if must else species
        prompt = (f"{style_text}. Full-body portrait of a single {lead}: {appearance}. "
                  f"The whole body is visible, sitting in {setting}, looking at the camera, "
                  f"sharp focus on the face and fur texture.{note}")
        negative = "text, watermark, logo, people, humans, extra animals, extra legs, deformed paws, blurry, cropped body"
    return prompt, negative
