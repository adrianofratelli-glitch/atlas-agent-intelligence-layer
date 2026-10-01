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

    def test_observed_rates_price_every_catalog_model_and_haiku_is_cheaper(self):
        self.assertTrue(all(m['priced'] for m in gw.model_catalog()))
        usage={'input_tokens':1000,'output_tokens':200}
        cost={}
        for m in ('claude-haiku-4-5','claude-sonnet-4-5'):
            r=gw.new_record(m,'test')
            gw.finish_record(r, __import__('time').perf_counter(), usage)
            cost[m]=r['estimated_cost_usd']
        self.assertAlmostEqual(cost['claude-haiku-4-5'],1200*1.38/1e6)
        self.assertLess(cost['claude-haiku-4-5'],cost['claude-sonnet-4-5'])

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
        self.assertEqual(cat['claude-haiku-4-5'],'anthropic')

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

class EvaluationTests(unittest.TestCase):
    def test_failed_tasks_remain_in_cost_per_success(self):
        from eval_report import summarize
        report=summarize([{'passed':True,'latency_ms':10,'llm_calls':[{'estimated_cost_usd':.01}]},
                          {'passed':False,'latency_ms':20,'llm_calls':[{'estimated_cost_usd':.02}]}])
        self.assertAlmostEqual(report['cost_per_success_usd'],.03)
        self.assertEqual(report['p95_ms'],20)
        self.assertEqual(report['pass_rate'],.5)
