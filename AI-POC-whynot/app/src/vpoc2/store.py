"""저장소. S3(운영·dev) 와 로컬 폴더(목 모드·테스트) 두 가지를 같은 모양으로 쓴다.

키 규칙 (모두 IRSA 허용 접두사 `users/` 아래):
  주 버킷   users/{owner}/video/_vpoc2/index.json            사용자의 작업 목록
            users/{owner}/video/_vpoc2/{runId}/run.json      작업 상태 (화면의 단일 원본)
            users/{owner}/video/_vpoc2/{runId}/characters/{charId}-{n}.jpg
            users/{owner}/video/_vpoc2/{runId}/final-{UTC시각}.mp4 15초 합본
            users/_vpoc2/ledger.json                         비용 원장
            users/_vpoc2/reviews.json                        리뷰 모음
  영상 버킷 users/{owner}/video/_vpoc2/{runId}/shots/{shotId}-{attempt}/  Luma 출력
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from typing import Any

from . import settings
from .errors import Vpoc2Error, from_client_error

MAIN = "main"
VIDEO = "video"


class PreconditionFailed(Exception):
    """조건부 쓰기 충돌 (다른 요청이 먼저 썼다)."""


class MaybeMissing(Exception):
    """GET 이 AccessDenied. 버킷 ListBucket 권한이 없으면 없는 키도 AccessDenied 로 온다.
    삼키지 않는다 — 기본값이 있는 문서만 '없으면 만들기(If-None-Match)' 로 판정한다 (get_or_create)."""

    def __init__(self, error: Vpoc2Error):
        super().__init__(error.message)
        self.error = error


def safe_owner(owner: str) -> str:
    """server.safe_user_prefix 와 같은 규칙. 사용자 이름(이메일 등)을 S3 키에 그대로 넣지 않는다."""
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "-", owner).strip("-")
    return normalized or "user"


def run_prefix(owner: str, run_id: str) -> str:
    return f"users/{safe_owner(owner)}/video/_vpoc2/{run_id}"


def index_key(owner: str) -> str:
    return f"users/{safe_owner(owner)}/video/_vpoc2/index.json"


LEDGER_KEY = "users/_vpoc2/ledger.json"
REVIEWS_KEY = "users/_vpoc2/reviews.json"


class LocalStore:
    """로컬 폴더 저장소. etag 는 내용의 sha1."""

    def __init__(self, root: str):
        self.root = root
        self._lock = threading.Lock()

    def _path(self, bucket: str, key: str) -> str:
        if ".." in key.split("/"):
            raise Vpoc2Error("BadKey", key, http=400)
        return os.path.join(self.root, bucket, *key.split("/"))

    @staticmethod
    def _etag(data: bytes) -> str:
        return hashlib.sha1(data).hexdigest()

    def get_bytes(self, bucket: str, key: str) -> tuple[bytes, str] | None:
        path = self._path(bucket, key)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            data = f.read()
        return data, self._etag(data)

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str,
                  if_match: str | None = None, if_none_match: bool = False) -> str:
        path = self._path(bucket, key)
        with self._lock:
            exists = os.path.exists(path)
            if if_none_match and exists:
                raise PreconditionFailed(key)
            if if_match is not None:
                if not exists:
                    raise PreconditionFailed(key)
                with open(path, "rb") as f:
                    if self._etag(f.read()) != if_match:
                        raise PreconditionFailed(key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return self._etag(data)

    def list_keys(self, bucket: str, prefix: str) -> list[str]:
        base = os.path.join(self.root, bucket)
        out = []
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                if name.endswith(".tmp"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), base).replace(os.sep, "/")
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)

    def presign(self, bucket: str, key: str, content_type: str | None = None) -> str:
        return f"/api/vpoc2/_local/{bucket}/{key}"


class S3Store:
    """S3 저장소. 버킷마다 그 리전 클라이언트(s3v4 + virtual addressing)를 쓴다."""

    def __init__(self, main_bucket: str, main_region: str, video_bucket: str, video_region: str):
        if not main_bucket or not video_bucket:
            raise Vpoc2Error("ConfigMissing", "VPOC2_MAIN_BUCKET / VPOC2_VIDEO_BUCKET 이 비어 있다", http=500)
        self.buckets = {MAIN: (main_bucket, main_region), VIDEO: (video_bucket, video_region)}
        self._clients: dict[str, Any] = {}
        self._conditional: bool | None = None
        self._lock = threading.Lock()

    def _client(self, region: str):
        with self._lock:
            if region not in self._clients:
                import boto3
                from botocore.config import Config

                self._clients[region] = boto3.client(
                    "s3",
                    region_name=region,
                    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
                )
            return self._clients[region]

    def _bucket(self, bucket: str) -> tuple[str, Any]:
        name, region = self.buckets[bucket]
        return name, self._client(region)

    def get_bytes(self, bucket: str, key: str) -> tuple[bytes, str] | None:
        name, client = self._bucket(bucket)
        try:
            resp = client.get_object(Bucket=name, Key=key)
        except Exception as exc:  # noqa: BLE001 - NoSuchKey 만 None, 나머지(AccessDenied 포함)는 올린다
            code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404"}:
                return None
            if code in {"AccessDenied", "403"}:
                raise MaybeMissing(from_client_error(exc, "s3.get_object")) from exc
            raise from_client_error(exc, "s3.get_object") from exc
        return resp["Body"].read(), resp["ETag"]

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str,
                  if_match: str | None = None, if_none_match: bool = False) -> str:
        name, client = self._bucket(bucket)
        kwargs: dict[str, Any] = {"Bucket": name, "Key": key, "Body": data, "ContentType": content_type}
        if self._conditional is not False:
            if if_match is not None:
                kwargs["IfMatch"] = if_match
            if if_none_match:
                kwargs["IfNoneMatch"] = "*"
        try:
            resp = client.put_object(**kwargs)
            if "IfMatch" in kwargs or "IfNoneMatch" in kwargs:
                self._conditional = True
        except Exception as exc:  # noqa: BLE001
            code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"}:
                raise PreconditionFailed(key) from exc
            if (type(exc).__name__ == "ParamValidationError" or code == "NotImplemented") and if_none_match:
                # '없을 때만 만들기' 를 판정할 수 없으면 덮어쓰지 않고 멈춘다 (원장 · 리뷰를 지우지 않게)
                raise from_client_error(exc, "s3.put_object(IfNoneMatch)") from exc
            if type(exc).__name__ == "ParamValidationError" or code == "NotImplemented":
                # 조건부 쓰기를 지원하지 않는 환경: 한 번 판정하고 이후 조건 없이 쓴다 (vpoc1 A17)
                self._conditional = False
                kwargs.pop("IfMatch", None)
                kwargs.pop("IfNoneMatch", None)
                resp = client.put_object(**kwargs)
            else:
                raise from_client_error(exc, "s3.put_object") from exc
        return resp["ETag"]

    def list_keys(self, bucket: str, prefix: str) -> list[str]:
        name, client = self._bucket(bucket)
        keys: list[str] = []
        token = None
        try:
            while True:
                kwargs = {"Bucket": name, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = client.list_objects_v2(**kwargs)
                keys.extend(obj["Key"] for obj in resp.get("Contents", []))
                if not resp.get("IsTruncated"):
                    break
                token = resp.get("NextContinuationToken")
        except Exception as exc:  # noqa: BLE001
            raise from_client_error(exc, "s3.list_objects_v2") from exc
        return sorted(keys)

    def presign(self, bucket: str, key: str, content_type: str | None = None) -> str:
        name, client = self._bucket(bucket)
        params = {"Bucket": name, "Key": key}
        if content_type:
            # Luma 출력은 application/octet-stream 으로 저장된다 → video/mp4 로 덮어야 재생된다
            params["ResponseContentType"] = content_type
        return client.generate_presigned_url("get_object", Params=params, ExpiresIn=settings.PRESIGN_SECONDS)


# --- JSON 헬퍼 ---------------------------------------------------------------

def get_json(store, key: str) -> tuple[Any, str] | None:
    try:
        got = store.get_bytes(MAIN, key)
    except MaybeMissing as mm:
        raise mm.error from None
    if got is None:
        return None
    data, etag = got
    return json.loads(data.decode("utf-8")), etag


def get_or_create_json(store, key: str, default: Any) -> tuple[Any, str]:
    """기본값이 있는 문서(목록 · 원장 · 리뷰). 없으면 If-None-Match 로 만든다.
    GET 이 AccessDenied 인데 만들기도 412(이미 있음)면 진짜 권한 문제 → 원래 오류를 올린다."""
    try:
        got = store.get_bytes(MAIN, key)
    except MaybeMissing as mm:
        try:
            etag = put_json(store, key, default, if_none_match=True)
        except PreconditionFailed:
            raise mm.error from None
        return json.loads(json.dumps(default)), etag
    if got is None:
        try:
            etag = put_json(store, key, default, if_none_match=True)
            return json.loads(json.dumps(default)), etag
        except PreconditionFailed:
            got = store.get_bytes(MAIN, key)
    data, etag = got
    return json.loads(data.decode("utf-8")), etag


def put_json(store, key: str, obj: Any, if_match: str | None = None, if_none_match: bool = False) -> str:
    data = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
    return store.put_bytes(MAIN, key, data, "application/json", if_match=if_match, if_none_match=if_none_match)


def update_json(store, key: str, default: Any, mutate, retries: int = 5) -> Any:
    """읽고 → 고치고 → 조건부로 쓴다. 충돌하면 다시 읽어서 재시도한다."""
    for _ in range(retries):
        obj, etag = get_or_create_json(store, key, default)
        result = mutate(obj)
        try:
            put_json(store, key, obj, if_match=etag)
            return result
        except PreconditionFailed:
            continue
    raise Vpoc2Error("WriteConflict", f"{key} 에 쓰기 충돌이 계속된다", http=409)


_STORE = None
_STORE_LOCK = threading.Lock()


def get_store():
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            if settings.MOCK and settings.LOCAL_DIR:
                _STORE = LocalStore(settings.LOCAL_DIR)
            else:
                _STORE = S3Store(settings.MAIN_BUCKET, settings.MAIN_REGION,
                                 settings.VIDEO_BUCKET, settings.VIDEO_REGION)
        return _STORE


def set_store(store) -> None:
    """테스트용."""
    global _STORE
    with _STORE_LOCK:
        _STORE = store
