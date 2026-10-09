"""
NVIDIA NIM -> Hermes Proxy

Proxy local OpenAI-compatível para a API NVIDIA NIM.

- Pool de API keys com token bucket por key e round-robin
- Failover em 429/5xx/401/403/timeout com cooldown (honra Retry-After, com teto)
- Streaming transparente (SSE)
- Log com request-id (X-Request-Id)
- GET /v1/models servido localmente (coerente com o model override)
- /health com estatísticas por key, uptime e status degraded
- POST /admin/model troca o modelo em runtime (sem restart)
- POST /v1/completions (legacy OpenAI) com TRADUÇÃO de protocolo para o
  chat/completions do NIM (o upstream não tem /v1/completions nativo)

Mudanças desta versão: ver ANALISE.md (C1, A2-A4, M1-M2, M4, M8-M9, B1-B8).
v2.2.0: rota POST /v1/completions com tradução de protocolo; núcleo de
failover extraído de proxy() para forward_to_nvidia() reutilizável.
"""

import json
import logging
import os
import sys
import threading
import time
import uuid

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory

# ============================================================
# CONFIGURAÇÃO
# ============================================================

load_dotenv()

app = Flask(__name__)

PROXY_VERSION = "2.2.0"

TARGET_URL = os.getenv("PROXY_TARGET_URL", "https://integrate.api.nvidia.com")

AVAILABLE_MODELS = {
    "0": "moonshotai/kimi-k3",
    "1": "z-ai/glm-5.3",
    "2": "nvidia/nemotron-3-super-120b-a12b",
    "3": "nvidia/nemotron-3.5-lightning-30b-a3b",
    "4": "deepseek-ai/deepseek-v4.1-flash",
    "5": "z-ai/glm-5.3-flash",
}

# Modelo em uso — prioridade: PROXY_MODEL (env) > menu interativo (apenas no
# __main__ com TTY) > POST /admin/model em runtime. Em import/WSGI nunca
# bloqueia: sem env fica None e o override de model é ignorado (nunca null).
DEFAULT_NVIDIA_MODEL = (os.getenv("PROXY_MODEL") or "").strip() or None

# Build do dashboard (web/dist) servido na mesma origem da API — sem CORS.
STATIC_DIR = os.getenv(
    "PROXY_STATIC_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "dist"),
)

# ============================================================
# RATE LIMIT
# ============================================================

TARGET_RPM_PER_KEY = int(os.getenv("PROXY_RPM", "39"))
BURST_CAPACITY = int(os.getenv("PROXY_BURST", str(TARGET_RPM_PER_KEY)))
TOKEN_REFILL_INTERVAL = 60.0 / TARGET_RPM_PER_KEY

# ============================================================
# FAILOVER
# ============================================================

KEY_COOLDOWN = 5             # cooldown base (s)
MAX_COOLDOWN = 60            # teto para cooldown vindo de Retry-After (s)
AUTH_FAIL_COOLDOWN = 3600    # 401/403: key provavelmente inválida/revogada (s)
CONNECT_TIMEOUT = 30
READ_TIMEOUT = int(os.getenv("PROXY_READ_TIMEOUT", "120"))  # entre chunks (era 600)
WAIT_BUDGET = float(os.getenv("PROXY_WAIT_BUDGET", "10"))   # espera GLOBAL por request

# ============================================================
# API KEYS
# ============================================================

API_KEYS = [
    key.strip()
    for key in (os.getenv(f"NVIDIA_API_KEY_{i}") for i in range(1, 11))
    if key and key.strip()
]

if not API_KEYS:
    raise RuntimeError(
        "Nenhuma NVIDIA API Key encontrada.\n"
        "Configure NVIDIA_API_KEY_1, NVIDIA_API_KEY_2, etc. no .env"
    )

# ============================================================
# LOGGING (com request-id)
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(request_id)s] %(message)s",
)
logger = logging.getLogger("proxy")


class _RequestIdFilter(logging.Filter):
    """Records sem request_id (ex.: werkzeug) recebem '-' em vez de quebrar o formato."""

    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RequestIdFilter())

# ============================================================
# ESTADO DAS KEYS
# ============================================================

lock = threading.Lock()
model_lock = threading.Lock()

current_key_index = 0
tokens = [float(BURST_CAPACITY) for _ in API_KEYS]
last_refill_times = [time.monotonic() for _ in API_KEYS]
key_cooldowns = [0.0 for _ in API_KEYS]

