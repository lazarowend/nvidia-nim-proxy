# Análise técnica — C:\Proxy-hermes (proxy.py)

Data: 2026-10-08
Método: 4 agentes especializados em paralelo (arquitetura, concorrência/rate-limit, segurança/HTTP, produção/observabilidade), somente leitura. O agente de segurança fez verificação empírica: reproduziu o comportamento do proxy ponta-a-ponta num ambiente isolado (requests 2.34.2 / urllib3 2.8.0) com clientes requests e httpx reais.

Veredito geral: o proxy funciona bem para o cenário atual (`python proxy.py` interativo, usuário único, backend do Hermes) e tem partes genuinamente bem construídas. Mas há 1 bug crítico latente, 4 problemas de severidade alta e uma série de médios que vão morder conforme o uso cresce. Nenhum achado invalida o design central (pool de keys + token bucket + failover pré-stream): o design está certo, a execução tem furos.

---

## CRÍTICO

### C1. Corpo descomprimido com header Content-Encoding repassado ao cliente
Linhas: 682-687 (denylist de resposta), 701-705 (iter_content)

`requests` decompõe transparentemente o corpo (iter_content → decode_content=True, hardcoded na biblioteca — verificado na fonte). A denylist de headers de resposta só remove content-length/transfer-encoding/connection, então o header `Content-Encoding: gzip` sobrevive e é repassado com o corpo JÁ descomprimido. Cliente que respeita o header quebra: reproduzido empiricamente — cliente `requests` lança ContentDecodingError, cliente `httpx` (base do SDK OpenAI) lança DecodingError.

Gatilho: qualquer camada upstream (NVIDIA/CDN/nginx) que passe a comprimir. O requests já anuncia `Accept-Encoding: gzip, deflate, zstd` por default. Probe live: a NVIDIA hoje não comprime os endpoints testados — por isso "latente". Quando disparar, quebra 100% das respostas, não intermitentemente.

Correção (1 linha): adicionar "content-encoding" à denylist em 682-687.
Alternativa (preserva compressão fim-a-fim): transmitir o corpo bruto com `response.raw.stream(1024, decode_content=False)`.
Cinto e suspensório: `headers["Accept-Encoding"] = "identity"` em build_headers elimina compressão na perna upstream.
Verificado que iter_content(1024) NÃO adiciona latência a streams SSE (teste empírico: primeiro chunk entrega imediato) — a correção não degrada streaming.

---

## ALTA

### A1. DEFAULT_NVIDIA_MODEL vira None sob qualquer servidor WSGI → "model": null em 100% das chamadas
Linhas: 36, 272-274, 1004-1006 (achado independente de 2 agentes)

A variável nasce None no escopo do módulo e só recebe valor dentro de `if __name__ == "__main__":` via input(). Sob gunicorn/waitress/`flask run`, o módulo é importado, o bloco nunca executa e prepare_request_body injeta `body["model"] = None` em TODAS as chamadas de chat/completions. O /health também reporta "model": null. Hoje funciona apenas porque o único modo suportado é `python proxy.py` interativo.

Correção: resolver no escopo do módulo com env + fail-fast:
```python
DEFAULT_NVIDIA_MODEL = os.getenv("PROXY_MODEL") or (select_model() if sys.stdin.isatty() else None)
if not DEFAULT_NVIDIA_MODEL:
    raise RuntimeError("Defina PROXY_MODEL no .env")
```

### A2. Seleção de modelo via input() interativo — inviável headless/container; troca exige restart e mata streams
Linhas: 946-991, 1004, 1076

Sem TTY (serviço, docker -d, nohup), input() levanta EOFError no boot. Trocar de modelo exige restart, que aborta todos os SSEs em andamento do Hermes. O bind fixo 127.0.0.1:5000 também quebra uso em container.

Correção: PROXY_MODEL via env (A1) + endpoint admin para troca em runtime sem restart:
```python
@app.route("/admin/model", methods=["GET", "POST"])
def admin_model():
    global DEFAULT_NVIDIA_MODEL
    if request.method == "GET":
        return {"model": DEFAULT_NVIDIA_MODEL, "available": sorted(set(AVAILABLE_MODELS.values()))}
    new_model = (request.get_json(silent=True) or {}).get("model", "").strip()
    if new_model not in AVAILABLE_MODELS.values():
        return {"error": {"message": f"Modelo invalido: {new_model}", "type": "invalid_request_error"}}, 400
    with model_lock:
        DEFAULT_NVIDIA_MODEL = new_model
    return {"model": DEFAULT_NVIDIA_MODEL}
```
(Como o body é preparado por requisição, requisições em voo não são afetadas.)

