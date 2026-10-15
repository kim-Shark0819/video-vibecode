#!/bin/bash
# 이미지 빌드 -> ECR push -> 배포 -> 상태 확인
set -euo pipefail

# --- 계정·리전·클러스터는 호출자가 넘긴다 (하드코딩하지 않는다) ---
#
# 예전에는 `REG=061051247564.dkr.ecr...` 와 `--region ap-northeast-2 --name quick-test-eks` 가
# 이 파일에 박혀 있었다. config.env 가 "단일 원본"이라고 문서에 적혀 있었지만 실제로는
# 여기에 사본이 있었고, **다른 계정으로 옮기면 조용히 원래 계정으로 푸시한다.**
# 태그가 immutable 이라 실패하거나, 더 나쁘면 성공해서 잘못된 계정에 올라간다.
#
# `:?` 를 쓰는 이유: 기본값을 두면 값을 빠뜨린 것이 드러나지 않는다. 없으면 즉시 멈춘다.
ACCOUNT="${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID 필요 (deploy.ps1 이 config.env 에서 넘긴다)}"
REGION="${AWS_REGION:?AWS_REGION 필요}"
CLUSTER="${EKS_CLUSTER_NAME:?EKS_CLUSTER_NAME 필요}"

# 이름도 하드코딩하지 않는다. 위 주석이 계정·리전에 대해 말한 문제가 이름에도 똑같이 있었다.
# 저장소 이름이 `toon-change-ai-pj-app` 으로 박혀 있어서, AI-POC 계정으로 옮기면 없는
# 저장소에 push 하고 실패한다. NS 기본값도 옛 이름이라 파드를 엉뚱한 곳에서 찾았다.
NS="${K8S_NAMESPACE:?K8S_NAMESPACE 필요}"
APP_REPO="${ECR_APP_REPO:?ECR_APP_REPO 필요}"
FRONTEND_REPO="${ECR_FRONTEND_REPO:?ECR_FRONTEND_REPO 필요}"
# Deployment / Service / 라벨 이름은 workloads.yaml.tmpl 이 ${APP_SERVICE_ACCOUNT} 로 만든다.
# 여기서도 같은 값을 봐야 rollout status 와 파드 조회가 맞는다.
APP_NAME="${APP_SERVICE_ACCOUNT:?APP_SERVICE_ACCOUNT 필요}"
FRONTEND_NAME="${FRONTEND_DEPLOYMENT_NAME:-$NS-frontend}"
AUTH_SECRET="${AUTH_SECRET_NAME:-$NS-auth}"

export HOME=/root KUBECONFIG=/root/.kube/config AWS_DEFAULT_REGION="$REGION"

REG="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
TAG="${IMAGE_TAG:?IMAGE_TAG 필요}"
WORK=/tmp/toon-deploy
cd "$WORK"

echo "대상: 계정 $ACCOUNT / 리전 $REGION / 클러스터 $CLUSTER / 네임스페이스 $NS"

# **실제로 그 계정에 있는지 확인한다.** 자격증명이 다른 계정을 가리키면 여기서 멈춘다.
# 배스천 역할이 바뀌었거나 프로필이 섞였을 때 원래 계정에 푸시하는 것을 막는 마지막 관문이다.
CALLER=$(aws sts get-caller-identity --query Account --output text)
if [ "$CALLER" != "$ACCOUNT" ]; then
  echo "!! 계정 불일치: 기대 $ACCOUNT / 실제 $CALLER. 중단합니다."
  exit 3
fi

systemctl start docker
aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER" --kubeconfig "$KUBECONFIG" >/dev/null 2>&1
aws ecr get-login-password | docker login --username AWS --password-stdin "$REG" >/dev/null 2>&1

echo "--- JS syntax check ---"
docker run --rm -v "$WORK/frontend/public/app.js:/a.js:ro" node:22-alpine node --check /a.js && echo "app.js OK"

