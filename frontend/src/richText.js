// Markdown mínimo das respostas do LLM: **negrito** e URL clicável. Sem innerHTML —
// a saída são segmentos que o React renderiza como texto, então nada no texto do modelo
// (ou de uma página web citada) vira HTML executável.
const URL_RE = /(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

export function parseInline(text) {
  const out = [];
  String(text ?? '').split(/(\*\*[^*\n]+\*\*)/g).forEach((chunk) => {
    if (!chunk) return;
    if (chunk.startsWith('**') && chunk.endsWith('**') && chunk.length > 4) {
      out.push({ type: 'bold', text: chunk.slice(2, -2) });
      return;
    }
    let last = 0;
    for (const m of chunk.matchAll(URL_RE)) {
      if (m.index > last) out.push({ type: 'text', text: chunk.slice(last, m.index) });
      out.push({ type: 'link', text: m[0], href: m[0] });
      last = m.index + m[0].length;
    }
    if (last < chunk.length) out.push({ type: 'text', text: chunk.slice(last) });
  });
  return out;
}
