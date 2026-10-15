"""결과물 작성자 — 로그라인 / 콘티 / 대본 아웃라인 / 대본 회차.

## 이 역할은 원문 타입을 모른다

바이블이 중간 표현이므로(roles/bible.py 참고) 여기서는 원문이 대본이었는지 로그라인이었는지
알 필요가 없다. 예외가 하나 있다 — 지시서 5절의 이어쓰기 판단:

    "입력이 대본이고 결과물도 대본이면: 범위가 원문 안이면 재작성, 이후면 이어쓰기"

그 판단은 **라우터가** 하고, 그 결과를 `continuation` 로 넘겨받는다. 작성자가 직접
story_type 을 보고 분기하면 바이블을 중간 표현으로 둔 의미가 없어진다.

## 결과물 종류별로 모델이 다르다

story_models 표가 갖고 있다. 이 모듈은 역할 id 만 알고 모델 ID 는 모른다.
"""

from __future__ import annotations

import logging

from .. import prompts_story, story_context, story_models
from . import _json

logger = logging.getLogger("toon-change-ai-pj-app.story.writer")

_SHOTS = {"wide", "medium", "close-up", "over-the-shoulder"}


def _context_block(bible: dict, story_text: str, notes: list[dict] | None = None) -> list[str]:
    """모든 결과물이 공유하는 입력 앞부분."""
    parts = ["[작품 바이블]", story_context.bible_digest(bible)]

    excerpt = story_context.story_excerpt(story_text)
    if excerpt:
        parts += [
            "",
            "[원문 발췌] 말투와 문체를 맞추는 근거로만 쓰세요. 여기 없는 사건을 만들지 마세요.",
            excerpt,
        ]

    if notes:
        # 재생성이다. 검수자가 무엇을 지적했는지 붙인다.
        # 지적만 붙이면 같은 실수를 반복하므로 fix(고칠 방향)를 함께 넣는다.
        lines = []
        for note in notes:
            detail = str(note.get("detail") or "").strip()
            fix = str(note.get("fix") or "").strip()
            if detail:
                lines.append(f"- {detail}" + (f" → {fix}" if fix else ""))
        if lines:
            parts += [
                "",
                "[직전 결과의 문제 — 반드시 고치세요]",
                *lines,
            ]
    return parts


# --------------------------------------------------------------------------- #
# 로그라인
# --------------------------------------------------------------------------- #
def logline(caller, bible: dict, story_text: str = "", notes: list[dict] | None = None) -> dict:
    spec = story_models.spec(story_models.ROLE_LOGLINE)
    parts = _context_block(bible, story_text, notes)
    parts += ["", "[요청] 서로 다른 각도의 로그라인 3안을 쓰세요."]

    payload, meta = _json.call_json(
        caller, prompts_story.LOGLINE, [{"text": "\n".join(parts)}],
        spec["max_tokens"], spec["temperature"], "logline",
    )

    options = []
    for index, item in enumerate(payload.get("options") or [], start=1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("logline") or "").strip()
        if not text:
            continue
        options.append(
            {
                "no": index,
                "angle": str(item.get("angle") or "").strip()[:40],
                "logline": text[:600],
                "genre": str(item.get("genre") or "").strip()[:80],
                "tone": str(item.get("tone") or "").strip()[:200],
                "why": str(item.get("why") or "").strip()[:200],
            }
        )
    if not options:
        raise _json.RoleError("로그라인을 만들지 못했습니다. 응답에 유효한 안이 없습니다.")

    try:
        recommended = int(payload.get("recommended") or 1)
    except (TypeError, ValueError):
        recommended = 1
    if not 1 <= recommended <= len(options):
        recommended = 1

    return {
        "kind": story_context.OUT_LOGLINE,
        "options": options[:3],
        "recommended": recommended,
        "retried": bool(meta.get("retried")),
    }


