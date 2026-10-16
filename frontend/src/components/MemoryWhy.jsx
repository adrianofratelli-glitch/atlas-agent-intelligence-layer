import React from 'react';
import { summarize } from '../memoryExplain.js';

// Inspector "por que este fato foi lembrado": estratégia que rodou, rerank (Public Preview)
// e, por fato, a posição e a contribuição de cada perna da fusão (scoreDetails).
export default function MemoryWhy({ block }) {
  const s = summarize(block);
  if (!s) return null;
  return (
    <details className="memory-why">
      <summary>
        <span>🔎 Por que estes fatos foram lembrados</span>
        <span className={`why-strategy ${s.fallback ? 'fallback' : ''}`}>{s.label}</span>
        {s.searchMs != null && <span className="mono dim">{Math.round(s.searchMs)} ms</span>}
      </summary>
      {s.rerank && (
        <div className="why-rerank mono">
          $rerank {s.rerank.model} · {s.rerank.status === 'ran' ? 'reordenou o resultado'
            : s.rerank.status === 'unavailable' ? 'indisponível, ordem da fusão mantida' : 'desligado'}
          {s.rerank.preview && <span className="why-preview">Public Preview</span>}
        </div>
      )}
      {s.fallbacks.length > 0 && <div className="why-fallbacks mono dim">{s.fallbacks.join(' · ')}</div>}
      <ol className="why-facts">
        {s.facts.map((f) => (
          <li key={f.id}>
            <div className="why-fact-text">{f.text}</div>
            {f.why.available ? (
              <div className="why-legs">
                {f.why.legs.map((l) => (
                  <div key={l.name} className="why-leg" title={`peso ${l.weight ?? 1} · contribuição ${l.contribution.toFixed(4)}`}>
                    <span className="why-leg-name">{l.label}</span>
                    <span className="why-bar"><span style={{ transform: `scaleX(${l.share})` }} /></span>
                    <span className="mono dim">{l.rank ? `#${l.rank}` : 'fora'}</span>
                  </div>
                ))}
                {f.rerankScore != null && <span className="mono dim">rerank {f.rerankScore.toFixed(2)}</span>}
              </div>
            ) : (
              <div className="mono dim">detalhe indisponível</div>
            )}
          </li>
        ))}
      </ol>
    </details>
  );
}
