# Lambda layers. Source zips live at ../layers/.
# source_code_hash forces a new layer version when the zip changes.

resource "aws_lambda_layer_version" "pillow" {
  layer_name               = "pillow-layer"
  filename                 = "${path.module}/../layers/pillow-layer.zip"
  source_code_hash         = filebase64sha256("${path.module}/../layers/pillow-layer.zip")
  compatible_runtimes      = ["python3.12"]
  compatible_architectures = ["x86_64"]
}

resource "aws_lambda_layer_version" "git_lfs" {
  layer_name               = "git-lfs-layer"
  filename                 = "${path.module}/../layers/git-lfs-layer.zip"
  source_code_hash         = filebase64sha256("${path.module}/../layers/git-lfs-layer.zip")
  compatible_runtimes      = ["python3.12"]
  compatible_architectures = ["x86_64"]
}
