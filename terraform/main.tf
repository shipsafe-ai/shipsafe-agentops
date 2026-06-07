terraform {
  required_version = ">= 1.7"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Cloud Run service — AgentOps agent
resource "google_cloud_run_v2_service" "agentops" {
  name     = "shipsafe-agentops"
  location = var.region

  template {
    containers {
      image = var.image_uri

      env {
        name = "DT_ENVIRONMENT"
        value_source {
          secret_key_ref {
            secret  = "DT_ENVIRONMENT"
            version = "latest"
          }
        }
      }
      env {
        name = "DT_OTLP_TOKEN"
        value_source {
          secret_key_ref {
            secret  = "DT_OTLP_TOKEN"
            version = "latest"
          }
        }
      }
      env {
        name = "DT_PLATFORM_TOKEN"
        value_source {
          secret_key_ref {
            secret  = "DT_PLATFORM_TOKEN"
            version = "latest"
          }
        }
      }
      env {
        name = "PHOENIX_API_KEY"
        value_source {
          secret_key_ref {
            secret  = "PHOENIX_API_KEY"
            version = "latest"
          }
        }
      }
      env {
        name = "PHOENIX_COLLECTOR_ENDPOINT"
        value_source {
          secret_key_ref {
            secret  = "PHOENIX_COLLECTOR_ENDPOINT"
            version = "latest"
          }
        }
      }
      env {
        name  = "GEMINI_MODEL"
        value = var.gemini_model
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }
    }

    service_account = google_service_account.agentops.email
  }
}

resource "google_service_account" "agentops" {
  account_id   = "shipsafe-agentops"
  display_name = "ShipSafe AgentOps Cloud Run SA"
}

# Grant Secret Manager access
resource "google_project_iam_member" "agentops_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.agentops.email}"
}

# Grant Vertex AI access (Gemini calls)
resource "google_project_iam_member" "agentops_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.agentops.email}"
}

# Allow unauthenticated invocations (dashboard + CLI can call without token)
resource "google_cloud_run_v2_service_iam_member" "agentops_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.agentops.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
