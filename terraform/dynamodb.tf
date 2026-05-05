# DynamoDB table used by CursiveDebouncer to track active letter uploads.
# Decision: no TTL (faithful to current behavior — entries accumulate after debounce fires).
resource "aws_dynamodb_table" "debounce" {
  name         = "CursiveDebounce"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "letterId"

  attribute {
    name = "letterId"
    type = "S"
  }
}
