FROM python:3.13-slim

WORKDIR /app

# Install system deps for audio processing + Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libsndfile1 \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libxshmfence1 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium --with-deps

COPY . .

# Create dirs that the app expects
RUN mkdir -p logs transcripts

EXPOSE 8080

CMD ["python", "app.py"]