# ============================================================
# ESTATÍSTICAS
# ============================================================

total_requests = [0 for _ in API_KEYS]
total_success = [0 for _ in API_KEYS]
total_failures = [0 for _ in API_KEYS]
total_429 = [0 for _ in API_KEYS]
last_errors = [None for _ in API_KEYS]

started_at = time.time()
requests_waiting = 0
active_streams = 0


# ============================================================
# HELPERS DE ESTADO (thread-safe)
# ============================================================

def _add_waiting(delta):
    global requests_waiting
    with lock:
        requests_waiting += delta


def _add_streams(delta):
    global active_streams
    with lock:
        active_streams += delta


def _record_success(index):
    with lock:
        total_success[index] += 1


def _record_failure(index, reason):
    with lock:
        total_failures[index] += 1
        last_errors[index] = reason


def _record_429(index):
    with lock:
        total_429[index] += 1
        total_failures[index] += 1
        last_errors[index] = "HTTP 429 (rate limit upstream)"


# ============================================================
# TOKEN BUCKET
# ============================================================

def refill_tokens(index):
    now = time.monotonic()
    elapsed = now - last_refill_times[index]
    if elapsed <= 0:
        return
    tokens[index] = min(
        BURST_CAPACITY,
        tokens[index] + elapsed / TOKEN_REFILL_INTERVAL,
    )
    last_refill_times[index] = now


def get_available_key(excluded=None):
    global current_key_index

    if excluded is None:
        excluded = set()

    with lock:
        for _ in range(len(API_KEYS)):
            index = current_key_index
            current_key_index = (current_key_index + 1) % len(API_KEYS)

            if index in excluded:
                continue

            refill_tokens(index)

            if key_cooldowns[index] > time.monotonic():
                continue
            if tokens[index] < 1:
                continue

            tokens[index] -= 1
            total_requests[index] += 1
            return index

    return None


def set_key_cooldown(index, seconds=KEY_COOLDOWN):
    with lock:
        key_cooldowns[index] = time.monotonic() + seconds


# ============================================================
# ERROS NO FORMATO OPENAI
# ============================================================

def openai_error(status, message, err_type, retry_after=None):
    body = {"error": {"message": message, "type": err_type, "code": str(status)}}
    resp = Response(
        json.dumps(body, ensure_ascii=False),
        status=status,
        mimetype="application/json",
    )
    if retry_after is not None and retry_after > 0:
        resp.headers["Retry-After"] = str(int(retry_after) + 1)
    return resp


# ============================================================
# PREPARAÇÃO DO BODY
# ============================================================

def prepare_request_body(clean_path):
    original_data = request.get_data()

    if clean_path != "v1/chat/completions":
        return original_data
    if not original_data:
        return original_data

    try:
        body = json.loads(original_data)
        if isinstance(body, dict):
            with model_lock:
                model = DEFAULT_NVIDIA_MODEL
            if model:
                # Nunca injeta null: sem modelo definido, repassa o body original.
                body["model"] = model
            return json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
    except Exception as e:
        logger.warning("não foi possível modificar JSON: %s", e)

    return original_data


# ============================================================
# HEADERS DE REQUEST
# ============================================================
# Hop-by-hop (RFC 9110 §7.6.1) + meta de proxy/forwarding + framing.
# Tokens nomeados no header Connection do cliente também são removidos
# em build_headers().

REQUEST_DROP_HEADERS = {
    "host", "content-length", "authorization", "expect",
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "forwarded", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto", "x-forwarded-port", "x-real-ip",
}


def build_headers(api_key):
    conn_tokens = {
        t.strip().lower()
        for t in request.headers.get("Connection", "").split(",")
        if t.strip()
    }
    headers = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in REQUEST_DROP_HEADERS or lower in conn_tokens:
            continue
        headers[name] = value

    headers["Authorization"] = f"Bearer {api_key}"

    if not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/json"

    return headers


# ============================================================
# HEADERS DE RESPOSTA A REMOVER
# ============================================================
# content-encoding: o requests/urllib3 decompõe o corpo transparentemente
# (iter_content -> decode_content=True), então o header deve ser descartado
# junto com o framing recalculado pelo Flask/Werkzeug.

RESPONSE_DROP_HEADERS = {
    "content-length", "transfer-encoding", "connection", "content-encoding",
}


