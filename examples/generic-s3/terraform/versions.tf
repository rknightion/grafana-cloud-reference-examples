terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 7.0"
    }
  }

  # Deliberately no backend block. You choose your own state backend; add a
  # backend.tf, or run with local state while you are trying this out.
}
