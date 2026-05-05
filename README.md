# letters-summariser-aws

Terraform-managed AWS infrastructure for the Cursive Letters metadata pipeline.

Deploys a Step Function orchestrating 15 Lambdas + 4 Bedrock invocations + Textract, triggered by S3 uploads (with 5-min debounce), with final MODS output pushed to a GitHub repo.

## Architecture

```
S3 upload (input/<letterId>/...) → EventBridge Rule → Debouncer Lambda
                  → upserts CursiveDebounce DynamoDB row
                  → creates one-shot EventBridge Scheduler entry (5-min debounce)
                  → Scheduler fires → states:StartExecution (via EventBridgeSchedulerRole)
                  → letters_metadata_automation state machine
                      ├─ PreparePages
                      ├─ Map[ AnalyzeDocument(Textract) → PayloadGenerator
                      │      → Bedrock TranscribePage → ResultParser ]
                      ├─ MergeTranscriptions
                      ├─ Bedrock MetadataExtract → MetadataParser
                      ├─ AATLookup → SubjectValidator
                      ├─ Reconciler → Bedrock Reconcile → ReconcileParser
                      │   (loops ≤2x on hallucinations)
                      ├─ MODSFormatter → Bedrock Judge → JudgeParser
                      ├─ ValidateMODS → FinalAssembler
                      └─ GitHubSync (pushes to letters-metadata repo)
```

## Layout

```
terraform/        # All .tf — provider, vars, lambdas, layers, IAM, SF, S3, EB, DDB, SSM
lambdas/          # 15 Lambda source dirs (snake_case names)
layers/           # git-lfs-layer.zip, pillow-layer.zip
statemachine/     # workflow.asl.json
scripts/          # fetch_from_aws.ps1 (idempotent re-pull) — TBD
```

## Prerequisites

- Terraform ≥ 1.6
- AWS CLI v2 configured (`aws sts get-caller-identity` works)
- Bedrock model access enabled in target account/region for `anthropic.claude-opus-4-6-v1`
- A GitHub PAT with `repo` scope on the destination metadata repo

## Deploy

For an existing AWS account that already has the manually-deployed Cursive resources, follow the [cutover plan](CUTOVER.md) — that includes the necessary `terraform import` steps for shared/named resources (S3 bucket, DynamoDB, SSM, state machine).

For a fresh AWS account (no existing Cursive resources):

```pwsh
cd terraform
cp ../terraform.tfvars.example ../terraform.tfvars
# Edit terraform.tfvars — github_token, git_user_email, git_user_name at minimum
terraform init
terraform plan  -var-file=../terraform.tfvars
terraform apply -var-file=../terraform.tfvars
```

## What is NOT managed by this Terraform

- Legacy S3 buckets `cursive-letters-input-1`, `cursive-letters-intermediate`, `cursive-letters-final`, `cursive-lambda-layers` (predate this migration; clean up manually if desired).
- Bedrock guardrail `cursive-pipeline-guardrail` (dormant, not referenced by state machine).
- Bedrock agent `agent-quick-start-glt3e` (console exploration leftover).
- A2I-related Lambdas (`CursiveA2ITrigger`, `CursiveA2IResultProcessor`) — dropped.
- `CursiveIngestion` Lambda — confirmed dead (no triggers, no resource policy, not in SM); dropped.

## Secrets

`terraform.tfvars` is gitignored. Never commit GitHub tokens. Rotate the PAT periodically.
