# NNP backend — Django + gunicorn
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl — healthcheck uchun; libpq — psycopg (Postgres) uchun
RUN apt-get update && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Avval faqat kutubxonalar — kod o'zgarganda qayta o'rnatilmasin (Docker keshi)
COPY requirements.txt requirements-docker.txt ./
RUN pip install -r requirements.txt -r requirements-docker.txt

COPY . .

# Windows'dan kelgan CRLF va yo'qolgan huquqlarni tuzatamiz
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Root'dan ishlamaymiz
RUN useradd --create-home --uid 1000 nnp \
    && mkdir -p /app/media /app/staticfiles /app/backups \
    && chown -R nnp:nnp /app
USER nnp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fs http://localhost:8000/api/health >/dev/null || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]

# 3 worker — kichik server uchun. Fayl yuklash va PDF/Excel hisobotlar uchun timeout 120 s.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
