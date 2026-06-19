# MLX inference (Apple Silicon)

[MLX](https://github.com/ml-explore/mlx) is Apple's array framework for Apple
Silicon — it runs models on the GPU/Neural Engine via Metal and takes
advantage of the unified memory architecture, which is generally faster and
more memory-efficient than CPU-only inference on an M-series Mac.

## Why this isn't a docker-compose service

Docker Desktop on macOS runs containers inside a lightweight Linux VM and
**does not pass the Mac's GPU/Metal device through to containers**. MLX has
no CPU fallback worth using, so it cannot run inside `ollama` (or any other
container) on a Mac the way it would on bare metal. Instead, it runs as a
native host process, exposing an OpenAI-compatible HTTP API that the
Dockerized stack reaches via `host.docker.internal` — the same pattern this
repo already uses for the optional LM Studio backend (see the README's
"Hindsight memory" section).

`ollama` keeps working as-is if you don't touch it; this just gives Apple
Silicon hosts a faster local-inference option to point Hindsight/Hermes at
instead.

## Setup

1. Install [`mlx-lm`](https://github.com/ml-explore/mlx-lm) in a virtualenv on
   the host (not in a container):

   ```sh
   python3 -m venv ~/.mlx-venv
   source ~/.mlx-venv/bin/activate
   pip install mlx-lm
   ```

2. Start the server:

   ```sh
   source ~/.mlx-venv/bin/activate
   ./mlx/serve.sh                                    # uses MLX_MODEL / MLX_HOST_PORT from .env if set
   ./mlx/serve.sh mlx-community/Qwen2.5-7B-Instruct-4bit   # or pass a model explicitly
   ```

   The first run downloads the model from Hugging Face (the
   `mlx-community` org hosts pre-converted MLX weights) and caches it under
   `~/.cache/huggingface`. `mlx_lm.server` loads exactly one model per
   process — restart it to switch models.

3. Confirm it's serving:

   ```sh
   curl http://localhost:8080/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"messages": [{"role": "user", "content": "Say PONG and nothing else"}]}'
   ```

## Pointing the stack at it

From inside a container, the host is reachable at `host.docker.internal`
(both `hermes` and `hindsight` already have the required `extra_hosts` entry
in `docker-compose.yml`).

**Hindsight** — edit `.env`:

```bash
HINDSIGHT_LLM_BASE_URL=${MLX_BASE_URL}   # http://host.docker.internal:8080/v1
HINDSIGHT_LLM_MODEL=mlx-community/Qwen2.5-7B-Instruct-4bit
HINDSIGHT_LLM_API_KEY=mlx               # mlx_lm.server doesn't check this; any value works
HINDSIGHT_LLM_PROVIDER=openai           # keep "openai", not "ollama" — see comment in .env.example
HINDSIGHT_LLM_MAX_CONCURRENT=1          # mlx_lm.server serves one request at a time
```

Then `docker compose up -d hindsight` to apply.

Embeddings and reranking are unaffected — Hindsight runs those locally with
bundled HuggingFace models regardless of which LLM backend you pick.

**Hermes** (route the agent's own model calls through MLX too):

```sh
make mlx           # sets model.provider=custom, model.base_url=$MLX_BASE_URL
make mlx-revert     # back to direct provider routing
```

## Model sizing for an M1 MacBook

Pick a quantised model that fits comfortably under your unified memory, with
headroom for the rest of the stack (Docker + Postgres + Hindsight's local
embedding/reranker models):

| RAM | Suggested model |
|---|---|
| 8 GB | `mlx-community/Qwen2.5-3B-Instruct-4bit` |
| 16 GB | `mlx-community/Qwen2.5-7B-Instruct-4bit` (default above) |
| 32 GB+ | `mlx-community/Qwen2.5-14B-Instruct-4bit` |

Browse more converted models at
[huggingface.co/mlx-community](https://huggingface.co/mlx-community).

## Caveats

- One model per `mlx_lm.server` process — there's no equivalent to Ollama's
  `OLLAMA_MAX_LOADED_MODELS` for serving several models concurrently. Run
  separate server instances on different ports if you need that.
- No GPU sharing with other apps — if LM Studio or another MLX/Metal process
  is also running, you'll contend for the same GPU.
- The server isn't managed by Docker, so it won't restart automatically on
  reboot or show up in `docker compose ps` / Prometheus. If you want it
  always-on, wrap `mlx/serve.sh` in a `launchd` agent.
