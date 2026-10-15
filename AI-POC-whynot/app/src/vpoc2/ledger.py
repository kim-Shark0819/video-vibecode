"""비용 원장. 기록만 하고 막지 않는다 (상한 없음 — D-L01).
예외는 720p 월 1편 제한 하나다 (설계자 결정 2026-09-30).

항목 키가 같으면 한 번만 기록한다 (Luma 는 clientRequestToken).
"""
from __future__ import annotations

from typing import Any

from . import settings, store
from .errors import Vpoc2Error, now_iso

_DEFAULT = {"spentUsd": 0.0, "entries": []}


def charge(key: str, *, user: str, run_id: str, kind: str, model: str, units: float,
           usd: float, resolution: str | None = None,
           tokens: tuple[int, int] | None = None) -> None:
    """tokens = (입력, 출력). 텍스트 호출에서만 넘긴다(admin 모델 관리가 읽는다)."""
    def mutate(ledger: dict[str, Any]) -> None:
        if any(e.get("key") == key for e in ledger["entries"]):
            return
        entry = {
            "key": key, "at": now_iso(), "user": user, "runId": run_id, "kind": kind,
            "model": model, "units": units, "usd": round(usd, 4), "resolution": resolution,
        }
        if tokens is not None:
            entry["inputTokens"], entry["outputTokens"] = int(tokens[0]), int(tokens[1])
        ledger["entries"].append(entry)
        ledger["spentUsd"] = round(sum(e["usd"] for e in ledger["entries"]), 4)

    store.update_json(store.get_store(), store.LEDGER_KEY, _DEFAULT, mutate)


def read() -> dict[str, Any]:
    return store.get_or_create_json(store.get_store(), store.LEDGER_KEY, _DEFAULT)[0]


def month_of(iso: str) -> str:
    return iso[:7]


def hq_used(month: str) -> int:
    """이번 달(UTC) 720p 로 시작한 영상 편수. 편 = 작업(run) 하나."""
    runs = {
        e.get("runId") for e in read()["entries"]
        if e.get("kind") == "hq-slot" and month_of(e.get("at", "")) == month
    }
    return len(runs)


def reserve_hq(user: str, run_id: str) -> None:
    """720p 1편 자리를 잡는다. 이미 이번 달 한도를 썼으면 409. 같은 작업은 다시 잡아도 한 번으로 센다."""
    month = month_of(now_iso())

    def mutate(ledger: dict[str, Any]) -> None:
        used = {
            e.get("runId") for e in ledger["entries"]
            if e.get("kind") == "hq-slot" and month_of(e.get("at", "")) == month
        }
        if run_id in used:
            return
        if len(used) >= settings.HQ_MONTHLY_LIMIT:
            raise Vpoc2Error(
                "HqMonthlyLimit",
                f"이번 달({month}) 720p 최종 확인 {settings.HQ_MONTHLY_LIMIT}편을 이미 사용했다. 540p 로 만들거나 다음 달에 다시 시도한다.",
                http=409,
            )
        ledger["entries"].append({
            "key": f"hq-{month}-{run_id}", "at": now_iso(), "user": user, "runId": run_id,
            "kind": "hq-slot", "model": settings.VIDEO_MODEL, "units": 0, "usd": 0.0,
            "resolution": settings.HQ_RESOLUTION,
        })

    store.update_json(store.get_store(), store.LEDGER_KEY, _DEFAULT, mutate)
