# config/config.yaml is the single source of truth for the Terraform stack too.
# Nothing is duplicated and nothing is defaulted here: every value below is read
# out of the YAML by direct attribute access, which Terraform hard-errors on when
# the key is absent ("This object does not have an attribute named ..."). That
# reproduces config.py's central property -- an experiment can never run under a
# setting nobody stated -- at plan time instead of import time.
#
# The invariants config.py enforces *semantically* (judge != generation, VECTOR
# experiments must name an embedding model) cannot be expressed by attribute
# access alone, so they are preconditions on `terraform_data.validate_config`
# below. A failed precondition fails the plan, so nothing is ever created under a
# configuration the Python loader would have rejected.

locals {
  # Relative paths resolve against this directory; absolute ones are taken as
  # given, so `config_path` behaves like the KB_CONFIG environment variable.
  config_path = startswith(var.config_path, "/") ? var.config_path : "${path.module}/${var.config_path}"
  cfg         = yamldecode(file(local.config_path))

  # --- aws -------------------------------------------------------------------
  # account_id is the one key config.yaml permits to be null, because STS can
  # discover it and it must match the caller either way.
  account_id = coalesce(local.cfg.aws.account_id, data.aws_caller_identity.current.account_id)

  project = var.name_suffix == "" ? local.cfg.aws.project_prefix : "${local.cfg.aws.project_prefix}-${var.name_suffix}"

  # The one region every resource in this module lives in, matching config.py's
  # PRIMARY_REGION. It is deliberately not per-experiment: an ingestion job cannot
  # read a cross-region corpus bucket, so a split region means a second copy of
  # the corpus and a second FM-parsing bill.
  #
  # This is also why no resource below carries a `region` argument. The provider
  # block takes its region from this same value, so the per-resource override the
  # AWS provider offers is simply never used -- which makes the split
  # unrepresentable rather than merely discouraged.
  region = local.cfg.aws.primary_region

  # --- model ARNs ------------------------------------------------------------
  # Mirrors aws.model_arn(): inference-profile ids are region-prefixed and live
  # under a different ARN resource type than on-demand foundation models. A
  # missing prefix here builds a malformed ARN that Bedrock rejects at create
  # time, which is why the list is explicit rather than a "starts with two
  # letters and a dot" heuristic.
  inference_profile_prefixes = ["us.", "eu.", "jp.", "apac.", "global."]

  fm_parser = local.cfg.models.fm_parser

  # --- experiments -----------------------------------------------------------
  # Normalised into a uniform map so it can drive for_each. The raw
  # `cfg.experiments` cannot: yamldecode gives it an object type whose three
  # values have different attribute sets, and Terraform refuses to convert that
  # to a map. Iterating with `for k, v in` sidesteps the conversion.
  #
  # Which keys are required depends on kb_type, exactly as in
  # config._build_experiment: a VECTOR experiment must name its embedding model,
  # dimensions, data type, and parsing modality; a MANAGED one must name its
  # connector version and image extraction setting, and must not name the others
  # because AWS supplies them.
  experiments_all = {
    for k, v in local.cfg.experiments : k => {
      key              = k
      label            = v.label
      kb_type          = v.kb_type
      parsing_strategy = v.parsing_strategy
      uses_s3_vectors  = v.uses_s3_vectors
      notes            = v.notes

      embedding_model     = v.kb_type == "VECTOR" ? v.embedding_model : null
      embedding_dims      = v.kb_type == "VECTOR" ? v.embedding_dims : null
      embedding_data_type = v.kb_type == "VECTOR" ? v.embedding_data_type : null
      parsing_modality    = v.kb_type == "VECTOR" ? v.parsing_modality : null

      # The FM parser is shared across experiments, so it lives under models.
      parser_model = v.kb_type == "VECTOR" ? local.fm_parser : null

      connector_version = v.kb_type == "MANAGED" ? v.connector_version : null
      image_extraction  = v.kb_type == "MANAGED" ? v.image_extraction : null
    }
  }

  requested = (
    var.experiments == null ? local.cfg.default_experiments :
    (length(var.experiments) == 1 && var.experiments[0] == "all" ? keys(local.experiments_all) : var.experiments)
  )

  selected = {
    for k, e in local.experiments_all : k => e if contains(local.requested, k)
  }

  vector_experiments       = { for k, e in local.selected : k => e if e.uses_s3_vectors }
  supplemental_experiments = { for k, e in local.selected : k => e if e.kb_type != "MANAGED" }

  # One corpus bucket for every experiment, which is only possible because they
  # share a region. Same shape as upload.bucket_name().
  corpus_bucket = "${local.project}-corpus-${local.account_id}-${local.region}"

  # --- derived resource names ------------------------------------------------
  # Same shapes as the Experiment properties in config.py, so a resource built
  # here is recognisable next to one built by `kb provision`.
  names = {
    for k, e in local.selected : k => {
      kb            = "${local.project}-${k}"
      role          = "${local.project}-${k}-kb-role"
      vector_bucket = "${local.project}-vectors-${k}"
      vector_index  = "${local.project}-${k}-index"
      supplemental  = "${local.project}-multimodal-${local.account_id}-${k}"
      data_source   = "${local.project}-${k}-s3"
    }
  }

  embedding_model_arn = {
    for k, e in local.selected : k => (
      e.embedding_model == null ? null :
      anytrue([for p in local.inference_profile_prefixes : startswith(e.embedding_model, p)])
      ? "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/${e.embedding_model}"
      : "arn:aws:bedrock:${local.region}::foundation-model/${e.embedding_model}"
    )
  }

  parser_model_arn = {
    for k, e in local.selected : k => (
      e.parser_model == null ? null :
      anytrue([for p in local.inference_profile_prefixes : startswith(e.parser_model, p)])
      ? "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/${e.parser_model}"
      : "arn:aws:bedrock:${local.region}::foundation-model/${e.parser_model}"
    )
  }
}

