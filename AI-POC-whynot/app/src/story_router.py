"""역할 분배자. **코드이고 LLM 이 아니다.**

## 왜 라우터가 LLM 이 아닌가 (지시서 1절)

    "분배자는 코드, 역할은 Bedrock 호출 1개 = system 프롬프트 1개"
    "사실만 보고 결정: 이미지 유무, 스토리 타입, 결과물 종류, 캐시 여부"

분배 판단에 필요한 것은 전부 사실이다. 이미지가 있는지, 해시가 바뀌었는지, 결과물이
무엇인지. 여기에 LLM 을 두면 판단이 흔들리고(같은 입력에 다른 순서), 호출이 한 번 더 늘고,
무엇보다 **왜 그 순서로 돌았는지 설명할 수 없게 된다.**

## 라우터가 정하는 순서 (지시서 5절)

    1. 첨부 이미지마다 비전 분석가 (병렬, 캐시 히트면 생략)
    2. source_hash 가 바뀌었을 때만 바이블 작성자
    3. 결과물 종류별 결과물 작성자
    4. 검수자 → pass 면 저장, fail 이면 notes 붙여 1회 재생성 → 그래도 fail 이면 needs_review
    5. trace 기록

## 이 모듈이 모르는 것

- 모델 ID / 리전  : story_models 표가 갖는다
- S3 / Neptune    : 호출부(server)가 주입하는 store 로 접근한다
- 프롬프트        : prompts_story 가 갖는다

그래서 이 모듈은 순서와 조건만 담는다. 테스트할 때 store 와 caller 를 가짜로 넣으면
Bedrock 없이 분배 로직을 확인할 수 있다.
"""

from __future__ import annotations

import logging

from . import story_context, story_models
from .roles import bible as bible_role
from .roles import validator as validator_role
from .roles import vision as vision_role
from .roles import writer as writer_role
from .roles._json import RoleError

logger = logging.getLogger("toon-change-ai-pj-app.story.router")


class StoryError(RuntimeError):
    """사용자에게 보여줄 수 있는 한국어 메시지를 담는다."""


# --------------------------------------------------------------------------- #
# 1단계: 이미지 분석 (캐시 포함)
# --------------------------------------------------------------------------- #
def analyze_images(pack: dict, images: list[dict], callers: dict, store) -> dict:
    """첨부 이미지를 분석해 팩의 image_analyses 를 채운다.

    Args:
        images: [{"key", "sha256", "name", "bytes", "format", "analysis"(선택)}]
                `analysis` 가 있으면 기획자가 화면에서 고친 것이다. 다시 분석하지 않는다.
        store:  캐시 저장소. get_image_analysis(cache_key) / put_image_analysis(cache_key, value)

    지시서 9절 수용 기준 두 개가 여기서 갈린다.
      - "이미지 없는 요청은 비전 분석가를 호출하지 않는다"
      - "같은 이미지 재첨부는 캐시 히트(trace로 확인)"
    """
    if not images:
        # 호출하지 않았다는 사실을 trace 에 남긴다. 남기지 않으면 "호출 안 함"과
        # "trace 기록 누락"을 구분할 수 없다.
        story_context.add_trace(pack, story_models.ROLE_VISION, note="이미지 없음 — 호출하지 않음")
        pack["image_analyses"] = []
        return pack

    model = story_models.spec(story_models.ROLE_VISION)["model"]
    results: list[dict | None] = [None] * len(images)
    pending: list[tuple[int, dict]] = []

    for index, image in enumerate(images):
        # (a) 기획자가 고친 분석이 있으면 그것이 최종이다.
        edited = image.get("analysis")
        if isinstance(edited, dict) and edited.get("edited_by_user"):
            results[index] = edited
            story_context.add_trace(
                pack, story_models.ROLE_VISION, model=model, cache_hit=True,
                note=f"사용자 편집본 사용: {image.get('name') or ''}"[:200],
            )
            continue

        # (b) 캐시. 키는 sha256 + 모델이다(story_context.image_cache_key 주석 참고).
        sha = image.get("sha256") or ""
        cached = None
        if sha and store is not None:
            cache_key = story_context.image_cache_key(sha, model)
            try:
                cached = store.get_image_analysis(cache_key)
            except Exception as error:  # noqa: BLE001 - 캐시 장애가 분석을 막아서는 안 된다
                logger.warning("이미지 분석 캐시 조회 실패: %s", error)
        if cached:
            results[index] = cached
            story_context.add_trace(
                pack, story_models.ROLE_VISION, model=model, cache_hit=True,
                note=f"캐시 히트: {image.get('name') or ''}"[:200],
            )
            continue

        pending.append((index, image))

    if pending:
        analyzed = vision_role.analyze_many(
            callers[story_models.ROLE_VISION],
            [image for _index, image in pending],
            pack.get("story_type") or story_context.TYPE_LOGLINE,
            pack.get("summary_story") or "",
        )
        for (index, image), analysis in zip(pending, analyzed):
            results[index] = analysis
            story_context.add_trace(
                pack, story_models.ROLE_VISION, model=model,
                ok=not analysis.get("degraded"),
                note=f"분석: {image.get('name') or ''}"[:200],
            )
            sha = image.get("sha256") or ""
            if sha and store is not None and not analysis.get("degraded"):
                try:
                    store.put_image_analysis(story_context.image_cache_key(sha, model), analysis)
                except Exception as error:  # noqa: BLE001
                    logger.warning("이미지 분석 캐시 저장 실패: %s", error)

    pack["image_analyses"] = [item for item in results if item]
    return pack


