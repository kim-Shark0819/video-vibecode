"""검수자 — 2단 검사. 1차는 코드(무료), 2차는 Haiku(일관성).

## 왜 2단인가 (지시서 1절)

    "1차 코드 스키마 검사(무료) → 2차 Haiku 일관성 검사.
     실패 시 notes를 붙여 1회 재생성, 그래도 실패면 needs_review로 사용자에게 표시"

형식 오류로 LLM 을 부르는 것은 낭비다. 그리고 형식이 깨진 결과에 "인물명이 맞느냐"고
물어도 의미가 없다. 그래서 코드가 먼저 본다.

1차에서 잡는 것: 스키마(roles/_json.check_schema), 컷 수, 회차 번호, 엔딩 훅 존재,
                바이블에 없는 인물 이름 (문자열 대조로 확실히 잡히는 것)
2차에서 잡는 것: 설정 불일치, 고유명사 변형 (판단이 필요한 것)

**엔딩 훅은 2차에 맡기지 않는다.** 존재 여부는 1차가 확실히 본다. "충분히 궁금한가"를
모델에게 물으면 멀쩡한 훅을 계속 반려한다 — 프롬프트를 두 번 조여도 그랬다. 그래서
2차가 낸 ending_hook 지적은 `_MODEL_BLOCKED_KINDS` 에서 버린다.

1차에서 이미 잡았으면 2차를 부르지 않는다. 어차피 재생성해야 하므로 Haiku 비용을
쓸 이유가 없다.

## 재생성 루프를 1회로 제한하는 이유

무한 루프가 되면 한 결과물에 모델 호출이 수십 번 붙는다. 검수자가 잘못 판정하는 경우도
있으므로(특히 판단이 필요한 항목) 1회로 끊고 `needs_review` 로 사용자에게 넘긴다.
사람이 보고 판단하는 것이 모델을 계속 돌리는 것보다 싸고 정확하다.
"""

from __future__ import annotations

import json
import logging

from .. import prompts_story, story_context, story_models
from . import _json

logger = logging.getLogger("toon-change-ai-pj-app.story.validator")

STATUS_OK = "ok"
STATUS_NEEDS_REVIEW = "needs_review"

KIND_LABELS = {
    "character_name": "인물명 불일치",
    "setting": "설정 불일치",
    "proper_noun": "고유명사 변형",
    "ending_hook": "엔딩 훅 부재",
    "length": "분량·컷 수 위반",
    "schema": "형식 오류",
}
_KINDS = set(KIND_LABELS)

# 2차(모델)가 낸 지적 중 버릴 종류.
#
# ending_hook: 코드가 존재 여부를 확실히 판정한다. 모델에게 "충분한가"를 물으면
#              멀쩡한 훅을 계속 반려한다(check_model 안의 주석 참고).
# schema:      1차가 이미 봤다. 2차가 다시 형식을 지적하면 같은 결함이 두 번 세어진다.
_MODEL_BLOCKED_KINDS = {"ending_hook", "schema"}


def _note(kind: str, detail: str, fix: str = "") -> dict:
    return {
        "kind": kind if kind in _KINDS else "setting",
        "detail": detail[:400],
        "fix": fix[:400],
        "label": KIND_LABELS.get(kind, kind),
    }


