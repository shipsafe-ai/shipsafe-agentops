variable "project_id" {
  type        = string
  description = "GCP project ID"
  default     = "shipsafe-ai"
}

variable "region" {
  type        = string
  description = "GCP region for Cloud Run"
  default     = "us-central1"
}

variable "image_uri" {
  type        = string
  description = "Artifact Registry image URI, e.g. us-central1-docker.pkg.dev/shipsafe-ai/shipsafe/agentops:latest"
}

variable "gemini_model" {
  type        = string
  description = "Gemini model name — read from config, never hardcoded (Rule 7)"
  default     = "gemini-2.5-flash"
}