### A3. 43 print() em vez de logging — sem níveis, timestamps ou request-id
Linhas: 287-290, 396-402, 525-534, 555-560, 568-572, 625-630, 716-720, 738-742, 765-769, 789-793, 807-812 + startup

Com threaded=True, linhas de requests concorrentes se intercalam no stdout. Uma requisição com failover em 3 keys emite 6+ linhas soltas impossíveis de reunir. Quando o Hermes falha, não há como reconstruir o que aconteceu.

Correção: logging com LoggerAdapter carregando request-id (aproveitar X-Request-Id do cliente quando existir, devolver no response). Roteamento/tokens em DEBUG, failover em WARNING, falha total em ERROR.

### A4. Retry frágil: 1 passada, cooldown fixo 5s, teto de espera de 10s POR TENTATIVA (não por request), 429 ao cliente sem Retry-After
Linhas: 56, 412, 420-426 (wait_start reseta a cada tentativa), 500-511, 736-754

Três fraquezas combinadas:
- Cada key é tentada 1x por requisição; um 429 na única key saudável falha a request em definitivo.
- O teto de 10s reinicia a cada tentativa (wait_start na linha 426 dentro do for) — esperas de 9,9s que não estouram o teto se SOMAM entre tentativas: um único request pode acumular ~10s × N keys antes do 429/502 final.
- O proxy honra Retry-After por key internamente (582-609), mas ignora no próprio 429 ao cliente (506-511) — o Hermes faz backoff às cegas e re-testa antes da hora.

Correções: deadline global por request; fail-fast quando next_wait calculado excede o orçamento (responder 429 imediato com Retry-After em vez de girar); dormir o next_wait exato em vez de polling de 50ms; Retry-After propagado ao cliente.

---

## MÉDIO

### M1. Erros do proxy em text/plain fora do formato OpenAI
Linhas: 506-511, 814-818 (2 agentes)
SDKs OpenAI-compatíveis tentam parsear JSON; com text/plain a mensagem de diagnóstico se perde e o 502 expõe a exceção crua. Correção: error object JSON (`{"error": {"message", "type", "code"}}`) + Retry-After no 429 + sanitizar last_error (detalhe no log do servidor, mensagem curta ao cliente).

### M2. GET /v1/models repassado à NVIDIA — catálogo incoerente com o override
Linhas: 272-274 vs 934-939 (2 agentes)
O client lista dezenas de modelos, escolhe um, e o proxy silenciosamente serve outro. A chamada ainda consome 1 token do bucket. Correção: interceptar localmente (mesmo padrão do /health) e retornar só o modelo efetivo (ou AVAILABLE_MODELS).

### M3. Headers hop-by-hop repassados à NVIDIA
Linhas: 299-331, denylist de só 3 headers
Connection, Keep-Alive, TE, Trailer, Transfer-Encoding, Upgrade, Proxy-* atravessam — viola RFC 9110 §7.6.1. Correção: denylist hop-by-hop + tokens nomeados no header Connection do cliente + scrub de X-Forwarded-*/Forwarded.

### M4. Erros mid-stream truncam a conexão silenciosamente — sem contagem de falha, sem cooldown, sucesso já creditado
Linhas: 653-661, 697-730
O desenho failover-antes-do-stream está correto (decisão no header, sem replay de corpo parcial). Mas quando o stream morre no meio: o Hermes vê fim de stream sem [DONE], total_success já foi incrementado, nenhum failure contabiliza, nenhum cooldown aplica — key cronicamente intermitente parece saudável no /health. Correção: flag failed no generate() → contabilizar failure + cooldown opcional; para SSE, emitir evento de erro no formato OpenAI antes de fechar.

