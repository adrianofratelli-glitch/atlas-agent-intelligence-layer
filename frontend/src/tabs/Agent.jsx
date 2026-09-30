import { useEffect, useRef, useState } from 'react';
import Badge from '@leafygreen-ui/badge';
import Banner from '@leafygreen-ui/banner';
import Button from '@leafygreen-ui/button';
import TextInput from '@leafygreen-ui/text-input';
import { api } from '../api.js';
import ReplacementChain from '../components/ReplacementChain.jsx';
import QueryDetails from '../components/QueryDetails.jsx';
import ExecutionSummary from '../components/ExecutionSummary.jsx';

// Prova visual do pitch "MongoDB reduz custo de LLM", direto na PoV — sem precisar
// abrir o Langfuse para ver. Duas camadas de cache SÃO dados reais deste turno,
// não estimativa de marketing: cache semântico ($vectorSearch em POC.semantic_cache,
// HIT = zero chamada ao LLM) e prompt cache da Anthropic (cache_control ephemeral no
// system prompt, ancorado no MongoDB via ai_brain.area_profiles/model_config — o que
// muda o texto do system é um documento, então o prefixo cacheável é estável entre
// turnos da mesma área).
function MongoCacheSavings({ run }) {
  const calls = run.llm_calls || [];
  const cost = run.economics?.estimated_cost_usd;
  const parallel = calls.some((a, i) => calls.slice(i + 1).some(b => a.agent !== b.agent &&
    Math.max(Date.parse(a.started_at), Date.parse(b.started_at)) <
    Math.min(Date.parse(a.started_at) + a.latency_ms, Date.parse(b.started_at) + b.latency_ms)));
  const blocked = run.guardrail?.input?.action === 'block';
  return <ExecutionSummary calls={calls} cost={cost}
    mode={run.cache?.hit ? 'Resposta em cache' : blocked ? 'Bloqueado pela política' : !calls.length ? 'Sem uso de LLM' : parallel ? 'Atendimento + memória em paralelo' : 'Loop de um agente'}
    note={run.cache?.hit ? 'Resposta reaproveitada do cache semântico do MongoDB.' : 'Um agente alterna raciocínio e ferramentas MCP. A extração de memória é uma etapa auxiliar e pode ocorrer em paralelo.'} />;
}

// As 6 fases do loop agêntico (mesma narrativa do Perceive→Reason→Act→Store).
const PHASES = [
  { key: 'perceive', icon: '👁️', label: 'Perceber', sub: 'Usuário → Agente' },
  { key: 'retrieve', icon: '🔍', label: 'Recuperar', sub: 'Agente ↔ MongoDB' },
  { key: 'reason', icon: '🧠', label: 'Raciocinar', sub: 'Agente → LLM' },
  { key: 'act', icon: '⚡', label: 'Agir', sub: 'Agente → MongoDB' },
  { key: 'store', icon: '💾', label: 'Salvar', sub: 'Agente → MongoDB' },
  { key: 'loop', icon: '🔁', label: 'Repetir', sub: 'Próximo turno' },
];

const PHASE_CAPTION = {
  perceive: 'O agente recebe a mensagem do cliente — início do turno.',
  retrieve: 'O agente consulta o MongoDB pelo MCP Server (find / $vectorSearch).',
  reason: 'O modelo raciocina sobre qual será a próxima ação.',
  act: 'O agente grava a decisão no MongoDB (update do pedido).',
  store: 'O turno é salvo em agent_sessions — memória persistida da conversa.',
  loop: 'Turno concluído. O agente está pronto para continuar a conversa.',
};

const short = (obj, n = 160) => {
  if (obj == null) return '';
  const s = typeof obj === 'string' ? obj : JSON.stringify(obj);
  if (s == null) return '';
  return s.length > n ? s.slice(0, n) + '…' : s;
};

const opTarget = (args = {}) => `${args.database ?? '?'}.${args.collection ?? '?'}`;
// O nome da ferramenta MCP ("aggregate") cobre tanto a busca vetorial do catálogo quanto a
// travessia da cadeia de trocas. O estágio real vem do pipeline que o servidor montou, então
// o rótulo é derivado do que foi de fato enviado — não de um palpite pelo nome da collection.
const STAGE_LABELS = { $vectorSearch: '$vectorSearch', $graphLookup: '$graphLookup' };
const opLabel = (event = {}) => {
  const stages = event.args?.pipeline;
  if (Array.isArray(stages)) {
    for (const stage of stages) {
      for (const key of Object.keys(stage || {})) {
        if (STAGE_LABELS[key]) return STAGE_LABELS[key];
      }
    }
  }
  return event.tool;
};
const opDetail = (args = {}) => {
  if (args.pipeline) return short(args.pipeline, 200);
  if (args.filter) return 'filter: ' + short(args.filter, 140);
  if (args.update) return 'update: ' + short(args.update, 140);
  if (args.query) return 'query: ' + short(args.query, 140);
  return '';
};

// Identidade: cada usuário (POC.app_users) tem seu user_key e pertence a uma ÁREA.
// user_key isola a memória (curta e longa); a área decide persona, guardrails e
// escopo do cache. Fallback quando o seed ainda não criou app_users.
const FALLBACK_USER = {
  user_key: 'cliente-demo',
  name: 'Adriano',
  area: 'default',
  area_label: 'Suporte E-commerce',
};

// Ponteiro user_key → session_id ativo, persistido na aba. Só o ponteiro: os
// turnos em si continuam sendo lidos do MongoDB (POC.agent_sessions).
const CONV_MAP_KEY = 'poc.agent.convMap';

