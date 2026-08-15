# The knowledge base service role, one per experiment. Mirrors
# provision._inline_policy: the statement set depends on the KB type, because a
# MANAGED knowledge base has neither a customer vector store nor an FM parser
# writing supplemental data.

data "aws_iam_policy_document" "kb_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
  }
}

# Written as a policy document rather than jsonencode() because the statements
# have non-uniform shapes -- only ReadCorpus carries a Condition -- and
# Terraform cannot concat() a list whose object types differ.
data "aws_iam_policy_document" "kb" {
  for_each = local.selected

  statement {
    sid    = "InvokeModels"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      # Required when the FM parser is an inference profile rather than an
      # on-demand model: CreateDataSource resolves the profile and fails with
      # "Not authorized to call GetInferenceProfile" without this, even though
      # InvokeModel is already granted.
      "bedrock:GetInferenceProfile",
    ]
    resources = [
      "arn:aws:bedrock:${local.region}::foundation-model/*",
      "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/*",
      # Inference profiles fan out to other regions; the profile grant alone
      # isn't enough without the underlying model in each.
      "arn:aws:bedrock:*::foundation-model/*",
    ]
  }

  statement {
    sid     = "ReadCorpus"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      "arn:aws:s3:::${local.corpus_bucket}",
      "arn:aws:s3:::${local.corpus_bucket}/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [local.account_id]
    }
  }

  dynamic "statement" {
    # Where the FM parser writes extracted figures and tables.
    for_each = each.value.kb_type != "MANAGED" ? [1] : []
    content {
      sid     = "WriteSupplementalData"
      effect  = "Allow"
      actions = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"]
      resources = [
        "arn:aws:s3:::${local.names[each.key].supplemental}",
        "arn:aws:s3:::${local.names[each.key].supplemental}/*",
      ]
    }
  }

  dynamic "statement" {
    for_each = each.value.uses_s3_vectors ? [1] : []
    content {
      sid     = "S3Vectors"
      effect  = "Allow"
      actions = ["s3vectors:*"]
      resources = [
        "arn:aws:s3vectors:${local.region}:${local.account_id}:bucket/${local.names[each.key].vector_bucket}",
        "arn:aws:s3vectors:${local.region}:${local.account_id}:bucket/${local.names[each.key].vector_bucket}/*",
      ]
    }
  }
}

resource "aws_iam_role" "kb" {
  for_each = local.selected

  name               = local.names[each.key].role
  description        = "Bedrock KB role for ${each.value.label}"
  assume_role_policy = data.aws_iam_policy_document.kb_trust.json
}

resource "aws_iam_role_policy" "kb" {
  for_each = local.selected

  name   = "kb-access"
  role   = aws_iam_role.kb[each.key].id
  policy = data.aws_iam_policy_document.kb[each.key].json
}

# CreateKnowledgeBase validates the role's S3 access, and IAM is eventually
# consistent, so the knowledge base cannot be created the instant the policy is
# written. provision.py sleeps and then retries CreateKnowledgeBase five times;
# Terraform has no per-resource retry hook for this, so the wait is all we get --
# if an apply still loses the race, re-running it succeeds.
#
# `triggers` on the policy JSON means the wait also happens when the policy
# *changes*, not only when the role is first created, which is the case
# provision.py's comment calls out as easy to miss.
resource "time_sleep" "iam_propagation" {
  for_each = local.selected

  create_duration = var.iam_propagation_delay
  triggers = {
    role   = aws_iam_role.kb[each.key].arn
    policy = data.aws_iam_policy_document.kb[each.key].json
  }

  depends_on = [aws_iam_role_policy.kb]
}
