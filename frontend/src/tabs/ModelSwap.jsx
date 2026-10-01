import { useEffect, useState } from 'react';
import Badge from '@leafygreen-ui/badge';
import Banner from '@leafygreen-ui/banner';
import Button from '@leafygreen-ui/button';
import TextInput from '@leafygreen-ui/text-input';
import JsonViewer from '../components/JsonViewer.jsx';
import { api } from '../api.js';

const modelBadge = (model) => {
  if (model?.startsWith('gpt')) return 'green';
  if (model?.includes('opus')) return 'purple';
  return model?.includes('sonnet') ? 'blue' : 'yellow';
};

// Cost comes from the measured provider ledger, including fallback attempts.
function costStats(messages) {
  const byModel = {};
  for (const m of messages) {
    if (!m.meta?.llm_calls?.length) continue;
    const key = m.meta.model;
    const cost = m.meta.economics?.estimated_cost_usd;
    byModel[key] ??= { n: 0, cost: 0, complete: true, latency: 0 };
    const row = byModel[key];
    row.n += 1;
    row.complete &&= cost != null;
    row.cost += cost || 0;
    row.latency += m.meta.latency_ms || 0;
  }
  return Object.entries(byModel).map(([model, s]) => ({family: model, n: s.n,
    avgLatency: Math.round(s.latency / s.n),
    perQuery: s.complete ? s.cost / s.n : null,
    monthly: s.complete ? s.cost / s.n * 10000 * 30 : null}));
}

