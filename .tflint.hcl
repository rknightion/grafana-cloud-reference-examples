# tflint runs recursively over common/terraform/modules/* and examples/*/terraform.
#
# `just lint` skips tflint when it is not installed rather than failing, because
# it is one of the few tools with no manifest to pin it in. CI installs it
# explicitly, so the gate is real there.

config {
  # Modules here are sourced by relative path and vendored at package time, so
  # there is nothing to download and inspecting them adds only runtime.
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  # renovate: datasource=github-releases depName=terraform-linters/tflint-ruleset-aws
  version = "0.49.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}

# Every variable and output in this repo carries a description on purpose: the
# text is what a customer reads when their deploy breaks, so a missing one is a
# real defect rather than a style nit.
rule "terraform_documented_variables" {
  enabled = true
}

rule "terraform_documented_outputs" {
  enabled = true
}

rule "terraform_typed_variables" {
  enabled = true
}

rule "terraform_naming_convention" {
  enabled = true
  format  = "snake_case"
}

# Deliberately off: an example's root module has no backend block and no
# provider version pin by design, because the customer chooses both.
rule "terraform_required_version" {
  enabled = true
}

rule "terraform_required_providers" {
  enabled = true
}
