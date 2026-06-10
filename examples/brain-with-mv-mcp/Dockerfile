# Example: derived The Brain image with Memory Vault's MCP server installed.
#
# This is the canonical "derive your own image" pattern for The Brain workflows
# that call Memory Vault via McpToolStep. Memory Vault is installed into a
# SEPARATE path (/opt/memory-vault) so its `src/` package does not collide with
# The Brain's own `src/` package. Workflows then spawn MV's MCP server by
# setting PYTHONPATH for that subprocess only:
#
#     server_command = "env PYTHONPATH=/opt/memory-vault python -m src.mcp"
#
# Memory Vault's MCP server connects to a Postgres instance via env vars
# (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD). See docker-compose.yml in
# this folder for the full wiring — MV's Postgres is a separate container so
# Brain's Postgres and MV's Postgres do not share schema.
#
# The Brain itself stays unchanged — stock image bundles ZERO MCP servers, per
# the ecosystem rule. This derived image is what YOU build if you want this
# specific composition.
#
# Build (from the repo root):
#   docker build -f examples/brain-with-mv-mcp/Dockerfile -t brain-with-mv-mcp .

# Start from the stock Brain image. Locally-built tag from docker-compose;
# in production you'd use ghcr.io/mihaibuilds/the-brain:latest.
FROM the-brain-brain:latest

USER root

# python:3.14-slim doesn't include curl/tar — install them so we can fetch
# MV's source tarball from GitHub.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# CPU-only PyTorch — MV's sentence-transformers needs torch for embeddings.
# Pin the index URL so we get the smaller CPU wheel, not the GPU one.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Pull Memory Vault source as a tarball and unpack into /opt/memory-vault.
# We do NOT pip install MV's package — that would put MV's `src/` on the
# default Python path and collide with The Brain's `src/`. Instead, we
# install MV's runtime dependencies via pip (read from MV's pyproject) and
# keep MV's source files at a separate path that only the MCP subprocess
# sees via PYTHONPATH.
ARG MV_REF=main
RUN mkdir -p /opt/memory-vault \
    && curl -fsSL "https://github.com/MihaiBuilds/memory-vault/archive/refs/heads/${MV_REF}.tar.gz" -o /tmp/mv.tar.gz \
    && tar -xzf /tmp/mv.tar.gz -C /tmp \
    && cp -r /tmp/memory-vault-${MV_REF}/src /opt/memory-vault/src \
    && cp -r /tmp/memory-vault-${MV_REF}/migrations /opt/memory-vault/migrations \
    && cp /tmp/memory-vault-${MV_REF}/pyproject.toml /opt/memory-vault/pyproject.toml \
    && rm -rf /tmp/mv.tar.gz /tmp/memory-vault-${MV_REF}

# Install MV's runtime dependencies (NOT MV itself — we use it via PYTHONPATH).
# `pip install --no-deps` of MV would also work but is fragile; instead we
# read MV's pyproject and install what it lists.
RUN pip install --no-cache-dir \
        "psycopg[binary]>=3.3.4,<4" \
        "psycopg-pool>=3.1,<4" \
        "sentence-transformers>=2.2,<4" \
        "numpy>=2.4.5" \
        "python-dotenv>=1.2.2" \
        "mcp>=1.27.1" \
        "spacy>=3.7,<4" \
    && python -m spacy download en_core_web_sm

# The Brain's ENTRYPOINT, PYTHONPATH=/app, and CLI all carry forward unchanged.
# MV is reachable only when a workflow step explicitly sets
# PYTHONPATH=/opt/memory-vault for that subprocess.
