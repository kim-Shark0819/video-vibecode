"""vpoc — ④영상 단계 "이 샷만 의견 반영해 다시 만들기".

정본: `docs/vpoc1/regen-plan.md`. 조사 결과는 `docs/vpoc1/regen-review.md`.

원칙(계획 문서 1장):
  G1 추가만 한다. 기존 액션의 동작·응답 키를 바꾸지 않는다.
  G2 `shot.status`·`videoUrl`·`attempt` 는 항상 **지금 쓰는(선택된) 버전**이다. 재생성 진행 상태는
     `shot.regen` 에만 둔다 — 그래서 재생성 중에도 기존 영상이 재생되고 ④ 상태가 바뀌지 않는다.
  G3 조회(GET)는 `jobs/*.json` 만 쓴다. 버전 전환은 사용자 동작(`select`)이 `state.json` 에 쓴다.
  G4 `final.json` 은 덮어쓰지 않는다. 버전을 바꿀 때마다 `finals/{UTC}.json` 을 새로 쓴다.
  G5 제출·조회·원장·조립기·검사·작업 스레드는 기존 함수를 부른다.

`routes.py` 의 재사용 대상은 전부 `create_blueprint()` 안의 클로저다(`deps`·`g` 의존).
그래서 이 모듈은 그것들을 **인자로 받는다** — import 하지 않는다.
"""

from __future__ import annotations

import logging
import secrets

from . import compose, ledger, llm, settings, store, video
from .errors import VpocError, now_iso

log = logging.getLogger("vpoc")

MAX_FEEDBACK_CHARS = 500

# 화면이 보여 주는 빠른 선택 칩. 서버는 검사하지 않는다(사용자가 자유롭게 고칠 수 있다).
FEEDBACK_CHIPS = (
    "표정 바꾸기",
    "움직임 바꾸기",
    "카메라 바꾸기",
    "다른 컷과 얼굴 맞추기",
    "배경 정리",
)


def enabled() -> bool:
    return settings.REGEN_ENABLED


def require_enabled(where: str) -> None:
    """G6: 스위치가 꺼져 있으면 없는 경로처럼 행동한다."""
    if not enabled():
        raise VpocError(where, "NotFound", "이 기능이 꺼져 있습니다.", 404)


# ---------------------------------------------------------------- 도구 스키마

EMIT_SHOT_REVISION = {
    "name": "emit_shot_revision",
    "description": "Return a revised version of exactly one shot.",
    "inputSchema": {
        "json": {
            "type": "object",
            "required": [
                "summaryKo",
                "cameraKo",
                "characters",
                "locationEn",
                "actionEn",
                "cameraEn",
                "notesKo",
            ],
            "properties": {
                "summaryKo": {"type": "string"},
                "cameraKo": {"type": "string"},
                "characters": {"type": "array", "items": {"type": "string"}},
                "locationEn": {"type": "string"},
                "actionEn": {"type": "string"},
                "cameraEn": {"type": "string"},
                "notesKo": {"type": "string"},
            },
        }
    },
}

