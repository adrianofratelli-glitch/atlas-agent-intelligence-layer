import { lazy, Suspense, useEffect, useState } from 'react';
import { api } from './api.js';

const FlexibleSchema = lazy(() => import('./tabs/FlexibleSchema.jsx'));
const ModelSwap = lazy(() => import('./tabs/ModelSwap.jsx'));
const Agent = lazy(() => import('./tabs/Agent.jsx'));

const TABS = [
  'Schema ao vivo',
  'Modelo e custo',
  'Agente',
];

export default function App() {
  const [selected, setSelected] = useState(0);
  const [visited, setVisited] = useState(() => new Set([0]));

  // Result state lives HERE (lifted state): switching tabs or re-rendering
  // any component never wipes pipeline/chat results.
  const [schemaState, setSchemaState] = useState({ doc: null, flash: 0 });
  const [modelSwapState, setModelSwapState] = useState({ config: null, messages: [] });
  const [agentState, setAgentState] = useState({
    run: null,
    step: -1,
    iteration: 0,
    conversationId: null,
    turns: [],
  });

  // cluster health — feeds the status pill and the stat bar
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(false);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (document.visibilityState !== 'visible') return;
      try {
        const h = await api.health();
        if (alive) {
          setHealth(h);
          setHealthError(false);
        }
      } catch {
        if (alive) setHealthError(true);
      }
    };
    tick();
    const id = setInterval(tick, 10_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const panes = [
    <FlexibleSchema state={schemaState} setState={setSchemaState} />,
    <ModelSwap state={modelSwapState} setState={setModelSwapState} />,
    <Agent state={agentState} setState={setAgentState} />,
  ];

  const selectTab = (index) => {
    setVisited((current) => new Set(current).add(index));
    setSelected(index);
  };

  const counts = health?.counts ?? {};

  return (
    <div data-pov-shell>
      <a className="pov-skip-link" href="#conteudo-principal">Pular para o conteúdo</a>
      <nav className="top-nav">
        <div className="nav-inner">
          <span className="nav-logo">
            <span className="leaf">●</span> MongoDB Intelligence Layer
          </span>
          <div className="nav-pills">
            {TABS.map((name, i) => (
              <button
                key={name}
                className={`nav-pill ${i === selected ? 'active' : ''}`}
                aria-current={i === selected ? 'page' : undefined}
                onClick={() => selectTab(i)}
              >
                {name}
              </button>
            ))}
          </div>
          <span className="status-pill">
            <span className={`status-dot ${healthError || !health ? 'err' : 'ok'}`} />
            {healthError ? 'sem conexão' : health ? 'Atlas · ping ok' : 'conectando…'}
          </span>
        </div>
      </nav>

      <main id="conteudo-principal" tabIndex={-1} className="content">
        <header className="stage-heading">
          <h1 className="page-title">Inteligência como <span>documento</span></h1>
          <p>{counts.support_orders ?? '—'} pedidos · {health?.primary_model ?? 'modelo conectando'}</p>
        </header>

        {panes.map((pane, i) => visited.has(i) && (
          <div key={i} style={{ display: i === selected ? 'block' : 'none' }}>
            <Suspense fallback={<div className="card">Carregando etapa…</div>}>
              <div className={i === selected ? 'fade-in' : ''}>{pane}</div>
            </Suspense>
          </div>
        ))}
      </main>

    </div>
  );
}
