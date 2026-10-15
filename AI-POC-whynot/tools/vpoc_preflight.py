"""vpoc1 Gate 0 preflight — PRD 부록 C 항목 C1~C10을 순서대로 검사한다.

표준 라이브러리 + boto3만 쓴다. 앱 코드를 import 하지 않으므로 파드 안에서
`python - --only C9,C10 < tools/vpoc_preflight.py` 형태로도 돌 수 있다.

워크스테이션에서는 --profile ai-poc-hub 를 붙인다. 파드 안(IRSA)에서는 붙이지 않는다.

C9(SD3.5 1장 0.08 USD)와 C10(Luma 1건 3.75 USD)만 유료다. 나머지는 무료다.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

VIDEO_MODEL = "luma.ray-v2:0"
IMAGE_MODEL = "stability.sd3-5-large-v1:0"
TEXT_MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
VIDEO_REGION = "us-west-2"
IMAGE_REGION = "us-west-2"
TEXT_REGION = "ap-northeast-2"

USD_PER_IMAGE = 0.08
USD_PER_VIDEO_540P_5S = 3.75

# PRD 6.5 형식. 약 1,100자로 맞춰 실효 프롬프트 길이를 함께 검증한다.
C10_PROMPT = (
    "Cinematic live-action film footage, photorealistic, natural skin texture, 35mm lens, "
    "soft natural light. A floodlit racing pit lane at night right after a race, wet asphalt "
    "reflecting the overhead lamps, rows of tool trolleys and coiled air hoses along the "
    "garage wall, thin haze of tyre smoke drifting through the beams. A tall man with short "
    "white hair and Western features, wearing a black-and-yellow racing suit with matching "
    "gloves and a fireproof collar, the suit scuffed at the elbows from a long stint. The "
    "white-haired racer pulls off his helmet with both hands, shakes out his damp hair, "
    "rolls his shoulders once and fixes a cold steady stare on someone standing off-screen "
    "to his left, breath visible in the cool night air. Background crew members move in soft "
    "focus behind him, carrying wheel guns and timing boards. Slow push-in, eye-level medium "
    "shot, shallow depth of field, warm practical lights behind the subject and cool ambient "
    "fill from the pit wall, subtle handheld float, film grain, natural motion blur on the "
    "moving crew, continuous single take with the framing settling on the face."
)


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def raw_error(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        meta = exc.response.get("ResponseMetadata", {})
        return json.dumps(
            {
                "code": err.get("Code", ""),
                "message": err.get("Message", ""),
                "requestId": meta.get("RequestId", ""),
                "httpStatus": meta.get("HTTPStatusCode", 0),
            },
            ensure_ascii=False,
        )
    return f"{type(exc).__name__}: {exc}"


class Preflight:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.session = (
            boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
        )
        self.results: list[dict] = []
        self.spent_usd = 0.0
        self.long = Config(read_timeout=300, connect_timeout=15, retries={"max_attempts": 0})

    # ---------- 결과 기록 ----------

    def record(self, cid: str, grade: str, ok: bool, detail, raw: str = "") -> None:
        self.results.append(
            {"id": cid, "grade": grade, "ok": ok, "detail": detail, "rawError": raw}
        )
        mark = "ok " if ok else "FAIL"
        print(f"[{mark}] {cid} ({grade}) {json.dumps(detail, ensure_ascii=False)[:400]}")
        if raw:
            print(f"       rawError: {raw[:600]}")

    def wants(self, cid: str) -> bool:
        return not self.args.only or cid in self.args.only

    # ---------- 개별 검사 ----------

    def c_model(self, cid: str, model_id: str, region: str) -> None:
        try:
            client = self.session.client("bedrock", region_name=region)
            details = client.get_foundation_model(modelIdentifier=model_id)["modelDetails"]
            self.record(
                cid,
                "P0",
                details.get("modelLifecycle", {}).get("status") == "ACTIVE",
                {
                    "modelId": details.get("modelId"),
                    "status": details.get("modelLifecycle", {}).get("status"),
                    "inferenceTypes": details.get("inferenceTypesSupported"),
                    "outputModalities": details.get("outputModalities"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            self.record(cid, "P0", False, {"modelId": model_id}, raw_error(exc))

    def c3_text(self) -> None:
        try:
            client = self.session.client(
                "bedrock-runtime", region_name=TEXT_REGION, config=self.long
            )
            started = time.time()
            response = client.converse(
                modelId=TEXT_MODEL,
                messages=[{"role": "user", "content": [{"text": "ping"}]}],
                inferenceConfig={"maxTokens": 5},
            )
            usage = response.get("usage", {})
            self.record(
                "C3",
                "P0",
                True,
                {
                    "modelId": TEXT_MODEL,
                    "region": TEXT_REGION,
                    "ms": int((time.time() - started) * 1000),
                    "inputTokens": usage.get("inputTokens"),
                    "outputTokens": usage.get("outputTokens"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            self.record("C3", "P0", False, {"modelId": TEXT_MODEL}, raw_error(exc))

    def c4_role(self) -> None:
        arn = self.args.role_arn
        note = "kubectl 로 SA annotation 조회. EKS API 가 사설 엔드포인트라 워크스테이션에서 불가하면 IAM 에서 확인한 ARN 을 인자로 받는다."
        if not arn:
            self.record("C4", "P0", False, {"note": note}, "role-arn 미지정")
            return
        try:
            name = arn.rsplit("/", 1)[-1]
            iam = self.session.client("iam")
            role = iam.get_role(RoleName=name)["Role"]
            trust = role.get("AssumeRolePolicyDocument", {})
            has_oidc = "oidc" in json.dumps(trust).lower()
            self.record(
                "C4",
                "P0",
                True,
                {"roleArn": role["Arn"], "webIdentityTrust": has_oidc, "note": note},
            )
        except Exception as exc:  # noqa: BLE001
            self.record("C4", "P0", False, {"roleArn": arn}, raw_error(exc))

    def c5_simulate(self) -> None:
        arn = self.args.role_arn
        if not arn:
            self.record("C5", "P0", False, {}, "role-arn 미지정")
            return
        video_bucket = self.args.bucket
        main_bucket = self.args.main_bucket
        checks = [
            ("bedrock:InvokeModel", f"arn:aws:bedrock:{VIDEO_REGION}::foundation-model/{VIDEO_MODEL}"),
            ("bedrock:InvokeModel", f"arn:aws:bedrock:{IMAGE_REGION}::foundation-model/{IMAGE_MODEL}"),
            ("bedrock:InvokeModel", f"arn:aws:bedrock:{TEXT_REGION}::foundation-model/{TEXT_MODEL.split('.', 1)[-1]}"),
            ("bedrock:GetAsyncInvoke", "*"),
            ("bedrock:ListAsyncInvokes", "*"),
            ("s3:PutObject", f"arn:aws:s3:::{video_bucket}/users/_vpoc1/x"),
            ("s3:GetObject", f"arn:aws:s3:::{video_bucket}/users/_vpoc1/x"),
            ("s3:ListBucket", f"arn:aws:s3:::{video_bucket}"),
            ("s3:PutObject", f"arn:aws:s3:::{main_bucket}/users/_vpoc1/x"),
            ("s3:GetObject", f"arn:aws:s3:::{main_bucket}/users/_vpoc1/x"),
        ]
        iam = self.session.client("iam")
        rows: list[dict] = []
        all_ok = True
        for action, resource in checks:
            # ListBucket 허용 문장에는 s3:prefix 조건이 붙어 있다. 컨텍스트 키를 주지 않으면
            # simulate 가 조건을 평가하지 못해 implicitDeny 로 답한다(실제 권한 문제가 아니다).
            context = (
                [
                    {
                        "ContextKeyName": "s3:prefix",
                        "ContextKeyValues": ["users/"],
                        "ContextKeyType": "string",
                    }
                ]
                if action == "s3:ListBucket"
                else []
            )
            try:
                out = iam.simulate_principal_policy(
                    PolicySourceArn=arn,
                    ActionNames=[action],
                    ResourceArns=[resource],
                    ContextEntries=context,
                )
                decision = out["EvaluationResults"][0]["EvalDecision"]
            except Exception as exc:  # noqa: BLE001
                decision = f"error:{raw_error(exc)[:200]}"
            ok = decision == "allowed"
            all_ok = all_ok and ok
            rows.append({"action": action, "resource": resource, "decision": decision})
        self.record("C5", "P0", all_ok, rows)

    def c6_video_bucket(self) -> None:
        bucket = self.args.bucket
        try:
            s3 = self.session.client("s3", region_name=VIDEO_REGION)
            location = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint") or "us-east-1"
            self.record(
                "C6",
                "P0",
                location == VIDEO_REGION,
                {"bucket": bucket, "region": location, "required": VIDEO_REGION},
            )
        except Exception as exc:  # noqa: BLE001
            self.record("C6", "P0", False, {"bucket": bucket}, raw_error(exc))

    def c7_main_bucket(self) -> None:
        bucket = self.args.main_bucket
        key = "users/_vpoc1/preflight.txt"
        try:
            s3 = self.session.client("s3", region_name="ap-northeast-2")
            body = f"vpoc1 preflight {now_iso()}\n".encode() + b"x" * 1024
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="text/plain",
                ServerSideEncryption="AES256",
            )
            got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            self.record(
                "C7", "P0", len(got) == len(body), {"bucket": bucket, "key": key, "bytes": len(got)}
            )
        except Exception as exc:  # noqa: BLE001
            self.record("C7", "P0", False, {"bucket": bucket, "key": key}, raw_error(exc))

    def c8_quota(self) -> None:
        try:
            client = self.session.client("service-quotas", region_name=VIDEO_REGION)
            found: list[dict] = []
            paginator = client.get_paginator("list_service_quotas")
            for page in paginator.paginate(ServiceCode="bedrock"):
                for quota in page.get("Quotas", []):
                    name = quota.get("QuotaName", "")
                    lowered = name.lower()
                    if "async" in lowered or "luma" in lowered or "ray" in lowered:
                        found.append({"name": name, "value": quota.get("Value")})
            # 실측: "On-demand model inference concurrent requests for Luma Ray V2" = 1
            # 쿼터가 1이면 동시 제출도 1이어야 한다. 쿼터를 못 읽으면 보수적으로 1.
            inflight = 1
            for item in found:
                lowered = item["name"].lower()
                if "luma" in lowered and "concurrent" in lowered and item["value"]:
                    inflight = max(1, min(4, int(item["value"])))
            self.record(
                "C8",
                "P1",
                True,
                {"matched": found[:20], "maxInflight": inflight},
            )
        except Exception as exc:  # noqa: BLE001
            self.record("C8", "P1", False, {"maxInflight": 2}, raw_error(exc))

    def c9_image(self) -> None:
        try:
            client = self.session.client(
                "bedrock-runtime", region_name=IMAGE_REGION, config=self.long
            )
            body = {
                "prompt": (
                    "Cinematic photograph, photorealistic, 35mm lens, soft natural light. "
                    "Waist-up portrait of a tall man with short white hair and Western features, "
                    "wearing a black-and-yellow racing suit, standing in a floodlit racing pit "
                    "lane at night, facing the camera, calm expression, sharp focus on the face."
                ),
                "negative_prompt": (
                    "text, watermark, logo, extra people, extra fingers, deformed hands, "
                    "blurry face, cropped head"
                ),
                "mode": "text-to-image",
                "aspect_ratio": "16:9",
                "output_format": "jpeg",
                "seed": 1837461,
            }
            started = time.time()
            response = client.invoke_model(modelId=IMAGE_MODEL, body=json.dumps(body))
            payload = json.loads(response["body"].read())
            self.spent_usd += USD_PER_IMAGE
            reasons = payload.get("finish_reasons") or [None]
            images = payload.get("images") or []
            raw_image = base64.b64decode(images[0]) if images else b""
            self.record(
                "C9",
                "P0",
                reasons[0] is None and bool(raw_image),
                {
                    "modelId": IMAGE_MODEL,
                    "ms": int((time.time() - started) * 1000),
                    "finishReasons": reasons,
                    "seeds": payload.get("seeds"),
                    "bytes": len(raw_image),
                    "usd": USD_PER_IMAGE,
                },
            )
        except Exception as exc:  # noqa: BLE001
            self.record("C9", "P0", False, {"modelId": IMAGE_MODEL}, raw_error(exc))

    def c10_video(self) -> None:
        bucket = self.args.bucket
        stamp = now_stamp()
        prefix = f"users/_vpoc1/preflight/{stamp}"
        token = f"preflight-{stamp}"
        try:
            runtime = self.session.client(
                "bedrock-runtime", region_name=VIDEO_REGION, config=self.long
            )
            started = time.time()
            submitted = runtime.start_async_invoke(
                clientRequestToken=token,
                modelId=VIDEO_MODEL,
                modelInput={
                    "prompt": C10_PROMPT,
                    "aspect_ratio": "16:9",
                    "duration": "5s",
                    "resolution": "540p",
                    "loop": False,
                },
                outputDataConfig={"s3OutputDataConfig": {"s3Uri": f"s3://{bucket}/{prefix}"}},
            )
            arn = submitted["invocationArn"]
            self.spent_usd += USD_PER_VIDEO_540P_5S
            print(f"       C10 submitted arn={arn} promptChars={len(C10_PROMPT)}")
        except Exception as exc:  # noqa: BLE001
            self.record(
                "C10",
                "P0",
                False,
                {"phase": "start_async_invoke", "promptChars": len(C10_PROMPT)},
                raw_error(exc),
            )
            return

        status = "InProgress"
        failure = ""
        deadline = time.time() + 900
        while time.time() < deadline:
            time.sleep(10)
            try:
                got = runtime.get_async_invoke(invocationArn=arn)
            except Exception as exc:  # noqa: BLE001
                self.record("C10", "P0", False, {"phase": "get_async_invoke", "arn": arn}, raw_error(exc))
                return
            status = got.get("status", "")
            failure = got.get("failureMessage", "") or ""
            print(f"       C10 status={status} elapsed={int(time.time() - started)}s")
            if status in ("Completed", "Failed"):
                break

        elapsed = int(time.time() - started)
        if status != "Completed":
            self.record(
                "C10",
                "P0",
                False,
                {"arn": arn, "status": status, "elapsedS": elapsed, "usd": USD_PER_VIDEO_540P_5S},
                failure or f"status={status}",
            )
            return

        s3 = self.session.client(
            "s3", region_name=VIDEO_REGION, config=Config(signature_version="s3v4")
        )
        keys: list[dict] = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append({"key": obj["Key"], "size": obj["Size"]})
        mp4 = next((item for item in keys if item["key"].lower().endswith(".mp4")), None)
        if not mp4:
            self.record(
                "C10",
                "P0",
                False,
                {"arn": arn, "elapsedS": elapsed, "prefix": prefix, "objects": keys},
                "출력 접두사에 .mp4 없음",
            )
            return

        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": mp4["key"]}, ExpiresIn=3600
        )
        # SigV4 presigned URL 은 메서드에 묶인다. get_object 로 서명한 URL 에 HEAD 를 보내면
        # SignatureDoesNotMatch 403 이 난다. 브라우저 <video> 도 GET 을 쓰므로 GET 으로 검사한다.
        head_status = 0
        head_type = ""
        try:
            request = urllib.request.Request(url, method="GET")
            request.add_header("Range", "bytes=0-0")
            with urllib.request.urlopen(request, timeout=30) as response:
                head_status = 200 if response.status in (200, 206) else response.status
                head_type = response.headers.get("Content-Type", "")
        except Exception as exc:  # noqa: BLE001
            head_type = f"{type(exc).__name__}: {exc}"

        self.record(
            "C10",
            "P0",
            head_status == 200,
            {
                "arn": arn,
                "status": status,
                "elapsedS": elapsed,
                "promptChars": len(C10_PROMPT),
                "outputKey": mp4["key"],
                "bytes": mp4["size"],
                "presignHeadStatus": head_status,
                "contentType": head_type,
                "usd": USD_PER_VIDEO_540P_5S,
            },
        )

    # ---------- 실행 ----------

    def run(self) -> dict:
        if self.wants("C1"):
            self.c_model("C1", VIDEO_MODEL, VIDEO_REGION)
        if self.wants("C2"):
            self.c_model("C2", IMAGE_MODEL, IMAGE_REGION)
        if self.wants("C3"):
            self.c3_text()
        if self.wants("C4"):
            self.c4_role()
        if self.wants("C5"):
            self.c5_simulate()
        if self.wants("C6"):
            self.c6_video_bucket()
        if self.wants("C7"):
            self.c7_main_bucket()
        if self.wants("C8"):
            self.c8_quota()
        if self.wants("C9"):
            self.c9_image()
        if self.wants("C10"):
            self.c10_video()

        p0_fail = [r["id"] for r in self.results if r["grade"] == "P0" and not r["ok"]]
        return {
            "at": now_iso(),
            "account": "730335451955",
            "profile": self.args.profile or "(IRSA)",
            "videoBucket": self.args.bucket,
            "mainBucket": self.args.main_bucket,
            "roleArn": self.args.role_arn,
            "gate0": "pass" if not p0_fail else "fail",
            "p0Failures": p0_fail,
            "spentUsd": round(self.spent_usd, 4),
            "checks": self.results,
        }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="vpoc1 Gate 0 preflight")
    parser.add_argument("--profile", default="")
    parser.add_argument("--role-arn", default="arn:aws:iam::730335451955:role/whynot-app-role")
    parser.add_argument("--bucket", default=os.getenv("VPOC_VIDEO_BUCKET", "whynot-video-730335451955-us-west-2"))
    parser.add_argument("--main-bucket", default=os.getenv("S3_BUCKET", "whynot-730335451955"))
    parser.add_argument("--only", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    args.only = [item.strip() for item in args.only.split(",") if item.strip()]

    report = Preflight(args).run()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"\nwrote {args.out}")
    print(f"\ngate0={report['gate0']} spentUsd={report['spentUsd']} p0Failures={report['p0Failures']}")
    return 0 if report["gate0"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
