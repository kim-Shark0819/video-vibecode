"""vpoc1 비용 원장 — PRD 6.6.

규칙 R5: **비용은 서버가 센다.** 화면 금액은 서버 계산값이고 화면에서 계산하지 않는다.
틀린 화면 추정치가 작업을 멈춰 세운 적이 있다.

항목 키는 Luma 의 clientRequestToken(또는 요청 id)이다. 같은 키는 한 번만 계상한다.

**상한 없음 — 누적 표시만 (2026-09-23 사용자 결정).** 지원 금액이 8,000 USD 라서 60 USD 상한을
없앴다. 기록은 그대로 하고 제출을 막지 않는다. `ensure_room` 과 `BudgetExceeded` 는 제거했다.
금액 불일치 검사(`CostMismatch`)는 유지한다 — 그것은 화면과 서버 계산이 어긋난 것을 잡는
다른 문제다.
"""

from __future__ import annotations

import logging
import threading

from . import settings
from .errors import VpocError, now_iso
from .store import LEDGER_KEY, STORE

log = logging.getLogger("vpoc")

_LOCK = threading.Lock()


def _empty() -> dict:
    return {"capUsd": None, "spentUsd": 0.0, "entries": []}


def read() -> tuple[dict, str]:
    data, etag = STORE.read_json(LEDGER_KEY)
    if data is None:
        return _empty(), ""
    data.setdefault("spentUsd", 0.0)
    data.setdefault("entries", [])
    # 상한 없음(2026-09-23 사용자 결정). 파일에 옛 값이 남아 있어도 null 로 둔다.
    data["capUsd"] = None
    return data, etag


def summary() -> dict:
    """화면이 보여 줄 값. 누적 금액만 있고 상한은 없다.

    `capUsd` 키는 호환을 위해 남기고 항상 null 이다 — 화면은 null 이면 상한을 그리지 않는다.
    """
    data, _ = read()
    return {"spentUsd": round(float(data["spentUsd"]), 2), "capUsd": None}


