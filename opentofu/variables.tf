variable "cesium_ion_token" {
  description = <<-EOT
    Cesium ion access token for world imagery on the globe. Optional: with
    the default empty string the frontend falls back to OpenStreetMap tiles.
    Supply it out-of-band (gitignored *.auto.tfvars or TF_VAR_ — never
    committed); it ends up in the public config.js either way (ion tokens
    are client-side by design), but keeping it out of git means rotating it
    never requires a history rewrite.
  EOT
  type        = string
  default     = ""
}
