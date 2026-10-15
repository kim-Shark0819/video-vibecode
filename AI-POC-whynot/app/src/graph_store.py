"""Neptune graph access layer.

Graph model
-----------
Vertices
  user      : username, passwordHash, passwordSalt, role, displayName, createdAt, lastLoginAt
  project   : id, title, story, visibility(shared|private), coverKey, coverSource, createdAt, updatedAt
  setting   : key, value               (single vertex per key, e.g. companyCode)

Edges
  (user)-[:owns]->(project)

스토리 계보
-----------
프로젝트는 고유한 스토리 하나를 갖고, 결과물은 **그 스토리로 만든 것만** 보관한다.
그 관계를 증명할 수 있게 story_output / script_job 에 생성 시점의 스토리 지문을 박는다
(storyHash, storyTextRef, storyType, storySnapshot, storyCharCount).
값을 만드는 곳은 story_context.stamp, 어긋남을 판정하는 곳은 story_context.stamp_drift 다.

Neptune speaks openCypher over HTTPS, so no extra driver is needed: plain
requests against /openCypher keeps the container image small and avoids the
gremlinpython dependency. Access is restricted at the VPC/security-group level.
"""

import base64
import hashlib
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

NEPTUNE_ENDPOINT = os.getenv("NEPTUNE_ENDPOINT", "").strip()
NEPTUNE_PORT = os.getenv("NEPTUNE_PORT", "8182").strip() or "8182"
NEPTUNE_USE_TLS = os.getenv("NEPTUNE_USE_TLS", "true").strip().lower() != "false"
REQUEST_TIMEOUT = int(os.getenv("NEPTUNE_TIMEOUT_SECONDS", "15"))
PBKDF2_ITERATIONS = int(os.getenv("PBKDF2_ITERATIONS", "210000"))
VISIBILITY_SHARED = "shared"
VISIBILITY_PRIVATE = "private"

# 진입점: 어떤 자료를 들고 시작하는지에 따라 AI가 수행할 단계가 달라진다.
INPUT_WEBTOON = "webtoon"      # 웹툰 원작만 있음 -> 영상화 분석, 각색 방향, 트리트먼트
INPUT_LOGLINE = "logline"      # 소재/로그라인만 있음 -> 구체화, 인물 설정, 트리트먼트
INPUT_TREATMENT = "treatment"  # 트리트먼트 보유 -> 대본 초안
INPUT_SCRIPT = "script"        # 미리 논의된 대본 보유 -> 톤 분석, 후속화 확장, 콘티
INPUT_TYPES = (INPUT_WEBTOON, INPUT_LOGLINE, INPUT_TREATMENT, INPUT_SCRIPT)

# 기획 방향 파라미터의 기본값. 화수 기본 4는 시연 시간을 고려한 값이다.
DIRECTION_DEFAULTS = {
    "targetPlatform": "OTT",
    "genres": [],
    "episodeCount": 4,
    "tone": "",
    "audience": "",
    "freeDirection": "",
}
# 표지를 바꿀 때 직전 이미지를 몇 개까지 보관할지. 초과분은 S3에서도 지운다.
COVER_HISTORY_LIMIT = int(os.getenv("COVER_HISTORY_LIMIT", "3"))
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

# --------------------------------------------------------------------------- #
# 결과물 생성 한도 (credit)
# --------------------------------------------------------------------------- #
# 사용자가 실제로 결과물을 하나 받을 때마다 1건씩 차감한다. **게이트웨이 호출 수가 아니다.**
# 콘티 한 장을 만들면 내부적으로 그림체 판단 1회 + 컷 나누기 1회 + 컷마다 이미지 1회가
# 돌아가서 호출은 10회가 넘는다. 그것을 세면 컷 8개짜리 콘티 한 번에 한도가 10 줄어든다.
# 사용자가 세고 싶은 것은 "내가 받은 결과물"이라 진입점에서 1건으로 센다.
#
# 두 갈래로 나눈 이유: 영상은 GPU·모델 시간이 압도적으로 비싸고 한 건이 수십 분이다.
# 문서·이미지와 같은 주머니에 넣으면 영상 몇 건이 나머지 전부를 먹는다.
QUOTA_VIDEO = "video"
QUOTA_ASSET = "asset"
QUOTA_CATEGORIES = (QUOTA_VIDEO, QUOTA_ASSET)

# AI 산출물 종류. 진입점 타입에 따라 만들 수 있는 것이 다르다(문서별 허용 목록은 documents.py).
DOC_ANALYSIS = "analysis"        # 영상화 가능성 분석 + 추천안
DOC_ADAPTATION = "adaptation"    # 각색 방향 제안
DOC_LOGLINE = "logline"          # 로그라인 구체화
DOC_CHARACTERS = "characters"    # 인물 설정
DOC_TREATMENT = "treatment"      # 트리트먼트 (화별 시놉시스 포함)
DOCUMENT_KINDS = (DOC_ANALYSIS, DOC_ADAPTATION, DOC_LOGLINE, DOC_CHARACTERS, DOC_TREATMENT)

# 생성은 수십 초가 걸려 요청 안에서 끝내지 않는다. 상태를 문서 자체에 남기고 프론트가 폴링한다.
DOC_GENERATING = "generating"
DOC_READY = "ready"
DOC_FAILED = "failed"
SETTING_COMPANY_CODE = "companyCode"

logger = logging.getLogger("toon-change-ai-pj-app.graph")
_bootstrap_lock = threading.Lock()
_bootstrap_done = False


class GraphError(RuntimeError):
    """Raised when the graph backend cannot serve a request."""


class GraphUnavailable(GraphError):
    """Raised when Neptune is unreachable or not configured."""


class ConflictError(GraphError):
    """Raised when a uniqueness rule would be violated."""


class NotFoundError(GraphError):
    """Raised when a referenced vertex does not exist."""


class AuthError(GraphError):
    """Raised when a credential check fails.

    `authenticate` 는 로그인 실패를 None 으로 돌려주지만, 비밀번호 변경은 "계정이 없음"과
    "현재 비밀번호가 틀림"을 호출부가 구분해야 해서 예외로 알린다.
    """


def is_configured() -> bool:
    return bool(NEPTUNE_ENDPOINT)


def _endpoint_url() -> str:
    scheme = "https" if NEPTUNE_USE_TLS else "http"
    return f"{scheme}://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/openCypher"


def run(query: str, parameters: dict | None = None) -> list[dict]:
    """Execute an openCypher query and return the result rows."""
    if not is_configured():
        raise GraphUnavailable(
            "NEPTUNE_ENDPOINT가 설정되지 않았습니다. 배포 설정을 확인해 주세요."
        )

    body = {"query": query}
    if parameters:
        # Neptune expects the parameter map as a JSON string.
        import json as _json

        body["parameters"] = _json.dumps(parameters)

    data = urllib.parse.urlencode(body).encode("utf-8")
    request = urllib.request.Request(
        _endpoint_url(),
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            import json as _json

            payload = _json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        logger.error("Neptune query failed: %s %s", error.code, detail)
        raise GraphError(f"그래프 질의가 실패했습니다: HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        logger.error("Neptune unreachable: %s", error)
        raise GraphUnavailable("그래프 데이터베이스에 연결할 수 없습니다.") from error

    return payload.get("results", [])


def health() -> dict:
    if not is_configured():
        return {"configured": False, "reachable": False, "detail": "NEPTUNE_ENDPOINT 미설정"}
    try:
        run("MATCH (n) RETURN count(n) AS total")
        return {"configured": True, "reachable": True, "detail": "ok"}
    except GraphError as error:
        return {"configured": True, "reachable": False, "detail": str(error)}


# --------------------------------------------------------------------------- #
# password hashing
# --------------------------------------------------------------------------- #
def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return base64.b64encode(digest).decode("ascii"), base64.b64encode(salt).decode("ascii")


def verify_password(password: str, stored_hash: str, stored_salt: str) -> bool:
    try:
        salt = base64.b64decode(stored_salt)
    except (ValueError, TypeError):
        return False
    candidate, _ = hash_password(password, salt)
    return secrets.compare_digest(candidate, stored_hash)


def _config_password_rotated(existing: dict | None, config_password: str) -> bool:
    """배포 설정의 비밀번호가 이 계정에 마지막으로 반영된 값과 다른가.

    내장 계정의 비밀번호를 언제 덮어쓸지 결정한다. 자세한 배경은 `merge_user` 참고.

    - 계정이 없다        -> False. `ON CREATE` 가 어차피 설정값을 넣는다.
    - 지문이 없다        -> True. 이 코드 이전에 만들어진 계정이라 한 번 맞춰야 한다.
    - 지문이 다르다      -> True. tfvars/Secret 이 교체되었다.
    - 지문이 같다        -> False. 설정은 그대로다. 사용자가 바꾼 값을 지키기 위해 손대지 않는다.
    """
    if not existing:
        return False
    stored_hash = existing.get("configPasswordHash")
    stored_salt = existing.get("configPasswordSalt")
    if not stored_hash or not stored_salt:
        return True
    return not verify_password(config_password, stored_hash, stored_salt)


def _filter_updates(fields: dict, allowed: set[str], what: str) -> dict:
    """부분 수정 요청에서 저장할 항목만 골라낸다.

    허용 목록에 없는 키는 버린다. 임의의 속성이 정점에 붙는 것을 막기 위한 것이다.

    ## 왜 걸러진 키를 오류 메시지에 넣는가

    예전에는 걸러낸 뒤 비어 있으면 "변경할 항목이 없습니다" 만 던졌다. 그래서 호출부가
    **정점에는 있지만 허용 목록에 없는** 속성명을 넘겼을 때 원인을 알 수 없었다.
    실제로 콘티 생성이 그렇게 죽었다. `update_storyboard` 의 허용 목록에 `prompt` 와
    `stylePrompt` 가 빠져 있어, 프롬프트 보강 결과를 저장하려던 호출이 통째로 걸러지고
    화면에는 "예상치 못한 오류: 변경할 항목이 없습니다" 만 떴다. 어느 키가 문제인지
    알려 주지 않아 코드를 다 읽어야 원인을 찾을 수 있었다.

    이제 걸러진 키를 이름으로 알려 준다. 같은 실수를 다시 하면 메시지가 바로 가리킨다.
    """
    updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
    if updates:
        return updates

    rejected = sorted(key for key in fields if key not in allowed)
    if rejected:
        raise GraphError(
            f"{what} 수정에서 저장할 수 없는 항목만 넘어왔습니다: {', '.join(rejected)}."
            f" 저장 가능한 항목은 {', '.join(sorted(allowed))} 입니다."
        )
    raise GraphError("변경할 항목이 없습니다.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# settings (company code)
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: str | None = None) -> str | None:
    rows = run(
        "MATCH (s:setting {key: $key}) RETURN s.value AS value LIMIT 1", {"key": key}
    )
    if not rows:
        return default
    return rows[0].get("value", default)


def set_setting(key: str, value: str, actor: str) -> str:
    run(
        """
        MERGE (s:setting {key: $key})
        SET s.value = $value, s.updatedAt = $now, s.updatedBy = $actor
        RETURN s.value AS value
        """,
        {"key": key, "value": value, "now": _now(), "actor": actor},
    )
    logger.info("Setting updated key=%s by=%s", key, actor)
    return value


def get_company_code() -> str | None:
    return get_setting(SETTING_COMPANY_CODE)


def set_company_code(code: str, actor: str) -> str:
    return set_setting(SETTING_COMPANY_CODE, code, actor)


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
def _quota_value(raw) -> int | None:
    """한도 값을 정수로 읽는다. **없으면 None = 무제한이다.**

    0 과 None 을 구분하는 것이 중요하다. 0 은 "한 번도 못 쓴다"이고 None 은 "제한 없음"이다.
    둘을 섞으면 한도를 지우려다 계정을 잠그거나, 반대로 잠그려다 무제한으로 열어 준다.
    기존 사용자에게는 이 속성이 아예 없으므로 자동으로 무제한이 된다.
    """
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _user_view(row: dict) -> dict:
    return {
        "username": row.get("username"),
        "displayName": row.get("displayName") or row.get("username"),
        "role": row.get("role") or ROLE_MEMBER,
        "createdAt": row.get("createdAt"),
        "lastLoginAt": row.get("lastLoginAt"),
        "projectCount": int(row.get("projectCount") or 0),
        # 결과물 생성 한도. None 이면 무제한.
        "quotas": {
            category: _quota_value(row.get(f"{category}Quota")) for category in QUOTA_CATEGORIES
        },
    }


def get_user(username: str) -> dict | None:
    rows = run(
        """
        MATCH (u:user {username: $username})
        OPTIONAL MATCH (u)-[:owns]->(p:project)
        RETURN u.username AS username, u.displayName AS displayName, u.role AS role,
               u.createdAt AS createdAt, u.lastLoginAt AS lastLoginAt,
               u.passwordHash AS passwordHash, u.passwordSalt AS passwordSalt,
               u.configPasswordHash AS configPasswordHash,
               u.configPasswordSalt AS configPasswordSalt,
               u.passwordUpdatedAt AS passwordUpdatedAt,
               u.videoQuota AS videoQuota, u.assetQuota AS assetQuota,
               count(p) AS projectCount
        """,
        {"username": username},
    )
    return rows[0] if rows else None


def list_users() -> list[dict]:
    rows = run(
        """
        MATCH (u:user)
        OPTIONAL MATCH (u)-[:owns]->(p:project)
        RETURN u.username AS username, u.displayName AS displayName, u.role AS role,
               u.createdAt AS createdAt, u.lastLoginAt AS lastLoginAt,
               u.videoQuota AS videoQuota, u.assetQuota AS assetQuota,
               count(p) AS projectCount
        ORDER BY u.role DESC, u.username ASC
        """
    )
    return [_user_view(row) for row in rows]


def merge_user(
    username: str,
    password: str,
    role: str = ROLE_MEMBER,
    display_name: str | None = None,
    sync_password: bool = False,
    quotas: dict | None = None,
    sync_quota: bool = False,
) -> tuple[dict, bool]:
    """Idempotently create a user.

    MERGE keeps this safe when several replicas bootstrap at the same time: the
    second caller matches the existing vertex instead of creating a duplicate.
    Returns the user view plus whether this call created the vertex.

    `sync_password=True`면 내장 계정(admin, 시드 사용자)의 비밀번호를 배포 설정에 맞춘다.
    그 계정들은 배포 설정이 단일 원본이고, 설정을 바꿔도 계정이 이미 있으면 반영되지 않는
    문제가 있었다.

    ## 왜 무조건 덮어쓰지 않는가

    예전에는 `ON MATCH SET` 으로 기동할 때마다 해시를 설정값으로 덮어썼다. 그래서
    사용자가 스스로 비밀번호를 바꾸는 기능(`change_password`)을 넣으면 다음 파드
    재시작에 그 변경이 조용히 되돌아갔다.

    이제 **설정값이 실제로 바뀌었을 때만** 덮어쓴다. 마지막으로 반영한 설정값의 지문을
    정점에 `configPasswordHash`/`configPasswordSalt` 로 남겨 두고 비교한다.
      지문 일치      -> 설정은 그대로다. 비밀번호를 건드리지 않는다(사용자 변경 보존).
      지문 불일치    -> tfvars/Secret 이 교체되었다. 설정값으로 덮어쓰고 지문을 갱신한다.
      지문 없음      -> 이 코드 이전에 만들어진 계정이다. 한 번 맞추고 지문을 남긴다.

    덮어쓰기를 남겨 둔 이유: 비밀번호를 잊었을 때 tfvars 를 바꾸고 apply 하는 것이
    유일한 복구 경로다. 그 경로가 죽으면 계정이 잠긴다.

    지문을 sha256 같은 맨 해시로 두지 않은 것은 의도적이다. 그래프를 읽을 수 있는 쪽에
    비밀번호 해시가 두 종류 남는데, 하나가 약하면 그것이 실효 강도가 된다. 그래서
    같은 PBKDF2 를 그대로 재사용한다.
    """
    password_hash, salt = hash_password(password)
    marker = _now()

    # 설정값을 덮어쓸지 판단한다. 기존 정점의 지문을 읽어야 하므로 MERGE 와 분리된다.
    # Neptune 의 openCypher 로는 이 분기를 한 문장에 담을 수 없다(deduplicate_users 와 같은 이유).
    apply_config_password = sync_password and _config_password_rotated(
        get_user(username), password
    )

    # 한도는 비밀번호와 **다르게** 다룬다.
    #
    # 비밀번호는 배포 설정이 단일 원본이라 기동할 때마다 덮어쓴다. 한도는 관리자가 화면에서
    # 바꿀 수 있어야 하므로, 덮어쓰면 파드가 재시작될 때마다 관리자의 변경이 설정값으로
    # 조용히 되돌아간다. 그것을 알아차릴 방법이 없다.
    #
    # 그래서 설정은 **처음 만들 때**와 **아직 한도가 하나도 없을 때**만 적용한다.
    # coalesce(기존값, 설정값) 이라 이렇게 동작한다.
    #   계정이 없다 -> ON CREATE 로 설정값이 들어간다
    #   있는데 한도가 없다 -> 설정값이 들어간다 (설정에 적었는데 무제한으로 남는 구멍을 막는다)
    #   관리자가 이미 정했다 -> 그대로 둔다
    # 한도를 완전히 없애려면 관리자 API 로 지운다. 설정에서 지우는 것만으로는 풀리지 않는다.
    # 0 은 "한 번도 못 쓴다"는 유효한 값이라 coalesce 가 유지한다(None 과 구분된다).
    quota_params = {f"{category}Quota": None for category in QUOTA_CATEGORIES}
    if quotas:
        for category in QUOTA_CATEGORIES:
            quota_params[f"{category}Quota"] = _quota_value(quotas.get(category))

    updates = []
    if apply_config_password:
        # 해시와 지문을 함께 갱신한다. 지문을 빼먹으면 다음 기동에도 불일치로 보여
        # 매번 덮어쓰게 되고, 사용자가 바꾼 비밀번호를 계속 되돌린다.
        updates.append(
            "u.passwordHash = $hash, u.passwordSalt = $salt, "
            "u.configPasswordHash = $hash, u.configPasswordSalt = $salt"
        )
    if sync_quota:
        updates.append(
            "u.videoQuota = coalesce(u.videoQuota, $videoQuota), "
            "u.assetQuota = coalesce(u.assetQuota, $assetQuota)"
        )
    on_match = ("ON MATCH SET " + ", ".join(updates)) if updates else ""

    rows = run(
        f"""
        MERGE (u:user {{username: $username}})
        ON CREATE SET u.passwordHash = $hash, u.passwordSalt = $salt, u.role = $role,
                      u.displayName = $displayName, u.createdAt = $now,
                      u.videoQuota = $videoQuota, u.assetQuota = $assetQuota,
                      u.configPasswordHash = $configHash,
                      u.configPasswordSalt = $configSalt
        {on_match}
        RETURN u.username AS username, u.displayName AS displayName, u.role AS role,
               u.createdAt AS createdAt, u.lastLoginAt AS lastLoginAt,
               u.videoQuota AS videoQuota, u.assetQuota AS assetQuota,
               0 AS projectCount
        """,
        {
            "username": username,
            "hash": password_hash,
            "salt": salt,
            "role": role,
            "displayName": display_name or username,
            "now": marker,
            # 내장 계정만 지문을 갖는다. 가입으로 만든 계정은 배포 설정이 관리하지 않으므로
            # null 로 두어 속성이 붙지 않게 한다(openCypher 는 null 대입을 속성 삭제로 다룬다).
            "configHash": password_hash if sync_password else None,
            "configSalt": salt if sync_password else None,
            **quota_params,
        },
    )
    if not rows:
        raise GraphError("사용자를 생성하지 못했습니다.")
    created = rows[0].get("createdAt") == marker
    if created:
        logger.info("User created username=%s role=%s", username, role)
    return _user_view(rows[0]), created


def create_user(username: str, password: str, role: str = ROLE_MEMBER, display_name: str | None = None) -> dict:
    user, created = merge_user(username, password, role=role, display_name=display_name)
    if not created:
        raise ConflictError("이미 존재하는 아이디입니다.")
    return user


def deduplicate_users() -> list[dict]:
    """Remove duplicate user vertices left by an earlier concurrent bootstrap.

    Grouping and selection happen in Python because Neptune's openCypher does not
    support head()/tail()/FOREACH over collected vertices. Only element ids and
    simple MATCH/DELETE/MERGE statements are used, which Neptune handles well.
    """
    rows = run("MATCH (u:user) RETURN id(u) AS vid, u.username AS username, u.createdAt AS createdAt")

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        username = row.get("username")
        if not username:
            continue
        grouped.setdefault(username, []).append(row)

    summary: list[dict] = []
    for username, vertices in grouped.items():
        if len(vertices) <= 1:
            continue
        # Keep the oldest vertex; createdAt is an ISO string so a plain sort works.
        vertices.sort(key=lambda item: (item.get("createdAt") or "", str(item.get("vid"))))
        keeper, extras = vertices[0], vertices[1:]

        for extra in extras:
            # Move any owned projects to the surviving vertex, then drop the duplicate.
            run(
                """
                MATCH (extra:user), (keeper:user)
                WHERE id(extra) = $extraId AND id(keeper) = $keeperId
                MATCH (extra)-[r:owns]->(p:project)
                DELETE r
                MERGE (keeper)-[:owns]->(p)
                """,
                {"extraId": extra["vid"], "keeperId": keeper["vid"]},
            )
            run(
                "MATCH (u:user) WHERE id(u) = $vid DETACH DELETE u",
                {"vid": extra["vid"]},
            )

        summary.append({"username": username, "removed": len(extras)})
        logger.warning("Removed %s duplicate vertices for user=%s", len(extras), username)
    return summary


def authenticate(username: str, password: str) -> dict | None:
    user = get_user(username)
    if not user or not user.get("passwordHash"):
        return None
    if not verify_password(password, user["passwordHash"], user.get("passwordSalt") or ""):
        return None
    run(
        "MATCH (u:user {username: $username}) SET u.lastLoginAt = $now",
        {"username": username, "now": _now()},
    )
    return _user_view(user)


def change_password(username: str, current_password: str, new_password: str) -> dict:
    """현재 비밀번호를 확인한 뒤 새 비밀번호로 바꾼다.

    로그인한 사용자가 자기 계정에만 쓴다. 관리자가 남의 비밀번호를 바꾸는 경로는 아니다.

    ## 왜 현재 비밀번호를 다시 받는가

    토큰만으로 변경을 허용하면, 자리를 비운 사이 열린 브라우저를 만진 사람이 계정을
    가져갈 수 있다. 토큰은 8시간 유효하고 로그아웃하지 않으면 그대로 살아 있다.

    ## configPassword 지문을 건드리지 않는 이유

    여기서 지문까지 새 비밀번호로 갱신하면, 다음 기동에 `merge_user` 가 "설정과 다르다"고
    판단해 tfvars 값으로 되돌린다. 지문은 **배포 설정이 마지막으로 반영된 값**을 가리켜야
    하므로 그대로 둔다. 그래서 이 변경은 파드 재시작을 넘어 유지되고, 그러면서도
    tfvars 를 실제로 바꾸면 그쪽이 다시 이긴다(비밀번호를 잊었을 때의 복구 경로).

    `authenticate` 를 재사용하지 않는다. 그쪽은 성공하면 lastLoginAt 을 갱신해서
    비밀번호 변경이 로그인 기록으로 남는다.
    """
    user = get_user(username)
    if not user:
        raise NotFoundError("대상 사용자를 찾을 수 없습니다.")

    stored_hash = user.get("passwordHash") or ""
    stored_salt = user.get("passwordSalt") or ""
    if not stored_hash or not verify_password(current_password, stored_hash, stored_salt):
        raise AuthError("현재 비밀번호가 올바르지 않습니다.")
    # 같은 값으로 바꾸면 사용자는 바뀐 줄 알지만 실제로는 아무것도 달라지지 않는다.
    if verify_password(new_password, stored_hash, stored_salt):
        raise ConflictError("현재 비밀번호와 다른 값을 입력해 주세요.")

    password_hash, salt = hash_password(new_password)
    changed_at = _now()
    rows = run(
        """
        MATCH (u:user {username: $username})
        SET u.passwordHash = $hash, u.passwordSalt = $salt, u.passwordUpdatedAt = $now
        RETURN u.username AS username, u.passwordUpdatedAt AS passwordUpdatedAt
        """,
        {"username": username, "hash": password_hash, "salt": salt, "now": changed_at},
    )
    if not rows:
        raise GraphError("비밀번호를 변경하지 못했습니다.")

    # 비밀번호는 로그에 남기지 않는다. 누가 언제 바꿨는지만 남긴다.
    logger.info("Password changed username=%s", username)
    return {"username": username, "passwordUpdatedAt": rows[0].get("passwordUpdatedAt") or changed_at}


def delete_user(username: str, delete_projects: bool) -> dict:
    """Remove a user. Optionally remove the projects they own.

    When delete_projects is False the projects are kept but detached from the
    owner, so shared content does not silently disappear.
    """
    user = get_user(username)
    if not user:
        raise NotFoundError("대상 사용자를 찾을 수 없습니다.")
    if (user.get("role") or ROLE_MEMBER) == ROLE_ADMIN:
        raise ConflictError("관리자 계정은 삭제할 수 없습니다.")

    owned = run(
        """
        MATCH (:user {username: $username})-[:owns]->(p:project)
        OPTIONAL MATCH (p)-[:has_document]->(d:document)
        RETURN p.id AS id, p.coverKey AS coverKey, p.visibility AS visibility,
               p.coverHistoryJson AS coverHistoryJson, p.sourceKey AS sourceKey,
               collect(d.s3Key) AS documentKeys
        """,
        {"username": username},
    )
    # 표지 이력, 원본 자료, 산출물까지 포함한 정리 대상 키를 호출자에게 넘긴다.
    for row in owned:
        row["coverKeys"] = [
            key
            for key in [row.get("coverKey"), row.get("sourceKey")]
            + list(row.get("documentKeys") or [])
            + [entry.get("key") for entry in _parse_cover_history(row.get("coverHistoryJson"))]
            if key
        ]

    if delete_projects:
        # 산출물 정점을 먼저 지운다. 프로젝트만 지우면 문서가 고아로 남는다.
        run(
            """
            MATCH (:user {username: $username})-[:owns]->(:project)-[:has_document]->(d:document)
            DETACH DELETE d
            """,
            {"username": username},
        )
        run(
            """
            MATCH (u:user {username: $username})-[:owns]->(p:project)
            DETACH DELETE p
            """,
            {"username": username},
        )
    else:
        run(
            """
            MATCH (u:user {username: $username})-[r:owns]->(p:project)
            SET p.orphanedFrom = $username, p.updatedAt = $now
            DELETE r
            """,
            {"username": username, "now": _now()},
        )

    # 사용량 기록과 롱테이크·콘티 작업은 사용자에 매달려 있어 함께 지운다. 남기면 고아 정점이 된다.
    # 콘티 이미지 파일은 다른 생성 이미지와 같은 취급으로 S3에 남긴다(라이브러리에서 개별 삭제).
    delete_usage_for_user(username)
    delete_credits_for_user(username)
    delete_sequences_for_user(username)
    delete_storyboards_for_user(username)
    run("MATCH (u:user {username: $username}) DETACH DELETE u", {"username": username})
    logger.info(
        "User deleted username=%s deleteProjects=%s affectedProjects=%s",
        username,
        delete_projects,
        len(owned),
    )
    return {
        "username": username,
        "deletedProjects": len(owned) if delete_projects else 0,
        "keptProjects": 0 if delete_projects else len(owned),
        "projects": owned,
    }


# --------------------------------------------------------------------------- #
# projects
# --------------------------------------------------------------------------- #
def _parse_cover_history(raw: str | None) -> list[dict]:
    """표지 이력은 JSON 문자열로 보관한다. Neptune openCypher가 리스트 속성을 지원하지 않는다."""
    if not raw:
        return []
    try:
        import json as _json

        parsed = _json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("표지 이력을 해석하지 못해 비워둡니다.")
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict) and entry.get("key")]


