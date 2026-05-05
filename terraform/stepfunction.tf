# State machine: letters_metadata_automation
#
# Definition is rendered from ../statemachine/workflow.asl.json via templatefile().
# That file uses ${pipeline_bucket} and ${bedrock_model_id} placeholders; everything
# else (JSONata expressions like "{% $states.input.foo %}") is left untouched.
#
# Lambda function names in the ASL have already been rewritten to the new
# kebab-case naming pattern (cursive-prepare-pages, etc.) — they line up
# with aws_lambda_function.fn[*].function_name from lambdas.tf.
#
# Logging: ENABLED here (was OFF in current AWS) — drives a dedicated log group.

resource "aws_cloudwatch_log_group" "step_function" {
  name              = "/aws/stepfunctions/letters_metadata_automation"
  retention_in_days = var.log_retention_days
}

resource "aws_sfn_state_machine" "pipeline" {
  name     = "letters_metadata_automation"
  type     = "STANDARD"
  role_arn = aws_iam_role.orchestrator.arn

  definition = templatefile(
    "${path.module}/../statemachine/workflow.asl.json",
    {
      pipeline_bucket  = var.pipeline_bucket_name
      bedrock_model_id = var.bedrock_model_id
    }
  )

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_function.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = false
  }

  # The SM references each Lambda — surface a clean dependency so destroys
  # tear down in the right order.
  depends_on = [aws_lambda_function.fn]
}
