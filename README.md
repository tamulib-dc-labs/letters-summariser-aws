# letters-summariser-aws

Terraform-managed AWS infrastructure for the Cursive Letters metadata pipeline. Deploys a Step Function that orchestrates 15 Lambdas + 4 Bedrock invocations + Textract, triggered by S3 uploads (with 5-minute debounce), with final MODS-formatted metadata pushed to a GitHub repo.

This README is a **complete walkthrough** — from "I have never used Terraform" to "I have a working pipeline deployed in AWS and verified by smoke test". Read top to bottom your first time.

---

## Table of contents

1. [What this project does](#1-what-this-project-does)
2. [Architecture](#2-architecture)
3. [What is Terraform?](#3-what-is-terraform)
4. [Prerequisites](#4-prerequisites)
5. [Repo layout](#5-repo-layout)
6. [First-time setup](#6-first-time-setup)
7. [Deployment — fresh AWS account](#7-deployment--fresh-aws-account)
8. [Deployment — existing AWS account (cutover)](#8-deployment--existing-aws-account-cutover)
9. [Smoke test](#9-smoke-test)
10. [Decommissioning the old stack](#10-decommissioning-the-old-stack)
11. [Day-2 operations](#11-day-2-operations)
12. [Troubleshooting](#12-troubleshooting)
13. [What's NOT managed by this Terraform](#13-whats-not-managed-by-this-terraform)
14. [Secrets handling](#14-secrets-handling)

---

## 1. What this project does

Given a folder of handwritten letter scans (JPG/PNG) uploaded to an S3 bucket, the pipeline:

1. Debounces multi-page uploads (waits 5 minutes after the last upload before starting).
2. Runs Amazon Textract over each page to get raw OCR + layout.
3. Sends each page to **Anthropic Claude (via Bedrock)** for full transcription.
4. Merges page transcriptions, then has Claude extract structured metadata (title, author, date, subjects, abstract, etc.).
5. Looks up Getty AAT vocabulary URIs for subject terms.
6. Has Claude reconcile any hallucinations (loops up to 2x if needed).
7. Formats results as MODS XML.
8. Has Claude judge the final quality.
9. Validates against rules from SSM Parameter Store.
10. Pushes final JSON + MODS XML to `tamulib-dc-labs/letters-metadata` on GitHub.

## 2. Architecture

```
┌─────────────────┐
│ User uploads to │
│ s3://...input/  │
└────────┬────────┘
         │ S3 Object Created
         ▼
┌─────────────────────┐
│ EventBridge Rule    │
│ (filter: input/*)   │
└────────┬────────────┘
         │
         ▼
┌─────────────────────────────┐
│ Debouncer Lambda            │
│ - upserts row in DynamoDB   │
│ - creates 5-min Scheduler   │
│   entry (resets on each     │
│   new upload for same       │
│   letter)                   │
└────────┬────────────────────┘
         │ 5 min later
         ▼
┌─────────────────────────────┐
│ EventBridge Scheduler       │
│ (assumes scheduler-invoker  │
│  role)                      │
└────────┬────────────────────┘
         │ states:StartExecution
         ▼
┌────────────────────────────────────────────────────────┐
│ Step Function: letters_metadata_automation             │
│ ┌──────────────────────────────────────────────────┐   │
│ │ PreparePages                                     │   │
│ │ ↓                                                │   │
│ │ Map (per page, max 5 concurrent):                │   │
│ │   AnalyzeDocument (Textract)                     │   │
│ │     ↓                                            │   │
│ │   PayloadGenerator                               │   │
│ │     ↓                                            │   │
│ │   Bedrock TranscribePage (Claude)                │   │
│ │     ↓                                            │   │
│ │   ParsePageResult                                │   │
│ │ ↓                                                │   │
│ │ MergeTranscriptions                              │   │
│ │ ↓                                                │   │
│ │ Bedrock MetadataExtract (Claude)                 │   │
│ │ ↓                                                │   │
│ │ MetadataParser → AATLookup → SubjectValidator    │   │
│ │ ↓                                                │   │
│ │ BuildReconcilePayload                            │   │
│ │ ↓                                                │   │
│ │ Bedrock Reconcile (Claude)                       │   │
│ │ ↓                                                │   │
│ │ ParseReconcileResult                             │   │
│ │ ↓ (if hallucinations + attempts < 2: loop back)  │   │
│ │ MetadataFormatter (→ MODS XML)                   │   │
│ │ ↓                                                │   │
│ │ Bedrock Judge (Claude)                           │   │
│ │ ↓                                                │   │
│ │ ParseJudgeResult → ValidateMetadata              │   │
│ │ ↓                                                │   │
│ │ AssembleFinalOutput                              │   │
│ │ ↓                                                │   │
│ │ GitHubSync → push to letters-metadata repo       │   │
│ └──────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────┘
```

**Key resources:**
- **15 Lambdas** (Python 3.12) — debouncer + 14 SM-invoked workers
- **2 Lambda layers** — `pillow` (image processing for PayloadGenerator), `git-lfs` (for GitHubSync)
- **1 S3 bucket** `cursive-letters-pipeline` for all I/O
- **1 DynamoDB table** `CursiveDebounce` for debouncer state
- **1 SSM parameter** `/cursive-pipeline/validation-rules` for ValidateMODS rules
- **1 EventBridge Scheduler group** `cursive-debounce`
- **1 Step Function** `letters_metadata_automation`
- **17 IAM roles** (1 SF orchestrator + 1 scheduler invoker + 15 Lambda exec roles)

---

## 3. What is Terraform?

**Terraform** is HashiCorp's infrastructure-as-code (IaC) tool. You write declarative `.tf` files describing what AWS resources you want, and Terraform figures out how to make AWS match.

### Why use Terraform here?

The pipeline was originally deployed manually through the AWS Console. That has problems:
- Configuration drift (no audit trail of who changed what)
- Hard to reproduce in another AWS account
- No code review on infrastructure changes
- Easy to forget about resources and rack up costs

Terraform fixes all that by making the AWS environment a **declarative artifact in this git repo**.

### Core concepts you'll see

| Concept | What it means |
|---|---|
| **Provider** | Plugin that talks to a cloud (we use `hashicorp/aws`) |
| **Resource** | One thing you want to exist (e.g., `aws_lambda_function "fn"`) |
| **Data source** | A read-only lookup (e.g., `data "archive_file"` zips a directory) |
| **Variable** | Input you pass at apply time (e.g., `github_token`) |
| **Output** | Value Terraform prints at the end of apply (e.g., the SM ARN) |
| **State** | Terraform's record of what it has created. Lives in `terraform.tfstate` (or remote backend). **Treat as sensitive** — contains resource attributes incl. some secrets. |
| **Lock file** | `.terraform.lock.hcl` pins exact provider versions. Committed to git. |

### The plan/apply lifecycle

You'll run these commands a lot:

| Command | What it does |
|---|---|
| `terraform init` | Downloads providers, sets up the working dir. Run once per project, or after changing `required_providers`. |
| `terraform plan` | Compares your `.tf` files to the real AWS state, prints what it WOULD do. **Always read the plan output before applying.** |
| `terraform apply` | Actually makes the changes to AWS. |
| `terraform destroy` | Tears down everything Terraform manages. Dangerous. |
| `terraform import <addr> <id>` | Brings an existing AWS resource under TF management without recreating it. |
| `terraform fmt` | Auto-formats `.tf` files. |
| `terraform validate` | Checks `.tf` syntax + references without hitting AWS. |
| `terraform output` | Prints values from `outputs.tf`. |

### Plan output symbols

When you run `terraform plan` you'll see lines like:
- `+ resource "aws_lambda_function" "fn[\"prepare_pages\"]"` — TF will **create**
- `~ resource "aws_sfn_state_machine" "pipeline"` — TF will **update in place**
- `-/+ resource "aws_iam_role" "..."` — TF will **destroy and recreate** (replacement)
- `- resource "..."` — TF will **destroy**

`-/+` (replacement) is the one to watch — it means the resource will be deleted and recreated, which can cause downtime or data loss for stateful things.

---

## 4. Prerequisites

You need **all four** of these before deploying.

### 4.1 Terraform CLI (≥ 1.6)

**Windows (recommended — Scoop, no admin needed):**
```pwsh
scoop install terraform
```

Verify: `terraform version` should print `Terraform v1.x.x`.

### 4.2 AWS CLI v2, configured

Install from <https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html>.

Configure with credentials for an account that can create IAM roles, Lambdas, Step Functions, S3 buckets, etc. (admin or equivalent):
```pwsh
aws configure
# Region: us-east-2 (the default for this project)
# Output: json
```

Verify: `aws sts get-caller-identity` should print your account info.

> **Security note:** If you're using AWS root account access keys, **stop and switch to an IAM admin user** before applying. Root keys are not appropriate for IaC. (You can keep them for the initial Terraform apply if necessary, then rotate.)

### 4.3 Bedrock model access enabled

Bedrock requires you to opt-in to specific foundation models per account/region.

1. Open the AWS Console → Bedrock → Model access (in `us-east-2`).
2. Request access to **Anthropic Claude Opus 4.6** (model ID `anthropic.claude-opus-4-6-v1`).
3. Wait for access to be granted (usually instant).

Without this, the 4 Bedrock state-machine tasks will fail at runtime even though Terraform apply succeeds.

### 4.4 GitHub PAT for the destination repo

The `github_sync` Lambda pushes MODS files to `tamulib-dc-labs/letters-metadata`. You need a Personal Access Token with `repo` scope on that repo:

1. <https://github.com/settings/tokens> → "Generate new token (classic)"
2. Scope: `repo` (full)
3. Save the token — you'll paste it into `terraform.tfvars` in section 6.

---

## 5. Repo layout

```
letters-summariser-aws/
├── README.md                      ← you are here
├── .gitignore
├── terraform.tfvars.example       ← copy to terraform.tfvars and fill in
│
├── terraform/                     ← all .tf code lives here
│   ├── main.tf                    ← provider + tags + locals
│   ├── variables.tf               ← input variables
│   ├── outputs.tf                 ← values printed after apply
│   ├── iam.tf                     ← all IAM roles + policies
│   ├── lambdas.tf                 ← 15 functions + log groups + zips
│   ├── layers.tf                  ← 2 Lambda layers
│   ├── s3.tf                      ← pipeline bucket
│   ├── eventbridge.tf             ← S3-triggered EB rule
│   ├── stepfunction.tf            ← state machine
│   ├── dynamodb.tf                ← debouncer state table
│   ├── scheduler.tf               ← EB Scheduler group
│   ├── ssm.tf                     ← validation rules parameter
│   └── validation-rules.json      ← seed value for SSM param
│
├── lambdas/                       ← Python source for each Lambda
│   ├── debouncer/
│   ├── prepare_pages/
│   ├── ... (15 dirs total)
│
├── layers/                        ← prebuilt zips for Lambda layers
│   ├── pillow-layer.zip
│   └── git-lfs-layer.zip
│
└── statemachine/
    └── workflow.asl.json          ← SF definition (templated)
```

---

## 6. First-time setup

### 6.1 Clone the repo

```pwsh
git clone https://github.com/tamulib-dc-labs/letters-summariser-aws.git
cd letters-summariser-aws
```

### 6.2 Create your `terraform.tfvars`

```pwsh
Copy-Item terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` and fill in **at minimum** these three sensitive values:

```hcl
github_token   = "ghp_yourTokenHere"
git_user_email = "you@example.com"
git_user_name  = "Your Name"
```

The other variables (region, bucket name, etc.) have sensible defaults in [variables.tf](terraform/variables.tf) — change only if you want a different value.

> **`terraform.tfvars` is gitignored.** Never commit it.

### 6.3 Initialize Terraform

```pwsh
cd terraform
terraform init
```

This downloads the AWS provider plugin and creates a `.terraform/` directory. You should see:
```
Terraform has been successfully initialized!
```

### 6.4 Validate the config

```pwsh
terraform validate
```

Expect: `Success! The configuration is valid.`

You're now ready to deploy. Skip to whichever section applies:
- **Section 7** if you're deploying to a fresh AWS account with no existing Cursive resources.
- **Section 8** if you're deploying to the existing AWS account (account `728905193692`) that already has manually-created Cursive resources.

---

## 7. Deployment — fresh AWS account

This path is the easy one. Run from the `terraform/` directory:

### 7.1 Plan

```pwsh
terraform plan -var-file=..\terraform.tfvars -out=plan.out
```

Read the plan output. You should see ~70 resources to create — Lambdas, log groups, IAM roles, the SM, the bucket, etc. **No destroys, no replacements.**

If the plan looks right, apply it:

### 7.2 Apply

```pwsh
terraform apply plan.out
```

Type `yes` if prompted. Apply takes ~3–5 minutes.

When it finishes, Terraform prints outputs:
```
Outputs:
state_machine_arn = "arn:aws:states:us-east-2:NNN:stateMachine:letters_metadata_automation"
pipeline_bucket_name = "cursive-letters-pipeline"
...
```

Skip to **section 9 (smoke test)**.

---

## 8. Deployment — existing AWS account (cutover)

If your AWS account already has the manually-deployed Cursive resources (PascalCase Lambda names, `CursiveOrchestratorRole`, etc.), you must do a **parallel-deploy-and-swap** migration. The principle:
- **Stateful or globally-named resources** (the S3 bucket, DynamoDB table, SSM param, scheduler group, state machine) get **imported** into TF state — TF takes over without recreating them.
- **Everything else** (Lambdas, IAM roles, EB rule, etc.) gets created **alongside** the old resources. The old ones keep running until the new pipeline is verified, then you delete them.

### 8.1 Import the 5 stateful/named resources

Run these from `terraform/`:

```pwsh
terraform import aws_s3_bucket.pipeline cursive-letters-pipeline
terraform import aws_dynamodb_table.debounce CursiveDebounce
terraform import aws_ssm_parameter.validation_rules /cursive-pipeline/validation-rules
terraform import aws_scheduler_schedule_group.debounce cursive-debounce
terraform import aws_sfn_state_machine.pipeline arn:aws:states:us-east-2:728905193692:stateMachine:letters_metadata_automation
```

Each command should print `Import successful!`.

### 8.2 Plan

```pwsh
terraform plan -var-file=..\terraform.tfvars -out=cutover.tfplan
```

Read the plan carefully. You should see:

- **~65 creates**: 15 new Lambdas (`cursive-prepare-pages` etc.), 15 log groups, 17 IAM roles + policies, 2 layer versions, EB rule + target + permission, SF log group, S3 bucket sub-resources (versioning/encryption/public-access-block/ownership/notification).
- **~5 updates in place**: state machine (new role, new ASL with kebab-case fn names, logging enabled); SSM parameter, S3 bucket, DDB table, scheduler group reconcile metadata only.
- **0 destroys.**

> **🚨 Read the plan for unexpected destroys or replacements.** If you see `-/+` lines on the bucket or state machine, stop and ask before applying.

### 8.3 Apply

```pwsh
terraform apply cutover.tfplan
```

Apply takes ~3–5 minutes.

After apply succeeds:
- New `cursive-*` Lambdas exist alongside the old `Cursive*` ones.
- The state machine has been swapped to use the new IAM role and the new (kebab-case) Lambda names. **The pipeline is now running on the new stack.**
- The old `Cursive*` Lambdas are orphaned but still deployed (cheap insurance for rollback).
- The old EventBridge rule (`CursiveS3UploadDebouncer`) is still active and routing S3 events to the old debouncer, which still calls into the (now-updated) state machine. **This works** because both old and new debouncer paths end up at the same SM.

Continue to **section 9 (smoke test)** before disabling the old EB rule.

---

## 9. Smoke test

Goal: verify a real letter goes through the pipeline end to end and ends up as a PR/commit on `tamulib-dc-labs/letters-metadata`.

### 9.1 Pick a test letter

Find a small JPG of a handwritten letter. 1–2 pages is fine. Name pages so they sort correctly: `page_01.jpg`, `page_02.jpg`, etc.

### 9.2 Upload to S3

Use a unique `letterId` to avoid colliding with anything already there:

```pwsh
$letter = "smoketest_$(Get-Date -Format yyyyMMddHHmmss)"
aws s3 cp page_01.jpg "s3://cursive-letters-pipeline/input/$letter/page_01.jpg"
# (If multi-page, upload the rest too)
```

### 9.3 Watch the debouncer fire

```pwsh
aws logs tail /aws/lambda/cursive-debouncer --follow --region us-east-2
```

You should see within ~10 seconds:
```
Detected upload for letterId: smoketest_..., key: input/smoketest_.../page_01.jpg
Reset timer — deleted existing schedule for ...
Scheduled Step Function for ... to fire at ... UTC
```

Press Ctrl-C to stop tailing.

### 9.4 Wait 5 minutes

The debouncer schedules the SM to fire 5 minutes after the last upload. Go make tea.

### 9.5 Watch the Step Function execution

Open the Step Functions console in `us-east-2` → State machines → `letters_metadata_automation` → Executions. You should see a new running execution. Click it and watch the graph as it progresses.

Each green checkmark means a state succeeded. The full pipeline takes 3–10 minutes depending on letter length.

If a state goes red, click it to see the error. Common ones:
- **`Bedrock.AccessDeniedException`** → you forgot to enable Bedrock model access (see [section 4.3](#43-bedrock-model-access-enabled)).
- **`Lambda timeout`** → bump the timeout in [lambdas.tf](terraform/lambdas.tf), `terraform apply`, retry.

### 9.6 Verify outputs

When the execution completes (green "Succeeded"):

1. **CloudWatch Logs** — every Lambda has a log group at `/aws/lambda/cursive-<name>`. Spot-check a couple to confirm they ran.
2. **S3 outputs** — there should be a `letters/<letterId>/` prefix in the bucket with `final.json` and `final.xml`:
   ```pwsh
   aws s3 ls "s3://cursive-letters-pipeline/letters/smoketest_.../"
   ```
3. **GitHub** — open <https://github.com/tamulib-dc-labs/letters-metadata/pulls> and look for a PR or commit referencing your `letterId`. The MODS XML and JSON files should be there.

If all three check out, **the pipeline works**. 🎉

### 9.7 Cut over (only relevant for section 8 cutover path)

Once the smoke test passes, disable the old EventBridge rule so it stops routing to the old (now-orphaned) debouncer:

```pwsh
aws events disable-rule --name CursiveS3UploadDebouncer --region us-east-2
```

Run another smoke test to confirm the new EB rule (`cursive-s3-upload-debouncer`) is the only path. Then proceed to section 10.

---

## 10. Decommissioning the old stack

After 24–48 hours of green operation on the new stack, delete the old PascalCase resources. Do this from the AWS Console (safer — easier to confirm what you're deleting) or via CLI.

```pwsh
# Old EventBridge rule + target
aws events remove-targets --rule CursiveS3UploadDebouncer --ids CursiveDebouncerTarget --region us-east-2
aws events delete-rule --name CursiveS3UploadDebouncer --region us-east-2

# Old customer-managed policy
aws iam delete-policy --policy-arn arn:aws:iam::728905193692:policy/CursiveSchedulerInvokeStepFunction
```

**Old Lambdas** (do these via console — Lambda → Functions → search "Cursive"):
- 15 Lambdas with PascalCase names (`CursivePreparePages`, `CursiveGitHubSync`, etc.)
- ⚠️ Do NOT delete the `cursive-*` (kebab-case) ones — those are TF-managed and live.

**Old IAM roles** (Console → IAM → Roles → search "Cursive" and "EventBridgeSchedulerRole"):
- `CursiveOrchestratorRole`
- `CursivePipelineLambdaRole`
- `CursiveDebouncerLambdaRole`
- `EventBridgeSchedulerRole`
- 14 auto-generated `Cursive*-role-<random>` roles

**Old layer versions** (after old Lambdas are deleted):
```pwsh
aws lambda delete-layer-version --layer-name pillow-layer --version-number 2 --region us-east-2
aws lambda delete-layer-version --layer-name git-lfs-layer --version-number 1 --region us-east-2
```

(The new TF-managed layer versions will be `pillow-layer:3` and `git-lfs-layer:2` or higher.)

---

## 11. Day-2 operations

### Making changes

1. Edit the relevant `.tf` or Lambda source file.
2. `terraform fmt`
3. `terraform validate`
4. `terraform plan -var-file=..\terraform.tfvars -out=plan.out` — read it.
5. `terraform apply plan.out`

### Common changes

| Goal | What to edit |
|---|---|
| Bump a Lambda's memory/timeout | `terraform/lambdas.tf` → `local.lambdas.<name>.memory` / `.timeout` |
| Change validation rules | `terraform/validation-rules.json` |
| Change Bedrock model | `terraform/variables.tf` → `bedrock_model_id` default |
| Add a new env var to a Lambda | `terraform/lambdas.tf` → `local.common_env` or a per-function block |
| Change SF logging level | `terraform/stepfunction.tf` → `logging_configuration.level` |
| Update Lambda source code | edit `lambdas/<name>/*.py` — TF detects the zip hash change and redeploys |

### Where state lives

By default, Terraform stores state locally in `terraform/terraform.tfstate`. **Do not commit this file** (it's in `.gitignore`). For team use, migrate to a remote backend (S3 + DynamoDB lock table) — out of scope for this README.

### Watching costs

The pipeline is mostly pay-per-use. The biggest line items are typically:
- Bedrock (Claude Opus 4.6) inference — by far the biggest, dollars per letter
- Lambda compute — pennies per letter
- Step Functions — pennies per execution
- Textract — ~$0.01–0.05 per page

Set a billing alert in AWS Budgets if you're processing many letters.

---

## 12. Troubleshooting

### `terraform init` complains about provider versions
Delete `.terraform/` and the lock file, re-run init:
```pwsh
Remove-Item -Recurse -Force .terraform
Remove-Item .terraform.lock.hcl
terraform init
```

### `terraform plan` wants to recreate (`-/+`) something I don't expect
Don't apply. Read the plan to see *which attribute* is forcing the replacement. Often it's a hash field (e.g., `source_code_hash`) — TF detected the zip changed. Or a name change you didn't intend.

### `Bedrock.AccessDeniedException` during smoke test
Bedrock model access is per-account-per-region. Open Bedrock console → Model access → request access to `anthropic.claude-opus-4-6-v1`. Same applies to any region you redeploy in.

### Step Function execution stuck/timed out
Open the execution in the SF console, click the failed state, read the error in the right panel. Then check that Lambda's CW log group `/aws/lambda/cursive-<name>` for the actual exception.

### GitHub sync fails with 401 or 403
Your `github_token` has expired or lacks `repo` scope. Generate a new one, update `terraform.tfvars`, run `terraform apply`. (You don't need a full plan/import cycle — just the token change.)

### "Resource already exists" on apply
You're deploying to an account that already has Cursive resources but didn't follow section 8's import steps. Either drop the existing resources or run the imports.

---

## 13. What's NOT managed by this Terraform

By design, the following are not part of the TF stack. They're either dead/legacy or external:

- Legacy S3 buckets `cursive-letters-input-1`, `cursive-letters-intermediate`, `cursive-letters-final`, `cursive-lambda-layers` — predate this migration; clean up via console if you want.
- Bedrock guardrail `cursive-pipeline-guardrail` — dormant, not referenced.
- Bedrock agent `agent-quick-start-glt3e` — console exploration leftover.
- A2I-related Lambdas (`CursiveA2ITrigger`, `CursiveA2IResultProcessor`) — dropped (workflow was dead).
- `CursiveIngestion` Lambda — dropped (not in the operational path; no triggers).
- The destination GitHub repo `tamulib-dc-labs/letters-metadata` itself.

---

## 14. Secrets handling

- `terraform.tfvars` contains the GitHub PAT and git user info. **It is gitignored. Never commit it.**
- The PAT is passed to the GitHub-sync Lambda as an environment variable (`GITHUB_TOKEN`). Anyone with `lambda:GetFunctionConfiguration` on that Lambda can read it.
- Rotate the PAT periodically. To rotate: generate a new one, update `terraform.tfvars`, `terraform apply`. The old one keeps working until you revoke it on github.com.
- AWS credentials are managed by the AWS CLI's credential chain (env vars / `~/.aws/credentials` / IAM Identity Center / etc.). Terraform doesn't touch them.

---

## License

(internal — pending)
