"""vpoc1 저장소 — PRD 6.3 · 6.4.

화면의 단일 원본은 S3 의 `state.json` 이다. 경합은 S3 조건부 쓰기(If-Match)로 막고,
boto3 가 조건부 쓰기를 못 하면 작업 중 저장을 409 로 막는 잠금만으로 간다(부록 F A17).

키는 모두 `users/{owner}/video/{pid}/vpoc1/` 아래에 둔다. 앱 IRSA 가 허용하는 접두사가
`users/*` 이고, 구 영상 상태 파일과 겹치지 않도록 `vpoc1/` 한 단계를 더 둔다.
"""

from __future__ import annotations

import io
import json
import secrets
import threading
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ParamValidationError

from . import settings
from .errors import VpocError, now_iso

_CLIENTS: dict[str, object] = {}
_CLIENT_LOCK = threading.Lock()

# 조건부 쓰기를 지원하지 않는 boto3 를 한 번 확인하고 기억한다.
_CONDITIONAL_WRITES = {"supported": True}


def s3_client(region: str):
    with _CLIENT_LOCK:
        client = _CLIENTS.get(region)
        if client is None:
            # 버킷마다 그 리전의 클라이언트를 쓴다. 서명 리전이 다르면 presign 이 실패한다.
            client = boto3.client(
                "s3",
                region_name=region,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "virtual"},
                    retries={"max_attempts": 3},
                ),
            )
            _CLIENTS[region] = client
        return client


def new_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{prefix}_{stamp}_{secrets.token_hex(2)}"


def short_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


# ---------------------------------------------------------------- 키 규칙


def base_prefix(owner: str, pid: str) -> str:
    return f"users/{owner}/video/{pid}/vpoc1"


def plan_prefix(owner: str, pid: str, plan_id: str) -> str:
    return f"{base_prefix(owner, pid)}/plans/{plan_id}"


def state_key(owner: str, pid: str, plan_id: str) -> str:
    return f"{plan_prefix(owner, pid, plan_id)}/state.json"


def new_gen_id() -> str:
    """생성 회차 id. ③을 고쳐 다시 승인하면 새 회차가 된다.

    이것이 없어서 2차 생성이 1차와 같은 clientRequestToken 을 만들었고, Bedrock 이 멱등 처리로
    1차 invocation 을 돌려줘 옛 영상이 다시 붙었다(docs/vpoc1/fix-regen-reload.md).
    """
    return "g" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def job_key(
    owner: str, pid: str, plan_id: str, shot_id: str, attempt: int, gen_id: str = ""
) -> str:
    base = plan_prefix(owner, pid, plan_id)
    if gen_id:
        return f"{base}/jobs/{gen_id}/{shot_id}-{attempt}.json"
    # genId 가 없는 옛 계획은 옛 키를 그대로 읽는다(마이그레이션 쓰기 없음).
    return f"{base}/jobs/{shot_id}-{attempt}.json"


def final_key(owner: str, pid: str, plan_id: str) -> str:
    return f"{plan_prefix(owner, pid, plan_id)}/final.json"


def final_mp4_key(owner: str, pid: str, plan_id: str) -> str:
    return f"{plan_prefix(owner, pid, plan_id)}/final.mp4"


def finals_prefix(owner: str, pid: str, plan_id: str) -> str:
    """버전 전환 기록을 쌓는 곳. `final.json` 은 덮어쓰지 않는다(regen-plan G4)."""
    return f"{plan_prefix(owner, pid, plan_id)}/finals/"


def finals_key(owner: str, pid: str, plan_id: str, stamp: str) -> str:
    safe = stamp.replace(":", "").replace("-", "")
    return f"{finals_prefix(owner, pid, plan_id)}{safe}.json"


def project_key(owner: str, pid: str) -> str:
    return f"{base_prefix(owner, pid)}/project.json"


def upload_key(owner: str, pid: str, file_id: str) -> str:
    return f"{base_prefix(owner, pid)}/uploads/{file_id}.jpg"


def upload_meta_key(owner: str, pid: str, file_id: str) -> str:
    return f"{base_prefix(owner, pid)}/uploads/{file_id}.json"


def character_key(owner: str, pid: str, char_id: str, image_id: str) -> str:
    return f"{base_prefix(owner, pid)}/characters/{char_id}/{image_id}.jpg"


def keyframe_key(owner: str, pid: str, plan_id: str, keyframe_id: str, kind: str, image_id: str) -> str:
    """키 장면 그림(kind = rough | final)."""
    return f"{base_prefix(owner, pid)}/plans/{plan_id}/keyframes/{keyframe_id}/{kind}-{image_id}.jpg"


