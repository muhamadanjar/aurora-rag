DOCKER_COMPOSE ?= docker compose
ENV_FILE ?= .env.docker
AURORA_URL ?= http://127.0.0.1:8007

COMPOSE_FILES := -f compose.yaml
ifneq ($(wildcard compose.codebase-roots.yaml),)
COMPOSE_FILES += -f compose.codebase-roots.yaml
endif
COMPOSE := $(DOCKER_COMPOSE) --env-file $(ENV_FILE) $(COMPOSE_FILES)

.PHONY: help multi-root config up ps logs health down worker

help:
	@printf '%s\n' \
	  'make multi-root  Copy the multi-root Compose template (does not overwrite)' \
	  'make config      Validate Compose configuration' \
	  'make up          Build and start/recreate the Aurora API' \
	  'make ps          Show Compose service status' \
	  'make logs        Follow Aurora API logs' \
	  'make worker      Follow durable codebase worker logs' \
	  'make health      Check the Aurora OpenAPI endpoint' \
	  'make down        Stop Compose services; preserve named volumes'

multi-root:
	@if [ -e compose.codebase-roots.yaml ]; then \
	  echo 'compose.codebase-roots.yaml already exists; leaving it unchanged'; \
	else \
	  cp compose.codebase-roots.example.yaml compose.codebase-roots.yaml; \
	  echo 'Created compose.codebase-roots.yaml; edit its host paths before running make up'; \
	fi

config:
	$(COMPOSE) config -q

up:
	$(COMPOSE) build api worker
	$(COMPOSE) run --rm --no-deps api alembic upgrade head
	$(COMPOSE) up -d --force-recreate api worker

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f api

worker:
	$(COMPOSE) logs -f worker

health:
	curl --fail --silent --show-error $(AURORA_URL)/openapi.json -o /dev/null
	@printf 'Aurora API is reachable at %s\n' '$(AURORA_URL)'

down:
	$(COMPOSE) down
