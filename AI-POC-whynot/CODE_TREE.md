# 와이낫 웹툰 → 영상 AI — 코드 구조

기준: 2026-10-19 · 운영 배포본과 같은 커밋 (통합계정 GitLab awstech/ai main `fceb363`, 이미지 `ci-fceb3634`).

## 화면 흐름과 코드

| 화면 | 화면 코드 | 서버 코드 | 외부 모델 |
|---|---|---|---|
| ① 원작 입력 | `frontend/public/vpoc.js` | `vpoc/routes.py` → `vpoc/llm.py`, `vpoc/images.py` | Claude (Bedrock) |
| ② 확인 · 설정 | 〃 | `vpoc/routes.py`, `vpoc/compose.py` | Claude |
| ③ 계획 승인 | 〃 | `vpoc/seedream.py`, `vpoc/images.py` | Seedream 5.0 (BytePlus), SD3.5 (Bedrock) |
| ④ 영상 | 〃 | `vpoc/ark.py`, `vpoc/fal.py`, `vpoc/video.py`, `vpoc/regen.py`, `stitcher.py` | Seedance 2.0/2.5 (BytePlus), fal.ai 모델(Kling·Veo·Hailuo 등), Luma Ray2 (Bedrock) |

## 압축본에서 뺀 것

- `deploy/config.dev.env`, `deploy/config.prod.env` — 실제 계정 · 엔드포인트 값
- `infra/inventory/` — 계정 자원 스냅샷
- `docs/`, `CLAUDE.md` — 내부 작업 기록 · 작업 지침

비밀값(API 키 · 비밀번호 · 토큰)은 코드에 없고 모두 환경변수 / Kubernetes Secret 으로 주입된다.
코드 주석과 설정 기본값에 AWS 계정 ID · 내부 도메인 · 버킷 이름이 남아 있으므로 비공개(Private) 저장소를 권장한다.

## 전체 트리

