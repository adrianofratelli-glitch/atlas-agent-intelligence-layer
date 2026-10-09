import asyncio
import os
import unittest
from unittest.mock import patch, AsyncMock

import gateway as gw


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Hermético: o .env (carregado por outros testes via main) não pode mudar tarifa.
        p=patch.dict(os.environ); p.start(); self.addCleanup(p.stop)
        os.environ.pop('LLM_BLENDED_PRICES',None); os.environ.pop('LLM_LIST_PRICES',None)

    def test_rates_and_usage(self):
        record=gw.new_record('gpt-5.6-luna','test')
        gw.finish_record(record, __import__('time').perf_counter(), {'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':40}}, True)
        self.assertEqual(record['input_tokens'],60)
        self.assertAlmostEqual(record['estimated_cost_usd'],120*.30/1e6)
        self.assertIsNone(gw.economics([{'estimated_cost_usd':None}])['estimated_cost_usd'])
        with patch.dict(os.environ, {'LLM_BLENDED_PRICES':'{"x":-1}'}):
            with self.assertRaises(ValueError): gw.rates()

    def test_observed_rates_price_every_catalog_model_and_sonnet_4_5_is_cheaper(self):
        self.assertTrue(all(m['priced'] for m in gw.model_catalog()))
        usage={'input_tokens':1000,'output_tokens':200}
        cost={}
        for m in ('claude-sonnet-5-5','claude-sonnet-4-5'):
            r=gw.new_record(m,'test')
            gw.finish_record(r, __import__('time').perf_counter(), usage)
            cost[m]=r['estimated_cost_usd']
        self.assertAlmostEqual(cost['claude-sonnet-5-5'],1200*7.71/1e6)
        self.assertLess(cost['claude-sonnet-4-5'],cost['claude-sonnet-5-5'])
        self.assertNotIn('claude-haiku-4-5',[m['model'] for m in gw.model_catalog()])

    def test_blended_env_merges_with_defaults(self):
        with patch.dict(os.environ, {'LLM_BLENDED_PRICES': '{"modelo-novo":2.5}'}):
            r=gw.rates()
            self.assertEqual(r['modelo-novo'],2.5)
            self.assertEqual(r['grok-4.3'],1.26)

    def test_list_prices_override_merges_and_validates(self):
        with patch.dict(os.environ, {'LLM_LIST_PRICES': '{"grok-4.3":[1.5,6]}'}):
            p=gw.list_prices()
            self.assertEqual(p['grok-4.3'],(1.5,6.0))
            self.assertEqual(set(p),{'grok-4.3'})   # tabela de lista é opt-in
        with patch.dict(os.environ, {'LLM_LIST_PRICES': '{"x":[1]}'}):
            with self.assertRaises(ValueError): gw.list_prices()

    def test_catalog_providers(self):
        cat={m['model']:m['provider'] for m in gw.model_catalog()}
        self.assertEqual(cat['gpt-4.1'],'openai')
        self.assertEqual(cat['claude-sonnet-5-5'],'anthropic')

    def test_endpoint_and_tool_history(self):
        with self.assertRaises(ValueError): gw.checked_url('https://example.com')
        msgs=gw.convert_messages('safe',[
          {'role':'assistant','content':[{'type':'tool_use','id':'t','name':'find','input':{'id':1}}]},
          {'role':'user','content':[{'type':'tool_result','tool_use_id':'t','content':'found'}]}])
        self.assertEqual(msgs[-1],{'role':'tool','tool_call_id':'t','content':'found'})
        self.assertEqual(msgs[1]['tool_calls'][0]['function']['arguments'],'{"id": 1}')

    async def test_openai_tool_roundtrip_and_ledger(self):
        response={'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{'id':'t','function':{'name':'find','arguments':'{"id":1}'}}]}}], 'usage':{'prompt_tokens':10,'completion_tokens':5}}
        client=gw.GatewayClient('test')
        @gw.metered_turn
        async def run():
            result=await client.messages.create(model='gpt-5.6-luna',messages=[{'role':'user','content':'lookup'}],tools=[{'name':'find','input_schema':{'type':'object'}}])
            self.assertEqual(result.content[0].input,{'id':1})
            return {}
        with patch.object(gw,'openai_request',AsyncMock(return_value=response)):
            result=await run()
        self.assertEqual(len(result['llm_calls']),1)
        self.assertTrue(result['economics']['cost_complete'])

    async def test_concurrent_turns_are_isolated(self):
        @gw.metered_turn
        async def run(model):
            await asyncio.sleep(0)
            record=gw.new_record(model,'test')
            gw.finish_record(record,__import__('time').perf_counter())
            return {}
        a,b=await asyncio.gather(run('a'),run('b'))
        self.assertEqual([x['model'] for x in a['llm_calls']],['a'])
        self.assertEqual([x['model'] for x in b['llm_calls']],['b'])

if __name__=='__main__': unittest.main()

class NoDirectProviderFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_grove_gateway_there_is_no_direct_anthropic_call(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ('GROVE_API_KEY', 'GROVE_ANTHROPIC_BASE_URL', 'GROVE_BASE_URL', 'ANTHROPIC_BASE_URL')}
        env['ANTHROPIC_API_KEY'] = 'sk-ant-direct-subscription'
        with patch.dict(os.environ, env, clear=True):
            client = gw.GatewayClient('test')
            self.assertIsNone(client.native, 'nenhum cliente apontando para api.anthropic.com')
            with self.assertRaises(ValueError):
                await client.messages.create(model='claude-sonnet-4-5', max_tokens=8,
                                             messages=[{'role': 'user', 'content': 'oi'}])


class EvaluationTests(unittest.TestCase):
    def test_failed_tasks_remain_in_cost_per_success(self):
        from eval_report import summarize
        report=summarize([{'passed':True,'latency_ms':10,'llm_calls':[{'estimated_cost_usd':.01}]},
                          {'passed':False,'latency_ms':20,'llm_calls':[{'estimated_cost_usd':.02}]}])
        self.assertAlmostEqual(report['cost_per_success_usd'],.03)
        self.assertEqual(report['p95_ms'],20)
        self.assertEqual(report['pass_rate'],.5)


class ModelCapabilityTests(unittest.IsolatedAsyncioTestCase):
    """Regressão P1 (2026-10-08): Sonnet 5.5 devolve 400 "`temperature` is
    deprecated for this model"; enviar o parâmetro quebrava a troca de modelo
    E o fallback (que também é 5.5)."""

    def _client(self, create):
        from types import SimpleNamespace
        client = gw.GatewayClient('test')
        client.native = SimpleNamespace(messages=SimpleNamespace(create=create))
        return client

    @staticmethod
    def _ok(model):
        from anthropic.types import Message
        return Message(id='m', type='message', role='assistant', model=model,
                       content=[{'type': 'text', 'text': 'ok'}], stop_reason='end_turn',
                       usage={'input_tokens': 1, 'output_tokens': 1})

    def test_capability_table(self):
        for m in ('claude-sonnet-5-5', 'claude-opus-5-5', 'claude-opus-4-8', 'claude-sonnet-5'):
            self.assertFalse(gw.capabilities(m)['sampling'], m)
        for m in ('claude-sonnet-4-5', 'claude-haiku-4-5', 'claude-opus-4-5'):
            self.assertTrue(gw.capabilities(m)['sampling'], m)
        self.assertFalse(gw.capabilities('claude-modelo-futuro')['sampling'], 'desconhecido: sem sampling')
        kw, dropped = gw.adapt_params('claude-sonnet-5-5', {'temperature': 0.3, 'top_p': 0.9, 'max_tokens': 9})
        self.assertEqual(kw, {'max_tokens': 9})
        self.assertEqual(dropped, ['temperature', 'top_p'])
        kw, dropped = gw.adapt_params('claude-sonnet-4-5', {'temperature': 0.3})
        self.assertEqual((kw, dropped), ({'temperature': 0.3}, []))

    async def test_sonnet_5_5_never_receives_temperature(self):
        seen = []
        async def create(**kw):
            seen.append(kw)
            if 'temperature' in kw and kw['model'] == 'claude-sonnet-5-5':
                raise AssertionError('temperature enviada a modelo que a rejeita')
            return self._ok(kw['model'])
        client = self._client(create)
        await client.messages.create(model='claude-sonnet-5-5', temperature=0.3, max_tokens=8,
                                     messages=[{'role': 'user', 'content': 'oi'}])
        self.assertNotIn('temperature', seen[0])
        await client.messages.create(model='claude-sonnet-4-5', temperature=0.3, max_tokens=8,
                                     messages=[{'role': 'user', 'content': 'oi'}])
        self.assertEqual(seen[1]['temperature'], 0.3, 'modelo que aceita mantém a temperatura')

    async def test_deprecated_param_400_retries_once_without_sampling(self):
        import httpx
        from anthropic import BadRequestError
        req = httpx.Request('POST', 'https://grove.mongodb.com/anthropic/v1/messages')
        calls = []
        async def create(**kw):
            calls.append(kw)
            if 'temperature' in kw:
                raise BadRequestError('`temperature` is deprecated for this model.',
                                      response=httpx.Response(400, request=req), body=None)
            return self._ok(kw['model'])
        with patch.dict(gw.MODEL_CAPABILITIES, {'claude-novo-x': {'sampling': True}}):
            client = self._client(create)
            await client.messages.create(model='claude-novo-x', temperature=0.3, max_tokens=8,
                                         messages=[{'role': 'user', 'content': 'oi'}])
        self.assertEqual(len(calls), 2)
        self.assertNotIn('temperature', calls[1])

    async def test_llm_call_model_with_seeded_config_for_every_catalog_claude(self):
        import llm
        seen = []
        async def create(**kw):
            seen.append(kw)
            if 'temperature' in kw and not gw.capabilities(kw['model'])['sampling']:
                raise AssertionError(kw['model'])
            return self._ok(kw['model'])
        client = self._client(create)
        with patch.object(llm, 'client', client):
            for m in [c for c in gw.CATALOG if gw.is_claude(c)]:
                r = await llm.call_model({'model': m, 'temperature': 0.3, 'max_tokens': 16}, 's',
                                         [{'role': 'user', 'content': 'oi'}])
                self.assertEqual(r['text'], 'ok')