# --------------------------------------------------------------------------- #
# 콘티
# --------------------------------------------------------------------------- #
def storyboard(
    caller,
    bible: dict,
    cut_count: int,
    story_text: str = "",
    summary_story: str = "",
    notes: list[dict] | None = None,
) -> dict:
    """콘티. 컷 배열을 돌려준다. 이미지 생성은 라우터가 기존 경로로 넘긴다.

    `summary_story` 를 받는 이유: 지시서 3절이 요약 스토리를 "이미지 생성 참고용"으로
    정했다. 컷의 image_prompt 를 만들 때 전체 맥락이 있으면 컷이 이야기에서 겉돌지 않는다.
    """
    spec = story_models.spec(story_models.ROLE_STORYBOARD)
    parts = _context_block(bible, story_text, notes)

    if (summary_story or "").strip():
        parts += ["", "[요약 스토리] 컷이 이 이야기 안에 있어야 합니다.",
                  summary_story.strip()[:story_context.SUMMARY_MAX_CHARS]]

    parts += ["", f"[요청] 컷을 정확히 {int(cut_count)}개 만드세요. 많거나 적으면 폐기됩니다."]

    payload, meta = _json.call_json(
        caller, prompts_story.STORYBOARD, [{"text": "\n".join(parts)}],
        spec["max_tokens"], spec["temperature"], "storyboard",
    )

    cuts = []
    for index, item in enumerate(payload.get("cuts") or [], start=1):
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        image_prompt = str(item.get("image_prompt") or "").strip()
        if not (description or image_prompt):
            continue
        shot = str(item.get("shot_size") or "").strip().lower().replace("_", "-")
        if shot not in _SHOTS:
            shot = "medium"
        try:
            seconds = int(item.get("seconds") or 4)
        except (TypeError, ValueError):
            seconds = 4
        cuts.append(
            {
                # 기존 콘티 구조(graph_store storyboard cuts)와 필드 이름을 맞춘다.
                # 그래야 이미지 생성 경로를 그대로 재사용할 수 있다.
                "number": index,
                "shot": shot,
                "context": description[:600],
                "scene": image_prompt[:600],
                "dialogue": str(item.get("dialogue") or "").strip()[:400],
                # 콘티 양식의 Audio 칸. 예전에는 빈 문자열로 고정해 두어 인쇄한
                # 기획표의 Audio 칸이 대사 없는 컷에서 통째로 비었다.
                # 길이 상한은 server.cut_fields_from 의 편집 상한과 같게 둔다.
                "audio": str(item.get("sound") or item.get("audio") or "").strip()[:300],
                "note": str(item.get("note") or "").strip()[:300],
                "seconds": max(1, min(30, seconds)),
                "status": "pending",
                "error": "",
                "imageKey": "",
            }
        )

    if not cuts:
        raise _json.RoleError("콘티 컷을 만들지 못했습니다.")

    return {
        "kind": story_context.OUT_STORYBOARD,
        "title": str(payload.get("title") or "").strip()[:200],
        # 모든 컷이 공유하는 영어 블록. 기존 documents.storyboard_scene_prompt 와 같은 역할이다.
        "characters": str(payload.get("characters") or "").strip()[:600],
        "setting": str(payload.get("setting") or "").strip()[:600],
        "cuts": cuts,
        "requestedCutCount": int(cut_count),
        "retried": bool(meta.get("retried")),
    }


# --------------------------------------------------------------------------- #
# 대본 아웃라인
# --------------------------------------------------------------------------- #
def script_outline(
    caller,
    bible: dict,
    episode_from: int,
    episode_to: int,
    length: str = story_context.LENGTH_NORMAL,
    story_text: str = "",
    continuation: bool = False,
    notes: list[dict] | None = None,
    quality: str = story_models.QUALITY_STANDARD,
) -> dict:
    spec = story_models.spec(story_models.ROLE_SCRIPT_OUTLINE, quality=quality)
    count = episode_to - episode_from + 1
    parts = _context_block(bible, story_text, notes)

    if continuation:
        # 지시서 5절의 이어쓰기. 원문 뒤를 잇는 것이라 원문에 이미 있는 사건을
        # 다시 쓰지 않아야 한다.
        parts += [
            "",
            "[이어쓰기] 원문은 이미 끝난 부분입니다. 그 뒤에 이어지는 회차를 쓰세요.",
            "원문에 이미 나온 사건을 다시 쓰지 마세요. 원문의 결말 상태에서 시작합니다.",
        ]
    parts += [
        "",
        f"[요청] {episode_from}화부터 {episode_to}화까지 정확히 {count}개 회차의 아웃라인을 쓰세요.",
        f"회차 분량 기준: {story_context.LENGTH_LABELS.get(length, length)}",
    ]

    payload, meta = _json.call_json(
        caller, prompts_story.SCRIPT_OUTLINE, [{"text": "\n".join(parts)}],
        spec["max_tokens"], spec["temperature"], "script_outline",
    )

    episodes = []
    for offset, item in enumerate(payload.get("episodes") or []):
        if not isinstance(item, dict):
            continue
        # 번호는 서버가 부여한다. 모델이 1부터 다시 세는 경우가 있어(범위가 5~7일 때도
        # 1,2,3 으로 내려온다) 그대로 쓰면 회차 번호가 틀어진다.
        no = episode_from + offset
        if no > episode_to:
            break
        episodes.append(
            {
                "no": no,
                "title": str(item.get("title") or "").strip()[:200],
                "summary": str(item.get("summary") or "").strip()[:1000],
                "beats": [
                    str(beat).strip()[:300]
                    for beat in (item.get("beats") or [])
                    if str(beat or "").strip()
                ][:10],
                "characters": [
                    str(name).strip()[:60]
                    for name in (item.get("characters") or [])
                    if str(name or "").strip()
                ][:12],
                "ending_hook": str(item.get("ending_hook") or "").strip()[:400],
            }
        )

    if len(episodes) != count:
        logger.warning("아웃라인 회차 수가 다릅니다 요청=%s 응답=%s", count, len(episodes))
    if not episodes:
        raise _json.RoleError("아웃라인을 만들지 못했습니다.")

    return {
        "arc": str(payload.get("arc") or "").strip()[:1000],
        "episodes": episodes,
        "episodeFrom": episode_from,
        "episodeTo": episode_to,
        "length": length,
        "continuation": continuation,
        "retried": bool(meta.get("retried")),
    }


