# ─────────────────────────────────────────────
#  Stage 1 — slim Python base
# ─────────────────────────────────────────────
FROM python:3.11-slim

# Prevents .pyc files & enables stdout/stderr logs in real-time
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy rest of the source
COPY . .

# Render injects PORT automatically; default 10000
EXPOSE 10000

# Start the bot
CMD ["python", "bot.py"]