# --------------------------------------------------------------------------- #
# 1차: 코드 검사 (무료)
# --------------------------------------------------------------------------- #
def check_code(output: dict, bible: dict, kind: str, options: dict | None = None) -> list[dict]:
    """확실히 판정할 수 있는 것만 검사한다. 판단이 필요한 것은 2차에 맡긴다."""
    options = options or {}
    notes: list[dict] = []
    known_names = {str(c.get("name") or "").strip() for c in (bible.get("characters") or [])}
    known_names.discard("")

    if kind == story_context.OUT_STORYBOARD:
        requested = int(options.get("cut_count") or output.get("requestedCutCount") or 0)
        actual = len(output.get("cuts") or [])
        if requested and actual != requested:
            notes.append(
                _note("length", f"컷을 {requested}개 요청했는데 {actual}개입니다.",
                      f"컷을 정확히 {requested}개로 맞추세요.")
            )
        # image_prompt 가 영어여야 한다. 한국어가 들어가면 확산 모델이 뭉갠다.
        korean_cuts = [
            cut["number"] for cut in (output.get("cuts") or [])
            if _has_korean(str(cut.get("scene") or ""))
        ]
        if korean_cuts:
            preview = ", ".join(str(n) for n in korean_cuts[:6])
            notes.append(
                _note("schema", f"{preview}번 컷의 image_prompt 에 한국어가 있습니다.",
                      "image_prompt 는 영어로만 쓰세요.")
            )

    elif kind == story_context.OUT_SCRIPT:
        if not str(output.get("ending_hook") or "").strip():
            notes.append(_note("ending_hook", "엔딩 훅이 비어 있습니다.",
                               "마지막 씬이 다음 회차를 궁금하게 만들고, 그것을 ending_hook 에 한 문장으로 적으세요."))
        expected_no = options.get("episode_no")
        if expected_no and int(output.get("episode_no") or 0) != int(expected_no):
            notes.append(
                _note("schema", f"{expected_no}화를 요청했는데 episode_no 가 {output.get('episode_no')} 입니다.",
                      f"episode_no 를 {expected_no} 로 맞추세요.")
            )
        # 바이블에 없는 인물이 대사를 하는 경우. 문자열 대조라 확실하다.
        if known_names:
            unknown = set()
            for scene in output.get("scenes") or []:
                for line in scene.get("dialogue") or []:
                    who = str(line.get("character") or "").strip()
                    # 괄호 주석이나 "여주(58)" 같은 표기를 감안해 부분 일치도 인정한다.
                    if who and not any(name in who or who in name for name in known_names):
                        unknown.add(who)
            if unknown:
                preview = ", ".join(sorted(unknown)[:6])
                notes.append(
                    _note("character_name", f"바이블에 없는 인물이 대사를 합니다: {preview}",
                          f"바이블의 인물({', '.join(sorted(known_names)[:8])})만 쓰거나, 이름 표기를 바이블과 맞추세요.")
                )

    elif kind == story_context.OUT_LOGLINE:
        options_list = output.get("options") or []
        if len(options_list) < 2:
            notes.append(_note("length", f"로그라인 안이 {len(options_list)}개입니다.",
                               "서로 다른 각도의 3안을 쓰세요."))
        # 세 안이 사실상 같은 문장인 경우. 앞 20자가 겹치면 각도를 나누지 않은 것이다.
        heads = [str(item.get("logline") or "")[:20] for item in options_list]
        if len(heads) >= 2 and len(set(heads)) < len(heads):
            notes.append(_note("length", "여러 안의 시작이 동일합니다.",
                               "목표 중심 / 갈등 중심 / 분위기 중심으로 각도를 나누세요."))

    return notes


def _has_korean(text: str) -> bool:
    return any("\uac00" <= ch <= "\ud7a3" for ch in text)


# --------------------------------------------------------------------------- #
# 2차: 모델 검사 (일관성)
# --------------------------------------------------------------------------- #
def _requested_conditions(kind: str, options: dict | None) -> str:
    """검수자에게 알려 줄 요청 조건.

    이것이 없으면 검수자가 **기준 없이 분량을 판정한다.** 실측에서 씬 1개짜리
    회차에 대해 목표를 모른 채 분량 위반을 지적했다. 목표를 모르면 지적할 수 없어야 한다.
    """
    options = options or {}
    lines: list[str] = []

    if kind == story_context.OUT_SCRIPT:
        length = options.get("length")
        if length:
            target = {
                story_context.LENGTH_SHORT: "씬 3~5개",
                story_context.LENGTH_NORMAL: "씬 6~10개",
                story_context.LENGTH_LONG: "씬 10~16개",
            }.get(length)
            if target:
                lines.append(f"- 목표 분량: {target} ({story_context.LENGTH_LABELS.get(length, length)})")
        if options.get("episode_no"):
            lines.append(f"- 회차 번호: {options['episode_no']}화")
    elif kind == story_context.OUT_STORYBOARD:
        if options.get("cut_count"):
            lines.append(f"- 목표 컷 수: {options['cut_count']}개")
    elif kind == story_context.OUT_LOGLINE:
        lines.append("- 목표: 서로 다른 각도의 3안")

    if not lines:
        return (
            "[요청 조건] 분량·개수에 대한 목표가 주어지지 않았습니다. "
            "**분량과 개수는 검사하지 마세요.** 일관성만 봅니다."
        )
    return "[요청 조건] 아래 목표에 대해서만 분량·개수를 판정하세요.\n" + "\n".join(lines)


