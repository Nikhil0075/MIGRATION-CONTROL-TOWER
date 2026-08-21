# ADR 0004: Terraform/OpenTofu for infrastructure, not more shell scripts

## Status
Accepted — Deploy & Harden Phase 2.

## Context
`infrastructure/gcp_setup.sh` and `teardown.sh` are imperative, idempotent-by-hand-checking bash
scripts that provision Firestore, Pub/Sub topics/subscriptions, BigQuery datasets, and 7 per-agent
IAM service accounts. They work, but they're not declarative: there's no single source of truth
for "what should exist," no plan/diff step before a change applies, and no state file to detect
drift. Phase 2 of this effort adds substantially more infrastructure — 9 Cloud Run services, 1
Cloud Run Job, Cloud SQL, Secret Manager bindings, Artifact Registry, monitoring alert policies, a
billing budget, and IAM bindings tying all of it together. Extending `gcp_setup.sh` further with
more `gcloud` calls would compound the audit's "no IaC" finding rather than resolve it.

## Decision
Introduce `infrastructure/terraform/` (Terraform or OpenTofu — functionally interchangeable for
this project's needs) as the source of truth for every resource listed above, plus everything
`gcp_setup.sh` already creates (migrated in, not left to drift against the new module). Shell
scripts are kept for exactly two things: bootstrapping the Terraform remote-state bucket (which
must exist before `terraform init` can use it) and emergency teardown
(`infrastructure/teardown.sh`, retained as a fast off-switch independent of Terraform state, for
the case where Terraform itself is unavailable or broken).

## Consequences
- `terraform plan` becomes the required review step before any infrastructure change applies —
  this is also where the "confirm before creating real, billed resources" checkpoints in the
  overall plan attach concretely (read the plan output, confirm, then `terraform apply`).
- State file management (`infrastructure/terraform/` remote state in a GCS bucket) is new
  operational surface — must be backed up/versioned like any other durable artifact.
- `gcp_setup.sh`'s existing resources need a one-time import into Terraform state
  (`terraform import`) rather than being recreated, to avoid duplicate/conflicting resources.
- Teardown at the end of the funded window (Phase-0-verified date) becomes `terraform destroy`
  plus `infrastructure/teardown.sh` for anything intentionally left outside Terraform's state.
