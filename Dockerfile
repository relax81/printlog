# Stufe 1: Version aus dem letzten Commit ablesen (siehe app/version.py).
# Fehlt .git im Build-Kontext, bleibt wenigstens die Build-Zeit.
FROM python:3.12-slim AS version
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN python -m app.version --write /version.json

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Berlin
# tesseract: Screenshots lesen beim Kalkulieren (Kosten vorab)
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl fonts-dejavu-core tesseract-ocr tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY --from=version /version.json ./app/version.json
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD curl -fs http://localhost:8080/ >/dev/null || exit 1
CMD ["python", "-m", "app.main"]