# --------------------------------------------------------------------------- #
# 2단계: 바이블 (해시가 바뀌었을 때만)
# --------------------------------------------------------------------------- #
def ensure_bible(pack: dict, story_text: str, callers: dict, existing: dict | None = None) -> dict:
    """바이블을 확보한다. 입력 해시가 같으면 재생성하지 않는다.

    지시서 9절 수용 기준: "원문이 안 바뀌면 바이블을 재생성하지 않는다."
    """
    story_type = pack.get("story_type") or story_context.TYPE_LOGLINE
    analyses = pack.get("image_analyses") or []
    wanted = bible_role.source_hash(story_text, story_type, analyses)

    if existing and existing.get("source_hash") == wanted:
        pack["bible"] = existing
        story_context.add_trace(
            pack, story_models.ROLE_BIBLE, cache_hit=True,
            note="source_hash 동일 — 재생성하지 않음",
        )
        logger.info("바이블 재사용 hash=%s", wanted[:12])
        return pack

    spec = story_models.spec(story_models.ROLE_BIBLE)
    try:
        built = bible_role.build(
            callers[story_models.ROLE_BIBLE], story_text, story_type, analyses
        )
    except RoleError as error:
        story_context.add_trace(
            pack, story_models.ROLE_BIBLE, model=spec["model"], ok=False, note=str(error)[:200]
        )
        raise StoryError(f"작품 바이블을 만들지 못했습니다. {error}") from error

    story_context.add_trace(pack, story_models.ROLE_BIBLE, model=spec["model"])
    pack["bible"] = built
    return pack


# --------------------------------------------------------------------------- #
# 3~4단계: 결과물 + 검수 (+1회 재생성)
# --------------------------------------------------------------------------- #
def _produce(kind: str, pack: dict, story_text: str, callers: dict, notes=None) -> dict:
    """결과물 작성자를 한 번 부른다."""
    bible = pack.get("bible") or {}
    options = pack.get("options") or {}
    quality = options.get("quality") or story_models.QUALITY_STANDARD

    if kind == story_context.OUT_LOGLINE:
        return writer_role.logline(
            callers[story_models.ROLE_LOGLINE], bible, story_text, notes
        )
    if kind == story_context.OUT_STORYBOARD:
        return writer_role.storyboard(
            callers[story_models.ROLE_STORYBOARD], bible,
            int(options.get("cut_count") or 6), story_text,
            pack.get("summary_story") or "", notes,
        )
    if kind == story_context.OUT_SCRIPT:
        # 대본은 아웃라인 → 승인 → 회차이므로 이 함수로 한 번에 만들지 않는다.
        raise StoryError("대본은 아웃라인 승인 뒤에 회차별로 생성합니다.")
    raise StoryError(f"'{kind}' 은 만들 수 없는 결과물 종류입니다.")


def _role_for(kind: str) -> str:
    return {
        story_context.OUT_LOGLINE: story_models.ROLE_LOGLINE,
        story_context.OUT_STORYBOARD: story_models.ROLE_STORYBOARD,
        story_context.OUT_SCRIPT: story_models.ROLE_SCRIPT_EPISODE,
    }[kind]


