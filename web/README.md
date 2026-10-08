# NVIDIA Proxy — Dashboard Web

Interface React (Vite) para monitorar e controlar o proxy em `http://127.0.0.1:5000`.

![dashboard](dashboard-screenshot.png)

## O que exibe

- **Status geral** — Operacional / Degradado / Offline (badge no header)
- **Resumo** — keys disponíveis (X de Y), RPM total, streams ativos, requests em espera, uptime
- **Totais** — requisições, sucessos, falhas, 429
- **Modelo** — modelo em uso, lista de disponíveis e troca em runtime (sem restart do proxy)
- **Chaves** — card por key: barra de tokens (disponível/burst), cooldown com contagem regressiva, last_error e estatísticas (req/ok/falha/429); keys com problema ficam com borda âmbar

## Requisitos atendidos

| Requisito | Implementação |
|---|---|
| Keys disponíveis | `GET /health` → resumo + cards por key, polling a cada 4s |
| Modelo utilizado | `GET /admin/model` + `/health` |
| Modelos disponíveis | `GET /admin/model` → `available` |
| Trocar modelo | `POST /admin/model` com feedback inline (sucesso/erro) |

## Como rodar

```bash
cd C:\Proxy-hermes\web
npm install   # apenas na primeira vez
npm run dev
```

Abra **http://localhost:5173** (o proxy do Flask precisa estar rodando na porta 5000).

> Nota: use `http://localhost:5173`, não `http://127.0.0.1:5173` — o dev server do Vite escuta apenas em `localhost` (IPv6 `::1`).

## Como funciona

- O dev server do Vite faz **proxy reverso** de `/health` e `/admin/*` para `127.0.0.1:5000` (`vite.config.js`) — mesma origem, zero configuração de CORS, nenhuma mudança no backend.
- Polling de `/health` e `/admin/model` a cada 4s, com pausa quando a aba está oculta e `AbortController` nos fetchs.
- Contagem regressiva de cooldown interpolada localmente (tick de 1s) entre polls.
- Estado offline tratado: banner com botão "Tentar agora", sem crash.

## Build de produção

```bash
npm run build   # gera dist/
npm run preview # serve o dist localmente
```

## Estrutura

```
web/
  index.html
  vite.config.js      # proxy /health e /admin -> 127.0.0.1:5000
  src/
    main.jsx          # bootstrap React
    App.jsx           # dashboard completo (componentes: StatusBadge, SummaryCard, KeyCard)
    styles.css        # tema escuro, CSS puro (sem framework)
  dist/               # build de produção (gerado)
  dashboard-screenshot.png
```

## QA realizado (navegador real)

- Dados ao vivo renderizando: status, keys 4/4, RPM 156, uptime, modelo, 6 opções
- Troca de modelo pela UI: z-ai/glm-5.3 → moonshotai/kimi-k3 → restaurado, com feedback de sucesso
- Contrato de erro 400: mensagem do backend exibida corretamente
- Build de produção: 29 módulos sem erros
