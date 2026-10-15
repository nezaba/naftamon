FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
COPY app ./app
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001

FROM base AS runtime
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--access-log"]

FROM base AS test
USER root
COPY requirements-dev.txt ./requirements-dev.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements-dev.txt
COPY tests ./tests
RUN chown -R 10001:10001 /app
USER 10001:10001
CMD ["python", "-m", "pytest", "-q", "tests"]