def check_model(caller, output: dict, bible: dict, kind: str, options: dict | None = None) -> list[dict]:
    """일관성 검사. 실패해도 통과로 본다.

    검수 호출이 실패했다는 이유로 결과물을 needs_review 로 만들지 않는다.
    검수는 품질을 올리는 보조 단계이고, 그 단계의 장애가 정상 결과물을
    문제 있는 것으로 보이게 하면 안 된다(이 코드베이스의 보조 단계 공통 태도).
    """
    spec = story_models.spec(story_models.ROLE_VALIDATOR)

    # 검수에 결과물 전문을 넣으면 회차 대본이 1.6만 자라 Haiku 입력이 커진다.
    # 검사 항목에 필요한 부분만 넣는다.
    payload_text = json.dumps(_digest_for_review(output, kind), ensure_ascii=False)
    user = "\n".join(
        [
            "[작품 바이블]",
            story_context.bible_digest(bible, max_chars=5000),
            "",
            _requested_conditions(kind, options),
            "",
            f"[검수할 결과물] 종류: {story_context.OUTPUT_KIND_LABELS.get(kind, kind)}",
            payload_text[:24000],
        ]
    )

    try:
        parsed, _meta = _json.call_json(
            caller, prompts_story.VALIDATE, [{"text": user}],
            spec["max_tokens"], spec["temperature"], "validate",
        )
    except _json.RoleError as error:
        logger.warning("검수 호출이 실패해 통과로 처리합니다: %s", error)
        return []
    except Exception as error:  # noqa: BLE001
        logger.warning("검수에서 예상치 못한 오류. 통과로 처리합니다: %s", error)
        return []

    if parsed.get("pass") is True:
        return []

    notes = []
    for item in parsed.get("notes") or []:
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail") or "").strip()
        if not detail:
            continue
        kind_name = str(item.get("kind") or "")
        if kind_name in _MODEL_BLOCKED_KINDS:
            # 엔딩 훅은 코드가 판정한다(1차 check_code). 모델에게 맡기지 않는 이유:
            # "훅이 있는가"는 문자열 검사로 확실하고, "훅이 충분한가"는 판단이라
            # 프롬프트를 두 번 조여도 멀쩡한 훅을 계속 반려했다("다음 화에서 진우가
            # 그 열쇠를 다시 들고 온다"를 부족하다고 지적). 지시서 6절이 요구한 것은
            # "엔딩 훅 부재"이고 그것은 코드가 이미 잡는다. 잘못된 반려 하나가
            # 재생성을 한 번 더 돌려 비용을 두 배로 만든다.
            logger.info("검수 2차가 %s 를 지적했지만 코드 판정 항목이라 무시합니다: %s", kind_name, detail[:80])
            continue
        notes.append(_note(kind_name, detail, str(item.get("fix") or "")))
    # pass=false 인데 notes 가 비어 있으면 판정 근거가 없다. 통과로 본다 —
    # 근거 없이 재생성을 돌리면 비용만 쓰고 같은 결과가 나온다.
    if not notes:
        logger.info("검수가 pass=false 이지만 notes 가 비어 근거가 없습니다. 통과로 봅니다.")
    return notes[:8]


def _digest_for_review(output: dict, kind: str) -> dict:
    """검수에 필요한 부분만 남긴다."""
    if kind == story_context.OUT_SCRIPT:
        return {
            "episode_no": output.get("episode_no"),
            "title": output.get("title"),
            "ending_hook": output.get("ending_hook"),
            "scenes": [
                {
                    "slug": scene.get("slug"),
                    # 지문 전문은 필요 없다. 인물·설정 불일치는 앞부분에서 드러난다.
                    "action": str(scene.get("action") or "")[:400],
                    "dialogue": [
                        {"character": line.get("character"), "line": str(line.get("line") or "")[:200]}
                        for line in (scene.get("dialogue") or [])
                    ],
                }
                for scene in (output.get("scenes") or [])
            ],
        }
    if kind == story_context.OUT_STORYBOARD:
        return {
            "title": output.get("title"),
            "requestedCutCount": output.get("requestedCutCount"),
            "cuts": [
                {"number": cut.get("number"), "context": cut.get("context"),
                 "dialogue": cut.get("dialogue")}
                for cut in (output.get("cuts") or [])
            ],
        }
    return output


# --------------------------------------------------------------------------- #
# 진입점
# --------------------------------------------------------------------------- #
def validate(caller, output: dict, bible: dict, kind: str, options: dict | None = None) -> dict:
    """{"pass": bool, "notes": [...], "stage": "code"|"model"|"none"} 를 돌려준다."""
    code_notes = check_code(output, bible, kind, options)
    if code_notes:
        logger.info("1차 코드 검사에서 %s건 발견 kind=%s", len(code_notes), kind)
        return {"pass": False, "notes": code_notes, "stage": "code"}

    model_notes = check_model(caller, output, bible, kind, options)
    if model_notes:
        logger.info("2차 일관성 검사에서 %s건 발견 kind=%s", len(model_notes), kind)
        return {"pass": False, "notes": model_notes, "stage": "model"}

    return {"pass": True, "notes": [], "stage": "model"}