export default function ModelSwap({ state, setState }) {
  const { config, messages } = state;
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [swapping, setSwapping] = useState(false);
  const [error, setError] = useState(null);
  const [flash, setFlash] = useState(0);
  const [savings, setSavings] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [noCache, setNoCache] = useState(false);

  const loadSavings = async () => {
    try {
      const m = await api.metrics();
      setSavings(m.savings || null);
    } catch {
      /* card de economia é opcional — nunca quebra a aba */
    }
  };

  const loadConfig = async () => {
    try {
      const c = await api.getModelConfig();
      setState((s) => ({ ...s, config: c }));
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => {
    if (!config) loadConfig();
    loadSavings();
    api.models().then((r) => setCatalog(r.models || [])).catch(() => {});
  }, []);

  const swap = async () => {
    setSwapping(true);
    setError(null);
    try {
      const c = await api.swapModels();
      setState((s) => ({ ...s, config: c }));
      setFlash((f) => f + 1);
    } catch (e) {
      setError(e.message);
    } finally {
      setSwapping(false);
    }
  };

  const pick = async (model) => {
    if (swapping || config?.primary?.model === model) return;
    setSwapping(true);
    setError(null);
    try {
      const c = await api.setPrimaryModel(model);
      setState((s) => ({ ...s, config: c }));
      setFlash((f) => f + 1);
    } catch (e) {
      setError(e.message);
    } finally {
      setSwapping(false);
    }
  };

  const ask = async () => {
    if (!question.trim() || busy) return;
    const q = question.trim();
    // Cada pergunta é independente (sem histórico): a comparação de modelo e
    // custo fica justa, e o input de tokens não cresce a cada turno.
    setBusy(true);
    setError(null);
    setState((s) => ({ ...s, messages: [...s.messages, { role: 'user', text: q }] }));
    setQuestion('');
    try {
      const r = await api.quickChat(q, [], noCache);
      setState((s) => ({
        ...s,
        messages: [...s.messages, { role: 'assistant', text: r.text, meta: r }],
      }));
      loadSavings();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      {error && (
        <Banner variant="warning" darkMode>
          {error}
        </Banner>
      )}

      <div className="two-col">
        <div className="card">
          <div className="card-header">
            <span className="card-title">ai_brain.model_config — lido a cada request</span>
            {config && <Badge variant={modelBadge(config.primary?.model)}>{config.primary?.model}</Badge>}
          </div>
          <div className="row" style={{ marginBottom: 12 }}>
            <Button darkMode variant="primary" onClick={swap} disabled={swapping || !config}>
              {swapping ? 'update_one no Atlas…' : 'Trocar primary: Sonnet ↔ Haiku'}
            </Button>
            <span className="dim">zero restart · zero deploy</span>
          </div>
          {catalog.length > 0 && (
            <div className="row" style={{ marginBottom: 12, flexWrap: 'wrap', gap: 6 }}>
              {catalog.map((m) => (
                <Button
                  key={m.model}
                  darkMode
                  size="xsmall"
                  variant={config?.primary?.model === m.model ? 'primary' : 'default'}
                  onClick={() => pick(m.model)}
                  disabled={swapping || !config}
                >
                  {m.model}
                </Button>
              ))}
            </div>
          )}
          <p className="dim" style={{ marginTop: 0, marginBottom: 12, fontSize: '0.8rem' }}>
            Este mesmo documento controla o <strong>agente da aba 03</strong>: trocar o
            primary aqui (Claude ou OpenAI, via Grove) muda o agente ao vivo — um
            <code> update_one</code>, sem deploy.
          </p>
          {config ? (
            <JsonViewer doc={config} flashKey={flash} />
          ) : (
            <div className="dim">carregando…</div>
          )}
        </div>

        <div className="card">
          <div className="card-header">
            <span className="card-title">Mini-chat — quem responde é o doc</span>
          </div>
          <div className="chat-box" style={{ minHeight: 220 }}>
            {messages.length === 0 && (
              <div className="dim">
                Cada pergunta é independente (sem contexto). Troque o primary no documento e repita a
                pergunta com "Comparar modelos" ligado — o badge muda sem reiniciar nada.
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i}>
                <div className={`chat-msg ${m.role}`}>{m.text}</div>
                {m.meta && (
                  <div className="chat-meta">
                    <Badge variant={modelBadge(m.meta.model)}>{m.meta.model}</Badge>
                    <span className="dim mono">{m.meta.latency_ms} ms</span>
                    <span className="dim mono">
                      {m.meta.input_tokens}→{m.meta.output_tokens} tokens
                    </span>
                    {m.meta.route === 'fallback' && <Badge variant="red">fallback</Badge>}
                    {m.meta.route === 'cache' && <Badge variant="green">cache semântico</Badge>}
                  </div>
                )}
              </div>
            ))}
            {busy && <div className="row"><div className="spinner" /> <span className="dim">chamando o modelo configurado…</span></div>}
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <div style={{ flex: 1 }}>
              <TextInput
                darkMode
                label="Pergunta para comparar modelos"
                placeholder="ex.: qual a capital da Austrália?"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && ask()}
              />
            </div>
            <Button darkMode variant="primary" onClick={ask} disabled={busy}>
              Enviar
            </Button>
          </div>
          <label className="dim" style={{ display: 'block', marginTop: 8, fontSize: '0.8rem' }}>
            <input type="checkbox" checked={noCache} onChange={(e) => setNoCache(e.target.checked)} />
            {' '}Comparar modelos (ignora o cache semântico)
          </label>
        </div>
      </div>

      {savings && savings.cache_hits > 0 && savings.estimated_saved_usd != null && (
        <div className="card neutral">
          <div className="card-header">
            <span className="card-title">Economia — cache semântico</span>
            <span className="dim mono">{savings.cache_hits} hits nesta sessão do backend</span>
          </div>
          <div className="cost-grid" style={{ gap: 40 }}>
            <div className="cost-item">
              <div className="cost-val">${savings.estimated_saved_usd.toFixed(4)}</div>
              <div className="cost-label">
                poupado (~${savings.avg_llm_call_usd.toFixed(5)} por turno evitado,
                estimativa pela média observada; economia efetiva não medida)
              </div>
            </div>
          </div>
        </div>
      )}

      {costStats(messages).length === 1 && (
        <div className="card neutral">
          <p className="dim" style={{ margin: 0 }}>
            💡 Agora troque o primary no documento e repita a pergunta — com respostas dos
            dois modelos, a comparação de custo aparece aqui com os tokens reais.
          </p>
        </div>
      )}

      {costStats(messages).length > 0 && (
        <div className="card neutral">
          <div className="card-header">
            <span className="card-title">O swap em dinheiro — tokens reais desta sessão</span>
            <span className="dim mono">projeção @ 10.000 queries/dia</span>
          </div>
          <div className="cost-grid" style={{ gap: 40 }}>
            {costStats(messages).map((s) => (
              <div className="cost-item" key={s.family}>
                <div className="row" style={{ marginBottom: 4 }}>
                  <Badge variant={modelBadge(s.family)}>
                    {s.family}
                  </Badge>
                  <span className="dim mono">{s.n} respostas · ~{s.avgLatency} ms</span>
                </div>
                <div className="cost-val">{s.monthly != null ? `$${s.monthly.toFixed(0)}/mês` : 'Custo indisponível'}</div>
                <div className="cost-label">
                  {s.perQuery != null ? `$${s.perQuery.toFixed(5)} por query (estimativa Grove)` : 'Tarifa ou consumo ausente'}
                </div>
              </div>
            ))}
          </div>
          <p className="dim" style={{ marginTop: 12, marginBottom: 0 }}>
            Trocar o modelo é um update_one — e a diferença de custo aparece aqui, calculada
            com os tokens reais e médias históricas do Grove. Projeção estimada, não cobrança exata.
          </p>
        </div>
      )}
    </div>
  );
}
