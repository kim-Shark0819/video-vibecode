output "queue_url" {
  description = "메인 큐 URL"
  value       = aws_sqs_queue.main.id
}

output "queue_arn" {
  description = "메인 큐 ARN. IAM 정책과 KEDA 트리거에 쓴다."
  value       = aws_sqs_queue.main.arn
}

output "queue_name" {
  description = "메인 큐 이름"
  value       = aws_sqs_queue.main.name
}

output "dlq_url" {
  description = "DLQ URL. needs_review 조사에 쓴다."
  value       = aws_sqs_queue.dlq.id
}

output "dlq_arn" {
  description = "DLQ ARN"
  value       = aws_sqs_queue.dlq.arn
}
