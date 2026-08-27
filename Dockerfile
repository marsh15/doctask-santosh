FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS web
WORKDIR /build/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANGGRAPH_STRICT_MSGPACK=true \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
RUN pip install --no-cache-dir uv==0.10.4
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
RUN addgroup --system app && adduser --system --ingroup app app
COPY --chown=app:app doctask/ doctask/
COPY --chown=app:app fixtures/rules/ fixtures/rules/
COPY --chown=app:app --from=web /build/apps/web/dist apps/web/dist
EXPOSE 8000
USER app
CMD ["uvicorn", "doctask.main:app", "--host", "0.0.0.0", "--port", "8000"]
