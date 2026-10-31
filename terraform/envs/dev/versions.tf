terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
  }

  # Remote state in the bootstrap bucket (terraform/bootstrap): versioned, KMS-encrypted, S3-native
  # locking. Partial config: `make init` supplies bucket/key/kms_key_id so no account id is committed.
  backend "s3" {}
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project     = "secure-llm-gateway"
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}
