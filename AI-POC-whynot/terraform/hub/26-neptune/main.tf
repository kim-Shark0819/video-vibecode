/*
  Neptune — 사용자·프로젝트 그래프. DEV / 운영 **각각 하나씩** (사용자 지시).

  ## 왜 필요한가

  `NEPTUNE_ENDPOINT` 가 비어 있으면 `graph_store.is_configured()` 가 false 가 되고
  모든 질의가 `GraphUnavailable` 로 떨어진다. `bootstrap_if_needed` 가 그 예외를 잡아
  기동은 막지 않으므로(server.py:7671) **앱은 뜨고 `/api/health` 도 응답하지만 아무도
  로그인할 수 없다.** admin 계정도 그래프에 살기 때문에 우회 경로가 없다.

  ## 왜 두 개인가

  하나를 공유하면 DEV 시험이 운영 사용자·프로젝트 데이터에 섞인다. 계정과 결과물이
  들어가는 저장소라 되돌리기 어렵다. 비용을 더 내고 분리한다.

  ## 접근 통제

  앱이 SigV4 서명을 하지 않아 IAM 인증을 켤 수 없다(var 설명 참고). 그래서 통제는
  네트워크 한 겹이다. 인바운드는 **해당 클러스터의 보안그룹에서 8182 만** 열고,
  두 Neptune 은 서로 다른 SG 를 가져 DEV 파드가 운영 그래프에 닿지 못한다.
  origin 계정도 같은 방식이었다 (`infra/neptune-sg-ingress.json`).

  ## 인스턴스 수

  writer 1대다. Multi-AZ 읽기 복제는 두 배 비용이고 POC 목표(기능 검증)에 기여하지 않는다.
  스토리지는 3 AZ 에 6중 복제되므로 데이터 내구성은 인스턴스 수와 무관하다.
  가용성이 필요해지면 `aws_neptune_cluster_instance` 에 count 를 올린다.
*/

locals {
  common_tags = {
    Project   = var.project_name
    ManagedBy = "terraform"
    Component = "neptune"
    Source    = "AI-POC/terraform/hub/26-neptune"
  }
}

# --------------------------------------------------------------------------- #
# 서브넷 그룹
# --------------------------------------------------------------------------- #
resource "aws_neptune_subnet_group" "this" {
  for_each = var.targets

  name        = each.value.subnet_group_name
  description = "AI-POC graph private subnets (${each.key})"
  subnet_ids  = var.subnet_ids

  tags = { Name = each.value.subnet_group_name }
}

# --------------------------------------------------------------------------- #
# 보안그룹
# --------------------------------------------------------------------------- #
resource "aws_security_group" "neptune" {
  for_each = var.targets

  name        = each.value.security_group_name
  description = "AI-POC Neptune (${each.key}): allow ${var.neptune_port} from EKS cluster SG only"
  vpc_id      = var.vpc_id

  tags = { Name = each.value.security_group_name }

  # 이름 변경 시 참조 중인 규칙 때문에 삭제가 먼저 일어나 실패하는 것을 막는다.
  lifecycle {
    create_before_destroy = true
  }
}

# 규칙은 별도 리소스로 둔다. 인라인 블록으로 쓰면 콘솔에서 누가 규칙을 추가했을 때
# Terraform 이 그것을 모두 지우는 방향으로 계획을 세운다.
resource "aws_vpc_security_group_ingress_rule" "from_cluster" {
  for_each = var.targets

  security_group_id = aws_security_group.neptune[each.key].id
  description       = "openCypher from ${each.key} EKS pods"

  referenced_security_group_id = each.value.allowed_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = var.neptune_port
  to_port                      = var.neptune_port

  tags = { Name = "${each.value.security_group_name}-in-${var.neptune_port}" }
}

# egress 규칙을 만들지 않는다. Neptune 이 먼저 연결을 여는 경로가 없다.
# aws_security_group 을 규칙 없이 두면 provider 가 기본 all-egress 를 제거한다.

# --------------------------------------------------------------------------- #
# 클러스터
# --------------------------------------------------------------------------- #
resource "aws_neptune_cluster" "this" {
  for_each = var.targets

  cluster_identifier = each.value.cluster_identifier
  engine             = "neptune"
  engine_version     = var.engine_version
  port               = var.neptune_port

  neptune_subnet_group_name = aws_neptune_subnet_group.this[each.key].name
  vpc_security_group_ids    = [aws_security_group.neptune[each.key].id]

  # 사용자 비밀번호 해시가 들어간다. 저장 시 암호화는 생성 시점에만 켤 수 있다.
  storage_encrypted = true

  # false 여야 앱이 동작한다. 이유는 variables.tf 설명 참고.
  iam_database_authentication_enabled = var.iam_database_authentication_enabled

  backup_retention_period      = each.value.backup_retention_days
  preferred_backup_window      = "17:00-18:00"      # UTC = KST 02:00-03:00
  preferred_maintenance_window = "sun:18:00-sun:19:00"

  # 지울 때 마지막 스냅샷을 남긴다. 계정·프로젝트가 들어가는 저장소라
  # skip_final_snapshot=true 는 되돌릴 수 없는 삭제가 된다.
  skip_final_snapshot       = false
  final_snapshot_identifier = "${each.value.cluster_identifier}-final"

  deletion_protection = var.deletion_protection

  # 파라미터 그룹은 기본값을 쓴다. 앱은 openCypher HTTPS 만 사용한다.

  tags = {
    Name        = each.value.cluster_identifier
    Environment = each.key
  }
}

resource "aws_neptune_cluster_instance" "writer" {
  for_each = var.targets

  identifier         = "${each.value.cluster_identifier}-1"
  cluster_identifier = aws_neptune_cluster.this[each.key].id
  engine             = "neptune"
  instance_class     = each.value.instance_class

  # 프라이빗 서브넷이지만 명시한다. 기본값이 바뀌어도 퍼블릭이 되지 않게 한다.
  publicly_accessible = false

  # 마이너 버전 자동 업그레이드를 끈다. 유지보수 창에 조용히 버전이 올라가면
  # "어제까지 되던 것"이 바뀐 이유를 찾기 어렵다. 올릴 때는 engine_version 을 바꾼다.
  auto_minor_version_upgrade = false

  tags = {
    Name        = "${each.value.cluster_identifier}-1"
    Environment = each.key
  }
}