echo "--- build ---"
# 커밋 SHA(와 dirty 여부)를 이미지에 라벨로 박는다. 이 파이프라인은 로컬 워킹 디렉터리를
# 배포하므로 태그만으로는 어떤 소스가 들어갔는지 알 수 없다. 품질 실험 결과를 코드 버전과
# 연결하려면 이미지에서 되읽을 수 있어야 한다(F-24).
#   확인: docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
STAMP="${SOURCE_STAMP:-unknown}"
ARCHIVE="${SOURCE_ARCHIVE:-unknown}"
GITCTX="${SOURCE_GIT:-unknown}"
echo "소스 스탬프: $STAMP (아카이브 $ARCHIVE / 커밋맥락 $GITCTX)"
docker build -q \
  --build-arg "IMAGE_TAG=$TAG" \
  --build-arg "SOURCE_STAMP=$STAMP" \
  --build-arg "SOURCE_ARCHIVE=$ARCHIVE" \
  --build-arg "SOURCE_GIT=$GITCTX" \
  --label "org.opencontainers.image.revision=$STAMP" \
  --label "org.opencontainers.image.version=$TAG" \
  -t "$REG/$APP_REPO:$TAG" app
# ASSET_VERSION을 넘기지 않으면 자산 URL이 dev로 고정되어 브라우저 캐시가 갱신되지 않는다.
docker build -q --build-arg "ASSET_VERSION=$TAG" \
  --label "org.opencontainers.image.revision=$STAMP" \
  --label "org.opencontainers.image.version=$TAG" \
  -t "$REG/$FRONTEND_REPO:$TAG" frontend

echo "--- import smoke test ---"
docker run --rm -e EXPECTED_ACCOUNT_ID=000000000000 "$REG/$APP_REPO:$TAG" \
  python -c "from src import server, graph_store, providers; print('imports OK'); print('adapters:', providers.known_providers())"

# 롱테이크는 컨테이너 안에서 ffmpeg를 실행한다. 루트 파일시스템이 읽기 전용이라
# 런타임에 바이너리를 내려받는 방식은 실패한다. 휠에 들어 있는지 여기서 확인한다.
echo "--- ffmpeg smoke test ---"
docker run --rm --read-only --tmpfs /tmp "$REG/$APP_REPO:$TAG" \
  python -c "
from src import stitcher
import subprocess
path = stitcher.ffmpeg_path()
out = subprocess.run([path, '-version'], capture_output=True, text=True).stdout.splitlines()[0]
print('ffmpeg OK:', path)
print(out)
"

echo "--- push ---"
#
# ## 이미 있는 태그는 건너뛴다
#
# ECR 저장소가 IMMUTABLE 이라 같은 태그로 다시 push 하면 실패한다. `set -e` 때문에
# 그 실패가 **kubectl apply 전에 스크립트를 죽인다.** 즉 예전 동작에서는
# deploy.ps1 의 -AllowExistingTag 가 "이미지 푸시는 실패하고 매니페스트만 다시
# 적용됩니다" 라고 안내했지만 실제로는 아무것도 적용되지 않았다.
#
# 이 경로가 필요한 이유는 클러스터가 둘이라는 것이다. 같은 이미지를 DEV 와 운영에
# 올리려면 두 번째 배포는 push 없이 apply 만 해야 한다. 태그를 올려 두 번 빌드하면
# **두 클러스터가 서로 다른 이미지를 돌린다** — 같은 소스라도 바이트가 다르다.
#
# 빌드와 스모크 테스트는 건너뛰지 않는다. 두 번째 대상에서도 이미지가 실제로 만들어지고
# import·ffmpeg 가 통과하는지 확인할 값이 있다. 빌드된 로컬 이미지는 push 하지 않으므로
# ECR 에 있는 것(첫 배포본)이 그대로 두 클러스터에 쓰인다.
push_if_absent() {
  repo="$1"
  if aws ecr describe-images --repository-name "$repo" --image-ids "imageTag=$TAG" >/dev/null 2>&1; then
    echo "이미 있어 푸시 생략: $repo:$TAG (ECR 의 기존 이미지를 그대로 쓴다)"
    return 0
  fi
  docker push -q "$REG/$repo:$TAG"
}
push_if_absent "$APP_REPO"
push_if_absent "$FRONTEND_REPO"