# 프로젝트 반환 필드는 여러 쿼리에서 동일해야 한다. 한 곳에서 관리해 누락을 막는다.
#
# 스토리 개편으로 네 필드가 늘었다(summaryStory, storyType, storyTextKey, storyTextCharCount,
# storyAssetsJson). 옛 프로젝트에는 이 속성이 없어 openCypher 가 null 을 준다 —
# _project_view 가 기본값으로 메운다. 마이그레이션 쿼리를 돌리지 않아도 되는 구조다.
_PROJECT_RETURN = """
    p.id AS id, p.title AS title, p.story AS story, p.visibility AS visibility,
    p.coverKey AS coverKey, p.coverSource AS coverSource,
    p.coverHistoryJson AS coverHistoryJson,
    p.inputType AS inputType, p.directionJson AS directionJson,
    p.sourceKey AS sourceKey, p.sourceName AS sourceName, p.sourceCharCount AS sourceCharCount,
    p.summaryStory AS summaryStory, p.storyType AS storyType,
    p.storyTextKey AS storyTextKey, p.storyTextCharCount AS storyTextCharCount,
    p.storyAssetsJson AS storyAssetsJson,
    p.productionType AS productionType,
    p.createdAt AS createdAt, p.updatedAt AS updatedAt
"""

# 프로젝트가 무엇을 만드는 곳인지. 제작은 프로젝트 안에서만 시작하므로 이 값이 어떤
# 작업 화면을 열지 정한다.
#
# **기본값을 넣지 않는다.** storyType 과 같은 이유다 — 빈 값은 "아직 고르지 않았다"는
# 뜻이고, 기본값을 넣으면 옛 프로젝트가 임의 유형으로 굳는다. 옛 프로젝트는 화면이
# 유형을 물어보고, 그때까지 두 작업 모두 열어 준다.
PRODUCTION_VIDEO = "video"
PRODUCTION_STORYBOARD = "storyboard"
PRODUCTION_TYPES = (PRODUCTION_VIDEO, PRODUCTION_STORYBOARD)

# --------------------------------------------------------------------------- #
# 스토리 개편 (로그라인 / 콘티 / 대본)
# --------------------------------------------------------------------------- #
# 원문 타입. story_context.STORY_TYPES 와 같은 값이어야 한다.
# **여기서 story_context 를 임포트하지 않는다** — graph_store 는 더 아래 계층이고
# 임포트하면 순환이 된다(image_brief 가 server 상수를 임포트하지 않는 것과 같은 이유).
STORY_TYPE_SCRIPT = "script"
STORY_TYPE_TREATMENT = "treatment"
STORY_TYPE_LOGLINE = "logline"
STORY_TYPES = (STORY_TYPE_SCRIPT, STORY_TYPE_TREATMENT, STORY_TYPE_LOGLINE)

# 결과물 종류
STORY_OUT_LOGLINE = "logline"
STORY_OUT_STORYBOARD = "storyboard"
STORY_OUT_SCRIPT = "script"
STORY_OUTPUT_KINDS = (STORY_OUT_LOGLINE, STORY_OUT_STORYBOARD, STORY_OUT_SCRIPT)

# 결과물 상태. needs_review 는 검수를 통과하지 못했지만 **저장된** 상태다.
STORY_OK = "ok"
STORY_NEEDS_REVIEW = "needs_review"
STORY_GENERATING = "generating"
STORY_FAILED = "failed"

# 대본 잡 상태
SCRIPT_PLANNING = "planning"      # 아웃라인 생성 중
SCRIPT_AWAITING = "awaiting"      # 아웃라인 완료, 사용자 승인 대기
SCRIPT_WRITING = "writing"        # 회차 생성 중
SCRIPT_READY = "ready"
SCRIPT_FAILED = "failed"
SCRIPT_ACTIVE_STATUSES = (SCRIPT_PLANNING, SCRIPT_WRITING)


def _parse_direction(raw: str | None) -> dict:
    """기획 방향 파라미터도 JSON 문자열로 보관하고 기본값과 병합해 돌려준다."""
    direction = dict(DIRECTION_DEFAULTS)
    if not raw:
        return direction
    try:
        import json as _json

        parsed = _json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("기획 방향을 해석하지 못해 기본값을 사용합니다.")
        return direction
    if isinstance(parsed, dict):
        for key in DIRECTION_DEFAULTS:
            if parsed.get(key) is not None:
                direction[key] = parsed[key]
    return direction


_DIRECTION_TEXT_LIMITS = {"targetPlatform": 40, "tone": 200, "audience": 100, "freeDirection": 2000}


def normalize_direction(payload: dict, base: dict | None = None) -> dict:
    """사용자 입력을 검증해 저장 가능한 형태로 만든다.

    payload에 없는 키는 base 값을 유지한다. 일부 항목만 보낸 요청이
    나머지 항목을 기본값으로 지워버리지 않게 하기 위한 것이다.
    빈 문자열은 '지움'이라는 의사 표시이므로 그대로 반영한다.
    """
    if not isinstance(payload, dict):
        raise GraphError("기획 방향 값이 올바르지 않습니다.")

    result = dict(DIRECTION_DEFAULTS)
    result.update({key: value for key, value in (base or {}).items() if key in DIRECTION_DEFAULTS})

    if payload.get("genres") is not None:
        genres = payload["genres"]
        if isinstance(genres, str):
            genres = genres.split(",")
        if not isinstance(genres, list):
            raise GraphError("장르는 목록이어야 합니다.")
        result["genres"] = [str(item).strip()[:40] for item in genres if str(item).strip()][:6]

    # 0은 falsy이지만 명시적으로 보낸 값이다. or로 기본값을 채우면 잘못된 값이 조용히 통과한다.
    if payload.get("episodeCount") is not None:
        try:
            episode_count = int(payload["episodeCount"])
        except (TypeError, ValueError):
            raise GraphError("에피소드 수는 숫자여야 합니다.")
        if not 1 <= episode_count <= 24:
            raise GraphError("에피소드 수는 1~24 사이여야 합니다.")
        result["episodeCount"] = episode_count

    for key, limit in _DIRECTION_TEXT_LIMITS.items():
        if payload.get(key) is not None:
            result[key] = str(payload[key]).strip()[:limit]

    # 플랫폼이 비면 프롬프트에 쓸 기준이 없어진다. 기본값으로 되돌린다.
    if not result["targetPlatform"]:
        result["targetPlatform"] = DIRECTION_DEFAULTS["targetPlatform"]
    return result


