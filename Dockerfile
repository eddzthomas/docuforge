# DocuForge — Container image
# ============================================================
# Base: Python 3.11 slim (Debian Bookworm)
# Includes poppler-utils for pdf2image (renders PDF pages to PNG)

FROM python:3.11-slim-bookworm

# Install system dependencies:
#   poppler-utils  —  pdftoppm, pdfinfo (required by pdf2image)
#   tesseract-ocr  —  included for fallback / future use
#   libgl1-mesa-glx — OpenGL lib for some Pillow operations
#   libglib2.0-0   — GLib runtime needed by poppler on slim images
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash docuforge

# Set working directory
WORKDIR /app

# Copy dependency list first (leverages Docker layer caching)
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY scripts/ ./scripts/

# Make scripts executable
RUN chmod +x ./scripts/*.sh

# Create data directories for volume mounts
RUN mkdir -p /data/uploads /data/output && \
    chown -R docuforge:docuforge /data /app

# Switch to non-root user
USER docuforge

# Expose web UI port
EXPOSE 8080

# Health check — app must respond on /api/health
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/api/health').raise_for_status()" || exit 1

# Start the FastAPI app via uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
