"""Interchangeable image observers. Only these adapters can call model services."""
import base64
import copy
import json
import mimetypes
import os
import shutil
import subprocess
import time
from urllib.request import Request, urlopen

from .observer import observe as codex_observe
from .process import communicate
from .store import canonical, write_new
from .contract import output_schema


QWEN_BBOX_FORMAT = 'qwen-1000-xyxy'


def local_wire_schema(schema, bbox_format):
    """Return the endpoint schema without weakening the stored experiment contract."""
    if not bbox_format:
        return schema
    if bbox_format != QWEN_BBOX_FORMAT:
        raise ValueError('SPECIMEN_LOCAL_VLM_BBOX_FORMAT must be empty or '+QWEN_BBOX_FORMAT)
    wire=copy.deepcopy(schema)
    observations=wire['properties']['observations']
    observations.update(minItems=16,maxItems=16)
    properties=observations['items']['properties']
    observations['items']['properties']={key:properties[key] for key in (
        'feature_id','status','value','visibility','confidence','note','evidence')}
    bbox=(wire['properties']['observations']['items']['properties']['evidence']
          ['items']['properties']['bbox'])
    bbox['items']={'type':'integer','minimum':0,'maximum':1000}
    base=observations['items']
    base['properties']['note']['maxLength']=160
    base['properties']['evidence']['items']['properties']['description'].update(minLength=1,maxLength=96)
    wire['properties']['summary']['maxLength']=400

    def variant(status,value,features=None,visibility=None):
        item=copy.deepcopy(base)
        props=item['properties']
        props['status']={'enum':status}
        props['value']=value
        if features is not None:props['feature_id']={'enum':features}
        if visibility is not None:props['visibility']={'enum':visibility}
        if status==['observed']:props['evidence']['minItems']=1
        else:props['note']['minLength']=1
        return item

    feature_ids=base['properties']['feature_id']['enum']
    positive={'anyOf':[{'const':True},{'type':'integer','minimum':1,'maximum':100},
                       {'type':'string'},{'type':'array','items':{'type':'string'}}]}
    observations['items']={'anyOf':[
        variant(['unknown','not_applicable'],{'type':'null'}),
        variant(['observed'],positive,[key for key in feature_ids if key!='legs.total_count']),
        variant(['observed'],{'anyOf':[{'const':False},{'const':0}]},visibility=['clear']),
        variant(['observed'],{'const':False},[key for key in feature_ids if key.startswith('image.')]),
        variant(['observed'],{'type':'integer','minimum':1,'maximum':100},['legs.total_count'],['clear']),
    ]}
    return wire


def normalize_local_response(payload, bbox_format):
    """Convert a documented endpoint coordinate convention to the stored contract."""
    if not bbox_format:
        return payload
    if bbox_format != QWEN_BBOX_FORMAT:
        raise ValueError('Unsupported local VLM bbox format')
    converted=copy.deepcopy(payload)
    for observation in converted.get('observations',[]):
        for evidence in observation.get('evidence',[]):
            box=evidence.get('bbox')
            if (not isinstance(box,list) or len(box)!=4 or
                    any(type(value) is not int for value in box)):
                raise ValueError('Qwen bbox must be integer [x1,y1,x2,y2]')
            x1,y1,x2,y2=box
            if not (0<=x1<x2<=1000 and 0<=y1<y2<=1000):
                raise ValueError('Qwen bbox must satisfy 0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000')
            evidence['bbox']=[round(x1/1000,6),round(y1/1000,6),
                              round((x2-x1)/1000,6),round((y2-y1)/1000,6)]
    return converted


def catalog():
    local_url=os.environ.get('SPECIMEN_LOCAL_VLM_URL','')
    local_model=os.environ.get('SPECIMEN_LOCAL_VLM_MODEL','')
    choices = [
        dict(id='codex',label='Codex',model='gpt-6-astra',available=bool(shutil.which('codex')),detail='Uses the signed-in Codex CLI'),
        dict(id='claude',label='Claude',model='sonnet',available=bool(shutil.which('claude')),detail='Uses the signed-in Claude CLI'),
        dict(id='local',label='Local VLM',model=local_model,available=bool(local_url and local_model),
             detail='Configured local vision endpoint' if local_url and local_model else 'Vision endpoint not configured'),
    ]
    preferred=os.environ.get('SPECIMEN_DEFAULT_PROVIDER','')
    return sorted(choices,key=lambda item:not (item['id']==preferred and item['available']))


def provider(provider_id):
    item=next((p for p in catalog() if p['id']==provider_id),None)
    if not item or not item['available']:raise ValueError('Selected model provider is not configured')
    return item


def image_blocks(store,image_ids,style):
    result=[]
    for image_id in image_ids:
        path=store.image_path(image_id,verify=True)
        mime=mimetypes.guess_type(path)[0]
        encoded=base64.b64encode(path.read_bytes()).decode()
        if style=='claude':result.append({'type':'image','source':{'type':'base64','media_type':mime,'data':encoded}})
        else:result.append({'type':'image_url','image_url':{'url':f'data:{mime};base64,{encoded}'}})
    return result


