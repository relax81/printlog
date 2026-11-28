FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Berlin
# tesseract: Screenshots lesen beim Kalkulieren (Kosten vorab)
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl fonts-dejavu-core tesseract-ocr tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD curl -fs http://localhost:8080/ >/dev/null || exit 1
CMD ["python", "-m", "app.main"]
