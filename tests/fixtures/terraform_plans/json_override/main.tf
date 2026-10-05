resource "terraform_data" "reviewed_policy" {
  input = "synthetic"
}
resource "terraform_data" "attachment" {
  input = terraform_data.reviewed_policy.id
}
