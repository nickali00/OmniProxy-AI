FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-ita ffmpeg espeak-ng \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 gateway \
    && useradd --system --uid 10001 --gid gateway --home-dir /app gateway

COPY requirements.txt requirements-media.txt requirements-images.txt ./
ARG ENABLE_LOCAL_AUDIO=false
ARG ENABLE_LOCAL_IMAGES=false
RUN python -m pip install --upgrade pip \
    && python -m pip install --upgrade 'setuptools>=78.1.1' \
    && python -m pip install -r requirements.txt \
    && if [ "$ENABLE_LOCAL_AUDIO" = "true" ]; then python -m pip install -r requirements-media.txt; fi \
    && if [ "$ENABLE_LOCAL_IMAGES" = "true" ]; then python -m pip install 'torch==2.15.0' --index-url https://download.pytorch.org/whl/cpu && python -m pip install -r requirements-images.txt; fi \
    && mkdir -p "${TIKTOKEN_CACHE_DIR}" \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && mkdir -p /data /vault \
    && chown -R gateway:gateway /app /data /vault "${TIKTOKEN_CACHE_DIR}"

COPY --chown=gateway:gateway app ./app
COPY --chown=gateway:gateway scripts ./scripts
COPY --chown=gateway:gateway conformance ./conformance
COPY --chown=gateway:gateway spec ./spec

# Dependencies are installed during the build; the read-only runtime needs no pip.
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
    /usr/local/lib/python3.12/ensurepip /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12

USER gateway

EXPOSE 8000

CMD ["python", "-m", "scripts.serve"]