# ============================================================
# NORMALIZAÇÃO DE PATH
# ============================================================

def normalize_path(path):
    clean_path = path.strip("/")
    if not clean_path:
        return "v1/chat/completions"

    prefixes = ("v1/api/v1/", "api/v1/", "v1/v1/", "v1/", "api/")
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if clean_path.startswith(prefix):
                clean_path = clean_path[len(prefix):]
                changed = True
                break

    return f"v1/{clean_path}"


# ============================================================
# FORWARD AO NVIDIA NIM (núcleo de tentativas — reutilizável)
# ============================================================
# Extraído de proxy(): espera por key disponível dentro do orçamento
# global de espera (WAIT_BUDGET), loop de attempts sobre o pool,
# requests.request(stream=True) e tratamento de 429 (honra Retry-After
# com teto), 401/403 (cooldown longo), 5xx e timeout. A rota legacy
# /v1/completions chama exatamente este núcleo: mesmo pool de keys,
# mesmo token bucket, mesmo failover.
# Depende do request context do Flask (method, args, headers).
# Devolve (Response, last_error): Response=None quando todas as keys
# falharam; nesse caso last_error explica a última falha.

def forward_to_nvidia(clean_path, req_data, request_id, log):
    url = f"{TARGET_URL}/{clean_path}"

    attempted_keys = set()
    max_attempts = len(API_KEYS)
    last_error = None

    # Orçamento de espera GLOBAL por request (era 10s POR TENTATIVA).
    wait_deadline = time.monotonic() + WAIT_BUDGET

    with model_lock:
        current_model = DEFAULT_NVIDIA_MODEL

    for attempt in range(max_attempts):
        key_index = None

        # ----------------------------------------------------
        # Espera por key disponível (sleep exato + fail-fast)
        # ----------------------------------------------------

        _add_waiting(1)
        try:
            while key_index is None:
                key_index = get_available_key(excluded=attempted_keys)
                if key_index is not None:
                    break

                # Menor tempo até a próxima candidata servir.
                # next_wait=None significa: nenhuma candidata avaliada.
                # (Não usar TOKEN_REFILL_INTERVAL como sentinela: ele vira
                # piso do min() e mascara esperas maiores que 1 intervalo.)
                next_wait = None
                with lock:
                    now = time.monotonic()
                    for i in range(len(API_KEYS)):
                        if i in attempted_keys:
                            continue
                        refill_tokens(i)
                        if key_cooldowns[i] > now:
                            candidate_wait = key_cooldowns[i] - now
                        elif tokens[i] < 1:
                            candidate_wait = TOKEN_REFILL_INTERVAL * (1 - tokens[i])
                        else:
                            # Candidata pronta (perdeu corrida na aquisição):
                            # retry imediato.
                            candidate_wait = 0.0
                        if next_wait is None or candidate_wait < next_wait:
                            next_wait = candidate_wait

                remaining = wait_deadline - time.monotonic()

                if next_wait is None:
                    # Todas as keys já foram tentadas nesta request.
                    log.warning("espera: nenhuma candidata restante")
                    exhausted = openai_error(
                        429,
                        "Todas as API keys estão temporariamente indisponíveis.",
                        "rate_limit_error",
                    )
                    return exhausted, None

                if remaining <= 0 or next_wait >= remaining:
                    # Nenhuma key servirá dentro do orçamento: falhe já,
                    # informando ao cliente quanto esperar (Retry-After).
                    log.warning(
                        "espera esgotada: nenhuma key disponível a tempo "
                        "(next_wait=%.2fs)", next_wait,
                    )
                    exhausted = openai_error(
                        429,
                        "Todas as API keys estão temporariamente indisponíveis.",
                        "rate_limit_error",
                        retry_after=next_wait,
                    )
                    return exhausted, None

                # Dorme o tempo exato até a próxima candidata (não polling fixo).
                time.sleep(max(0.001, min(next_wait, remaining)))
        finally:
            _add_waiting(-1)

        attempted_keys.add(key_index)
        api_key = API_KEYS[key_index]
        headers = build_headers(api_key)

        with lock:
            tokens_left = tokens[key_index]
        log.info(
            "upstream key=#%s attempt=%s/%s model=%s tokens_restantes=%.2f",
            key_index + 1, attempt + 1, max_attempts,
            current_model, tokens_left,
        )

        # ====================================================
        # REQUEST
        # ====================================================

        try:
            response = requests.request(
                method=request.method,
                url=url,
                headers=headers,
                data=req_data,
                params=list(request.args.items(multi=True)),
                stream=True,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )

            log.info("resposta key=#%s http=%s", key_index + 1, response.status_code)

            # ------------------------------------------------
            # 429 — cooldown honrando Retry-After (com teto)
            # ------------------------------------------------

            if response.status_code == 429:
                log.warning("failover key=#%s recebeu 429", key_index + 1)
                _record_429(key_index)

                retry_after = response.headers.get("Retry-After")
                cooldown = KEY_COOLDOWN
                if retry_after:
                    try:
                        cooldown = min(
                            MAX_COOLDOWN,
                            max(KEY_COOLDOWN, float(retry_after)),
                        )
                    except ValueError:
                        pass

                set_key_cooldown(key_index, cooldown)
                response.close()
                last_error = "HTTP 429"
                continue

            # ------------------------------------------------
            # 401/403 — key inválida/revogada: cooldown longo
            # ------------------------------------------------

            if response.status_code in (401, 403):
                log.warning(
                    "failover key=#%s recebeu %s (key inválida?)",
                    key_index + 1, response.status_code,
                )
                _record_failure(
                    key_index, f"HTTP {response.status_code} (key inválida?)"
                )
                set_key_cooldown(key_index, AUTH_FAIL_COOLDOWN)
                response.close()
                last_error = f"HTTP {response.status_code} (key inválida?)"
                continue

            # ------------------------------------------------
            # 5XX
            # ------------------------------------------------

            if response.status_code >= 500:
                log.warning(
                    "failover key=#%s recebeu %s",
                    key_index + 1, response.status_code,
                )
                _record_failure(key_index, f"HTTP {response.status_code}")
                set_key_cooldown(key_index)
                response.close()
                last_error = f"HTTP {response.status_code}"
                continue

            # ------------------------------------------------
            # ESTATÍSTICAS
            # ------------------------------------------------

            if 200 <= response.status_code < 300:
                _record_success(key_index)
            else:
                _record_failure(key_index, f"HTTP {response.status_code}")

            # ------------------------------------------------
            # RESPONSE HEADERS
            # ------------------------------------------------
            # Usa response.raw.headers (HTTPHeaderDict) para preservar
            # headers repetidos (ex.: múltiplos Set-Cookie) sem colapsar
            # em um único valor separado por vírgula.

            response_headers = {}
            for name, value in response.raw.headers.items():
                if name.lower() in RESPONSE_DROP_HEADERS:
                    continue
                response_headers[name] = value
            response_headers["X-Request-Id"] = request_id

            # ------------------------------------------------
            # STREAMING
            # ------------------------------------------------

            def generate():
                failed = False
                reason = "stream: erro desconhecido"
                _add_streams(1)
                try:
                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:
                            yield chunk
                except requests.exceptions.RequestException as e:
                    failed = True
                    reason = f"stream: {e}"
                    log.warning("stream error key=#%s: %s", key_index + 1, e)
                    if (response.headers.get("Content-Type") or "").startswith(
                        "text/event-stream"
                    ):
                        err = {
                            "error": {
                                "message": "upstream connection lost",
                                "type": "server_error",
                                "code": "upstream_disconnected",
                            }
                        }
                        yield f"event: error\ndata: {json.dumps(err)}\n\n".encode()
                finally:
                    response.close()
                    _add_streams(-1)
                    if failed:
                        # Falha mid-stream não é mais silenciosa: contabiliza
                        # e pune a key intermitente.
                        _record_failure(key_index, reason)
                        set_key_cooldown(key_index)

            return (
                Response(
                    generate(),
                    status=response.status_code,
                    headers=response_headers,
                ),
                None,
            )

        # ====================================================
        # TIMEOUT
        # ====================================================

        except requests.exceptions.Timeout as e:
            log.warning("timeout key=#%s: %s", key_index + 1, e)
            _record_failure(key_index, "Timeout")
            set_key_cooldown(key_index)
            last_error = "Timeout"
            continue

        # ====================================================
        # REQUEST ERROR
        # ====================================================

        except requests.exceptions.RequestException as e:
            log.warning("request error key=#%s: %s", key_index + 1, e)
            _record_failure(key_index, str(e))
            set_key_cooldown(key_index)
            last_error = str(e)
            continue

        # ====================================================
        # ERRO GENÉRICO
        # ====================================================

        except Exception as e:
            log.error("erro key=#%s: %s", key_index + 1, e)
            _record_failure(key_index, str(e))
            last_error = str(e)
            continue

    # ========================================================
    # TODAS AS KEYS FALHARAM
    # ========================================================

    log.error("todas as %s keys falharam. último erro: %s", len(API_KEYS), last_error)
    return None, last_error