def parse_claude_events(text):
    result=None;errors=[];models=set()
    for line in text.splitlines():
        if not line.strip():continue
        event=json.loads(line)
        if event.get('type')=='assistant':
            message=event.get('message',{})
            if message.get('model'):models.add(message['model'])
            for block in message.get('content',[]):
                if block.get('type')=='tool_use':
                    errors.append('Unexpected Claude tool use: '+str(block.get('name')))
        if event.get('type')=='result':result=event
    if not result or result.get('is_error') or result.get('subtype')!='success':
        errors.append('Claude did not return a successful final result')
    if result is not None and result.get('structured_output') is not None:
        raw=canonical(result['structured_output'])
    else:raw=(result or {}).get('result','')
    return raw,errors,dict(models=sorted(models),usage=(result or {}).get('usage',{}),reported_cost_usd=(result or {}).get('total_cost_usd'))


def claude_observe(store,specimen_id,image_ids,model,*,next_views=(),timeout=240,cancel=None,on_started=None):
    suffix='\nRequired response JSON schema:\n'+canonical(output_schema(image_ids,next_views))+'\nReturn the complete JSON object as text, with observations and every required key. No markdown fences.\n'
    run=store.begin_run(specimen_id,image_ids,model,next_views=next_views,engine='claude-cli',prompt_suffix=suffix)
    if on_started:on_started(run['id'])
    folder=store.root/'runs'/run['id'];work=folder/'empty-workdir';work.mkdir()
    command=['claude','-p','--safe-mode','--tools','','--disable-slash-commands','--strict-mcp-config',
             '--mcp-config','{"mcpServers":{}}','--no-session-persistence','--model',model,
             '--effort','medium','--input-format','stream-json','--output-format','stream-json','--verbose',
             '--system-prompt','You are an image observer. Return the requested structured observation. Use only supplied image content; do not use tools or infer hidden anatomy.']
    write_new(folder/'execution.json',(canonical({'argv':command,'timeout_seconds':timeout})+'\n').encode())
    raw='';errors=[];telemetry={};started=time.monotonic()
    try:
        content=[{'type':'text','text':(folder/'prompt.txt').read_text()},*image_blocks(store,image_ids,'claude')]
        payload=(canonical({'type':'user','message':{'role':'user','content':content}})+'\n').encode()
        if len(payload)>9_500_000:raise ValueError('Claude CLI image input exceeds the demo payload budget')
        with (folder/'events.jsonl').open('xb') as stdout,(folder/'stderr.log').open('xb') as stderr:
            proc=subprocess.Popen(command,cwd=work,stdin=subprocess.PIPE,stdout=stdout,stderr=stderr,start_new_session=True)
            error=communicate(proc,payload,timeout,cancel)
            if error:errors.append(error)
            if proc.returncode:errors.append(f'Claude exit code {proc.returncode}')
        raw,stream_errors,telemetry=parse_claude_events((folder/'events.jsonl').read_text())
        errors.extend(stream_errors)
    except (OSError,ValueError) as exc:errors.append(str(exc))
    telemetry['wall_seconds']=round(time.monotonic()-started,3)
    return store.finish_run(run['id'],raw,execution_error='; '.join(errors) or None,telemetry=telemetry)