def master_background_key(owner: str, pid: str, plan_id: str, image_id: str) -> str:
    """영상 1개(single)의 배경 그림(인물 없음) 키. 설계자 2026-10-19.

    IRSA 가 `users/*` 로 제한되므로 그 아래에 둔다. 마스터 1개 = 배경 1장이지만 다시 그릴 때마다
    새 image_id 를 써서 캐시가 섞이지 않게 한다.
    """
    return f"{base_prefix(owner, pid)}/plans/{plan_id}/master/background-{image_id}.jpg"


def plans_prefix(owner: str, pid: str) -> str:
    return f"{base_prefix(owner, pid)}/plans/"


def video_output_prefix(
    owner: str, pid: str, plan_id: str, shot_id: str, attempt: int, gen_id: str = ""
) -> str:
    base = f"users/{owner}/video/{pid}/vpoc1/plans/{plan_id}/shots"
    if gen_id:
        return f"{base}/{gen_id}/{shot_id}-{attempt}"
    return f"{base}/{shot_id}-{attempt}"


LEDGER_KEY = "users/_vpoc1/ledger.json"
SMOKE_PREFIX = "users/{user}/video/_smoke"


# ---------------------------------------------------------------- 원시 S3


class Store:
    def __init__(self) -> None:
        self.main_bucket = settings.MAIN_BUCKET
        self.main_region = settings.AWS_REGION
        self.video_bucket = settings.VIDEO_BUCKET
        self.video_region = settings.VIDEO_REGION

    # --- bytes ---

    def put_bytes(self, key: str, body: bytes, content_type: str, *, bucket: str = "") -> None:
        target = bucket or self.main_bucket
        region = self.video_region if target == self.video_bucket else self.main_region
        s3_client(region).put_object(
            Bucket=target,
            Key=key,
            Body=body,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )

    def get_bytes(self, key: str, *, bucket: str = "") -> bytes:
        target = bucket or self.main_bucket
        region = self.video_region if target == self.video_bucket else self.main_region
        return s3_client(region).get_object(Bucket=target, Key=key)["Body"].read()

    def exists(self, key: str, *, bucket: str = "") -> bool:
        target = bucket or self.main_bucket
        region = self.video_region if target == self.video_bucket else self.main_region
        try:
            s3_client(region).head_object(Bucket=target, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def list_keys(self, prefix: str, *, bucket: str = "") -> list[dict]:
        target = bucket or self.main_bucket
        region = self.video_region if target == self.video_bucket else self.main_region
        client = s3_client(region)
        out: list[dict] = []
        token = ""
        while True:
            kwargs = {"Bucket": target, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for obj in page.get("Contents", []) or []:
                out.append({"key": obj["Key"], "size": obj["Size"]})
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken") or ""
            if not token:
                break
        return out

    def list_prefixes(self, prefix: str) -> list[str]:
        client = s3_client(self.main_region)
        out: list[str] = []
        token = ""
        while True:
            kwargs = {
                "Bucket": self.main_bucket,
                "Prefix": prefix,
                "Delimiter": "/",
                "MaxKeys": 1000,
            }
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for item in page.get("CommonPrefixes", []) or []:
                out.append(item["Prefix"])
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken") or ""
            if not token:
                break
        return out

    def presign(self, key: str, *, bucket: str = "", content_type: str = "", expires: int = 3600) -> str:
        """조회 때마다 새로 서명한다. URL 은 저장하지 않는다(IRSA 임시 자격 증명)."""
        target = bucket or self.main_bucket
        region = self.video_region if target == self.video_bucket else self.main_region
        params: dict = {"Bucket": target, "Key": key}
        if content_type:
            # Luma 출력은 application/octet-stream 으로 올라온다. 브라우저가 재생하게 덮어쓴다.
            params["ResponseContentType"] = content_type
        return s3_client(region).generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires
        )

    # --- json ---

    def read_json(self, key: str) -> tuple[dict | None, str]:
        try:
            response = s3_client(self.main_region).get_object(Bucket=self.main_bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None, ""
            raise
        etag = (response.get("ETag") or "").strip('"')
        return json.loads(response["Body"].read().decode("utf-8")), etag

    def write_json(self, key: str, data: dict, *, etag: str = "", create_only: bool = False) -> str:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        kwargs: dict = {
            "Bucket": self.main_bucket,
            "Key": key,
            "Body": body,
            "ContentType": "application/json; charset=utf-8",
            "ServerSideEncryption": "AES256",
        }
        if _CONDITIONAL_WRITES["supported"]:
            if create_only:
                kwargs["IfNoneMatch"] = "*"
            elif etag:
                kwargs["IfMatch"] = f'"{etag}"'
        try:
            response = s3_client(self.main_region).put_object(**kwargs)
        except ParamValidationError:
            # boto3 가 조건부 쓰기를 모르는 버전이다. 한 번만 확인하고 이후로는 붙이지 않는다.
            _CONDITIONAL_WRITES["supported"] = False
            kwargs.pop("IfNoneMatch", None)
            kwargs.pop("IfMatch", None)
            response = s3_client(self.main_region).put_object(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise VpocError(
                    "store.write_json",
                    "PreconditionFailed",
                    "다른 탭이나 작업이 같은 계획을 먼저 저장했습니다. 새로고침한 뒤 다시 시도해 주세요.",
                    409,
                ) from exc
            if code == "NotImplemented":
                _CONDITIONAL_WRITES["supported"] = False
                kwargs.pop("IfNoneMatch", None)
                kwargs.pop("IfMatch", None)
                response = s3_client(self.main_region).put_object(**kwargs)
            else:
                raise
        return (response.get("ETag") or "").strip('"')


STORE = Store()


# ---------------------------------------------------------------- 계획 상태


def empty_step(number: int) -> dict:
    if number == 1:
        return {"status": "empty", "text": "", "characters": [], "sceneFiles": []}
    if number == 2:
        return {
            "status": "empty",
            "story": {},
            "characters": [],
            "questions": [],
            "answers": {},
            "extraKo": "",
        }
    if number == 3:
        return {
            "status": "empty",
            "useKeyframes": False,
            "characters": [],
            "shots": [],
            "cost": {},
        }
    return {"status": "empty", "shots": [], "final": {"status": "none", "url": None, "error": None}}


def new_state(owner: str, pid: str, plan_id: str) -> dict:
    return {
        "planId": plan_id,
        "projectId": pid,
        "owner": owner,
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
        "currentStep": 1,
        "maxReachedStep": 1,
        "task": {
            "name": None,
            "status": "idle",
            "startedAt": None,
            "finishedAt": None,
            "error": None,
        },
        # 단계별 revision. 작업이 끝날 때 값이 바뀌었으면 결과를 버린다(되돌리기 = 취소).
        "revisions": {"1": 0, "2": 0, "3": 0},
        "steps": {str(n): empty_step(n) for n in (1, 2, 3, 4)},
    }


def load_state(owner: str, pid: str, plan_id: str) -> tuple[dict, str]:
    data, etag = STORE.read_json(state_key(owner, pid, plan_id))
    if data is None:
        raise VpocError("store.load_state", "NotFound", "계획을 찾을 수 없습니다.", 404)
    return data, etag


def save_state(owner: str, pid: str, plan_id: str, state: dict, etag: str = "") -> str:
    state["updatedAt"] = now_iso()
    return STORE.write_json(state_key(owner, pid, plan_id), state, etag=etag)


def mutate_state(owner: str, pid: str, plan_id: str, mutator, *, attempts: int = 4):
    """읽고-고치고-조건부쓰기. 경합이면 다시 읽어서 재시도한다."""
    last: BaseException | None = None
    for _ in range(attempts):
        state, etag = load_state(owner, pid, plan_id)
        result = mutator(state)
        try:
            save_state(owner, pid, plan_id, state, etag)
            return state, result
        except VpocError as exc:
            if exc.kind != "PreconditionFailed":
                raise
            last = exc
    raise last if last else RuntimeError("mutate_state failed")


def mark_stale(state: dict, from_step: int) -> dict:
    """앞 단계를 다시 실행하면 뒤 단계는 '다시 만들어야 함'이 된다.

    ④는 status 만 바꾸는 것으로 끝나지 않는다. 이전 회차의 `shots`(invocationArn·outputKey)와
    `regen`·`revision`·`final`·`genId` 가 남아 있으면 되돌아간 직후 화면이 옛 영상을 그대로
    보여 주고, 완료 처리가 이전 회차에 묶인다(2026-09-23, docs/vpoc1/fix-regen-reload.md).
    그래서 **④ 참조는 비운다.** S3 의 과거 jobs·영상 파일은 지우지 않는다 — 기록이다.
    """
    dropped = {"characterImages": 0, "shots": 0, "videos": 0}
    for number in range(from_step + 1, 5):
        step = state["steps"][str(number)]
        if number == 3:
            dropped["characterImages"] = sum(
                1 for item in step.get("characters", []) if (item.get("image") or {}).get("imageId")
            )
            dropped["shots"] = len(step.get("shots", []))
        if number == 4:
            dropped["videos"] = sum(
                1 for item in step.get("shots", []) if item.get("status") == "completed"
            )
            if step.get("status") not in ("empty",):
                state["steps"]["4"] = empty_step(4)
                state["steps"]["4"]["status"] = "stale"
                state.pop("finalizedAt", None)
                continue
        if step.get("status") not in ("empty",):
            step["status"] = "stale"
    return dropped


def bump_revision(state: dict, step: int) -> int:
    key = str(step)
    value = int(state.get("revisions", {}).get(key, 0)) + 1
    state.setdefault("revisions", {})[key] = value
    return value


def revision_of(state: dict, step: int) -> int:
    return int(state.get("revisions", {}).get(str(step), 0))
