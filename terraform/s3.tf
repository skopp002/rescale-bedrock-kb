# The corpus bucket and the FM-parser supplemental buckets (one per self-managed
# experiment). Settings mirror upload.ensure_bucket: versioning on, all public
# access blocked.
#
# There is exactly *one* corpus bucket because every experiment shares
# aws.primary_region. That is the whole benefit of the single-region rule: an
# ingestion job cannot read cross-region, so a second region would mean a second
# copy of the corpus and a second FM-parsing bill over the same PDFs.
#
# No resource here carries a `region` argument; the provider's region comes from
# aws.primary_region. Terraform also has no equivalent of the us-east-1
# LocationConstraint special case ensure_bucket has to carry.

resource "aws_s3_bucket" "corpus" {
  bucket        = local.corpus_bucket
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_versioning" "corpus" {
  bucket = aws_s3_bucket.corpus.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "corpus" {
  bucket                  = aws_s3_bucket.corpus.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Where the FM parser writes extracted figures and tables so they can be returned
# in retrieval source attribution. A MANAGED knowledge base has none of this --
# AWS owns the parser and its output.
#
# The knowledge base references this bucket's *root*: Bedrock rejects a
# supplemental data URI containing any sub-folder, which is why it cannot be a
# prefix inside the corpus bucket.
resource "aws_s3_bucket" "supplemental" {
  for_each = local.supplemental_experiments

  bucket        = local.names[each.key].supplemental
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_versioning" "supplemental" {
  for_each = local.supplemental_experiments

  bucket = aws_s3_bucket.supplemental[each.key].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "supplemental" {
  for_each = local.supplemental_experiments

  bucket                  = aws_s3_bucket.supplemental[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
