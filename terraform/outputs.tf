output "project_prefix" {
  description = "Prefix every resource in this stack is named under."
  value       = local.project
}

output "experiments" {
  description = "The experiments this stack built, with the IDs each pipeline stage needs."
  value = {
    for k, e in local.selected : k => {
      label             = e.label
      kb_type           = e.kb_type
      region            = local.region
      knowledge_base_id = aws_bedrockagent_knowledge_base.kb[k].id
      data_source_id    = aws_bedrockagent_data_source.kb[k].data_source_id
      role_arn          = aws_iam_role.kb[k].arn
      corpus_bucket     = local.corpus_bucket
      # null for the managed KB, which has no customer vector store.
      vector_index_arn = try(aws_s3vectors_index.kb[k].index_arn, null)
      # null for the managed KB, whose parser output AWS owns.
      supplemental_bucket = try(aws_s3_bucket.supplemental[k].id, null)
    }
  }
}

output "corpus_objects_uploaded" {
  description = "Number of corpus PDFs uploaded. 0 means `kb split` has not run or manage_corpus_objects is false."
  value       = length(aws_s3_object.corpus_pdf)
}

output "next_steps" {
  description = "Terraform cannot start an ingestion job; this is the rest of the pipeline."
  value       = <<-EOT
    Terraform has built the infrastructure and uploaded the corpus, but a
    knowledge base is not queryable until it has ingested. There is no
    StartIngestionJob resource in the AWS provider, so run the remaining stages
    against the generated config:

      export KB_CONFIG=${var.rendered_config_rel}
      uv run kb ingest ${join(" ", sort(keys(local.selected)))}
      uv run kb status
      uv run kb eval ${join(" ", sort(keys(local.selected)))}
      uv run kb compare

    Resource IDs are in ${var.state_file_rel}.
  EOT
}