def produce_with_review(kind: str, pack: dict, story_text: str, callers: dict) -> dict:
    """결과물 생성 + 검수 + (실패 시) 1회 재생성.

    돌려주는 것: {"output", "status", "notes", "regenerated"}
    status 는 validator.STATUS_OK 또는 STATUS_NEEDS_REVIEW.

    needs_review 도 **저장한다.** 버리지 않는 이유: 검수자가 잘못 판정하는 경우가 있고,
    사람이 보면 쓸 수 있는 결과물인 경우가 많다. 지시서 3절도 "needs_review인 결과물은
    카드에 검수 메모 표시 + 재생성 버튼"이라고 정했다.
    """
    role = _role_for(kind)
    spec = story_models.spec(role, quality=(pack.get("options") or {}).get("quality", ""))
    bible = pack.get("bible") or {}
    options = pack.get("options") or {}

    try:
        output = _produce(kind, pack, story_text, callers)
    except RoleError as error:
        story_context.add_trace(pack, role, model=spec["model"], ok=False, note=str(error)[:200])
        raise StoryError(str(error)) from error
    story_context.add_trace(pack, role, model=spec["model"])

    verdict = validator_role.validate(
        callers[story_models.ROLE_VALIDATOR], output, bible, kind, options
    )
    story_context.add_trace(
        pack, story_models.ROLE_VALIDATOR,
        model=story_models.spec(story_models.ROLE_VALIDATOR)["model"],
        # 1차 코드 검사는 모델을 부르지 않았다. 그것을 호출로 세면 통계가 틀어진다.
        cache_hit=(verdict["stage"] == "code"),
        ok=verdict["pass"],
        note=f"stage={verdict['stage']} notes={len(verdict['notes'])}",
    )

    if verdict["pass"]:
        return {"output": output, "status": validator_role.STATUS_OK, "notes": [], "regenerated": False}

    # 1회 재생성. notes 를 붙여 무엇을 고쳐야 하는지 알려 준다.
    logger.info("검수 실패로 1회 재생성합니다 kind=%s stage=%s", kind, verdict["stage"])
    try:
        retried = _produce(kind, pack, story_text, callers, notes=verdict["notes"])
    except RoleError as error:
        # 재생성이 실패하면 **첫 결과를 needs_review 로 남긴다.** 버리면 사용자는
        # 아무것도 받지 못하고, 첫 결과는 검수만 통과하지 못한 것이지 쓸 수 없는 것이 아니다.
        story_context.add_trace(pack, role, model=spec["model"], ok=False, note=f"재생성 실패: {error}"[:200])
        return {
            "output": output,
            "status": validator_role.STATUS_NEEDS_REVIEW,
            "notes": verdict["notes"],
            "regenerated": False,
        }
    story_context.add_trace(pack, role, model=spec["model"], note="검수 실패 후 재생성")

    second = validator_role.validate(
        callers[story_models.ROLE_VALIDATOR], retried, bible, kind, options
    )
    story_context.add_trace(
        pack, story_models.ROLE_VALIDATOR,
        model=story_models.spec(story_models.ROLE_VALIDATOR)["model"],
        cache_hit=(second["stage"] == "code"), ok=second["pass"],
        note=f"재검수 stage={second['stage']} notes={len(second['notes'])}",
    )

    if second["pass"]:
        return {"output": retried, "status": validator_role.STATUS_OK, "notes": [], "regenerated": True}

    return {
        "output": retried,
        "status": validator_role.STATUS_NEEDS_REVIEW,
        "notes": second["notes"],
        "regenerated": True,
    }


