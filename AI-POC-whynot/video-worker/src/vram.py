"""VRAM 계측. 단계별 사용량을 manifest 로 남긴다.

## 왜 manifest 인가 (계획서 G-12)

계측값을 추론 서버 stdout 수집기로 보내지 않는다. 로그는 회전되고 잘리고, 무엇보다
**클립당 시간·VRAM·비용의 기준선**은 P2 게이트의 산출물이라 구조화된 채로 남아야 한다.
숫자를 로그에서 긁어내야 하면 그 기준선은 만들어지지 않는다.

## 왜 단계별인가

"peak 38GB" 하나만 알면 44GB 한 장에 들어가는지는 알지만 **무엇을 줄여야 하는지**
모른다. T5 인코딩이 8GB 인지 확산이 30GB 인지에 따라 대응이 다르다.
"""

from __future__ import annotations

import contextlib
import logging
import time

logger = logging.getLogger("video-worker.vram")


def _torch():
    try:
        import torch  # noqa: PLC0415

        return torch
    except ImportError:
        return None


def snapshot_mb() -> dict:
    """지금 VRAM 사용량(MB). CUDA 가 없으면 0."""
    torch = _torch()
    if torch is None or not torch.cuda.is_available():
        return {"allocated": 0, "reserved": 0, "peak": 0}
    return {
        "allocated": int(torch.cuda.memory_allocated() / 1024 / 1024),
        "reserved": int(torch.cuda.memory_reserved() / 1024 / 1024),
        "peak": int(torch.cuda.max_memory_allocated() / 1024 / 1024),
    }


def reset_peak() -> None:
    torch = _torch()
    if torch is not None and torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()


def release() -> None:
    """단계가 끝난 뒤 명시적으로 놓아 준다.

    이것이 없으면 연속 처리에서 VRAM 이 계단식으로 늘어난다. 20잡째에 OOM 이 나고
    원인은 첫 잡이 아니라 누적이다. 계획서 G-12 의 수용 기준이 이것이다 —
    "연속 20잡 처리 후 VRAM 사용량이 첫 잡 대비 증가하지 않는다".
    """
    import gc  # noqa: PLC0415

    gc.collect()
    torch = _torch()
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()


class Manifest:
    """한 잡의 단계별 계측. `stage()` 로 감싸면 시간과 VRAM 이 자동으로 쌓인다."""

    def __init__(self, job_id: str, shot_id: str, instance_type: str, hourly_usd: float):
        self.job_id = job_id
        self.shot_id = shot_id
        self.instance_type = instance_type
        self.hourly_usd = hourly_usd
        self.stages: list[dict] = []
        self._started = time.monotonic()

    @contextlib.contextmanager
    def stage(self, name: str):
        reset_peak()
        began = time.monotonic()
        before = snapshot_mb()
        error = ""
        try:
            yield
        except Exception as exc:  # noqa: BLE001 - 실패한 단계도 계측에 남긴다
            error = f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            after = snapshot_mb()
            elapsed = round(time.monotonic() - began, 2)
            self.stages.append(
                {
                    "name": name,
                    "sec": elapsed,
                    "vram_before_mb": before["allocated"],
                    "vram_after_mb": after["allocated"],
                    "vram_peak_mb": after["peak"],
                    "reserved_mb": after["reserved"],
                    "error": error,
                }
            )
            logger.info(
                "단계 %s %.2fs peak=%sMB after=%sMB%s",
                name,
                elapsed,
                after["peak"],
                after["allocated"],
                f" 오류={error}" if error else "",
            )

    def to_dict(self) -> dict:
        total = round(time.monotonic() - self._started, 2)
        peak = max((s["vram_peak_mb"] for s in self.stages), default=0)
        return {
            "jobId": self.job_id,
            "shotId": self.shot_id,
            "instanceType": self.instance_type,
            "stages": self.stages,
            "peakVramMb": peak,
            "totalSec": total,
            # 클립당 비용. P2 게이트가 요구하는 기준선의 핵심 숫자다.
            # Luma Ray2 비교값은 720p 5초 $7.50 이다.
            "costUsd": round(self.hourly_usd * total / 3600.0, 4),
            "failed": any(s["error"] for s in self.stages),
        }
