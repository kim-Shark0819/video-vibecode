terraform {
  # 1.10 이상: S3 backend 의 native lock(use_lockfile), import 블록.
  # 로컬 설치본은 1.14.3 이다.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # bootstrap 레이어가 만든 버킷을 쓴다. 값은 backend.hcl 로 넘긴다.
  #   terraform init -backend-config=backend.hcl
  # DynamoDB 테이블은 쓰지 않는다. use_lockfile(S3 조건부 쓰기)로 잠근다.
  backend "s3" {}
}
