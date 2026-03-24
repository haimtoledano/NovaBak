FROM python:3.11-slim

# Metadata
LABEL maintainer="THIS Cyber Security" \
      description="NovaBak — VM Backup Enterprise"

# System dependencies (for pysmb, cryptography, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create required directories
RUN mkdir -p data bin/ovftool static

# The data directory will be mounted as a volume
VOLUME ["/app/data"]

# Expose Web UI port
EXPOSE 8000

# Default: run web service
# (Worker daemon runs as a separate container via docker-compose)
CMD ["python", "-u", "main.py"]
