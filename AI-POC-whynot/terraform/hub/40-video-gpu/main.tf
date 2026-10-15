locals {
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
    Layer       = "40-video-gpu"
    Feature     = "gpu-video-generation"
  }

  # 환경을 맨 앞에 붙인다. dev-ai-poc-* / prd-ai-poc-*
  name_prefix = "${var.environment}-${var.project_name}"
}

# ---------------------------------------------------------------------------
# 기존 리소스는 data 로만 읽는다. 이 레이어가 관리하지 않는다.
#
# [하드 제약] 운영 클러스터 AI-POC-eks 에는 팀원의 ArgoCD·GitLab Runner 가
# 동작 중이다(Pod Identity 4건: argocd/argocd-server,
# argocd/argocd-application-controller, gitlab-runner/gitlab-runner,
# kube-system/aws-load-balancer-controller). 이 레이어는 클러스터 자체를
# 건드리지 않고 노드그룹·큐·ECR·IAM 만 더한다.
# ---------------------------------------------------------------------------

data "aws_eks_cluster" "target" {
  name = var.cluster_name
}

# ---------------------------------------------------------------------------
# 다른 레이어가 소유한 것 — 여기서 만들지 않는다
#
#   hub/25-app-iam   : OIDC provider, Bedrock·S3 IAM 정책
#   hub/26-neptune   : Neptune 클러스터(dev-ai-poc-graph / ai-poc-graph)와 그 SG
#
# 같은 계정에서 여러 세션이 동시에 작업한다. 리소스를 중복 생성하면
# DBClusterAlreadyExistsFault 처럼 apply 가 중간에 깨지고 고아 리소스가 남는다.
# 이 레이어의 소유 범위는 GPU 노드그룹 · 큐 · 워커 ECR · 워커 IAM 뿐이다.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GPU 노드그룹
# ---------------------------------------------------------------------------

module "gpu_nodegroup" {
  source = "../../modules/video_gpu_nodegroup"

  name_prefix   = local.name_prefix
  cluster_name  = data.aws_eks_cluster.target.name
  subnet_ids    = var.gpu_subnet_ids
  instance_type = var.gpu_instance_type
  max_size      = var.gpu_max_size
  disk_size     = var.gpu_disk_size
  tags          = local.common_tags
}

# ---------------------------------------------------------------------------
# 잡 큐 — 워커 종류별로 분리한다.
# 하나로 합치면 학습 잡이 추론을 막고, KEDA 가 어느 워커를 늘릴지 알 수 없다.
# ---------------------------------------------------------------------------

module "queues" {
  source = "../../modules/video_queue"

  for_each = var.queue_visibility_timeout

  name               = "${local.name_prefix}-video-${each.key}"
  visibility_timeout = each.value
  max_receive_count  = var.queue_max_receive_count
  tags               = local.common_tags
}

# ---------------------------------------------------------------------------
# 워커 이미지 저장소
#
# 수명주기 태그 패턴을 "*" 로 두지 않는다. backup-* 태그까지 개수 제한에
# 들어가 조용히 지워진 사고가 있었다. 반드시 "20" 접두로 좁힌다(R-20.3).
# 이미지가 크므로 유지 개수는 5개다(기존 앱은 10개).
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "video_worker" {
  name                 = "${local.name_prefix}-video-worker"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# EBS CSI 드라이버 애드온
#
# 모델 가중치(Wan 2.2 I2V-A14B 약 68GB)를 둘 곳이 필요하다. 선택지는 셋이었다.
#
#   인스턴스 스토어 250GB : 가장 빠르지만 노드 교체 시 소멸. scale-to-zero 를 켜면
#                          노드가 내려갈 때마다 68GB 를 다시 내려받는다
#   S3 + initContainer   : 매 기동마다 수십 GB 전송
#   EBS gp3 PVC          : 살아남는다. 대신 이 애드온이 선행이다
#
# EBS 를 고른다. 실측 결과 두 클러스터에 이 애드온이 없었다(애드온 4개뿐).
#
# 자격증명은 Pod Identity 를 쓴다. IRSA 를 쓰면 OIDC provider 에 묶이고 그것은
# 다른 레이어(25-app-iam)가 소유한다. Pod Identity 에이전트는 이미 설치돼 있어
# 레이어 간 결합 없이 끝난다.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ebs_csi_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${local.name_prefix}-ebs-csi-role"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_assume.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_eks_pod_identity_association" "ebs_csi" {
  cluster_name    = data.aws_eks_cluster.target.name
  namespace       = "kube-system"
  service_account = "ebs-csi-controller-sa"
  role_arn        = aws_iam_role.ebs_csi.arn

  tags = local.common_tags
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name = data.aws_eks_cluster.target.name
  addon_name   = "aws-ebs-csi-driver"

  # 충돌 시 우리 설정을 적용한다. 기존 설정이 없으므로 덮어쓸 것도 없다.
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  tags = local.common_tags

  depends_on = [aws_eks_pod_identity_association.ebs_csi]
}

# ---------------------------------------------------------------------------
# 워커 IAM
#
# 앱 역할을 재사용하지 않는다. 워커는 영상 잡 프리픽스와 큐만 필요하고,
# 앱은 그보다 넓은 권한을 갖는다. 섞으면 권한 경계가 흐려진다.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "worker_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "video_worker" {
  name               = "${local.name_prefix}-video-worker-role"
  assume_role_policy = data.aws_iam_policy_document.worker_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "video_worker" {
  statement {
    sid    = "VideoJobObjects"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]

    resources = [
      "arn:aws:s3:::${var.s3_bucket}/users/*/video-jobs/*",
      "arn:aws:s3:::${var.s3_bucket}/_cache/keyframes/*",
    ]
  }

  statement {
    sid    = "CharacterRefsReadOnly"
    effect = "Allow"

    # 레퍼런스는 읽기만 한다. 워커가 캐릭터 자료를 지울 이유가 없다.
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.s3_bucket}/users/*/characters/*"]
  }

  statement {
    sid       = "ListBucketScoped"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.s3_bucket}"]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["users/*/video-jobs/*", "_cache/keyframes/*"]
    }
  }

  statement {
    sid    = "JobQueues"
    effect = "Allow"

    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
      "sqs:SendMessage",
    ]

    resources = [for key, queue in module.queues : queue.queue_arn]
  }
}

resource "aws_iam_policy" "video_worker" {
  name        = "${local.name_prefix}-video-worker"
  description = "GPU 영상 워커. 영상 잡 프리픽스와 큐만."
  policy      = data.aws_iam_policy_document.video_worker.json
  tags        = local.common_tags
}

resource "aws_iam_role_policy_attachment" "video_worker" {
  role       = aws_iam_role.video_worker.name
  policy_arn = aws_iam_policy.video_worker.arn
}

resource "aws_eks_pod_identity_association" "video_worker" {
  cluster_name    = data.aws_eks_cluster.target.name
  namespace       = var.k8s_namespace
  service_account = "video-worker"
  role_arn        = aws_iam_role.video_worker.arn

  tags = local.common_tags
}

resource "aws_ecr_lifecycle_policy" "video_worker" {
  repository = aws_ecr_repository.video_worker.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "untagged 는 1일 후 삭제"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "20* 태그는 최근 5개만 유지. 패턴을 * 로 넓히지 않는다"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["20"]
          countType     = "imageCountMoreThan"
          countNumber   = 5
        }
        action = { type = "expire" }
      },
    ]
  })
}
