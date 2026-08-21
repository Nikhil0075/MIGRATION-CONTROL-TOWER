# Deploy & Harden Phase 2a (docs/adr/0004-terraform-for-infrastructure.md).
# Written for Terraform >=1.5 or OpenTofu >=1.6 — the two are
# functionally interchangeable for everything this module uses; pick
# whichever your CI/local toolchain already has.
#
# Live-applied with Terraform 1.9.8 against the real autonomous-data-migration
# project (Deploy & Harden Phase 5 close-out) — see README.md's opening
# note for what that live apply actually found and fixed.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.40"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.40"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State bucket bootstrapped by infrastructure/terraform_state_bootstrap.sh
  # (the one shell script this phase keeps, per ADR 0004 — Terraform
  # cannot create the bucket it stores its own state in). Fill in the
  # real bucket name before first `terraform init`.
  backend "gcs" {
    bucket = "autonomous-data-migration-tfstate"
    prefix = "control-tower"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}
