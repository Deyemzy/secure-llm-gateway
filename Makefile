# AWS credentials: the standard chain (export AWS_PROFILE=<your-profile> if you use named profiles).
AWS_REGION  ?= us-east-1
TAG         ?= v0.3.0
TF          := terraform -chdir=terraform/envs/dev
PY          := .venv/bin/python

.PHONY: hooks secrets-scan venv test test-redis test-live evidence verify-iam verify-guardrail run-local valkey-up valkey-down fmt validate plan apply destroy push redeploy logs

VALKEY_ENV := REDIS_TEST_URL=redis://slg-test:test-only@127.0.0.1:6379/0 \
              REDIS_TEST_APP_URL=redis://slg-gateway:local-dev-only@127.0.0.1:6379/0 \
              REDIS_TEST_REPLICA_URL=redis://slg-test:test-only@127.0.0.1:6380/0 \
              REQUIRE_REDIS=1

hooks:                ## enable the repo's git hooks (gitleaks secret scan; no AI attribution in commit messages)
	git config core.hooksPath .githooks && echo "hooks enabled: .githooks/pre-commit"

secrets-scan:         ## gitleaks over full history + the files that would be committed
	gitleaks git --log-opts="--all" --redact --no-banner
	tmp=$$(mktemp -d) && git ls-files -co --exclude-standard | rsync -a --files-from=- . $$tmp && \
	  gitleaks dir --redact --no-banner $$tmp; rc=$$?; rm -rf $$tmp; exit $$rc

venv:
	python3 -m venv .venv && .venv/bin/pip install -q -r gateway/requirements.txt pytest httpx openai

test:                 ## unit + pipeline abuse tests (no AWS, no Redis)
	$(PY) -m pytest -q -m "not live"

valkey-up:            ## local Valkey primary + replica with the production ACL (docker compose)
	docker compose up -d --wait

valkey-down:
	docker compose down

test-redis: valkey-up ## same as CI: ledger, concurrency, replica-ack, failover on real Valkey as the least-privilege user
	$(VALKEY_ENV) $(PY) -m pytest -q -m "not live"

evidence: valkey-up   ## run everything and write docs/evidence/test-evidence.md
	$(VALKEY_ENV) $(PY) scripts/evidence.py

verify-iam:           ## post-deploy: IAM simulation matrix -> docs/evidence/iam-simulation.md
	$(PY) scripts/verify_iam.py

verify-guardrail:     ## post-deploy: real Converse calls from inside the VPC -> docs/evidence/guardrail-probe.md
	scripts/verify_guardrail.sh

test-live:            ## black-box abuse tests: GATEWAY_URL + GATEWAY_TOKEN
	$(PY) -m pytest -q -m live

run-local:            ## gateway on :8080 with dev JWTs, in-memory budget, real Bedrock
	@test -f .dev/jwks.json || $(PY) scripts/dev_token.py init
	cd gateway && eval "$$(../$(PY) ../scripts/dev_token.py env)" && AWS_REGION=$(AWS_REGION) \
	  ../.venv/bin/uvicorn app.main:build --factory --host 127.0.0.1 --port 8080 --no-server-header --no-access-log

fmt:
	terraform fmt -recursive terraform

validate:
	$(TF) init -input=false -backend=false >/dev/null && $(TF) validate

plan:
	$(TF) plan

apply:
	$(TF) apply

destroy:
	$(TF) destroy

push:                 ## build ARM64 image and push to ECR
	$(eval REPO := $(shell $(TF) output -raw ecr_repository_url))
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $(firstword $(subst /, ,$(REPO)))
	docker buildx build --platform linux/arm64 -t $(REPO):$(TAG) --push gateway

redeploy:
	aws ecs update-service --region $(AWS_REGION) --cluster $$($(TF) output -raw cluster_name) \
	  --service $$($(TF) output -raw service_name) --force-new-deployment >/dev/null && echo redeploying

logs:
	aws logs tail /slg-dev/gateway --region $(AWS_REGION) --follow --format short
