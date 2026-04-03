terraform {
  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.0"
    }
  }
}

provider "docker" {}

variable "postgres_password" {
  type    = string
  default = "fraud123"
}

docker_network "fraudshield" {
  name = "fraudshield"
}

docker_image "postgres" {
  name         = "postgres:16-alpine"
  keep_locally = true
}

docker_image "redis" {
  name         = "redis:7-alpine"
  keep_locally = true
}

docker_container "postgres" {
  name  = "fraudshield-postgres"
  image = docker_image.postgres.image_id

  env = [
    "POSTGRES_USER=fraud",
    "POSTGRES_PASSWORD=${var.postgres_password}",
    "POSTGRES_DB=fraudshield",
  ]

  ports {
    internal = 5432
    external = 5432
  }

  healthcheck {
    test         = ["CMD-SHELL", "pg_isready -U fraud"]
    interval     = "5s"
    timeout      = "5s"
    retries      = 5
    start_period = "5s"
  }

  networks_advanced {
    name = docker_network.fraudshield.name
  }
}

docker_container "redis" {
  name  = "fraudshield-redis"
  image = docker_image.redis.image_id

  ports {
    internal = 6379
    external = 6379
  }

  networks_advanced {
    name = docker_network.fraudshield.name
  }
}

output "postgres_port" {
  value = 5432
}

output "redis_port" {
  value = 6379
}