def local_observe(store,specimen_id,image_ids,model,*,next_views=(),timeout=240,cancel=None,on_started=None):
    bbox_format=os.environ.get('SPECIMEN_LOCAL_VLM_BBOX_FORMAT','')
    suffix=''
    if bbox_format==QWEN_BBOX_FORMAT:
        suffix='''\nQwen grounding adapter requirements:\n- Emit every evidence bbox in native [x1,y1,x2,y2] relative coordinates, using integer values from 0 through 1000. The adapter converts them to the stored normalized [x,y,width,height] contract.\n- For an observed count N, emit N evidence entries with N distinct, feature-specific instance_id values and a tight bbox around each visible instance. If individual instances cannot be boxed, return unknown/null with a reason instead of guessing a count.\n- Do not reuse a whole-specimen box as evidence for distinct antennae, legs or wings.\n'''
        suffix+='Keep descriptions and notes concise. Use empty evidence for unknown features. Return all 16 features exactly once.\n'
        suffix+='A feature that cannot be seen is unknown, not false: this includes wings, eyes, hairs and damage. Every observed claim, even false or zero, needs a relevant region. A positive count N needs N distinct instance IDs; zero needs a clearly visible region with instance_id null. If two appendages cannot be localized separately, do not reuse one broad box to assert two instances. Keep the summary purely visual without taxonomic guesses.\n'
    wire_schema=local_wire_schema(output_schema(image_ids,next_views),bbox_format)
    suffix+='\nRequired response JSON schema:\n'+json.dumps(wire_schema,ensure_ascii=False,separators=(',',':'))+'\nReturn one complete JSON object.\n'
    run=store.begin_run(specimen_id,image_ids,model,next_views=next_views,
                        engine='local-openai-compatible',prompt_suffix=suffix)
    if on_started:on_started(run['id'])
    folder=store.root/'runs'/run['id'];raw='';error=None;telemetry={};started=time.monotonic()
    try:
        if cancel is not None and cancel.is_set():raise ValueError('Observer cancelled')
        configured_timeout=int(os.environ.get('SPECIMEN_LOCAL_VLM_TIMEOUT_SECONDS','0'))
        if not 0<=configured_timeout<=3600:raise ValueError('SPECIMEN_LOCAL_VLM_TIMEOUT_SECONDS must be 0 or between 1 and 3600')
        timeout=max(timeout,configured_timeout)
        streaming=os.environ.get('SPECIMEN_LOCAL_VLM_STREAM','0')=='1'
        telemetry.update(bbox_adapter=bbox_format or None,stream=streaming)
        url=os.environ['SPECIMEN_LOCAL_VLM_URL'].rstrip('/')+'/chat/completions'
        schema=wire_schema
        body={'model':model,'messages':[{'role':'user','content':[
            {'type':'text','text':(folder/'prompt.txt').read_text()},*image_blocks(store,image_ids,'openai')]}],
            'response_format':{'type':'json_schema','json_schema':{'name':'specimen_observation','strict':True,'schema':schema}},
            'max_tokens':6000,'temperature':0,'seed':0}
        if streaming:body.update(stream=True,stream_options={'include_usage':True})
        write_new(folder/'wire-schema.json',(canonical(schema)+'\n').encode())
        write_new(folder/'execution.json',(canonical({
            'timeout_seconds':timeout,'bbox_adapter':bbox_format or None,
            'max_tokens':body['max_tokens'],'temperature':body['temperature'],
            'seed':body['seed'],'image_count':len(image_ids),'stream':streaming})+'\n').encode())
        headers={'Content-Type':'application/json'}
        if os.environ.get('SPECIMEN_LOCAL_VLM_API_KEY'):headers['Authorization']='Bearer '+os.environ['SPECIMEN_LOCAL_VLM_API_KEY']
        with urlopen(Request(url,json.dumps(body,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode(),headers=headers),
                     timeout=min(timeout,120) if streaming else timeout) as response:
            if streaming:
                if 'text/event-stream' not in response.headers.get('Content-Type',''):
                    raise ValueError('Local endpoint did not return an event stream')
                choice={'message':{},'finish_reason':None};done=False;received=0
                with (folder/'http-response.sse').open('xb') as events:
                    while True:
                        if cancel is not None and cancel.is_set():raise ValueError('Observer cancelled')
                        if time.monotonic()-started>timeout:raise TimeoutError('Local model wall-time limit exceeded')
                        line=response.readline(1_000_001)
                        if not line:break
                        events.write(line);events.flush();received+=len(line)
                        if len(line)>1_000_000 or received>16_000_000:
                            raise ValueError('Local model event stream exceeded size budget')
                        if not line.startswith(b'data:'):continue
                        packet=line[5:].strip()
                        if packet==b'[DONE]':done=True;break
                        output=json.loads(packet)
                        if output.get('error'):raise ValueError('Local model stream error: '+str(output['error']))
                        if output.get('model'):telemetry['model_reported']=output['model']
                        if output.get('usage'):telemetry['usage']=output['usage']
                        for part in output.get('choices',[]):
                            if part.get('index',0)!=0:raise ValueError('Unexpected additional model choice')
                            delta=part.get('delta',{})
                            raw+=delta.get('content') or ''
                            if delta.get('tool_calls'):raise ValueError('Local response used tools')
                            if part.get('finish_reason'):choice['finish_reason']=part['finish_reason']
                    if not done:raise ValueError('Local event stream ended before its terminal marker')
            else:
                response_raw=response.read(16_000_001)
                write_new(folder/'http-response.json',response_raw)
                if len(response_raw)>16_000_000:raise ValueError('Local model response exceeded size budget')
                output=json.loads(response_raw);choice=output['choices'][0]
                raw=choice['message'].get('content') or ''
                telemetry={'usage':output.get('usage',{}),'model_reported':output.get('model')}
        telemetry.update(bbox_adapter=bbox_format or None,finish_reason=choice.get('finish_reason'),stream=streaming)
        if choice.get('finish_reason')!='stop' or choice['message'].get('tool_calls'):raise ValueError('Local response incomplete or used tools')
        if bbox_format:
            write_new(folder/'wire-response.txt',raw.encode())
            raw=canonical(normalize_local_response(json.loads(raw),bbox_format))
        if cancel is not None and cancel.is_set():error='Observer cancelled'
    except Exception as exc:error=str(exc)
    telemetry['wall_seconds']=round(time.monotonic()-started,3)
    return store.finish_run(run['id'],raw,execution_error=error,telemetry=telemetry)


def observe(store,specimen_id,image_ids,provider_id,**kwargs):
    item=provider(provider_id)
    return {'codex':codex_observe,'claude':claude_observe,'local':local_observe}[provider_id](
        store,specimen_id,image_ids,item['model'],**kwargs)
