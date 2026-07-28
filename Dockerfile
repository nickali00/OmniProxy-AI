FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache

WORKDIR /app

RUN groupadd --system --gid 10001 gateway \
    && useradd --system --uid 10001 --gid gateway --home-dir /app gateway

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && mkdir -p "${TIKTOKEN_CACHE_DIR}" \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && mkdir -p /data /vault \
    && chown -R gateway:gateway /app /data /vault "${TIKTOKEN_CACHE_DIR}"

COPY --chown=gateway:gateway app ./app

USER gateway

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
