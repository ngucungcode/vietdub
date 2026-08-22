# syntax=docker/dockerfile:1.7

# llama.cpp is built from an immutable source commit with CUDA kernels for the
# closed support matrix. Real cubins cover every supported generation and the
# final PTX target preserves forward JIT compatibility without accepting an
# untested architecture in the runtime preflight.
FROM nvidia/cuda:12.8.0-cudnn-devel-ubuntu24.04@sha256:81d4dd36435f4ccf0aafb111c6b09182084f0a3ff044ffa5b74bbeb7c5a5fd33 AS llama-builder

ARG LLAMA_CPP_COMMIT=9d9a6d29f6b981cc7f41983d26e56485c6af1811
ARG LLAMA_CUDA_ARCHITECTURES="70-real;75-real;80-real;86-real;89-real;90-real;120-real;120-virtual"
ARG TIGER_SOURCE_COMMIT=9f18d4a10a7137e1ce8052cfb62215179f1287b6
ARG VIENEU_SOURCE_COMMIT=4002d8d6749d516b446c012f5e6729b7661529d2

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        ninja-build \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/llama.cpp

RUN git init -q . \
    && git remote add origin https://github.com/ggml-org/llama.cpp.git \
    && git fetch --depth 1 origin "${LLAMA_CPP_COMMIT}" \
    && git checkout -q --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${LLAMA_CPP_COMMIT}"

RUN cmake -S . -B build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON \
        -DCMAKE_CUDA_ARCHITECTURES="${LLAMA_CUDA_ARCHITECTURES}" \
        -DGGML_CUDA=ON \
        -DLLAMA_CURL=OFF \
        -DLLAMA_BUILD_UI=OFF \
        -DLLAMA_USE_PREBUILT_UI=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=ON \
    && cmake --build build --target llama-server llama-cli --parallel 4 \
    && mkdir -p /opt/llama.cpp \
    && cp -a build/bin/. /opt/llama.cpp/

COPY native/tiger-layers-init.py /tmp/tiger-layers-init.py

RUN git init -q /opt/tiger \
    && git -C /opt/tiger remote add origin https://github.com/JusperLee/TIGER.git \
    && git -C /opt/tiger fetch --depth 1 origin "${TIGER_SOURCE_COMMIT}" \
    && git -C /opt/tiger checkout -q --detach FETCH_HEAD \
    && test "$(git -C /opt/tiger rev-parse HEAD)" = "${TIGER_SOURCE_COMMIT}" \
    && install -m 0444 /tmp/tiger-layers-init.py /opt/tiger/look2hear/layers/__init__.py \
    && install -d -m 0755 /opt/vieneu \
    && git init -q /opt/vieneu/source \
    && git -C /opt/vieneu/source remote add origin https://github.com/pnnbao97/VieNeu-TTS.git \
    && git -C /opt/vieneu/source fetch --depth 1 origin "${VIENEU_SOURCE_COMMIT}" \
    && git -C /opt/vieneu/source checkout -q --detach FETCH_HEAD \
    && test "$(git -C /opt/vieneu/source rev-parse HEAD)" = "${VIENEU_SOURCE_COMMIT}" \
    && chmod -R a-w /opt/tiger /opt/vieneu/source

# CUDA/cuDNN and Ubuntu are intentionally immutable. Updating this digest is a
# reviewed dependency change, not a runtime update.
FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu24.04@sha256:c40d1065da90274969f9faa7fe1a7fcd1c374d5783482eec09ee5b516746088f

LABEL org.opencontainers.image.title="Thuyet Minh Offline GPU" \
      org.opencontainers.image.description="Local NVIDIA GPU Vietnamese dubbing pipeline" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ARG APP_UID=10001
ARG APP_GID=10001

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    DUB_DATABASE_PATH=/state/jobs.sqlite3 \
    DUB_MODELS_LOCK_PATH=/app/config/models.lock.json \
    DUB_MODELS_DIR=/models \
    DUB_INCOMING_DIR=/data/incoming \
    DUB_JOBS_DIR=/data/jobs \
    DUB_OUTPUT_DIR=/data/output \
    DUB_API_ACCESS_POLICY=remote \
    DUB_DEFAULT_ASR_MODEL_ID=asr-faster-whisper-small \
    DUB_DEFAULT_TRANSLATION_MODEL_ID=mt-gemma4-e2b-q4 \
    DUB_DEFAULT_SEPARATION_MODEL_ID=separation-tiger-dnr \
    DUB_DEFAULT_TTS_MODEL_ID=tts-piper-vi-vais1000-medium \
    DUB_TTS_SUPPORT_MODEL_ID=tts-neucodec-onnx-int8 \
    DUB_TIGER_SOURCE_DIR=/opt/tiger \
    DUB_VIENEU_ENTRYPOINT=/opt/vieneu/vieneu-offline.py \
    VIENEU_CODEC_PATH=/models/tts/support/neucodec-onnx-int8 \
    PYTHONPATH=/opt/tiger:/opt/vieneu/source/src \
    DUB_LLAMA_SERVER_BINARY=/usr/local/lib/llama.cpp/llama-server \
    DUB_LLAMA_SERVER_PORT=18081 \
    DUB_LLAMA_CONTEXT_SIZE=2048 \
    DUB_LLAMA_MAX_OUTPUT_TOKENS=512 \
    DUB_LLAMA_STARTUP_TIMEOUT_SECONDS=300 \
    DUB_LLAMA_REQUEST_TIMEOUT_SECONDS=180

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        ffmpeg \
        python3.12 \
        python3-pip \
        python3-venv \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" dub \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin dub

WORKDIR /app

# PyTorch's CUDA 12.8 wheels are installed explicitly. The remaining project
# dependencies live in a source-independent lock layer. Editing application
# code or bumping only the package version therefore never reinstalls the
# multi-gigabyte GPU environment.
COPY requirements/docker-gpu.lock requirements/torch-cu128.lock ./requirements/
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python3.12 -m pip install --break-system-packages \
        --index-url https://download.pytorch.org/whl/cu128 \
        --extra-index-url https://pypi.org/simple \
        --require-hashes \
        --requirement requirements/torch-cu128.lock \
    && python3.12 -c 'import torch; flags=set(torch._C._cuda_getArchFlags().split()); assert "sm_70" in flags, f"PyTorch wheel lost Volta support: {sorted(flags)}"' \
    && python3.12 -m pip install --break-system-packages \
        --require-hashes \
        --requirement requirements/docker-gpu.lock \
    && python3.12 -m pip check

COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
COPY config ./config
COPY --from=llama-builder /opt/llama.cpp /usr/local/lib/llama.cpp
COPY --from=llama-builder /opt/tiger /opt/tiger
COPY --from=llama-builder /opt/vieneu/source /opt/vieneu/source
COPY scripts/vieneu-offline.py /opt/vieneu/vieneu-offline.py

RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python3.12 -m pip install --break-system-packages --no-deps . \
    && python3.12 -m pip check \
    && chmod 0555 /opt/vieneu/vieneu-offline.py \
    && mkdir -p /config /models /state /data/incoming /data/jobs /data/output \
    && chown -R "${APP_UID}:${APP_GID}" /app /config /models /state /data

USER ${APP_UID}:${APP_GID}

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "dub_server.api:app", "--host", "0.0.0.0", "--port", "8080"]
