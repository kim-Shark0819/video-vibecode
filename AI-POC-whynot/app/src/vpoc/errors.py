"""vpoc1 오류 봉투 — PRD 6.7.

AWS 오류는 Error.Code · Error.Message · RequestId · HTTP 상태를 **원문 그대로** 싣는다.
운영(VPOC_RAW_ERRORS=false)에서는 message 만 갈아치우고 원문은 로그에 남긴다.
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from . import settings

log = logging.getLogger("vpoc")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VpocError(Exception):
    """화면에 그대로 보여 줄 오류. status 와 kind 를 직접 정한다."""

    def __init__(self, where: str, kind: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.where = where
        self.kind = kind
        self.message = message
        self.status = status


# 우리가 직접 만든 안내 문구라 운영에서도 그대로 보여 준다(원문 감추기 대상이 아니다).
# 감추면 사용자가 "처리 중 오류(코드: BadDuration)"만 보고 무엇을 고칠지 모른다(2026-10-13).
# fal.py 가 직접 쓴 한국어 안내 kind 도 같은 취지로 더한다. 원문이 외부 응답 그대로인
# HTTP<code> · NoRequestId 는 제외한다(감출 이유가 있다)(2026-10-19).
USER_FACING_KINDS = {
    "BadDuration",
    "FalBalanceExhausted",
    "FalKeyMissing",
    "FalUnreachable",
    "AlreadyRegistered",
    "SchemaUnreadable",
    "ModelUnavailable",
}


def envelope(where: str, exc: BaseException) -> dict:
    """예외를 오류 봉투로 바꾼다."""
    if isinstance(exc, VpocError):
        body = {
            "where": exc.where or where,
            "type": exc.kind,
            "message": exc.message,
            "requestId": "",
            "httpStatus": exc.status,
            "at": now_iso(),
        }
    elif isinstance(exc, ClientError):
        err = exc.response.get("Error", {}) or {}
        meta = exc.response.get("ResponseMetadata", {}) or {}
        body = {
            "where": where,
            "type": str(err.get("Code") or type(exc).__name__),
            "message": str(err.get("Message") or exc),
            "requestId": str(meta.get("RequestId") or ""),
            "httpStatus": int(meta.get("HTTPStatusCode") or 502),
            "at": now_iso(),
        }
    else:
        body = {
            "where": where,
            "type": type(exc).__name__,
            "message": str(exc),
            "requestId": "",
            "httpStatus": 500,
            "at": now_iso(),
        }

    log.error(
        "vpoc.error where=%s type=%s msg=%s",
        body["where"],
        body["type"],
        body["message"][:800],
    )
    if not settings.RAW_ERRORS and not (
        isinstance(exc, VpocError) and exc.kind in USER_FACING_KINDS
    ):
        log.error("vpoc.error.raw %s", traceback.format_exc()[:4000])
        body["message"] = f"처리 중 오류가 발생했습니다 (코드: {body['type']})"
        body["requestId"] = ""
    return body


def status_for(body: dict) -> int:
    """봉투의 httpStatus 를 라우트 응답 코드로 옮긴다."""
    status = int(body.get("httpStatus") or 500)
    if status in (400, 401, 403, 404, 409, 413):
        return status
    if 200 <= status < 300:
        return 502
    if status >= 500:
        return 502
    return 502
