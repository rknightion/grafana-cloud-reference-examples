# Shared Terraform modules

Real reusable modules, sourced by relative path from each example's root module
and vendored into the release bundle by `just package <example>`.

| Module | What it owns |
| --- | --- |
| [`lambda-function`](lambda-function) | The function, its execution role, its log group, its alarms |
| [`s3-event-source`](s3-event-source) | SQS queue, dead-letter queue, queue policy, bucket notification, event source mapping |
| [`grafana-cloud-credentials`](grafana-cloud-credentials) | Read access to an existing credential secret. Creates nothing |

## Source paths are a contract, not a preference

An example references a module as
`source = "../../../common/terraform/modules/<name>"`.
`tools/package_example.py` copies the module into the bundle and rewrites that to
`./modules/<name>`, so the packaged tree has no path escaping its own directory.

A module source that is an absolute path, a registry reference or a git URL
**breaks packaging**, and the packager fails loudly rather than shipping a bundle
that only falls over on the customer's `terraform init`.

## Conventions every module here follows

- **No provider block and no backend block.** A module that configures a provider
  cannot be used twice with different providers, and a customer chooses their own
  state backend.
- **`versions.tf` declares `required_providers` with a range, never a pin.** The
  root module is where a version gets pinned; a pin in a shared module conflicts
  with every other module the caller uses.
- **Every variable has a `description`.** Where a variable has a failure mode
  worth knowing, the description says what goes wrong, not just what the variable
  is. That text is what a customer reads when their deploy breaks.
- **`validation` blocks over documentation** wherever the rule fits in one
  variable. Where a rule spans two variables, a `lifecycle.precondition` on the
  resource, because a `validation` block can only see its own variable.
- **Outputs include the ids a caller needs to attach its own policy** (`role_id`,
  `queue_arn`), so an example can add a permission without this module growing a
  parameter for it.
- **Tags merge, never replace.** Each module adds
  `grafana-cloud-reference-example = "true"` and `ManagedBy = "terraform"` under
  the caller's tags.

## Adding a module

Add it here, wire it into an example, and run `just check` - `just tf-validate`
alone skips tflint, cfn-lint and the conformance rules. If the module
is only ever used by one example, it belongs in that example's root module
instead - a module with one caller is indirection, not reuse.
