terraform {
  # 1.10 이상: S3 backend 의 native lock(use_lockfile), import 블록.
  # 로컬 설치본은 1.14.3 이다.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 이매지너스의 .terraform.lock.hcl 이 aws 6.63.0 이었다. 같은 major 로 맞춘다.
      version = "~> 6.0"
    }
  }
}
