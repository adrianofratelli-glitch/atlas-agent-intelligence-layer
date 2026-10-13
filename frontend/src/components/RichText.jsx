import { parseInline } from '../richText.js';

/** Resposta do LLM com **negrito** e links renderizados (ver richText.js). */
export default function RichText({ text }) {
  return parseInline(text).map((seg, i) => {
    if (seg.type === 'bold') return <strong key={i}>{seg.text}</strong>;
    if (seg.type === 'link') {
      return <a key={i} href={seg.href} target="_blank" rel="noopener noreferrer">{seg.text}</a>;
    }
    return <span key={i}>{seg.text}</span>;
  });
}
