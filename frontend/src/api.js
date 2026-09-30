// O prazo cobre headers e corpo; nenhuma escrita é reenviada automaticamente.
async function boundedRequest(work, timeoutMs = 30000) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try { return await work(controller.signal) }
  finally { clearTimeout(timer) }
}

// HTTP client. Backend errors arrive as {error: {kind, message}} (503) and
// become ApiError — the UI shows them in a yellow Banner, never a stack trace.

export class ApiError extends Error {
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

// JWT da identidade demo ativa: obtido em /api/auth/token quando o usuário é
// trocado no switcher; enviado em toda request. O backend resolve a identidade
// do token (claim sub), não do payload.
let authToken = null;
export function setAuthToken(token) {
  authToken = token;
}

async function request(path, options = {}) {
  return boundedRequest(async (signal) => {
    let res;
    try {
      res = await fetch(path, {
        headers: {
          'Content-Type': 'application/json',
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        ...options,
        signal,
      });
    } catch {
      throw new ApiError('rede', 'Backend não respondeu. O FastAPI está rodando na porta 8010?');
    }
    const body = await res.json().catch(() => {
      if (res.ok) throw new Error('Resposta incompleta ou inválida do backend. Tente novamente.')
      return {}
    });
    if (!res.ok) {
      const err = body.error || {};
      throw new ApiError(err.kind || 'erro', err.message || body.detail || `Erro HTTP ${res.status}`);
    }
    return body;
  }, path.includes('/agent/run') || path.includes('/chat/') ? 300000 : 30000)
}

export const api = {
  health: () => request('/api/health'),
  metrics: () => request('/api/metrics'),

  // Auth — o switcher de identidade é o "login" da demo
  login: async (userKey) => {
    const tok = await request('/api/auth/token', {
      method: 'POST',
      body: JSON.stringify({ user_key: userKey }),
    });
    setAuthToken(tok.access_token);
    return tok;
  },

  // Tab 1
  listTemplates: () => request('/api/templates'),
  getTemplate: (id) => request(`/api/templates/${id}`),
  addVariant: (id, modelName) =>
    request(`/api/templates/${id}/variant`, {
      method: 'POST',
      body: JSON.stringify({ model_name: modelName }),
    }),
  removeVariant: (id, modelName) =>
    request(`/api/templates/${id}/variant/${modelName}`, { method: 'DELETE' }),

  // Tab 2
  getModelConfig: () => request('/api/model-config'),
  swapModels: () => request('/api/model-config/swap', { method: 'POST' }),
  quickChat: (question, history = []) =>
    request('/api/chat/quick', { method: 'POST', body: JSON.stringify({ question, history }) }),

  // Tab 3 — Agent (autonomous loop via MongoDB MCP Server)
  users: () => request('/api/users'),
  agentScenarios: (userKey) =>
    request(`/api/agent/scenarios${userKey ? `?user_key=${encodeURIComponent(userKey)}` : ''}`),
  agentPlaylist: () => request('/api/agent/playlist'),
  agentTools: () => request('/api/agent/tools'),
  agentRun: (body) =>
    request('/api/agent/run', { method: 'POST', body: JSON.stringify(body) }),
  // Streaming (SSE) do mesmo turno: `onTrace(event)` é chamado incrementalmente
  // a cada passo (Perceive/Retrieve/Reason/Act/Store/Loop) conforme o backend
  // os gera, em vez de só no final. Resolve com o `result` final (mesmo shape
  // de agentRun) quando o turno termina.
  agentRunStream: async (body, onTrace) => {
    let res;
    try {
      res = await fetch('/api/agent/run/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        body: JSON.stringify(body),
      });
    } catch {
      throw new ApiError('rede', 'Backend não respondeu. O FastAPI está rodando na porta 8010?');
    }
    if (!res.ok || !res.body) {
      const errBody = await res.json().catch(() => {
    if (res.ok) throw new Error('Resposta incompleta ou inválida do backend. Tente novamente.')
    return {}
  });
      const err = errBody.error || {};
      throw new ApiError(err.kind || 'erro', err.message || errBody.detail || `Erro HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    try {
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, '\n');
      // Eventos SSE separados por linha em branco; cada bloco tem "event: x\ndata: {...}"
      let sep;
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const eventLine = block.split('\n').find((l) => l.startsWith('event: '));
        const dataLine = block.split('\n').find((l) => l.startsWith('data: '));
        if (!eventLine || !dataLine) continue;
        const eventName = eventLine.slice('event: '.length);
        const data = JSON.parse(dataLine.slice('data: '.length));
        if (eventName === 'trace') {
          onTrace?.(data);
        } else if (eventName === 'result') {
          return data;
        } else if (eventName === 'error') {
          throw new ApiError(data.kind || 'erro', data.message || 'Falha ao executar o agente.');
        }
      }
    }
    throw new ApiError('rede', 'Conexão de streaming encerrada sem resultado.');
    } finally {
      reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  },

  // Intelligence features — cache, memory, guardrails (inspect / reset)
  cacheInspect: () => request('/api/cache'),
  cacheClear: () => request('/api/cache', { method: 'DELETE' }),
  memoryInspect: (userKey) => request(`/api/memory/${encodeURIComponent(userKey)}`),
  memoryClear: (userKey) =>
    request(`/api/memory/${encodeURIComponent(userKey)}`, { method: 'DELETE' }),
  memoryShort: (conversationId, userKey) =>
    request(`/api/memory-short/${encodeURIComponent(conversationId)}?user_key=${encodeURIComponent(userKey)}`),
  guardrailsPolicy: () => request('/api/guardrails/policy'),
  guardrailsRules: (area) =>
    request(`/api/guardrails/rules${area ? `?area=${encodeURIComponent(area)}` : ''}`),
  guardrailsEvents: (userKey) =>
    request(`/api/guardrails/events${userKey ? `?user_key=${encodeURIComponent(userKey)}` : ''}`),
};
