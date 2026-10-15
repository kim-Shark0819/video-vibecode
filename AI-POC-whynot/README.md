# AI-POC-whynot

> **고객 `whynot`(와이낫미디어) 디렉터리.** `AI-POC/`(정본)를 복사해서 만들었다
> (`docs/새-고객-추가.md` 절차). 이름표는 전부 `whynot`로 치환되어 있다
> (`deploy/config.env.example`: `K8S_NAMESPACE=whynot`, `ECR_APP_REPO=whynot-app` 등).
> 아래 본문은 정본 `AI-POC/`의 설명이 그대로 남아 있으니 계정·리소스 상태 서술은
> 참고만 하고, 실제 배포 상태는 `deploy/config.env.example`과 인프라 쪽 확인을 따른다.
>
> 이 디렉터리 고유 기능: 장면 영상화(Seedance 2.0) 모듈 — `app/src/scene_video/`.
> 붙였다 뗄 수 있는 기능이고, 나머지는 정본과 같다. 자세한 내용은
> `docs/scene-video-seedance.md` 참고.

# AI-POC

웹툰 IP를 드라마 영상으로 각색하는 사내 AI 도구. AWS 계정 `730335451955`, 리전 `ap-northeast-2`.

`toon-change-ai-pj`(계정 `061051247564`)에서 **실제로 동작하고 연결된 것만** 골라 옮긴 것이다.
GPU 관련 자산은 이관하지 않았다. 무엇을 왜 뺐는지는 아래 "옮기지 않은 것"에 적었다.

---

## 지금 상태

| 계층 | 상태 |
| --- | --- |
| VPC / 서브넷 / IGW / NAT / 라우트 | 있음 (기존 `AI-poc-vpc` 재사용) |
| S3 `ai-poc-730335451955` | 있음 (서울) |
| ECR `ai-poc-app`, `ai-poc-frontend` | 있음 (수명주기 정책 적용) |
| 배스천 `AI-POC-bastion` | 있음 (`i-0ca57a18e69b73a1f`, SSM Online) |
| EKS `AI-POC-eks` 컨트롤 플레인 | 있음 (ACTIVE, k8s 1.36, 프라이빗 엔드포인트) |
| **노드그룹** | **없음** |
| **관리형 애드온** | **없음** (vpc-cni / kube-proxy / coredns 가 자체 관리) |
| **ALB / Ingress** | **없음** |
| **Neptune** | **없음** |
| **us-west-2 영상 출력 버킷** | **없음** |

**아직 배포할 수 없다.** 노드가 없어 파드가 뜨지 않고, Neptune이 없어 로그인·프로젝트가 동작하지 않는다.

배포까지 남은 순서는 이렇다.

1. 관리형 애드온 4종 등록 (vpc-cni, kube-proxy, coredns, metrics-server)
2. 노드그룹 (t3.medium, private1/2/3). GPU 노드그룹은 만들지 않는다
3. AWS Load Balancer Controller
4. Neptune `AI-POC-graph` + `config.env`의 `NEPTUNE_ENDPOINT` / `NEPTUNE_SECURITY_GROUP` 채우기
5. IRSA 역할 `AI-POC-app-role` (`infra/irsa-trust-policy.json.tmpl`, `app-s3-policy.json.tmpl`, `app-bedrock-policy.json.tmpl`)
6. `deploy.ps1`

---

## 시작하기

값의 단일 원본은 `deploy/config.env`다. 이 파일만 고치면 계정을 옮길 수 있다.
소스 코드와 이 문서에는 계정 번호를 적지 않는다.

```powershell
# 계정 / 클러스터 / OIDC 확인. 실패하면 아무것도 하지 않는다.
powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/preflight.ps1

# 매니페스트 렌더링 (config.env + *.tmpl -> deploy/rendered/)
python AI-POC/deploy/render.py

# 빌드 + 배포 (노드그룹이 생긴 뒤에 쓴다)
powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/deploy.ps1
```

EKS API가 프라이빗 전용이라 로컬에서 `kubectl`로 직접 붙을 수 없다. 배스천을 경유한다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/bastion.ps1 -Script AI-POC/infra/status-check.sh
```

---

## 구성

```
AI-POC/
  app/                 Flask + Gunicorn API. 이미지 빌드 대상
  frontend/            Nginx + 정적 자산 (app.js, index.html, styles.css, Pretendard)
  k8s/                 *.tmpl 매니페스트 (namespace, service-account, workloads, ingress, model-catalog)
  infra/               IAM/S3 정책 템플릿, 배스천에서 도는 빌드·검증 스크립트
  deploy/              config.env(단일 원본), preflight, render, deploy, 운영 스크립트, verify/
  docs/구조분석.md      시스템 구조 분석 (2026-09-03)