```text
AI-POC-whynot/
├── app/  # 백엔드 (Python · Flask + gunicorn)
│   ├── src/  # 서버 소스
│   │   ├── fixtures/  # 목(mock) 응답
│   │   │   └── __init__.py
│   │   ├── roles/  # 문서 생성 역할 (vision · bible · writer · validator)
│   │   │   ├── __init__.py
│   │   │   ├── _json.py
│   │   │   ├── bible.py
│   │   │   ├── character_sheet.py
│   │   │   ├── validator.py
│   │   │   ├── vision.py
│   │   │   └── writer.py
│   │   ├── vpoc/  # ★ 현재 영상 기능 — 화면 ①~④ 서버
│   │   │   ├── __init__.py
│   │   │   ├── ark.py  # BytePlus Seedance 제출/조회 · 첫/끝 프레임 · 소리
│   │   │   ├── compose.py  # 영어 프롬프트 · 화풍 · 안전 문구 (모델 호출 없음)
│   │   │   ├── errors.py  # 오류 봉투 · 사용자에게 보일 오류 종류
│   │   │   ├── fal.py  # fal.ai 모델 카탈로그 · 등록 · 스키마 해석 · 큐 제출/조회
│   │   │   ├── images.py  # 업로드 정규화 · 분석 타일 · SD3.5 인물 그림
│   │   │   ├── ledger.py  # 비용 원장 (관리자 화면 집계)
│   │   │   ├── llm.py  # Claude 분석 · 콘티 · 장면 설명 · 모델 자동 대체 · 교차 계정 역할
│   │   │   ├── regen.py  # ④ 이 샷만 의견 반영해 다시 만들기
│   │   │   ├── routes.py  # ①~④ API · 백그라운드 준비/제출 · ④ 드라이버 스레드
│   │   │   ├── seedream.py  # BytePlus Seedream 키 장면 그림 · 필터 재시도
│   │   │   ├── settings.py  # 모든 설정값 (환경변수 → ConfigMap)
│   │   │   ├── store.py  # S3 키 규칙 · state.json 읽기/쓰기(잠금)
│   │   │   └── video.py  # Luma Ray2 (Bedrock 비동기) 경로
│   │   ├── vpoc2/  # 15초 영상 실험 경로 (명령 → 캐릭터 → 영상 → 리뷰)
│   │   │   ├── __init__.py
│   │   │   ├── compose.py
│   │   │   ├── errors.py
│   │   │   ├── images.py
│   │   │   ├── ledger.py
│   │   │   ├── llm.py
│   │   │   ├── mock.py
│   │   │   ├── routes.py
│   │   │   ├── service.py
│   │   │   ├── settings.py
│   │   │   ├── stitch.py
│   │   │   ├── store.py
│   │   │   └── video.py
│   │   ├── __init__.py
│   │   ├── account_guard.py  # 다른 AWS 계정이면 /health 외 전부 503
│   │   ├── documents.py  # 기획 문서 생성 (분석 · 각색 · 인물)
│   │   ├── gateway.py  # Bedrock 호출 단일 통로
│   │   ├── graph_store.py  # Neptune 접근 계층
│   │   ├── image_brief.py  # 참고 이미지 역할 판단 · 질문 생성
│   │   ├── image_validation.py  # 생성 이미지 바이트 검증
│   │   ├── model_capabilities.py  # 모델 수명주기 · 가격 근거
│   │   ├── model_registry.py  # 모델 카탈로그 (계층형)
│   │   ├── pdf_export.py  # 콘티표 · 대본 PDF
│   │   ├── project_tracks.py  # 와이낫 프로젝트 트랙 라우트
│   │   ├── prompt_boost.py  # 짧은 설명 → 자세한 설명
│   │   ├── prompt_layers.py  # 프롬프트 두 계층 관리
│   │   ├── prompts_story.py  # 역할별 system 프롬프트 · JSON 스키마
│   │   ├── providers.py  # 생성 요청 → Bedrock 페이로드 변환
│   │   ├── server.py  # Flask 진입점 · 인증/로그인 유지 · 프로젝트 API
│   │   ├── stitcher.py  # ffmpeg 영상 이어붙이기 · 소리 없는 클립 무음 맞춤
│   │   ├── story_context.py  # 역할 입출력 컨테이너
│   │   ├── story_models.py  # 역할 → 모델 매핑표
│   │   ├── story_router.py  # 역할 분배 (코드, LLM 아님)
│   │   ├── styles.py  # 그림체 프리셋 · 모델 라우팅
│   │   ├── toon_convert.py  # 웹툰 컷 화풍 변환 (첫 프레임)
│   │   └── video_prompt.py  # 영상 프롬프트 조립
│   ├── Dockerfile
│   └── requirements.txt
├── deploy/  # 렌더링 · 배포 · 백업 · 검증 스크립트
│   ├── verify/  # API · 화면 · 권한 검증
│   │   ├── common.ps1
│   │   ├── fixtures.json
│   │   ├── try-documents.py
│   │   ├── verify-credentials.ps1
│   │   ├── verify-documents-api.ps1
│   │   ├── verify-layout.ps1
│   │   ├── verify-models-usage.ps1
│   │   ├── verify-projects-api.ps1
│   │   ├── verify-prompts.ps1
│   │   ├── verify-quota.ps1
│   │   ├── verify-styles-api.ps1
│   │   └── verify_merge_nodelete.py
│   ├── backup.ps1
│   ├── bastion.ps1
│   ├── config.env.example  # 설정 예시 (실제 값 파일은 포함하지 않음)
│   ├── deploy.ps1
│   ├── preflight.ps1
│   ├── render.py  # config.env + *.tmpl → 매니페스트
│   ├── schedules.ps1
│   ├── source-stamp.ps1
│   ├── start-all.ps1
│   └── stop-all.ps1
├── frontend/  # 화면 (정적 파일 · nginx)
│   ├── public/  # nginx 가 그대로 서빙하는 파일
│   │   ├── assets/
│   │   │   ├── brand/
│   │   │   │   ├── logo-white.svg
│   │   │   │   └── logo.svg
│   │   │   └── login/
│   │   │       ├── butterfly-effect-poster.jpg
│   │   │       └── butterfly-effect.mp4
│   │   ├── fonts/
│   │   │   ├── OFL.txt
│   │   │   └── PretendardVariable.woff2
│   │   ├── app.js  # 공통 앱 (로그인 · 프로젝트 · 화면 복원)
│   │   ├── index.html  # 앱 셸 · 로그인 화면
│   │   ├── runtime-config.js  # 기능 스위치 (배포 시 ConfigMap 이 덮어씀)
│   │   ├── styles.css
│   │   ├── vpoc-smoke.html
│   │   ├── vpoc-smoke.js
│   │   ├── vpoc.css  # 영상 화면 스타일
│   │   ├── vpoc.js  # ★ 영상 ①~④ 화면
│   │   ├── vpoc2.css
│   │   ├── vpoc2.html
│   │   └── vpoc2.js  # 15초 영상 실험 화면
│   ├── .dockerignore
│   ├── Dockerfile
│   └── nginx.conf  # 보안 헤더 · CSP · 캐시 정책
├── infra/  # IAM · S3 정책 템플릿 · 점검 스크립트
│   ├── app-bedrock-policy.json.tmpl
│   ├── app-s3-policy-dual-video.json.tmpl
│   ├── app-s3-policy.json.tmpl
│   ├── apply-ingress.sh.tmpl
│   ├── bastion-backup-policy.json
│   ├── build-deploy.sh
│   ├── ecr-lifecycle.json
│   ├── irsa-trust-policy.json.tmpl
│   ├── neptune-sg-ingress.json
│   ├── s3-encryption.json
│   ├── s3-lifecycle.json
│   ├── s3-tls-policy.json.tmpl
│   ├── status-check.sh
│   ├── verify-all.sh.tmpl
│   ├── verify-api.sh.tmpl
│   └── verify-manifests.sh.tmpl
├── k8s/  # 쿠버네티스 매니페스트 템플릿
│   ├── ingress.yaml.tmpl  # ALB Ingress (/api → app, / → frontend)
│   ├── model-catalog.yaml.tmpl
│   ├── namespace.yaml.tmpl
│   ├── service-account.yaml.tmpl  # IRSA 역할 연결
│   └── workloads.yaml.tmpl  # frontend · app Deployment/Service · 환경변수
├── k8s-gpu/  # 자체 GPU 영상 워커 매니페스트 (현재 미사용)
│   ├── device-plugin.yaml.tmpl
│   ├── model-download.yaml.tmpl
│   ├── storage.yaml.tmpl
│   └── video-worker.yaml.tmpl
├── terraform/  # IaC (bootstrap · hub · modules)
│   ├── bootstrap/
│   │   ├── .terraform.lock.hcl
│   │   ├── main.tf
│   │   ├── outputs.tf
│   │   ├── providers.tf
│   │   ├── terraform.tfvars.example
│   │   ├── variables.tf
│   │   └── versions.tf
│   ├── hub/
│   │   ├── 20-eks-prod/
│   │   │   ├── .terraform.lock.hcl
│   │   │   ├── backend.hcl.example
│   │   │   ├── backend.tf.example
│   │   │   ├── main.tf
│   │   │   ├── outputs.tf
│   │   │   ├── providers.tf
│   │   │   ├── terraform.tfvars.example
│   │   │   ├── variables.tf
│   │   │   └── versions.tf
│   │   ├── 25-app-iam/
│   │   │   ├── .terraform.lock.hcl
│   │   │   ├── backend.hcl.example
│   │   │   ├── backend.tf.example
│   │   │   ├── main.tf
│   │   │   ├── outputs.tf
│   │   │   ├── providers.tf
│   │   │   ├── terraform.tfvars.example
│   │   │   ├── variables.tf
│   │   │   └── versions.tf
│   │   ├── 26-neptune/
│   │   │   ├── .terraform.lock.hcl
│   │   │   ├── backend.hcl.example
│   │   │   ├── backend.tf.example
│   │   │   ├── main.tf
│   │   │   ├── outputs.tf
│   │   │   ├── providers.tf
│   │   │   ├── terraform.tfvars.example
│   │   │   ├── variables.tf
│   │   │   └── versions.tf
│   │   └── 40-video-gpu/
│   │       ├── .terraform.lock.hcl
│   │       ├── backend.hcl.example
│   │       ├── main.tf
│   │       ├── outputs.tf
│   │       ├── providers.tf
│   │       ├── terraform.tfvars.example
│   │       ├── variables.tf
│   │       └── versions.tf
│   ├── modules/
│   │   ├── video_gpu_nodegroup/
│   │   │   ├── main.tf
│   │   │   ├── outputs.tf
│   │   │   └── variables.tf
│   │   └── video_queue/
│   │       ├── main.tf
│   │       ├── outputs.tf
│   │       └── variables.tf
│   └── README.md
├── tools/  # CI 검사기 · E2E · 사전 점검
│   ├── check_vpoc.py  # CI 검사기 (check:whynot)
│   ├── vpoc_e2e.py
│   └── vpoc_preflight.py
├── video-worker/  # Wan 2.2 GPU 워커 (현재 미사용)
│   ├── src/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── inference.py
│   │   ├── vram.py
│   │   └── worker.py
│   ├── .dockerignore
│   ├── Dockerfile
│   └── requirements.txt
├── .dockerignore
├── .gitattributes
├── .gitignore
└── README.md
```