def charge(
    key: str,
    *,
    kind: str,
    model: str,
    units: float,
    usd: float,
    user: str = "",
    project_id: str = "",
    plan_id: str = "",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict:
    """같은 key 는 한 번만 계상한다. 실패한 Luma 작업도 계상한다(보수적).

    토큰은 admin 모델 관리의 "영상 비용·토큰"이 읽는다(2026-10-07). 모르면 넣지 않는다.
    """
    entry = {
        "key": key,
        "at": now_iso(),
        "user": user,
        "projectId": project_id,
        "planId": plan_id,
        "kind": kind,
        "model": model,
        "units": round(float(units), 4),
        "usd": round(float(usd), 6),
    }
    if input_tokens is not None:
        entry["inputTokens"] = int(input_tokens)
    if output_tokens is not None:
        entry["outputTokens"] = int(output_tokens)
    with _LOCK:
        for _ in range(4):
            data, etag = read()
            if any(item.get("key") == key for item in data["entries"]):
                return summary()
            data["entries"].append(entry)
            data["spentUsd"] = round(
                sum(float(item.get("usd") or 0) for item in data["entries"]), 6
            )
            try:
                STORE.write_json(LEDGER_KEY, data, etag=etag)
                log.info(
                    "vpoc.ledger key=%s kind=%s usd=%.4f spent=%.2f",
                    key,
                    kind,
                    entry["usd"],
                    data["spentUsd"],
                )
                return {
                    "spentUsd": round(float(data["spentUsd"]), 2),
                    "capUsd": None,
                }
            except VpocError as exc:
                if exc.kind != "PreconditionFailed":
                    raise
    log.error("vpoc.ledger.conflict key=%s — 3회 재시도 실패", key)
    return summary()


def charge_video(
    token: str,
    *,
    user: str,
    project_id: str,
    plan_id: str,
    model: str = "",
    resolution: str = "",
    seconds: float | None = None,
) -> dict:
    # model 이 Seedance 면 그 모델 ID 와 단가로 남긴다. 비면 Luma. 화질·길이는 ③에서 고른 값.
    ark_id = settings.ARK_MODEL_IDS.get(model) if model else None
    return charge(
        token,
        kind="video",
        model=ark_id or settings.VIDEO_MODEL,
        units=settings.DURATION_S if seconds is None else seconds,
        usd=settings.shot_usd(model, resolution, seconds),
        user=user,
        project_id=project_id,
        plan_id=plan_id,
    )


def charge_image(
    key: str, *, user: str, project_id: str, plan_id: str, count: int = 1
) -> dict:
    return charge(
        key,
        kind="image",
        model=settings.IMAGE_MODEL,
        units=count,
        usd=settings.IMAGE_USD * count,
        user=user,
        project_id=project_id,
        plan_id=plan_id,
    )


def charge_seedream(key: str, *, model: str, user: str, project_id: str, plan_id: str) -> dict:
    return charge(
        key,
        kind="image",
        model=model,
        units=1,
        usd=settings.SEEDREAM_USD,
        user=user,
        project_id=project_id,
        plan_id=plan_id,
    )


def charge_text(
    key: str,
    *,
    input_tokens: int,
    output_tokens: int,
    user: str,
    project_id: str,
    plan_id: str,
    model: str = "",
) -> dict:
    model = model or settings.TEXT_MODEL
    opus = "opus" in model
    usd = input_tokens / 1_000_000 * (
        settings.OPUS_USD_PER_MTOK_IN if opus else settings.TEXT_USD_PER_MTOK_IN
    ) + output_tokens / 1_000_000 * (
        settings.OPUS_USD_PER_MTOK_OUT if opus else settings.TEXT_USD_PER_MTOK_OUT
    )
    return charge(
        key,
        kind="text",
        model=model,
        units=input_tokens + output_tokens,
        usd=usd,
        user=user,
        project_id=project_id,
        plan_id=plan_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def record_video_tokens(
    key: str, *, model: str, usage: dict | None, user: str, project_id: str, plan_id: str
) -> dict:
    """Seedance 작업이 끝났을 때 BytePlus 가 알려 준 토큰을 남긴다. 금액은 제출 때 이미 계상했다(0)."""
    usage = usage or {}
    total = int(usage.get("total_tokens") or 0)
    output = int(usage.get("completion_tokens") or total)
    return charge(
        key,
        kind="video-tokens",
        model=model,
        units=0,
        usd=0,
        user=user,
        project_id=project_id,
        plan_id=plan_id,
        input_tokens=max(total - output, 0),
        output_tokens=output,
    )


# 앱 밖(워크스테이션)에서 이미 쓴 금액. 원장이 상한을 제대로 세려면 이것도 들어가야 한다.
# 키가 같으면 한 번만 계상되므로 파드가 여러 개여도, 재기동해도 중복되지 않는다.
SEED_ENTRIES: tuple[tuple[str, float, str, str], ...] = (
    (
        "gate0-preflight-20260923",
        3.83,
        "luma.ray-v2:0+stability.sd3-5-large-v1:0",
        "Gate 0 사전 점검 — SD3.5 1장 0.08 + Luma 1건 3.75 (완료, 출력 1.16MB)",
    ),
    (
        "tokencheck-20260923",
        3.75,
        "luma.ray-v2:0",
        "clientRequestToken 형식 실검증 — 작업이 1초 뒤 'prompt is required' 로 Failed 되어"
        " 출력이 없다(arn .../mm0oexhd8y31). Luma 단가는 출력 초당이라 실제 청구는 0일 가능성이"
        " 높지만 원장은 실패 작업도 보수적으로 계상한다",
    ),
)


def seed() -> dict:
    """앱 밖에서 쓴 금액을 원장에 넣는다(PRD 6.6). 멱등하다."""
    result = summary()
    for key, usd_amount, model, note in SEED_ENTRIES:
        result = charge(key, kind="preflight", model=model, units=1, usd=usd_amount)
        log.debug("vpoc.ledger.seed key=%s note=%s", key, note)
    return result
