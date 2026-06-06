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
  description = "Container image URI, e.g. gcr.io/shipsafe-ai/agentops:latest"
}

variable "gemini_model" {
  type        = string
  description = "Gemini model name — read from config, never hardcoded (Rule 7)"
  default     = "gemini-2.0-flash"
}
