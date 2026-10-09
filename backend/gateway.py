"""Provider routing and auditable, blended cost estimates (never invoice tariffs)."""
import contextvars
import functools
import json
import math
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from anthropic import AsyncAnthropic, APIConnectionError, APIStatusError
from anthropic.types import Message

_calls = contextvars.ContextVar('llm_calls', default=None)
# USD por 1M tokens (input+output+cache): média OBSERVADA na Grove, tela
# "Usage & Spend" (2026-10-01). Estimativa de demo, não tarifa de fatura.
DEFAULT_RATES = {
    'claude-sonnet-5': 3.13, 'claude-sonnet-4-6': 4.40,
    'claude-sonnet-4-5': 1.79, 'claude-sonnet-5-5': 7.71, 'claude-opus-4-5': 17.67,
    'claude-opus-4-8': 15.00, 'claude-opus-5': 11.38, 'claude-opus-5-5': 17.33,
    'gpt-4.1': 1.16, 'gpt-4.1-mini': 0.69, 'gpt-4o': 4.32, 'gpt-4o-mini': 0.28,
    'gpt-5': 6.60, 'gpt-5-mini': 1.32, 'gpt-5.1': 4.49, 'gpt-5.2': 5.83,
    'gpt-5.4': 5.15, 'gpt-5.4-mini': 1.34, 'gpt-5.4-nano': 0.58,
    'gpt-5.6-luna': 0.30, 'gpt-6-astra': 25.72, 'deepseek-v3.2': 0.97,
    'Llama-4-Maverick-17B-128E-Instruct-FP8': 0.74, 'grok-4.3': 1.26,
}


# Tarifa de LISTA opcional (USD por 1M tokens, input/output) por modelo, via
# LLM_LIST_PRICES (JSON {"modelo": [input, output]}). Quando existe para um
# modelo, vence a média observada. Modelos fora da tabela seguem na blended.
# Vazio por padrão: a média observada (DEFAULT_RATES) manda. Opt-in via LLM_LIST_PRICES.
DEFAULT_LIST_PRICES: dict = {}


def list_prices():
    # O override MESCLA com os padrões: acrescentar um modelo no .env não pode
    # apagar a tarifa dos demais.
    values = {**DEFAULT_LIST_PRICES, **(json.loads(os.getenv('LLM_LIST_PRICES', 'null')) or {})}
    for k, v in values.items():
        if (not isinstance(v, (list, tuple)) or len(v) != 2 or any(
                isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0
                for x in v)):
            raise ValueError(f'LLM_LIST_PRICES[{k!r}] requires [input, output] finite nonnegative')
    return {k: (float(v[0]), float(v[1])) for k, v in values.items()}


def rates():
    # O env MESCLA com os padrões: um LLM_BLENDED_PRICES antigo/parcial não apaga
    # a tarifa dos demais modelos.
    values = {**DEFAULT_RATES, **json.loads(os.getenv('LLM_BLENDED_PRICES', '{}'))}
    if not isinstance(values, dict) or any(isinstance(v, bool) or not isinstance(v, (int, float))
            or not math.isfinite(v) or v < 0 for v in values.values()):
        raise ValueError('LLM_BLENDED_PRICES requires finite nonnegative rates')
    return values


def economics(calls):
    known = [c['estimated_cost_usd'] for c in calls if c.get('estimated_cost_usd') is not None]
    complete = len(known) == len(calls)
    return {'estimated_cost_usd': round(sum(known), 8) if complete else None,
            'known_cost_usd': round(sum(known), 8), 'cost_complete': complete,
            'cost_basis': 'historical_blended_estimate',
            'measurement_scope': 'List price when known, else observed Grove average; estimate, not invoice'}


def metered_turn(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        calls = []
        token = _calls.set(calls)
        try:
            result = await fn(*args, **kwargs)
            result.update(llm_calls=calls, economics=economics(calls))
            if calls:
                import observability
                cost = result['economics']['estimated_cost_usd']
                observability.metrics.bump('priced_turns' if cost is not None else 'unpriced_turns')
                if cost is not None:
                    observability.metrics.bump('estimated_cost_nanousd', round(cost * 1e9))
            return result
        finally:
            _calls.reset(token)
    return wrapped


def checked_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.mongodb.com') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Grove endpoint must use the trusted HTTPS gateway')
    return url


async def openai_request(body):
    url = os.getenv('GROVE_CHAT_COMPLETIONS_URL')
    if not url:
        raise ValueError('GROVE_CHAT_COMPLETIONS_URL is required for Grove OpenAI-compatible models')
    url = checked_url(url)
    key = os.getenv('GROVE_API_KEY', '')
    if not key:
        raise ValueError('GROVE_API_KEY is required for Grove OpenAI models')
    async with httpx.AsyncClient(timeout=float(os.getenv('LLM_TIMEOUT_SECONDS', '45')), follow_redirects=False) as client:
        response = await client.post(url, headers={'Authorization': f'Bearer {key}'}, json=body)
        response.raise_for_status()
        return response.json()


