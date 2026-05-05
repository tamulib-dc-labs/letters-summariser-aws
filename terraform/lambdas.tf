# 15 Lambda functions + their log groups + per-function source-zip data sources.
#
# Naming: AWS function name = cursive-<snake-to-hyphen> (e.g. cursive-prepare-pages).
# This is a rename from the original PascalCase (CursivePreparePages); the state
# machine ASL is rewritten via templatefile() in stepfunction.tf to match.
#
# Source layout: each function's code lives at ../lambdas/<snake_name>/.
# The dir is zipped on apply via data.archive_file. Zip output goes to
# .lambda-zips/<name>.zip (gitignored).
#
# Per-function attributes (memory, timeout, handler, layers) are preserved
# from the current AWS config.

############################
# Per-function settings
############################

locals {
  lambdas = {
    debouncer = {
      handler = "lambda_function.lambda_handler"
      memory  = 128
      timeout = 30
      layers  = []
    }
    prepare_pages = {
      handler = "prepare_pages.lambda_handler"
      memory  = 128
      timeout = 60
      layers  = []
    }
    payload_generator = {
      handler = "lambda_function.lambda_handler"
      memory  = 1024
      timeout = 300
      layers  = ["pillow"]
    }
    result_parser = {
      handler = "lambda_function.lambda_handler"
      memory  = 512
      timeout = 300
      layers  = []
    }
    merge_transcriptions = {
      handler = "lambda_function.lambda_handler"
      memory  = 512
      timeout = 120
      layers  = []
    }
    metadata_parser = {
      handler = "lambda_function.lambda_handler"
      memory  = 256
      timeout = 120
      layers  = []
    }
    aat_lookup = {
      handler = "lambda_function.lambda_handler"
      memory  = 256
      timeout = 60
      layers  = []
    }
    subject_validator = {
      handler = "lambda_function.lambda_handler"
      memory  = 256
      timeout = 120
      layers  = []
    }
    reconciler = {
      handler = "lambda_function.lambda_handler"
      memory  = 512
      timeout = 300
      layers  = []
    }
    reconcile_parser = {
      handler = "lambda_function.lambda_handler"
      memory  = 256
      timeout = 120
      layers  = []
    }
    mods_formatter = {
      handler = "lambda_function.lambda_handler"
      memory  = 512
      timeout = 300
      layers  = []
    }
    judge_parser = {
      handler = "lambda_function.lambda_handler"
      memory  = 256
      timeout = 120
      layers  = []
    }
    validate_mods = {
      handler = "lambda_function.lambda_handler"
      memory  = 256
      timeout = 60
      layers  = []
    }
    final_assembler = {
      handler = "lambda_function.lambda_handler"
      memory  = 128
      timeout = 60
      layers  = []
    }
    github_sync = {
      handler = "CursiveGitHubSync.lambda_handler"
      memory  = 1024
      timeout = 300
      layers  = ["git_lfs"]
    }
  }

  # Look-up table for layer ARNs by short name used in `local.lambdas[*].layers`.
  layer_arns = {
    pillow  = aws_lambda_layer_version.pillow.arn
    git_lfs = aws_lambda_layer_version.git_lfs.arn
  }

  # Env vars baked into every Lambda. Even Lambdas that currently hardcode the
  # bucket name will accept this var (no-op for them); enables future cross-account
  # portability without further IAM/code churn.
  common_env = {
    PIPELINE_BUCKET = var.pipeline_bucket_name
  }

  # Per-function extra env vars.
  per_lambda_env = {
    debouncer = {
      # SFN ARN is computed string-wise to avoid a TF resource cycle
      # (aws_sfn_state_machine.pipeline depends_on the Lambdas).
      SFN_ARN            = "arn:aws:states:${var.region}:${local.account_id}:stateMachine:letters_metadata_automation"
      SCHEDULER_ROLE_ARN = aws_iam_role.scheduler_invoker.arn
      SCHEDULE_GROUP     = aws_scheduler_schedule_group.debounce.name
      DEBOUNCE_TABLE     = aws_dynamodb_table.debounce.name
    }
    github_sync = {
      GITHUB_TOKEN       = var.github_token
      GITHUB_REPO        = var.github_repo
      GITHUB_BASE_BRANCH = var.github_base_branch
      GIT_USER_EMAIL     = var.git_user_email
      GIT_USER_NAME      = var.git_user_name
    }
  }
}

############################
# Source zip per Lambda
############################

data "archive_file" "lambda_src" {
  for_each    = local.lambdas
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/${each.key}"
  output_path = "${path.module}/.lambda-zips/${each.key}.zip"
}

############################
# Log groups (explicit, with retention)
############################

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.lambdas
  name              = "/aws/lambda/cursive-${replace(each.key, "_", "-")}"
  retention_in_days = var.log_retention_days
}

############################
# Lambda functions
############################

resource "aws_lambda_function" "fn" {
  for_each = local.lambdas

  function_name    = "cursive-${replace(each.key, "_", "-")}"
  role             = aws_iam_role.lambda_exec[each.key].arn
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  handler          = each.value.handler
  memory_size      = each.value.memory
  timeout          = each.value.timeout
  filename         = data.archive_file.lambda_src[each.key].output_path
  source_code_hash = data.archive_file.lambda_src[each.key].output_base64sha256

  layers = [for l in each.value.layers : local.layer_arns[l]]

  environment {
    variables = merge(local.common_env, lookup(local.per_lambda_env, each.key, {}))
  }

  # Make sure the log group exists before the function is created (otherwise
  # Lambda auto-creates one without our retention setting).
  depends_on = [aws_cloudwatch_log_group.lambda]
}
