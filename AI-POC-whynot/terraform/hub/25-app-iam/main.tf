/*
  앱 IRSA — 두 클러스터의 ai-poc/ai-poc-app ServiceAccount 가 맡을 IAM 역할.

  ## 왜 이 레이어가 필요한가

  렌더된 `k8s/service-account.yaml` 은 `eks.amazonaws.com/role-arn` 으로 역할을 가리키는데
  그 역할이 **계정에 존재하지 않았다.** 이 상태로 배포하면 파드는 정상 기동하고
  `/api/health` 도 200 을 준다. 실패는 S3·Bedrock 을 처음 부르는 순간에만 나타나고
  화면에는 "생성 실패"로만 보여서 원인이 멀다.

  ## 왜 역할이 두 개인가

  IRSA 신뢰 정책은 클러스터의 OIDC provider ARN 과 발급자 호스트에 묶인다. 두 클러스터의
  발급자가 다르므로 하나의 역할로 양쪽을 받을 수 없다.

      AI-POC-eks      oidc .../id/2A49105728911261AD4F240BFD9A8AB1   (IAM 등록됨)
      Dev-AI-POC-eks  oidc .../id/9761A0FCEF28DCFBC85E9C998F5FA37B   (IAM 미등록)

  ## 정책 본문을 여기서 다시 쓰지 않는 이유

  `AI-POC/infra/*.json.tmpl` 이 이 저장소의 단일 원본이다(가드레일). 같은 내용을 HCL 로
  옮겨 적으면 사본이 생기고, 한쪽만 고쳐서 배포 검증 스크립트가 보는 정책과 실제 정책이
  어긋난다. 그래서 `templatefile()` 로 **원본 템플릿을 그대로 읽는다.** 템플릿의
  `${VAR}` 표기가 Terraform 보간과 같아 변환 없이 쓸 수 있다.

  ## 범위

  이 레이어는 IAM 만 만든다. Neptune 은 별도 결정 대기 중이라 여기 없다.
  `NEPTUNE_ENDPOINT` 가 빈 값인 동안 앱은 기동하지만 로그인은 동작하지 않는다.
*/

locals {
  common_tags = {
    Project   = var.project_name
    ManagedBy = "terraform"
    Component = "app-iam"
    Source    = "AI-POC/terraform/hub/25-app-iam"
  }

  # AI-POC/infra 의 템플릿 경로. 이 root 는 AI-POC/terraform/hub/25-app-iam 이다.
  infra_dir = "${path.module}/../../../infra"

  # 영상 버킷이 없으면 콘텐츠 버킷으로 대체한다 (render.py add_derived 와 같은 규칙).
  s3_bucket_arn = "arn:aws:s3:::${var.s3_bucket}"
  s3_video_bucket_arn = (
    var.s3_video_bucket != ""
    ? "arn:aws:s3:::${var.s3_video_bucket}"
    : "arn:aws:s3:::${var.s3_bucket}"
  )

  # 클러스터 OIDC 발급자 URL / 호스트(스킴 제거). 신뢰 정책의 조건 키 접두어로 쓰인다.
  oidc_issuer = {
    for key, cluster in data.aws_eks_cluster.target :
    key => cluster.identity[0].oidc[0].issuer
  }
  oidc_host = {
    for key, issuer in local.oidc_issuer :
    key => replace(issuer, "https://", "")
  }

  # 등록된 provider ARN. 기존 것은 data, 새로 만든 것은 resource 에서 온다.
  oidc_provider_arn = merge(
    { for key, p in data.aws_iam_openid_connect_provider.existing : key => p.arn },
    { for key, p in aws_iam_openid_connect_provider.created : key => p.arn },
  )

  new_oidc_targets = setsubtract(
    toset(keys(var.targets)),
    var.existing_oidc_provider_targets,
  )
}

# --------------------------------------------------------------------------- #
# 대상 클러스터 (읽기 전용)
# --------------------------------------------------------------------------- #
# 클러스터 자체는 이 레이어가 관리하지 않는다. 이름이 틀리면 여기서 즉시 실패한다.
data "aws_eks_cluster" "target" {
  for_each = var.targets
  name     = each.value.cluster_name
}

# --------------------------------------------------------------------------- #
# OIDC provider
# --------------------------------------------------------------------------- #
data "aws_iam_openid_connect_provider" "existing" {
  for_each = var.existing_oidc_provider_targets
  url      = local.oidc_issuer[each.key]
}

data "tls_certificate" "oidc" {
  for_each = local.new_oidc_targets
  url      = local.oidc_issuer[each.key]
}