function readConvMap() {
  try {
    return JSON.parse(sessionStorage.getItem(CONV_MAP_KEY) ?? '{}') ?? {};
  } catch {
    return {};
  }
}

function persistConvMap(map) {
  try {
    sessionStorage.setItem(CONV_MAP_KEY, JSON.stringify(map));
  } catch {
    /* modo privado / storage cheio: a demo segue, só não sobrevive ao F5 */
  }
}

// Rótulo curto pro indicador de progresso ao vivo (SSE) — traduz phase/kind
// em algo que um cliente na demo entende, sem esperar o turno inteiro (até 120s).
function describeLiveEvent(event) {
  if (event.kind === 'tool_call') return `chamando ferramenta ${event.tool ?? '?'}...`;
  if (event.kind === 'guardrail') return `verificando guardrails (${event.stage ?? '?'})...`;
  if (event.kind === 'reasoning') return 'processando resposta...';
  const byPhase = {
    perceive: 'lendo a mensagem...',
    retrieve: 'consultando o MongoDB...',
    reason: 'raciocinando...',
    act: 'preparando a resposta...',
    store: 'salvando na memória...',
    loop: 'concluindo o turno...',
  };
  return byPhase[event.phase] ?? 'processando...';
}

export default function Agent({ state, setState }) {
  const { run, step, iteration, conversationId, turns = [] } = state;
  const [scenarios, setScenarios] = useState([]);
  const [tools, setTools] = useState([]);
  const [users, setUsers] = useState([]);
  const [user, setUser] = useState(FALLBACK_USER);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [liveStatus, setLiveStatus] = useState(null); // streaming: passo em andamento, em vez de loading genérico
  const [error, setError] = useState(null);
  const [playing, setPlaying] = useState(false);
  const [walk, setWalk] = useState(false);
  const [showInspector, setShowInspector] = useState(false);
  const [playlist, setPlaylist] = useState([]);
  const [demo, setDemo] = useState({ active: false, idx: -1, paused: false });
  const [tokensSaved, setTokensSaved] = useState(0);
  // histórico da demo: um snapshot {run, user, conversationId, turns} por script já
  // executado — permite VOLTAR aos scripts anteriores (pausado) sem nova chamada.
  const [demoHist, setDemoHist] = useState([]);
  const timerRef = useRef(null);
  const demoTimerRef = useRef(null); // separado do replay: não é limpo pelo cleanup do replay
  // conversa corrente de cada usuário: trocar de identidade e voltar retoma a
  // MESMA sessão daquele usuário (a memória curta continua de onde parou).
  // Persistido em sessionStorage: um F5 no meio da demo não pode inventar uma
  // sessão nova e fazer a memória curta parecer perdida (ela vive no Atlas, com
  // TTL de 24h — o que se perdia era só o ponteiro no browser).
  const convMapRef = useRef(readConvMap());
  // refs read inside the replay-end effect (avoid stale closures on chaining)
  const demoRef = useRef(demo);
  const playlistRef = useRef(playlist);
  const usersRef = useRef(users);
  const demoHistRef = useRef(demoHist);
  useEffect(() => { demoRef.current = demo; }, [demo]);
  useEffect(() => { playlistRef.current = playlist; }, [playlist]);
  useEffect(() => { usersRef.current = users; }, [users]);
  useEffect(() => { demoHistRef.current = demoHist; }, [demoHist]);

  useEffect(() => {
    api.agentPlaylist().then((d) => setPlaylist(d.playlist)).catch(() => {});
    api.agentTools().then((d) => setTools(d.tools)).catch(() => {});
    api.users()
      .then((d) => {
        if (d.users?.length) {
          setUsers(d.users);
          setUser(d.users.find((u) => u.user_key === FALLBACK_USER.user_key) ?? d.users[0]);
        }
      })
      .catch(() => {});
  }, []);

  // Contador acumulado de tokens economizados (cache hits evitando o LLM) —
  // vem de /api/metrics.counters.tokens_economizados, atualiza a cada turno.
  useEffect(() => {
    api.metrics().then((d) => setTokensSaved(d?.counters?.tokens_economizados ?? 0)).catch(() => {});
  }, [run]);

  // "Login" da demo: trocar de identidade no switcher emite um JWT novo; toda
  // request seguinte vai com Bearer e o backend resolve a identidade do token.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!user?.user_key) return;
      try {
        await api.login(user.user_key);
      } catch {
        return; // sem token não adianta pedir a sessão: o backend nega
      }
      if (cancelled) return;
      // Reidrata a memória curta a partir do MongoDB. Sem isso, voltar para um
      // usuário mostrava o transcript vazio mesmo com a sessão viva no Atlas —
      // parecia perda de contexto e era só a tela.
      const convId = convMapRef.current[user.user_key];
      if (!convId) return;
      try {
        const doc = await api.memoryShort(convId, user.user_key);
        if (cancelled) return;
        const restored = (doc?.turns ?? []).map((t) => ({
          role: t.role === 'assistant' ? 'agent' : 'user',
          text: t.content,
        }));
        if (!restored.length) return;
        // só preenche se a tela ainda está vazia — nunca sobrescreve um turno
        // que acabou de ser executado
        setState((s) => (s.turns?.length ? s : { ...s, conversationId: convId, turns: restored }));
      } catch {
        /* sessão expirada pelo TTL ou indisponível: segue com transcript vazio */
      }
    })();
    // chips de sugestão são POR ÁREA: trocar de identidade recarrega os cenários
    // do departamento do usuário atual.
    api.agentScenarios(user?.user_key).then((d) => setScenarios(d.scenarios)).catch(() => {});
    return () => { cancelled = true; };
  }, [user?.user_key, setState]);

  const events = run?.trace ?? [];
  const lastStep = events.length - 1;
  const visible = step >= 0 ? events.slice(0, step + 1) : [];
  const current = step >= 0 ? events[step] : null;
  const activePhase = current?.phase;

  // auto-avanço do replay (Pausar/Continuar e Tour guiado)
  useEffect(() => {
    clearTimeout(timerRef.current);
    if (playing && step < lastStep) {
      timerRef.current = setTimeout(
        () => setState((s) => ({ ...s, step: s.step + 1 })),
        walk ? 1700 : 850,
      );
    } else if (playing && step >= lastStep) {
      setPlaying(false);
      setWalk(false);
      // demo automática: ao terminar o replay de um script, toca o próximo —
      // exceto se estiver pausada (aí o usuário explora e retoma quando quiser).
      // Usa demoTimerRef (não timerRef) para o cleanup deste efeito não cancelá-lo.
      if (demoRef.current.active && !demoRef.current.paused) {
        clearTimeout(demoTimerRef.current);
        demoTimerRef.current = setTimeout(advanceDemo, 1500);
      }
    }
    return () => clearTimeout(timerRef.current);
  }, [playing, step, lastStep, walk]);

  // O usuário que um script da playlist declara (troca de identidade ao vivo).
  const resolveUser = (item) =>
    item?.user_key ? usersRef.current.find((u) => u.user_key === item.user_key) ?? null : null;

  // Restaura o snapshot do script `i` do histórico da demo — replay puro, sem
  // chamada de API. pos: 'start' (passo 0) ou 'end' (último passo do trace).
  const jumpDemo = (i, pos = 'start') => {
    const snap = demoHistRef.current[i];
    if (!snap) return;
    clearTimeout(timerRef.current);
    clearTimeout(demoTimerRef.current);
    setPlaying(false);
    setWalk(false);
    setUser(snap.user);
    setDemo({ active: true, idx: i, paused: true });
    setState((s) => ({
      ...s,
      run: snap.run,
      conversationId: snap.conversationId,
      turns: snap.turns,
      step: pos === 'end' ? Math.max(0, (snap.run.trace?.length ?? 1) - 1) : 0,
    }));
  };

  const advanceDemo = () => {
    if (!demoRef.current.active) return;
    const next = demoRef.current.idx + 1;
    if (next < demoHistRef.current.length) {
      // já executado (o usuário voltou no histórico): replay do snapshot
      jumpDemo(next, 'start');
      setDemo({ active: true, idx: next, paused: false });
      setPlaying(true);
    } else if (next < playlistRef.current.length) {
      const item = playlistRef.current[next];
      setDemo({ active: true, idx: next, paused: false });
      runScenario({ message: item.message }, resolveUser(item));
    } else {
      setDemo({ active: false, idx: -1, paused: false });
    }
  };

  const startDemo = () => {
    if (busy || !playlist.length) return;
    setDemoHist([]);
    setDemo({ active: true, idx: 0, paused: false });
    runScenario({ message: playlist[0].message }, resolveUser(playlist[0]));
  };

  // Pausar: congela a demo onde está — o replay para, o avanço é cancelado, e o
  // usuário pode explorar tudo (trace, ops, painéis) manualmente.
  const pauseDemo = () => {
    clearTimeout(demoTimerRef.current);
    setPlaying(false);
    setDemo((d) => ({ ...d, paused: true }));
  };

  // Continuar: retoma de onde parou. Se o replay do script atual não acabou,
  // segue reproduzindo; se já acabou, avança para o próximo script.
  const resumeDemo = () => {
    setDemo((d) => ({ ...d, paused: false }));
    if (step >= lastStep) advanceDemo();
    else setPlaying(true);
  };

  // Encerrar de vez (usado no reset/erro/nova conversa e no botão ⏹).
  const stopDemo = () => {
    clearTimeout(timerRef.current);
    clearTimeout(demoTimerRef.current);
    setDemo({ active: false, idx: -1, paused: false });
    setPlaying(false);
  };

  // asUser (opcional): a demo automática troca de identidade por script. Trocar de
  // usuário retoma a conversa dele (convMapRef) — memória curta continua de onde parou.
  const runScenario = async (payload, asUser = null) => {
    if (busy) return;
    const target = asUser ?? user;
    const switched = target.user_key !== user.user_key;
    if (switched) setUser(target);
    setBusy(true);
    setError(null);
    setPlaying(false);
    setWalk(false);
    setLiveStatus('iniciando...');
    const convId =
      (switched ? convMapRef.current[target.user_key]
                : conversationId ?? convMapRef.current[target.user_key]) ?? null;
    try {
      const result = await api.agentRunStream(
        { ...payload, conversation_id: convId, user_key: target.user_key },
        (event) => setLiveStatus(describeLiveEvent(event)),
      );
      const finalConvId = result.conversation_id ?? convId;
      convMapRef.current[target.user_key] = finalConvId;
      persistConvMap(convMapRef.current);
      const newTurns = [
        ...(switched ? [] : turns ?? []),
        { role: 'user', text: result.user_message },
        { role: 'agent', text: result.answer },
      ];
      setState((s) => ({
        ...s,
        conversationId: finalConvId,
        run: result,
        step: 0,
        iteration: (s.iteration ?? 0) + 1,
        turns: newTurns,
      }));
      // demo: guarda o snapshot deste script para o "voltar" navegar sem re-executar
      if (demoRef.current.active && demoRef.current.idx >= 0) {
        const idx = demoRef.current.idx;
        setDemoHist((h) => [
          ...h.slice(0, idx),
          { run: result, user: target, conversationId: finalConvId, turns: newTurns },
        ]);
      }
      setPlaying(true); // auto-reproduz o trace
    } catch (e) {
      setError(e.message);
      setDemo({ active: false, idx: -1, paused: false }); // interrompe a demo se um script falhar
    } finally {
      setBusy(false);
      setLiveStatus(null);
    }
  };

  const send = () => {
    if (!input.trim()) return;
    runScenario({ message: input.trim() });
    setInput('');
  };

  // Reset: limpa só o replay atual; a conversa (memória) permanece
  const reset = () => {
    clearTimeout(timerRef.current);
    clearTimeout(demoTimerRef.current);
    setPlaying(false);
    setWalk(false);
    setDemo({ active: false, idx: -1, paused: false });
    setState((s) => ({ ...s, run: null, step: -1 }));
  };

  // Nova conversa: sessão nova — zera memória curta e replay (memória longa fica)
  const newConversation = () => {
    clearTimeout(timerRef.current);
    clearTimeout(demoTimerRef.current);
    setPlaying(false);
    setWalk(false);
    setDemo({ active: false, idx: -1, paused: false });
    delete convMapRef.current[user.user_key];
    persistConvMap(convMapRef.current);
    setState({ run: null, step: -1, iteration: 0, conversationId: null, turns: [] });
  };

  // Trocar de usuário = trocar de identidade: memória longa, guardrails e cache
  // passam a ser os da NOVA área; se esse usuário já conversou, retoma a sessão dele.
  const switchUser = (u) => {
    if (busy || u.user_key === user.user_key) return;
    stopDemo();
    setUser(u);
    // turns: [] é provisório — o efeito de login reidrata o transcript desse
    // usuário a partir de POC.agent_sessions (a sessão dele continua viva).
    setState({
      run: null, step: -1, iteration: 0,
      conversationId: convMapRef.current[u.user_key] ?? null, turns: [],
    });
  };

  // Navegação passo a passo. Com a demo pausada, passar do primeiro/último passo
  // salta para o script ANTERIOR/SEGUINTE do histórico da demo (replay, sem API).
  const go = (n) => {
    if (demo.active && demo.paused) {
      if (n < 0 && demo.idx > 0) return jumpDemo(demo.idx - 1, 'end');
      if (n > lastStep && demo.idx + 1 < demoHist.length) return jumpDemo(demo.idx + 1, 'start');
    }
    setPlaying(false);
    setWalk(false);
    setState((s) => ({ ...s, step: Math.max(0, Math.min(lastStep, n)) }));
  };

  // habilita Anterior/Próximo também na fronteira entre scripts da demo pausada
  const demoNav = demo.active && demo.paused;
  const canBack = run && (step > 0 || (demoNav && demo.idx > 0));
  const canForward = run && (step < lastStep || (demoNav && demo.idx + 1 < demoHist.length));

  // métricas acumuladas até o passo atual (sensação "ao vivo")
  const calls = visible.filter((e) => e.kind === 'tool_call');
  const metrics = {
    reads: calls.filter((e) => e.phase === 'retrieve').length,
    writes: calls.filter((e) => e.phase === 'act' || e.phase === 'store').length,
    tools: calls.length,
    latency: visible.reduce((a, e) => a + (e.latency_ms || 0), 0),
  };

  const ops = visible.filter((e) => e.kind === 'tool_call');
  const reasonings = visible.filter((e) => e.kind === 'reasoning');
  const usedTools = new Set(ops.map((e) => e.tool));
  const thought = [...reasonings].reverse()[0]?.text;

  return (
    <div className="stack">
      {error && (
        <Banner variant="warning" darkMode>
          {error}
        </Banner>
      )}

      <div className="card neutral agent-shell">
        {/* cabeçalho + controles */}
        <div className="agent-head">
          <div className="agent-title">
            <span className="agent-emoji">🤖</span>
            <div>
              <div className="card-title">Agente de Suporte</div>
              <div className="dim mono" style={{ fontSize: 11 }}>Powered by MongoDB MCP Server</div>
            </div>
          </div>
          <div className="agent-controls">
            <span className="dim mono" style={{ fontSize: 12 }}>
              Iteração <span className="accent-num">{iteration ?? 0}</span>
            </span>
            <span
              className="dim mono"
              style={{ fontSize: 12 }}
              title="Total acumulado de tokens estimados evitados por respostas servidas do cache semântico, sem chamar o LLM"
            >
              ⚡ <span className="accent-num">{tokensSaved.toLocaleString('pt-BR')}</span> tokens economizados
            </span>
            <Button size="xsmall" darkMode onClick={reset} disabled={!run}>Reset</Button>
            <Button size="xsmall" darkMode onClick={() => go(step - 1)} disabled={!canBack}>◀ Anterior</Button>
            <Button size="xsmall" darkMode onClick={() => go(step + 1)} disabled={!canForward}>Próximo ▶</Button>
            <Button
              size="xsmall"
              darkMode
              onClick={() => { setWalk(false); setPlaying((p) => !p); }}
              disabled={!run || step >= lastStep}
            >
              {playing ? '⏸ Pausar' : '▶ Continuar'}
            </Button>
            <Button
              size="xsmall"
              darkMode
              variant="primary"
              onClick={() => { setState((s) => ({ ...s, step: 0 })); setWalk(true); setPlaying(true); }}
              disabled={!run}
            >
              📖 Tour guiado
            </Button>
            <Button size="xsmall" darkMode onClick={() => setShowInspector((v) => !v)}>
              {showInspector ? '🔎 Ocultar dados' : '🔎 Inspecionar MongoDB'}
            </Button>
          </div>
        </div>

        {/* identidade: quem está falando decide memória (user_key) e área (persona,
            guardrails, cache). Em produção isso viria do login, não de um seletor. */}
        <div className="agent-users">
          <span className="dim mono">Usuário:</span>
          {(users.length ? users : [user]).map((u) => (
            <button
              key={u.user_key}
              className={`user-pill ${u.user_key === user.user_key ? 'active' : ''} ${demo.active ? 'demo-live' : ''}`}
              onClick={() => switchUser(u)}
              disabled={busy}
              title={`user_key: ${u.user_key} · área: ${u.area_label ?? u.area}`}
            >
              👤 {u.name}
              <span className="user-area">{u.area_label ?? u.area}</span>
            </button>
          ))}
          <span className="dim mono" style={{ fontSize: 11 }}>
            memória por usuário · guardrails e persona por área
          </span>
        </div>

        {/* ferramentas MCP disponíveis + memória da sessão */}
        <div className="agent-meta">
          <div className="tools-row">
            <span className="dim mono">Ferramentas MCP:</span>
            {tools.map((t) => (
              <span
                key={t.name}
                className={`tool-pill ${t.kind} ${usedTools.has(t.name) ? 'used' : ''}`}
                title={usedTools.has(t.name) ? 'usada neste turno' : 'disponível'}
              >
                {t.name}
              </span>
            ))}
          </div>
          <div className="memory-pill" title="POC.agent_sessions">
            🧠 <span className="accent-num">{turns.length}</span> mensagens na memória
            <Button size="xsmall" darkMode onClick={newConversation} disabled={busy}>Nova conversa</Button>
          </div>
        </div>

        {/* strip das 6 fases */}
        <div className="phase-strip">
          {PHASES.map((p, i) => (
            <div key={p.key} className="phase-wrap">
              <div className={`phase-card ${activePhase === p.key ? 'active' : ''} ${visible.some((e) => e.phase === p.key) ? 'done' : ''}`}>
                <div className="phase-icon">{p.icon}</div>
                <div className="phase-label">{p.label}</div>
                <div className="phase-sub">{p.sub}</div>
              </div>
              {i < PHASES.length - 1 && (
                <span className="phase-arrow">{i === PHASES.length - 2 ? '↩' : '→'}</span>
              )}
            </div>
          ))}
        </div>

        {walk && current && (
          <div className="walk-caption">
            <span className="mono">{PHASE_CAPTION[activePhase]}</span>
          </div>
        )}

        {/* demo automática: 10 scripts variados tocados em sequência */}
        <div className="demo-bar">
          {!demo.active ? (
            <button className="demo-btn" onClick={startDemo} disabled={busy || !playlist.length}>
              ▶ Demo automática · {playlist.length} scripts
            </button>
          ) : demo.paused ? (
            <>
              <button className="demo-btn" onClick={resumeDemo} disabled={busy}>▶ Continuar demo</button>
              <button className="demo-btn stop" onClick={stopDemo}>⏹ Encerrar demo</button>
            </>
          ) : (
            <button className="demo-btn pause" onClick={pauseDemo}>⏸ Pausar demo</button>
          )}
          {demo.active && (
            <span className="demo-now mono">
              {demo.idx + 1}/{playlist.length} · {playlist[demo.idx]?.label ?? ''}
              {playlist[demo.idx]?.user_key && (
                <span className="demo-user"> · 👤 {users.find((u) => u.user_key === playlist[demo.idx].user_key)?.name ?? playlist[demo.idx].user_key}</span>
              )}
              {demo.paused && <span className="demo-paused"> · pausado — ◀/▶ revisitam os scripts anteriores</span>}
            </span>
          )}
          <div className="demo-track">
            {playlist.map((it, i) => (
              <span
                key={it.key}
                className={`demo-dot badge-${it.badge} ${demo.active && i < demo.idx ? 'done' : ''} ${demo.active && i === demo.idx ? 'now' : ''}`}
                title={`${i + 1}. ${it.label}`}
              />
            ))}
          </div>
        </div>

        {/* flags das features de inteligência (cache / guardrails / memória) */}
        {run && <FeatureFlags run={run} />}

        {/* cadeia de trocas: só aparece quando a travessia de grafo rodou neste turno */}
        <ReplacementChain events={visible} />

        {/* inspetor das collections do MongoDB */}
        {showInspector && <MongoInspector userKey={user.user_key} area={user.area} conversationId={conversationId} run={run} />}

        {/* 3 colunas */}
        <div className="agent-grid">
          <div className="agent-col">
            <div className="col-head"><span className="leaf">🍃</span> MongoDB</div>
            {ops.length === 0 && <div className="dim">Clique numa fase ou envie uma mensagem para começar…</div>}
            {ops.map((e, i) => (
              <div key={i} className={`op-item ${e.phase === 'act' || e.phase === 'store' ? 'write' : 'read'} ${current === e ? 'pulse' : ''}`}>
                <div className="op-head">
                  <Badge variant={e.phase === 'act' || e.phase === 'store' ? 'yellow' : 'green'}>{opLabel(e)}</Badge>
                  <span className="mono dim">{opTarget(e.args)}</span>
                  {e.latency_ms != null && <span className="mono dim">{e.latency_ms} ms</span>}
                </div>
                {opDetail(e.args) && <div className="op-detail mono">{opDetail(e.args)}</div>}
                <div className="op-result mono">{short(e.result, 180)}</div>
                <QueryDetails
                  operation={opLabel(e)}
                  namespace={opTarget(e.args)}
                  query={e.args}
                  explain={e.explain}
                  label="Ver chamada MCP / query executada"
                />
              </div>
            ))}
          </div>

          <div className="agent-col">
            <div className="col-head"><span className="brain">🧠</span> Raciocínio do LLM
              {run && <Badge variant="blue" className="ml">{run.model}</Badge>}
              {run?.langfuse_trace_url && (
                <a href={run.langfuse_trace_url} target="_blank" rel="noreferrer"
                   className="ml langfuse-link" title="Abrir trace completo (tokens, custo, latência por passo) no Langfuse">
                  🔭 Ver trace no Langfuse ↗
                </a>
              )}
            </div>
            {run && <MongoCacheSavings run={run} />}
            {reasonings.length === 0 && <div className="dim">O raciocínio aparece na fase "Raciocinar".</div>}
            {reasonings.map((e, i) => (
              <div key={i} className={`reason-item ${current === e ? 'pulse' : ''}`}>
                <div className="reason-text">{e.text}</div>
                <div className="reason-meta mono dim">{e.tokens} tokens · {e.latency_ms} ms</div>
              </div>
            ))}
          </div>

          <div className="agent-col">
            <div className="col-head"><span className="agent-emoji">🤖</span> Agente</div>
            <div className="thought-box">
              <div className="thought-label mono">💭 Pensamento atual</div>
              <div className="thought-text">{thought ?? 'Aguardando entrada do usuário…'}</div>
            </div>
            <div className="agent-chat">
              {turns.length === 0 && <div className="dim">Sem mensagens ainda. Experimente uma sugestão abaixo.</div>}
              {turns.map((t, i) => (
                <div key={i} className={`chat-msg ${t.role === 'user' ? 'user' : 'assistant'}`}>{t.text}</div>
              ))}
              {busy && (
                <div className="row"><div className="spinner" /> <span className="dim">{liveStatus ?? 'o agente está trabalhando…'}</span></div>
              )}
            </div>
            <div className="row" style={{ marginTop: 10 }}>
              <div style={{ flex: 1 }}>
                <TextInput
                  darkMode
                  label="Mensagem para o agente"
                  placeholder="Digite uma mensagem ou clique num cenário"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && send()}
                />
              </div>
              <Button darkMode variant="primary" onClick={send} disabled={busy}>
                {busy ? 'Rodando…' : 'Enviar'}
              </Button>
            </div>
            <div className="chip-row">
              {scenarios.map((s) => (
                <button key={s.key} className="agent-chip" onClick={() => runScenario({ message: s.message })} disabled={busy}>
                  {s.label}
                </button>
              ))}
              <button
                className="agent-chip memory"
                onClick={() => runScenario({ message: 'Pode consolidar todas as perguntas que eu fiz nesta sessão?' })}
                disabled={busy || turns.length === 0}
                title="Demonstra a persistência: o agente faz um find em agent_sessions"
              >
                🧠 Consolidar minhas perguntas
              </button>
            </div>
          </div>
        </div>

        {/* métricas */}
        <div className="metric-bar">
          <div className="metric"><div className="metric-val">{metrics.reads}</div><div className="metric-label">reads</div></div>
          <div className="metric"><div className="metric-val">{metrics.writes}</div><div className="metric-label">writes</div></div>
          <div className="metric"><div className="metric-val">{metrics.tools}</div><div className="metric-label">operações</div></div>
          <div className="metric"><div className="metric-val">{metrics.latency}<span className="metric-unit">ms</span></div><div className="metric-label">latência</div></div>
        </div>
      </div>
    </div>
  );
}