def _project_view(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "story": row.get("story") or "",
        "visibility": row.get("visibility") or VISIBILITY_PRIVATE,
        "coverKey": row.get("coverKey"),
        "coverSource": row.get("coverSource"),
        "coverHistory": _parse_cover_history(row.get("coverHistoryJson")),
        "inputType": row.get("inputType") or INPUT_WEBTOON,
        "direction": _parse_direction(row.get("directionJson")),
        "source": {
            "key": row.get("sourceKey"),
            "name": row.get("sourceName"),
            "charCount": int(row.get("sourceCharCount") or 0),
        },
        # --- 스토리 개편 ---
        # summaryStory 는 300자 이내라 인라인이다. 실제 스토리는 길이 제한이 없어 S3 에 둔다
        # (이 코드베이스의 방침: 짧은 텍스트는 인라인, 긴 본문은 S3 키만).
        "summaryStory": row.get("summaryStory") or "",
        # storyType 이 null 이면 아직 타입을 고르지 않은 프로젝트다. 화면이 첫 진입에
        # 타입 선택을 요구한다(지시서 4절 마이그레이션 규칙). 기본값을 넣지 않는 것이 중요하다 —
        # 넣으면 옛 프로젝트가 임의 타입으로 굳는다.
        "storyType": row.get("storyType") or "",
        # 빈 문자열이면 제작 유형 미선택(옛 프로젝트). 화면이 물어보고, 그때까지 두
        # 작업 화면을 모두 열어 준다. 기본값을 넣으면 옛 프로젝트가 임의 유형으로 굳는다.
        "productionType": row.get("productionType") or "",
        "storyText": {
            "key": row.get("storyTextKey") or "",
            "charCount": int(row.get("storyTextCharCount") or 0),
        },
        "storyAssets": _load_json_list(row.get("storyAssetsJson")),
        "owner": row.get("owner"),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def _load_json_list(raw) -> list:
    """JSON 문자열을 목록으로 읽는다. Neptune openCypher 가 리스트 속성을 지원하지 않아
    구조체는 전부 `*Json` 문자열로 보관한다(coverHistoryJson, cutsJson 과 같은 방식)."""
    if not raw:
        return []
    try:
        import json as _json

        value = _json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("JSON 목록 속성을 해석하지 못해 비워둡니다.")
        return []
    return value if isinstance(value, list) else []


def project_cover_keys(project: dict) -> list[str]:
    """현재 표지와 이력에 있는 모든 S3 키. 표지만 정리할 때 쓴다."""
    keys = [project.get("coverKey")] + [entry.get("key") for entry in project.get("coverHistory", [])]
    return [key for key in keys if key]


def project_storage_keys(project: dict) -> list[str]:
    """프로젝트가 S3에 만든 모든 객체 키. 프로젝트 삭제 시 정리 대상이다."""
    keys = project_cover_keys(project) + [(project.get("source") or {}).get("key")]
    return [key for key in keys if key]


def create_project(
    owner: str,
    title: str,
    story: str,
    visibility: str,
    input_type: str = INPUT_WEBTOON,
    direction: dict | None = None,
    production_type: str = "",
) -> dict:
    import json as _json

    if visibility not in {VISIBILITY_SHARED, VISIBILITY_PRIVATE}:
        raise GraphError("visibility는 shared 또는 private만 허용됩니다.")
    if input_type not in INPUT_TYPES:
        raise GraphError("진입점 타입이 올바르지 않습니다.")
    # 새 프로젝트는 유형을 고른다. 빈 값은 옛 프로젝트에만 남는다.
    if production_type and production_type not in PRODUCTION_TYPES:
        raise GraphError("제작 유형은 영상 만들기 또는 일반 콘티 만들기여야 합니다.")
    if not get_user(owner):
        raise NotFoundError("프로젝트 소유자를 찾을 수 없습니다.")

    project_id = uuid.uuid4().hex
    now = _now()
    direction_json = _json.dumps(
        normalize_direction(direction or {}), ensure_ascii=False, separators=(",", ":")
    )
    rows = run(
        """
        MATCH (u:user {username: $owner})
        CREATE (p:project {id: $id, title: $title, story: $story, visibility: $visibility,
                           inputType: $inputType, directionJson: $directionJson,
                           sourceCharCount: 0,
                           summaryStory: $summaryStory,
                           productionType: $productionType,
                           createdAt: $now, updatedAt: $now})
        CREATE (u)-[:owns]->(p)
        RETURN """
        + _PROJECT_RETURN
        + """, u.username AS owner
        """,
        {
            "owner": owner,
            "id": project_id,
            "title": title,
            "story": story,
            "visibility": visibility,
            "inputType": input_type,
            "directionJson": direction_json,
            # 만들기 모달의 입력칸이 '요약 스토리'로 바뀌었다(지시서 3절). 같은 글을
            # story 와 summaryStory 두 곳에 넣는다 — story 는 기존 표지 생성이 읽고,
            # summaryStory 는 스토리 구획과 컨텍스트 팩이 읽는다. 300자는 요약의 상한.
            "summaryStory": (story or "").strip()[:300],
            "productionType": production_type,
            "now": now,
        },
    )
    if not rows:
        raise GraphError("프로젝트를 생성하지 못했습니다.")
    logger.info(
        "Project created id=%s owner=%s visibility=%s inputType=%s",
        project_id,
        owner,
        visibility,
        input_type,
    )
    return _project_view(rows[0])


def list_visible_projects(username: str) -> dict:
    """Shared projects are visible to everyone; private ones only to the owner."""
    shared = run(
        """
        MATCH (p:project {visibility: $shared})
        OPTIONAL MATCH (u:user)-[:owns]->(p)
        RETURN """
        + _PROJECT_RETURN
        + """, u.username AS owner
        ORDER BY p.updatedAt DESC
        """,
        {"shared": VISIBILITY_SHARED},
    )
    private = run(
        """
        MATCH (u:user {username: $username})-[:owns]->(p:project {visibility: $private})
        RETURN """
        + _PROJECT_RETURN
        + """, u.username AS owner
        ORDER BY p.updatedAt DESC
        """,
        {"username": username, "private": VISIBILITY_PRIVATE},
    )
    return {
        "shared": [_project_view(row) for row in shared],
        "private": [_project_view(row) for row in private],
    }


def get_project(project_id: str) -> dict | None:
    rows = run(
        """
        MATCH (p:project {id: $id})
        OPTIONAL MATCH (u:user)-[:owns]->(p)
        RETURN """
        + _PROJECT_RETURN
        + """, u.username AS owner
        """,
        {"id": project_id},
    )
    return _project_view(rows[0]) if rows else None


def update_project(project_id: str, fields: dict) -> dict:
    allowed = {
        "title",
        "story",
        "visibility",
        "coverKey",
        "coverSource",
        "coverHistoryJson",
        "inputType",
        "directionJson",
        "sourceKey",
        "sourceName",
        "sourceCharCount",
        # --- 스토리 개편 ---
        # 이 목록에 빠지면 _filter_updates 가 조용히 걸러서 "변경할 항목이 없습니다" 가
        # 던져진다. 콘티 생성이 그 이유로 통째로 죽은 적이 있다(update_storyboard 주석 참고).
        "summaryStory",
        "storyType",
        "storyTextKey",
        "storyTextCharCount",
        "storyAssetsJson",
        "productionType",
    }
    updates = _filter_updates(fields, allowed, "프로젝트")
    if "visibility" in updates and updates["visibility"] not in {VISIBILITY_SHARED, VISIBILITY_PRIVATE}:
        raise GraphError("visibility는 shared 또는 private만 허용됩니다.")
    if "inputType" in updates and updates["inputType"] not in INPUT_TYPES:
        raise GraphError("진입점 타입이 올바르지 않습니다.")
    if "storyType" in updates and updates["storyType"] not in STORY_TYPES:
        raise GraphError("스토리 타입은 대본 / 트리트먼트 / 로그라인 중 하나여야 합니다.")
    if "productionType" in updates and updates["productionType"] not in PRODUCTION_TYPES:
        raise GraphError("제작 유형은 영상 만들기 또는 일반 콘티 만들기여야 합니다.")

    assignments = ", ".join(f"p.{key} = ${key}" for key in updates)
    parameters = {**updates, "id": project_id, "now": _now()}
    rows = run(
        f"""
        MATCH (p:project {{id: $id}})
        SET {assignments}, p.updatedAt = $now
        WITH p
        OPTIONAL MATCH (u:user)-[:owns]->(p)
        RETURN {_PROJECT_RETURN}, u.username AS owner
        """,
        parameters,
    )
    if not rows:
        raise NotFoundError("프로젝트를 찾을 수 없습니다.")
    return _project_view(rows[0])


def set_project_cover(project_id: str, cover_key: str, cover_source: str) -> tuple[dict, list[str]]:
    """새 표지를 지정하고 직전 표지를 이력 맨 앞으로 밀어 넣는다.

    이력은 COVER_HISTORY_LIMIT개까지만 남기고, 밀려난 항목의 S3 키를 함께 돌려준다.
    호출자가 그 키들을 실제로 삭제한다.
    """
    import json as _json

    project = get_project(project_id)
    if not project:
        raise NotFoundError("프로젝트를 찾을 수 없습니다.")

    history = list(project.get("coverHistory", []))
    previous_key = project.get("coverKey")
    if previous_key and previous_key != cover_key:
        history.insert(
            0,
            {
                "key": previous_key,
                "source": project.get("coverSource") or "unknown",
                "replacedAt": _now(),
            },
        )

    # 새로 지정한 키가 이력에 남아 있으면 중복이므로 제거한다(되돌리기 직후 재교체 대비).
    history = [entry for entry in history if entry.get("key") != cover_key]

    kept, evicted = history[:COVER_HISTORY_LIMIT], history[COVER_HISTORY_LIMIT:]
    updated = update_project(
        project_id,
        {
            "coverKey": cover_key,
            "coverSource": cover_source,
            "coverHistoryJson": _json.dumps(kept, ensure_ascii=False),
        },
    )
    evicted_keys = [entry["key"] for entry in evicted if entry.get("key")]
    if evicted_keys:
        logger.info(
            "Cover history trimmed project=%s kept=%s evicted=%s",
            project_id,
            len(kept),
            len(evicted_keys),
        )
    return updated, evicted_keys


def restore_project_cover(project_id: str, cover_key: str) -> dict:
    """이력에 있는 표지를 현재 표지로 되돌린다. 현재 표지는 이력으로 들어간다."""
    import json as _json

    project = get_project(project_id)
    if not project:
        raise NotFoundError("프로젝트를 찾을 수 없습니다.")

    history = list(project.get("coverHistory", []))
    target = next((entry for entry in history if entry.get("key") == cover_key), None)
    if not target:
        raise NotFoundError("이력에서 해당 표지를 찾을 수 없습니다.")

    remaining = [entry for entry in history if entry.get("key") != cover_key]
    current_key = project.get("coverKey")
    if current_key:
        remaining.insert(
            0,
            {
                "key": current_key,
                "source": project.get("coverSource") or "unknown",
                "replacedAt": _now(),
            },
        )

    # 되돌리기는 교체가 아니라 자리바꿈이므로 총 개수가 늘지 않는다. 삭제 대상도 없다.
    return update_project(
        project_id,
        {
            "coverKey": cover_key,
            "coverSource": target.get("source") or "restored",
            "coverHistoryJson": _json.dumps(remaining[:COVER_HISTORY_LIMIT], ensure_ascii=False),
        },
    )


def set_project_direction(project_id: str, direction: dict) -> dict:
    """기획자가 지시한 방향(타겟, 장르, 화수, 톤앤매너)을 저장한다."""
    import json as _json

    project = get_project(project_id)
    if not project:
        raise NotFoundError("프로젝트를 찾을 수 없습니다.")

    # 기존 값을 기준으로 병합한다. 일부 항목만 보낸 요청이 나머지를 지우지 않게 한다.
    normalized = normalize_direction(direction, base=project.get("direction"))
    return update_project(
        project_id,
        {"directionJson": _json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))},
    )


def set_project_source(project_id: str, source_key: str, source_name: str, char_count: int) -> tuple[dict, list[str]]:
    """원본 자료의 S3 위치를 기록한다.

    본문은 Neptune 속성 크기 제한을 넘길 수 있어 S3에 두고 키만 저장한다.
    교체 시 이전 자료는 보관 가치가 없으므로 삭제 대상 키로 돌려준다.
    """
    project = get_project(project_id)
    if not project:
        raise NotFoundError("프로젝트를 찾을 수 없습니다.")

    previous_key = (project.get("source") or {}).get("key")
    stale = [previous_key] if previous_key and previous_key != source_key else []

    updated = update_project(
        project_id,
        {
            "sourceKey": source_key,
            "sourceName": source_name,
            # 0은 update_project의 None 필터를 통과하지만 의미가 있으므로 최소 1로 보정하지 않는다.
            "sourceCharCount": int(char_count),
        },
    )
    return updated, stale


def set_project_input_type(project_id: str, input_type: str) -> dict:
    """진입점 타입을 바꾼다. 이후 생성 단계가 달라진다."""
    if input_type not in INPUT_TYPES:
        raise GraphError("진입점 타입이 올바르지 않습니다.")
    return update_project(project_id, {"inputType": input_type})


def delete_project(project_id: str) -> list[str]:
    """프로젝트를 삭제하고 정리해야 할 S3 키 목록을 돌려준다(표지 이력, 원본 자료, 산출물)."""
    project = get_project(project_id)
    if not project:
        raise NotFoundError("프로젝트를 찾을 수 없습니다.")
    storage_keys = (
        project_storage_keys(project)
        + document_keys(project_id)
        # 이 프로젝트로 만든 콘티 그림도 함께 정리한다. 프로젝트가 없어지면 볼 곳이 없다.
        + storyboard_keys_for_project(project_id)
    )
    # 산출물 정점을 먼저 지운다. 프로젝트만 지우면 문서가 고아로 남는다.
    run(
        """
        MATCH (:project {id: $id})-[:has_document]->(d:document)
        DETACH DELETE d
        """,
        {"id": project_id},
    )
    delete_storyboards_for_project(project_id)
    run("MATCH (p:project {id: $id}) DETACH DELETE p", {"id": project_id})
    logger.info("Project deleted id=%s storageKeys=%s", project_id, len(storage_keys))
    return storage_keys


# --------------------------------------------------------------------------- #
# documents (AI 산출물)
# --------------------------------------------------------------------------- #
_DOCUMENT_RETURN = """
    d.id AS id, d.kind AS kind, d.status AS status, d.error AS error,
    d.s3Key AS s3Key, d.charCount AS charCount, d.title AS title,
    d.model AS model, d.origin AS origin, d.version AS version,
    d.optionsJson AS optionsJson,
    d.createdBy AS createdBy, d.createdAt AS createdAt, d.updatedAt AS updatedAt
"""


