output "state_bucket" {
  value       = aws_s3_bucket.tfstate.id
  description = "다른 레이어의 backend.hcl 에 넣을 버킷 이름"
}

output "state_bucket_arn" {
  value = aws_s3_bucket.tfstate.arn
}

output "backend_hcl" {
  description = "각 레이어의 backend.hcl 에 붙여 쓸 내용. key 만 레이어마다 바꾼다."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.tfstate.id}"
    key          = "hub/<layer>.tfstate"
    region       = "${var.region}"
    encrypt      = true
    use_lockfile = true
  EOT
}
