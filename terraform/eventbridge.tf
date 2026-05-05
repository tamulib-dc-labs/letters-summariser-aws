# EventBridge rule that fires the Debouncer on every S3 upload to input/.
# Pattern is faithful to the existing CursiveS3UploadDebouncer rule.

resource "aws_cloudwatch_event_rule" "s3_upload_debouncer" {
  name        = "cursive-s3-upload-debouncer"
  description = "Routes S3 Object Created events under input/ to the debouncer Lambda."
  state       = "ENABLED"

  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = {
        name = [var.pipeline_bucket_name]
      }
      object = {
        key = [{ prefix = "input/" }]
      }
    }
  })

  # The S3 bucket must have EventBridge notifications enabled before this rule
  # has anything to receive.
  depends_on = [aws_s3_bucket_notification.pipeline]
}

resource "aws_cloudwatch_event_target" "debouncer" {
  rule      = aws_cloudwatch_event_rule.s3_upload_debouncer.name
  target_id = "debouncer-target"
  arn       = aws_lambda_function.fn["debouncer"].arn
}

resource "aws_lambda_permission" "allow_eventbridge_debouncer" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["debouncer"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.s3_upload_debouncer.arn
}