```

`app/src`는 전부 옮겼다. 파일별 역할은 이렇다.

| 파일 | 역할 |
| --- | --- |
| `server.py` | 모든 HTTP 라우트. 이 프로젝트에서 가장 큰 파일 |
| `graph_store.py` | Neptune(openCypher) 접근. 사용자·프로젝트·산출물·한도 |
| `gateway.py` | **모든 Bedrock 호출의 유일한 경로.** 사용량 기록과 타임아웃을 여기서 보장한다 |
| `documents.py` | 산출물(분석·각색·로그라인·인물) 스키마와 프롬프트 |
| `model_registry.py` | 모델 카탈로그. 활성화 시점에 LEGACY·출력버킷 리전을 검사 |
| `providers.py` | 모델별 요청 형식 어댑터 (Luma, Stability, Nova 등) |
| `styles.py` | 그림체 프리셋 12종과 용도 등급 |
| `video_prompt.py` | 영상 프롬프트 조립. 이미지가 있으면 모션만, 없으면 전부 |
| `prompt_layers.py`, `prompt_boost.py` | 프롬프트 역할 계층과 보강 |
| `image_brief.py` | 참고 이미지 분석 (역할 판단, 첫 프레임 선정) |
| `toon_convert.py` | 화풍 변환 |
| `stitcher.py` | ffmpeg로 컷 이어 붙이기. 롱테이크가 쓴다 |
| `account_guard.py` | `EXPECTED_ACCOUNT_ID`와 다른 계정에서 기동되면 기능 라우트를 전부 503 |
| `wan_client.py` | 자체 GPU 추론 서버 클라이언트. **아래 설명 참고** |

### `wan_client.py`를 남긴 이유

GPU를 이관하지 않았는데도 이 파일은 옮겼다. `server.py`가 모듈 최상단에서
`wan_client`를 임포트하기 때문이다. 빼면 앱이 기동 자체를 못 한다(ImportError).

이 코드는 추론 서버 연결 실패를 **정상 경로로** 다룬다. `wan_client.health()`가
실패하면 화면이 "서버 연결 안 됨"을 이유와 함께 보여준다. 옛 계정에서도 GPU 노드가
매일 18시에 0으로 줄었으므로 이 동작은 원래부터 검증된 경로다.

`k8s/workloads.yaml.tmpl`의 `VIDEO_GEN_URL`도 같은 이유로 남겼다. 값을 비우면
연결 실패가 아니라 설정 오류가 된다.

---

## 옮기지 않은 것

### GPU / Wan 2.2 (사용자 지시로 제외)

| 대상 | 개수 | 이유 |
| --- | --- | --- |
| `video-gen/` 전체 | 8 | Wan 2.2 추론 서버 이미지와 패치. GPU 노드가 없다 |
| `infra/gpu-*.sh` | 9 | GPU 노드 진단·드라이버·스토리지·노드그룹 교체 |
| `infra/wan22-*.sh` | 17 | Wan 작업 로그·용량 탐색·가중치 검사 |
| `infra/video-gen-*.sh`, `build-video-gen.sh` | 3 | video-gen 빌드·스케일 |
| `k8s/gpu-device-plugin.yaml`, `gpu-storage.yaml`, `video-gen.yaml.tmpl`, `wan22-download-job.yaml` | 4 | GPU 노드와 모델 가중치 PVC |
| `deploy/deploy-video-gen.ps1` | 1 | video-gen 배포 |
| `deploy/bench/` | 3 | Wan 스테이지 시간 측정. GPU 노드 Ready가 아니면 exit 1 |
| `deploy/verify/verify-video-new.ps1`, `verify-wan-capacity.ps1` | 2 | Wan 경로 검증 |
| `benchmarks/` (워크스페이스 루트) | - | GPU 벤치 결과 |

### 도메인 / TLS

| 대상 | 이유 |
| --- | --- |
| `deploy/apply-route53.ps1` | 도메인과 호스팅 존이 없다. 80 포트로 ALB DNS에 직접 접속해 테스트한다 |

### 배스천 디스크 유틸

| 대상 | 이유 |
| --- | --- |
| `infra/bastion-disk-cleanup.sh`, `bastion-grow-root.sh` | video-gen 이미지(PyTorch+CUDA 20GB+)가 루트를 채워서 만든 것이다. 그 이미지를 빌드하지 않고 새 배스천은 30GB gp3다 |

### 문서

| 대상 | 이유 |
| --- | --- |
| `docs/architecture-for-slides.md` | 2026-08-14 기준이고 옛 계정·옛 URL·옛 버전(`20260814-v21`)을 사실로 적고 있다. `docs/구조분석.md`가 더 최신이다 |
| `docs/frontend-redesign/` (28개, 약 460KB) | 프론트엔드 재설계 **과정** 산출물. 결과는 이미 `frontend/public`에 들어가 있고 재설계는 완료됐다(`05-completed.md`). 코드에서 참조하는 곳이 없다 |
| 루트의 `kiro-*.md`, `NEW_영상제작_구축_작업지시서.md` | GPU 영상 품질·속도 계획서. GPU를 제외해 지금 실행할 수 없다 |
| `gitlab_정보.md`, `변호인_트리트먼트1.5고.pdf` | 액세스 토큰과 제3자 저작물. 원래 `.gitignore` 대상이다 |

---

## 이관하면서 고친 것

옛 폴더에는 계정을 옮기면 조용히 깨지는 하드코딩이 남아 있었다. 그대로 복사하면
배포 시점에 실패한다. 네 곳을 고쳤다.

**1. `infra/build-deploy.sh` — 저장소·Secret·Deployment 이름**

파일 맨 위 주석이 계정·리전 하드코딩을 문제로 지적하고 `:?`로 강제하고 있었는데,
정작 이름은 `toon-change-ai-pj-app` 그대로였다. AI-POC 계정에는 없는 저장소라 push가 실패한다.
`ECR_APP_REPO` / `ECR_FRONTEND_REPO` / `APP_SERVICE_ACCOUNT` / `AUTH_SECRET_NAME` /
`K8S_NAMESPACE`를 환경변수로 받게 바꿨다. `K8S_NAMESPACE`의 옛 이름 기본값도 없앴다.

**2. `infra/build-deploy.sh` — 없는 파일 apply**

`kubectl apply -f k8s/gpu-device-plugin.yaml`이 있었다. 그 파일을 빼서 `set -e`로 배포가 죽는다. 제거했다.

**3. `k8s/ingress.yaml.tmpl` — 443과 ACM**

`listen-ports`에 `{"HTTPS":443}`, `certificate-arn: ${ACM_CERTIFICATE_ARN}`,
`ssl-redirect: "443"`, `host: ${APP_DOMAIN}`이 있었다. 인증서와 도메인이 없는 계정에서는
빈 값이 렌더링되어 컨트롤러가 리스너를 만들지 못하고, `ssl-redirect` 때문에 80까지 함께 죽는다.
**80 리스너만** 남기고 host 규칙을 뺐다. 도메인이 생기면 되돌릴 세 줄을 파일 안에 주석으로 적어 두었다.

**4. `deploy/render.py` — 빈 영상 버킷**

`S3_VIDEO_BUCKET`이 비어 있으면 `arn:aws:s3:::`가 만들어진다. `app-s3-policy.json.tmpl`이
이 ARN을 5곳에서 쓰므로 IAM 정책이 `MalformedPolicyDocument`로 거절된다.
값이 없으면 서울 버킷 ARN으로 대체하게 했다. 그러면 `RestrictToProjectBucketsOnly`의
`NotResource`에도 서울 버킷만 남아 의도한 제한이 정확히 걸린다.

`k8s/*.tmpl`의 `toon-change-ai-pj-frontend` / `-auth` / `part-of` 라벨도 함께 변수로 바꿨다.
`python AI-POC/deploy/render.py --check`가 템플릿 13개에서 미해결 변수 0으로 통과한다.

---

## 알려진 제약

- **Bedrock 영상 생성이 동작하지 않는다.** 영상 모델은 서울에 없고 비동기 출력 버킷이
  모델과 같은 리전(us-west-2)에 있어야 한다. `config.env`의 `S3_VIDEO_BUCKET`이 빈 동안
  `/api/videos/generate`와 롱테이크는 쓸 수 없다.
- **ALB가 80만 열려 있어 로그인 자격증명이 평문으로 오간다.** 도메인과 ACM이 붙기 전까지는
  사내 테스트로만 쓰고 외부에 공유하지 않는다.
- **`deploy/schedules.ps1`, `start-all.ps1`, `stop-all.ps1`은 옛 계정의 스케줄 이름
  (`quick-test-*`)을 갖고 있다.** 이 계정에는 스케줄이 하나도 없어 지금은 동작하지 않는다.
  스케줄을 만들 때 이름을 함께 갱신해야 한다. `stop-all.ps1`은 노드그룹을 클러스터에서
  조회하므로 그 부분은 이름과 무관하게 맞는다.
- `deploy/verify/verify-image-brief.ps1`과 `verify-prompts.ps1`은 Wan 경로(`/api/v2/video/*`,
  `video:wan` 역할)도 함께 검사한다. 라우트는 살아 있으므로 검사 자체는 돌지만
  실제 생성은 추론 서버가 없어 실패한다.
