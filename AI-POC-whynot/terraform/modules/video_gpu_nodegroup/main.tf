terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

locals {
  nodegroup_name = "${var.name_prefix}-video-gpu"
}

# ---------------------------------------------------------------------------
# 노드 IAM 역할
#
# 앱 노드그룹 역할을 재사용하지 않는다. GPU 워커는 영상 잡 관련 S3 프리픽스와
# SQS 만 필요하고, 앱은 그보다 넓은 권한을 갖는다. 섞으면 권한 경계가 흐려진다.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "node_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${local.nodegroup_name}-node-role"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
  tags               = var.tags
}

# EKS 워커 노드에 필요한 최소 관리형 정책 집합.
# AmazonEKS_CNI_Policy 는 노드 역할에 붙인다(기존 Dev-AI-POC-eks-node-role 과 동일한 방식).
resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
  ])

  role       = aws_iam_role.node.name
  policy_arn = each.value
}

# ---------------------------------------------------------------------------
# GPU 노드그룹
# ---------------------------------------------------------------------------

resource "aws_eks_node_group" "this" {
  cluster_name    = var.cluster_name
  node_group_name = local.nodegroup_name
  node_role_arn   = aws_iam_role.node.arn

  # [하드 제약] g6e 계열은 ap-northeast-2a / 2b 에만 있다
  # (실측: describe-instance-type-offerings). 3-AZ 로 두면 2c/2d 에 배치된
  # 노드가 용량 없음으로 실패한다.
  subnet_ids = var.subnet_ids

  instance_types = [var.instance_type]

  # NVIDIA 드라이버와 nvidia-container-toolkit 이 포함된 AMI.
  # device plugin 은 이 AMI 에 없으므로 DaemonSet 으로 따로 넣는다.
  ami_type = "AL2023_x86_64_NVIDIA"

  # Spot 쿼터 L-3819A6DF 가 0 이다(실측). 온디맨드만 쓴다.
  capacity_type = "ON_DEMAND"

  disk_size = var.disk_size

  scaling_config {
    min_size = 0

    # 기본 0 — 유휴 시 GPU 비용이 0 이다.
    # 실제 기동은 KEDA + Cluster Autoscaler 가 한다.
    desired_size = 0

    max_size = var.max_size
  }

  # GPU 노드에는 GPU 워크로드만 올린다.
  taint {
    key    = "nvidia.com/gpu"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  labels = {
    workload    = "video-gpu"
    "gpu.model" = "l40s"
  }

  update_config {
    max_unavailable = 1
  }

  tags = merge(var.tags, {
    Name = local.nodegroup_name
  })

  lifecycle {
    # 오토스케일러나 스케줄이 내린 노드를 apply 가 되올리지 않게 한다.
    # 이것이 없으면 apply 한 번이 GPU 노드를 되살려 비용이 발생한다.
    ignore_changes = [scaling_config[0].desired_size]

    precondition {
      condition     = length(var.subnet_ids) <= 2
      error_message = "g6e 는 ap-northeast-2a/2b 에만 있습니다. 서브넷을 2개 이하로 지정하세요."
    }
  }

  depends_on = [aws_iam_role_policy_attachment.node]
}
