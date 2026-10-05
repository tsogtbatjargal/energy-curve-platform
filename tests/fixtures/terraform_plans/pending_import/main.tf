terraform {
  required_version = "~> 1.15"
}

resource "terraform_data" "imported" {}

import {
  to = terraform_data.imported
  id = "review-synthetic-id"
}
