# Outputs (filled in as resources land in their respective files).

output "orchestrator_role_arn" {
  value = aws_iam_role.orchestrator.arn
}

output "scheduler_invoker_role_arn" {
  value = aws_iam_role.scheduler_invoker.arn
}

output "lambda_exec_role_arns" {
  value = { for k, r in aws_iam_role.lambda_exec : k => r.arn }
}

output "debounce_table_name" {
  value = aws_dynamodb_table.debounce.name
}

output "scheduler_group_name" {
  value = aws_scheduler_schedule_group.debounce.name
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.pipeline.arn
}

output "pipeline_bucket_name" {
  value = aws_s3_bucket.pipeline.bucket
}

output "eventbridge_rule_arn" {
  value = aws_cloudwatch_event_rule.s3_upload_debouncer.arn
}

output "lambda_function_names" {
  value = { for k, fn in aws_lambda_function.fn : k => fn.function_name }
}
