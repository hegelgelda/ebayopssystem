FROM python:3.12-slim

ARG INSTALL_REMBG=1
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Tokyo \
    DATA_DIR=/data \
    U2NET_HOME=/data/models \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

# 社内プロキシ等で追加のルート証明書が必要なビルド環境向け（通常は空）。docker/extra-ca/*.crt を信頼する
COPY docker/extra-ca/ /usr/local/share/ca-certificates/extra/
RUN if ls /usr/local/share/ca-certificates/extra/*.crt >/dev/null 2>&1; then update-ca-certificates; fi

WORKDIR /app
COPY requirements.txt requirements-rembg.txt ./
RUN pip install -r requirements.txt \
 && if [ "$INSTALL_REMBG" = "1" ]; then pip install -r requirements-rembg.txt; fi

COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY config ./config
COPY app ./app

RUN useradd --uid 1000 --create-home app && mkdir -p /data && chown -R app:app /data
USER app
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
