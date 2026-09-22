# Three-way JSON Merge API
FROM python:3.12-slim

# Do not buffer stdout/stderr and do not write .pyc files.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser app ./app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import json,urllib.request,sys; \
sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)).get('status') == 'ok' else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
