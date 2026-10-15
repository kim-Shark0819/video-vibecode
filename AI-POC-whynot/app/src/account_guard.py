"""Hard guard that keeps this workload inside one AWS account.

The allowed account is supplied at deploy time through the EXPECTED_ACCOUNT_ID
environment variable, which the Deployment fills from deploy/config.env. No
account number is baked into this image, so moving to another account is a
configuration change only.

Any mismatch is treated as a fatal misconfiguration: the API keeps serving
/health so operators can see the failure, but every functional route is
refused. An unset EXPECTED_ACCOUNT_ID also fails closed rather than allowing
an unintended account.
"""

import logging
import os
import threading

import boto3
from botocore.exceptions import BotoCoreError, ClientError

EXPECTED_ACCOUNT_ID = os.getenv("EXPECTED_ACCOUNT_ID", "").strip()
AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or ""

logger = logging.getLogger("toon-change-ai-pj-app.account-guard")

_lock = threading.Lock()
_state = {"checked": False, "ok": False, "account": None, "detail": ""}


def _resolve_account() -> tuple[bool, str | None, str]:
    if not EXPECTED_ACCOUNT_ID:
        return (
            False,
            None,
            "EXPECTED_ACCOUNT_ID가 설정되지 않았습니다. deploy/config.env의 "
            "AWS_ACCOUNT_ID를 확인하고 매니페스트를 다시 렌더링해 배포해야 합니다.",
        )
    try:
        identity = boto3.client("sts", region_name=AWS_REGION or None).get_caller_identity()
    except (BotoCoreError, ClientError) as error:
        return False, None, f"STS 계정 확인에 실패했습니다: {type(error).__name__}"

    account = identity.get("Account")
    if account != EXPECTED_ACCOUNT_ID:
        return (
            False,
            account,
            f"허용되지 않은 AWS 계정입니다. 기대값 {EXPECTED_ACCOUNT_ID}, 실제 {account}",
        )
    return True, account, "ok"


def verify(force: bool = False) -> dict:
    """Return the cached guard state, resolving it on first use."""
    with _lock:
        if _state["checked"] and not force:
            return dict(_state)
        ok, account, detail = _resolve_account()
        _state.update({"checked": True, "ok": ok, "account": account, "detail": detail})
        if ok:
            logger.info("Account guard passed for %s", account)
        else:
            logger.error("Account guard blocked this workload: %s", detail)
        return dict(_state)


def is_allowed() -> bool:
    return verify()["ok"]


def state() -> dict:
    return verify()
