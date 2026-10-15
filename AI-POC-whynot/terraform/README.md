# Terraform (Hub 계정 전용)

계정 `730335451955` / 리전 `ap-northeast-2` / 프로필 `ai-poc-hub`.

고객사 배포용 재사용 모듈은 `AI-POC/terraform-modules/`에 따로 둔다. 이 폴더는 **이 계정의
root 모음**이다.

```
terraform/
├─ bootstrap/          tfstate 버킷 (로컬 state, 1회 apply)          [적용됨]
└─ hub/
   ├─ 10-network/      VPC AI-poc-vpc import + 기존 클러스터 data    [미착수]
   ├─ 20-eks-dev/      Dev-AI-POC-eks import (개발)                  [미착수]
   ├─ 20-eks-prod/     운영 노드그룹 import + desired_size           [적용됨]
   ├─ 25-app-iam/      앱 IRSA — 역할·정책·OIDC provider (dev/prod)  [적용됨]
   └─ 26-neptune/      Neptune 2개 (dev/prod 분리) + SG              [적용됨]
```

## 적용된 레이어 요약

| 레이어 | state key | 관리하는 것 |
|---|---|---|
| `20-eks-prod` | `hub/20-eks-prod.tfstate` | `AI-POC-eks-ng` **노드그룹만**. 클러스터는 `data` 로만 읽는다 |
| `25-app-iam` | `hub/25-app-iam.tfstate` | `{AI-POC,Dev-AI-POC}-app-role` + s3/bedrock 정책 4개 + Dev OIDC provider. **운영 OIDC provider 는 `data`** (albc 역할이 함께 쓰므로 관리 대상에서 뺀다) |
| `26-neptune` | `hub/26-neptune.tfstate` | `ai-poc-graph` · `dev-ai-poc-graph` + 서브넷그룹 2 + SG 2 + 8182 인그레스 2 |

IAM 정책과 IRSA 신뢰 정책의 본문은 HCL 로 옮겨 적지 않고 `AI-POC/infra/*.json.tmpl` 을
`templatefile()` 로 직접 읽는다. 사본을 만들면 배포 검증 스크립트가 보는 정책과 실제
정책이 어긋난다.

## 실행 시 주의 (실측으로 확인된 것)

- **`apply` 는 저장된 plan 파일로만.** `terraform plan "-out=tfplan.<layer>"` →
  `terraform apply "tfplan.<layer>"`. `-auto-approve` 는 금지 규칙이고, 없으면 대화형
  확인에서 멈춘다. 저장된 plan 은 보여준 것과 적용본이 같음도 보장한다
- **레이어마다 `TF_DATA_DIR` 를 명시**한다
  (`$env:TF_DATA_DIR = "$env:LOCALAPPDATA\terraform-data\ai-poc\<layer>"`).
  부모 프로세스가 엉뚱한 값을 주입하고 있어 그대로 두면 다른 레이어의 `.terraform` 을
  공유해 provider 캐시 불일치로 `validate` 가 실패한다
- PowerShell 5.1 은 `-out=x` 처럼 `=` 가 든 인수를 쪼개 `Too many command line arguments`
  를 낸다. **인수 전체를 따옴표로 감싼다**
- IAM **역할** `description` 에 한글·em dash 를 넣지 않는다. `CreateRole` 이
  `ValidationError` 로 거절한다 (정책 `description` 은 받아 주어 헷갈린다)
- **노드그룹 import 시 `release_version` 을 명시**한다. 비우면 provider 가 최신 AMI 를
  원해 plan 이 노드 교체를 제안한다

## 실행 순서

```powershell
# 0. 프로필 확인 — 이걸 빼면 엉뚱한 계정에 붙는다 (기본 프로필은 다른 계정이다)
aws sts get-caller-identity --profile ai-poc-hub

# 1. state 버킷 (1회)
cd terraform/bootstrap
terraform init
terraform plan          # 승인 후 apply
terraform apply

# 2. 각 레이어
cd ../hub/10-network
Copy-Item backend.hcl.example backend.hcl   # 값 확인
terraform init -backend-config=backend.hcl
terraform plan
```

## backend 를 나중에 붙이는 이유

state 버킷이 생기기 전에는 S3 backend 를 쓸 수 없다. 각 레이어는 `backend.tf.example` 로
두고, `bootstrap` apply 가 끝난 뒤 아래처럼 활성화한다.

```powershell
Copy-Item backend.tf.example backend.tf
Copy-Item backend.hcl.example backend.hcl
terraform init -backend-config=backend.hcl -migrate-state
```

## 규칙

- **`terraform destroy` 금지.** `-auto-approve` 금지
- `apply` 는 `plan` 을 사람에게 보여주고 승인받은 뒤에만
- import 한 리소스에는 `prevent_destroy` 를 건다
- 작업 범위는 VPC `vpc-0de9ff1693bd56f38` 내부. 범위 밖 리소스(`Metanet-GitLab*`,
  `AI-poc-ALB*`, `Grafana-vpc`, `eks-metaclouds`)는 건드리지 않는다
- provider 에 `allowed_account_ids` 를, root 마다 `account_guard` precondition 을 둔다 (이중 방어)