resource "terraform_data" "validate_config" {
  input = local.config_path

  lifecycle {
    precondition {
      condition     = local.cfg.models.judge != local.cfg.models.generation
      error_message = "config.yaml: models.judge must differ from models.generation. A model grading its own answers reports its own blind spots as correct."
    }

    precondition {
      condition     = length(setsubtract(toset(local.requested), toset(keys(local.experiments_all)))) == 0
      error_message = "Unknown experiment(s) requested: ${join(", ", setsubtract(toset(local.requested), toset(keys(local.experiments_all))))}. Defined in config.yaml: ${join(", ", keys(local.experiments_all))}."
    }

    precondition {
      condition = alltrue([
        for k, e in local.experiments_all : contains(["VECTOR", "MANAGED"], e.kb_type)
      ])
      error_message = "config.yaml: every experiments.<key>.kb_type must be VECTOR or MANAGED."
    }

    # Rejected rather than ignored, mirroring config.py. Every experiment runs in
    # aws.primary_region, so a leftover per-experiment `region:` reads as though
    # it were in force while this module builds everything somewhere else.
    precondition {
      condition = alltrue([
        for k, v in local.cfg.experiments : !can(v.region)
      ])
      error_message = "config.yaml: 'experiments.<key>.region' is not a valid key. Every experiment runs in aws.primary_region (${local.cfg.aws.primary_region}); a per-experiment region would need its own copy of the corpus, because an ingestion job cannot read cross-region."
    }

    # config.py rejects a null here rather than falling back to something
    # plausible; so does this.
    precondition {
      condition = alltrue([
        for k, e in local.experiments_all : (
          e.kb_type != "VECTOR" ? true :
          e.embedding_model != null && e.embedding_dims != null && e.embedding_data_type != null && e.parsing_modality != null
        )
      ])
      error_message = "config.yaml: a VECTOR experiment must set embedding_model, embedding_dims, embedding_data_type, and parsing_modality to non-null values."
    }

    precondition {
      condition = alltrue([
        for k, e in local.experiments_all : (
          e.kb_type != "MANAGED" ? true : e.connector_version != null && e.image_extraction != null
        )
      ])
      error_message = "config.yaml: a MANAGED experiment must set connector_version and image_extraction."
    }

    precondition {
      condition = alltrue([
        for k, v in local.cfg.tags : can(regex(".+", v))
      ])
      error_message = "config.yaml: every tags.<key> must be a non-empty string. An empty tag value is silently useless for cost attribution."
    }

    # If config.yaml pins an account, it must be the one we are authenticated as
    # -- otherwise every ARN this module builds points somewhere else.
    precondition {
      condition     = local.cfg.aws.account_id == null || local.cfg.aws.account_id == data.aws_caller_identity.current.account_id
      error_message = "config.yaml pins aws.account_id to ${coalesce(local.cfg.aws.account_id, "null")} but the current credentials are for ${data.aws_caller_identity.current.account_id}."
    }

    # S3 Vectors only accepts float32 today, and `dimension` must be an integer.
    precondition {
      condition = alltrue([
        for k, e in local.vector_experiments : lower(e.embedding_data_type) == "float32"
      ])
      error_message = "aws_s3vectors_index only supports data_type float32, so an S3-Vectors-backed experiment cannot set embedding_data_type to anything else."
    }
  }
}
