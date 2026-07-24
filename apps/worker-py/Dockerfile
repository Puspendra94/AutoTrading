FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "asyncpg>=0.29" "httpx>=0.27" "apscheduler>=3.10" "python-dotenv>=1.0"

COPY worker ./worker

# Long-running scheduler process (24/7).
CMD ["python", "-m", "worker.main"]
