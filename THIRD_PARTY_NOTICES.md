# Third-party notices — GPU server

This Phase 4 inventory was reviewed on 2026-08-01. Exact transitive packages
and container-layer licenses will be exported as an SBOM during Phase 5.

## Runtime and application dependencies

| Component | Pinned version/image | License / terms | Source |
|---|---|---|---|
| NVIDIA CUDA/cuDNN Ubuntu image | CUDA 12.8.0, cuDNN 9.7, digest pinned | NVIDIA CUDA Toolkit and cuDNN terms | <https://hub.docker.com/r/nvidia/cuda> |
| Python | 3.12 (Ubuntu package) | PSF-2.0 | <https://www.python.org/> |
| FastAPI | 0.139.2 | MIT | <https://github.com/fastapi/fastapi> |
| Uvicorn | 0.51.0 | BSD-3-Clause | <https://github.com/encode/uvicorn> |
| HTTPX | 0.28.1 | BSD-3-Clause | <https://github.com/encode/httpx> |
| Pydantic Settings | 2.14.2 | MIT | <https://github.com/pydantic/pydantic-settings> |
| Typer | 0.27.0 | MIT | <https://github.com/fastapi/typer> |
| PyTorch | 2.8.0, CUDA 12.8 wheel | BSD-3-Clause | <https://github.com/pytorch/pytorch> |
| CTranslate2 | 4.8.1 | MIT | <https://github.com/OpenNMT/CTranslate2> |
| faster-whisper | 1.2.1 | MIT | <https://github.com/SYSTRAN/faster-whisper> |
| llama.cpp | `b10208` (`9d9a6d29f6b981cc7f41983d26e56485c6af1811`) | MIT | <https://github.com/ggml-org/llama.cpp> |
| TIGER source | `9f18d4a10a7137e1ce8052cfb62215179f1287b6` | MIT | <https://github.com/JusperLee/TIGER> |
| VieNeu-TTS source | `3.2.3` (`4002d8d6749d516b446c012f5e6729b7661529d2`) | Apache-2.0 | <https://github.com/pnnbao97/VieNeu-TTS> |
| Hugging Face Hub | 0.36.0 | Apache-2.0 | <https://github.com/huggingface/huggingface_hub> |
| Transformers | 4.57.6 | Apache-2.0 | <https://github.com/huggingface/transformers> |
| Safetensors | 0.7.0 | Apache-2.0 | <https://github.com/huggingface/safetensors> |
| ONNX Runtime | 1.24.4 | MIT | <https://github.com/microsoft/onnxruntime> |
| NumPy | 2.3.4 | BSD-3-Clause | <https://github.com/numpy/numpy> |
| PySoundFile | 0.13.1 | BSD-3-Clause | <https://github.com/bastibe/python-soundfile> |
| Python-SoXR | 1.0.0 | LGPL-2.1-or-later | <https://github.com/dofuuz/python-soxr> |
| sea-g2p | 0.7.20 | See package distribution metadata | <https://pypi.org/project/sea-g2p/0.7.20/> |
| Piper TTS | 1.5.0 | GPL-3.0-or-later | <https://github.com/OHF-Voice/piper1-gpl> |
| FFmpeg | Ubuntu 24.04 package | LGPL-2.1-or-later/GPL depending on build configuration | <https://ffmpeg.org/legal.html> |
| Supervisor | 4.3.0 (native mode) | BSD-derived | <https://github.com/Supervisor/supervisor> |

## Embedded web dashboard

The production wheel contains the dashboard's compiled browser assets, not a
Node.js server. Exact direct and transitive JavaScript dependencies are locked
in `web/package-lock.json` and inventoried with runtime/build scope, integrity
hashes, versions, licenses, and npm package URLs in `release/sbom.cdx.json`.

| Component | Pinned version | Use | License | Source |
|---|---:|---|---|---|
| Next.js | 16.2.12 | Build/runtime browser bundle | MIT | <https://github.com/vercel/next.js> |
| React | 19.2.6 | Browser UI runtime | MIT | <https://github.com/facebook/react> |
| React DOM | 19.2.6 | Browser rendering runtime | MIT | <https://github.com/facebook/react> |
| Vinext | 0.0.50 | Static dashboard build adapter | MIT | <https://github.com/cloudflare/vinext> |
| Vite | 8.0.13 | Build-only bundler | MIT | <https://github.com/vitejs/vite> |
| Tailwind CSS | 4.2.1 | Build-only CSS tooling | MIT | <https://github.com/tailwindlabs/tailwindcss> |
| Cloudflare Vite plugin | 1.37.1 | Development/build adapter | MIT | <https://github.com/cloudflare/workers-sdk> |

`npm ci` is required for a reproducible frontend build. Production native and
Docker deployments serve the checked-in `dub_server/web_static` files through
FastAPI and do not install or execute Node.js dependencies.

The managed-container deployment reuses provider packages Python 3.11.0rc1 and
PyTorch 2.7.0+cu126 instead of replacing them with the Docker pins above. It uses
FFmpeg 4.4.2 from Ubuntu 22.04. Docker pins remain available for a separate
full Ubuntu host; every deployment must produce its own hardware acceptance evidence.

## Optional visual lip-sync backends

VietDub invokes these as separate, operator-provisioned local processes. Their code and
model artifacts are not vendored in this repository and are disabled by default.

