"""Claude 호출 — 명령 해석(연출) · 확정 이미지에서 영상용 외형 문장.

규칙 (vpoc1 교훈): 도구 1개 강제 → 코드 검사 → 위반 목록을 붙여 1회 재요청. 읽기 300초, 재시도 0.
"""
from __future__ import annotations

import base64
import json
import threading
from typing import Any

from . import compose, settings
from .errors import Vpoc2Error, from_client_error

DIRECTOR_SYSTEM = """You are a film director and prompt writer for a text-to-video model (Luma Ray2).
A Korean user gives a short command describing a scene. Your job is to turn it into ONE continuous
15-second scene made of exactly 3 shots of 5 seconds each (beginning -> development -> ending), so that
the final video reflects EVERY part of the user's command.

Work in this order:
1. elements: split the user's command into small checkable elements (who, what object, where,
   time/weather, action, change of emotion or state, relationship, mood, camera wish, style wish).
   Each element is one short Korean phrase. Keep the user's own words. Do not invent elements the user
   did not ask for. ids: e1, e2, ...
2. characters: at most 2 people, animals or creatures that appear. Keep every fact the user wrote
   (species, names, colors, fur/hair length, patterns, clothes, relationship) unchanged; fill only what
   is missing. If no name is given, do not invent one: use a display name like "여자 1" or "고양이 1" for
   nameKo and an empty string for nameEn.
   kind: "human", "animal" or "creature".
   speciesEn: what it is in one or two words ("woman", "cat", "golden retriever", "dragon").
   mustKeepEn: every visual trait the USER explicitly wrote about this character, translated literally
   into short English phrases (e.g. user "털이 짧고 까만 고양이" -> ["black", "short fur", "cat"]).
   Empty list only if the user wrote no visual trait. Never add traits the user did not write here.
   appearanceEn: 25-40 words of plain English that START with the mustKeepEn traits and the species
   (e.g. "A black short-haired cat with ..."), then fill the rest.
     human: hair, face impression, clothes, one signature item.
     animal/creature: fur/feather/skin color and pattern, fur length, body size and build, eye color,
     one signature detail. Use single solid colors exactly as the user wrote them; describe a black
     animal as black all over.
   handleEn: a short unique way to refer to the character in shot text (e.g. "the black cat").
3. look: ONE visual look shared by all shots (color palette, light quality, lens/texture, mood),
   derived from the command. lookEn max 160 chars.
4. setting: ONE location and time for the whole scene. All shots happen inside it.
5. shots: exactly 3. Every element id must be assigned to at least one shot (elementIds). Spread the
   story: shot 1 establishes, shot 2 develops, shot 3 lands the key change the user asked for.
   For each shot write:
   - framingEn: shot size and composition (who is where in frame, foreground/background).
   - locationEn: the specific part of the setting seen in this shot.
   - beatsEn: what visibly changes during the 5 seconds, as "starts ... then ..." with concrete body
     movement, facial expression and object motion. Physical and filmable. This is the most important field.
   - lightEn: light in this shot, consistent with the look.
   - detailEn: 1-2 concrete visual details that make the frame vivid (rain drops on the umbrella, breath fog...).
   - cameraEn: one camera move with speed (slow push-in, gentle handheld drift, static locked-off...).
   - cameraKo: the camera in easy Korean with the term in parentheses, e.g. "천천히 다가가기(푸시 인)".
   - summaryKo: one Korean sentence the user will read.

Hard rules for every English field:
- English only. Never write character names; refer to people only by their handleEn.
- Positive phrasing only. Never use negation words (no, not, without, never, don't...). Describe what IS
  in the frame instead.
- No real people, brands, teams, logos or on-screen text.
- Kissing, nudity and violence only if the user explicitly asked. Physical contact up to hands, shoulders, hug.
- Stay within the character budgets given in the tool schema descriptions.
style: "live_action" unless the user asks for anime/animation/webtoon style, then "anime".
Always answer by calling the tool emit_interpretation."""

_STR = {"type": "string"}


