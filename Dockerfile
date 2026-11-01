FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .
COPY src/ ./src/

RUN pip install --no-cache-dir .

FROM base AS test

RUN pip install --no-cache-dir pytest pytest-cov

COPY tests/ ./tests/
COPY kibana/ ./kibana/

CMD ["python", "-m", "pytest", "-v"]

FROM base AS production

USER 10001

EXPOSE 8080

CMD ["python", "-m", "dataguardian.health_server"]
