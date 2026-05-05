# IAM — three categories of identities:
#
#   1. orchestrator       (Step Functions assumes this; runs the state machine)
#   2. scheduler_invoker  (EventBridge Scheduler assumes this; calls states:StartExecution)
#   3. lambda_exec[name]  (each Lambda's execution role)
#
# Policies replace the over-permissioned managed policies (AmazonS3FullAccess, etc.)
# attached on the original AWS roles. Scope is now narrow: only the pipeline bucket,
# only the relevant Bedrock model + inference profile, only the Step Function ARN, etc.

############################
# Local helpers
############################

locals {
  # Every Lambda exec role gets these S3 permissions on the pipeline bucket.
  # All 15 Lambdas read/write under a single bucket — collapsing the
  # 3-5 overlapping inline policies the old roles each had.
  lambda_pipeline_bucket_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PipelineBucketObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.pipeline.arn}/*"
      },
      {
        Sid      = "PipelineBucketList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.pipeline.arn
      }
    ]
  })

  # Trust policies (assume-role) reused across resources.
  lambda_assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  # Map: short_name -> attributes used to generate per-Lambda role.
  # `extra_inline` = optional list of additional inline policy JSON snippets.
  lambda_roles = {
    debouncer            = {} # See aws_iam_role_policy.debouncer_extra below
    prepare_pages        = {}
    payload_generator    = {}
    result_parser        = {}
    merge_transcriptions = {}
    metadata_parser      = {}
    aat_lookup           = {}
    subject_validator    = {}
    reconciler           = {}
    reconcile_parser     = {}
    mods_formatter       = {}
    judge_parser         = {}
    validate_mods        = {} # See aws_iam_role_policy.validate_mods_ssm below
    final_assembler      = {}
    github_sync          = {} # No PutObject needed (read-only); kept broader for parity
  }
}

############################
# 1. Step Functions orchestrator role
############################

resource "aws_iam_role" "orchestrator" {
  name = "cursive-orchestrator-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Lambda invoke — only the 14 Lambdas the SM actually calls (no debouncer).
# The old role had AWSLambdaRole (invoke any Lambda) — replaced with explicit list.
resource "aws_iam_role_policy" "orchestrator_lambda_invoke" {
  name = "lambda-invoke"
  role = aws_iam_role.orchestrator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "lambda:InvokeFunction"
      Resource = [
        for name in [
          "prepare_pages", "payload_generator", "result_parser",
          "merge_transcriptions", "metadata_parser", "aat_lookup",
          "subject_validator", "reconciler", "reconcile_parser",
          "mods_formatter", "judge_parser", "validate_mods",
          "final_assembler", "github_sync"
        ] : "arn:aws:lambda:${var.region}:${local.account_id}:function:cursive-${replace(name, "_", "-")}"
      ]
    }]
  })
}

# Bedrock invoke on the inference profile + the foundation model it resolves to.
# (Inference profiles require both ARNs in the policy.)
resource "aws_iam_role_policy" "orchestrator_bedrock" {
  name = "bedrock-invoke"
  role = aws_iam_role.orchestrator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["bedrock:InvokeModel"]
      Resource = [
        var.bedrock_foundation_model_arn_pattern,
        "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/${var.bedrock_model_id}"
      ]
    }]
  })
}

# Textract — resource ARNs not supported, must be "*".
resource "aws_iam_role_policy" "orchestrator_textract" {
  name = "textract-analyze"
  role = aws_iam_role.orchestrator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["textract:AnalyzeDocument"]
      Resource = "*"
    }]
  })
}

# CloudWatch Logs — required for Step Functions logging (when enabled).
resource "aws_iam_role_policy" "orchestrator_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.orchestrator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogDelivery",
        "logs:GetLogDelivery",
        "logs:UpdateLogDelivery",
        "logs:DeleteLogDelivery",
        "logs:ListLogDeliveries",
        "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies",
        "logs:DescribeLogGroups"
      ]
      Resource = "*"
    }]
  })
}

############################
# 2. EventBridge Scheduler invoker role
############################

resource "aws_iam_role" "scheduler_invoker" {
  name = "cursive-scheduler-invoker-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_invoker_start_execution" {
  name = "start-state-machine"
  role = aws_iam_role.scheduler_invoker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "states:StartExecution"
      Resource = aws_sfn_state_machine.pipeline.arn
    }]
  })
}

############################
# 3. Per-Lambda execution roles (15 roles)
############################

resource "aws_iam_role" "lambda_exec" {
  for_each           = local.lambda_roles
  name               = "cursive-${replace(each.key, "_", "-")}-role"
  assume_role_policy = local.lambda_assume_role_policy
}

# CloudWatch Logs — write to each Lambda's own log group only.
# (Replaces the auto-generated AWSLambdaBasicExecutionRole-<uuid> managed policies
# the old roles each had, scoped tighter — only the function's own log group.)
resource "aws_iam_role_policy" "lambda_logs" {
  for_each = local.lambda_roles
  name     = "cloudwatch-logs"
  role     = aws_iam_role.lambda_exec[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = [
        "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/cursive-${replace(each.key, "_", "-")}:*"
      ]
    }]
  })
}

# Pipeline bucket access — every Lambda gets the same baseline policy.
resource "aws_iam_role_policy" "lambda_pipeline_bucket" {
  for_each = local.lambda_roles
  name     = "pipeline-bucket-access"
  role     = aws_iam_role.lambda_exec[each.key].id
  policy   = local.lambda_pipeline_bucket_policy
}

# --- Lambda-specific extra policies ---

# Debouncer: DynamoDB upsert + EB Scheduler create/delete + iam:PassRole on scheduler role.
resource "aws_iam_role_policy" "debouncer_extra" {
  name = "debouncer-extras"
  role = aws_iam_role.lambda_exec["debouncer"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DebounceTable"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem"]
        Resource = aws_dynamodb_table.debounce.arn
      },
      {
        Sid    = "ManageSchedules"
        Effect = "Allow"
        Action = [
          "scheduler:CreateSchedule",
          "scheduler:DeleteSchedule",
          "scheduler:GetSchedule"
        ]
        Resource = "arn:aws:scheduler:${var.region}:${local.account_id}:schedule/${aws_scheduler_schedule_group.debounce.name}/*"
      },
      {
        Sid      = "PassSchedulerRole"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.scheduler_invoker.arn
      }
    ]
  })
}

# ValidateMODS: SSM read for /cursive-pipeline/validation-rules.
resource "aws_iam_role_policy" "validate_mods_ssm" {
  name = "ssm-read"
  role = aws_iam_role.lambda_exec["validate_mods"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/cursive-pipeline/*"
    }]
  })
}

# AATLookup: outbound HTTPS to vocab.getty.edu/aat — no AWS perms needed beyond baseline.
# SubjectValidator: same — pure compute on input.
# (Both roles intentionally have no extra inline policies.)