// ---- Flags das features: o que cada camada de MongoDB fez neste turno ----
function FeatureFlags({ run }) {
  const cache = run.cache ?? {};
  const gIn = run.guardrail?.input ?? {};
  const gOut = run.guardrail?.output ?? {};
  const mem = run.memory ?? {};
  const prof = run.profile ?? {};

  // Guardrail
  const blocked = gIn.allowed === false;
  const masked = gOut?.masked;
  let guardClass = 'ok';
  let guardTitle = '🛡️ Guardrails · aprovado';
  let guardDetail = `Entrada e saída dentro das políticas da área ${run.profile?.label ?? 'padrão'} (ai_brain.guardrail_policies).`;
  if (blocked) {
    guardClass = 'block';
    guardTitle = '🛡️ Guardrails · BLOQUEADO';
    const v = gIn.violations?.[0];
    guardDetail = v ? `${v.rule}: ${v.detail}` : 'Pedido barrado pela política.';
  } else if (masked) {
    guardClass = 'warn';
    guardTitle = '🛡️ Guardrails · PII mascarada';
    guardDetail = 'Dado sensível removido da resposta antes de enviar ao cliente.';
  } else if (gIn.violations?.length) {
    guardClass = 'warn';
    guardTitle = '🛡️ Guardrails · alerta';
    guardDetail = `${gIn.violations.length} ocorrência(s) registrada(s) em guardrail_events.`;
  }

  // Cache
  let cacheClass = 'miss';
  let cacheTitle = cache.mode === 'bypass' ? '⚡ Cache semântico · IGNORADO' : '⚡ Cache semântico · MISS';
  let cacheDetail = cache.blocked
    ? 'Não consultado (pedido bloqueado).'
    : cache.mode === 'bypass'
    ? 'Ignorado: turno pessoal (depende da memória do usuário) — nunca usa nem grava o cache compartilhado.'
    : `Melhor score ${cache.score ?? 0} < ${cache.threshold ?? 0.92}. ${cache.stored ? 'Resposta gravada no cache.' : ''}`;
  if (cache.hit) {
    cacheClass = 'hit';
    cacheTitle = '⚡ Cache semântico · HIT';
    cacheDetail = `Servido do MongoDB sem LLM · score ${cache.score} ≥ ${cache.threshold} · ${cache.latency_ms} ms · entrada ${cache.matched_area ? `da área ${cache.matched_area}` : 'global (FAQ)'} · ~${cache.tokens_economizados ?? 0} tokens economizados`;
  }

  // Memória
  const stFacts = mem.longterm?.facts?.length ?? 0;
  const newFacts = mem.new_facts?.length ?? 0;
  const superseded = mem.superseded?.length ?? 0;
  const memTitle = '🧠 Memória · curto + longo prazo';
  const m = run.metrics ?? {};
  const cacheTokens = m.cache_read_input_tokens ?? 0;
  const extractor = m.memory_extraction_skipped
    ? ' · extração LP evitada'
    : ` · extrator ${m.memory_extractor_input_tokens ?? 0}→${m.memory_extractor_output_tokens ?? 0}`;
  const memDetail = `Curto: agent_sessions (${run.turn_count} msgs). Longo: agent_memory (${stFacts} fatos${newFacts ? `, +${newFacts} novo(s)` : ''}${superseded ? `, ${superseded} substituído(s)${mem.transaction ? ' em transação ACID' : ''}` : ''}). LLM: ${m.input_tokens ?? 0}→${m.output_tokens ?? 0} tokens${extractor}${cacheTokens ? ` · ${cacheTokens} reutilizados do prompt cache` : ''}.`;

  return (
    <div className="feat-flags">
      <div className="feat-card identity">
        <div className="feat-title">👥 Identidade & Área · {prof.label ?? 'padrão'}</div>
        <div className="feat-detail mono">
          {`${prof.user_name ?? prof.user_key ?? '—'} (user_key: ${prof.user_key ?? '—'}) · persona, guardrails e cache desta área · memória isolada por usuário`}
        </div>
      </div>
      <div className={`feat-card cache-${cacheClass}`}>
        <div className="feat-title">{cacheTitle}</div>
        <div className="feat-detail mono">{cacheDetail}</div>
      </div>
      <div className={`feat-card guard-${guardClass}`}>
        <div className="feat-title">{guardTitle}</div>
        <div className="feat-detail mono">{guardDetail}</div>
      </div>
      <div className="feat-card mem">
        <div className="feat-title">{memTitle}</div>
        <div className="feat-detail mono">{memDetail}</div>
      </div>
    </div>
  );
}

