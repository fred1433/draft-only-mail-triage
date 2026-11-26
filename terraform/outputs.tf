output "job_definition_id" {
  value = scaleway_job_definition.triage.id
}

output "registry_endpoint" {
  value = scaleway_registry_namespace.triage.endpoint
}
