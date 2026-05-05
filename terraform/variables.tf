variable "region" {
  type    = string
  default = "us-east-2"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "pipeline_bucket_name" {
  description = "S3 bucket for pipeline I/O. Must be globally unique."
  type        = string
  default     = "cursive-letters-pipeline"
}

variable "bedrock_model_id" {
  description = "Bedrock inference profile id (cross-region system profile)."
  type        = string
  default     = "us.anthropic.claude-opus-4-6-v1"
}

variable "bedrock_foundation_model_arn_pattern" {
  description = "Foundation model ARN(s) the inference profile resolves to. Used in IAM policy. Wildcard at end covers versioned (model-id:0) and unversioned (model-id) forms across regions."
  type        = string
  default     = "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-6-v1*"
}

variable "github_repo" {
  description = "Target GitHub repo for CursiveGitHubSync output (owner/repo)."
  type        = string
  default     = "tamulib-dc-labs/letters-metadata"
}

variable "github_base_branch" {
  type    = string
  default = "main"
}

variable "github_token" {
  description = "GitHub PAT used by CursiveGitHubSync. Provided via tfvars or env var."
  type        = string
  sensitive   = true
}

variable "git_user_email" {
  type      = string
  sensitive = true
}

variable "git_user_name" {
  type      = string
  sensitive = true
}

variable "log_retention_days" {
  type    = number
  default = 30
}
