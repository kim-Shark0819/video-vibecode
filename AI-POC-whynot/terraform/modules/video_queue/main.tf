terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

# ---------------------------------------------------------------------------
# 잡 큐 + DLQ
#
# 앱은 메시지를 넣고 즉시 반환하고 워커가 폴링한다. 백그라운드 스레드를 쓰지
# 않는다 — 파드 롤아웃 시 진행 중 작업이 끊기는 문제를 없애기 위한 것이다.
# GPU 잡은 수 분 단위라 이 문제가 더 크다.
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "dlq" {
  name = "${var.name}-dlq"

  # 실패 원인을 조사할 여유를 둔다
  message_retention_seconds = var.dlq_retention_seconds

  sqs_managed_sse_enabled = true

  tags = merge(var.tags, {
    Name = "${var.name}-dlq"
    Role = "dlq"
  })
}

resource "aws_sqs_queue" "main" {
  name = var.name

  # 최장 잡 시간의 2배. 이보다 짧으면 처리 중인 메시지가 다른 워커에게
  # 다시 보여 같은 잡이 두 번 돌아간다.
  visibility_timeout_seconds = var.visibility_timeout

  message_retention_seconds = var.retention_seconds

  # long polling — 빈 응답으로 API 호출을 태우지 않는다
  receive_wait_time_seconds = 20

  sqs_managed_sse_enabled = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = merge(var.tags, {
    Name = var.name
    Role = "main"
  })
}

# DLQ 쪽에서도 원본 큐를 알게 해 둔다. 재처리(redrive)를 콘솔에서 할 때 쓴다.
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.main.arn]
  })
}
