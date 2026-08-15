# Everything that describes *what* to build lives in config/config.yaml, which
# this module reads directly. These variables only describe *where* this stack
# sits relative to the Python-built one, and the couple of behaviours that have
# no meaningful config.yaml home.

variable "config_path" {
  description = <<-EOT
    The config file to build from, relative to this directory. The Terraform
    equivalent of the KB_CONFIG environment variable the Python CLI honours.
  EOT
  type        = string
  default     = "../config/config.yaml"
}

variable "name_suffix" {
  description = <<-EOT
    Appended to `aws.project_prefix` from config.yaml to namespace every resource
    this stack owns.

    The default keeps the Terraform stack strictly parallel to the one
    `kb provision` already built (`starccm-kb-tf-*` vs `starccm-kb-*`), so both
    can exist in the account at once and neither can adopt or clobber the other's
    resources.

    Set to "" to use the bare config.yaml prefix -- only correct if the Python
    stack is gone, or if you are deliberately importing its resources.
  EOT
  type        = string
  default     = "tf"

  validation {
    condition     = can(regex("^[a-z0-9-]*$", var.name_suffix))
    error_message = "name_suffix must be lowercase alphanumeric or hyphens: it becomes part of S3 bucket names."
  }
}

variable "experiments" {
  description = <<-EOT
    Which experiments from config.yaml to build. Mirrors the CLI's semantics:
    null uses `default_experiments`, ["all"] builds every defined experiment.
  EOT
  type        = list(string)
  default     = null
}

variable "manage_corpus_objects" {
  description = <<-EOT
    Upload the split PDFs and their metadata sidecars as `aws_s3_object`,
    replacing `kb upload`. Requires `kb split` to have written
    <split_dir>/<manifest_name> first.

    Set false to have Terraform create the buckets but leave their contents to
    `kb upload`.
  EOT
  type        = bool
  default     = true
}

variable "iam_propagation_delay" {
  description = <<-EOT
    Wait inserted between writing the KB role policy and creating the knowledge
    base. CreateKnowledgeBase validates the role's S3 access, and IAM is
    eventually consistent, so without this the first apply fails with a
    ValidationException. This is provision.py's `time.sleep(20)`; Terraform has no
    retry hook of its own here, so re-run `apply` if it still loses the race.
  EOT
  type        = string
  default     = "25s"
}

variable "state_file_rel" {
  description = <<-EOT
    Where to write the resource-ID handoff file, relative to the repository root.

    Deliberately NOT `.kb-state.json`: that file belongs to the Python-built
    stack, and overwriting it would strand those knowledge bases. The rendered
    config this module also emits points `paths.state_file` here.
  EOT
  type        = string
  default     = "terraform/kb-state.json"
}

variable "rendered_config_rel" {
  description = <<-EOT
    Where to write the derived config, relative to the repository root. It is
    config/config.yaml with exactly two keys overridden -- `aws.project_prefix`
    and `paths.state_file` -- so `KB_CONFIG=<this> uv run kb ingest` drives the
    Terraform stack with no changes to the Python code.
  EOT
  type        = string
  default     = "terraform/config.tf.yaml"
}

variable "force_destroy" {
  description = <<-EOT
    Allow `terraform destroy` to delete non-empty S3 buckets and vector buckets
    that still hold indexes and vectors.

    Off by default because destroying the vector store means paying to re-embed,
    and FM-parsing the corpus again is the largest single cost in this project.
  EOT
  type        = bool
  default     = false
}