def _document_view(row: dict) -> dict:
    import json as _json

    # 생성 시점에 고른 옵션. 화면이 마지막 선택을 다시 보여주고, 다시 생성할 때
    # 같은 값을 기본값으로 쓰기 위해 저장한다(콘티가 detail/cutCount를 두는 것과 같은 이유).
    try:
        options = _json.loads(row.get("optionsJson") or "{}")
    except (ValueError, TypeError):
        options = {}
    return {
        "id": row.get("id"),
        "kind": row.get("kind"),
        "status": row.get("status") or DOC_GENERATING,
        "error": row.get("error") or "",
        "s3Key": row.get("s3Key"),
        "charCount": int(row.get("charCount") or 0),
        "title": row.get("title") or "",
        "model": row.get("model") or "",
        "origin": row.get("origin") or "ai",
        "version": int(row.get("version") or 0),
        "options": options if isinstance(options, dict) else {},
        "createdBy": row.get("createdBy"),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def _document_key(project_id: str, kind: str) -> str:
    """프로젝트당 종류별로 하나만 둔다. 재생성은 같은 정점을 덮어쓴다."""
    return f"{project_id}:{kind}"


def begin_document(
    project_id: str, kind: str, model: str, actor: str, options: dict | None = None
) -> tuple[dict, str | None]:
    """생성 시작을 기록하고 (문서, 정리할 이전 S3 키)를 돌려준다."""
    if kind not in DOCUMENT_KINDS:
        raise GraphError("산출물 종류가 올바르지 않습니다.")
    if not get_project(project_id):
        raise NotFoundError("프로젝트를 찾을 수 없습니다.")

    previous = get_document(project_id, kind)
    stale_key = (previous or {}).get("s3Key")

    import json as _json

    now = _now()
    rows = run(
        """
        MERGE (d:document {docKey: $docKey})
        ON CREATE SET d.id = $id, d.kind = $kind, d.version = 0, d.createdAt = $now
        SET d.status = $status, d.model = $model, d.createdBy = $actor,
            d.origin = 'ai', d.error = '', d.updatedAt = $now,
            d.optionsJson = $optionsJson,
            d.version = d.version + 1
        WITH d
        MATCH (p:project {id: $projectId})
        MERGE (p)-[:has_document]->(d)
        RETURN """
        + _DOCUMENT_RETURN,
        {
            "docKey": _document_key(project_id, kind),
            "id": uuid.uuid4().hex,
            "kind": kind,
            "projectId": project_id,
            "status": DOC_GENERATING,
            "model": model,
            "actor": actor,
            "optionsJson": _json.dumps(options or {}, ensure_ascii=False),
            "now": now,
        },
    )
    if not rows:
        raise GraphError("산출물 생성을 시작하지 못했습니다.")
    logger.info("Document generation started project=%s kind=%s model=%s", project_id, kind, model)
    return _document_view(rows[0]), stale_key


def touch_document(project_id: str, kind: str) -> None:
    """생성이 진행 중임을 알린다. updatedAt만 새로 찍는다.

    **이것이 없으면 오래 걸리는 산출물이 진행 중에 실패로 바뀐다.**
    `reap_stale_document`는 `updatedAt`이 DOC_STALE_MINUTES(기본 10분)를 넘겼는지로
    판단하는데, 예전에는 그 값이 생성 시작 시점에 한 번만 찍혔다. 트리트먼트는 모델을
    여러 번(설계 1 + 본문 3 + 화별 시놉시스 N) 호출해서 10분을 넘길 수 있고, 그러면
    아직 쓰고 있는데도 조회 시점에 실패로 정리됐다.

    이 함수를 단계마다 부르면 stale 판정이 "시작한 지 오래됐다"가 아니라
    "10분 동안 진척이 없다"는 뜻이 된다.
    """
    try:
        run(
            """
            MATCH (d:document {docKey: $docKey})
            WHERE d.status = $status
            SET d.updatedAt = $now
            """,
            {
                "docKey": _document_key(project_id, kind),
                "status": DOC_GENERATING,
                "now": _now(),
            },
        )
    except GraphError:
        # 진행 신호는 실패해도 생성을 멈출 이유가 아니다. 다음 단계에서 다시 시도한다.
        logger.warning("Document touch failed project=%s kind=%s", project_id, kind)


def complete_document(
    project_id: str, kind: str, s3_key: str, char_count: int, title: str, origin: str = "ai"
) -> dict:
    rows = run(
        """
        MATCH (d:document {docKey: $docKey})
        SET d.status = $status, d.s3Key = $s3Key, d.charCount = $charCount,
            d.title = $title, d.origin = $origin, d.error = '', d.updatedAt = $now
        RETURN """
        + _DOCUMENT_RETURN,
        {
            "docKey": _document_key(project_id, kind),
            "status": DOC_READY,
            "s3Key": s3_key,
            "charCount": int(char_count),
            "title": title[:200],
            "origin": origin,
            "now": _now(),
        },
    )
    if not rows:
        raise NotFoundError("산출물을 찾을 수 없습니다.")
    logger.info("Document ready project=%s kind=%s chars=%s", project_id, kind, char_count)
    return _document_view(rows[0])


def fail_document(project_id: str, kind: str, message: str) -> dict | None:
    rows = run(
        """
        MATCH (d:document {docKey: $docKey})
        SET d.status = $status, d.error = $error, d.updatedAt = $now
        RETURN """
        + _DOCUMENT_RETURN,
        {
            "docKey": _document_key(project_id, kind),
            "status": DOC_FAILED,
            "error": str(message)[:600],
            "now": _now(),
        },
    )
    if not rows:
        return None
    logger.warning("Document failed project=%s kind=%s: %s", project_id, kind, message)
    return _document_view(rows[0])


def save_document(project_id: str, kind: str, s3_key: str, char_count: int, title: str, actor: str) -> dict:
    """사람이 직접 작성/수정한 내용을 저장한다."""
    if kind not in DOCUMENT_KINDS:
        raise GraphError("산출물 종류가 올바르지 않습니다.")
    if not get_project(project_id):
        raise NotFoundError("프로젝트를 찾을 수 없습니다.")

    now = _now()
    rows = run(
        """
        MERGE (d:document {docKey: $docKey})
        ON CREATE SET d.id = $id, d.kind = $kind, d.version = 0, d.createdAt = $now,
                      d.model = ''
        SET d.status = $status, d.s3Key = $s3Key, d.charCount = $charCount,
            d.title = $title, d.origin = 'manual', d.error = '',
            d.createdBy = $actor, d.updatedAt = $now, d.version = d.version + 1
        WITH d
        MATCH (p:project {id: $projectId})
        MERGE (p)-[:has_document]->(d)
        RETURN """
        + _DOCUMENT_RETURN,
        {
            "docKey": _document_key(project_id, kind),
            "id": uuid.uuid4().hex,
            "kind": kind,
            "projectId": project_id,
            "status": DOC_READY,
            "s3Key": s3_key,
            "charCount": int(char_count),
            "title": title[:200],
            "actor": actor,
            "now": now,
        },
    )
    if not rows:
        raise GraphError("산출물을 저장하지 못했습니다.")
    return _document_view(rows[0])


def get_document(project_id: str, kind: str) -> dict | None:
    rows = run(
        "MATCH (d:document {docKey: $docKey}) RETURN " + _DOCUMENT_RETURN,
        {"docKey": _document_key(project_id, kind)},
    )
    return _document_view(rows[0]) if rows else None


def list_documents(project_id: str) -> list[dict]:
    rows = run(
        """
        MATCH (:project {id: $projectId})-[:has_document]->(d:document)
        RETURN """
        + _DOCUMENT_RETURN
        + """
        ORDER BY d.updatedAt DESC
        """,
        {"projectId": project_id},
    )
    return [_document_view(row) for row in rows]


def document_keys(project_id: str) -> list[str]:
    """프로젝트의 산출물이 S3에 만든 키. 삭제 시 정리 대상이다."""
    rows = run(
        """
        MATCH (:project {id: $projectId})-[:has_document]->(d:document)
        RETURN d.s3Key AS s3Key
        """,
        {"projectId": project_id},
    )
    return [row["s3Key"] for row in rows if row.get("s3Key")]


# --------------------------------------------------------------------------- #
# sequences (롱테이크: 여러 컷을 이어 붙인 영상)
# --------------------------------------------------------------------------- #
SEQ_PLANNING = "planning"
SEQ_RENDERING = "rendering"
SEQ_STITCHING = "stitching"
SEQ_READY = "ready"
SEQ_FAILED = "failed"

_SEQUENCE_RETURN = """
    s.id AS id, s.owner AS owner, s.status AS status, s.error AS error,
    s.title AS title, s.prompt AS prompt, s.stylePrompt AS stylePrompt,
    s.targetSeconds AS targetSeconds, s.shotSeconds AS shotSeconds,
    s.shotCount AS shotCount, s.doneCount AS doneCount, s.failedCount AS failedCount,
    s.shotsJson AS shotsJson, s.outputKey AS outputKey, s.outputBucket AS outputBucket,
    s.modelId AS modelId, s.createdAt AS createdAt, s.updatedAt AS updatedAt
"""


def _sequence_view(row: dict) -> dict:
    import json as _json

    try:
        shots = _json.loads(row.get("shotsJson") or "[]")
    except (ValueError, TypeError):
        shots = []
    return {
        "id": row.get("id"),
        "owner": row.get("owner"),
        "status": row.get("status") or SEQ_PLANNING,
        "error": row.get("error") or "",
        "title": row.get("title") or "",
        "prompt": row.get("prompt") or "",
        "stylePrompt": row.get("stylePrompt") or "",
        "targetSeconds": int(row.get("targetSeconds") or 0),
        "shotSeconds": int(row.get("shotSeconds") or 0),
        "shotCount": int(row.get("shotCount") or 0),
        "doneCount": int(row.get("doneCount") or 0),
        "failedCount": int(row.get("failedCount") or 0),
        "shots": shots if isinstance(shots, list) else [],
        "outputKey": row.get("outputKey"),
        "outputBucket": row.get("outputBucket"),
        "modelId": row.get("modelId"),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def create_sequence(
    owner: str,
    title: str,
    prompt: str,
    style_prompt: str,
    target_seconds: int,
    shot_seconds: int,
    shot_count: int,
    model_id: str,
) -> dict:
    """롱테이크 작업을 만든다. 컷 목록은 계획이 끝난 뒤 채운다."""
    sequence_id = uuid.uuid4().hex
    now = _now()
    rows = run(
        """
        MATCH (u:user {username: $owner})
        CREATE (s:sequence {id: $id, owner: $owner, status: $status, error: '',
                            title: $title, prompt: $prompt, stylePrompt: $stylePrompt,
                            targetSeconds: $targetSeconds, shotSeconds: $shotSeconds,
                            shotCount: $shotCount, doneCount: 0, failedCount: 0,
                            shotsJson: '[]', modelId: $modelId,
                            createdAt: $now, updatedAt: $now})
        CREATE (u)-[:made]->(s)
        RETURN """
        + _SEQUENCE_RETURN,
        {
            "owner": owner,
            "id": sequence_id,
            "status": SEQ_PLANNING,
            "title": title[:200],
            "prompt": prompt[:4000],
            "stylePrompt": (style_prompt or "")[:1000],
            "targetSeconds": int(target_seconds),
            "shotSeconds": int(shot_seconds),
            "shotCount": int(shot_count),
            "modelId": model_id[:200],
            "now": now,
        },
    )
    if not rows:
        raise GraphError("롱테이크 작업을 만들지 못했습니다.")
    logger.info(
        "Sequence created id=%s owner=%s target=%ss shots=%s",
        sequence_id,
        owner,
        target_seconds,
        shot_count,
    )
    return _sequence_view(rows[0])


def update_sequence(sequence_id: str, fields: dict) -> dict:
    allowed = {
        "status",
        "error",
        "shotsJson",
        "shotCount",
        "doneCount",
        "failedCount",
        "outputKey",
        "outputBucket",
        "title",
    }
    updates = _filter_updates(fields, allowed, "롱테이크")
    assignments = ", ".join(f"s.{key} = ${key}" for key in updates)
    rows = run(
        f"""
        MATCH (s:sequence {{id: $id}})
        SET {assignments}, s.updatedAt = $now
        RETURN {_SEQUENCE_RETURN}
        """,
        {**updates, "id": sequence_id, "now": _now()},
    )
    if not rows:
        raise NotFoundError("롱테이크 작업을 찾을 수 없습니다.")
    return _sequence_view(rows[0])


def get_sequence(sequence_id: str) -> dict | None:
    rows = run(
        "MATCH (s:sequence {id: $id}) RETURN " + _SEQUENCE_RETURN, {"id": sequence_id}
    )
    return _sequence_view(rows[0]) if rows else None


def list_sequences(owner: str, limit: int = 20) -> list[dict]:
    rows = run(
        """
        MATCH (s:sequence {owner: $owner})
        RETURN """
        + _SEQUENCE_RETURN
        + """
        ORDER BY s.createdAt DESC
        LIMIT $limit
        """,
        {"owner": owner, "limit": int(limit)},
    )
    return [_sequence_view(row) for row in rows]


def delete_sequences_for_user(username: str) -> None:
    run("MATCH (s:sequence {owner: $username}) DETACH DELETE s", {"username": username})


# --------------------------------------------------------------------------- #
# storyboards (콘티 기획표: 컷마다 그림 + 설명)
# --------------------------------------------------------------------------- #
SB_PLANNING = "planning"   # 컷을 나누는 중
SB_DRAWING = "drawing"     # 컷 그림을 그리는 중
SB_READY = "ready"
SB_FAILED = "failed"

SB_ACTIVE_STATUSES = (SB_PLANNING, SB_DRAWING)

# 설명 상세도. 값이 프롬프트 분량과 표에 들어가는 열을 함께 결정한다.
SB_DETAIL_BRIEF = "brief"
SB_DETAIL_NORMAL = "normal"
SB_DETAIL_FULL = "full"
SB_DETAILS = (SB_DETAIL_BRIEF, SB_DETAIL_NORMAL, SB_DETAIL_FULL)

_STORYBOARD_RETURN = """
    b.id AS id, b.owner AS owner, b.projectId AS projectId, b.status AS status,
    b.error AS error, b.title AS title, b.prompt AS prompt, b.stylePrompt AS stylePrompt,
    b.detail AS detail, b.aspectRatio AS aspectRatio, b.cutCount AS cutCount,
    b.doneCount AS doneCount, b.failedCount AS failedCount, b.cutsJson AS cutsJson,
    b.presetId AS presetId, b.presetLabel AS presetLabel, b.styleReason AS styleReason,
    b.modelId AS modelId, b.modelLabel AS modelLabel, b.mode AS mode,
    b.sharedJson AS sharedJson, b.styleJson AS styleJson,
    b.createdAt AS createdAt, b.updatedAt AS updatedAt
"""


SB_MODE_AUTO = "auto"      # AI가 이야기를 컷으로 나눈다
SB_MODE_MANUAL = "manual"  # 기획자가 컷을 직접 적는다
SB_MODES = (SB_MODE_AUTO, SB_MODE_MANUAL)


def _storyboard_view(row: dict) -> dict:
    import json as _json

    def _load(raw, fallback):
        try:
            value = _json.loads(raw or "")
        except (ValueError, TypeError):
            return fallback
        return value if isinstance(value, type(fallback)) else fallback

    cuts = _load(row.get("cutsJson"), [])
    return {
        # 컷을 개별로 다시 그릴 때 처음과 같은 그림체·인물로 그려야 한다.
        # 판단 결과를 저장해 두지 않으면 다시 그릴 때마다 화풍이 달라진다.
        "shared": _load(row.get("sharedJson"), {}),
        "style": _load(row.get("styleJson"), {}),
        "mode": row.get("mode") or SB_MODE_AUTO,
        "id": row.get("id"),
        "owner": row.get("owner"),
        "projectId": row.get("projectId") or "",
        "status": row.get("status") or SB_PLANNING,
        "error": row.get("error") or "",
        "title": row.get("title") or "",
        "prompt": row.get("prompt") or "",
        "stylePrompt": row.get("stylePrompt") or "",
        "detail": row.get("detail") or SB_DETAIL_NORMAL,
        "aspectRatio": row.get("aspectRatio") or "16:9",
        "cutCount": int(row.get("cutCount") or 0),
        "doneCount": int(row.get("doneCount") or 0),
        "failedCount": int(row.get("failedCount") or 0),
        "cuts": cuts if isinstance(cuts, list) else [],
        "presetId": row.get("presetId") or "",
        "presetLabel": row.get("presetLabel") or "",
        "styleReason": row.get("styleReason") or "",
        "modelId": row.get("modelId") or "",
        "modelLabel": row.get("modelLabel") or "",
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def create_storyboard(
    owner: str,
    title: str,
    prompt: str,
    style_prompt: str,
    detail: str,
    aspect_ratio: str,
    cut_count: int,
    project_id: str = "",
    mode: str = SB_MODE_AUTO,
) -> dict:
    """콘티 작업을 만든다. 컷 목록은 기획이 끝난 뒤 채운다."""
    storyboard_id = uuid.uuid4().hex
    now = _now()
    rows = run(
        """
        MATCH (u:user {username: $owner})
        CREATE (b:storyboard {id: $id, owner: $owner, projectId: $projectId,
                              status: $status, error: '', title: $title, prompt: $prompt,
                              stylePrompt: $stylePrompt, detail: $detail,
                              aspectRatio: $aspectRatio, cutCount: $cutCount,
                              doneCount: 0, failedCount: 0, cutsJson: '[]',
                              presetId: '', presetLabel: '', styleReason: '',
                              modelId: '', modelLabel: '', mode: $mode,
                              sharedJson: '{}', styleJson: '{}',
                              createdAt: $now, updatedAt: $now})
        CREATE (u)-[:made]->(b)
        RETURN """
        + _STORYBOARD_RETURN,
        {
            "owner": owner,
            "id": storyboard_id,
            "projectId": (project_id or "")[:64],
            "status": SB_PLANNING,
            "title": title[:200],
            "prompt": prompt[:8000],
            "stylePrompt": (style_prompt or "")[:1000],
            "detail": detail,
            "aspectRatio": aspect_ratio[:12],
            "cutCount": int(cut_count),
            "mode": mode,
            "now": now,
        },
    )
    if not rows:
        raise GraphError("콘티 작업을 만들지 못했습니다.")
    logger.info(
        "Storyboard created id=%s owner=%s cuts=%s detail=%s project=%s",
        storyboard_id,
        owner,
        cut_count,
        detail,
        project_id or "-",
    )
    return _storyboard_view(rows[0])


def update_storyboard(storyboard_id: str, fields: dict) -> dict:
    allowed = {
        "status",
        "error",
        "title",
        # prompt / stylePrompt 는 create_storyboard 가 만드는 속성인데 이 목록에 빠져 있었다.
        # run_storyboard 0단계가 프롬프트 보강 결과를 이 두 키로 저장하려 했고, 여기서
        # 조용히 걸러진 뒤 "변경할 항목이 없습니다" 가 던져져 콘티가 시작도 못 하고 실패했다.
        # 보강은 거의 항상 문장을 바꾸므로 사실상 모든 콘티 생성이 이 지점에서 죽었다.
        "prompt",
        "stylePrompt",
        "cutsJson",
        "cutCount",
        "doneCount",
        "failedCount",
        "presetId",
        "presetLabel",
        "styleReason",
        "modelId",
        "modelLabel",
        "sharedJson",
        "styleJson",
    }
    updates = _filter_updates(fields, allowed, "콘티")
    assignments = ", ".join(f"b.{key} = ${key}" for key in updates)
    rows = run(
        f"""
        MATCH (b:storyboard {{id: $id}})
        SET {assignments}, b.updatedAt = $now
        RETURN {_STORYBOARD_RETURN}
        """,
        {**updates, "id": storyboard_id, "now": _now()},
    )
    if not rows:
        raise NotFoundError("콘티 작업을 찾을 수 없습니다.")
    return _storyboard_view(rows[0])


def get_storyboard(storyboard_id: str) -> dict | None:
    rows = run(
        "MATCH (b:storyboard {id: $id}) RETURN " + _STORYBOARD_RETURN, {"id": storyboard_id}
    )
    return _storyboard_view(rows[0]) if rows else None


def list_storyboards(owner: str, project_id: str = "", limit: int = 20) -> list[dict]:
    """콘티 목록. project_id를 주면 그 프로젝트 것만 돌려준다."""
    if project_id:
        query = """
            MATCH (b:storyboard {owner: $owner, projectId: $projectId})
            RETURN """ + _STORYBOARD_RETURN + """
            ORDER BY b.createdAt DESC
            LIMIT $limit
        """
        params = {"owner": owner, "projectId": project_id, "limit": int(limit)}
    else:
        query = """
            MATCH (b:storyboard {owner: $owner})
            RETURN """ + _STORYBOARD_RETURN + """
            ORDER BY b.createdAt DESC
            LIMIT $limit
        """
        params = {"owner": owner, "limit": int(limit)}
    return [_storyboard_view(row) for row in run(query, params)]


def delete_storyboard(storyboard_id: str) -> list[str]:
    """콘티를 지우고 정리해야 할 S3 키를 돌려준다."""
    board = get_storyboard(storyboard_id)
    if not board:
        raise NotFoundError("콘티 작업을 찾을 수 없습니다.")
    keys = [cut.get("imageKey") for cut in board["cuts"] if cut.get("imageKey")]
    run("MATCH (b:storyboard {id: $id}) DETACH DELETE b", {"id": storyboard_id})
    logger.info("Storyboard deleted id=%s images=%s", storyboard_id, len(keys))
    return keys


def storyboard_keys_for_user(username: str) -> list[str]:
    """사용자 삭제 시 함께 지울 콘티 이미지 키."""
    keys: list[str] = []
    for board in list_storyboards(username, limit=500):
        keys.extend(cut.get("imageKey") for cut in board["cuts"] if cut.get("imageKey"))
    return keys


def delete_storyboards_for_user(username: str) -> None:
    run("MATCH (b:storyboard {owner: $username}) DETACH DELETE b", {"username": username})


def storyboard_keys_for_project(project_id: str) -> list[str]:
    """프로젝트 삭제 시 함께 지울 콘티 이미지 키."""
    rows = run(
        "MATCH (b:storyboard {projectId: $projectId}) RETURN " + _STORYBOARD_RETURN,
        {"projectId": project_id},
    )
    keys: list[str] = []
    for row in rows:
        board = _storyboard_view(row)
        keys.extend(cut.get("imageKey") for cut in board["cuts"] if cut.get("imageKey"))
    return keys


def delete_storyboards_for_project(project_id: str) -> None:
    run("MATCH (b:storyboard {projectId: $projectId}) DETACH DELETE b", {"projectId": project_id})


# --------------------------------------------------------------------------- #
# usage (모델 사용량)
# --------------------------------------------------------------------------- #
def record_usage(
    username: str,
    model_id: str,
    kind: str,
    purpose: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    units: float = 0.0,
    region: str = "",
    ok: bool = True,
) -> None:
    """모델 호출 한 건을 기록한다.

    호출 흐름을 막지 않는 것이 원칙이다. 기록이 실패해도 예외를 올리지 않고 경고만 남긴다.
    사용자와 연결해 두어야 사용자별 집계를 할 수 있다.
    """
    try:
        run(
            """
            MATCH (u:user {username: $username})
            CREATE (x:usage {id: $id, modelId: $modelId, kind: $kind, purpose: $purpose,
                             inputTokens: $inputTokens, outputTokens: $outputTokens,
                             units: $units, region: $region, ok: $ok, at: $at})
            CREATE (u)-[:used]->(x)
            """,
            {
                "username": username,
                "id": uuid.uuid4().hex,
                "modelId": model_id[:200],
                "kind": kind[:20],
                "purpose": purpose[:60],
                "inputTokens": int(input_tokens or 0),
                "outputTokens": int(output_tokens or 0),
                "units": float(units or 0.0),
                "region": region[:20],
                "ok": bool(ok),
                "at": _now(),
            },
        )
    except (GraphError, GraphUnavailable) as error:
        logger.warning("사용량 기록에 실패했습니다 (%s/%s): %s", username, model_id, error)


def usage_rows(since_iso: str | None = None, limit: int = 20000) -> list[dict]:
    """집계 원본 행을 돌려준다.

    합계를 Neptune에서 계산하지 않고 파이썬에서 묶는다. openCypher 집계 함수의
    지원 범위가 버전에 따라 달라 조용히 틀린 값이 나오는 것보다 안전하다.
    PoC 규모에서는 행 수가 적어 비용도 문제되지 않는다.
    """
    query = """
        MATCH (u:user)-[:used]->(x:usage)
        WHERE $since = '' OR x.at >= $since
        RETURN u.username AS username, x.modelId AS modelId, x.kind AS kind,
               x.purpose AS purpose, x.inputTokens AS inputTokens,
               x.outputTokens AS outputTokens, x.units AS units, x.ok AS ok, x.at AS at
        ORDER BY x.at DESC
        LIMIT $limit
    """
    rows = run(query, {"since": since_iso or "", "limit": int(limit)})
    return [
        {
            "username": row.get("username"),
            "modelId": row.get("modelId"),
            "kind": row.get("kind"),
            "purpose": row.get("purpose"),
            "inputTokens": int(row.get("inputTokens") or 0),
            "outputTokens": int(row.get("outputTokens") or 0),
            "units": float(row.get("units") or 0.0),
            "ok": bool(row.get("ok")),
            "at": row.get("at"),
        }
        for row in rows
    ]


def delete_usage_for_user(username: str) -> None:
    """사용자 삭제 시 사용량 기록도 함께 지운다."""
    run(
        "MATCH (:user {username: $username})-[:used]->(x:usage) DETACH DELETE x",
        {"username": username},
    )


# --------------------------------------------------------------------------- #
# credit (결과물 생성 한도 차감)
# --------------------------------------------------------------------------- #
def spend_credit(username: str, category: str, action: str, ref_id: str = "") -> None:
    """결과물 한 건을 기록한다. **성공한 결과물에만 부른다.**

    usage 와 따로 두는 이유가 두 가지다.

    1. 세는 단위가 다르다. usage 는 Bedrock 호출 한 건이고 이것은 사용자가 받은 결과물
       한 건이다. 콘티 하나가 호출 10회 이상이라 usage 로 세면 한도가 의미를 잃는다.
    2. usage 에 남지 않는 결과물이 있다. NEW_영상 제작은 자체 GPU 라 Bedrock 을 거치지
       않아 usage 에 영상 기록이 아예 없다. usage 로 한도를 걸면 그 기능만 무제한이 된다.

    기록 실패가 생성 실패로 번지지 않게 예외를 삼킨다. 다만 usage 와 달리 이쪽은
    삼키면 한도가 덜 차감되므로, 경고를 남겨 나중에 추적할 수 있게 한다.
    """
    if not username or category not in QUOTA_CATEGORIES:
        return

    reference = (ref_id or "")[:120]
    parameters = {
        "username": username,
        "id": uuid.uuid4().hex,
        "category": category,
        "action": action[:60],
        "refId": reference,
        "at": _now(),
    }

    # refId 가 있으면 **그 결과물에 대해 한 번만** 차감한다.
    #
    # 단일 영상은 화면이 상태를 폴링하고, 완료를 확인한 자리에서 차감한다. 폴링은 완료
    # 뒤에도 몇 번 더 오고, 사용자가 새로고침하면 또 온다. CREATE 로 두면 그때마다
    # 한도가 줄어 10건 한도가 서너 건에서 소진된다. refId(비동기 ARN, S3 키, 작업 id)로
    # MERGE 하면 같은 결과물은 몇 번을 확인해도 1건이다.
    #
    # refId 가 없는 호출은 CREATE 로 남긴다. 그런 경로가 남아 있으면 중복 차감이 가능하니
    # 호출부는 되도록 결과물을 식별하는 값을 넘긴다.
    if reference:
        query = """
            MATCH (u:user {username: $username})
            MERGE (u)-[:spent]->(c:credit {refId: $refId})
            ON CREATE SET c.id = $id, c.category = $category, c.action = $action, c.at = $at
        """
    else:
        query = """
            MATCH (u:user {username: $username})
            CREATE (c:credit {id: $id, category: $category, action: $action,
                              refId: $refId, at: $at})
            CREATE (u)-[:spent]->(c)
        """

    try:
        run(query, parameters)
    except GraphError as error:
        # GraphUnavailable 도 하위 클래스라 함께 잡힌다.
        logger.warning("한도 차감 기록에 실패했습니다 (%s/%s): %s", username, category, error)


def credit_counts(username: str) -> dict:
    """카테고리별로 지금까지 쓴 결과물 건수.

    집계는 파이썬에서 한다. usage_rows 와 같은 이유다(openCypher 집계 지원 범위가
    버전에 따라 달라 조용히 틀린 값이 나오는 것이 가장 나쁘다). 한도가 걸린 계정의
    행 수는 한도 합계를 넘지 않아 양이 문제되지 않는다.
    """
    counts = {category: 0 for category in QUOTA_CATEGORIES}
    if not username:
        return counts
    rows = run(
        """
        MATCH (:user {username: $username})-[:spent]->(c:credit)
        RETURN c.category AS category
        """,
        {"username": username},
    )
    for row in rows:
        category = row.get("category")
        if category in counts:
            counts[category] += 1
    return counts


def credit_actions(username: str, limit: int = 200) -> list[dict]:
    """무엇에 몇 건을 썼는지. 최근 것이 앞에 온다.

    숫자만 보여 주면 "왜 벌써 다 썼지"에 답할 수 없다. 어떤 기능이 얼마나 먹었는지
    보이면 한도를 올릴지, 특정 기능을 그만 쓰게 할지 판단할 수 있다.
    """
    if not username:
        return []
    rows = run(
        """
        MATCH (:user {username: $username})-[:spent]->(c:credit)
        RETURN c.category AS category, c.action AS action, c.refId AS refId, c.at AS at
        ORDER BY c.at DESC
        LIMIT $limit
        """,
        {"username": username, "limit": int(limit)},
    )
    return [
        {
            "category": row.get("category"),
            "action": row.get("action"),
            "refId": row.get("refId"),
            "at": row.get("at"),
        }
        for row in rows
    ]


def quota_status(username: str) -> dict:
    """이 사용자의 한도와 사용량. 한도가 없으면 limited=False.

    화면과 한도 검사가 같은 함수를 쓰게 해서, 화면이 "3회 남음"이라고 적었는데 서버가
    거절하는 상황이 생기지 않게 한다.
    """
    user = get_user(username)
    if not user:
        return {"limited": False, "categories": {}}

    quotas = {category: _quota_value(user.get(f"{category}Quota")) for category in QUOTA_CATEGORIES}
    limited = any(limit is not None for limit in quotas.values())
    if not limited:
        return {"limited": False, "categories": {}}

    used = credit_counts(username)
    categories = {}
    for category in QUOTA_CATEGORIES:
        limit = quotas[category]
        spent = used.get(category, 0)
        categories[category] = {
            "limit": limit,
            "used": spent,
            # 한도가 없는 카테고리는 남은 수를 말할 수 없다. None 으로 두고 화면이 "무제한"으로 적는다.
            "remaining": None if limit is None else max(0, limit - spent),
        }
    return {"limited": True, "categories": categories}


def quota_remaining(username: str, category: str) -> int | None:
    """남은 건수. None 이면 무제한이라 막지 않는다."""
    status = quota_status(username)
    if not status.get("limited"):
        return None
    entry = (status.get("categories") or {}).get(category)
    if not entry or entry.get("limit") is None:
        return None
    return int(entry.get("remaining") or 0)


def delete_credits_for_user(username: str) -> None:
    """사용자 삭제 시 한도 기록도 함께 지운다. 남기면 고아 정점이 된다."""
    run(
        "MATCH (:user {username: $username})-[:spent]->(c:credit) DETACH DELETE c",
        {"username": username},
    )


def set_user_quotas(username: str, quotas: dict) -> dict:
    """관리자가 한도를 바꾼다. 여기가 설정보다 우선하는 최종 값이다.

    `quotas` 에 담긴 카테고리만 바꾼다. 값이 None 이면 그 카테고리의 한도를 **없앤다**
    (무제한). 보내지 않은 카테고리는 건드리지 않는다. 화면이 한 항목만 고칠 때
    나머지를 함께 보내야 하는 구조면, 보내는 것을 빠뜨렸을 때 조용히 무제한이 된다.
    """
    user = get_user(username)
    if not user:
        raise NotFoundError("대상 사용자를 찾을 수 없습니다.")

    updates = []
    parameters: dict = {"username": username}
    for category in QUOTA_CATEGORIES:
        if category not in quotas:
            continue
        field = f"{category}Quota"
        updates.append(f"u.{field} = ${field}")
        # None 을 SET 하면 openCypher 가 속성을 지운다. 그것이 무제한을 뜻한다.
        parameters[field] = _quota_value(quotas.get(category))
    if not updates:
        raise GraphError("변경할 한도가 없습니다. video 또는 asset 중 하나를 보내 주세요.")

    run(
        f"MATCH (u:user {{username: $username}}) SET {', '.join(updates)}",
        parameters,
    )
    logger.info("User quotas updated username=%s %s", username, quotas)
    return quota_status(username)


def reset_credits(username: str) -> int:
    """한도를 초기화한다. 지운 건수를 돌려준다.

    계정을 다시 쓰게 하려면 한도를 되돌릴 방법이 있어야 한다. 이것이 없으면 다 쓴 계정은
    삭제하고 다시 만드는 수밖에 없고, 그러면 그 사용자의 프로젝트까지 함께 정리된다.
    """
    before = credit_counts(username)
    delete_credits_for_user(username)
    return sum(before.values())


def delete_document(project_id: str, kind: str) -> str | None:
    document = get_document(project_id, kind)
    if not document:
        raise NotFoundError("산출물을 찾을 수 없습니다.")
    run(
        "MATCH (d:document {docKey: $docKey}) DETACH DELETE d",
        {"docKey": _document_key(project_id, kind)},
    )
    logger.info("Document deleted project=%s kind=%s", project_id, kind)
    return document.get("s3Key")


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #
def bootstrap(admin_username: str, admin_password: str, seed_users: list[dict], company_code: str) -> dict:
    """Create the default admin, seed users and company code when missing.

    호출을 반복해도 안전하다. 프로젝트나 산출물은 건드리지 않는다.

    **내장 계정의 비밀번호는 설정이 바뀌었을 때만 설정값으로 맞춘다.** admin은 k8s Secret
    `toon-change-ai-pj-auth`의 `password`, 시드 사용자는 `SEED_USERS`가 단일 원본이다.
    예전에는 `ON CREATE SET`만 써서 계정이 이미 있으면 설정을 바꿔도 비밀번호가 그대로였고,
    약한 초기 비밀번호를 교체할 방법이 아예 없었다.

    그 뒤 기동마다 무조건 덮어쓰도록 바꿨더니 사용자가 스스로 비밀번호를 바꿀 수 없게 됐다
    (재시작하면 되돌아갔다). 지금은 설정값의 지문을 정점에 남겨 두고 **설정이 실제로
    바뀐 경우에만** 덮어쓴다. 판단 규칙은 `_config_password_rotated`,
    배경은 `merge_user` docstring 에 있다.
    """
    global _bootstrap_done
    with _bootstrap_lock:
        created: list[str] = []
        # 비밀번호가 비어 있으면 동기화하지 않는다. Secret 주입이 실패한 환경에서
        # 관리자 비밀번호를 빈 값으로 덮어써 잠기는 것을 막는다.
        _, admin_created = merge_user(
            admin_username,
            admin_password,
            role=ROLE_ADMIN,
            display_name="Administrator",
            sync_password=bool(admin_password),
        )
        if admin_created:
            created.append(admin_username)

        for entry in seed_users:
            name = entry.get("username")
            if not name:
                continue
            # 한도도 설정이 단일 원본이라 매 기동에 맞춘다(sync_quota=True).
            # 설정에서 한도를 빼면 속성이 지워져 무제한으로 돌아간다.
            _, was_created = merge_user(
                name,
                entry.get("password") or name,
                role=ROLE_MEMBER,
                display_name=name,
                sync_password=True,
                quotas=entry.get("quotas"),
                sync_quota=True,
            )
            if was_created:
                created.append(name)

        # Clean up anything an earlier non-idempotent bootstrap may have duplicated.
        deduplicate_users()

        if get_company_code() is None:
            set_company_code(company_code, actor=admin_username)

        _bootstrap_done = True
        if created:
            logger.info("Bootstrap created users: %s", ", ".join(created))
        return {"createdUsers": created, "companyCode": get_company_code()}


def bootstrap_if_needed(admin_username: str, admin_password: str, seed_users: list[dict], company_code: str) -> None:
    if _bootstrap_done:
        return
    try:
        bootstrap(admin_username, admin_password, seed_users, company_code)
    except GraphError as error:
        # Never block start-up on the graph being briefly unavailable.
        logger.warning("Bootstrap deferred: %s", error)


def wait_until_ready(attempts: int = 5, delay_seconds: float = 2.0) -> bool:
    for attempt in range(attempts):
        if health().get("reachable"):
            return True
        if attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


# --------------------------------------------------------------------------- #
# wanjobs (NEW_영상 제작: 자체 GPU의 Wan 2.2 I2V)
# --------------------------------------------------------------------------- #
# 기존 영상(Bedrock 비동기 호출)과 정점을 따로 둔다. 지시서 1-2가 코드·라우팅을 섞지 말라고
# 정했고, 실제로 저장해야 하는 값도 다르다. Bedrock 경로는 invocationArn과 출력 버킷을
# Bedrock이 관리하지만, Wan은 추론 서버가 job을 메모리에만 들고 있어서
# **앱이 진행 상황과 결과 위치를 직접 기록해야 한다.**
#
# 추론 서버가 재시작되면 그 job은 사라진다. 그때 사용자가 영원히 기다리지 않도록
# 앱 쪽 기록을 근거로 실패 처리한다(server.reap_stale_wan_job).
WAN_QUEUED = "queued"
WAN_RUNNING = "running"
WAN_UPLOADING = "uploading"
WAN_READY = "ready"
WAN_FAILED = "failed"
WAN_ACTIVE_STATUSES = (WAN_QUEUED, WAN_RUNNING, WAN_UPLOADING)

_WANJOB_RETURN = """
    w.id AS id, w.owner AS owner, w.projectId AS projectId, w.remoteJobId AS remoteJobId,
    w.status AS status, w.error AS error, w.title AS title,
    w.userText AS userText, w.prompt AS prompt, w.stylePrompt AS stylePrompt,
    w.resolution AS resolution, w.seconds AS seconds, w.frames AS frames,
    w.cameraMove AS cameraMove, w.sourceKey AS sourceKey, w.sourceSha256 AS sourceSha256,
    w.sourceKeysJson AS sourceKeysJson, w.sourceNamesJson AS sourceNamesJson,
    w.answersJson AS answersJson,
    w.outputKey AS outputKey, w.outputBytes AS outputBytes,
    w.elapsedSeconds AS elapsedSeconds, w.queuePosition AS queuePosition,
    w.presetId AS presetId, w.presetLabel AS presetLabel,
    w.boostJson AS boostJson, w.profileJson AS profileJson,
    w.createdAt AS createdAt, w.updatedAt AS updatedAt
"""


def _load_list(raw) -> list:
    """JSON 문자열을 목록으로 읽는다. 깨졌거나 비었으면 빈 목록."""
    import json as _json

    try:
        value = _json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _wanjob_view(row: dict) -> dict:
    import json as _json

    try:
        boost = _json.loads(row.get("boostJson") or "{}")
    except (ValueError, TypeError):
        boost = {}
    # 스테이지 프로파일. 추론 서버가 준 값을 그대로 담는다.
    # 예전 작업에는 이 속성이 없어 None 이 온다(openCypher 는 없는 속성을 null 로 준다).
    try:
        profile = _json.loads(row.get("profileJson") or "{}")
    except (ValueError, TypeError):
        profile = {}
    return {
        "id": row.get("id"),
        "owner": row.get("owner"),
        "projectId": row.get("projectId") or "",
        "remoteJobId": row.get("remoteJobId") or "",
        "status": row.get("status") or WAN_QUEUED,
        "error": row.get("error") or "",
        "title": row.get("title") or "",
        "userText": row.get("userText") or "",
        "prompt": row.get("prompt") or "",
        "stylePrompt": row.get("stylePrompt") or "",
        "resolution": row.get("resolution") or "480p",
        "seconds": int(row.get("seconds") or 0),
        "frames": int(row.get("frames") or 0),
        "cameraMove": row.get("cameraMove") or "",
        "sourceKey": row.get("sourceKey") or "",
        # 예전 작업에는 이 속성이 없어 None 이 온다(openCypher 는 없는 속성을 null 로 준다).
        "sourceSha256": row.get("sourceSha256") or "",
        # 참고 이미지 여러 장과 기획자 답변. 결과를 다시 볼 때 무엇으로 만들었는지 알아야 한다.
        "sourceKeys": _load_list(row.get("sourceKeysJson")),
        "sourceNames": _load_list(row.get("sourceNamesJson")),
        "answers": _load_list(row.get("answersJson")),
        "outputKey": row.get("outputKey") or "",
        "outputBytes": int(row.get("outputBytes") or 0),
        "elapsedSeconds": float(row.get("elapsedSeconds") or 0),
        "queuePosition": int(row.get("queuePosition") or 0),
        "presetId": row.get("presetId") or "",
        "presetLabel": row.get("presetLabel") or "",
        "boost": boost if isinstance(boost, dict) else {},
        # 스테이지별 시간·피크 VRAM. 기준선 측정과 A/B 비교의 근거다.
        # **추론 서버 메모리가 아니라 여기에 남긴다.** 파드가 재시작되면 그쪽은 사라지고,
        # 기준선은 며칠에 걸쳐 여러 번 재서 비교하는 값이다.
        "profile": profile if isinstance(profile, dict) else {},
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def create_wanjob(
    owner: str,
    *,
    title: str,
    user_text: str,
    prompt: str,
    style_prompt: str,
    resolution: str,
    seconds: int,
    frames: int,
    camera_move: str,
    source_key: str,
    # 첫 프레임의 sha256. 전처리를 바꾼 전후를 구분하려면 입력 신원이 필요하다 (F-23).
    # 키만으로는 부족하다 — 같은 키의 객체를 덮어쓰면 키는 같고 내용은 다르다.
    source_sha256: str = "",
    source_keys: list[str] | None = None,
    source_names: list[str] | None = None,
    answers: list[dict] | None = None,
    project_id: str = "",
    preset_id: str = "",
    preset_label: str = "",
    boost_json: str = "{}",
) -> dict:
    """Wan 영상 작업을 기록한다. 추론 서버에 제출하기 전에 만든다.

    제출 전에 만드는 이유: 제출이 성공했는데 기록이 실패하면 GPU가 15분을 쓰고도
    결과를 어디에도 남기지 못한다. 기록을 먼저 만들고 remoteJobId를 나중에 붙인다.
    """
    import json as _json

    job_id = uuid.uuid4().hex
    now = _now()
    rows = run(
        """
        MATCH (u:user {username: $owner})
        CREATE (w:wanjob {id: $id, owner: $owner, projectId: $projectId,
                          remoteJobId: '', status: $status, error: '',
                          title: $title, userText: $userText, prompt: $prompt,
                          stylePrompt: $stylePrompt, resolution: $resolution,
                          seconds: $seconds, frames: $frames, cameraMove: $cameraMove,
                          sourceKey: $sourceKey, sourceSha256: $sourceSha256,
                          sourceKeysJson: $sourceKeysJson,
                          sourceNamesJson: $sourceNamesJson,
                          answersJson: $answersJson,
                          outputKey: '', outputBytes: 0,
                          elapsedSeconds: 0, queuePosition: 0,
                          presetId: $presetId, presetLabel: $presetLabel,
                          boostJson: $boostJson, profileJson: '{}',
                          createdAt: $now, updatedAt: $now})
        CREATE (u)-[:made]->(w)
        RETURN """
        + _WANJOB_RETURN,
        {
            "owner": owner,
            "id": job_id,
            "projectId": (project_id or "")[:64],
            "status": WAN_QUEUED,
            "title": (title or "")[:200],
            "userText": (user_text or "")[:8000],
            "prompt": (prompt or "")[:4000],
            "stylePrompt": (style_prompt or "")[:1000],
            "resolution": resolution[:24],
            "seconds": int(seconds),
            "frames": int(frames),
            "cameraMove": (camera_move or "")[:80],
            "sourceKey": (source_key or "")[:400],
            "sourceSha256": (source_sha256 or "")[:64],
            "sourceKeysJson": _json.dumps(source_keys or [], ensure_ascii=False)[:4000],
            "sourceNamesJson": _json.dumps(source_names or [], ensure_ascii=False)[:4000],
            "answersJson": _json.dumps(answers or [], ensure_ascii=False)[:6000],
            "presetId": (preset_id or "")[:64],
            "presetLabel": (preset_label or "")[:80],
            "boostJson": boost_json or "{}",
            "now": now,
        },
    )
    if not rows:
        raise GraphError("영상 작업을 만들지 못했습니다.")
    logger.info(
        "Wan job created id=%s owner=%s resolution=%s frames=%s", job_id, owner, resolution, frames
    )
    return _wanjob_view(rows[0])


def update_wanjob(job_id: str, fields: dict) -> dict:
    allowed = {
        "remoteJobId",
        "status",
        "error",
        "title",
        "outputKey",
        "outputBytes",
        "elapsedSeconds",
        "queuePosition",
        # 추론 서버의 스테이지 프로파일(JSON 문자열). 성공·실패 양쪽에서 쓴다 —
        # 실패한 작업의 프로파일이 어디서 멎었는지 알려 준다.
        "profileJson",
    }
    updates = _filter_updates(fields, allowed, "영상 작업")
    assignments = ", ".join(f"w.{key} = ${key}" for key in updates)
    rows = run(
        f"""
        MATCH (w:wanjob {{id: $id}})
        SET {assignments}, w.updatedAt = $now
        RETURN {_WANJOB_RETURN}
        """,
        {**updates, "id": job_id, "now": _now()},
    )
    if not rows:
        raise NotFoundError("영상 작업을 찾을 수 없습니다.")
    return _wanjob_view(rows[0])


def touch_wanjob(job_id: str) -> None:
    """진행 중임을 알린다. 15분 넘게 도는 작업이 stale로 오해되지 않게 한다."""
    try:
        run(
            "MATCH (w:wanjob {id: $id}) SET w.updatedAt = $now",
            {"id": job_id, "now": _now()},
        )
    except GraphError:
        logger.warning("Wan job touch failed id=%s", job_id)


def get_wanjob(job_id: str) -> dict | None:
    rows = run("MATCH (w:wanjob {id: $id}) RETURN " + _WANJOB_RETURN, {"id": job_id})
    return _wanjob_view(rows[0]) if rows else None


def list_wanjobs(owner: str, project_id: str = "", limit: int = 20) -> list[dict]:
    if project_id:
        query = (
            "MATCH (w:wanjob {owner: $owner, projectId: $projectId}) RETURN "
            + _WANJOB_RETURN
            + " ORDER BY w.createdAt DESC LIMIT $limit"
        )
        params = {"owner": owner, "projectId": project_id, "limit": int(limit)}
    else:
        query = (
            "MATCH (w:wanjob {owner: $owner}) RETURN "
            + _WANJOB_RETURN
            + " ORDER BY w.createdAt DESC LIMIT $limit"
        )
        params = {"owner": owner, "limit": int(limit)}
    return [_wanjob_view(row) for row in run(query, params)]


def delete_wanjob(job_id: str) -> str | None:
    """작업을 지우고 정리할 S3 키를 돌려준다."""
    job = get_wanjob(job_id)
    if not job:
        raise NotFoundError("영상 작업을 찾을 수 없습니다.")
    run("MATCH (w:wanjob {id: $id}) DETACH DELETE w", {"id": job_id})
    logger.info("Wan job deleted id=%s", job_id)
    return job.get("outputKey") or None


# --------------------------------------------------------------------------- #
# 스토리 개편: 프로젝트 필드
# --------------------------------------------------------------------------- #
def set_story_type(project_id: str, story_type: str) -> dict:
    """원문 타입을 정한다. 지시서 3절에서 필수 입력이다."""
    if story_type not in STORY_TYPES:
        raise GraphError("스토리 타입은 대본 / 트리트먼트 / 로그라인 중 하나여야 합니다.")
    return update_project(project_id, {"storyType": story_type})


def set_summary_story(project_id: str, summary: str) -> dict:
    """요약 스토리(≤300자). 이미지 생성의 참고 맥락으로 쓰인다."""
    return update_project(project_id, {"summaryStory": (summary or "").strip()[:300]})


def set_story_text(project_id: str, s3_key: str, char_count: int) -> tuple[dict, list[str]]:
    """실제 스토리 본문을 S3 에 두고 키만 저장한다. 정리할 옛 키를 함께 돌려준다.

    `set_project_source` 와 같은 형태를 쓴다. 호출자가 옛 객체를 실제로 지운다 —
    graph_store 가 S3 를 만지지 않는다는 이 코드베이스의 경계를 지킨다.
    """
    project = get_project(project_id)
    if not project:
        raise NotFoundError("프로젝트를 찾을 수 없습니다.")
    stale = (project.get("storyText") or {}).get("key") or ""
    updated = update_project(
        project_id,
        {"storyTextKey": s3_key, "storyTextCharCount": int(char_count)},
    )
    return updated, ([stale] if stale and stale != s3_key else [])


def set_story_assets(project_id: str, assets: list[dict]) -> dict:
    """첨부 이미지 목록. 각 항목 {s3_key, sha256, name, analysis, analysis_model, edited_by_user}.

    분석 결과를 프로젝트에 함께 저장하는 이유: 기획자가 화면에서 고친 분석이
    바이블 재생성의 근거가 되어야 한다(지시서 9절 수용 기준). 캐시에만 두면
    사용자 편집을 어디에도 남길 수 없다.
    """
    import json as _json

    trimmed = []
    for item in (assets or [])[:10]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("s3_key") or item.get("key") or "").strip()
        if not key:
            continue
        trimmed.append(
            {
                "s3_key": key[:400],
                "sha256": str(item.get("sha256") or "")[:64],
                "name": str(item.get("name") or "")[:200],
                "analysis": item.get("analysis") if isinstance(item.get("analysis"), dict) else {},
                "analysis_model": str(item.get("analysis_model") or "")[:120],
                "edited_by_user": bool(item.get("edited_by_user")),
            }
        )
    return update_project(
        project_id,
        {"storyAssetsJson": _json.dumps(trimmed, ensure_ascii=False)},
    )


def story_asset_keys(project: dict) -> list[str]:
    """첨부 이미지와 원문 본문의 S3 키. 프로젝트 삭제 시 정리 대상이다."""
    keys = [(project.get("storyText") or {}).get("key") or ""]
    keys += [str(item.get("s3_key") or "") for item in (project.get("storyAssets") or [])]
    return [key for key in keys if key]


# --------------------------------------------------------------------------- #
# 스토리 바이블
# --------------------------------------------------------------------------- #
# 프로젝트당 하나. 재생성은 같은 정점을 덮어쓴다(document 와 같은 방식).
_BIBLE_RETURN = """
    b.bibleKey AS bibleKey, b.projectId AS projectId, b.sourceHash AS sourceHash,
    b.payloadJson AS payloadJson, b.model AS model, b.version AS version,
    b.createdAt AS createdAt, b.updatedAt AS updatedAt
"""


def _bible_view(row: dict) -> dict:
    import json as _json

    try:
        payload = _json.loads(row.get("payloadJson") or "{}")
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # source_hash 는 payload 안에도 있고 정점 속성에도 있다. 정점 속성이 기준이다 —
    # 조회 조건으로 쓰이므로 인덱싱 가능한 곳에 있어야 한다.
    payload["source_hash"] = row.get("sourceHash") or ""
    return {
        "projectId": row.get("projectId") or "",
        "sourceHash": row.get("sourceHash") or "",
        "model": row.get("model") or "",
        "version": int(row.get("version") or 0),
        "bible": payload,
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def save_story_bible(project_id: str, bible: dict, model: str) -> dict:
    """바이블을 저장한다. 같은 프로젝트면 덮어쓰고 version 을 올린다."""
    import json as _json

    if not get_project(project_id):
        raise NotFoundError("프로젝트를 찾을 수 없습니다.")

    now = _now()
    rows = run(
        """
        MERGE (b:story_bible {bibleKey: $bibleKey})
        ON CREATE SET b.version = 0, b.createdAt = $now, b.projectId = $projectId
        SET b.sourceHash = $sourceHash, b.payloadJson = $payloadJson,
            b.model = $model, b.updatedAt = $now, b.version = b.version + 1
        WITH b
        MATCH (p:project {id: $projectId})
        MERGE (p)-[:has_bible]->(b)
        RETURN """
        + _BIBLE_RETURN,
        {
            "bibleKey": f"{project_id}:bible",
            "projectId": project_id,
            "sourceHash": str(bible.get("source_hash") or "")[:64],
            "payloadJson": _json.dumps(bible, ensure_ascii=False),
            "model": (model or "")[:200],
            "now": now,
        },
    )
    if not rows:
        raise GraphError("바이블을 저장하지 못했습니다.")
    logger.info("Story bible saved project=%s hash=%s", project_id, str(bible.get("source_hash"))[:12])
    return _bible_view(rows[0])


def get_story_bible(project_id: str) -> dict | None:
    rows = run(
        "MATCH (b:story_bible {bibleKey: $bibleKey}) RETURN " + _BIBLE_RETURN,
        {"bibleKey": f"{project_id}:bible"},
    )
    return _bible_view(rows[0]) if rows else None


def delete_story_bible(project_id: str) -> None:
    run(
        "MATCH (b:story_bible {bibleKey: $bibleKey}) DETACH DELETE b",
        {"bibleKey": f"{project_id}:bible"},
    )


# --------------------------------------------------------------------------- #
# 결과물 (outputs)
# --------------------------------------------------------------------------- #
# document 정점을 재사용하지 않는 이유:
#   document 는 docKey = "{project}:{kind}" 로 **프로젝트당 종류별 하나**이고
#   DOCUMENT_KINDS 화이트리스트에 묶여 있다. 스토리 결과물은 같은 종류를 여러 번 만들어
#   나란히 두어야 하고(로그라인 3안을 두 번 뽑아 비교), 검수 메모와 trace 를 갖는다.
#   그래서 별도 정점을 둔다.
_OUTPUT_RETURN = """
    o.id AS id, o.projectId AS projectId, o.owner AS owner, o.kind AS kind,
    o.status AS status, o.error AS error, o.model AS model, o.version AS version,
    o.s3Key AS s3Key, o.title AS title, o.charCount AS charCount,
    o.tokensIn AS tokensIn, o.tokensOut AS tokensOut,
    o.validatorNotesJson AS validatorNotesJson, o.traceJson AS traceJson,
    o.contextPackRef AS contextPackRef, o.optionsJson AS optionsJson,
    o.aiGenerated AS aiGenerated, o.storyboardId AS storyboardId,
    o.storyHash AS storyHash, o.storyTextRef AS storyTextRef,
    o.storyType AS storyType, o.storySnapshot AS storySnapshot,
    o.storyCharCount AS storyCharCount,
    o.createdAt AS createdAt, o.updatedAt AS updatedAt
"""

# 결과물·대본 잡이 공유하는 스토리 계보 속성. 두 곳에 같은 이름으로 두어야
# story_context.stamp / stamp_drift 가 양쪽에 그대로 쓰인다.
# 자세한 배경은 story_context.stamp 의 주석 참고.
STORY_STAMP_FIELDS = (
    "storyHash",
    "storyTextRef",
    "storyType",
    "storySnapshot",
    "storyCharCount",
)


def _stamp_params(stamp: dict | None) -> dict:
    """스탬프를 쿼리 파라미터로 정규화한다. 없으면 빈 값으로 채운다.

    빈 문자열로 두는 것이 중요하다. null 을 넣으면 openCypher 가 속성 삭제로 다뤄
    RETURN 절이 없는 키를 돌려주고, `_output_view` 가 매번 기본값으로 메워야 한다.
    """
    source = stamp if isinstance(stamp, dict) else {}
    return {
        "storyHash": str(source.get("storyHash") or "")[:64],
        "storyTextRef": str(source.get("storyTextRef") or "")[:400],
        "storyType": str(source.get("storyType") or "")[:20],
        "storySnapshot": str(source.get("storySnapshot") or "")[:300],
        "storyCharCount": int(source.get("storyCharCount") or 0),
    }


def _stamp_view(row: dict) -> dict:
    """정점에서 읽은 스탬프. 이 코드 이전 결과물에는 속성이 없어 전부 빈 값이 된다.

    빈 값은 "어긋나지 않았다"가 아니라 "모른다"는 뜻이다. 판정은 stamp_drift 가 한다.
    """
    return {
        "storyHash": row.get("storyHash") or "",
        "storyTextRef": row.get("storyTextRef") or "",
        "storyType": row.get("storyType") or "",
        "storySnapshot": row.get("storySnapshot") or "",
        "storyCharCount": int(row.get("storyCharCount") or 0),
    }


def _output_view(row: dict) -> dict:
    import json as _json

    def _load(raw, fallback):
        try:
            value = _json.loads(raw or "")
        except (ValueError, TypeError):
            return fallback
        return value if isinstance(value, type(fallback)) else fallback

    return {
        "id": row.get("id"),
        "projectId": row.get("projectId") or "",
        "owner": row.get("owner") or "",
        "kind": row.get("kind") or "",
        "status": row.get("status") or STORY_GENERATING,
        "error": row.get("error") or "",
        "model": row.get("model") or "",
        "version": int(row.get("version") or 1),
        "s3Key": row.get("s3Key") or "",
        "title": row.get("title") or "",
        "charCount": int(row.get("charCount") or 0),
        "tokensIn": int(row.get("tokensIn") or 0),
        "tokensOut": int(row.get("tokensOut") or 0),
        "validatorNotes": _load(row.get("validatorNotesJson"), []),
        "trace": _load(row.get("traceJson"), []),
        "contextPackRef": row.get("contextPackRef") or "",
        "options": _load(row.get("optionsJson"), {}),
        # 콘티 결과물이 가리키는 storyboard 정점. **컷 그림은 그쪽에만 있다.**
        # 이 값이 없으면 결과물 화면은 텍스트 계획만 보여 주고 그림을 찾을 방법이 없다.
        # 실제로 그래서 "콘티 내용" 모달에 글만 나왔다.
        "storyboardId": row.get("storyboardId") or "",
        # 지시서 0절: "생성물은 전부 AI 생성 표기(뱃지 + 파일 메타데이터)".
        # 값을 저장해 두면 화면이 조건 없이 뱃지를 붙일 수 있고, 나중에 사람이 편집한
        # 결과물이 생겨도 구분된다. 기본 True 다.
        "aiGenerated": row.get("aiGenerated") is not False,
        # 어느 스토리로 만들었는지. story_context.stamp 가 만든다.
        "story": _stamp_view(row),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def create_story_output(
    project_id: str,
    owner: str,
    kind: str,
    *,
    model: str = "",
    options: dict | None = None,
    stamp: dict | None = None,
) -> dict:
    """결과물 생성 시작을 기록한다. 상태는 generating.

    `stamp` 는 story_context.stamp 가 만든 "어느 스토리로 만드는가" 지문이다.
    완료 시점이 아니라 **시작 시점에** 박는다 — 생성이 실패한 결과물도 어느 스토리를
    근거로 시도했는지 남아야 하고, 생성 도중에 사용자가 스토리를 고칠 수도 있다.
    """
    import json as _json

    if kind not in STORY_OUTPUT_KINDS:
        raise GraphError("결과물 종류가 올바르지 않습니다.")
    if not get_project(project_id):
        raise NotFoundError("프로젝트를 찾을 수 없습니다.")

    output_id = uuid.uuid4().hex
    now = _now()
    stamp_params = _stamp_params(stamp)
    rows = run(
        """
        MATCH (u:user {username: $owner}), (p:project {id: $projectId})
        CREATE (o:story_output {id: $id, projectId: $projectId, owner: $owner, kind: $kind,
                               status: $status, error: '', model: $model, version: 1,
                               s3Key: '', title: '', charCount: 0,
                               tokensIn: 0, tokensOut: 0,
                               validatorNotesJson: '[]', traceJson: '[]',
                               contextPackRef: '', optionsJson: $optionsJson,
                               aiGenerated: true, storyboardId: '',
                               storyHash: $storyHash, storyTextRef: $storyTextRef,
                               storyType: $storyType, storySnapshot: $storySnapshot,
                               storyCharCount: $storyCharCount,
                               createdAt: $now, updatedAt: $now})
        CREATE (u)-[:made]->(o)
        CREATE (p)-[:has_output]->(o)
        RETURN """
        + _OUTPUT_RETURN,
        {
            "id": output_id,
            "projectId": project_id,
            "owner": owner,
            "kind": kind,
            "status": STORY_GENERATING,
            "model": (model or "")[:200],
            "optionsJson": _json.dumps(options or {}, ensure_ascii=False),
            **stamp_params,
            "now": now,
        },
    )
    if not rows:
        raise GraphError("결과물을 만들지 못했습니다.")
    logger.info(
        "Story output created id=%s project=%s kind=%s storyHash=%s",
        output_id, project_id, kind, (stamp_params["storyHash"] or "-")[:12],
    )
    return _output_view(rows[0])


def update_story_output(output_id: str, fields: dict) -> dict:
    allowed = {
        "status", "error", "model", "s3Key", "title", "charCount",
        "tokensIn", "tokensOut", "validatorNotesJson", "traceJson",
        "contextPackRef", "optionsJson", "version", "aiGenerated",
        # 콘티 결과물 → storyboard 링크. 결과물을 만든 뒤에 콘티 정점이 생기므로
        # 생성 시점에는 빈 값이고 나중에 채운다.
        "storyboardId",
        # 스탬프는 보통 생성 시점에 박히지만, 옛 결과물 보정과 재생성 경로가
        # 나중에 채울 수 있어야 한다. 목록에서 빠지면 _filter_updates 가 조용히
        # 걸러내고 "변경할 항목이 없습니다" 를 던진다(update_storyboard 주석 참고).
        *STORY_STAMP_FIELDS,
    }
    updates = _filter_updates(fields, allowed, "결과물")
    assignments = ", ".join(f"o.{key} = ${key}" for key in updates)
    rows = run(
        f"""
        MATCH (o:story_output {{id: $id}})
        SET {assignments}, o.updatedAt = $now
        RETURN {_OUTPUT_RETURN}
        """,
        {**updates, "id": output_id, "now": _now()},
    )
    if not rows:
        raise NotFoundError("결과물을 찾을 수 없습니다.")
    return _output_view(rows[0])


def get_story_output(output_id: str) -> dict | None:
    rows = run("MATCH (o:story_output {id: $id}) RETURN " + _OUTPUT_RETURN, {"id": output_id})
    return _output_view(rows[0]) if rows else None


def list_story_outputs(project_id: str, kind: str = "", limit: int = 50) -> list[dict]:
    if kind:
        query = (
            "MATCH (o:story_output {projectId: $projectId, kind: $kind}) RETURN "
            + _OUTPUT_RETURN + " ORDER BY o.createdAt DESC LIMIT $limit"
        )
        params = {"projectId": project_id, "kind": kind, "limit": int(limit)}
    else:
        query = (
            "MATCH (o:story_output {projectId: $projectId}) RETURN "
            + _OUTPUT_RETURN + " ORDER BY o.createdAt DESC LIMIT $limit"
        )
        params = {"projectId": project_id, "limit": int(limit)}
    return [_output_view(row) for row in run(query, params)]


def delete_story_output(output_id: str) -> list[str]:
    """결과물을 지우고 정리할 S3 키를 돌려준다."""
    output = get_story_output(output_id)
    if not output:
        raise NotFoundError("결과물을 찾을 수 없습니다.")
    keys = [key for key in (output.get("s3Key"), output.get("contextPackRef")) if key]
    run("MATCH (o:story_output {id: $id}) DETACH DELETE o", {"id": output_id})
    return keys


def story_output_keys_for_project(project_id: str) -> list[str]:
    keys: list[str] = []
    for output in list_story_outputs(project_id, limit=500):
        keys += [key for key in (output.get("s3Key"), output.get("contextPackRef")) if key]
    return keys


def delete_story_outputs_for_project(project_id: str) -> None:
    run("MATCH (o:story_output {projectId: $projectId}) DETACH DELETE o", {"projectId": project_id})


def delete_story_outputs_for_user(username: str) -> None:
    run("MATCH (o:story_output {owner: $username}) DETACH DELETE o", {"username": username})


# --------------------------------------------------------------------------- #
# 대본 잡 (script_job)
# --------------------------------------------------------------------------- #
# 지시서 3절: 아웃라인 승인 전에는 회차 생성이 시작되지 않는다.
# 그래서 상태가 planning -> awaiting -> writing -> ready 다. awaiting 이 승인 대기다.
_SCRIPT_JOB_RETURN = """
    j.id AS id, j.projectId AS projectId, j.owner AS owner, j.status AS status,
    j.error AS error, j.episodeFrom AS episodeFrom, j.episodeTo AS episodeTo,
    j.length AS length, j.format AS format, j.quality AS quality,
    j.outlineKey AS outlineKey, j.arc AS arc,
    j.episodesJson AS episodesJson, j.doneCount AS doneCount, j.failedCount AS failedCount,
    j.contextPackRef AS contextPackRef, j.traceJson AS traceJson,
    j.approvedAt AS approvedAt,
    j.storyHash AS storyHash, j.storyTextRef AS storyTextRef,
    j.storyType AS storyType, j.storySnapshot AS storySnapshot,
    j.storyCharCount AS storyCharCount,
    j.createdAt AS createdAt, j.updatedAt AS updatedAt
"""


def _script_job_view(row: dict) -> dict:
    import json as _json

    def _load(raw, fallback):
        try:
            value = _json.loads(raw or "")
        except (ValueError, TypeError):
            return fallback
        return value if isinstance(value, type(fallback)) else fallback

    return {
        "id": row.get("id"),
        "projectId": row.get("projectId") or "",
        "owner": row.get("owner") or "",
        "status": row.get("status") or SCRIPT_PLANNING,
        "error": row.get("error") or "",
        "episodeFrom": int(row.get("episodeFrom") or 1),
        "episodeTo": int(row.get("episodeTo") or 1),
        "length": row.get("length") or "normal",
        "format": row.get("format") or "drama",
        "quality": row.get("quality") or "standard",
        "outlineKey": row.get("outlineKey") or "",
        "arc": row.get("arc") or "",
        # [{no, status, title, summary, s3Key, charCount, validatorNotes, endingHook}]
        "episodes": _load(row.get("episodesJson"), []),
        "doneCount": int(row.get("doneCount") or 0),
        "failedCount": int(row.get("failedCount") or 0),
        "contextPackRef": row.get("contextPackRef") or "",
        "trace": _load(row.get("traceJson"), []),
        "approvedAt": row.get("approvedAt") or "",
        # 어느 스토리로 만들었는지. 결과물과 같은 구조다.
        "story": _stamp_view(row),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def create_script_job(
    project_id: str,
    owner: str,
    *,
    episode_from: int,
    episode_to: int,
    length: str,
    fmt: str = "drama",
    quality: str = "standard",
) -> dict:
    job_id = uuid.uuid4().hex
    now = _now()
    rows = run(
        """
        MATCH (u:user {username: $owner}), (p:project {id: $projectId})
        CREATE (j:script_job {id: $id, projectId: $projectId, owner: $owner,
                              status: $status, error: '',
                              episodeFrom: $episodeFrom, episodeTo: $episodeTo,
                              length: $length, format: $format, quality: $quality,
                              outlineKey: '', arc: '', episodesJson: '[]',
                              doneCount: 0, failedCount: 0,
                              contextPackRef: '', traceJson: '[]', approvedAt: '',
                              storyHash: '', storyTextRef: '',
                              storyType: '', storySnapshot: '', storyCharCount: 0,
                              createdAt: $now, updatedAt: $now})
        CREATE (u)-[:made]->(j)
        CREATE (p)-[:has_script_job]->(j)
        RETURN """
        + _SCRIPT_JOB_RETURN,
        {
            "id": job_id,
            "projectId": project_id,
            "owner": owner,
            "status": SCRIPT_PLANNING,
            "episodeFrom": int(episode_from),
            "episodeTo": int(episode_to),
            "length": length,
            "format": fmt,
            "quality": quality,
            "now": now,
        },
    )
    if not rows:
        raise GraphError("대본 작업을 만들지 못했습니다.")
    logger.info(
        "Script job created id=%s project=%s %s~%s화 quality=%s",
        job_id, project_id, episode_from, episode_to, quality,
    )
    return _script_job_view(rows[0])


def update_script_job(job_id: str, fields: dict) -> dict:
    allowed = {
        "status", "error", "outlineKey", "arc", "episodesJson",
        "doneCount", "failedCount", "contextPackRef", "traceJson", "approvedAt",
        # 대본 잡은 결과물과 달리 생성 시점에 스탬프를 박을 수 없다. 팩(=바이블)이
        # 백그라운드 스레드에서 만들어지므로, 아웃라인이 완성될 때 여기로 채운다.
        *STORY_STAMP_FIELDS,
    }
    updates = _filter_updates(fields, allowed, "대본 작업")
    assignments = ", ".join(f"j.{key} = ${key}" for key in updates)
    rows = run(
        f"""
        MATCH (j:script_job {{id: $id}})
        SET {assignments}, j.updatedAt = $now
        RETURN {_SCRIPT_JOB_RETURN}
        """,
        {**updates, "id": job_id, "now": _now()},
    )
    if not rows:
        raise NotFoundError("대본 작업을 찾을 수 없습니다.")
    return _script_job_view(rows[0])


def touch_script_job(job_id: str) -> None:
    """진행 중임을 알린다. 12화 잡은 15분을 넘길 수 있어 stale 판정을 피해야 한다."""
    try:
        run("MATCH (j:script_job {id: $id}) SET j.updatedAt = $now", {"id": job_id, "now": _now()})
    except GraphError:
        logger.warning("Script job touch failed id=%s", job_id)


def get_script_job(job_id: str) -> dict | None:
    rows = run("MATCH (j:script_job {id: $id}) RETURN " + _SCRIPT_JOB_RETURN, {"id": job_id})
    return _script_job_view(rows[0]) if rows else None


def list_script_jobs(owner: str, project_id: str = "", limit: int = 20) -> list[dict]:
    if project_id:
        query = (
            "MATCH (j:script_job {owner: $owner, projectId: $projectId}) RETURN "
            + _SCRIPT_JOB_RETURN + " ORDER BY j.createdAt DESC LIMIT $limit"
        )
        params = {"owner": owner, "projectId": project_id, "limit": int(limit)}
    else:
        query = (
            "MATCH (j:script_job {owner: $owner}) RETURN "
            + _SCRIPT_JOB_RETURN + " ORDER BY j.createdAt DESC LIMIT $limit"
        )
        params = {"owner": owner, "limit": int(limit)}
    return [_script_job_view(row) for row in run(query, params)]


def delete_script_job(job_id: str) -> list[str]:
    job = get_script_job(job_id)
    if not job:
        raise NotFoundError("대본 작업을 찾을 수 없습니다.")
    keys = [job.get("outlineKey") or "", job.get("contextPackRef") or ""]
    keys += [str(ep.get("s3Key") or "") for ep in (job.get("episodes") or [])]
    run("MATCH (j:script_job {id: $id}) DETACH DELETE j", {"id": job_id})
    return [key for key in keys if key]


def script_job_keys_for_project(project_id: str) -> list[str]:
    keys: list[str] = []
    for job in run(
        "MATCH (j:script_job {projectId: $projectId}) RETURN " + _SCRIPT_JOB_RETURN,
        {"projectId": project_id},
    ):
        view = _script_job_view(job)
        keys += [view.get("outlineKey") or "", view.get("contextPackRef") or ""]
        keys += [str(ep.get("s3Key") or "") for ep in (view.get("episodes") or [])]
    return [key for key in keys if key]


def delete_script_jobs_for_project(project_id: str) -> None:
    run("MATCH (j:script_job {projectId: $projectId}) DETACH DELETE j", {"projectId": project_id})


def delete_script_jobs_for_user(username: str) -> None:
    run("MATCH (j:script_job {owner: $username}) DETACH DELETE j", {"username": username})


# --------------------------------------------------------------------------- #
# 장면 영상화 (Seedance 2.0) — 붙였다 뗄 수 있는 독립 기능
#
# wanjob과 같은 CRUD 패턴을 그대로 따른다(create -> update -> get -> list).
# 샷 배열은 여기 넣지 않는다(속성 크기 제한). planKey가 가리키는 S3 JSON이 정본이고,
# 여기서는 상태·요약만 갖는다(계획서 5.1절).
# --------------------------------------------------------------------------- #
SCENE_DRAFT = "draft"
SCENE_ANALYZED = "analyzed"
SCENE_PLANNED = "planned"
SCENE_GENERATING = "generating"
SCENE_ASSEMBLING = "assembling"
SCENE_DONE = "done"
SCENE_NEEDS_REVIEW = "needs_review"
SCENE_FAILED = "failed"
# 사용자가 취소했거나 이전 단계로 돌아가 현 실행을 접은 상태. 실패와 구분한다 —
# 실패는 고칠 것이 있다는 뜻이고, 취소는 사용자가 의도한 중단이다.
SCENE_CANCELLED = "cancelled"

_VIDEO_SCENE_RETURN = """
    s.id AS id, s.owner AS owner, s.projectId AS projectId, s.title AS title,
    s.style AS style, s.status AS status, s.error AS error,
    s.inputKey AS inputKey, s.analysisKey AS analysisKey, s.planKey AS planKey,
    s.finalKey AS finalKey, s.vttKey AS vttKey, s.artifactVersion AS artifactVersion,
    s.resultType AS resultType, s.aiShotCount AS aiShotCount,
    s.aiProvider AS aiProvider, s.aiModelId AS aiModelId,
    s.aiRegion AS aiRegion, s.aiOutputBucket AS aiOutputBucket,
    s.aiConfigFingerprint AS aiConfigFingerprint, s.aiSourceRevision AS aiSourceRevision,
    s.aiTaskId AS aiTaskId, s.aiOutputUri AS aiOutputUri,
    s.aiVerifiedAt AS aiVerifiedAt,
    s.qualityReviewStatus AS qualityReviewStatus,
    s.qualityReviewedAt AS qualityReviewedAt,
    s.qualityReviewNote AS qualityReviewNote,
    s.resultAcceptanceRequired AS resultAcceptanceRequired,
    s.resultAcceptanceStatus AS resultAcceptanceStatus,
    s.productionStage AS productionStage, s.productionKey AS productionKey,
    s.productionRequestId AS productionRequestId,
    s.generationRunId AS generationRunId,
    s.cancelRequestedRun AS cancelRequestedRun, s.cancelReason AS cancelReason,
    s.cancelRequestedAt AS cancelRequestedAt, s.remoteMayContinue AS remoteMayContinue,
    s.shotCount AS shotCount, s.shotsDone AS shotsDone,
    s.tokensUsed AS tokensUsed, s.costEstimateUsd AS costEstimateUsd,
    s.characterIdsJson AS characterIdsJson,
    s.createdAt AS createdAt, s.updatedAt AS updatedAt
"""


def _video_scene_view(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "owner": row.get("owner"),
        "projectId": row.get("projectId") or "",
        "title": row.get("title") or "",
        "style": row.get("style") or "anime",
        "status": row.get("status") or SCENE_DRAFT,
        "error": row.get("error") or "",
        "inputKey": row.get("inputKey") or "",
        "analysisKey": row.get("analysisKey") or "",
        "planKey": row.get("planKey") or "",
        "finalKey": row.get("finalKey") or "",
        "vttKey": row.get("vttKey") or "",
        "artifactVersion": int(row.get("artifactVersion") or 0),
        # v3 이전 scene에는 없으므로 optional 기본값으로 흡수한다. migration query가 필요 없다.
        "resultType": row.get("resultType") or "",
        "aiShotCount": int(row.get("aiShotCount") or 0),
        "aiProvider": row.get("aiProvider") or "",
        "aiModelId": row.get("aiModelId") or "",
        "aiRegion": row.get("aiRegion") or "",
        "aiOutputBucket": row.get("aiOutputBucket") or "",
        "aiConfigFingerprint": row.get("aiConfigFingerprint") or "",
        "aiSourceRevision": row.get("aiSourceRevision") or "",
        "aiTaskId": row.get("aiTaskId") or "",
        "aiOutputUri": row.get("aiOutputUri") or "",
        "aiVerifiedAt": row.get("aiVerifiedAt"),
        # 인프라/자동 생성 성공/사람 품질 검수는 서로 다른 상태다.
        "qualityReviewStatus": row.get("qualityReviewStatus") or "unreviewed",
        "qualityReviewedAt": row.get("qualityReviewedAt"),
        "qualityReviewNote": row.get("qualityReviewNote") or "",
        # 검수용 Luma 결과는 기술적으로 생성돼도 사용자가 수락하기 전에는 완료본·공유
        # readiness 증거가 아니다. 과거 scene은 False/빈 값으로 읽어 migration이 필요 없다.
        "resultAcceptanceRequired": bool(row.get("resultAcceptanceRequired", False)),
        "resultAcceptanceStatus": row.get("resultAcceptanceStatus") or "",
        # 대화형 제작 흐름 state는 S3 productionKey가 정본이고, 이 세 값은 목록·새로고침에
        # 필요한 작은 인덱스다. 과거 scene은 빈 값으로 읽는다.
        "productionStage": row.get("productionStage") or "",
        "productionKey": row.get("productionKey") or "",
        "productionRequestId": row.get("productionRequestId") or "",
        # 실행 버전과 취소 상태. **취소의 정본은 그래프**다 — 워커가 단계 사이마다
        # 여기를 읽어 판단하므로, 파드가 재시작하거나 큐가 같은 메시지를 다시 전달해도
        # 취소된 실행이 되살아나지 않는다.
        #
        # `cancelRequestedRun == generationRunId` 이면 현재 실행이 취소된 것이다.
        # 실행 ID로 비교하는 이유: 취소한 실행의 늦은 응답이 그다음 새 실행을 덮어쓰면
        # 안 된다. 과거 scene은 빈 값이라 migration 없이 읽힌다.
        "generationRunId": row.get("generationRunId") or "",
        "cancelRequestedRun": row.get("cancelRequestedRun") or "",
        "cancelReason": row.get("cancelReason") or "",
        "cancelRequestedAt": row.get("cancelRequestedAt") or "",
        # provider 가 원격 취소를 지원하지 않으면 이미 제출된 호출은 남는다. 그 사실을
        # 숨기지 않는다(요구 6의 비용 안내 근거).
        "remoteMayContinue": bool(row.get("remoteMayContinue", False)),
        "shotCount": int(row.get("shotCount") or 0),
        "shotsDone": int(row.get("shotsDone") or 0),
        "tokensUsed": int(row.get("tokensUsed") or 0),
        "costEstimateUsd": float(row.get("costEstimateUsd") or 0.0),
        "characterIds": _load_list(row.get("characterIdsJson")),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def create_video_scene(
    owner: str,
    *,
    title: str,
    style: str,
    input_key: str,
    project_id: str = "",
    character_ids: list[str] | None = None,
) -> dict:
    """장면 영상화 작업을 기록한다. `draft` 상태로 시작한다."""
    import json as _json

    scene_id = uuid.uuid4().hex
    now = _now()
    rows = run(
        """
        MATCH (u:user {username: $owner})
        CREATE (s:video_scene {id: $id, owner: $owner, projectId: $projectId,
                               title: $title, style: $style, status: $status, error: '',
                               inputKey: $inputKey, analysisKey: '', planKey: '',
                               finalKey: '', vttKey: '', artifactVersion: 0,
                               resultType: '', aiShotCount: 0,
                               aiProvider: '', aiModelId: '', aiRegion: '', aiOutputBucket: '',
                               aiConfigFingerprint: '', aiSourceRevision: '',
                               aiTaskId: '', aiOutputUri: '', aiVerifiedAt: '',
                               qualityReviewStatus: 'unreviewed', qualityReviewedAt: '', qualityReviewNote: '',
                               resultAcceptanceRequired: false, resultAcceptanceStatus: '',
                               productionStage: '', productionKey: '', productionRequestId: '',
                               shotCount: 0, shotsDone: 0, tokensUsed: 0, costEstimateUsd: 0.0,
                               characterIdsJson: $characterIdsJson,
                               createdAt: $now, updatedAt: $now})
        CREATE (u)-[:made]->(s)
        RETURN """
        + _VIDEO_SCENE_RETURN,
        {
            "owner": owner,
            "id": scene_id,
            "projectId": (project_id or "")[:64],
            "title": (title or "")[:200],
            "style": style if style in ("anime", "live_action") else "anime",
            "status": SCENE_DRAFT,
            "inputKey": (input_key or "")[:400],
            "characterIdsJson": _json.dumps(character_ids or [], ensure_ascii=False)[:2000],
            "now": now,
        },
    )
    if not rows:
        raise GraphError("장면 작업을 만들지 못했습니다.")
    logger.info("Video scene created id=%s owner=%s style=%s", scene_id, owner, style)
    return _video_scene_view(rows[0])


def update_video_scene(scene_id: str, fields: dict) -> dict:
    allowed = {
        "status",
        "error",
        "title",
        # 프로젝트 소속. 옛 장면을 사용자가 직접 연결할 때만 바뀐다(server 의
        # scene_production_attach_project 가 소유자·권한·중복 연결을 확인한다).
        # 이 값만 바꾸므로 planKey·provider 작업 ID·artifactVersion 은 그대로 남는다.
        "projectId",
        "inputKey",
        "analysisKey",
        "planKey",
        "finalKey",
        "vttKey",
        "artifactVersion",
        "resultType",
        "aiShotCount",
        "aiProvider",
        "aiModelId",
        "aiRegion",
        "aiOutputBucket",
        "aiConfigFingerprint",
        "aiSourceRevision",
        "aiTaskId",
        "aiOutputUri",
        "aiVerifiedAt",
        "qualityReviewStatus",
        "qualityReviewedAt",
        "qualityReviewNote",
        "resultAcceptanceRequired",
        "resultAcceptanceStatus",
        "productionStage",
        "productionKey",
        "productionRequestId",
        # 실행 버전 · 취소 상태. 워커가 읽는 취소의 정본이다.
        "generationRunId",
        "cancelRequestedRun",
        "cancelReason",
        "cancelRequestedAt",
        "remoteMayContinue",
        "shotCount",
        "shotsDone",
        "tokensUsed",
        "costEstimateUsd",
        "characterIdsJson",
    }
    updates = _filter_updates(fields, allowed, "장면 작업")
    assignments = ", ".join(f"s.{key} = ${key}" for key in updates)
    rows = run(
        f"""
        MATCH (s:video_scene {{id: $id}})
        SET {assignments}, s.updatedAt = $now
        RETURN {_VIDEO_SCENE_RETURN}
        """,
        {**updates, "id": scene_id, "now": _now()},
    )
    if not rows:
        raise NotFoundError("장면 작업을 찾을 수 없습니다.")
    return _video_scene_view(rows[0])


def get_video_scene(scene_id: str) -> dict | None:
    rows = run("MATCH (s:video_scene {id: $id}) RETURN " + _VIDEO_SCENE_RETURN, {"id": scene_id})
    return _video_scene_view(rows[0]) if rows else None


def list_video_scenes(owner: str, project_id: str = "", limit: int = 20) -> list[dict]:
    if project_id:
        query = (
            "MATCH (s:video_scene {owner: $owner, projectId: $projectId}) RETURN "
            + _VIDEO_SCENE_RETURN
            + " ORDER BY s.createdAt DESC LIMIT $limit"
        )
        params = {"owner": owner, "projectId": project_id, "limit": int(limit)}
    else:
        query = (
            "MATCH (s:video_scene {owner: $owner}) RETURN "
            + _VIDEO_SCENE_RETURN
            + " ORDER BY s.createdAt DESC LIMIT $limit"
        )
        params = {"owner": owner, "limit": int(limit)}
    return [_video_scene_view(row) for row in run(query, params)]


def get_video_scene_by_production_request(owner: str, request_id: str) -> dict | None:
    """같은 클라이언트 요청의 scene을 돌려준다.

    버튼 이중 클릭·네트워크 응답 유실 뒤 재전송이 새 장면/새 이미지 비용을 만들지
    않게 하는 멱등성 키다. 빈 request_id는 조회하지 않는다.
    """
    if not request_id:
        return None
    rows = run(
        "MATCH (s:video_scene {owner: $owner, productionRequestId: $requestId}) RETURN "
        + _VIDEO_SCENE_RETURN,
        {"owner": owner, "requestId": request_id[:120]},
    )
    return _video_scene_view(rows[0]) if rows else None


def latest_ai_video_success() -> dict | None:
    """가장 최근의 실제 AI 영상 성공 기록을 Neptune에서 읽는다.

    readiness의 `lastAiVideo`는 프로세스 dict가 아니라 이 값을 써야 한다. 앱은
    gunicorn worker가 둘이고 ArgoCD 롤아웃 때 프로세스가 바뀌므로, 메모리 캐시는
    다른 worker/Pod 재시작 뒤에 "검증한 적 없음"으로 돌아간다.

    반환은 운영 준비 상태용이라 **owner와 S3 key를 노출하지 않는다.** 사용자가 다른
    사용자의 scene 존재나 결과 위치를 알아서는 안 된다.
    """
    rows = run(
        """
        MATCH (s:video_scene)
        WHERE s.resultType IN ['ai_video', 'hybrid']
          AND s.finalKey <> ''
          AND s.aiVerifiedAt <> ''
          AND (coalesce(s.resultAcceptanceRequired, false) = false
               OR s.resultAcceptanceStatus = 'approved')
        RETURN s.aiVerifiedAt AS verifiedAt, s.aiProvider AS provider,
               s.aiModelId AS modelId, s.aiRegion AS region,
               s.aiOutputBucket AS outputBucket,
               s.aiConfigFingerprint AS configFingerprint,
               s.aiSourceRevision AS sourceRevision,
               s.aiTaskId AS taskId,
               s.resultType AS resultType, s.aiShotCount AS aiShotCount,
               s.qualityReviewStatus AS qualityReviewStatus,
               s.qualityReviewedAt AS qualityReviewedAt
        ORDER BY s.aiVerifiedAt DESC
        LIMIT 1
        """
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "at": row.get("verifiedAt"),
        "provider": row.get("provider") or "",
        "modelId": row.get("modelId") or "",
        "region": row.get("region") or "",
        "outputBucket": row.get("outputBucket") or "",
        "configFingerprint": row.get("configFingerprint") or "",
        "sourceRevision": row.get("sourceRevision") or "",
        "taskId": row.get("taskId") or "",
        "resultType": row.get("resultType") or "",
        "aiShotCount": int(row.get("aiShotCount") or 0),
        "qualityReviewStatus": row.get("qualityReviewStatus") or "unreviewed",
        "qualityReviewedAt": row.get("qualityReviewedAt"),
    }


# --------------------------------------------------------------------------- #
# 장면 결과 리뷰 · 사용자 메모리
#
# 새 정점만 추가한다. 기존 video_scene를 일괄 갱신하는 migration은 하지 않는다.
# Review는 결과 평가의 증거이고, preference_memory는 사용자가 "다음에도 반영"을
# 명시적으로 선택한 지침이다. 점수만으로 취향을 추론해 memory를 만들지 않는다.
# --------------------------------------------------------------------------- #
REVIEW_TAGS = (
    "character_consistency",
    "requested_motion",
    "camera",
    "reference_image",
    "style",
    "pacing",
    "safety_expression",
    "overall",
    # 기술 결함은 개인 취향과 별도 구조로 저장한다.
    "missing_character",
    "no_motion",
    "unwanted_camera",
    "text_error",
    "continuity",
)
MEMORY_SCOPES = ("global", "project", "scene")

_SCENE_REVIEW_RETURN = """
    r.id AS id, r.owner AS owner, r.sceneId AS sceneId, r.projectId AS projectId,
    r.artifactVersion AS artifactVersion, r.revision AS revision,
    r.score AS score, r.tagsJson AS tagsJson, r.comment AS comment,
    r.verdict AS verdict, r.createdAt AS createdAt, r.updatedAt AS updatedAt
"""


def _scene_review_view(row: dict) -> dict:
    return {
        "id": row.get("id") or "",
        "sceneId": row.get("sceneId") or "",
        "projectId": row.get("projectId") or "",
        "artifactVersion": int(row.get("artifactVersion") or 0),
        "revision": int(row.get("revision") or 0),
        # None(미평가)과 0점을 구분한다.
        "score": int(row["score"]) if row.get("score") is not None else None,
        "tags": _load_list(row.get("tagsJson")),
        "comment": row.get("comment") or "",
        "verdict": row.get("verdict") or "unreviewed",
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def get_scene_review(owner: str, scene_id: str, artifact_version: int = 0) -> dict | None:
    rows = run(
        """
        MATCH (r:scene_review {owner: $owner, sceneId: $sceneId})
        WHERE $artifactVersion = 0 OR r.artifactVersion = $artifactVersion
        RETURN """ + _SCENE_REVIEW_RETURN + " ORDER BY r.artifactVersion DESC LIMIT 1",
        {"owner": owner, "sceneId": scene_id, "artifactVersion": int(artifact_version or 0)},
    )
    return _scene_review_view(rows[0]) if rows else None


def upsert_scene_review(
    owner: str,
    scene: dict,
    *,
    score: int | None,
    tags: list[str],
    comment: str,
    verdict: str,
    artifact_version: int = 0,
) -> dict:
    """사용자·scene·artifactVersion당 현재 리뷰 한 건과 revision 이력을 저장한다."""
    import json as _json

    version = int(artifact_version or scene.get("artifactVersion") or 0)
    now = _now()
    review_id = uuid.uuid4().hex
    history_id = uuid.uuid4().hex
    rows = run(
        """
        MATCH (u:user {username: $owner}), (s:video_scene {id: $sceneId, owner: $owner})
        MERGE (r:scene_review {owner: $owner, sceneId: $sceneId,
                               artifactVersion: $artifactVersion})
        ON CREATE SET r.id = $id, r.projectId = $projectId,
                      r.revision = 0, r.createdAt = $now
        SET r.revision = coalesce(r.revision, 0) + 1,
            r.score = $score, r.tagsJson = $tagsJson, r.comment = $comment,
            r.verdict = $verdict, r.updatedAt = $now
        MERGE (u)-[:wrote]->(r)
        MERGE (r)-[:reviews]->(s)
        CREATE (h:scene_review_revision {id: $historyId, reviewId: r.id,
                    artifactVersion: $artifactVersion, revision: r.revision,
                    score: $score, tagsJson: $tagsJson, comment: $comment,
                    verdict: $verdict, createdAt: $now})
        CREATE (r)-[:has_revision]->(h)
        RETURN """ + _SCENE_REVIEW_RETURN,
        {
            "owner": owner,
            "sceneId": scene["id"],
            "artifactVersion": version,
            "id": review_id,
            "historyId": history_id,
            "projectId": scene.get("projectId") or "",
            "score": score,
            "tagsJson": _json.dumps(tags, ensure_ascii=False),
            "comment": comment[:2000],
            "verdict": verdict,
            "now": now,
        },
    )
    if not rows:
        raise GraphError("영상 리뷰를 저장하지 못했습니다.")
    return _scene_review_view(rows[0])


def delete_memories_for_review(owner: str, review_id: str) -> int:
    rows = run(
        """
        MATCH (m:preference_memory {owner: $owner})
        WHERE m.sourceReviewId = $reviewId OR m.sourceReviewIdsJson CONTAINS $reviewId
        RETURN m.id AS id
        """,
        {"owner": owner, "reviewId": review_id},
    )
    for row in rows:
        run(
            "MATCH (m:preference_memory {id: $id, owner: $owner}) DETACH DELETE m",
            {"id": row.get("id"), "owner": owner},
        )
    return len(rows)


def delete_scene_review(owner: str, scene_id: str, artifact_version: int = 0) -> bool:
    review = get_scene_review(owner, scene_id, artifact_version)
    if not review:
        return False
    review_id = review["id"]
    # 리뷰에서 파생된 메모리를 먼저 제거한다. 리뷰만 지우면 다음 계획에 취향이 계속 남는다.
    delete_memories_for_review(owner, review_id)
    run(
        "MATCH (:scene_review {id: $reviewId})-[:has_revision]->(h:scene_review_revision) DETACH DELETE h",
        {"reviewId": review_id},
    )
    run(
        "MATCH (r:scene_review {id: $reviewId, owner: $owner}) DETACH DELETE r",
        {"reviewId": review_id, "owner": owner},
    )
    return True


_MEMORY_RETURN = """
    m.id AS id, m.owner AS owner, m.scope AS scope, m.projectId AS projectId,
    m.sceneId AS sceneId, m.text AS text, m.sourceReviewIdsJson AS sourceReviewIdsJson,
    m.sourceReviewId AS sourceReviewId, m.sourceReviewRevision AS sourceReviewRevision,
    m.structuredJson AS structuredJson, m.revision AS revision,
    m.createdAt AS createdAt, m.updatedAt AS updatedAt
"""


def _memory_view(row: dict) -> dict:
    import json as _json
    try:
        structured = _json.loads(row.get("structuredJson") or "{}")
        if not isinstance(structured, dict):
            structured = {}
    except (TypeError, ValueError):
        structured = {}
    return {
        "id": row.get("id") or "",
        "scope": row.get("scope") or "global",
        "projectId": row.get("projectId") or "",
        "sceneId": row.get("sceneId") or "",
        "text": row.get("text") or "",
        "sourceReviewIds": _load_list(row.get("sourceReviewIdsJson")),
        "sourceReviewId": row.get("sourceReviewId") or "",
        "sourceReviewRevision": int(row.get("sourceReviewRevision") or 0),
        "structured": structured,
        "revision": int(row.get("revision") or 1),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def list_preference_memories(
    owner: str, *, scope: str = "", project_id: str = "", scene_id: str = "", limit: int = 100
) -> list[dict]:
    clauses = ["m.owner = $owner"]
    params = {"owner": owner, "limit": max(1, min(int(limit), 100))}
    if scope:
        clauses.append("m.scope = $scope")
        params["scope"] = scope
    if project_id:
        clauses.append("m.projectId = $projectId")
        params["projectId"] = project_id
    if scene_id:
        clauses.append("m.sceneId = $sceneId")
        params["sceneId"] = scene_id
    rows = run(
        "MATCH (m:preference_memory) WHERE " + " AND ".join(clauses)
        + " RETURN " + _MEMORY_RETURN + " ORDER BY m.updatedAt DESC LIMIT $limit",
        params,
    )
    return [_memory_view(row) for row in rows]


def create_preference_memory(
    owner: str,
    *,
    scope: str,
    text: str,
    project_id: str = "",
    scene_id: str = "",
    source_review_ids=None,
    source_review_id: str = "",
    source_review_revision: int = 0,
    structured: dict | None = None,
) -> dict:
    import json as _json

    now = _now()
    memory_id = uuid.uuid4().hex
    rows = run(
        """
        MATCH (u:user {username: $owner})
        CREATE (m:preference_memory {id: $id, owner: $owner, scope: $scope,
                                     projectId: $projectId, sceneId: $sceneId,
                                     text: $text, sourceReviewIdsJson: $sourceReviewIdsJson,
                                     sourceReviewId: $sourceReviewId,
                                     sourceReviewRevision: $sourceReviewRevision,
                                     structuredJson: $structuredJson, revision: 1,
                                     createdAt: $now, updatedAt: $now})
        CREATE (u)-[:owns]->(m)
        RETURN """
        + _MEMORY_RETURN,
        {
            "owner": owner, "id": memory_id, "scope": scope,
            "projectId": project_id[:64], "sceneId": scene_id[:64],
            "text": text[:1200],
            "sourceReviewIdsJson": _json.dumps(source_review_ids or [], ensure_ascii=False),
            "sourceReviewId": source_review_id[:64],
            "sourceReviewRevision": int(source_review_revision or 0),
            "structuredJson": _json.dumps(structured or {}, ensure_ascii=False)[:4000],
            "now": now,
        },
    )
    if not rows:
        raise GraphError("제작 메모를 저장하지 못했습니다.")
    return _memory_view(rows[0])


def update_preference_memory(owner: str, memory_id: str, fields: dict) -> dict:
    updates = _filter_updates(
        fields,
        {"text", "scope", "projectId", "sceneId", "sourceReviewIdsJson", "structuredJson"},
        "제작 메모",
    )
    assignments = ", ".join(f"m.{key} = ${key}" for key in updates)
    rows = run(
        f"""
        MATCH (m:preference_memory {{id: $id, owner: $owner}})
        SET {assignments}, m.revision = coalesce(m.revision, 1) + 1, m.updatedAt = $now
        RETURN {_MEMORY_RETURN}
        """,
        {**updates, "id": memory_id, "owner": owner, "now": _now()},
    )
    if not rows:
        raise NotFoundError("제작 메모를 찾을 수 없습니다.")
    return _memory_view(rows[0])


def delete_preference_memory(owner: str, memory_id: str) -> bool:
    rows = run(
        "MATCH (m:preference_memory {id: $id, owner: $owner}) DETACH DELETE m RETURN 1 AS deleted",
        {"id": memory_id, "owner": owner},
    )
    return bool(rows)


def reset_preference_memories(owner: str, *, scope: str = "", project_id: str = "", scene_id: str = "") -> int:
    clauses = ["m.owner = $owner"]
    params = {"owner": owner}
    if scope:
        clauses.append("m.scope = $scope")
        params["scope"] = scope
    if project_id:
        clauses.append("m.projectId = $projectId")
        params["projectId"] = project_id
    if scene_id:
        clauses.append("m.sceneId = $sceneId")
        params["sceneId"] = scene_id
    # Neptune openCypher의 FOREACH/집계 DELETE 지원 범위가 버전별로 갈린다.
    # 먼저 id만 읽고 한 개씩 DETACH DELETE 한다. reset은 드문 관리 동작이라 안전성이
    # 한 번의 복잡한 쿼리보다 낫다.
    rows = run(
        "MATCH (m:preference_memory) WHERE " + " AND ".join(clauses) + " RETURN m.id AS id",
        params,
    )
    for row in rows:
        run(
            "MATCH (m:preference_memory {id: $id, owner: $owner}) DETACH DELETE m",
            {"id": row.get("id"), "owner": owner},
        )
    # 초기화 tombstone. 향후 비동기 재처리가 생겨도 이 시각 이전 리뷰가 메모리를
    # 되살리지 않도록 기준을 보존한다.
    run(
        """
        CREATE (x:preference_memory_reset {id: $id, owner: $owner, scope: $scope,
                    projectId: $projectId, sceneId: $sceneId, resetAt: $resetAt})
        """,
        {
            "id": uuid.uuid4().hex,
            "owner": owner,
            "scope": scope or "all",
            "projectId": project_id,
            "sceneId": scene_id,
            "resetAt": _now(),
        },
    )
    return len(rows)


def preference_context(owner: str, *, project_id: str = "", scene_id: str = "") -> list[dict]:
    """현재 요청보다 낮은 우선순위의 explicit memory만 돌려준다.

    score-only review는 여기에 절대 들어오지 않는다. 사용자가 리뷰에서 구체 의견을
    쓰고 '다음에도 반영'을 선택해 memory를 만든 경우만 반영한다.
    """
    memories = list_preference_memories(owner, limit=100)
    rank = {"global": 1, "project": 2, "scene": 3}
    selected = []
    for memory in memories:
        scope = memory["scope"]
        if scope == "global":
            selected.append(memory)
        elif scope == "project" and project_id and memory.get("projectId") == project_id:
            selected.append(memory)
        elif scope == "scene" and scene_id and memory.get("sceneId") == scene_id:
            selected.append(memory)
    return sorted(selected, key=lambda item: (rank.get(item["scope"], 0), item.get("updatedAt") or ""))


def delete_video_scene(scene_id: str) -> dict:
    """작업을 지우고 정리할 S3 키 목록을 돌려준다."""
    scene = get_video_scene(scene_id)
    if not scene:
        raise NotFoundError("장면 작업을 찾을 수 없습니다.")
    run("MATCH (s:video_scene {id: $id}) DETACH DELETE s", {"id": scene_id})
    logger.info("Video scene deleted id=%s", scene_id)
    keys = [scene.get("inputKey"), scene.get("analysisKey"), scene.get("planKey"),
            scene.get("finalKey"), scene.get("vttKey")]
    return {"keysToClean": [key for key in keys if key]}


# --------------------------------------------------------------------------- #
# 캐릭터 시트 (장면 영상화 전용) — 캐릭터 등록 정보
#
# v2(계획서 4절·6.7절): 탭 8종 중 "기본"·"외형"은 v1부터 있던 name·role·appearanceText로
# 남긴다(하위 호환 — 기존 E2E·클라이언트가 그대로 동작해야 한다). 나머지 탭(체형·성격·
# 특징·취향·관계)과 genreTags는 크기가 커서 정점에 못 두므로 `sheetKey`가 가리키는
# S3 sheet.json에 산다(video_scene의 planKey와 같은 이유). 모델링 진행 상태(6.7절)는
# 목록·게이트(6.8절)에서 바로 읽어야 하므로 정점 속성으로 둔다.
# --------------------------------------------------------------------------- #
MODEL_SOURCE_NONE = "none"
MODEL_SOURCE_UPLOAD = "upload"
MODEL_SOURCE_GENERATED = "generated"

_CHARACTER_SHEET_RETURN = """
    c.id AS id, c.owner AS owner, c.projectId AS projectId, c.name AS name,
    c.role AS role, c.appearanceText AS appearanceText,
    c.refImageKeysJson AS refImageKeysJson, c.castAnimeKey AS castAnimeKey,
    c.castLiveActionKey AS castLiveActionKey, c.castSheetKeysJson AS castSheetKeysJson,
    c.sheetKey AS sheetKey, c.sourceHash AS sourceHash,
    c.modelStatus AS modelStatus, c.modelSource AS modelSource,
    c.modelKey AS modelKey, c.modelTaskId AS modelTaskId,
    c.renderKeysJson AS renderKeysJson, c.measurementsJson AS measurementsJson,
    c.createdAt AS createdAt, c.updatedAt AS updatedAt
"""


def _character_sheet_view(row: dict) -> dict:
    import json as _json

    try:
        measurements = _json.loads(row.get("measurementsJson") or "{}")
        if not isinstance(measurements, dict):
            measurements = {}
    except (ValueError, TypeError):
        measurements = {}
    try:
        cast_sheet_keys = _json.loads(row.get("castSheetKeysJson") or "{}")
        if not isinstance(cast_sheet_keys, dict):
            cast_sheet_keys = {}
    except (ValueError, TypeError):
        cast_sheet_keys = {}

    return {
        "id": row.get("id"),
        "owner": row.get("owner"),
        "projectId": row.get("projectId") or "",
        "name": row.get("name") or "",
        "role": row.get("role") or "",
        "appearanceText": row.get("appearanceText") or "",
        "refImageKeys": _load_list(row.get("refImageKeysJson")),
        "castKeys": {
            "anime": row.get("castAnimeKey") or "",
            "live_action": row.get("castLiveActionKey") or "",
        },
        "castSheetKeys": cast_sheet_keys,
        "sheetKey": row.get("sheetKey") or "",
        "sourceHash": row.get("sourceHash") or "",
        # 6.2절 modelStatus: none | needs_data | generating | ready | failed.
        "modelStatus": row.get("modelStatus") or "none",
        "modelSource": row.get("modelSource") or MODEL_SOURCE_NONE,
        "modelKey": row.get("modelKey") or "",
        "modelTaskId": row.get("modelTaskId") or "",
        "renderKeys": _load_list(row.get("renderKeysJson")),
        "measurements": measurements,
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
    }


def create_character_sheet(
    owner: str,
    *,
    name: str,
    role: str = "",
    appearance_text: str = "",
    ref_image_keys: list[str] | None = None,
    project_id: str = "",
) -> dict:
    import json as _json

    character_id = uuid.uuid4().hex
    now = _now()
    rows = run(
        """
        MATCH (u:user {username: $owner})
        CREATE (c:character_sheet {id: $id, owner: $owner, projectId: $projectId,
                                   name: $name, role: $role, appearanceText: $appearanceText,
                                   refImageKeysJson: $refImageKeysJson,
                                   castAnimeKey: '', castLiveActionKey: '', castSheetKeysJson: '{}',
                                   sheetKey: '', sourceHash: '',
                                   modelStatus: 'none', modelSource: 'none', modelKey: '', modelTaskId: '',
                                   renderKeysJson: '[]', measurementsJson: '{}',
                                   createdAt: $now, updatedAt: $now})
        CREATE (u)-[:made]->(c)
        RETURN """
        + _CHARACTER_SHEET_RETURN,
        {
            "owner": owner,
            "id": character_id,
            "projectId": (project_id or "")[:64],
            "name": (name or "")[:80],
            "role": (role or "")[:40],
            "appearanceText": (appearance_text or "")[:2000],
            "refImageKeysJson": _json.dumps(ref_image_keys or [], ensure_ascii=False)[:4000],
            "now": now,
        },
    )
    if not rows:
        raise GraphError("캐릭터를 만들지 못했습니다.")
    logger.info("Character sheet created id=%s owner=%s name=%s", character_id, owner, name)
    return _character_sheet_view(rows[0])


def update_character_sheet(character_id: str, fields: dict) -> dict:
    allowed = {
        "name",
        "role",
        "appearanceText",
        "refImageKeysJson",
        "castAnimeKey",
        "castLiveActionKey",
        "castSheetKeysJson",
        "sheetKey",
        "sourceHash",
        "modelStatus",
        "modelSource",
        "modelKey",
        "modelTaskId",
        "renderKeysJson",
        "measurementsJson",
    }
    updates = _filter_updates(fields, allowed, "캐릭터")
    assignments = ", ".join(f"c.{key} = ${key}" for key in updates)
    rows = run(
        f"""
        MATCH (c:character_sheet {{id: $id}})
        SET {assignments}, c.updatedAt = $now
        RETURN {_CHARACTER_SHEET_RETURN}
        """,
        {**updates, "id": character_id, "now": _now()},
    )
    if not rows:
        raise NotFoundError("캐릭터를 찾을 수 없습니다.")
    return _character_sheet_view(rows[0])


def get_character_sheet(character_id: str) -> dict | None:
    rows = run(
        "MATCH (c:character_sheet {id: $id}) RETURN " + _CHARACTER_SHEET_RETURN, {"id": character_id}
    )
    return _character_sheet_view(rows[0]) if rows else None


def list_character_sheets(owner: str, project_id: str = "") -> list[dict]:
    if project_id:
        query = (
            "MATCH (c:character_sheet {owner: $owner, projectId: $projectId}) RETURN "
            + _CHARACTER_SHEET_RETURN
            + " ORDER BY c.createdAt DESC"
        )
        params = {"owner": owner, "projectId": project_id}
    else:
        query = (
            "MATCH (c:character_sheet {owner: $owner}) RETURN "
            + _CHARACTER_SHEET_RETURN
            + " ORDER BY c.createdAt DESC"
        )
        params = {"owner": owner}
    return [_character_sheet_view(row) for row in run(query, params)]


def delete_character_sheet(character_id: str) -> dict:
    character = get_character_sheet(character_id)
    if not character:
        raise NotFoundError("캐릭터를 찾을 수 없습니다.")
    run("MATCH (c:character_sheet {id: $id}) DETACH DELETE c", {"id": character_id})
    logger.info("Character sheet deleted id=%s", character_id)
    keys = list(character.get("refImageKeys") or [])
    keys += [key for key in character.get("castKeys", {}).values() if key]
    keys += [key for key in character.get("castSheetKeys", {}).values() if key]
    keys += list(character.get("renderKeys") or [])
    if character.get("modelKey"):
        keys.append(character["modelKey"])
    if character.get("sheetKey"):
        keys.append(character["sheetKey"])
    return {"keysToClean": keys}
