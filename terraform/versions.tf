terraform {
  required_version = ">= 1.6"
  required_providers {
    scaleway = {
      source  = "scaleway/scaleway"
      version = "~> 2.93"
    }
  }
}

# Credentials come from the deployment identity (SCW_ACCESS_KEY / SCW_SECRET_KEY in the
# environment), never from this file.
provider "scaleway" {
  region = "fr-par"
  zone   = "fr-par-1"
}