# --------------------------------------------------------------------------- #
# 대본: 아웃라인
# --------------------------------------------------------------------------- #
def plan_script(pack: dict, story_text: str, callers: dict) -> dict:
    """대본 아웃라인. 회차 생성은 승인 뒤에 시작한다(지시서 3절).

    이어쓰기 판단이 여기 있다 (지시서 5절):
        "입력이 대본이고 결과물도 대본이면: 범위가 원문 안이면 재작성, 이후면 이어쓰기"

    이 판단을 작성자에 두지 않는 이유는 roles/writer.py 의 주석에 있다 —
    작성자가 story_type 을 보면 바이블을 중간 표현으로 둔 의미가 없어진다.
    """
    options = pack.get("options") or {}
    episode_range = options.get("episode_range") or [1, 1]
    try:
        episode_from, episode_to = int(episode_range[0]), int(episode_range[1])
    except (TypeError, ValueError, IndexError):
        raise StoryError("회차 범위가 올바르지 않습니다.")

    if episode_from < 1 or episode_to < episode_from:
        raise StoryError("회차 범위가 올바르지 않습니다. 시작화가 끝화보다 작아야 합니다.")
    count = episode_to - episode_from + 1
    if count > story_context.MAX_EPISODES_PER_JOB:
        raise StoryError(
            f"한 번에 최대 {story_context.MAX_EPISODES_PER_JOB}화까지 만들 수 있습니다. "
            f"지금 {count}화를 요청했습니다."
        )

    continuation = _is_continuation(pack, episode_from)
    quality = options.get("quality") or story_models.QUALITY_STANDARD
    role = story_models.ROLE_SCRIPT_OUTLINE
    spec = story_models.spec(role, quality=quality)

    try:
        outline = writer_role.script_outline(
            callers[role],
            pack.get("bible") or {},
            episode_from,
            episode_to,
            length=options.get("length") or story_context.LENGTH_NORMAL,
            story_text=story_text,
            continuation=continuation,
            quality=quality,
        )
    except RoleError as error:
        story_context.add_trace(pack, role, model=spec["model"], ok=False, note=str(error)[:200])
        raise StoryError(f"아웃라인을 만들지 못했습니다. {error}") from error

    story_context.add_trace(pack, role, model=spec["model"],
                            note=f"{episode_from}~{episode_to}화 continuation={continuation}")
    return outline


def _is_continuation(pack: dict, episode_from: int) -> bool:
    """이어쓰기인가.

    원문이 대본이 아니면 이어쓸 대상이 없다. 원문이 대본이면 요청 범위가 원문이 덮는
    회차 뒤인지 본다. 원문이 몇 화까지인지는 바이블의 plot_beats 로는 알 수 없으므로,
    화면이 알려 준 값(options.source_episode_to)을 쓴다. 없으면 이어쓰기가 아니다 —
    모르는 상태에서 이어쓰기로 판단하면 원문에 이미 있는 사건을 빼먹는다.
    """
    if (pack.get("story_type") or "") != story_context.TYPE_SCRIPT:
        return False
    options = pack.get("options") or {}
    try:
        source_to = int(options.get("source_episode_to") or 0)
    except (TypeError, ValueError):
        return False
    return source_to > 0 and episode_from > source_to


# --------------------------------------------------------------------------- #
# 대본: 회차 하나
# --------------------------------------------------------------------------- #
def write_episode(
    pack: dict,
    story_text: str,
    callers: dict,
    outline_episode: dict,
    arc: str = "",
    previous_summary: str = "",
) -> dict:
    """회차 하나 + 검수 + 1회 재생성. 잡 러너가 회차마다 부른다."""
    options = pack.get("options") or {}
    quality = options.get("quality") or story_models.QUALITY_STANDARD
    role = story_models.ROLE_SCRIPT_EPISODE
    spec = story_models.spec(role, quality=quality)
    bible = pack.get("bible") or {}
    length = options.get("length") or story_context.LENGTH_NORMAL
    no = int(outline_episode.get("no") or 1)

    def build(notes=None):
        return writer_role.script_episode(
            callers[role], bible, outline_episode, arc=arc,
            previous_summary=previous_summary, length=length,
            story_text=story_text, notes=notes, quality=quality,
        )

    try:
        episode = build()
    except RoleError as error:
        story_context.add_trace(pack, role, model=spec["model"], ok=False, note=f"{no}화: {error}"[:200])
        raise StoryError(f"{no}화를 만들지 못했습니다. {error}") from error
    story_context.add_trace(pack, role, model=spec["model"], note=f"{no}화")

    check_options = {"episode_no": no, "length": length}
    verdict = validator_role.validate(
        callers[story_models.ROLE_VALIDATOR], episode, bible,
        story_context.OUT_SCRIPT, check_options,
    )
    story_context.add_trace(
        pack, story_models.ROLE_VALIDATOR,
        model=story_models.spec(story_models.ROLE_VALIDATOR)["model"],
        cache_hit=(verdict["stage"] == "code"), ok=verdict["pass"],
        note=f"{no}화 stage={verdict['stage']}",
    )

    if verdict["pass"]:
        return {"episode": episode, "status": validator_role.STATUS_OK, "notes": [], "regenerated": False}

    logger.info("%s화 검수 실패로 1회 재생성합니다 stage=%s", no, verdict["stage"])
    try:
        retried = build(notes=verdict["notes"])
    except RoleError as error:
        story_context.add_trace(pack, role, model=spec["model"], ok=False,
                                note=f"{no}화 재생성 실패: {error}"[:200])
        return {"episode": episode, "status": validator_role.STATUS_NEEDS_REVIEW,
                "notes": verdict["notes"], "regenerated": False}
    story_context.add_trace(pack, role, model=spec["model"], note=f"{no}화 재생성")

    second = validator_role.validate(
        callers[story_models.ROLE_VALIDATOR], retried, bible,
        story_context.OUT_SCRIPT, check_options,
    )
    story_context.add_trace(
        pack, story_models.ROLE_VALIDATOR,
        model=story_models.spec(story_models.ROLE_VALIDATOR)["model"],
        cache_hit=(second["stage"] == "code"), ok=second["pass"],
        note=f"{no}화 재검수 stage={second['stage']}",
    )
    return {
        "episode": retried,
        "status": validator_role.STATUS_OK if second["pass"] else validator_role.STATUS_NEEDS_REVIEW,
        "notes": [] if second["pass"] else second["notes"],
        "regenerated": True,
    }