// ---- Inspetor: lê as collections reais do MongoDB para mostrar ao cliente ----
const INSP_TABS = [
  { key: 'cache', label: 'POC.semantic_cache' },
  { key: 'short', label: 'Memória curta · agent_sessions' },
  { key: 'memory', label: 'Memória longa · agent_memory' },
  { key: 'rules', label: 'Guardrails · regras' },
  { key: 'events', label: 'Guardrails · log' },
];

function MongoInspector({ userKey, area, conversationId, run }) {
  const [tab, setTab] = useState('cache');
  const [data, setData] = useState({});
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const load = async () => {
    setLoading(true);
    setErr(null);
    try {
      if (tab === 'cache') setData({ cache: await api.cacheInspect() });
      else if (tab === 'short')
        setData({ short: conversationId ? await api.memoryShort(conversationId, userKey) : { turns: [] } });
      else if (tab === 'memory') setData({ memory: await api.memoryInspect(userKey) });
      else if (tab === 'rules') setData({ rules: await api.guardrailsRules(area) });
      else setData({ events: await api.guardrailsEvents(userKey) });
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  };

  // recarrega ao trocar de aba, de usuário/área ou após cada turno do agente
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [tab, run, userKey, area]);

  const clearCache = async () => { await api.cacheClear(); load(); };
  const clearMemory = async () => { await api.memoryClear(userKey); load(); };

  const policy = data.rules?.policy ?? {};

  return (
    <div className="inspector">
      <div className="inspector-tabs">
        {INSP_TABS.map((t) => (
          <button
            key={t.key}
            className={`insp-tab ${tab === t.key ? 'active' : ''}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
        <span className="insp-spacer" />
        <button className="insp-mini" onClick={load} disabled={loading}>↻ Atualizar</button>
        {tab === 'cache' && <button className="insp-mini danger" onClick={clearCache}>Limpar cache</button>}
        {tab === 'memory' && <button className="insp-mini danger" onClick={clearMemory}>Esquecer cliente</button>}
      </div>

      {err && <div className="dim" style={{ padding: 8 }}>⚠ {err}</div>}
      {loading && <div className="dim" style={{ padding: 8 }}>carregando…</div>}

      {!loading && tab === 'cache' && (
        <div className="insp-body">
          <div className="dim mono insp-head">
            Índice {data.cache?.index} · HIT quando score ≥ {data.cache?.threshold}
          </div>
          {(data.cache?.entries ?? []).length === 0 && <div className="dim">Cache vazio.</div>}
          {(data.cache?.entries ?? []).map((e) => (
            <div key={e._id} className="insp-row">
              <div className="insp-q mono">Q: {e.question}</div>
              <div className="insp-a">A: {short(e.answer, 160)}</div>
              <div className="insp-meta mono dim">
                hits: {e.hits} · {e.model} · área: {e.area ?? 'global'} ·{' '}
                {e.expires_at ? `expira ${short(e.expires_at, 19)} (TTL)` : 'FAQ — sem expiração'}
              </div>
            </div>
          ))}
        </div>
      )}

      {!loading && tab === 'short' && (
        <div className="insp-body">
          <div className="dim mono insp-head">
            session_id: {data.short?.session_id ?? conversationId ?? '—'} · turnos desta conversa
            {data.short?.ttl_expires_at && ` · expira ${short(data.short.ttl_expires_at, 19)} sem novo turno (TTL ${Math.round((data.short.ttl_idle_seconds ?? 0) / 60)} min)`}
          </div>
          {(data.short?.turns ?? []).length === 0 && (
            <div className="dim">Sem turnos ainda nesta conversa. "Nova conversa" zera esta memória.</div>
          )}
          {(data.short?.turns ?? []).map((t, i) => (
            <div key={i} className={`insp-row short-${t.role}`}>
              <div className="insp-q">{t.role === 'user' ? '👤' : '🤖'} {short(t.content, 200)}</div>
              <div className="insp-meta mono dim">{t.role} · {t.at}</div>
            </div>
          ))}
        </div>
      )}

      {!loading && tab === 'memory' && (
        <div className="insp-body">
          <div className="dim mono insp-head">
            user_key: {data.memory?.user_key} · fatos consolidados entre sessões (persistem em "Nova conversa")
          </div>
          {(data.memory?.facts ?? []).length === 0 && (
            <div className="dim">Nada memorizado ainda. Diga seu nome ou uma preferência ao agente.</div>
          )}
          {(data.memory?.facts ?? []).map((f, i) => (
            <div key={i} className="insp-row">
              <div className="insp-q">🔖 {f.fact}</div>
              <div className="insp-meta mono dim">{f.category} · {f.at}</div>
            </div>
          ))}
          {(data.memory?.history ?? []).length > 0 && (
            <div className="dim mono insp-head" style={{ marginTop: 8 }}>
              Fatos supersedidos (active: false — trilha de auditoria, nada é apagado)
            </div>
          )}
          {(data.memory?.history ?? []).map((f, i) => (
            <div key={`h${i}`} className="insp-row fact-old">
              <div className="insp-q"><s>{f.fact}</s></div>
              <div className="insp-meta mono dim">{f.category} · substituído · superseded_by: {f.superseded_by ?? '—'}</div>
            </div>
          ))}
        </div>
      )}

      {!loading && tab === 'rules' && (
        <div className="insp-body">
          <div className="dim mono insp-head">
            Guardrails da área "{data.rules?.area ?? 'default'}" · política em {data.rules?.policy_collection}, denylist em {data.rules?.denylist_collection}
          </div>
          <div className="insp-row">
            <div className="insp-q">🚫 Denylist semântica (bloqueio por intenção via $vectorSearch)</div>
            {(data.rules?.denylist ?? []).map((d) => (
              <div key={d._id} className="insp-meta mono dim">
                • "{d.phrase}" — {d.category}{d.area ? ` · só área ${d.area}` : ' · global'}
              </div>
            ))}
          </div>
          <div className="insp-row">
            <div className="insp-q">🔒 PII mascarada na saída</div>
            {(policy.pii_patterns ?? []).map((p, i) => (
              <div key={i} className="insp-meta mono dim">• {p.name} → {p.mask}</div>
            ))}
          </div>
          <div className="insp-row">
            <div className="insp-q">⛔ Termos banidos</div>
            {(policy.banned_terms ?? []).map((p, i) => (
              <div key={i} className="insp-meta mono dim">• {p.name}: {p.pattern}</div>
            ))}
            {(policy.banned_terms ?? []).length === 0 && <div className="insp-meta mono dim">—</div>}
          </div>
          <div className="insp-meta mono dim" style={{ padding: '4px 10px' }}>
            Limiar da denylist: {policy.denylist_threshold} · bloqueio → "{short(policy.block_message, 80)}"
          </div>
        </div>
      )}

      {!loading && tab === 'events' && (
        <div className="insp-body">
          <div className="dim mono insp-head">Log de auditoria dos guardrails (mais recentes primeiro)</div>
          {(data.events?.events ?? []).length === 0 && <div className="dim">Nenhum evento registrado.</div>}
          {(data.events?.events ?? []).map((e) => (
            <div key={e._id} className={`insp-row evt-${e.action}`}>
              <div className="insp-q mono">
                [{e.stage}] {e.action.toUpperCase()} — {short(e.text_sample, 90)}
              </div>
              {(e.violations ?? []).length > 0 && (
                <div className="insp-meta mono dim">
                  {e.violations.map((v) => `${v.rule}(${v.kind})`).join(', ')}
                </div>
              )}
              <div className="insp-meta mono dim">{e.at}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
