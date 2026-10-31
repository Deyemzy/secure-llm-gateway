# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

WORKDIR /srv
COPY requirements.txt .
# pip is removed after install: not needed at runtime (less attack surface, and its CA bundle goes with it).
RUN pip install --no-compile -r requirements.txt \
 && pip uninstall -y pip \
 && useradd --system --uid 10001 --no-create-home gateway

# policy.yaml is NOT baked in: the gateway reads it from SSM (POLICY_SSM_PARAMETER) and refuses to start without it.
COPY --chown=root:root app/ ./app/

USER 10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

# CMD (not ENTRYPOINT) so ECS can run `python -m app.probe` from the same image (scripts/verify_guardrail.sh).
CMD ["uvicorn", "app.main:build", "--factory", "--host", "0.0.0.0", "--port", "8080", \
            "--workers", "2", "--no-server-header", "--no-access-log", "--timeout-keep-alive", "75"]