def openai_models():
    return json.loads(os.getenv('GROVE_OPENAI_MODELS', '["gpt-4o-mini","gpt-4.1","gpt-5.1","gpt-5.5","gpt-5.6-luna"]'))


# Modelos validados no gateway novo (Grove), sondados com a
# chave real em 2026-10-01. Fora de propósito: gpt-5.5/gpt-5.6-*/gpt-6-* (só
# respondem pela Responses API, não por chat/completions) e gpt-5/gpt-5-mini
# (gastam o orçamento de tokens em raciocínio e voltam incompletos).
CATALOG = ['claude-sonnet-4-5', 'claude-sonnet-5-5', 'claude-opus-4-5',
           'gpt-4o-mini', 'gpt-4.1', 'gpt-5.4-mini', 'gpt-5.4', 'grok-4.3', 'deepseek-v3.2']


def is_claude(model):
    return model.startswith('claude-')


# Capacidades por modelo, sondadas no Grove em 2026-10-08 (mesma chave, mesmo
# prompt, com e sem o parâmetro): as gerações novas da Anthropic devolvem
# 400 "`temperature` is deprecated for this model" (idem `top_p`). Enviar o
# parâmetro quebrava a troca para Sonnet 5.5 e o fallback, que também é 5.5.
# A config viva (ai_brain.model_config) continua podendo ter temperature: o
# gateway filtra o que o modelo não aceita, em vez de exigir um doc por modelo.
SAMPLING_PARAMS = ('temperature', 'top_p', 'top_k')
MODEL_CAPABILITIES = {
    'claude-sonnet-4-5': {'sampling': True},
    'claude-sonnet-4-6': {'sampling': True},
    'claude-haiku-4-5': {'sampling': True},
    'claude-opus-4-5': {'sampling': True},
    'claude-opus-4-8': {'sampling': False},
    'claude-sonnet-5': {'sampling': False},
    'claude-sonnet-5-5': {'sampling': False},
    'claude-opus-5': {'sampling': False},
    'claude-opus-5-5': {'sampling': False},
}


def capabilities(model):
    """Capacidades conhecidas; modelo Claude desconhecido NÃO recebe sampling
    (omitir temperature nunca gera 400; enviá-la a um modelo novo, sim)."""
    return MODEL_CAPABILITIES.get(model, {'sampling': False})


def adapt_params(model, kwargs):
    """Remove os parâmetros que o modelo não aceita. Devolve (kwargs, removidos)."""
    if capabilities(model).get('sampling'):
        return kwargs, []
    dropped = [k for k in SAMPLING_PARAMS if k in kwargs]
    return {k: v for k, v in kwargs.items() if k not in SAMPLING_PARAMS}, dropped


def _deprecated_param_error(exc):
    return getattr(exc, 'status_code', None) == 400 and 'deprecated for this model' in str(exc)


def model_catalog():
    return [{'model': m, 'provider': 'anthropic' if is_claude(m) else 'openai',
             'priced': m in list_prices() or m in rates()} for m in CATALOG]


def new_record(model, role, fallback=False):
    return {'agent': role, 'model': model, 'fallback': fallback,
            'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'error',
            'usage_known': False, 'estimated_cost_usd': None}


def finish_record(record, started, usage=None, openai=False):
    record['latency_ms'] = round((time.perf_counter() - started) * 1000, 2)
    if usage is not None:
        u = usage if isinstance(usage, dict) else usage.model_dump()
        ik, ok = ('prompt_tokens', 'completion_tokens') if openai else ('input_tokens', 'output_tokens')
        if u.get(ik) is not None and u.get(ok) is not None:
            cached = ((u.get('prompt_tokens_details') or {}).get('cached_tokens', 0) or 0) if openai else (u.get('cache_read_input_tokens', 0) or 0)
            write = 0 if openai else (u.get('cache_creation_input_tokens', 0) or 0)
            inp = max(0, u[ik] - cached) if openai else u[ik]
            record.update(input_tokens=inp, output_tokens=u[ok], cache_read_tokens=cached,
                          cache_write_tokens=write, usage_known=True)
            listed = list_prices().get(record['model'])
            if listed is not None:
                pin, pout = listed
                record['list_price_usd_per_mtok'] = {'input': pin, 'output': pout}
                record['estimated_cost_usd'] = round(
                    (inp * pin + u[ok] * pout + cached * pin * 0.1 + write * pin * 1.25) / 1e6, 10)
            else:
                rate = rates().get(record['model'])
                record['blended_rate_usd_per_mtok'] = rate
                if rate is not None:
                    record['estimated_cost_usd'] = round((inp + u[ok] + cached + write) * rate / 1e6, 10)
    ledger = _calls.get()
    if ledger is not None:
        ledger.append(record)


def text_content(content):
    if isinstance(content, str):
        return content
    return '\n'.join((b if isinstance(b, dict) else b.model_dump()).get('text', '') for b in content)


