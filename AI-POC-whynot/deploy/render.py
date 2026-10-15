#!/usr/bin/env python3
"""config.env 값으로 .tmpl 매니페스트를 렌더링한다.

계정 종속 값(계정 번호, 리전, 인증서 ARN 등)은 템플릿에 ${VAR} 형태로만 존재하고,
실제 값은 config.env 한 곳에서만 관리한다.

클러스터가 둘이라(DEV / 운영) 일부 값은 대상마다 다르다. 공통은 config.env 에 한 번만 두고
다른 값만 config.<target>.env 로 덮어쓴다.

사용법:
    python AI-POC/deploy/render.py
    python AI-POC/deploy/render.py --check           # 미해결 변수만 검사
    python AI-POC/deploy/render.py --target dev      # config.dev.env 오버레이
    python AI-POC/deploy/render.py --target prod     # config.prod.env 오버레이
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "deploy" / "config.env"
OUTPUT_ROOT = PROJECT_ROOT / "deploy" / "rendered"
# k8s-gpu 를 k8s 와 나눈 이유: deploy.ps1 이 `rendered/k8s` 를 통째로 tar 에 담아
# 대상 클러스터에 적용한다. GPU 매니페스트를 그 안에 두면 GPU 노드가 없는 클러스터에도
# DaemonSet·PVC 가 생기고 파드가 영구 Pending 이 된다(원본 계정이 그 상태였다).
# 별도 디렉터리에 두고 GPU 가 있는 대상에만 명시적으로 적용한다.
TEMPLATE_DIRS = ("k8s", "k8s-gpu", "infra")
PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


def load_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"설정 파일이 없습니다: {path}")
    config: dict[str, str] = {}
    # utf-8-sig: 일부 에디터/도구가 이 파일들에 UTF-8 BOM 을 붙인다. 일반 utf-8 로 읽으면
    # 첫 줄 맨 앞에 BOM 문자가 남아 "# 주석" 이 "#␦ 주석" 처럼 보이지는 않지만
    # 첫 KEY=VALUE 줄(주석이 없는 파일이면) 앞에 붙어 파싱이 깨진다. BOM 이 없는
    # 파일에는 영향이 없다.
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"{path.name}:{lineno} KEY=VALUE 형식이 아닙니다: {raw}")
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()
    return config


def load_config_chain(target: str) -> dict[str, str]:
    """config.env 를 읽고, target 이 있으면 config.<target>.env 로 덮어쓴다.

    배포 대상이 두 클러스터(DEV / 운영)이고 다음 값이 서로 다르다.

        EKS_CLUSTER_NAME  EKS_OIDC_ID  EKS_CLUSTER_SECURITY_GROUP
        IAM_APP_ROLE_NAME  ALB_NAME

    config.env 를 두 벌 복사해 두면 공통 값(계정, 서브넷, 모델 카탈로그, 시드 계정)이
    두 곳에 생기고 한쪽만 고쳐서 조용히 어긋난다. 실제로 build-deploy.sh 가 그 방식으로
    계정·저장소 이름을 사본으로 갖고 있었고, 계정을 옮겼을 때 원래 계정으로 push 했다.
    그래서 공통은 한 곳에 두고 **다른 값만** 오버레이한다.

    오버레이에 config.env 에 없는 키가 있으면 실패시킨다. 오타로 새 키를 만들면
    아무 효과 없이 조용히 통과하고, 원래 값이 그대로 쓰인다.
    """
    config = load_config(CONFIG_PATH)
    if not target:
        return config
    overlay_path = CONFIG_PATH.parent / f"config.{target}.env"
    overlay = load_config(overlay_path)
    unknown = sorted(set(overlay) - set(config))
    if unknown:
        raise SystemExit(
            f"{overlay_path.name}: config.env 에 없는 키 {unknown}. "
            "오버레이는 기존 키를 덮어쓰기만 한다."
        )
    config.update(overlay)
    return config


def add_derived(config: dict[str, str]) -> dict[str, str]:
    account = config.get("AWS_ACCOUNT_ID", "")
    region = config.get("AWS_REGION", "")
    for key in ("AWS_ACCOUNT_ID", "AWS_REGION"):
        if not config.get(key):
            raise SystemExit(f"config.env에 {key}가 필요합니다.")

    # --- 이미지 레지스트리 계정 ---
    #
    # 보통은 앱이 도는 계정과 ECR 계정이 같다. 고객사 운영 배포는 다르다:
    # AWS_ACCOUNT_ID 는 고객 계정이어야 하고(account_guard 의 EXPECTED_ACCOUNT_ID, Bedrock 정책의
    # 계정 조건이 이 값을 쓴다) 이미지는 우리 계정 ECR 에서 교차 계정으로 당겨온다.
    # 두 값을 한 변수로 만들면 고객 계정에 없는 레지스트리 주소가 렌더된다.
    #
    # ECR_ACCOUNT_ID 가 비어 있으면 예전처럼 AWS_ACCOUNT_ID 를 쓴다. 개발계는 영향이 없다.
    ecr_account = config.get("ECR_ACCOUNT_ID", "").strip() or account
    registry = f"{ecr_account}.dkr.ecr.{region}.amazonaws.com"

    # 영상 출력 버킷은 us-west-2 에 있어야 하고(Bedrock 영상 모델이 서울에 없다) 이 계정에는
    # 아직 없다. 빈 값을 그대로 붙이면 `arn:aws:s3:::` 이 되어 IAM 정책이
    # MalformedPolicyDocument 로 거절된다. 실패 지점이 정책 적용 시점이라 원인이 멀다.
    #
    # 없을 때는 **서울 버킷 ARN 으로 대체한다.** 정책 Resource 목록에 같은 ARN 이 두 번
    # 들어가는 것은 합법이고, RestrictToProjectBucketsOnly 의 NotResource 도 서울 버킷만
    # 남아 의도한 제한이 정확히 된다. 버킷을 만들면 config.env 만 채우면 된다.
    video_bucket = config.get("S3_VIDEO_BUCKET", "")
    video_bucket_arn = (
        f"arn:aws:s3:::{video_bucket}" if video_bucket else f"arn:aws:s3:::{config['S3_BUCKET']}"
    )
    # Active I2V output and legacy read bucket can differ. The dedicated 25-app-iam
    # policy template consumes both derived ARNs; ordinary k8s manifests consume
    # only S3_VIDEO_BUCKET. Empty legacy keeps backwards-compatible single bucket.
    legacy_video_bucket = config.get("S3_VIDEO_LEGACY_BUCKET", "").strip() or video_bucket
    legacy_video_bucket_arn = (
        f"arn:aws:s3:::{legacy_video_bucket}" if legacy_video_bucket else f"arn:aws:s3:::{config['S3_BUCKET']}"
    )

    # 두 provider 레인을 동시에 운영할 때만 두 번째 버킷에 쓰기를 준다(사용자 승인
    # 2026-09-16). 기본은 읽기 전용이다 — 지난 결과물을 보존하는 것이 목적이고, 쓰기를
    # 상시로 열어 두면 그 보호가 사라진다.
    #
    # 스위치를 끌 때 Action 을 빈 배열로 두지 않는다. Action 이 없는 statement 는
    # MalformedPolicyDocument 다. 이미 허용된 GetObject 를 그대로 둬서 statement 를
    # 유효한 무효과 상태로 만든다(video ARN 이 없을 때 콘텐츠 버킷 ARN 으로 대체하는
    # 위 방식과 같은 요령이다).
    legacy_writable = str(config.get("S3_VIDEO_LEGACY_WRITABLE", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )
    secondary_write_actions = (
        '["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]'
        if legacy_writable
        else '["s3:GetObject"]'
    )

    # GPU 추론 서버(Wan 2.2)는 이 계정으로 이관하지 않았다. 저장소도 태그도 비어 있다.
    # k8s/video-gen.yaml.tmpl 을 함께 뺐으므로 이 값을 소비하는 템플릿은 없다.
    # 다시 들여올 때 조용히 `registry/:` 가 되지 않도록, 값이 없으면 눈에 보이게 표시한다.
    video_repo = config.get("ECR_VIDEO_GEN_REPO", "")
    video_tag = config.get("VIDEO_GEN_TAG", "")
    video_gen_image = (
        f"{registry}/{video_repo}:{video_tag}"
        if video_repo and video_tag
        else "VIDEO_GEN_NOT_CONFIGURED"
    )

    # --- ALB 리스너 / TLS ---
    #
    # 렌더러는 ${VAR} 치환만 하고 조건 분기가 없다. 그런데 인증서가 없는 계정에서
    # certificate-arn 을 빈 값으로 넣으면 컨트롤러가 리스너를 만들지 못하고,
    # ssl-redirect 까지 함께 있으면 80 도 죽는다(ingress.yaml.tmpl 주석의 사고).
    #
    # 그래서 "값이 비면 80만 연다"는 안전장치를 파생값으로 유지한다.
    # S3_VIDEO_BUCKET_ARN / VIDEO_GEN_IMAGE 가 쓰는 것과 같은 방식이다.
    #
    # 실측 2026-09-09: 두 도메인이 Route53 alias 로 각 ALB 를 이미 가리키고 있었고
    # ACM 에 *.meta-clouds.com 인증서(ISSUED)가 있었는데, Ingress 가 80 리스너만
    # 만들어 https:// 접속이 "사이트에 연결할 수 없음" 이었다. 443 에 아무것도
    # 듣고 있지 않으면 TCP 연결 자체가 되지 않는다.
    cert = config.get("ACM_CERTIFICATE_ARN", "").strip()
    domain = config.get("APP_DOMAIN", "").strip()

    if cert:
        listen_ports = '[{"HTTP":80},{"HTTPS":443}]'
        tls_lines = [f"alb.ingress.kubernetes.io/certificate-arn: {cert}"]
        if domain:
            # 도메인이 있을 때만 리다이렉트한다. ALB DNS 이름(*.elb.amazonaws.com)은
            # 이 인증서가 덮지 않으므로, 도메인 없이 리다이렉트를 켜면 ALB DNS 로
            # 하던 테스트가 인증서 불일치로 막힌다.
            tls_lines.append('alb.ingress.kubernetes.io/ssl-redirect: "443"')
        # 첫 줄의 들여쓰기는 템플릿이 준다. 이어지는 줄만 4칸을 붙인다.
        tls_annotations = "\n    ".join(tls_lines)
    else:
        listen_ports = '[{"HTTP":80}]'
        tls_annotations = (
            "# ACM_CERTIFICATE_ARN 이 비어 있어 80 리스너만 만든다. "
            "인증서를 채우면 443 이 함께 열린다."
        )

    # GPU 영상 워커 이미지. video_worker_repo/tag 가 비어 있으면(GPU 미배포 대상)
    # 이 값을 참조하는 k8s-gpu/*.tmpl 도 쓰이지 않으므로 문제되지 않는다.
    worker_repo = config.get("VIDEO_WORKER_REPO", "")
    worker_tag = config.get("VIDEO_WORKER_TAG", "")
    video_worker_image = (
        f"{registry}/{worker_repo}:{worker_tag}"
        if worker_repo and worker_tag
        else "VIDEO_WORKER_NOT_CONFIGURED"
    )

    derived = {
        "ALB_LISTEN_PORTS": listen_ports,
        "ALB_TLS_ANNOTATIONS": tls_annotations,
        "VIDEO_WORKER_IMAGE": video_worker_image,
        "ECR_REGISTRY": registry,
        "FRONTEND_IMAGE": f"{registry}/{config['ECR_FRONTEND_REPO']}:{config['IMAGE_TAG']}",
        "APP_IMAGE": f"{registry}/{config['ECR_APP_REPO']}:{config['IMAGE_TAG']}",
        "VIDEO_GEN_IMAGE": video_gen_image,
        "IAM_APP_ROLE_ARN": f"arn:aws:iam::{account}:role/{config['IAM_APP_ROLE_NAME']}",
        "OIDC_PROVIDER_HOST": f"oidc.eks.{region}.amazonaws.com/id/{config['EKS_OIDC_ID']}",
        "OIDC_PROVIDER_ARN": (
            f"arn:aws:iam::{account}:oidc-provider/"
            f"oidc.eks.{region}.amazonaws.com/id/{config['EKS_OIDC_ID']}"
        ),
        "S3_BUCKET_ARN": f"arn:aws:s3:::{config['S3_BUCKET']}",
        "S3_VIDEO_BUCKET_ARN": video_bucket_arn,
        "S3_VIDEO_ACTIVE_BUCKET_ARN": video_bucket_arn,
        "S3_VIDEO_LEGACY_BUCKET_ARN": legacy_video_bucket_arn,
        "S3_VIDEO_SECONDARY_WRITE_ACTIONS": secondary_write_actions,
    }
    merged = dict(config)
    merged.update(derived)
    return merged


def render_text(text: str, values: dict[str, str], origin: str) -> str:
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            missing.add(name)
            return match.group(0)
        return values[name]

    result = PLACEHOLDER.sub(replace, text)
    if missing:
        raise SystemExit(f"{origin}: config.env에 없는 변수 {sorted(missing)}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="파일을 쓰지 않고 검증만 수행")
    parser.add_argument(
        "--target",
        default="",
        help="config.<target>.env 오버레이를 적용한다 (dev | prod). 생략하면 config.env 만 쓴다.",
    )
    args = parser.parse_args()

    values = add_derived(load_config_chain(args.target))
    templates = sorted(
        path
        for directory in TEMPLATE_DIRS
        for path in (PROJECT_ROOT / directory).rglob("*.tmpl")
    )
    if not templates:
        raise SystemExit("렌더링할 .tmpl 템플릿을 찾지 못했습니다.")

    written = 0
    for template in templates:
        relative = template.relative_to(PROJECT_ROOT)
        rendered = render_text(template.read_text(encoding="utf-8"), values, str(relative))
        target = OUTPUT_ROOT / relative.parent / template.name[: -len(".tmpl")]
        if args.check:
            print(f"OK    {relative}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8", newline="\n")
        written += 1
        print(f"렌더링 {relative} -> {target.relative_to(PROJECT_ROOT)}")

    # 계정 종속 값이 없어 그대로 사용하는 파일도 함께 복사해 배포 묶음을 완성한다.
    if not args.check:
        for directory in TEMPLATE_DIRS:
            for path in sorted((PROJECT_ROOT / directory).rglob("*")):
                if path.is_dir() or path.suffix == ".tmpl":
                    continue
                relative = path.relative_to(PROJECT_ROOT)
                target = OUTPUT_ROOT / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                print(f"복사   {relative} -> {target.relative_to(PROJECT_ROOT)}")

    if args.check:
        print(f"\n템플릿 {len(templates)}개 검증 완료. 미해결 변수 없음.")
    else:
        print(f"\n템플릿 {written}개 렌더링 완료 -> {OUTPUT_ROOT.relative_to(PROJECT_ROOT)}")
        print(f"계정 {values['AWS_ACCOUNT_ID']} / 리전 {values['AWS_REGION']} 기준")
        # 렌더 결과는 대상마다 다르고 출력 경로는 하나다. 어느 대상으로 렌더된 트리인지
        # 눈에 보이게 남긴다. deploy.ps1 은 이 값 대신 실제 매니페스트 내용을 검증한다.
        print(
            "대상 {0} / 클러스터 {1} / ALB {2} / 앱 역할 {3}".format(
                args.target or "(오버레이 없음)",
                values["EKS_CLUSTER_NAME"],
                values["ALB_NAME"],
                values["IAM_APP_ROLE_NAME"],
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
