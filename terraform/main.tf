terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = local.common_tags
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  common_tags = {
    Project     = "letters-summariser"
    ManagedBy   = "terraform"
    Repo        = "tamulib-dc-labs/letters-summariser-aws"
    Environment = var.environment
  }
}