REVISE_SYSTEM = """당신은 이미 만들어진 짧은 무음 영상의 **한 샷만** 사용자 의견대로 고치는 연출자다.

바꿔도 되는 것은 이 샷의 summaryKo · cameraKo · actionEn · cameraEn · locationEn 뿐이다.

반드시 지킬 것:
1. 캐릭터의 외형(appearanceEn)·화풍·배경 전체·다른 샷은 절대 바꾸지 않는다. 조립기가 캐릭터 외형
   문자열을 그대로 붙이기 때문에 여기서 외형을 다시 쓰면 다른 샷과 어긋난다.
2. locationEn 은 주어진 settingEn 안에서 고른다. 장소를 이야기 밖으로 옮기지 않는다.
3. 인물은 기본적으로 현재 샷의 characters 를 그대로 둔다. 의견이 인물을 빼거나 더하라고 명시하면
   0~2명 안에서 반영한다. characters 에는 주어진 캐릭터 id 만 쓴다.
4. actionEn · cameraEn · locationEn 은 영어로만 쓴다. 한글·캐릭터 이름·실존 상표·부정어
   (no/not/without/never/don't/doesn't)를 쓰지 않는다. 부정형 의견은 긍정형으로 바꿔 쓴다 —
   "울지 않게" 는 "clenched jaw, holding back tears" 처럼.
5. 인물은 이름이 아니라 주어진 handleEn 으로 지칭한다.
6. cameraEn 은 긍정형 영어 카메라 지시다(static camera, slow push-in, tracking shot 등).
   cameraKo 는 쉬운 한국말이고 괄호 안에 용어를 넣는다
   (얼굴을 크게=클로즈업 / 허리 위까지=미디엄 샷 / 온몸과 배경까지=롱 샷 / 두 사람을 한 화면에=투 샷 /
    한 사람 어깨 너머로 상대를=오버 더 숄더 / 천천히 다가가기=푸시 인 / 천천히 멀어지기=풀 아웃 /
    옆으로 훑기=팬 / 인물을 따라가기=트래킹 / 카메라를 고정=고정 샷).
7. 글자 예산: summaryKo 80 · cameraKo 40 · locationEn 120 · actionEn 260 · cameraEn 80.
8. 앞뒤 샷의 흐름을 깨지 않는다. 이 샷 하나만 고친다.
9. 의견이 **외형이나 화풍**을 바꾸라는 것이면(머리 길이·머리색·옷·그림체 등) 적용하지 말고
   notesKo 에 "외형·화풍은 ②단계에서 바꾸면 모든 샷에 함께 적용됩니다" 라고 적는다.
10. 의견이 **다른 컷과 얼굴을 맞추라는 것**이면 프롬프트로는 해결되지 않는다. notesKo 에
    "1인 샷은 '캐릭터 이미지로 시작하기'를 켜면 조금 나아질 수 있어요(보장은 아닙니다)" 라고 적고,
    동작과 카메라를 얼굴이 잘 보이는 안정적인 구도로 바꾼다.
11. 키스·노출·폭력은 사용자가 의견에서 명시하지 않으면 넣지 않는다.
12. notesKo 는 사용자에게 보일 한국어 메모다. 적을 것이 없으면 빈 문자열로 둔다.

emit_shot_revision 도구로만 답한다."""


# ---------------------------------------------------------------- 버전


def max_attempt(shot: dict) -> int:
    values = [int(shot.get("attempt") or 1)]
    for item in shot.get("versions") or []:
        values.append(int(item.get("attempt") or 1))
    regen = shot.get("regen")
    if isinstance(regen, dict):
        values.append(int(regen.get("attempt") or 1))
    return max(values)


def _version_from_shot(shot: dict, plan_shot: dict | None, gen_id: str = "") -> dict:
    """현재 샷 필드로 버전 항목 1개를 만든다(재생성 전 버전 또는 기존 데이터용 R9)."""
    plan_shot = plan_shot or {}
    return {
        "attempt": int(shot.get("attempt") or 1),
        "genId": gen_id,
        "origin": "plan",
        "feedbackKo": None,
        "summaryKo": plan_shot.get("summaryKo") or "",
        "cameraKo": plan_shot.get("cameraKo") or "",
        "promptEn": plan_shot.get("promptEn") or "",
        "mode": shot.get("mode") or "text",
        "status": shot.get("status") or "queued",
        "outputKey": shot.get("outputKey") or "",
        "outputBytes": shot.get("outputBytes"),
        "submittedAt": shot.get("submittedAt"),
        "completedAt": shot.get("completedAt"),
        "note": shot.get("note"),
        "error": shot.get("error"),
        "createdAt": shot.get("submittedAt") or now_iso(),
    }