### M5. BURST_CAPACITY == RPM == 39 permite até 2x o limite nominal em janela de 60s
Linhas: 43-49, 102-105
Bucket inicia cheio: 39 imediatos + 39 refilados = 78 req/60s pós-ociosidade. A matemática do refill em si está CORRETA (1,5385s/token, sem drift). Se a NVIDIA aplicar janela fixa de ~40 RPM, rajada pós-ociosidade gera 429s remotos em cascata. Correção: BURST_CAPACITY = 1-5 (a taxa sustentada continua 39/min).

### M6. Sem requirements.txt, README, testes — failover crítico validado só contra quota real
Dependências (flask, requests, python-dotenv) existem só como imports. A parte mais delicada (failover concorrente) não tem teste nenhum — refatorar às cegas. Nota: a importação do módulo levanta RuntimeError sem keys no .env, então testes exigem conftest.py setando env ANTES do import. Correção: requirements.txt pinado, README (.env, base_url do Hermes, como rodar), testes de failover com mock (esboço no relatório do agente 4).

### M7. Sem shutdown gracil + servidor dev Werkzeug
Linhas: 1075-1079
SIGTERM corta todos os SSEs em andamento. Correção: waitress (Windows-friendly, drena conexões) + contador active_streams; com A2 (troca de modelo sem restart), o shutdown importa só em updates de código.

### M8. READ_TIMEOUT 600s excessivo + pior caso sem deadline total
Linhas: 58-60, 549-553, 736-754
600s ENTRE chunks: stream que para de emitir deixa o Hermes pendurado até 10 min nessa tentativa; o timeout re-entra no failover → pior caso ~len(keys) × (30s + 600s) numa única geração. Gaps reais entre chunks de LLM são de milissegundos. Correção: 60-120s configurável + deadline total por request.

### M9. Loop de espera sem fail-fast: gira até 10s quando nenhuma candidata servirá
Linhas: 432-511
Se as keys restantes têm cooldown/Retry-After maior que o orçamento, o loop sabe desde a primeira iteração e ainda assim gira até o timeout. O cálculo preciso de next_wait (447-488) é descartado pelo `min(next_wait, 0.05)` (490-498) — polling fixo que adquire o lock global 2x por tick. Correção: dormir next_wait exato; se next_wait > orçamento, falhar já com Retry-After.

### M10. Sem autenticação no próprio proxy
Linhas: 911-939, 1076
Bind 127.0.0.1 mitiga acesso remoto direto, mas: processo local hostil gasta as keys (build_headers injeta Bearer real); site malicioso pode disparar POSTs fire-and-forget em 127.0.0.1:5000 (simple request não sofre preflight), cada um consumindo 1 token; DNS rebinding permite LER respostas (o proxy descarta Host e não valida Origin). Correção: token opcional X-Proxy-Token via env (vazio = comportamento atual) + validar Host ∈ {127.0.0.1:5000, localhost:5000}.

### M11. Model override incondicional
Linhas: 270-274
O cliente nunca recebe o model que pediu — violação silenciosa de contrato; o usuário do Hermes acha que usa X e recebe Y. Correção: modo configurável (always | respect | fallback) via env.

### M12. Arquivo único de 1079 linhas físicas / 261 statements, estado global solto
5 listas paralelas de estado, lock genérico `lock` protegendo 4 seções distintas, app Flask no top-level (sem factory — impossível injetar dependências em testes). Os banners de seção atuais já são o mapa pronto para dividir: config.py (Settings dataclass via env), keypool.py (classe KeyPool), upstream.py, app.py (create_app), __main__.py.

---

## BAIXO

