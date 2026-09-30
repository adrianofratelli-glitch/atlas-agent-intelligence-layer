import { useEffect, useState } from 'react';
import Badge from '@leafygreen-ui/badge';
import Banner from '@leafygreen-ui/banner';
import Button from '@leafygreen-ui/button';
import JsonViewer from '../components/JsonViewer.jsx';
import { api } from '../api.js';

const TEMPLATE_ID = 'tmpl_product_assistant_v2';
const NEW_MODEL = 'gemini-3-pro';

const POSTGRES_DDL = `-- Modelar prompt_templates com variantes por modelo
-- exige normalizar... e prever TODAS as colunas futuras:

CREATE TABLE prompt_templates (
  id            VARCHAR(64) PRIMARY KEY,
  name          VARCHAR(128) NOT NULL,
  version       INT NOT NULL,
  tags          TEXT[]
);

CREATE TABLE prompt_variants (
  id            SERIAL PRIMARY KEY,
  template_id   VARCHAR(64) REFERENCES prompt_templates(id),
  model_name    VARCHAR(64) NOT NULL,
  system_prompt TEXT,
  user_template TEXT,
  -- campos que SÓ ALGUMAS variantes têm → colunas nullable:
  few_shot_examples JSONB NULL,
  output_format     VARCHAR(32) NULL,
  max_products      INT NULL
);

-- Toda leitura vira JOIN:
SELECT t.*, v.*
  FROM prompt_templates t
  JOIN prompt_variants v ON v.template_id = t.id
 WHERE t.name = 'product_assistant'
   AND v.model_name = 'claude-sonnet-4-5';`;

const POSTGRES_MIGRATION = `-- Saiu um modelo novo com estrutura diferente?
-- (ex.: gemini-3-pro precisa de 'safety_settings')

ALTER TABLE prompt_variants
  ADD COLUMN safety_settings JSONB NULL;   -- migration

-- O ALTER em si é rápido no PG moderno.
-- O custo real é o PROCESSO em volta dele:
-- + escrever e revisar a migração
-- + staging + janela de deploy
-- + backfill quando há DEFAULT / NOT NULL
-- + sincronizar o vector DB separado
-- + invalidar caches da aplicação`;

const CONCERNS = [
  {
    concern: 'Novo modelo de LLM',
    pg: 'ALTER TABLE + migration + deploy',
    mdb: '$set de uma nova chave em variants',
  },
  {
    concern: 'A/B test de prompt',
    pg: 'Tabela de versões + JOINs condicionais',
    mdb: 'Outro documento com version: 3',
  },
  {
    concern: 'Turnos variáveis por sessão',
    pg: 'Tabela de turns + JOIN por sessão',
    mdb: 'Array turns[] dentro do documento',
  },
  {
    concern: 'Swap de provider (OpenAI → Anthropic)',
    pg: 'Migration de schema + backfill',
    mdb: 'update_one no model_config',
  },
];

// Race: the same "new model shipped to production" in both worlds
const RACE_MDB = [
  'update_one: $set da variante nova',
  'Backend lê o doc no próximo request',
  'autoEmbed re-vetoriza o que mudou',
  'Em produção',
];
const RACE_ALT = [
  'Escrever migration (ALTER TABLE)',
  'Code review da migration',
  'Rodar em staging + smoke test',
  'Janela de deploy em produção',
  'Backfill das colunas novas',
  'Sincronizar vector DB separado',
  'Invalidar caches / reiniciar serviços',
  'Em produção',
];

