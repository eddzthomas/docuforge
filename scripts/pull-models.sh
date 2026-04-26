#!/bin/bash
# ============================================================
# DocuForge — Pull required Ollama models on first boot
# ============================================================
# This script pulls the OCR and tagging models into the
# ollama container so they're ready when processing starts.
#
# Run this inside the ollama container:
#   docker exec -it docuforge-ollama /bin/bash -c "$(cat scripts/pull-models.sh)"

set -e

echo "Pulling OCR model: glm-ocr..."
ollama pull glm-ocr

echo "Pulling tagging model: llama3.2..."
ollama pull llama3.2

echo "All models pulled successfully."
echo "Run 'ollama list' to verify."
