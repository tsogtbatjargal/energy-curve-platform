terraform {
  required_version = "~> 1.15"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.67"
    }
  }

}

# The management account (ADR-0021). Applied by the Identity Center admin only, never by CI.
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project       = "energy-curve-platform"
      owner         = var.owner
      "cost-center" = "personal"
      ttl           = "permanent"
      stack         = "org"
    }
  }
}