# --------------------------------------------------------------------------- #
# 비용 예상 (지시서 3절: 대본 모달에 예상 토큰·비용 표시)
# --------------------------------------------------------------------------- #
# 단가를 코드에 박지 않는다. AWS 가격이 바뀌면 조용히 틀린 값이 쌓인다.
# 화면에는 **토큰 추정치**를 보여 주고, 비용은 환경변수로 단가를 준 경우에만 계산한다.
_LENGTH_TOKENS = {
    story_context.LENGTH_SHORT: 4000,
    story_context.LENGTH_NORMAL: 8000,
    story_context.LENGTH_LONG: 14000,
}


def estimate_script(episode_count: int, length: str, quality: str) -> dict:
    """대본 잡의 예상 호출 수와 토큰. 승인 전에 화면에 보여준다."""
    per_episode_out = _LENGTH_TOKENS.get(length, _LENGTH_TOKENS[story_context.LENGTH_NORMAL])
    # 회차마다 바이블 요약 + 아웃라인 + 직전 요약이 들어간다. 대략 고정값이다.
    per_episode_in = 4000
    outline_out = 800 + 400 * episode_count
    # 검수는 회차마다 1~2회(재생성 시 2회). 평균 1.3 으로 잡는다.
    validator_calls = round(episode_count * 1.3)

    return {
        "episodes": episode_count,
        "length": length,
        "quality": quality,
        "model": story_models.spec(story_models.ROLE_SCRIPT_EPISODE, quality=quality)["model"],
        "calls": {
            "outline": 1,
            "episodes": episode_count,
            "validator": validator_calls,
            "total": 1 + episode_count + validator_calls,
        },
        "tokens": {
            "inputEstimate": 3000 + per_episode_in * episode_count + 1500 * validator_calls,
            "outputEstimate": outline_out + per_episode_out * episode_count + 300 * validator_calls,
        },
        # 순차로 돌므로 회차 수에 비례한다. 범위로 준다.
        #
        # 예전에는 회차당 1.2분 하나만 돌려줬다(12화 = 14분). 실제로는 재생성이 붙거나
        # 분량 상한에 가까운 회차가 나오면 그 3배가 걸린다. 화면이 14분을 약속하고
        # 40분이 걸리면 사용자는 정상 동작을 고장으로 판단한다. 실제로 그렇게 보고가 왔다.
        #
        # 낙관값은 재생성 없이 짧게 끝나는 경우, 비관값은 회차마다 검수 재생성이 붙고
        # 분량을 다 쓰는 경우다. 고품질(Opus)은 더 느리다.
        "estimateMinutes": max(1, round(episode_count * 1.2)),
        "estimateMinutesRange": [
            max(1, round(episode_count * 1.0)),
            max(2, round(episode_count * (5.0 if quality == story_models.QUALITY_HIGH else 3.5))),
        ],
        "note": (
            "순차로 만들기 때문에 회차 수에 비례합니다. 검수에서 재생성이 걸리거나 "
            "분량이 길면 예상보다 오래 걸립니다. 창을 닫아도 생성은 계속됩니다."
        ),
    }
