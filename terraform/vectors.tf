# The S3 Vectors store for the self-managed (VECTOR) experiments. The MANAGED
# knowledge base has none: AWS owns its vector store entirely.

resource "aws_s3vectors_vector_bucket" "kb" {
  for_each = local.vector_experiments

  vector_bucket_name = local.names[each.key].vector_bucket
  force_destroy      = var.force_destroy
}

resource "aws_s3vectors_index" "kb" {
  for_each = local.vector_experiments

  index_name         = local.names[each.key].vector_index
  vector_bucket_name = aws_s3vectors_vector_bucket.kb[each.key].vector_bucket_name

  # S3 Vectors takes a lowercase data type where Bedrock takes FLOAT32.
  data_type       = lower(each.value.embedding_data_type)
  dimension       = each.value.embedding_dims
  distance_metric = local.cfg.retrieval.distance_metric

  metadata_configuration {
    # S3 Vectors allows only 2 KB of *filterable* metadata per vector. Bedrock's
    # own chunk-text and document-metadata keys exceed that on their own, so they
    # must be declared here or every PutVectors call fails mid-ingestion with
    # "Filterable metadata must have at most 2048 bytes".
    #
    # Note this whole block is Forces-new-resource: the index cannot be amended
    # afterwards, so getting this list wrong means Terraform destroys and
    # recreates the index -- and you pay to re-embed the corpus. Our sidecar
    # attributes (chapter, pages) stay filterable, which is what makes
    # --chapter / --page-from work.
    non_filterable_metadata_keys = local.cfg.retrieval.non_filterable_metadata_keys
  }
}
