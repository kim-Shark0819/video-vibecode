output "gpu_nodegroup_name" {
  description = "GPU 노드그룹 이름"
  value       = module.gpu_nodegroup.nodegroup_name
}

output "gpu_node_role_arn" {
  description = "GPU 노드 IAM 역할 ARN. access entry 등록에 쓴다."
  value       = module.gpu_nodegroup.node_role_arn
}

output "gpu_instance_type" {
  description = "적용된 GPU 인스턴스 타입"
  value       = var.gpu_instance_type
}

output "queue_urls" {
  description = "워커별 SQS 큐 URL"
  value       = { for k, m in module.queues : k => m.queue_url }
}

output "dlq_urls" {
  description = "워커별 DLQ URL. needs_review 조사에 쓴다."
  value       = { for k, m in module.queues : k => m.dlq_url }
}

output "video_worker_repository_url" {
  description = "워커 이미지 ECR URL"
  value       = aws_ecr_repository.video_worker.repository_url
}

output "bedrock_image_region" {
  description = <<-EOT
    키프레임 생성용 Bedrock 이미지 모델 리전.
    서울에 IMAGE 출력 모델이 없어 크로스 리전이다(실측 2026-09-09).
  EOT
  value       = var.bedrock_image_region
}

output "worker_env" {
  description = <<-EOT
    워커 컨테이너에 넣을 환경변수.

    NEPTUNE_ENDPOINT 는 26-neptune 레이어에서 받은 writer 엔드포인트다.
    BEDROCK_IMAGE_REGION 은 서울에 IMAGE 모델이 없어 us-west-2 다.
  EOT
  value = {
    NEPTUNE_ENDPOINT     = var.neptune_endpoint
    NEPTUNE_PORT         = tostring(var.neptune_port)
    BEDROCK_IMAGE_REGION = var.bedrock_image_region
    VIDEO_WORKER_IMAGE   = aws_ecr_repository.video_worker.repository_url
    QUEUE_CLIP_URL       = module.queues["clip"].queue_url
    QUEUE_EDIT_URL       = module.queues["edit"].queue_url
    QUEUE_KEYFRAME_URL   = module.queues["keyframe"].queue_url
    QUEUE_LORA_URL       = module.queues["lora"].queue_url
  }
}