def _s(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


EMIT_INTERPRETATION = {
    "name": "emit_interpretation",
    "description": "Return the interpretation of the user's command as a 15-second, 3-shot scene.",
    "inputSchema": {"json": {
        "type": "object",
        "required": ["titleKo", "summaryKo", "style", "settingKo", "settingEn", "lookEn",
                     "elements", "characters", "shots"],
        "properties": {
            "titleKo": _s("Korean title, max 30 chars"),
            "summaryKo": _s("Korean one-paragraph summary of the scene, max 200 chars"),
            "style": {"type": "string", "enum": ["live_action", "anime"]},
            "settingKo": _s("Korean setting, max 200 chars"),
            "settingEn": _s("English setting: place, time, weather, light. max 200 chars"),
            "lookEn": _s("English shared look: palette, light quality, lens/texture, mood. max 160 chars"),
            "elements": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
                "type": "object", "required": ["id", "kind", "textKo"],
                "properties": {"id": _STR, "kind": _s("who|object|place|time|action|change|relation|mood|camera|style|other"),
                               "textKo": _s("short Korean phrase from the command")}}},
            "characters": {"type": "array", "maxItems": 2, "items": {
                "type": "object", "required": ["id", "nameKo", "kind", "speciesEn", "mustKeepEn", "roleKo", "appearanceKo",
                             "appearanceEn", "handleEn"],
                "properties": {"id": _s("c1, c2"), "nameKo": _STR, "nameEn": _s("English name, or empty string if the user gave none"),
                               "kind": {"type": "string", "enum": ["human", "animal", "creature"]},
                               "speciesEn": _s("one or two words, e.g. woman, cat"),
                               "mustKeepEn": {"type": "array", "items": _STR,
                                              "description": "visual traits the user explicitly wrote, literal English"},
                               "roleKo": _STR, "appearanceKo": _STR,
                               "appearanceEn": _s("25-40 words, no names"),
                               "handleEn": _s("max 60 chars, unique")}}},
            "shots": {"type": "array", "minItems": 3, "maxItems": 3, "items": {
                "type": "object",
                "required": ["id", "summaryKo", "elementIds", "characters", "framingEn", "locationEn",
                             "beatsEn", "lightEn", "detailEn", "cameraEn", "cameraKo"],
                "properties": {"id": _s("s1, s2, s3"), "summaryKo": _s("max 80 chars"),
                               "elementIds": {"type": "array", "items": _STR},
                               "characters": {"type": "array", "maxItems": 2, "items": _STR},
                               "framingEn": _s("max 110 chars"), "locationEn": _s("max 130 chars"),
                               "beatsEn": _s("max 320 chars, 'starts ... then ...'"),
                               "lightEn": _s("max 100 chars"), "detailEn": _s("max 120 chars"),
                               "cameraEn": _s("max 100 chars"), "cameraKo": _s("max 40 chars")}}},
        },
    }},
}

APPEARANCE_SYSTEM = """You write the fixed English appearance line for a character in a text-to-video prompt.
Look at the confirmed character image and the character sheet. Start with the species/person and the
must-keep traits from the sheet, then what is visible.
  human: hair (color, length, style), face impression, clothes and colors, one signature item.
  animal/creature: fur/feather/skin color and pattern, fur length, build, eye color, one signature detail.
12-20 words, max 120 characters. Plain English, no names, no negation words, no brand names.
The same line will be repeated in every shot. If the image and the sheet disagree, trust the image
(the user confirmed it), but keep every must-keep trait word.
Always answer by calling the tool emit_appearance."""

EMIT_APPEARANCE = {
    "name": "emit_appearance",
    "description": "Return the fixed appearance line.",
    "inputSchema": {"json": {"type": "object", "required": ["appearanceShortEn"],
                             "properties": {"appearanceShortEn": _s("max 120 chars")}}},
}

_client = None
_client_lock = threading.Lock()


def _bedrock():
    global _client
    with _client_lock:
        if _client is None:
            import boto3
            from botocore.config import Config

            _client = boto3.client("bedrock-runtime", region_name=settings.TEXT_REGION,
                                   config=Config(read_timeout=300, connect_timeout=10, retries={"max_attempts": 0}))
        return _client


