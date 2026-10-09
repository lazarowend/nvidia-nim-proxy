# syntax=docker/dockerfile:1
# ============================================================
# NVIDIA NIM -> Hermes Proxy — imagem multi-stage
#
#   frontend : node:22-alpine compila o dashboard React (Vite)
#   runtime  : python:3.12-slim + proxy.py + web/dist estático
#
# A imagem final NÃO contém: Node, node_modules, código-fonte do
# frontend (só o build), .env (segredos entram apenas em runtime,
# via env_file do compose) nem backups.
# ============================================================

# ------------------------------------------------------------
# Stage 1 — build do frontend
# ------------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /app/web

# 1) Só os manifests primeiro: a camada de node_modules fica cacheada
#    isolada (mudança em src/ não reinstala dependências).
COPY web/package.json web/package-lock.json ./

# Cache mount do npm: pacotes persistem no cache do BuildKit entre
# builds. npm ci instala deps + devDeps (vite) — não setar
# NODE_ENV=production nesta stage.
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

# 2) Restante do código-fonte do frontend. O .dockerignore garante
#    que node_modules/ e dist/ do host NÃO entram no contexto.
COPY web/ ./

RUN --mount=type=cache,target=/root/.npm \
    npm run build
# -> saída em /app/web/dist (outDir default do Vite)

# ------------------------------------------------------------
# Stage 2 — runtime
# ------------------------------------------------------------
FROM python:3.12-slim AS runtime

WORKDIR /app

# curl é usado pelo healthcheck do compose (roda DENTRO do
# container, contra 127.0.0.1:5000).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependências Python em camada própria: cacheada enquanto o
# requirements.txt não mudar. --no-cache-dir não leva cache do pip.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App: proxy Flask + build do frontend servido na MESMA origem da
# API. PROXY_STATIC_DIR default do proxy.py = <dir>/web/dist
# -> cai exatamente em /app/web/dist (não precisa de env extra).
COPY proxy.py ./
COPY --from=frontend /app/web/dist ./web/dist

# Escuta em 0.0.0.0 DENTRO do container; o bind no host é feito
# apenas pelo compose (loopback). PROXY_PORT fixo em 5000: o
# mapeamento do compose é ${PROXY_PORT:-5000}:5000.
# Sem TTY no container: o proxy exige PROXY_MODEL via .env.
ENV PROXY_HOST=0.0.0.0 \
    PROXY_PORT=5000 \
    PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["python", "proxy.py"]
