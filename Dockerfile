FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir \
    fastapi>=0.104.0 \
    uvicorn[standard]>=0.24.0 \
    pydantic>=2.0.0 \
    python-multipart>=0.0.6 \
    PyJWT>=2.8.0 \
    $(cat requirements.txt | grep -v '^$' | grep -v '^#')

# Copy application files
COPY http_wrapper.py .
COPY oauth_middleware.py .
COPY space_calculator.py .
COPY wrapper.py .

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8851/health || exit 1

# Run the application
CMD ["python", "-m", "uvicorn", "http_wrapper:app", "--host", "0.0.0.0", "--port", "8851"]