# --------------------------------------------------------------------------- #
# 대본 회차
# --------------------------------------------------------------------------- #
def script_episode(
    caller,
    bible: dict,
    outline_episode: dict,
    arc: str = "",
    previous_summary: str = "",
    length: str = story_context.LENGTH_NORMAL,
    story_text: str = "",
    notes: list[dict] | None = None,
    quality: str = story_models.QUALITY_STANDARD,
) -> dict:
    """회차 하나. 회차당 1콜이다.

    `previous_summary` 를 받는 이유 (지시서 5절): 회차 간 연속성. 직전 회차 전문을
    넣으면 입력 토큰이 회차마다 누적되어 12화에서 감당이 안 된다. 요약만 넘긴다.
    그 요약은 이 함수가 결과에 함께 담아 돌려주고, 라우터가 다음 회차에 넘긴다.
    """
    spec = story_models.spec(story_models.ROLE_SCRIPT_EPISODE, quality=quality)
    no = int(outline_episode.get("no") or 1)
    parts = _context_block(bible, story_text, notes)

    if arc.strip():
        parts += ["", "[전체 흐름]", arc.strip()[:1000]]

    if previous_summary.strip():
        parts += [
            "",
            "[직전 회차 요약] 이 상태에서 이어집니다. 이미 일어난 일을 되돌리지 마세요.",
            previous_summary.strip()[:1500],
        ]
    else:
        parts += ["", "[직전 회차] 없습니다. 이 회차가 시작입니다."]

    beats = outline_episode.get("beats") or []
    parts += [
        "",
        f"[이 회차의 아웃라인] {no}화 — {outline_episode.get('title', '')}",
        f"요약: {outline_episode.get('summary', '')}",
    ]
    if beats:
        parts.append("장면 순서:")
        parts += [f"  {index}. {beat}" for index, beat in enumerate(beats, start=1)]
    if outline_episode.get("ending_hook"):
        parts.append(f"엔딩 훅(반드시 실현): {outline_episode['ending_hook']}")

    parts += ["", f"[요청] {no}화 대본을 쓰세요. 분량 기준: {story_context.LENGTH_LABELS.get(length, length)}"]

    payload, meta = _json.call_json(
        caller, prompts_story.SCRIPT_EPISODE, [{"text": "\n".join(parts)}],
        spec["max_tokens"], spec["temperature"], "script_episode",
    )

    scenes = []
    for item in payload.get("scenes") or []:
        if not isinstance(item, dict):
            continue
        dialogue = []
        for line in item.get("dialogue") or []:
            if not isinstance(line, dict):
                continue
            text = str(line.get("line") or "").strip()
            if not text:
                continue
            dialogue.append(
                {
                    "character": str(line.get("character") or "").strip()[:60],
                    "line": text[:1500],
                }
            )
        slug = str(item.get("slug") or "").strip()
        action = str(item.get("action") or "").strip()
        if not (slug or action or dialogue):
            continue
        scenes.append({"slug": slug[:200], "action": action[:3000], "dialogue": dialogue})

    if not scenes:
        raise _json.RoleError(f"{no}화 대본을 만들지 못했습니다. 씬이 하나도 없습니다.")

    return {
        "episode_no": no,
        "title": str(payload.get("title") or "").strip()[:200],
        "scenes": scenes,
        "ending_hook": str(payload.get("ending_hook") or "").strip()[:400],
        # 다음 회차의 입력이 된다. 모델이 안 주면 아웃라인 요약으로 대신한다 —
        # 비워 두면 다음 회차가 연속성 근거를 잃는다.
        "summary": (
            str(payload.get("summary") or "").strip()
            or str(outline_episode.get("summary") or "").strip()
        )[:1500],
        "retried": bool(meta.get("retried")),
    }


def episode_char_count(episode: dict) -> int:
    """회차 대본의 글자 수. 화면 표시와 분량 검사에 쓴다."""
    total = len(str(episode.get("title") or ""))
    for scene in episode.get("scenes") or []:
        total += len(str(scene.get("slug") or "")) + len(str(scene.get("action") or ""))
        for line in scene.get("dialogue") or []:
            total += len(str(line.get("character") or "")) + len(str(line.get("line") or ""))
    return total
