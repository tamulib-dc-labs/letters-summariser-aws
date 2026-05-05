# SSM parameter consumed by CursiveValidateMODS.
# Value is sourced from validation-rules.json (version-controlled in this repo).
resource "aws_ssm_parameter" "validation_rules" {
  name  = "/cursive-pipeline/validation-rules"
  type  = "String"
  value = file("${path.module}/validation-rules.json")
}
