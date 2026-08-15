# The data sources. Again two shapes, and this is where the managed knowledge
# base diverges most from a plain one.
#
# Data sources are untagged, as in the Python stack: CreateDataSource has no tags
# parameter and a data source has no ARN to tag afterwards. Nothing is lost for
# cost attribution -- the ingestion cost it drives bills as Bedrock model usage
# regardless.

resource "aws_bedrockagent_data_source" "kb" {
  for_each = local.selected

  knowledge_base_id = aws_bedrockagent_knowledge_base.kb[each.key].id
  name              = local.names[each.key].data_source
  description       = "Split STAR-CCM+ guide PDFs under s3://${local.corpus_bucket}/${local.cfg.paths.corpus_prefix}"

  # Keep vectors when the data source is deleted -- avoids surprise re-embedding
  # costs on teardown/rebuild cycles.
  data_deletion_policy = "RETAIN"

  data_source_configuration {
    type = each.value.kb_type == "MANAGED" ? "MANAGED_KNOWLEDGE_BASE_CONNECTOR" : "S3"

    dynamic "s3_configuration" {
      for_each = each.value.kb_type == "MANAGED" ? [] : [1]
      content {
        bucket_arn         = aws_s3_bucket.corpus.arn
        inclusion_prefixes = [local.cfg.paths.corpus_prefix]
      }
    }

    dynamic "managed_knowledge_base_connector_configuration" {
      for_each = each.value.kb_type == "MANAGED" ? [1] : []
      content {
        # `connector_parameters` is a JSON-encoded document, so neither Terraform
        # nor the provider validates its shape -- exactly like the boto3
        # `document` type. A wrong shape returns a successful apply and the data
        # source flips to FAILED asynchronously.
        #
        # The managed connector wants a *flat* connectionConfiguration keyed by
        # bucket name and owner account -- not the bucketArn a plain S3 data
        # source takes. `version` is required and only "1" is currently accepted,
        # which is why config.yaml states it rather than this hardcoding it.
        connector_parameters = jsonencode({
          type    = "S3"
          version = each.value.connector_version
          connectionConfiguration = {
            bucketName           = local.corpus_bucket
            bucketOwnerAccountId = local.account_id
            inclusionPrefixes    = [local.cfg.paths.corpus_prefix]
          }
        })

        # The managed connector controls its own multimodal extraction, rather
        # than it being a property of the parser as it is for a VECTOR KB.
        media_extraction_configuration {
          image_extraction_configuration {
            image_extraction_status = each.value.image_extraction
          }
        }
      }
    }
  }

  # Chunking and parsing.
  #
  # For a VECTOR experiment we state both. For MANAGED, AWS owns chunking
  # entirely, so only the parsing strategy is sent -- SMART_PARSING, with no model
  # configuration, because AWS also chooses the parser model.
  #
  # NOTE this diverges deliberately from provision.py, which sends no ingestion
  # configuration at all for a managed KB. Both are accepted; sending it means
  # `experiments.managed.parsing_strategy` in config.yaml is actually used rather
  # than silently ignored, which is the whole premise of that file. If a future
  # API change rejects it, drop this block for MANAGED and the KB still parses
  # with SMART_PARSING, because that is the managed default.
  dynamic "vector_ingestion_configuration" {
    for_each = [1]
    content {
      dynamic "chunking_configuration" {
        for_each = each.value.kb_type == "MANAGED" ? [] : [1]
        content {
          chunking_strategy = local.cfg.chunking.strategy

          dynamic "fixed_size_chunking_configuration" {
            for_each = local.cfg.chunking.strategy == "FIXED_SIZE" ? [1] : []
            content {
              max_tokens         = local.cfg.chunking.max_tokens
              overlap_percentage = local.cfg.chunking.overlap_percentage
            }
          }
        }
      }

      parsing_configuration {
        parsing_strategy = each.value.parsing_strategy

        dynamic "bedrock_foundation_model_configuration" {
          for_each = each.value.parsing_strategy == "BEDROCK_FOUNDATION_MODEL" ? [1] : []
          content {
            model_arn        = local.parser_model_arn[each.key]
            parsing_modality = each.value.parsing_modality
          }
        }
      }
    }
  }

  # The corpus should be in place before the data source exists, so the first
  # `kb ingest` has something to read. Terraform would otherwise be free to
  # create the data source while objects are still uploading.
  depends_on = [
    aws_s3_object.corpus_pdf,
    aws_s3_object.corpus_metadata,
  ]
}