def seed_versions(shot: dict, plan_shot: dict | None, gen_id: str = "") -> list[dict]:
    """첫 regen 전에 현재 버전을 versions 에 심는다.

    `generate`·`retry` 를 고치지 않기 위한 장치다(G1). 그 액션들은 versions 를 만들지 않고,
    여기서 처음 만들어진다.

    생성 회차가 바뀌면 이전 회차의 버전은 **버린다** — 다른 회차의 job 이 한 목록에 섞이면
    사용자가 옛 영상을 현재 버전으로 고를 수 있다.
    """
    versions = shot.get("versions")
    if versions and gen_id:
        versions = [
            item for item in versions if str(item.get("genId") or "") == str(gen_id)
        ]
        shot["versions"] = versions
    if not versions:
        versions = [_version_from_shot(shot, plan_shot, gen_id)]
        shot["versions"] = versions
    return versions


def sync_versions(shot: dict, gen_id: str = "") -> bool:
    """폴링 결과를 versions 항목에 옮긴다. versions 가 없으면 아무것도 하지 않는다."""
    versions = shot.get("versions")
    if not versions:
        return False
    changed = False
    targets: list[tuple[dict, str]] = [(shot, "plan")]
    regen = shot.get("regen")
    if isinstance(regen, dict):
        targets.append((regen, "regen"))

    for target, origin in targets:
        attempt = int(target.get("attempt") or 1)
        entry = next(
            (item for item in versions if int(item.get("attempt") or 0) == attempt), None
        )
        if entry is None:
            entry = {
                "attempt": attempt,
                "genId": gen_id,
                "origin": origin,
                "feedbackKo": target.get("feedbackKo"),
                "summaryKo": target.get("summaryKo") or "",
                "cameraKo": target.get("cameraKo") or "",
                "promptEn": target.get("promptEn") or "",
                "createdAt": now_iso(),
            }
            versions.append(entry)
            changed = True
        for field in (
            "status",
            "mode",
            "outputKey",
            "outputBytes",
            "submittedAt",
            "completedAt",
            "note",
            "error",
        ):
            if field in target and entry.get(field) != target.get(field):
                entry[field] = target.get(field)
                changed = True
    return changed


def poll_targets(shot: dict) -> list[dict]:
    """조회에서 상태를 물어야 하는 대상. 샷 본체와 진행 중 regen 이다."""
    targets = [shot]
    regen = shot.get("regen")
    if isinstance(regen, dict) and regen.get("status") in ("queued", "submitted"):
        targets.append(regen)
    return targets


def versions_view(shot: dict, plan_shot: dict | None, gen_id: str = "") -> list[dict]:
    """PlanView 용 버전 목록. URL 은 조회 때마다 새로 서명한다.

    현재 생성 회차의 버전만 보여 준다. 이전 회차의 job 이 섞이면 사용자가 옛 영상을
    고를 수 있다(2026-09-23, docs/vpoc1/fix-regen-reload.md).
    """
    versions = shot.get("versions") or []
    if versions and gen_id:
        versions = [
            item for item in versions if str(item.get("genId") or "") == str(gen_id)
        ]
    if not versions:
        versions = [_version_from_shot(shot, plan_shot, gen_id)]
    out: list[dict] = []
    for item in sorted(versions, key=lambda row: int(row.get("attempt") or 0)):
        key = item.get("outputKey") or ""
        url = None
        if key:
            try:
                url = video.video_url(key)
            except Exception as exc:  # noqa: BLE001
                log.warning("vpoc.regen.presign_failed key=%s err=%s", key, exc)
        out.append(
            {
                "attempt": int(item.get("attempt") or 1),
                "genId": item.get("genId") or "",
                "origin": item.get("origin") or "plan",
                "feedbackKo": item.get("feedbackKo"),
                "summaryKo": item.get("summaryKo") or "",
                "cameraKo": item.get("cameraKo") or "",
                "promptEn": item.get("promptEn") or "",
                "mode": item.get("mode") or "text",
                "status": item.get("status") or "queued",
                "videoUrl": url,
                "createdAt": item.get("createdAt"),
            }
        )
    return out


