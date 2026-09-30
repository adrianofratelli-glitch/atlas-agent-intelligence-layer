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
DEFAULT_RATES = {'claude-haiku-4-5': 2.40, 'gpt-5.6-luna': 0.38,
                 'claude-sonnet-4-5': 1.96, 'claude-sonnet-5': 2.43}


def rates():
    values = json.loads(os.getenv('LLM_BLENDED_PRICES', json.dumps(DEFAULT_RATES)))
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
            'measurement_scope': 'Observed Grove average; estimate, not invoice'}


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
    if parsed.scheme != 'https' or parsed.hostname != 'grove-gateway-prod.azure-api.net' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Grove endpoint must use the trusted HTTPS gateway')
    return url


async def openai_request(body):
    url = checked_url(os.getenv('GROVE_CHAT_COMPLETIONS_URL',
        'https://grove-gateway-prod.azure-api.net/grove-foundry-prod/openai/v1/chat/completions'))
    key = os.getenv('GROVE_API_KEY', '')
    if not key:
        raise ValueError('GROVE_API_KEY is required for Grove OpenAI models')
    async with httpx.AsyncClient(timeout=float(os.getenv('LLM_TIMEOUT_SECONDS', '45')), follow_redirects=False) as client:
        response = await client.post(url, headers={'api-key': key}, json=body)
        response.raise_for_status()
        return response.json()


def openai_models():
    return json.loads(os.getenv('GROVE_OPENAI_MODELS', '["gpt-5.6-luna"]'))


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
        base = (os.getenv('GROVE_ANTHROPIC_BASE_URL') or 'https://grove-gateway-prod.azure-api.net/grove-foundry-prod/anthropic') if key else os.getenv('ANTHROPIC_BASE_URL')
        kwargs = {}
        if base:
            checked_url(base)
            kwargs = {'base_url': base, 'default_headers': {'api-key': key or os.getenv('ANTHROPIC_API_KEY', '')}}
        self.native = AsyncAnthropic(api_key=key or os.getenv('ANTHROPIC_API_KEY') or 'not-configured',
                                    max_retries=0, timeout=45, **kwargs)

    async def create(self, *, model, **kwargs):
        fallback = kwargs.pop('_fallback', False)
        record, started, usage = new_record(model, self.role, fallback), time.perf_counter(), None
        is_openai = model in openai_models()
        try:
            if not is_openai:
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
