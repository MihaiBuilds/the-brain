# The Brain — Docker image
# Single-stage: pure-Python workflow orchestrator, no native build steps.

FROM python:3.14-slim

WORKDIR /app

# Copy project files and install
COPY pyproject.toml ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY examples/ ./examples/
COPY scripts/start.sh ./scripts/start.sh

ENV PYTHONPATH=/app

RUN pip install --no-cache-dir . \
    && sed -i 's/\r$//' ./scripts/start.sh \
    && chmod +x ./scripts/start.sh

ENTRYPOINT ["./scripts/start.sh"]