def regen_view(shot: dict) -> dict | None:
    regen = shot.get("regen")
    if not isinstance(regen, dict):
        return None
    key = regen.get("outputKey") or ""
    url = None
    if key:
        try:
            url = video.video_url(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("vpoc.regen.presign_failed key=%s err=%s", key, exc)
    return {
        "attempt": int(regen.get("attempt") or 1),
        "status": regen.get("status") or "queued",
        "mode": regen.get("mode") or "text",
        "feedbackKo": regen.get("feedbackKo"),
        "summaryKo": regen.get("summaryKo") or "",
        "cameraKo": regen.get("cameraKo") or "",
        "submittedAt": regen.get("submittedAt"),
        "completedAt": regen.get("completedAt"),
        "videoUrl": url,
        "note": regen.get("note"),
        "error": regen.get("error"),
    }


def revision_view(shot: dict) -> dict | None:
    revision = shot.get("revision")
    if not isinstance(revision, dict):
        return None
    return {
        "id": revision.get("id"),
        "feedbackKo": revision.get("feedbackKo") or "",
        "summaryKo": revision.get("summaryKo") or "",
        "cameraKo": revision.get("cameraKo") or "",
        "characters": revision.get("characters") or [],
        "promptEn": revision.get("promptEn") or "",
        "promptChars": len(revision.get("promptEn") or ""),
        "useKeyframe": bool(revision.get("useKeyframe")),
        "notesKo": revision.get("notesKo") or "",
        "reviewKo": revision.get("reviewKo"),
        "createdAt": revision.get("createdAt"),
    }


# ---------------------------------------------------------------- 키프레임


def keyframe_ready(state: dict, plan_shot: dict) -> bool:
    """키프레임을 쓸 수 있는 샷인가. 인물 1명 + 그 캐릭터 이미지가 AI 생성일 때만."""
    char_ids = plan_shot.get("characters") or []
    if len(char_ids) != 1:
        return False
    entry = next(
        (
            item
            for item in state["steps"]["3"].get("characters", [])
            if item.get("id") == char_ids[0]
        ),
        None,
    )
    image = (entry or {}).get("image") or {}
    return image.get("source") == "ai" and bool(image.get("key"))


# ---------------------------------------------------------------- revise


def _find_shot(state: dict, shot_id: str) -> dict:
    shot = next(
        (item for item in state["steps"]["4"].get("shots", []) if item.get("id") == shot_id),
        None,
    )
    if not shot:
        raise VpocError("regen", "NotFound", "샷을 찾을 수 없습니다.", 404)
    return shot


def _find_plan_shot(state: dict, shot_id: str) -> dict:
    plan_shot = next(
        (item for item in state["steps"]["3"].get("shots", []) if item.get("id") == shot_id),
        None,
    )
    if not plan_shot:
        raise VpocError("regen", "NotFound", "계획에서 이 샷을 찾을 수 없습니다.", 404)
    return plan_shot


def check_revise_preconditions(state: dict, shot_id: str, feedback: str) -> None:
    shot = _find_shot(state, shot_id)
    if shot.get("status") not in ("completed", "failed"):
        raise VpocError(
            "regen.revise",
            "PreconditionFailed",
            "완료되거나 실패한 샷만 다시 만들 수 있습니다.",
            409,
        )
    regen = shot.get("regen")
    if isinstance(regen, dict) and regen.get("status") in ("queued", "submitted"):
        raise VpocError(
            "regen.revise", "RegenRunning", "이 샷은 이미 새 버전을 만들고 있습니다.", 409
        )
    if not feedback.strip():
        raise VpocError("regen.revise", "FeedbackEmpty", "어떻게 바꾸고 싶은지 적어 주세요.", 400)
    if len(feedback) > MAX_FEEDBACK_CHARS:
        raise VpocError(
            "regen.revise",
            "FeedbackTooLong",
            f"의견은 {MAX_FEEDBACK_CHARS}자까지 쓸 수 있습니다 (지금 {len(feedback)}자).",
            400,
        )


def _revise_blocks(state: dict, shot_id: str, feedback: str) -> list[dict]:
    step2 = state["steps"]["2"]
    step3 = state["steps"]["3"]
    story = step2.get("story") or {}
    characters = step2.get("characters") or []
    shots = step3.get("shots") or []
    index = next((i for i, item in enumerate(shots) if item.get("id") == shot_id), 0)
    target = shots[index]

    lines = [
        "[이야기]",
        f"배경(한국어): {story.get('settingKo', '')}",
        f"배경(영어, locationEn 은 이 안에서 고른다): {story.get('settingEn', '')}",
        f"화풍(바꾸지 않는다): {story.get('style', 'live_action')}",
        "",
        "[캐릭터 — 외형은 바꾸지 않는다]",
    ]
    for item in characters:
        lines.append(
            f"- id={item.get('id')} / 이름={item.get('nameKo')}"
            f" / handleEn={item.get('handleEn')} / appearanceEn={item.get('appearanceEn')}"
        )
    lines += [
        "",
        "[고칠 샷 — 현재 내용]",
        f"id: {target.get('id')}",
        f"summaryKo: {target.get('summaryKo', '')}",
        f"cameraKo: {target.get('cameraKo', '')}",
        f"characters: {target.get('characters') or []}",
        f"locationEn: {target.get('locationEn', '')}",
        f"actionEn: {target.get('actionEn', '')}",
        f"cameraEn: {target.get('cameraEn', '')}",
        "",
        "[앞뒤 샷 — 흐름 유지용. 고치지 않는다]",
        f"앞 샷: {shots[index - 1].get('summaryKo') if index > 0 else '(없음)'}",
        f"뒤 샷: {shots[index + 1].get('summaryKo') if index + 1 < len(shots) else '(없음)'}",
        "",
        "[사용자 의견]",
        feedback.strip(),
        "",
        "위 의견을 반영해 이 샷 하나만 고친 결과를 emit_shot_revision 으로 돌려주세요.",
    ]
    return [{"text": "\n".join(lines)}]


def _candidate(payload: dict, shot_id: str, fallback: dict) -> dict:
    return {
        "id": shot_id,
        "summaryKo": payload.get("summaryKo") or "",
        "cameraKo": payload.get("cameraKo") or "",
        "characters": payload.get("characters") or fallback.get("characters") or [],
        "locationEn": payload.get("locationEn") or "",
        "actionEn": payload.get("actionEn") or "",
        "cameraEn": payload.get("cameraEn") or "",
    }


def do_revise(
    *,
    state: dict,
    shot_id: str,
    feedback: str,
    use_keyframe: bool,
    user: str,
    pid: str,
    plan_id: str,
) -> dict:
    """작업 스레드에서 돈다. Claude 1회(+위반 시 1회 재요청)로 수정안을 만든다.

    `llm._converse` 와 `llm._retry_block` 은 같은 패키지의 내부 헬퍼다. 공개 API 로 바꾸면
    기존 파일 변경이 늘어나므로 그대로 쓴다(G1).
    """
    step2 = state["steps"]["2"]
    characters = step2.get("characters") or []
    style = compose.style_of((step2.get("story") or {}).get("style") or "live_action")
    plan_shot = _find_plan_shot(state, shot_id)
    blocks = _revise_blocks(state, shot_id, feedback)

    payload, usage = llm._converse(
        system_text=REVISE_SYSTEM,
        blocks=blocks,
        tool=EMIT_SHOT_REVISION,
        max_tokens=2000,
        where="claude.revise",
    )
    candidate = _candidate(payload, shot_id, plan_shot)
    problems = compose.check_shot(candidate, characters, style)
    if problems:
        payload, usage2 = llm._converse(
            system_text=REVISE_SYSTEM,
            blocks=blocks + [llm._retry_block(problems)],
            tool=EMIT_SHOT_REVISION,
            max_tokens=2000,
            where="claude.revise.retry",
        )
        usage = {
            "inputTokens": (usage.get("inputTokens") or 0) + (usage2.get("inputTokens") or 0),
            "outputTokens": (usage.get("outputTokens") or 0) + (usage2.get("outputTokens") or 0),
        }
        candidate = _candidate(payload, shot_id, plan_shot)
        problems = compose.check_shot(candidate, characters, style)

    ledger.charge_text(
        f"revise-{plan_id}-{shot_id}-{secrets.token_hex(3)}",
        input_tokens=int(usage.get("inputTokens") or 0),
        output_tokens=int(usage.get("outputTokens") or 0),
        user=user,
        project_id=pid,
        plan_id=plan_id,
    )

    revision = {
        "id": f"rev_{secrets.token_hex(6)}",
        "shotId": shot_id,
        "feedbackKo": feedback.strip(),
        "summaryKo": candidate["summaryKo"],
        "cameraKo": candidate["cameraKo"],
        "characters": candidate["characters"],
        "locationEn": candidate["locationEn"],
        "actionEn": candidate["actionEn"],
        "cameraEn": candidate["cameraEn"],
        "notesKo": payload.get("notesKo") or "",
        "useKeyframe": bool(use_keyframe) and keyframe_ready(state, plan_shot),
        "createdAt": now_iso(),
        "promptEn": "",
        "reviewKo": None,
    }
    if problems:
        # R1: 두 번째 시도에도 위반이면 저장은 하되 '검토 필요'로 두고 승인을 잠근다.
        revision["reviewKo"] = "검토 필요: " + " / ".join(problems[:4])
    else:
        revision["promptEn"] = compose.build_prompt(candidate, characters, style)
    return {"shotId": shot_id, "revision": revision}


def apply_revision(state: dict, result: dict) -> None:
    shot = _find_shot(state, result["shotId"])
    shot["revision"] = result["revision"]


# ---------------------------------------------------------------- regenerate


def start_regenerate(
    state: dict,
    *,
    shot_id: str,
    revision_id: str,
    prompt_en: str | None,
    confirm_usd,
    owner: str,
    pid: str,
    plan_id: str,
    user: str,
    refresh_shots,
) -> dict:
    shot = _find_shot(state, shot_id)
    plan_shot = _find_plan_shot(state, shot_id)
    step2 = state["steps"]["2"]
    characters = step2.get("characters") or []
    style = compose.style_of((step2.get("story") or {}).get("style") or "live_action")

    regen = shot.get("regen")
    if isinstance(regen, dict) and regen.get("status") in ("queued", "submitted"):
        raise VpocError(
            "regen.regenerate", "RegenRunning", "이 샷은 이미 새 버전을 만들고 있습니다.", 409
        )

    revision = shot.get("revision")
    if not isinstance(revision, dict) or revision.get("id") != revision_id:
        raise VpocError(
            "regen.regenerate",
            "RevisionMismatch",
            "수정안이 바뀌었습니다. 새로 만든 뒤 다시 승인해 주세요.",
            409,
        )

    prompt = revision.get("promptEn") or ""
    if prompt_en is not None:
        # R3: 고급 편집으로 고친 프롬프트도 조립기 검사를 다시 통과해야 한다.
        prompt = str(prompt_en).strip()
        problems: list[str] = []
        if not prompt:
            problems.append("프롬프트가 비어 있습니다")
        if compose.HANGUL.search(prompt):
            problems.append("프롬프트에 한글이 있습니다")
        if compose.NEGATION.search(prompt):
            problems.append("프롬프트에 부정어가 있습니다")
        for name in compose.character_names(characters):
            pattern = compose._name_pattern(name)
            if pattern and pattern.search(prompt):
                problems.append(f"프롬프트에 캐릭터 이름 '{name}' 이 있습니다")
        if len(prompt) > compose.MAX_PROMPT_CHARS:
            problems.append(
                f"프롬프트가 {compose.MAX_PROMPT_CHARS}자를 넘습니다 ({len(prompt)}자)."
                " 자르지 않고 멈췄습니다"
            )
        if problems:
            raise VpocError(
                "regen.regenerate",
                "PromptRejected",
                "고친 프롬프트를 쓸 수 없습니다: " + " / ".join(problems),
                400,
            )
    elif revision.get("reviewKo"):
        raise VpocError(
            "regen.regenerate",
            "PreconditionFailed",
            revision["reviewKo"] + " — 고급 편집으로 고치거나 의견을 다시 써 주세요.",
            409,
        )

    # 재생성도 그 회차의 모델 · 화질 · 그 샷의 길이로 제출된다(routes.submit_shot). 금액도 같게 맞춘다.
    step4 = state["steps"]["4"]
    expected = settings.shot_usd(
        str(step4.get("videoModel") or ""), str(step4.get("resolution") or ""), shot.get("durationS")
    )
    if confirm_usd is None or abs(float(confirm_usd) - expected) > 0.005:
        raise VpocError(
            "regen.regenerate",
            "CostMismatch",
            f"화면 금액({confirm_usd})과 서버 계산({expected:.2f} USD)이 다릅니다. 새로고침해 주세요.",
            409,
        )

    gen_id = str(state["steps"]["4"].get("genId") or "")
    versions = seed_versions(shot, plan_shot, gen_id)
    attempt = max_attempt(shot) + 1
    use_keyframe = bool(revision.get("useKeyframe")) and keyframe_ready(state, plan_shot)

    target = {
        "attempt": attempt,
        "genId": gen_id,
        "origin": "regen",
        "status": "queued",
        "mode": "keyframe" if use_keyframe else "text",
        "useKeyframe": use_keyframe,
        "feedbackKo": revision.get("feedbackKo"),
        "summaryKo": revision.get("summaryKo") or "",
        "cameraKo": revision.get("cameraKo") or "",
        "promptEn": prompt,
        "submittedAt": None,
        "completedAt": None,
        "outputKey": "",
        "note": None,
        "error": None,
        "lastPolledAt": "",
        "createdAt": now_iso(),
    }
    versions.append(
        {
            "attempt": attempt,
            "genId": gen_id,
            "origin": "regen",
            "feedbackKo": target["feedbackKo"],
            "summaryKo": target["summaryKo"],
            "cameraKo": target["cameraKo"],
            "promptEn": prompt,
            "mode": target["mode"],
            "status": "queued",
            "outputKey": "",
            "createdAt": target["createdAt"],
        }
    )
    shot["regen"] = target
    shot["revision"] = None

    log.info(
        "vpoc.call where=regenerate plan=%s shot=%s attempt=%s mode=%s chars=%d",
        plan_id,
        shot_id,
        attempt,
        target["mode"],
        len(prompt),
    )
    # 기존 조회·제출 로직을 그대로 태운다 — 동시 한도(VPOC_MAX_INFLIGHT)와 대기(queued)가 같이 적용된다.
    refresh_shots(state, owner, pid, plan_id, user)
    return {"attempt": attempt, "status": target.get("status")}


# ---------------------------------------------------------------- select


def select_version(state: dict, *, shot_id: str) -> dict:
    """`shot.regen` 을 비우고 선택 버전을 샷 본체에 반영한다."""
    shot = _find_shot(state, shot_id)
    plan_shot = _find_plan_shot(state, shot_id)
    gen_id = str(state["steps"]["4"].get("genId") or "")
    versions = seed_versions(shot, plan_shot, gen_id)
    return {"shot": shot, "versions": versions}


def apply_select(state: dict, *, shot_id: str, attempt: int) -> dict:
    shot = _find_shot(state, shot_id)
    plan_shot = _find_plan_shot(state, shot_id)
    gen_id = str(state["steps"]["4"].get("genId") or "")
    versions = seed_versions(shot, plan_shot, gen_id)
    sync_versions(shot, gen_id)

    entry = next(
        (item for item in versions if int(item.get("attempt") or 0) == int(attempt)), None
    )
    if entry is None:
        raise VpocError("regen.select", "NotFound", "그 버전을 찾을 수 없습니다.", 404)
    if entry.get("status") != "completed" or not entry.get("outputKey"):
        raise VpocError(
            "regen.select", "PreconditionFailed", "완성된 버전만 고를 수 있습니다.", 409
        )

    previous = int(shot.get("selectedAttempt") or shot.get("attempt") or 1)
    shot.update(
        {
            "selectedAttempt": int(attempt),
            "attempt": int(attempt),
            "status": "completed",
            "mode": entry.get("mode") or "text",
            "outputKey": entry.get("outputKey"),
            "outputBytes": entry.get("outputBytes"),
            "submittedAt": entry.get("submittedAt"),
            "completedAt": entry.get("completedAt"),
            "note": entry.get("note"),
            "error": None,
            "invocationArn": "",
            "lastPolledAt": "",
        }
    )
    shot["regen"] = None

    # 합본은 shot.outputKey 로 만든다. 버전이 바뀌면 합본이 옛 영상이라 stale 로 표시한다.
    final = state["steps"]["4"].get("final") or {}
    stale = False
    if previous != int(attempt) and final.get("status") == "ready":
        final["status"] = "stale"
        state["steps"]["4"]["final"] = final
        stale = True

    log.info(
        "vpoc.select shot=%s %s -> %s stitchStale=%s", shot_id, previous, attempt, stale
    )
    return {"previousAttempt": previous, "attempt": int(attempt), "stitchStale": stale}


# ---------------------------------------------------------------- 완료 기록


def finals_record(state: dict, *, owner: str, pid: str, plan_id: str, base: dict) -> str | None:
    """버전 전환을 담은 완료 기록을 새로 쓴다. `final.json` 은 덮어쓰지 않는다(G4)."""
    step3 = state["steps"]["3"]
    step4 = state["steps"]["4"]
    record = dict(base)
    record["completedAt"] = now_iso()
    record["supersedes"] = base.get("completedAt")
    record["shots"] = [
        {
            "id": shot.get("id"),
            "status": shot.get("status"),
            "attempt": shot.get("attempt"),
            "selectedAttempt": int(shot.get("selectedAttempt") or shot.get("attempt") or 1),
            "mode": shot.get("mode"),
            "outputKey": shot.get("outputKey"),
            "promptEn": next(
                (
                    item.get("promptEn")
                    for item in step3.get("shots", [])
                    if item.get("id") == shot.get("id")
                ),
                "",
            ),
            "summaryKo": next(
                (
                    item.get("summaryKo")
                    for item in step3.get("shots", [])
                    if item.get("id") == shot.get("id")
                ),
                "",
            ),
            "submittedAt": shot.get("submittedAt"),
            "completedAt": shot.get("completedAt"),
            "error": shot.get("error"),
            "versions": [
                {
                    "attempt": item.get("attempt"),
                    "origin": item.get("origin"),
                    "feedbackKo": item.get("feedbackKo"),
                    "status": item.get("status"),
                    "outputKey": item.get("outputKey"),
                }
                for item in shot.get("versions") or []
            ],
        }
        for shot in step4.get("shots", [])
    ]
    key = store.finals_key(owner, pid, plan_id, record["completedAt"])
    try:
        store.STORE.write_json(key, record, create_only=True)
    except VpocError as exc:
        if exc.kind != "PreconditionFailed":
            raise
        log.info("vpoc.regen.finals_exists key=%s", key)
    return key


def latest_final(owner: str, pid: str, plan_id: str) -> dict | None:
    """가장 최근 완료 기록. `finals/` 최신 → 없으면 `final.json`.

    회차마다 finals 기록이 새로 쌓이므로 최신이 곧 현재 회차다. `final.json` 은 옛 계획
    호환용이다(2026-09-23, docs/vpoc1/fix-regen-reload.md).
    """
    keys = store.STORE.list_keys(store.finals_prefix(owner, pid, plan_id))
    newest = None
    for item in sorted(keys, key=lambda row: row["key"]):
        if item["key"].endswith(".json"):
            newest = item["key"]
    if newest:
        record, _ = store.STORE.read_json(newest)
        if record:
            return record
    record, _ = store.STORE.read_json(store.final_key(owner, pid, plan_id))
    return record
