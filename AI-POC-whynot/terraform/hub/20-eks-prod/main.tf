/*
  운영 클러스터 `AI-POC-eks` 의 노드그룹.

  ## 이 레이어의 현재 범위

  **노드그룹 하나만** 관리한다. 클러스터·애드온·IRSA 는 여기 없다.
  클러스터는 `data` 로만 읽는다 — 운영 컨트롤 플레인을 이 시점에 Terraform 밑으로
  넣을 이유가 없고, 넣으면 잘못된 plan 한 번의 여파가 훨씬 커진다.
  (IRSA 는 `hub/25-app-iam`, Neptune 은 `hub/26-neptune` 에 있다.)

  ## 왜 만들었나

  D-23 A안: 운영 파드 용량 부족을 노드 1대 추가로 해소한다. `desired_size` 1 -> 2.
  `max_size` 는 이미 2라 상한은 건드리지 않는다.

  가드레일이 "인프라 변경은 Terraform 으로만" 이므로 CLI 대신 이 레이어를 만들고
  기존 노드그룹을 `import` 한다. 이 작업은 Phase 3 의 노드그룹 관리로도 이어진다.

  ## 안전장치

  이 노드에는 argocd 와 gitlab-runner(둘 다 범위 밖)가 함께 올라가 있다. 노드그룹이
  **교체**되면 그것들이 함께 내려간다. 교체를 유발하는 것은 `ami_type`·`capacity_type`·
  `instance_types`·`disk_size`·`node_role_arn`·`subnet_ids` 불일치다. 그래서

    1. 모든 속성을 실물과 같은 값으로 변수 기본값에 박았다
    2. `prevent_destroy = true` 로 두어 **교체가 계획되면 apply 가 실패한다**

  즉 잘못 적어도 조용히 노드가 날아가지 않는다.
*/

locals {
  common_tags = {
    Project   = var.project_name
    ManagedBy = "terraform"
    Component = "eks-nodegroup"
    Source    = "AI-POC/terraform/hub/20-eks-prod"
  }
}

# 클러스터는 읽기만 한다. 이름이 틀리면 여기서 즉시 실패한다.
data "aws_eks_cluster" "prod" {
  name = var.cluster_name
}

import {
  to = aws_eks_node_group.prod
  # EKS 노드그룹의 import ID 형식은 `<클러스터>:<노드그룹>` 이다.
  id = "AI-POC-eks:AI-POC-eks-ng"
}

resource "aws_eks_node_group" "prod" {
  cluster_name    = data.aws_eks_cluster.prod.name
  node_group_name = var.nodegroup_name
  node_role_arn   = var.node_role_arn
  subnet_ids      = var.subnet_ids

  # --- 교체 유발 속성. 실물과 같아야 한다 ---
  ami_type       = var.ami_type
  capacity_type  = var.capacity_type
  instance_types = var.instance_types
  disk_size      = var.disk_size

  # 버전을 명시한다. 비워 두면 provider 가 최신을 원해 노드 교체를 제안한다.
  version         = var.kubernetes_version
  release_version = var.release_version

  labels = var.node_labels

  # --- 이번에 바꾸는 값 ---
  scaling_config {
    min_size     = var.min_size
    max_size     = var.max_size
    desired_size = var.desired_size
  }

  update_config {
    max_unavailable = var.max_unavailable
  }

  tags = {
    Name        = var.nodegroup_name
    Environment = "prod"
  }

  # 운영 노드그룹이다. 교체·삭제가 계획되면 apply 를 실패시킨다.
  # 이 노드에 argocd·gitlab-runner(범위 밖)가 함께 올라가 있다.
  lifecycle {
    prevent_destroy = true
  }
}
