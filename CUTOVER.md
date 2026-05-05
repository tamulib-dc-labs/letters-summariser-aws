# Cutover plan: parallel deploy then swap

This document describes how to migrate from the existing AWS resources (deployed manually, with `Cursive*` PascalCase naming) to the new Terraform-managed stack (`cursive-*` kebab-case naming), with **rollback safety**.

## What collides vs what runs in parallel

| Resource | Strategy |
|---|---|
| 15 Lambda functions (`Cursive*` → `cursive-*`) | **Parallel** — TF creates new alongside old |
| 17 IAM roles (random suffix → deterministic kebab-case) | **Parallel** — new roles created; old roles orphaned |
| EventBridge rule (`CursiveS3UploadDebouncer` → `cursive-s3-upload-debouncer`) | **Parallel** — old still routes to old debouncer |
| Lambda layers (`pillow-layer`, `git-lfs-layer`) | **Parallel** — new layer *versions* (v3) alongside old (v1/v2) |
| `EventBridgeSchedulerRole` → `cursive-scheduler-invoker-role` | **Parallel** — new role created; old orphaned |
| **S3 bucket `cursive-letters-pipeline`** | **Import** — names are global; we keep the bucket and its contents |
| **DynamoDB `CursiveDebounce`** | **Import** — has live runtime state |
| **SSM `/cursive-pipeline/validation-rules`** | **Import** — the same param |
| **Scheduler group `cursive-debounce`** | **Import** — the same group |
| **State machine `letters_metadata_automation`** | **Import** — TF will update it in place to use new role + new Lambda names |

## Step-by-step

### 1. Pre-flight

```pwsh
cd letters-summariser-aws\terraform
terraform init      # already done
terraform validate  # already passing
```

Provision `terraform.tfvars` from `terraform.tfvars.example`. **Do not commit it.**

### 2. Import the 5 stateful/named resources

```pwsh
terraform import aws_s3_bucket.pipeline cursive-letters-pipeline
terraform import aws_dynamodb_table.debounce CursiveDebounce
terraform import aws_ssm_parameter.validation_rules /cursive-pipeline/validation-rules
terraform import aws_scheduler_schedule_group.debounce cursive-debounce
terraform import aws_sfn_state_machine.pipeline arn:aws:states:us-east-2:728905193692:stateMachine:letters_metadata_automation
```

The bucket's sub-resources can be left as-is — TF will reconcile them on first apply. Notably the bucket-notification import would preserve the stale `A2IResumeCallback` config; not importing means TF will overwrite it (the desired outcome — that's the broken Lambda we documented in session 2).

### 3. Plan and review

```pwsh
terraform plan -var-file=..\terraform.tfvars -out=cutover.tfplan
```

Expected plan summary (high level):
- **Create**: 15 new Lambda functions, 15 log groups, 17 IAM roles, ~30 IAM role policies, 2 layer versions, EventBridge rule + target + permission, SF log group, S3 bucket sub-resources (versioning, public-access-block, encryption, ownership, notification).
- **Update in place**: state machine (new role, new ASL with kebab-case fn names, logging enabled).

Read the plan carefully. Look specifically for any unexpected destroys.

### 4. Apply

```pwsh
terraform apply cutover.tfplan
```

After apply succeeds:
- **New Lambdas exist** at `cursive-*` names.
- **State machine has been swapped** to use the new role + new Lambda names. The next pipeline run will use the new stack end to end.
- **Old debouncer Lambda still wired in** (via the old EventBridge rule) — it still calls into the (updated) state machine, but using the old `EventBridgeSchedulerRole`. Functionally equivalent because both old and new scheduler-invoker roles grant the same `states:StartExecution`.

### 5. Smoke test

Upload a new letter to `s3://cursive-letters-pipeline/input/<letterId>/page_01.jpg` and watch the Step Functions console + CloudWatch logs (`/aws/lambda/cursive-*` and `/aws/stepfunctions/letters_metadata_automation`).

Also check the GitHub PR/commit on `tamulib-dc-labs/letters-metadata`.

If anything breaks: the **old** Lambdas are still deployed. Restore by editing the SM definition manually back to `Cursive*` names + old role, OR `terraform state rm aws_sfn_state_machine.pipeline` and revert the SM via console. (Save the original SM JSON from `aws_pull/statemachine/describe.json` as your rollback artifact.)

### 6. Cut traffic over

Once smoke test passes:

```pwsh
# Disable old EB rule (keeps it around but stops it firing)
aws events disable-rule --name CursiveS3UploadDebouncer --region us-east-2
```

The new EB rule (`cursive-s3-upload-debouncer`) is already enabled and now drives the pipeline.

Wait 10–15 minutes. Watch one more upload-triggered run end-to-end.

### 7. Decommission old resources (manual, via console or aws CLI)

After 24–48 hours of green operation:

```pwsh
# Old EventBridge rule + target
aws events remove-targets --rule CursiveS3UploadDebouncer --ids CursiveDebouncerTarget --region us-east-2
aws events delete-rule --name CursiveS3UploadDebouncer --region us-east-2

# 15 old Lambdas (PascalCase) — Console: Lambda → Functions → search "Cursive"
# Note: only delete Cursive* (PascalCase). Do NOT delete cursive-* (kebab-case, TF-managed).

# 16 old IAM roles (auto-generated random-suffix names) — Console: IAM → Roles → search "Cursive"
# Plus: CursiveOrchestratorRole, CursivePipelineLambdaRole, CursiveDebouncerLambdaRole, EventBridgeSchedulerRole

# 1 old customer-managed policy
aws iam delete-policy --policy-arn arn:aws:iam::728905193692:policy/CursiveSchedulerInvokeStepFunction

# Old layer versions (after old Lambdas deleted)
aws lambda delete-layer-version --layer-name pillow-layer --version-number 2 --region us-east-2
aws lambda delete-layer-version --layer-name git-lfs-layer --version-number 1 --region us-east-2
```

### 8. Optional cleanup (can defer)

Per session 2 decision — these are NOT TF-managed but harmless if left:
- S3 bucket `cursive-letters-input-1` (stale test images, Feb 2026)
- S3 bucket `cursive-letters-intermediate` (A2I path, dead)
- S3 bucket `cursive-letters-final` (replaced by GitHub repo)
- S3 bucket `cursive-lambda-layers` (only stored layer zips)
- Bedrock guardrail `cursive-pipeline-guardrail`
- Bedrock agent `agent-quick-start-glt3e`

## Rollback

If apply fails midway, `terraform state` may be in a partial state. To recover:
1. `terraform state list` to see what's tracked.
2. `terraform plan` to see what TF wants to do next.
3. If unsalvageable: `terraform state rm <addr>` for problematic resources, restore manually from `aws_pull/`, then re-import once fixed.

The original SM definition lives at `aws_pull/statemachine/describe.json` (field `definition` — needs to be parsed out of its embedded string). Use it as the source of truth if you need to manually revert the SM.
