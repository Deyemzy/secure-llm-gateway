terraform {
  required_version = ">= 1.10" # S3-native state locking (use_lockfile)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # First apply runs with local state (the bucket doesn't exist yet); the state is then migrated into
  # the bucket it created (`make bootstrap-init`). On a fresh account: comment this out, apply, restore.
  backend "s3" {}
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = "secure-llm-gateway"
      Component = "bootstrap"
      ManagedBy = "terraform"
    }
  }
}
