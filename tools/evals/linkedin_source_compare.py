"""Read-only LinkedIn MCP filter comparison; requires explicit established-session opt-in."""
import json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from eregion.runtime.career.linkedin_source import LinkedInMcpSource,LinkedInSourceStopped
from eregion.runtime.mcp.config import McpRuntimeConfig,McpStdioServerConfig
from eregion.runtime.mcp.manager import McpToolManager
from eregion.runtime.registry import ToolRegistry

QUERIES=[('junior data analyst','New York, NY','entry'),('graduate data analyst','New York, NY','entry'),('software engineer intern','New York, NY','internship')]
def size(value):
    text=json.dumps(value,ensure_ascii=False,separators=(',',':'))
    return {'chars':len(text),'utf8_bytes':len(text.encode()),'token_estimate_chars_div4':round(len(text)/4),
            'token_estimator':'rough chars/4; not model-specific or billed usage'}

def main():
    if os.getenv('JOB_AGENT_LINKEDIN_COMPARISON_LIVE')!='1':raise SystemExit('Explicit established-session opt-in required')
    repeat='--repeat-searches' in sys.argv
    suffix='-repeat' if repeat else ''
    report=ROOT/f'docs/benchmarks/filter-api-mcp-20261012/mcp{suffix}-results.json'
    if report.exists():raise SystemExit('Do not overwrite or automatically repeat a live sample')
    out=ROOT/'.codex-runs/filter-api-mcp-20261012';records=[];store={};raw_sizes=[]
    allowed={'search_jobs','get_job_details','close_session'}
    config=McpRuntimeConfig(servers=[McpStdioServerConfig(server_id='linkedin',required=True,
        command=str(ROOT/'.codex-runs/linkedin-comparison-venv/Scripts/python.exe'),
        args=['-m','linkedin_mcp_server','--transport','stdio','--no-auto-import',
              '--user-data-dir',str(ROOT/'.codex-runs/linkedin-comparison-session/profile'),
              '--chrome-path','C:/Program Files/Google/Chrome/Application/chrome.exe'],
        cwd=ROOT,tool_allowlist=allowed,read_only_tools=allowed,
        startup_timeout_seconds=120,call_timeout_seconds=120,max_result_bytes=2000000)])
    manager=McpToolManager(config=config,registry=ToolRegistry())
    def call(server,tool,args,**kwargs):
        start=time.monotonic();value=manager.call_tool(server,tool,args,**kwargs)
        data=value.model_dump(mode='json') if hasattr(value,'model_dump') else value
        index=len(raw_sizes);(out/f'mcp{suffix}-raw-{index}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        raw_sizes.append({'tool':tool,'seconds':round(time.monotonic()-start,3),'size':size(data)})
        return value
    source=LinkedInMcpSource(call,store=store,call_budget=12)
    error=None;started=time.monotonic()
    try:
        manager.start();startup=round(time.monotonic()-started,3)
        for query,location,level in QUERIES:
            start=time.monotonic();result=source.search(query,location,experience_level=level,max_pages=1)
            records.append({'query':query,'location':location,'seconds':round(time.monotonic()-start,3),
                            'result':result,'normalized_size':size(result),'model_calls':0,'model_tokens':0})
        for target in ([] if repeat else json.loads((out/'shared-targets.json').read_text())):
            start=time.monotonic();result=source.details(target['provider_job_id'])
            records.append({'target':target,'seconds':round(time.monotonic()-start,3),
                            'status':result['status'],'incomplete_reasons':result['incomplete_reasons'],
                            'normalized_size':size(result),'model_calls':0,'model_tokens':0})
    except Exception as exc:
        error={'type':type(exc).__name__,'reason':getattr(exc,'reason',None)}
    finally:
        try:manager.call_tool('linkedin','close_session',{},timeout_seconds=15)
        except Exception:pass
        manager.stop()
        report.write_text(json.dumps({'scope':'source-only; no model usage','records':records,'raw_sizes':raw_sizes,
            'store':store,'error':error,'elapsed_seconds':round(time.monotonic()-started,3)},ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