resource "aws_iam_openid_connect_provider" "created" {
  for_each = local.new_oidc_targets

  url = local.oidc_issuer[each.key]
  # EKS 의 IRSA 는 이 audience 하나만 쓴다.
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.oidc[each.key].certificates[0].sha1_fingerprint]

  tags = {
    Name    = "${var.targets[each.key].cluster_name}-oidc"
    Cluster = var.targets[each.key].cluster_name
  }

  # 지우면 그 클러스터의 모든 IRSA 역할이 한꺼번에 끊긴다.
  lifecycle {
    prevent_destroy = true
  }
}

# --------------------------------------------------------------------------- #
# 앱 역할
# --------------------------------------------------------------------------- #
resource "aws_iam_role" "app" {
  for_each = var.targets

  name = each.value.role_name
  # **ASCII 만 쓴다.** IAM 역할 description 은 [\u0009\u000A\u000D\u0020-\u007E\u00A1-\u00FF]
  # 만 받는다. 한글도 em dash 도 거절되고, 실패는 CreateRole 시점에 ValidationError 로 난다.
  # (같은 계정에서 aws_iam_policy 의 description 은 한글을 받아 주어 더 헷갈린다.)
  description = "AI-POC app pods (${var.k8s_namespace}/${var.app_service_account}) on ${each.value.cluster_name}"

  # 신뢰 정책은 infra/irsa-trust-policy.json.tmpl 이 단일 원본이다.
  assume_role_policy = templatefile("${local.infra_dir}/irsa-trust-policy.json.tmpl", {
    OIDC_PROVIDER_ARN   = local.oidc_provider_arn[each.key]
    OIDC_PROVIDER_HOST  = local.oidc_host[each.key]
    K8S_NAMESPACE       = var.k8s_namespace
    APP_SERVICE_ACCOUNT = var.app_service_account
  })

  tags = {
    Name    = each.value.role_name
    Cluster = each.value.cluster_name
  }
}

# --------------------------------------------------------------------------- #
# 정책
# --------------------------------------------------------------------------- #
resource "aws_iam_policy" "s3" {
  for_each = var.targets

  name        = each.value.s3_policy_name
  description = "AI-POC 앱 S3 접근 (users/*, _config/*) — ${each.value.cluster_name}"

  policy = templatefile("${local.infra_dir}/app-s3-policy.json.tmpl", {
    S3_BUCKET_ARN       = local.s3_bucket_arn
    S3_VIDEO_BUCKET_ARN = local.s3_video_bucket_arn
  })

  tags = { Name = each.value.s3_policy_name }
}

# `VerifyModelEntitlement` 문장이 있는 이유 (infra/app-bedrock-policy.json.tmpl).
#
# Anthropic 모델은 AWS Marketplace 구독으로 제공된다. Converse 호출마다 Bedrock 이
# **호출 주체의** 엔타이틀먼트를 확인하고, 그 확인에 `aws-marketplace:ViewSubscriptions`
# 가 필요하다. 없으면 이런 오류가 난다.
#
#   AccessDeniedException ... Converse: Model access is denied due to IAM user or
#   service role is not authorized to perform the required AWS Marketplace actions
#   (aws-marketplace:ViewSubscriptions, aws-marketplace:Subscribe)
#
# 이 오류는 "모델 액세스가 계정에 없다"로 읽히기 쉽지만 그렇지 않았다. 실측으로
# `AdministratorAccess` 사용자가 같은 모델(`global.anthropic.claude-haiku-4-5-...`)을
# Converse 로 호출해 성공했다. 구독은 이미 있고 **확인 권한만** 없던 것이다.
#
# `aws-marketplace:Subscribe` 는 넣지 않는다. 새 구독을 만드는 권한이라 과금을 발생시킬
# 수 있고, 앱이 스스로 구독할 이유가 없다. 새 모델을 쓰려면 관리자가 모델 액세스를 켠다.
# `ViewSubscriptions` 는 리소스 수준 권한을 지원하지 않아 Resource 가 `*` 다(읽기 전용).
resource "aws_iam_policy" "bedrock" {
  for_each = var.targets

  name        = each.value.bedrock_policy_name
  description = "AI-POC 앱 Bedrock 호출 (자기 계정 한정) — ${each.value.cluster_name}"

  policy = templatefile("${local.infra_dir}/app-bedrock-policy.json.tmpl", {
    AWS_ACCOUNT_ID = var.account_id
  })

  tags = { Name = each.value.bedrock_policy_name }
}

resource "aws_iam_role_policy_attachment" "s3" {
  for_each = var.targets

  role       = aws_iam_role.app[each.key].name
  policy_arn = aws_iam_policy.s3[each.key].arn
}

resource "aws_iam_role_policy_attachment" "bedrock" {
  for_each = var.targets

  role       = aws_iam_role.app[each.key].name
  policy_arn = aws_iam_policy.bedrock[each.key].arn
}
