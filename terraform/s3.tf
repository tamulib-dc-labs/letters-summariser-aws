# Pipeline bucket — the only S3 resource referenced by all 15 in-scope Lambdas.
#
# Faithful to current AWS config:
#   - AES256 SSE w/ bucket key, blocks SSE-C uploads
#   - Ownership: BucketOwnerEnforced (ACLs disabled)
#   - Public access fully blocked
#   - EventBridge notifications enabled
#   - No CORS, no lifecycle, no tags
#
# Added vs current AWS:
#   - Versioning ENABLED (current bucket has versioning OFF — added for pipeline safety)
#
# Dropped from current config:
#   - Stale S3-event Lambda notification to A2IResumeCallback (Lambda doesn't exist)
#   - Bucket policy granting orchestrator role (redundant with IAM role policy)

resource "aws_s3_bucket" "pipeline" {
  bucket = var.pipeline_bucket_name
}

resource "aws_s3_bucket_ownership_controls" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "pipeline" {
  bucket                  = aws_s3_bucket.pipeline.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# EventBridge enabled — the actual rule lives in eventbridge.tf.
resource "aws_s3_bucket_notification" "pipeline" {
  bucket      = aws_s3_bucket.pipeline.id
  eventbridge = true
}