echo "--- apply ---"
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/service-account.yaml
# GPU 노드를 두지 않기로 해서 k8s/gpu-device-plugin.yaml 을 이관하지 않았다.
# 예전에는 여기서 그 파일을 apply 했다. 없는 파일을 가리키면 set -e 로 배포가 죽는다.

# 인증 Secret. 예전에는 수동으로 만들어 두고 어디에도 기록이 없었다. 회전 방법도 없었다.
# config.env의 ADMIN_USERNAME/ADMIN_PASSWORD를 단일 원본으로 삼아 매 배포에 맞춘다.
# token-secret은 유지한다. 바꾸면 발급된 모든 세션이 즉시 무효가 된다.
echo "--- auth secret ---"
EXISTING_TOKEN=$(kubectl get secret "$AUTH_SECRET" -n "$NS" -o jsonpath='{.data.token-secret}' 2>/dev/null || true)
if [ -n "$EXISTING_TOKEN" ]; then
  TOKEN_SECRET=$(printf '%s' "$EXISTING_TOKEN" | base64 -d)
else
  TOKEN_SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '\n')
  echo "token-secret을 새로 만들었습니다."
fi
# --from-literal은 argv로 전달되어 특수문자가 셸에 해석되지 않는다.
kubectl create secret generic "$AUTH_SECRET" -n "$NS" \
  --from-literal=username="${ADMIN_USERNAME:?ADMIN_USERNAME 필요}" \
  --from-literal=password="${ADMIN_PASSWORD:?ADMIN_PASSWORD 필요}" \
  --from-literal=token-secret="$TOKEN_SECRET" \
  --from-literal=company-code="${DEFAULT_COMPANY_CODE:?DEFAULT_COMPANY_CODE 필요}" \
  --from-literal=seed-users="${SEED_USERS:-}" \
  --from-literal=user-quotas="${USER_QUOTAS:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/model-catalog.yaml
kubectl apply -f k8s/workloads.yaml
kubectl apply -f k8s/ingress.yaml

kubectl rollout status deployment/"$APP_NAME" -n "$NS" --timeout=300s
kubectl rollout status deployment/"$FRONTEND_NAME" -n "$NS" --timeout=300s

echo "--- pods ---"
kubectl get pods -n "$NS" --no-headers

echo "--- health (graph included) ---"
# 파드를 고르는 규칙이 두 번 틀렸다. 지금 형태가 세 번째다.
#
#   1) items[0] 을 그냥 썼다 -> 종료 중인 이전 파드를 골라 exec 가 NotFound
#   2) --field-selector=status.phase=Running 을 붙였다 -> **여전히 실패했다.**
#      종료 중인 파드는 phase 가 Running 그대로다. 쿠버네티스는 종료를
#      metadata.deletionTimestamp 로 표시하고 phase 는 건드리지 않는다.
#   3) deletionTimestamp 가 없고 Ready 조건이 True 인 파드만 고른다.
#
# 롤아웃 직후에는 새 파드와 종료 중인 옛 파드가 함께 보이므로 이 구분이 필요하다.
# 이 실패는 배포를 무작위로 실패시키는데, 실제 롤아웃은 성공한 상태여서 원인이 헷갈린다.
POD=$(kubectl get pods -n "$NS" -l app.kubernetes.io/name=$APP_NAME -o json \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
for item in data.get('items', []):
    meta = item.get('metadata', {})
    if meta.get('deletionTimestamp'):
        continue
    status = item.get('status', {})
    if status.get('phase') != 'Running':
        continue
    conditions = status.get('conditions') or []
    if any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in conditions):
        print(meta['name'])
        break
")
if [ -z "$POD" ]; then
  echo "!! Ready 상태인 앱 파드가 없습니다."
  kubectl get pods -n "$NS" -l app.kubernetes.io/name=$APP_NAME -o wide
  exit 1
fi
echo "  대상 파드: $POD"
kubectl exec -n "$NS" "$POD" -- python -c "
import json, urllib.request
print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=20)), ensure_ascii=False, indent=2))
"
echo "DEPLOY COMPLETE tag=$TAG"
