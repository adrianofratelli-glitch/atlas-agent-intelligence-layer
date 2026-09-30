import asyncio, json, os, sys, time
from pathlib import Path
from dotenv import load_dotenv
root=Path.cwd()
load_dotenv(root/'.env')
kind='single'
rows=[]
async def main():
 if kind=='single':
  sys.path.insert(0,str(root/'backend'))
  import gateway
  @gateway.metered_turn
  async def run(model):
   client=gateway.GatewayClient('test_tool_contract')
   response=await client.messages.create(model=model,max_tokens=120,system='You test a tool protocol. Call echo with value hello. Do not answer without a tool call.',messages=[{'role':'user','content':'Call echo now.'}],tools=[{'name':'echo','description':'Test-only echo, never executed','input_schema':{'type':'object','properties':{'value':{'type':'string'}},'required':['value']}}])
   return {'passed':any(b.type=='tool_use' and b.name=='echo' and b.input.get('value')=='hello' for b in response.content)}
 else:
  from finscope.model_gateway import InvestigatorModel
  from langchain_core.messages import HumanMessage, SystemMessage
  async def run(model):
   os.environ['LLM_MODEL']=model
   os.environ['LLM_FALLBACK_MODEL']=''
   llm=InvestigatorModel().bind_tools([{'name':'echo','description':'Test-only echo, never executed','parameters':{'type':'object','properties':{'value':{'type':'string'}},'required':['value']}}])
   result=await llm.ainvoke([SystemMessage(content='Call echo with value hello. Do not answer without a tool call.'),HumanMessage(content='Call echo now.')])
   return {'passed':any(t['name']=='echo' and t['args'].get('value')=='hello' for t in result.tool_calls),'llm_calls':result.response_metadata['llm_calls']}
 for model in ['claude-haiku-4-5','gpt-5.6-luna']:
  start=time.perf_counter()
  result=await run(model)
  result['latency_ms']=round((time.perf_counter()-start)*1000)
  rows.append(result)
  print(model,'PASS' if result['passed'] else 'FAIL',result['latency_ms'],'ms',flush=True)
 Path('docs/internal/grove-protocol-evals.json').write_text(json.dumps(rows,indent=2))
 if not all(r['passed'] for r in rows): raise SystemExit(1)
asyncio.run(main())
