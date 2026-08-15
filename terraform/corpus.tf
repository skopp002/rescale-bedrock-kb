# The corpus objects, replacing `kb upload`.
#
# The split itself stays in Python -- cutting a 14,125-page PDF on its own outline
# is not something Terraform can do -- so this reads the manifest that
# `kb split` writes and uploads exactly the parts it lists. That keeps the
# manifest the single description of the corpus for both stacks.
#
# Each PDF gets a `<key>.metadata.json` sidecar, which is how Bedrock KB attaches
# filterable metadata to every chunk derived from that file. It is what makes the
# split invisible at query time (`--chapter`, `--page-from`), so it is generated
# here from the same manifest fields rather than being duplicated by hand.

locals {
  split_dir     = "${path.module}/../${local.cfg.paths.split_dir}"
  manifest_path = "${local.split_dir}/${local.cfg.paths.manifest_name}"

  # The conditional selects between two *strings* before decoding, rather than
  # between a decoded manifest and an empty stand-in: Terraform cannot unify an
  # object that has `parts`, `source_pdf`, ... with one that only has `parts`.
  manifest_parts = jsondecode(
    var.manage_corpus_objects && fileexists(local.manifest_path)
    ? file(local.manifest_path)
    : jsonencode({ parts = [] })
  ).parts

  # `source_pdf` is what re-unifies the parts into one logical document, so it
  # comes from config.yaml's path rather than from any individual part.
  source_pdf_name = basename(local.cfg.paths.source_pdf)

  # Mirrors upload.metadata_for(). `chapter` is embedded because a chapter title
  # is real semantic context; page numbers are not, because they would pollute
  # the vectors.
  #
  # start_page/end_page are +1 because the manifest stores 0-based page indexes
  # while citations and --page-from are 1-based pages of the original guide.
  part_metadata = {
    for p in local.manifest_parts : p.filename => {
      metadataAttributes = {
        chapter = {
          value               = { type = "STRING", stringValue = p.chapter }
          includeForEmbedding = true
        }
        source_pdf = {
          value               = { type = "STRING", stringValue = local.source_pdf_name }
          includeForEmbedding = false
        }
        # Which slice of a multi-part chapter this is, for provenance.
        part = {
          value               = { type = "STRING", stringValue = "${p.part_index}/${p.part_count}" }
          includeForEmbedding = false
        }
        start_page = {
          value               = { type = "NUMBER", numberValue = p.start_page + 1 }
          includeForEmbedding = false
        }
        end_page = {
          value               = { type = "NUMBER", numberValue = p.end_page + 1 }
          includeForEmbedding = false
        }
        pages = {
          value               = { type = "NUMBER", numberValue = p.pages }
          includeForEmbedding = false
        }
      }
    }
  }

  # One object per part, and only one: every experiment reads the same bucket
  # because they share aws.primary_region. An ingestion job cannot read
  # cross-region, so a per-experiment region would fan this out into a copy of
  # the whole corpus per region.
  corpus_filenames = toset([for p in local.manifest_parts : p.filename])
}

resource "aws_s3_object" "corpus_pdf" {
  for_each = local.corpus_filenames

  bucket       = aws_s3_bucket.corpus.id
  key          = "${local.cfg.paths.corpus_prefix}${each.value}"
  source       = "${local.split_dir}/${each.value}"
  content_type = "application/pdf"
  # Without this Terraform cannot tell a re-split file from the uploaded one, and
  # would never replace the object. It is the declarative form of
  # upload._needs_upload, and stricter: a content change of the same size still
  # triggers a re-upload.
  etag = filemd5("${local.split_dir}/${each.value}")
}

resource "aws_s3_object" "corpus_metadata" {
  for_each = local.corpus_filenames

  bucket       = aws_s3_bucket.corpus.id
  key          = "${local.cfg.paths.corpus_prefix}${each.value}.metadata.json"
  content      = jsonencode(local.part_metadata[each.value])
  content_type = "application/json"
}

# `kb split` has to run before `terraform apply` when this module owns the
# corpus. A missing manifest is otherwise a silent no-op: buckets get created,
# the knowledge base gets created, and the first ingestion job indexes nothing.
check "corpus_present" {
  assert {
    condition     = !var.manage_corpus_objects || fileexists(local.manifest_path)
    error_message = "manage_corpus_objects is true but there is no manifest at ${local.manifest_path}. Run `uv run kb split` first, or set manage_corpus_objects = false to upload with `kb upload` instead."
  }

  assert {
    condition     = !var.manage_corpus_objects || length(local.manifest_parts) > 0
    error_message = "The manifest at ${local.manifest_path} lists no parts, so nothing will be uploaded and the knowledge bases will index an empty corpus."
  }
}
