terraform {
  # 1.9 for `terraform_data` lifecycle preconditions referencing locals.
  required_version = ">= 1.9.0"

  required_providers {
    # 6.56.0 is the floor: it is the release that added the Managed Knowledge
    # Base type (`managed_knowledge_base_configuration`) and the
    # `MANAGED_KNOWLEDGE_BASE_CONNECTOR` data source. Earlier 6.x can build the
    # `titan`/`nova` experiments but not `managed`.
    #
    # Other version-gated features this module depends on:
    #   6.24.0  aws_s3vectors_vector_bucket / aws_s3vectors_index
    #   6.25.0  aws_s3vectors_index.metadata_configuration
    #   6.27.0  knowledge base storage_configuration.s3_vectors_configuration
    #
    # This module deliberately does *not* use the per-resource `region` argument
    # (6.0.0). Every experiment lives in aws.primary_region, so the one provider
    # block is the only place a region is stated -- see config.tf.
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.56.0"
    }
    # Writes the .kb-state.json handoff file the Python CLI reads.
    local = {
      source  = "hashicorp/local"
      version = ">= 2.5.0"
    }
    # IAM propagation delay before CreateKnowledgeBase; see iam.tf.
    time = {
      source  = "hashicorp/time"
      version = ">= 0.12.0"
    }
  }
}
