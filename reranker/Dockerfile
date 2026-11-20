# syntax=docker/dockerfile:1.7
# Internal model service. Build context: reranker/. The model is baked in at a pinned revision and
# verified file by file, so a pod needs no network and two builds of one commit run the same weights.
# MODEL_LOCK picks the model and with it the service's job: model.lock.json is the reranker (moks-reranker),
# embedder.lock.json the embedding model (moks-embedder). One image never holds both.
FROM python:3.12-slim AS build
ARG MODEL_LOCK=model.lock.json
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY requirements.txt fetch_model.py model.lock.json embedder.lock.json ./
RUN pip install --prefix=/install -r requirements.txt
RUN PYTHONPATH="$(python -c 'import site,sys;print("/install/lib/python%d.%d/site-packages" % sys.version_info[:2])')" python fetch_model.py "$MODEL_LOCK" /opt/model \
 && rm -rf /opt/model/.cache && cp "$MODEL_LOCK" /opt/model/model.lock.json

FROM python:3.12-slim
ARG GIT_COMMIT=dev
LABEL org.opencontainers.image.revision=$GIT_COMMIT
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 RERANKER_MODEL_DIR=/opt/model \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 HF_HOME=/tmp/hf TOKENIZERS_PARALLELISM=false
RUN groupadd --gid 10001 moks && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin moks
COPY --from=build /install /usr/local
COPY --from=build /opt/model /opt/model
COPY app.py engine.py schemas.py /app/
WORKDIR /app
USER 10001:10001
EXPOSE 8090 9464
ENTRYPOINT ["python", "app.py"]
