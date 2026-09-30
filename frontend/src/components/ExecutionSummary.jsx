const labels = { support_agent: 'Atendimento', memory_extractor: 'Memória', model_swap: 'Assistente', investigator: 'Investigador' };
const states = { ok: 'Concluída', error: 'Falhou', incomplete: 'Incompleta', circuit_open: 'Não enviada' };
const modelName = model => model.replace(/^claude-haiku-4-5$/, 'Claude Haiku 4.5').replace(/^claude-sonnet-4-5$/, 'Claude Sonnet 4.5').replace(/^claude-sonnet-5$/, 'Claude Sonnet 5').replace(/^gpt-5.6-luna$/, 'GPT-5.6 Luna');

export default function ExecutionSummary({ calls, cost, mode, title = 'Resumo do turno', note, pending = false }) {
  const attempts = calls.filter(c => c.status !== 'circuit_open');
  const fallback = calls.some(c => c.fallback);
  const failures = calls.filter(c => c.status === 'error' || c.status === 'incomplete').length;
  const costLabel = cost != null ? `$${cost.toFixed(4)}` : '—';
  return <details className="pov-execution">
    <summary>
      <span className="pov-execution-title">{title}</span>
      <span className="pov-execution-metric"><strong>{costLabel}</strong><small>{cost != null ? 'USD estimados' : pending ? 'Calculando custo' : 'Custo não disponível'}</small></span>
      <span className="pov-execution-metric"><strong>{attempts.length}</strong><small>{attempts.length === 1 ? 'chamada de LLM' : 'chamadas de LLM'}</small></span>
      <span className="pov-execution-mode">{mode}</span>
      {fallback && <span className="pov-execution-alert">Modelo alternativo</span>}
      {!!failures && <span className="pov-execution-alert">{failures} {failures === 1 ? 'tentativa sem conclusão' : 'tentativas sem conclusão'}</span>}
      <span className="pov-execution-toggle" aria-hidden="true" />
    </summary>
    <div className="pov-execution-body">
      <p>{note}</p>
      {!calls.length ? <p>{pending ? 'As chamadas aparecerão conforme forem concluídas.' : 'Nenhuma chamada de LLM registrada nesta execução.'}</p> :
        <ol className="pov-execution-calls">{calls.map((call, i) => <li key={i}>
          <div><strong>{labels[call.agent] || call.agent}</strong><span>{modelName(call.model)}{call.fallback ? ' · alternativo (fallback)' : ''}</span></div>
          <div className="pov-execution-call-meta"><span className={call.status === 'error' || call.status === 'incomplete' ? 'pov-execution-alert' : ''}>{states[call.status] || 'Estado não informado'}</span><span>{Number.isFinite(call.latency_ms) ? `${(call.latency_ms / 1000).toLocaleString('pt-BR', { maximumFractionDigits: 2 })} s` : 'Duração não informada'}</span>{call.usage_known && <span>{((call.input_tokens || 0) + (call.output_tokens || 0) + (call.cache_read_tokens || 0) + (call.cache_write_tokens || 0)).toLocaleString('pt-BR')} tokens</span>}</div>
        </li>)}</ol>}
      {calls.some(c => (c.cache_read_tokens || 0) > 0) && <p>Cache do provedor: {calls.reduce((n, c) => n + (c.cache_read_tokens || 0), 0).toLocaleString('pt-BR')} tokens reaproveitados.</p>}
      <p className="pov-execution-disclaimer">Custo estimado pelas médias observadas no Grove, incluindo tokens de cache uma única vez. Não representa cobrança exata.{cost == null && !pending ? ' Há tarifa ou consumo não informado; o valor não foi tratado como zero.' : ''}</p>
    </div>
  </details>;
}
