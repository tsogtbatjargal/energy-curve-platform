# Real offline Terraform plans (synthetic, no provider, no backend)

Plan JSON from Terraform 1.15.8 for `scripts/org_plan_check.py` tests. Each directory holds the
configuration, the hand-written starting state (if any; `synthetic-state.json`, copied to
`terraform.tfstate` to regenerate) and `synthetic-plan.json`. Nothing is applied. To regenerate:

    terraform init -backend=false && terraform plan -lock=false -out=p.tfplan
    terraform show -json p.tfplan > synthetic-plan.json

- `pending_import`: an import into an empty state. Terraform lists the resource being imported in
  `prior_state` (with `change.importing` set), although no state exists.
- `owned_and_import`: a resource the state already owns (no `importing`) next to a pending import.
- `forget`: `removed { lifecycle { destroy = false } }`; the action is `forget`, and `change.before`
  still holds the relinquished object.
- `json_override`: `main.tf` sets `input = terraform_data.reviewed_policy.id`, and `override.tf.json`
  replaces it with `replace(terraform_data.reviewed_policy.id, "/^.*$/", "p-FullAWSAccess")`.
  Terraform merges the override; the plan shows only the direct references and an unknown value,
  so plan JSON cannot reveal it (review of PR #24, reproduced by the reviewer).
