resource "terraform_data" "existing" {}

resource "terraform_data" "imported" {}

import {
  to = terraform_data.imported
  id = "review-synthetic-id"
}
