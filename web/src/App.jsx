import { useCallback, useEffect, useRef, useState } from "react";

const POLL_MS = 4000;
const nf = new Intl.NumberFormat("pt-BR");

function formatUptime(seconds) {
  if (seconds == null || seconds < 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

async function getJson(url, signal) {
  const res = await fetch(url, { signal, headers: { Accept: "application/json" } });
  const data = await res.json().catch(() => null);
  return { ok: res.ok && data != null, status: res.status, data };
}

function StatusBadge({ state }) {
  const map = {
    ok: { label: "Operacional", cls: "badge badge-ok" },
    degraded: { label: "Degradado", cls: "badge badge-degraded" },
    offline: { label: "Offline", cls: "badge badge-offline" },
  };
  const m = map[state] || map.offline;
  return (
    <span className={m.cls} role="status" aria-live="polite">
      <span className="dot" aria-hidden="true" />
      {m.label}
    </span>
  );
}

function SummaryCard({ label, value, sub, tone }) {
  return (
    <div className={`card summary${tone ? ` ${tone}` : ""}`}>
      <div className="summary-label">{label}</div>
      <div className="summary-value">{value}</div>
      {sub ? <div className="summary-sub">{sub}</div> : null}
    </div>
  );
}

function KeyCard({ ks, fetchedAt }) {
  const elapsed = fetchedAt ? (Date.now() - fetchedAt) / 1000 : 0;
  const cooldown = Math.max(0, (ks.cooldown_seconds || 0) - elapsed);
  const tokens = ks.tokens_available ?? 0;
  const burst = ks.burst_capacity || 1;
  const pct = Math.max(0, Math.min(100, (tokens / burst) * 100));
  const problem = cooldown > 0 || ks.last_error;
  return (
    <div className={`card key-card${problem ? " key-problem" : ""}`}>
      <div className="key-head">
        <span className="key-id">{ks.key}</span>
        {cooldown > 0 ? (
          <span className="cooldown" title="Key em cooldown">cooldown {Math.ceil(cooldown)}s</span>
        ) : null}
      </div>
      <div className="meter-row">
        <div
          className="meter"
          role="progressbar"
          aria-valuenow={Math.round(tokens)}
          aria-valuemin={0}
          aria-valuemax={burst}
          aria-label={`Tokens disponíveis ${ks.key}`}
        >
          <div className="meter-fill" style={{ width: `${pct}%` }} />
        </div>
        <span className="meter-text">{tokens.toFixed(1)}/{burst}</span>
      </div>
      <div className="key-stats">
        <span title="Requisições">req {nf.format(ks.total_requests ?? 0)}</span>
        <span className="ok-text" title="Sucessos">ok {nf.format(ks.total_success ?? 0)}</span>
        <span className="fail-text" title="Falhas">falha {nf.format(ks.total_failures ?? 0)}</span>
        <span className="warn-text" title="HTTP 429">429 {nf.format(ks.total_429 ?? 0)}</span>
      </div>
      {ks.last_error ? (
        <div className="key-error" title={ks.last_error}>
          {ks.last_error}
        </div>
      ) : null}
    </div>
  );
}

export default function App() {
  const [health, setHealth] = useState(null);
  const [modelInfo, setModelInfo] = useState(null);
  const [fetchedAt, setFetchedAt] = useState(null);
  const [selected, setSelected] = useState(null);
  const [applying, setApplying] = useState(false);
  const [feedback, setFeedback] = useState(null);
  const [, setTick] = useState(0);
  const abortRef = useRef(null);
  const manualRefresh = useRef(null);

  const refresh = useCallback(async (signal) => {
    let anyOk = false;
    try {
      const h = await getJson("/health", signal);
      if (h.ok) {
        setHealth(h.data);
        setFetchedAt(Date.now());
        anyOk = true;
      }
    } catch (e) {
      if (e.name === "AbortError") throw e;
    }
    try {
      const m = await getJson("/admin/model", signal);
      if (m.ok) setModelInfo(m.data);
      anyOk = anyOk || m.ok;
    } catch (e) {
      if (e.name === "AbortError") throw e;
    }
    if (!anyOk) {
      setHealth(null);
      setModelInfo(null);
    }
  }, []);

  useEffect(() => {
    let stopped = false;
    let timer = null;

    const loop = async () => {
      if (stopped) return;
      if (!document.hidden) {
        const ctrl = new AbortController();
        abortRef.current = ctrl;
        try {
          await refresh(ctrl.signal);
        } catch (e) {
          /* rede indisponível: segue o ciclo */
        }
      }
      if (!stopped) timer = setTimeout(loop, POLL_MS);
    };

    manualRefresh.current = () => {
      clearTimeout(timer);
      abortRef.current?.abort();
      loop();
    };

    const onVisible = () => {
      if (!document.hidden) manualRefresh.current?.();
    };

    loop();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
      abortRef.current?.abort();
    };
  }, [refresh]);

  // Tick de 1s apenas para a contagem regressiva dos cooldowns.
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const state = !health ? "offline" : health.status === "degraded" ? "degraded" : "ok";
  const currentModel = modelInfo?.model ?? health?.model ?? null;
  const options = modelInfo?.available ?? [];
  const selectValue = selected ?? currentModel ?? "";

  const applyModel = async () => {
    if (!selected || applying || selected === currentModel) return;
    setApplying(true);
    setFeedback(null);
    try {
      const res = await fetch("/admin/model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: selected }),
      });
      const data = await res.json().catch(() => null);
      if (res.ok && data?.model) {
        setFeedback({ type: "success", message: `Modelo alterado para ${data.model}.` });
        setModelInfo((mi) => (mi ? { ...mi, model: data.model } : mi));
        setHealth((h) => (h ? { ...h, model: data.model } : h));
        setSelected(null);
      } else {
        setFeedback({
          type: "error",
          message: data?.error?.message || `Erro HTTP ${res.status} ao alterar o modelo.`,
        });
      }
    } catch (e) {
      setFeedback({ type: "error", message: "Falha de rede ao contatar o proxy." });
    } finally {
      setApplying(false);
    }
  };

  return (
    <div className="page">
      <header className="header">
        <div className="header-title">
          <h1>NVIDIA Proxy</h1>
          {health?.version ? <span className="version">v{health.version}</span> : null}
        </div>
        <StatusBadge state={state} />
      </header>

      {state === "offline" ? (
        <div className="offline-banner card" role="alert">
          <span>Não foi possível contatar o proxy em 127.0.0.1:5000.</span>
          <button type="button" className="btn" onClick={() => manualRefresh.current?.()}>
            Tentar agora
          </button>
        </div>
      ) : null}

      <section className="summary-grid" aria-label="Resumo">
        <SummaryCard
          label="Keys disponíveis"
          value={health ? `${health.keys_available} de ${health.keys}` : "—"}
          sub={health ? `${health.rpm_per_key} RPM por key` : null}
          tone={health && health.keys_available === 0 ? "danger" : null}
        />
        <SummaryCard
          label="RPM total"
          value={health ? nf.format(health.total_target_rpm) : "—"}
          sub={health ? `burst ${nf.format(health.total_burst_capacity)}` : null}
        />
        <SummaryCard
          label="Streams ativos"
          value={health ? nf.format(health.active_streams) : "—"}
          sub={health ? `${nf.format(health.requests_waiting)} em espera` : null}
        />
        <SummaryCard
          label="Uptime"
          value={health ? formatUptime(health.uptime_seconds) : "—"}
        />
      </section>

      {health ? (
        <section className="card totals" aria-label="Totais">
          <span>Requisições <strong>{nf.format(health.totals?.requests ?? 0)}</strong></span>
          <span className="ok-text">Sucessos <strong>{nf.format(health.totals?.success ?? 0)}</strong></span>
          <span className="fail-text">Falhas <strong>{nf.format(health.totals?.failures ?? 0)}</strong></span>
          <span className="warn-text">429 <strong>{nf.format(health.totals?.http_429 ?? 0)}</strong></span>
        </section>
      ) : null}

      <section className="card model-section" aria-label="Modelo">
        <div className="model-head">
          <h2>Modelo</h2>
          <span className="model-current" title="Modelo em uso">
            em uso: <strong>{currentModel ?? "—"}</strong>
          </span>
        </div>
        <div className="model-form">
          <label className="model-label" htmlFor="model-select">Disponíveis</label>
          <select
            id="model-select"
            className="select"
            value={selectValue}
            disabled={applying || options.length === 0}
            onChange={(e) => setSelected(e.target.value)}
          >
            {options.length === 0 ? <option value="">carregando…</option> : null}
            {options.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
          <button
            type="button"
            className="btn btn-primary"
            onClick={applyModel}
            disabled={applying || !selected || selected === currentModel}
            aria-busy={applying}
          >
            {applying ? "Aplicando…" : "Aplicar"}
          </button>
        </div>
        {feedback ? (
          <div className={`feedback ${feedback.type}`} role="status" aria-live="polite">
            {feedback.message}
          </div>
        ) : null}
      </section>

      <section aria-label="Chaves de API" className="keys-section">
        <h2>Chaves</h2>
        {health?.keys_status?.length ? (
          <div className="keys-grid">
            {health.keys_status.map((ks) => (
              <KeyCard key={ks.key} ks={ks} fetchedAt={fetchedAt} />
            ))}
          </div>
        ) : (
          <div className="card empty">Sem dados de keys.</div>
        )}
      </section>
    </div>
  );
}
