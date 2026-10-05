removed {
  from = terraform_data.relinquished
  lifecycle {
    destroy = false
  }
}