| Component | Pinned source/model | License declared upstream | Source |
|---|---|---|---|
| MuseTalk 1.5 | source `0a89dec45a0192b824e3cf4daf96c239440c5ed8`; model `3ef28bc5cff08c90ad8178a25f1b570cd800170f` | code MIT; model CreativeML Open RAIL-M | <https://github.com/TMElyralab/MuseTalk> |
| LatentSync 1.6 | source `a229c3948406bc2cf6eaf4873e662e70c6a04746`; model `c42c7e6c8e9c213626389fa7d9a3c444b8536353` | code Apache-2.0; model OpenRAIL++ | <https://github.com/bytedance/LatentSync> |

Each backend brings additional model licenses. Operators must preserve notices, verify
checkpoint terms, and avoid upstream test media in commercial outputs. See
`docs/LIP_SYNC.md` for the complete pinned layout and acceptance limits.

## Separate Compose services

Prowlarr and qBittorrent run as separate processes and are not linked into the
application Python package. Their source and license obligations still need to
accompany a redistributed offline bundle.

| Component | Image tag | License | Source |
|---|---|---|---|
| Prowlarr | `2.5.2.5491-ls155@sha256:2f3d31307beba3ba2dd226d191f5f5c14ee3b4d8b49277c64683f5ed97083179` | GPL-3.0 | <https://github.com/Prowlarr/Prowlarr> |
| qBittorrent | `5.2.3_v2.0.13-ls469@sha256:b024436f8ca665d16d9a997d26fd27fdf867ee5566ba09f32764e7b2976d3e02` | GPL-2.0-or-later | <https://github.com/qbittorrent/qBittorrent> |

Native mode instead installs Prowlarr `2.5.2.5491` from its official Linux x64
release archive (SHA-256 pinned in `native/components.lock.json`) and qBittorrent
`4.4.1-2` from Ubuntu 22.04. Both remain separate foreground processes managed by
Supervisor and are not linked into the Python application.

No indexer or tracker configuration is bundled. OpenSubtitles is accessed
through its documented REST API and is not used for cloud inference.

## Model assets

ASR weights are installed separately by the operator, not bundled in the source
archive or downloaded by the inference worker.

| Model | Locked revision | License | Source |
|---|---|---|---|
| Systran faster-whisper-small | `536b0662742c02347bc0e980a01041f333bce120` | MIT | <https://huggingface.co/Systran/faster-whisper-small> |
| Dropbox Dash faster-whisper-large-v3-turbo | `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` | MIT | <https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo> |
| Systran faster-whisper-large-v3 | `edaa852ec7e145841d8ffdb056a99866b5f0a478` | MIT | <https://huggingface.co/Systran/faster-whisper-large-v3> |
| Google Gemma 4 E2B IT QAT Q4_0 GGUF | `675cff42a74c774d6cb76f76d8eacb49b48c9b93` | Apache-2.0 | <https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf> |
| Google Gemma 4 31B IT QAT Q4_0 GGUF | `59dde24573e7e61570dba08b18a2e1fe246955ed` | Apache-2.0 | <https://huggingface.co/google/gemma-4-31B-it-qat-q4_0-gguf> |
| TIGER-DnR cinematic separation | `b7a59560bbca10febbcd46fb01600f868e587f57` | Apache-2.0 | <https://huggingface.co/JusperLee/TIGER-DnR> |
| VieNeu-TTS v2 | `b62b1cbddec67cb1d26ac602965d39f0a7faddf2` | Apache-2.0 | <https://huggingface.co/pnnbao-ump/VieNeu-TTS-v2> |
| NeuCodec ONNX int8 decoder | `706f4bd5fcc39b039c333d5407f58b0075dcee07` | Apache-2.0 | <https://huggingface.co/neuphonic/neucodec-onnx-decoder-int8> |
| Piper `vi_VN-vais1000-medium` voice | `320d5f7f7751a17ef6512d5c23863056c6a11c0f` | CC-BY-4.0; Piper runtime GPL-3.0-or-later | <https://huggingface.co/rhasspy/piper-voices/tree/320d5f7f7751a17ef6512d5c23863056c6a11c0f/vi/vi_VN/vais1000/medium> |

`config/models.lock.json` records the exact allowlist, byte size, SHA-256 of every
runtime file and a canonical tree SHA-256. Small, Large-v3-Turbo and Gemma 4 E2B
were downloaded and verified on the acceptance host. Gemma 4 31B is the default
translation model and has an exact 17,651,001,568-byte lock; E2B remains the faster
manual choice. Translation models are opened only by the offline worker through
the pinned local `llama-server`; they are never downloaded at inference time.
TIGER-DnR, VieNeu v2, its NeuCodec decoder and the Piper voice now have exact
revision, byte-size, per-file SHA-256 and canonical tree locks. The worker opens
only verified local directories and runs with network access disabled. The TIGER
runtime applies the documented minimal initializer overlay recorded in
`native/components.lock.json`; model implementation files are unchanged.

Piper embeds eSpeak-NG phonemization and therefore reinforces this application's
GPL distribution requirement. The Vais1000 voice/dataset attribution required by
CC-BY-4.0 must accompany redistributed model assets. Preset or user-supplied voice
rights remain separate from engine and weight licenses; operators must only use
voice references they are authorized to process.
