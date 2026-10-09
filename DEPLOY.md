# Deploy local com Docker

Sobe o proxy + dashboard em um único container, acessível apenas no seu
computador (bind 127.0.0.1).

## Requisitos

- Docker Desktop (WSL2) instalado e rodando
- `.env` preenchido na raiz (copie de `.env.example`) — **obrigatório**:
  - Ao menos uma `NVIDIA_API_KEY_N`
  - `PROXY_MODEL` (no container não há menu interativo)

## Subir

```bash
cd C:\Proxy-hermes
docker compose up -d --build
```

Primeiro build: ~2-4 min (baixa node:22-alpine e python:3.12-slim, compila o
frontend dentro da imagem). Builds seguintes usam cache e levam segundos.

Acessos (na porta definida por `PROXY_PORT`, default 5000):

| O quê | URL |
|---|---|
| Dashboard | http://127.0.0.1:5000/ |
| API OpenAI-compatível | http://127.0.0.1:5000/v1/chat/completions |
| Health | http://127.0.0.1:5000/health |
| Modelo em runtime | http://127.0.0.1:5000/admin/model |

## Arquitetura do container

- **Multi-stage**: stage 1 compila o React (node:22-alpine); stage 2 é a imagem
  final (python:3.12-slim + proxy.py + web/dist estático). A imagem final NÃO
  contém Node, node_modules, .env nem código do frontend — só o build.
- **Uma origem só**: o Flask serve o dashboard de `web/dist` na MESMA porta da
  API — zero CORS, zero Vite em produção.
- **Segredos fora da imagem**: `.dockerignore` exclui `.env`; as keys entram
  por `env_file` no compose, apenas em runtime.
- **Rede**: `ports: "127.0.0.1:..."` — o serviço é visível só no loopback do
  host; nada exposto na rede local. Ouvir 0.0.0.0 é interno ao container.
- **Servidor**: waitress (16 threads, WSGI de produção, suporta streaming) —
  substitui o dev server do Flask no container.
- **Healthcheck**: `curl -fs http://127.0.0.1:5000/health` a cada 30s.
- **Logs**: limitados a 3 rotações de 10 MB (json-file).

## Operação

```bash
docker compose ps          # status + health
docker compose logs -f proxy  # logs em tempo real (com request-id)
docker compose restart     # restart rápido (sem rebuild)
docker compose down        # parar e remover o container
docker compose up -d --build  # rebuild após mudanças no código
```

Troca de modelo em runtime (sem restart): pelo dashboard, ou

```bash
curl -X POST http://127.0.0.1:5000/admin/model \
  -H "Content-Type: application/json" \
  -d '{"model":"z-ai/glm-5.3-flash"}'
```

## Convenção do host vs container

- **Uso diário no host** (o que você já faz): `python proxy.py` — menu
  interativo de modelo, sem Docker, porta 5000 direto.
- **Deploy em container**: `docker compose up -d --build` — `PROXY_MODEL` via
  .env, dashboard e API juntos, restart: unless-stopped.

Migração sem interromper a sessão atual do Hermes: suba primeiro na porta
5001 (`PROXY_PORT=5001 docker compose up -d --build`), valide
(http://127.0.0.1:5001/health), troque a config do Hermes, e só então faça
`docker compose down` do antigo e suba na 5000.

## Apontando o Hermes para o proxy

No config do Hermes (base_url do provider OpenAI-compatível):

```
base_url = http://127.0.0.1:5000/v1
api_key  = <qualquer valor — o proxy substitui pelo pool>
```