# 이 스레드에서 금액으로 바꾼 토큰을 모은다. 호출부(service)가 원장에 쓰기 직전에 take_tokens()
# 로 꺼내 간다 — 함수들이 (값, usd, …) 만 돌려주는 모양을 바꾸지 않고 토큰을 남기기 위한 것이다
# (admin 모델 관리 "영상 비용·토큰", 2026-10-07).
_TOKENS = threading.local()


def usd_for(usage: dict[str, Any]) -> float:
    tin = usage.get("inputTokens", 0)
    tout = usage.get("outputTokens", 0)
    _TOKENS.inp = getattr(_TOKENS, "inp", 0) + int(tin or 0)
    _TOKENS.out = getattr(_TOKENS, "out", 0) + int(tout or 0)
    return tin / 1e6 * settings.TEXT_USD_PER_MTOK_IN + tout / 1e6 * settings.TEXT_USD_PER_MTOK_OUT


def take_tokens() -> tuple[int, int]:
    """모은 (입력, 출력) 토큰을 꺼내고 비운다."""
    inp, out = getattr(_TOKENS, "inp", 0), getattr(_TOKENS, "out", 0)
    _TOKENS.inp = _TOKENS.out = 0
    return inp, out


def _converse(system: str, content: list[dict[str, Any]], tool: dict[str, Any], max_tokens: int,
              where: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """(tool input, usage, raw text) 를 돌려준다."""
    try:
        resp = _bedrock().converse(
            modelId=settings.TEXT_MODEL,
            system=[{"text": system}],
            messages=[{"role": "user", "content": content}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.4},
            toolConfig={"tools": [{"toolSpec": tool}], "toolChoice": {"tool": {"name": tool["name"]}}},
        )
    except Exception as exc:  # noqa: BLE001
        raise from_client_error(exc, where) from exc
    usage = resp.get("usage", {})
    blocks = resp.get("output", {}).get("message", {}).get("content", [])
    raw = json.dumps(blocks, ensure_ascii=False)
    for b in blocks:
        if "toolUse" in b:
            return b["toolUse"].get("input") or {}, usage, raw
    raise Vpoc2Error("SchemaViolation", "도구 호출이 없다: " + raw[:2000], where=where, http=502)


def _command_text(command: str, user_characters: list[dict[str, Any]], extra: str = "") -> str:
    lines = ["사용자 명령:", command.strip() or "(비어 있음)"]
    chars = [c for c in user_characters if (c.get("name") or c.get("description"))]
    if chars:
        lines.append("\n사용자가 적은 캐릭터:")
        for c in chars:
            lines.append(f"- 이름: {c.get('name') or '(없음)'} / 설명: {c.get('description') or '(없음)'}")
    if extra:
        lines.append("\n" + extra)
    return "\n".join(lines)


def normalize_interpretation(data: dict[str, Any]) -> dict[str, Any]:
    """모델 출력의 id 를 정리한다 (c1.., s1.., e1..)."""
    data = dict(data)
    data.setdefault("elements", [])
    data.setdefault("characters", [])
    data.setdefault("shots", [])
    for i, s in enumerate(data["shots"], 1):
        s["id"] = f"s{i}"
        s.setdefault("elementIds", [])
        s.setdefault("characters", [])
    return data


def interpret(command: str, user_characters: list[dict[str, Any]]) -> tuple[dict[str, Any], float, list[str]]:
    """(해석, 비용 USD, 남은 위반 목록). 위반이 남으면 화면에서 '검토 필요' 로 보여 준다."""
    text = _command_text(command, user_characters)
    data, usage, raw = _converse(DIRECTOR_SYSTEM, [{"text": text}], EMIT_INTERPRETATION, 8000, "claude.interpret")
    usd = usd_for(usage)
    data = normalize_interpretation(data)
    violations = compose.check_interpretation(data)
    if violations:
        retry = _command_text(command, user_characters,
                              "이전 답에 아래 위반이 있었다. 모두 고쳐서 다시 도구를 호출하라:\n- " + "\n- ".join(violations))
        data2, usage2, _ = _converse(DIRECTOR_SYSTEM, [{"text": retry}], EMIT_INTERPRETATION, 8000,
                                     "claude.interpret.retry")
        usd += usd_for(usage2)
        data = normalize_interpretation(data2)
        violations = compose.check_interpretation(data)
    return data, usd, violations


def appearance_from_image(image_jpeg: bytes, character: dict[str, Any]) -> tuple[str, float, list[str]]:
    sheet = (f"character sheet: kind={character.get('kind')}; species={character.get('speciesEn')}; "
             f"mustKeep={character.get('mustKeepEn')}; role={character.get('roleKo')}; "
             f"appearanceKo={character.get('appearanceKo')}; appearanceEn={character.get('appearanceEn')}")
    content = [{"image": {"format": "jpeg", "source": {"bytes": image_jpeg}}}, {"text": sheet}]
    data, usage, _ = _converse(APPEARANCE_SYSTEM, content, EMIT_APPEARANCE, 500, "claude.appearance")
    usd = usd_for(usage)
    line = (data.get("appearanceShortEn") or "").strip()
    violations = compose.check_text("appearanceShortEn", line, [character]) + compose.check_must_keep(line, character)
    if violations:
        content2 = content + [{"text": "위반을 고쳐라: " + "; ".join(violations)}]
        data, usage2, _ = _converse(APPEARANCE_SYSTEM, content2, EMIT_APPEARANCE, 500, "claude.appearance.retry")
        usd += usd_for(usage2)
        line = (data.get("appearanceShortEn") or "").strip()
        violations = compose.check_text("appearanceShortEn", line, [character]) + compose.check_must_keep(line, character)
    return line, usd, violations


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


REVISE_CHARACTER_SYSTEM = """You update one character sheet for an image and video prompt from the user's Korean note.
Apply the note literally (e.g. "더 까맣게" -> make the fur pure black; "털을 더 짧게" -> shorter fur).
Keep everything the note does not change. Rewrite appearanceEn so it STARTS with the must-keep traits and
the species, 25-40 words, plain English, no names, no negation words. Add any new visual trait the user
states in the note to mustKeepEn (literal short English phrases); keep the old mustKeepEn entries unless
the note explicitly contradicts them (then replace them).
Always answer by calling the tool emit_character."""

EMIT_CHARACTER = {
    "name": "emit_character",
    "description": "Return the revised appearance fields.",
    "inputSchema": {"json": {"type": "object", "required": ["appearanceEn", "mustKeepEn", "appearanceKo"],
                             "properties": {"appearanceEn": _s("25-40 words"),
                                            "mustKeepEn": {"type": "array", "items": _STR},
                                            "appearanceKo": _s("Korean appearance summary")}}},
}


def revise_character(character: dict[str, Any], feedback_ko: str) -> tuple[dict[str, Any], float]:
    """사용자 의견(한국어)으로 캐릭터 영어 외형을 고친다. (고친 캐릭터, 비용 USD)."""
    sheet = json.dumps({k: character.get(k) for k in ("kind", "speciesEn", "mustKeepEn", "appearanceKo",
                                                        "appearanceEn")}, ensure_ascii=False)
    content = [{"text": f"character sheet: {sheet}\n사용자 의견: {feedback_ko}"}]
    data, usage, _ = _converse(REVISE_CHARACTER_SYSTEM, content, EMIT_CHARACTER, 800, "claude.revise_character")
    usd = usd_for(usage)
    updated = dict(character)
    for k in ("appearanceEn", "mustKeepEn", "appearanceKo"):
        if data.get(k):
            updated[k] = data[k]
    problems = compose.check_text("appearanceEn", updated["appearanceEn"], [character]) + \
        compose.check_must_keep(updated["appearanceEn"], updated)
    if problems:
        retry = content + [{"text": "위반을 고쳐라: " + "; ".join(problems)}]
        data, usage2, _ = _converse(REVISE_CHARACTER_SYSTEM, retry, EMIT_CHARACTER, 800, "claude.revise_character.retry")
        usd += usd_for(usage2)
        for k in ("appearanceEn", "mustKeepEn", "appearanceKo"):
            if data.get(k):
                updated[k] = data[k]
    return updated, usd
