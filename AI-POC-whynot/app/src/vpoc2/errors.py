"""오류 봉투. 모든 실패는 원문을 그대로 담는다 (PRD 6.7 규칙을 따른다)."""
from __future__ import annotations

import datetime as _dt
from typing import Any


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Vpoc2Error(Exception):
    """화면에 보여 줄 오류. http 는 응답 상태 코드."""

    def __init__(self, type_: str, message: str, *, where: str = "vpoc2", http: int = 400,
                 request_id: str = "", http_status: int | None = None):
        super().__init__(message)
        self.type = type_
        self.message = message
        self.where = where
        self.http = http
        self.request_id = request_id
        self.http_status = http_status if http_status is not None else http

    def envelope(self) -> dict[str, Any]:
        return {
            "where": self.where,
            "type": self.type,
            "message": self.message,
            "requestId": self.request_id,
            "httpStatus": self.http_status,
            "at": now_iso(),
        }


def from_client_error(exc: Exception, where: str) -> Vpoc2Error:
    """botocore ClientError 를 원문 그대로 옮긴다. 다른 예외도 타입과 메시지를 보존한다."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        err = response.get("Error", {}) or {}
        meta = response.get("ResponseMetadata", {}) or {}
        status = int(meta.get("HTTPStatusCode") or 502)
        return Vpoc2Error(
            str(err.get("Code") or type(exc).__name__),
            str(err.get("Message") or exc),
            where=where,
            http=502,
            request_id=str(meta.get("RequestId") or ""),
            http_status=status,
        )
    return Vpoc2Error(type(exc).__name__, str(exc), where=where, http=502, http_status=500)


def public(envelope: dict[str, Any] | None, raw: bool) -> dict[str, Any] | None:
    """운영에서 원문을 숨길 때 message 만 바꾼다."""
    if envelope is None or raw:
        return envelope
    hidden = dict(envelope)
    hidden["message"] = f"처리 중 오류가 발생했습니다 (코드: {envelope.get('type')})"
    return hidden