# ============================================================
# PROXY (roteador geral — usa o núcleo de tentativas)
# ============================================================

def proxy(path):
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
    log = logging.LoggerAdapter(logger, {"request_id": request_id})

    # Dot-segments (path traversal): rejeita antes de encaminhar.
    if any(seg in ("..", ".") for seg in path.split("/")):
        return openai_error(400, "Path inválido.", "invalid_request_error")

    clean_path = normalize_path(path)

    log.info("router original=/%s -> nvidia=/%s", path, clean_path)

    # --------------------------------------------------------
    # Endpoints servidos localmente (não consomem key nem token)
    # --------------------------------------------------------

    if request.method == "OPTIONS":
        resp = Response(status=204)
        resp.headers["Allow"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        resp.headers["X-Request-Id"] = request_id
        return resp

    if clean_path == "v1/models" and request.method == "GET":
        with model_lock:
            current = DEFAULT_NVIDIA_MODEL
        if current:
            data = [{"id": current, "object": "model", "owned_by": "proxy"}]
        else:
            data = [
                {"id": m, "object": "model", "owned_by": "proxy"}
                for m in sorted(set(AVAILABLE_MODELS.values()))
            ]
        return {"object": "list", "data": data}

    req_data = prepare_request_body(clean_path)

    resp, last_error = forward_to_nvidia(clean_path, req_data, request_id, log)
    if resp is None:
        # Todas as keys falharam: mesmo 502 de antes (formato OpenAI).
        return openai_error(502, f"NVIDIA proxy error: {last_error}", "api_error")
    return resp


# ============================================================
# LEGACY — POST /v1/completions (tradução de protocolo)
# ============================================================
# O NVIDIA NIM não expõe /v1/completions (404 upstream). Esta rota
# recebe o formato legacy de completions, traduz para chat/completions,
# envia pelo MESMO núcleo de tentativas (pool + failover + token
# bucket) e traduz a resposta de volta (choices[0].message.content
# -> choices[0].text), formato esperado por clientes legacy.
#
# Decisão de design — stream: REJEITADO com 400. Converter SSE de
# chat para SSE de completions exigiria parser de eventos, buffer de
# deltas e remontagem de text on-the-fly; clientes legacy que usam
# /v1/completions raramente streamam, e a simplicidade correta vale
# mais que cobertura completa. Documentado e com erro claro.

# Campos de completions aceitos e repassados ao chat/completions.
COMPLETIONS_PASSTHROUGH_FIELDS = (
    "max_tokens", "temperature", "top_p", "stop",
    "seed", "presence_penalty", "frequency_penalty",
)


@app.route("/v1/completions", methods=["POST"])
def legacy_completions():
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
    log = logging.LoggerAdapter(logger, {"request_id": request_id})

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return openai_error(
            400, "Body JSON inválido para /v1/completions.",
            "invalid_request_error",
        )

    # -------- Validações do formato legacy --------
    if "stream" in body and body["stream"]:
        return openai_error(
            400,
            "stream=true não é suportado em /v1/completions. "
            "Use /v1/chat/completions para streaming.",
            "invalid_request_error",
        )

    prompt = body.get("prompt")
    if prompt is None or (isinstance(prompt, str) and not prompt.strip()):
        return openai_error(
            400,
            "Campo 'prompt' obrigatório (string não vazia) para "
            "/v1/completions.",
            "invalid_request_error",
        )
    if not isinstance(prompt, str):
        return openai_error(
            400,
            "Campo 'prompt' deve ser uma string. Lista de prompts não é "
            "suportada; faça uma requisição por prompt.",
            "invalid_request_error",
        )

    # -------- Modelo global (nunca null; ignora o do body) --------
    with model_lock:
        current_model = DEFAULT_NVIDIA_MODEL
    if not current_model:
        return openai_error(
            503,
            "Nenhum modelo definido. Configure PROXY_MODEL no .env ou "
            "troque via POST /admin/model.",
            "api_error",
        )

    # -------- Tradução completions -> chat --------
    chat_body = {"messages": [{"role": "user", "content": prompt}]}
    for field in COMPLETIONS_PASSTHROUGH_FIELDS:
        if field in body:
            value = body[field]
            if field == "stop" and isinstance(value, str):
                value = [value]
            chat_body[field] = value
    chat_body["model"] = current_model

    req_data = json.dumps(
        chat_body, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")

    log.info(
        "legacy completions -> chat/completions model=%s", current_model,
    )

    resp, last_error = forward_to_nvidia(
        "v1/chat/completions", req_data, request_id, log,
    )
    if resp is None:
        return openai_error(502, f"NVIDIA proxy error: {last_error}", "api_error")

    # -------- Tradução da resposta: chat -> completions --------
    # Respostas de erro (4xx/5xx do upstream já não-caem no failover)
    # são repassadas como vieram; só o corpo 200 é traduzido.
    if resp.status_code != 200:
        return resp

    try:
        chat_json = json.loads(resp.get_data())
    except Exception:
        return resp  # corpo não-JSON: repassa intacto

    choices = chat_json.get("choices") or []
    if not choices:
        return openai_error(
            502,
            "Resposta do upstream sem choices ao traduzir completions.",
            "api_error",
        )

    finish = (choices[0].get("finish_reason") or "stop")
    completions_json = {
        "id": chat_json.get("id") or f"cmpl-{request_id}",
        "object": "text_completion",
        "created": chat_json.get("created") or int(time.time()),
        "model": chat_json.get("model") or current_model,
        "choices": [{
            "text": choices[0].get("message", {}).get("content", ""),
            "index": 0,
            "finish_reason": "length" if finish == "length" else "stop",
        }],
    }
    if "usage" in chat_json:
        completions_json["usage"] = chat_json["usage"]

    out = jsonify(completions_json)
    out.headers["X-Request-Id"] = request_id
    return out


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/health",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
def health():
    now = time.monotonic()

    with lock:
        keys_status = []
        healthy_keys = 0
        g_requests = g_success = g_failures = g_429 = 0

        for i in range(len(API_KEYS)):
            refill_tokens(i)
            cooldown = max(0.0, key_cooldowns[i] - now)

            if cooldown <= 0 and tokens[i] >= 1:
                healthy_keys += 1

            g_requests += total_requests[i]
            g_success += total_success[i]
            g_failures += total_failures[i]
            g_429 += total_429[i]

            keys_status.append(
                {
                    "key": f"#{i + 1}",
                    "tokens_available": round(tokens[i], 2),
                    "burst_capacity": BURST_CAPACITY,
                    "rpm_limit": TARGET_RPM_PER_KEY,
                    "cooldown_seconds": round(cooldown, 2),
                    "total_requests": total_requests[i],
                    "total_success": total_success[i],
                    "total_failures": total_failures[i],
                    "total_429": total_429[i],
                    "last_error": last_errors[i],
                }
            )

        waiting = requests_waiting
        streams = active_streams

    return {
        "status": "ok" if healthy_keys > 0 else "degraded",
        "version": PROXY_VERSION,
        "model": DEFAULT_NVIDIA_MODEL,
        "keys": len(API_KEYS),
        "keys_available": healthy_keys,
        "rpm_per_key": TARGET_RPM_PER_KEY,
        "total_target_rpm": TARGET_RPM_PER_KEY * len(API_KEYS),
        "burst_per_key": BURST_CAPACITY,
        "total_burst_capacity": BURST_CAPACITY * len(API_KEYS),
        "uptime_seconds": round(time.time() - started_at, 1),
        "requests_waiting": waiting,
        "active_streams": streams,
        "totals": {
            "requests": g_requests,
            "success": g_success,
            "failures": g_failures,
            "http_429": g_429,
        },
        "streaming": True,
        "failover": True,
        "token_bucket": True,
        "keys_status": keys_status,
    }


# ============================================================
# ADMIN — troca de modelo em runtime (sem restart)
# ============================================================

@app.route("/admin/model", methods=["GET", "POST"])
def admin_model():
    global DEFAULT_NVIDIA_MODEL

    with model_lock:
        current = DEFAULT_NVIDIA_MODEL

    if request.method == "GET": 
        return {
            "model": current,
            "available": sorted(set(AVAILABLE_MODELS.values())),
        }

    body = request.get_json(silent=True) or {}
    new_model = (body.get("model") or "").strip()

    valid = set(AVAILABLE_MODELS.values())
    if new_model not in valid:
        return openai_error(
            400,
            f"Modelo inválido: {new_model or '(vazio)'}. "
            f"Disponíveis: {', '.join(sorted(valid))}",
            "invalid_request_error",
        )

    with model_lock:
        DEFAULT_NVIDIA_MODEL = new_model

    logger.info("modelo trocado em runtime: %s", new_model)
    return {"model": new_model}


# ============================================================
# ROTAS
# ============================================================

@app.route(
    "/",
    defaults={"path": ""},
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
@app.route(
    "/<path:path>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
def catch_all(path):
    # Dashboard estático (se o build existir) — mesma origem que a API.
    # send_from_directory + normpath bloqueiam path traversal em arquivos.
    if request.method == "GET" and os.path.isdir(STATIC_DIR):
        rel = "index.html" if path == "" else path
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if full.startswith(os.path.normpath(STATIC_DIR)) and os.path.isfile(full):
            return send_from_directory(STATIC_DIR, rel)
    return proxy(path)


# ============================================================
# SELEÇÃO DO MODELO
# ============================================================

def select_model():
    last = max(AVAILABLE_MODELS)

    print()
    print("=" * 65)
    print(" SELECIONE O MODELO NVIDIA NIM")
    print("=" * 65)
    print()

    for number, model in AVAILABLE_MODELS.items():
        print(f"[{number}] {model}")

    print()
    print("=" * 65)

    while True:
        choice = input(f"Digite o número do modelo [0-{last}]: ").strip()

        if choice in AVAILABLE_MODELS:
            selected_model = AVAILABLE_MODELS[choice]

            print()
            print("Modelo selecionado:")
            print(f"  {selected_model}")
            print()

            return selected_model

        print()
        print(f"Opção inválida. Digite um número entre 0 e {last}.")
        print()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    if not DEFAULT_NVIDIA_MODEL:
        if sys.stdin and sys.stdin.isatty():
            DEFAULT_NVIDIA_MODEL = select_model()
        else:
            raise RuntimeError(
                "PROXY_MODEL não definido e sem terminal interativo.\n"
                "Configure PROXY_MODEL no .env (ex.: PROXY_MODEL=z-ai/glm-5.3)."
            )

    total_burst = BURST_CAPACITY * len(API_KEYS)
    total_rpm = TARGET_RPM_PER_KEY * len(API_KEYS)

    print("=" * 65)
    print(" NVIDIA NIM -> HERMES PROXY")
    print("=" * 65)
    print(f"Versão:        {PROXY_VERSION}")
    print(f"Modelo:        {DEFAULT_NVIDIA_MODEL}")
    print(f"API Keys:      {len(API_KEYS)}")
    print(f"RPM por chave: {TARGET_RPM_PER_KEY} | RPM total: {total_rpm}")
    print(f"Burst/chave:   {BURST_CAPACITY} | Burst total: {total_burst}")
    print(f"Read timeout:  {READ_TIMEOUT}s | Orçamento de espera: {WAIT_BUDGET}s")
    print("Token Bucket: ATIVADO | Streaming: ATIVADO | Failover: ATIVADO")
    print("Health: /health | Troca de modelo em runtime: /admin/model")
    print("Legacy: POST /v1/completions (traduzido para chat/completions)")
    print("=" * 65)
    print()

    host = os.getenv("PROXY_HOST", "127.0.0.1")
    port = int(os.getenv("PROXY_PORT", "5000"))
    print(f"URL: http://{host}:{port}")
    print("=" * 65)
    print()

    # Waitress quando disponível (produção, drena conexões no stop);
    # werkzeug dev server apenas como fallback explícito (PROXY_SERVER=flask).
    server = os.getenv("PROXY_SERVER", "auto")
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        waitress_serve = None

    if waitress_serve and server != "flask":
        print(f"Servidor: waitress | threads=16 | http://{host}:{port}")
        waitress_serve(app, host=host, port=port, threads=16)
    else:
        print(f"Servidor: werkzeug dev | http://{host}:{port}")
        app.run(host=host, port=port, threaded=True)
