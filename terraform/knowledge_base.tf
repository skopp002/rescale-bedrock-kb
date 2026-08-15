# The knowledge bases. Two shapes, selected by kb_type:
#
#   VECTOR  (titan, nova) -- we own the vector store (S3 Vectors), the chunking
#       strategy, and the parser. Everything is stated explicitly.
#   MANAGED (managed)     -- AWS owns the vector store, chunking, embeddings, and
#       parsing, so there is no storage_configuration at all and the whole
#       configuration is one line.
#
# The contrast between the two dynamic blocks below is the point of the
# experiment: it is the entire surface area difference between managing a vector
# store and not.

resource "aws_bedrockagent_knowledge_base" "kb" {
  for_each = local.selected

  name        = local.names[each.key].kb
  description = each.value.label
  role_arn    = aws_iam_role.kb[each.key].arn

  knowledge_base_configuration {
    type = each.value.kb_type

    dynamic "vector_knowledge_base_configuration" {
      for_each = each.value.kb_type == "VECTOR" ? [1] : []
      content {
        embedding_model_arn = local.embedding_model_arn[each.key]

        embedding_model_configuration {
          bedrock_embedding_model_configuration {
            dimensions          = each.value.embedding_dims
            embedding_data_type = each.value.embedding_data_type
          }
        }

        supplemental_data_storage_configuration {
          storage_location {
            type = "S3"
            s3_location {
              # Bucket root only -- Bedrock rejects any sub-folder in this URI.
              uri = "s3://${aws_s3_bucket.supplemental[each.key].id}"
            }
          }
        }
      }
    }

    dynamic "managed_knowledge_base_configuration" {
      for_each = each.value.kb_type == "MANAGED" ? [1] : []
      content {
        # MANAGED = let AWS pick and manage the embedder. The provider also
        # accepts CUSTOM here with an explicit model, which config.yaml does not
        # currently exercise -- doing so would make the managed KB a different
        # experiment than "AWS chooses everything".
        embedding_model_type = "MANAGED"
      }
    }
  }

  # Omitted entirely for a MANAGED knowledge base: Amazon Bedrock manages the
  # vector store, and supplying this is an error rather than a redundancy.
  dynamic "storage_configuration" {
    for_each = each.value.uses_s3_vectors ? [1] : []
    content {
      type = "S3_VECTORS"
      s3_vectors_configuration {
        index_arn = aws_s3vectors_index.kb[each.key].index_arn
      }
    }
  }

  depends_on = [
    # CreateKnowledgeBase validates the role's S3 access, so the corpus bucket
    # must already exist and the role policy must have had time to propagate.
    time_sleep.iam_propagation,
    aws_s3_bucket.corpus,
  ]
}
