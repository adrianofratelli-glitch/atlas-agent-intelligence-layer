/**
 * Cadeia de trocas — leitura visual do que o $graphLookup devolveu.
 *
 * O backend já resume a travessia antes de ela virar tool_result (agent.py:
 * _summarize_chain_text), então o trace carrega o resumo em JSON. Aqui ele vira a
 * corrente de pedidos: é a única forma de mostrar numa tela por que a resposta mudou —
 * nenhum documento sozinho diz "é a terceira unidade do mesmo item".
 */
function parseChain(events) {
  const call = (events || []).find(
    (event) => event.kind === 'tool_call'
      && event.tool === 'aggregate'
      && event.args?.pipeline?.some?.((stage) => stage && stage.$graphLookup),
  );
  if (!call) return null;
  try {
    const parsed = typeof call.result === 'string' ? JSON.parse(call.result) : call.result;
    return parsed?.path?.length ? parsed : null;
  } catch {
    return null; // resultado não-estruturado: o painel some, o trace cru continua visível
  }
}

export default function ReplacementChain({ events }) {
  const chain = parseChain(events);
  if (!chain) return null;

  const recurring = Boolean(chain.recurring_defect);
  const reasons = chain.reasons || [];

  return (
    <section className={`chain-panel ${recurring ? 'chain-alert' : ''}`} aria-label="Cadeia de trocas do pedido">
      <div className="chain-head">
        <span>cadeia de trocas · travessia de grafo</span>
        <code>$graphLookup</code>
      </div>

      <ol className="chain-track">
        {chain.path.map((orderId, index) => (
          <li className="chain-node" key={orderId || index}>
            {index > 0 && (
              <span className="chain-link" aria-hidden="true" title={reasons[index - 1] || 'reposição'}>
                <i />repôs<i />
              </span>
            )}
            <span className={`chain-pill ${index === 0 ? 'origin' : ''} ${index === chain.path.length - 1 ? 'current' : ''}`}>
              <code>{orderId}</code>
              <small>{index === 0 ? 'pedido original' : `${index}ª reposição`}</small>
            </span>
          </li>
        ))}
      </ol>

      <div className="chain-signals">
        <div className="chain-signal">
          <strong>{chain.replacements}</strong>
          <span>reposições na cadeia</span>
        </div>
        <div className="chain-signal">
          <strong>{chain.same_sku_count ?? '—'}</strong>
          <span>unidades do mesmo SKU</span>
        </div>
        <div className="chain-signal">
          <strong className="sku">{chain.sku || '—'}</strong>
          <span>{chain.product_name || 'produto'}</span>
        </div>
      </div>

      {/* Cor nunca é o único indicador: ícone + rótulo acompanham o estado. */}
      <p className={`chain-verdict ${recurring ? 'alert' : 'ok'}`}>
        <span aria-hidden="true">{recurring ? '⚠' : '✓'}</span>
        {recurring
          ? `Defeito recorrente: ${chain.product_name || 'o mesmo item'} falhou repetidas vezes. O agente abre chamado de qualidade em vez de processar mais uma troca — trocar de novo repetiria o defeito.`
          : 'Cadeia curta: reposição pontual, sem padrão de defeito de lote. Atendimento segue o fluxo normal.'}
      </p>

      <p className="chain-footnote">
        Uma agregação percorreu a cadeia inteira dentro do banco. Sem <code>$graphLookup</code> seriam{' '}
        {chain.replacements + 1} idas ao MongoDB — e o número de saltos não é conhecido antes de percorrer.
        O pipeline é montado pelo servidor a partir do <code>order_id</code>; o modelo nunca escreve a travessia.
      </p>
    </section>
  );
}