def convert_messages(system, messages):
    result = [{'role': 'system', 'content': text_content(system)}] if system else []
    for message in messages:
        content = message['content']
        if isinstance(content, str):
            result.append({'role': message['role'], 'content': content})
            continue
        blocks = [b if isinstance(b, dict) else b.model_dump() for b in content]
        uses = [b for b in blocks if b['type'] == 'tool_use']
        texts = [b for b in blocks if b['type'] == 'text']
        if texts or uses:
            row = {'role': message['role'], 'content': text_content(texts) or None}
            if uses:
                row['tool_calls'] = [{'id': b['id'], 'type': 'function', 'function':
                    {'name': b['name'], 'arguments': json.dumps(b['input'])}} for b in uses]
            result.append(row)
        for b in blocks:
            if b['type'] == 'tool_result':
                result.append({'role': 'tool', 'tool_call_id': b['tool_use_id'], 'content': text_content(b['content'])})
            elif b['type'] not in {'text', 'tool_use'}:
                raise ValueError('Unsupported message content for Grove')
    return result


class GatewayClient:
    """Anthropic-shaped transport; tool policy remains in the caller."""
    def __init__(self, role='assistant'):
        self.role = role
        self.messages = self
        key = os.getenv('GROVE_API_KEY')
        base = (os.getenv('GROVE_ANTHROPIC_BASE_URL') or os.getenv('GROVE_BASE_URL')) if key else os.getenv('ANTHROPIC_BASE_URL')
        if key and not base:
            # Falha fechado: sem base o SDK mandaria a chave Grove para api.anthropic.com.
            raise ValueError('GROVE_ANTHROPIC_BASE_URL is required when GROVE_API_KEY is set')
        # Sem base de gateway NÃO existe caminho para o LLM: o SDK cairia em
        # api.anthropic.com com ANTHROPIC_API_KEY (assinatura direta), o que a
        # regra do workspace proíbe. Falha fechado na chamada, não no import.
        self.native = None
        if base:
            checked_url(base)
            kwargs = {'base_url': base, 'default_headers': {'Authorization': f"Bearer {key or os.getenv('ANTHROPIC_API_KEY', '')}"}}
            self.native = AsyncAnthropic(api_key=key or os.getenv('ANTHROPIC_API_KEY') or 'not-configured',
                                        max_retries=0, timeout=45, **kwargs)

    async def create(self, *, model, **kwargs):
        fallback = kwargs.pop('_fallback', False)
        record, started, usage = new_record(model, self.role, fallback), time.perf_counter(), None
        is_openai = not is_claude(model)
        try:
            if not is_openai:
                if self.native is None:
                    raise ValueError('GROVE_API_KEY + GROVE_ANTHROPIC_BASE_URL são obrigatórios: '
                                     'o LLM só é chamado pelo gateway Grove (sem fallback direto)')
                kwargs, dropped = adapt_params(model, kwargs)
                if dropped:
                    record['dropped_params'] = dropped
                try:
                    response = await self.native.messages.create(model=model, **kwargs)
                except APIStatusError as exc:
                    # Rede de segurança para um modelo novo que a tabela ainda
                    # marca como compatível: uma única repetição sem sampling.
                    if not _deprecated_param_error(exc) or not any(k in kwargs for k in SAMPLING_PARAMS):
                        raise
                    kwargs, dropped = {k: v for k, v in kwargs.items() if k not in SAMPLING_PARAMS}, \
                        [k for k in SAMPLING_PARAMS if k in kwargs]
                    record['dropped_params'] = dropped
                    response = await self.native.messages.create(model=model, **kwargs)
                usage = response.usage
            else:
                body = {'model': model, 'messages': convert_messages(kwargs.get('system'), kwargs['messages']),
                        'max_completion_tokens': kwargs.get('max_tokens', 1024)}
                if kwargs.get('tools'):
                    body['tools'] = [{'type': 'function', 'function': {'name': t['name'],
                        'description': t.get('description', ''), 'parameters': t['input_schema']}} for t in kwargs['tools']]
                data = await openai_request(body)
                choice = data['choices'][0]
                usage = data.get('usage')
                content = []
                message = choice['message']
                if message.get('content'):
                    content.append({'type': 'text', 'text': message['content']})
                for call in message.get('tool_calls') or []:
                    content.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'],
                                    'input': json.loads(call['function']['arguments'])})
                u = usage or {}
                cached = (u.get('prompt_tokens_details') or {}).get('cached_tokens', 0) or 0
                response = Message(id=data.get('id', 'grove'), type='message', role='assistant', model=model,
                    content=content, stop_reason={'stop':'end_turn', 'tool_calls':'tool_use'}.get(choice.get('finish_reason'), 'max_tokens'),
                    usage={'input_tokens': max(0, u.get('prompt_tokens', 0) - cached),
                           'output_tokens': u.get('completion_tokens', 0), 'cache_read_input_tokens': cached})
            record['status'] = 'ok' if response.stop_reason in {'end_turn', 'tool_use'} else 'incomplete'
            if record['status'] == 'incomplete':
                raise RuntimeError('Incomplete model response; tool execution withheld')
            return response
        except httpx.HTTPStatusError as exc:
            raise APIStatusError('Grove request failed', response=exc.response, body=None) from None
        except httpx.TransportError as exc:
            raise APIConnectionError(request=exc.request) from None
        finally:
            finish_record(record, started, usage, is_openai)