- B1. Corrida nos contadores: total_success/failures/429 incrementados FORA do lock em 6 pontos (574-580, 632-638, 659-667, 744-746, 771-777, 795-797) — LOAD/ADD/STORE não é atômico; subcontagem silenciosa. Correção: helper _bump(counter, index) com lock.
- B2. Retry-After sem teto (596-601): Retry-After: 3600 remove a key por 1h. Correção: min(60, ...).
- B3. 401/403 não removem key do pool (566-647): key revogada queima 1 tentativa em TODAS as requests, para sempre. Correção: cooldown de 3600s em 401/403.
- B4. Path traversal contido: dot-segments são normalizados pelo requests e o host é fixo — NÃO escapa do domínio; pior caso alcança outros paths do mesmo host. Correção: rejeitar ".."/"." no handler.
- B5. Set-Cookie duplicados colapsados (673-691): CaseInsensitiveDict junta com vírgula. Correção: response.raw.headers.getlist("Set-Cookie").
- B6. Query params repetidos descartados (547): params=request.args perde valores além do primeiro. Correção: params=list(request.args.items(multi=True)).
- B7. Checagem case-sensitive de Content-Type (325-329): cliente httpx com "content-type" minúsculo ganha header duplicado; o merge do requests faz o default prevalecer. Correção: checar lower().
- B8. OPTIONS repassado à NVIDIA: consome token + preflight falha de qualquer forma. Correção: responder 204 localmente.
- B9. /health expõe estatísticas sem controle de acesso (mitigado por M10); /health muta estado sob o lock do caminho quente (refill em 841) — aceitável localmente; no redesign, snapshot() por key.
- B10. Estatísticas voláteis (zeram no restart); total_requests conta TENTATIVAS, não requisições (216).
- B11. Formatação vertical: 4,13 linhas físicas por instrução (115 linhas só de parênteses de fechamento); normalize_path ocupa 42 linhas onde 13 bastam. ruff format/black num commit isolado derruba o arquivo para ~300-350 linhas.
- B12. Zero type hints/docstrings; AVAILABLE_MODELS hardcoded com menu "[0-5]" desincronizado do dict (966, 989); sem pyproject/entry point; /health com roteamento dividido em 2 lugares (825-828 vs 936-937).
- B13. Round-robin correto e sem starvation entre keys (o ponteiro avança mesmo em skips), mas sem FIFO entre requests em espera simultâneas.

---

## Pontos positivos (consolidado)

- Núcleo do rate limiter race-free: TODA mutação de tokens/last_refill/cooldowns/índice sob lock (o GIL não garante atomicidade de x[i] += 1, mas aqui o lock cobre).
- time.monotonic() em todo cálculo de bucket/cooldown — imune a saltos de relógio.
- Matemática do refill correta: 60/39 = 1,5385s/token, acumulação fracionária sem drift.
- Failover decide estritamente ANTES do streaming (429/5xx/timeout no header) — elimina por construção o problema de replay de corpo parcial.
- Streaming sem latência adicional (verificado empiricamente) e response.close() garantido no finally — sem vazamento de conexão.
- Fail-fast no startup sem keys; Retry-After upstream honrado por key; nenhum segredo em log ou /health (só índice #N); Authorization do cliente sempre descartada; framing de resposta recalculado corretamente.
- Teto de espera de 10s retorna 429 em vez de loop infinito; normalize_path robusto contra prefixos compostos.

---

## Plano priorizado

QUICK WINS (~1 dia, sem mudança estrutural):
1. content-encoding na denylist (C1) — 1 linha
2. PROXY_MODEL via env + atribuição no escopo do módulo + fail-fast (A1/A2) — ~1h
3. /admin/model para troca em runtime (A2) — ~1-2h
4. Erros no formato OpenAI + Retry-After no 429 (M1/A4) — ~1h
5. Interceptar /v1/models localmente (M2) — ~30min
6. logging com request-id (A3) — ~1h
7. _bump() nos 6 incrementos de contadores (B1) — 5 linhas
8. requirements.txt + README (M6) — ~1h
9. Teto no cooldown de Retry-After + cooldown longo em 401/403 (B2/B3) — ~15min
10. Dormir next_wait exato + deadline global + fail-fast no wait loop (M9/A4) — ~2h
11. /health: uptime, last_error por key, contadores globais, active_streams, status degraded — ~2h
12. READ_TIMEOUT 120s configurável (M8) — 1 linha + env

ESTRUTURAIS (fazer depois dos testes):
13. Testes de failover com mock (pré-requisito para tudo abaixo) — meio dia
14. KeyPool/KeyGate com threading.Condition por key — mata busy-wait, dá FIFO aproximado, /health vira snapshot puro
15. Divisão em módulos seguindo os banners existentes (M12) + create_app factory
16. waitress + shutdown gracil (M7)
17. BURST_CAPACITY 1-5 (M5) + segunda passada com backoff exponencial + jitter
18. ruff format via pre-commit (B11)
19. Modelos dinâmicos via GET /v1/models com fallback estático (B12)
20. Token X-Proxy-Token + validação de Host (M10); Dockerfile por último
