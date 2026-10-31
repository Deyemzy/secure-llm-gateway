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

  # Local state for the dev sandbox. For shared environments use an S3 backend
  # with use_lockfile = true and a KMS-encrypted bucket.
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
