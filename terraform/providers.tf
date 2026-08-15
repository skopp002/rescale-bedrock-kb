provider "aws" {
  region = local.cfg.aws.primary_region

  # The cost-attribution tags from config.yaml, applied to every taggable
  # resource in the module. This replaces the three hand-written tag shape
  # converters in aws.py -- Terraform renders the right shape per service.
  #
  # The caveats from the README still hold and Terraform cannot change them:
  # Bedrock *model invocation* (FM parsing, embedding, generation, judging) bills
  # as account-level model usage and is not attributable to these tags, and a tag
  # only reaches Cost Explorer after being activated as a cost allocation tag in
  # the payer account's Billing console.
  default_tags {
    tags = local.cfg.tags
  }
}

data "aws_caller_identity" "current" {}
