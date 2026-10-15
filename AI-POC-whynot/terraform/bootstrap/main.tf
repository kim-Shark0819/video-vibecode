/*
  tfstate 버킷.

  ## 왜 별도 root 인가
  다른 레이어의 state 를 담을 버킷이므로 자기 자신을 S3 backend 에 넣을 수 없다.
  이 root 만 로컬 state 로 1회 apply 하고, 그 뒤 다른 레이어가 이 버킷을 쓴다.

  ## DynamoDB 를 쓰지 않는 이유
  Terraform 1.10 부터 S3 backend 가 native lock(`use_lockfile = true`)을 지원한다.
  잠금 전용 테이블을 따로 두고 관리할 이유가 없어졌다.

  ## 이 버킷의 state 는 어디에 두는가
  로컬(`terraform.tfstate`)이다. `.gitignore` 가 막고 있다. 이 root 는 버킷 하나만
  만들고 이후 거의 건드리지 않으므로 로컬 state 로 충분하다. 잃어버려도 `import` 로
  되찾을 수 있다(버킷은 이름으로 식별된다).
*/

locals {
  # 계정·리전을 이름에 넣어 다른 계정/리전에서 같은 이름이 충돌하지 않게 한다.
  bucket_name = "${var.project_name}-tfstate-${var.account_id}-${var.region}"

  common_tags = {
    Project   = var.project_name
    ManagedBy = "terraform"
    Component = "tfstate"
    Source    = "AI-POC/terraform/bootstrap"
  }
}

resource "aws_s3_bucket" "tfstate" {
  bucket = local.bucket_name

  # state 버킷을 실수로 지우면 모든 레이어의 state 를 잃는다.
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name = local.bucket_name
  }
}

# 버저닝은 state 버킷의 유일한 복구 수단이다. 잘못된 apply 로 state 가 깨지면
# 이전 버전으로 되돌린다. 끄면 되돌릴 방법이 없다.
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

# state 에는 리소스 ID·ARN 이 들어간다. 비밀값을 넣지 않는 것이 원칙이지만
# provider 가 넣는 값까지 통제할 수 없으므로 저장 시 암호화한다.
resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# 소유권을 버킷 소유자로 고정한다. ACL 기반 접근을 아예 배제한다.
resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# HTTPS 가 아닌 요청을 거부한다. state 가 평문으로 오가는 경로를 막는다.
resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.tfstate.arn,
          "${aws_s3_bucket.tfstate.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.tfstate]
}

# 오래된 state 버전이 무한정 쌓이는 것을 막는다. 90일이면 되돌릴 일은 이미 끝났다.
# 현재 버전은 건드리지 않는다.
resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    id     = "expire-noncurrent-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}