function FlowRace() {
  const [mdbState, setMdbState] = useState([]); // 'active' | 'done' per index
  const [altState, setAltState] = useState([]);
  const [running, setRunning] = useState(false);
  const [doneSides, setDoneSides] = useState({ mdb: false, alt: false });

  const run = () => {
    if (running) return;
    setRunning(true);
    setMdbState([]);
    setAltState([]);
    setDoneSides({ mdb: false, alt: false });

    const animate = (steps, setFn, stepMs, onDone) => {
      steps.forEach((_, i) => {
        setTimeout(() => setFn((s) => { const n = [...s]; n[i] = 'active'; return n; }), i * stepMs + 100);
        setTimeout(() => setFn((s) => { const n = [...s]; n[i] = 'done'; return n; }), (i + 1) * stepMs);
      });
      setTimeout(onDone, steps.length * stepMs + 200);
    };

    animate(RACE_MDB, setMdbState, 450, () => setDoneSides((d) => ({ ...d, mdb: true })));
    animate(RACE_ALT, setAltState, 900, () => {
      setDoneSides((d) => ({ ...d, alt: true }));
      setRunning(false);
    });
  };

  return (
    <div className="card neutral">
      <div className="card-header">
        <span className="card-title">Saiu um modelo novo de LLM — o caminho até produção</span>
        <Button darkMode variant="primary" size="small" onClick={run} disabled={running}>
          {running ? 'Rodando…' : '▶ Rodar comparação'}
        </Button>
      </div>
      <div className="flow-grid">
        <div>
          <div className="row" style={{ marginBottom: 10 }}>
            <Badge variant="green">MongoDB — uma plataforma</Badge>
          </div>
          <div className="flow-path">
            {RACE_MDB.map((label, i) => (
              <div key={label} className={`flow-node ${mdbState[i] === 'active' ? 'active-mdb' : ''} ${mdbState[i] === 'done' ? 'done-mdb' : ''}`}>
                <span>{label}</span>
                <span className="flow-check">✓</span>
              </div>
            ))}
          </div>
          <div className={`flow-total mdb ${doneSides.mdb ? 'show' : ''}`}>
            ✓ minutos — operação de dado
          </div>
        </div>
        <div>
          <div className="row" style={{ marginBottom: 10 }}>
            <Badge variant="lightgray">Stack costurada — relacional + vector DB</Badge>
          </div>
          <div className="flow-path">
            {RACE_ALT.map((label, i) => (
              <div key={label} className={`flow-node ${altState[i] === 'active' ? 'active-alt' : ''} ${altState[i] === 'done' ? 'done-alt' : ''}`}>
                <span>{label}</span>
                <span className="flow-check">✓</span>
              </div>
            ))}
          </div>
          <div className={`flow-total alt ${doneSides.alt ? 'show' : ''}`}>
            dias/semanas — não pela técnica, pelo processo (review, staging, janela)
          </div>
        </div>
      </div>
    </div>
  );
}

export default function FlexibleSchema({ state, setState }) {
  const { doc, flash } = state;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const load = async (withFlash = false) => {
    try {
      const d = await api.getTemplate(TEMPLATE_ID);
      setState((s) => ({ doc: d, flash: withFlash ? s.flash + 1 : s.flash }));
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => {
    if (!doc) load();
  }, []);

  const hasNewVariant = !!doc?.variants?.[NEW_MODEL];

  const toggleVariant = async () => {
    setBusy(true);
    setError(null);
    try {
      const d = hasNewVariant
        ? await api.removeVariant(TEMPLATE_ID, NEW_MODEL)
        : await api.addVariant(TEMPLATE_ID, NEW_MODEL);
      setState((s) => ({ doc: d, flash: s.flash + 1 }));
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

      <div className="card schema-stage">
          <div className="card-header">
            <span className="card-title">Documento ao vivo no Atlas</span>
            <span style={{ whiteSpace: 'nowrap' }}><Badge variant="green">1 update, zero migration</Badge></span>
          </div>
          <div className="row" style={{ marginBottom: 12 }}>
            <Button
              darkMode
              variant="primary"
              onClick={toggleVariant}
              disabled={busy || !doc}
            >
              {busy
                ? 'Aplicando $set…'
                : hasNewVariant
                  ? `Remover variante ${NEW_MODEL} ($unset)`
                  : `Adicionar variante de modelo (${NEW_MODEL})`}
            </Button>
          </div>
          {doc ? (
            <JsonViewer doc={doc} flashKey={flash} />
          ) : (
            <div className="dim">carregando documento…</div>
          )}
      </div>

      <details className="card secondary-comparison">
        <summary>Contraste relacional</summary>
        <p className="dim">No modelo relacional, uma variante estrutural nova exige migration e coordenação de deploy. Aqui, a mudança é o próprio <code>$set</code> demonstrado acima.</p>
      </details>
    </div>
  );
}
