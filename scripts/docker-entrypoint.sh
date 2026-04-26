#!/bin/bash
# ============================================================
# DocuForge — Docker Entrypoint
# ============================================================
# Runs at container start. Ensures data directories exist,
# optionally pulls Ollama models on first boot, then starts
# the FastAPI application.
#
# Set these env vars in docker-compose or .env:
#   OLLAMA_AUTO_PULL=true          — Enable auto-pull of models
#   OLLAMA_AUTO_PULL_MODELS=...    — Comma-separated model names
#   OLLAMA_HOST=http://ollama:11434

set -e

echo "========================================"
echo "  DocuForge — Starting up"
echo "========================================"

# Ensure data directories exist (mounted volume might be empty)
mkdir -p /data/uploads /data/output

echo "Data directories: /data/uploads, /data/output"

# Auto-pull Ollama models on first boot
if [ "${OLLAMA_AUTO_PULL}" = "true" ] && [ -n "${OLLAMA_AUTO_PULL_MODELS}" ]; then
    echo ""
    echo "Auto-pulling Ollama models..."
    echo "  Host: ${OLLAMA_HOST}"
    echo "  Models: ${OLLAMA_AUTO_PULL_MODELS}"

    # Extract host from URL (strip http:// prefix and port)
    OLLAMA_HOST_CLEAN=$(echo "${OLLAMA_HOST}" | sed -e 's|^https\?://||' -e 's|:.*||')

    # Wait for Ollama to be ready (health check covers this, but belt-and-suspenders)
    echo "  Waiting for Ollama to be ready..."
    for i in $(seq 1 30); do
        if curl -s "http://${OLLAMA_HOST_CLEAN}:11434/api/tags" > /dev/null 2>&1; then
            echo "  Ollama is ready."
            break
        fi
        echo "  ...waiting (${i}/30)"
        sleep 2
    done

    # Pull each model in the background (don't block app startup)
    IFS=',' read -ra MODELS <<< "${OLLAMA_AUTO_PULL_MODELS}"
    for model in "${MODELS[@]}"; do
        model=$(echo "$model" | xargs)  # Trim whitespace
        if [ -n "$model" ]; then
            echo "  Starting background pull for: $model"
            curl -s -X POST "http://${OLLAMA_HOST_CLEAN}:11434/api/pull" \
                -d "{\"name\": \"$model\"}" > /dev/null 2>&1 &
        fi
    done

    echo "  Model pull(s) started in background. Check 'docker logs docuforge-ollama' for progress."
fi

echo ""
echo "Starting DocuForge application..."
echo "========================================"

# Start uvicorn (replaces the old CMD)
exec uvicorn app.main:app --host 0.0.0.0 --port 8080
