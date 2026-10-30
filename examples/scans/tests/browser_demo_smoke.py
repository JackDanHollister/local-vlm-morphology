"""Real browser start/progress/stop checks; temporary data and synthetic models only."""
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import gi
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from specimen_lab import providers
from specimen_lab.contract import FEATURES
from specimen_lab.demo import DemoManager
from specimen_lab.server import handler
from specimen_lab.store import Store

gi.require_version('Gtk','3.0');gi.require_version('WebKit2','4.1')
from gi.repository import Gtk,WebKit2,GLib

calls=[]
def observe(store,specimen_id,image_ids,provider_id,**kwargs):
    calls.append((provider_id,len(image_ids)))
    run=store.begin_run(specimen_id,image_ids,'synthetic-browser',engine='synthetic-test')
    kwargs['on_started'](run['id'])
    if len(calls)==2:
        deadline=time.monotonic()+15
        while not kwargs['cancel'].is_set() and time.monotonic()<deadline:time.sleep(.05)
    response=dict(summary='Synthetic browser demonstration',next_view_id=None,next_view_reason='Finished',
        observations=[dict(feature_id=k,status='unknown',value=None,visibility='occluded',confidence=None,evidence=[],note='Synthetic unknown') for k in FEATURES])
    return store.finish_run(run['id'],json.dumps(response),execution_error='Cancelled' if kwargs['cancel'].is_set() else None)

script=r'''(() => {
 const check=(ok,message)=>{if(!ok)throw Error(message);};
 const wait=async predicate=>{for(let i=0;i<150;i++){if(predicate())return;await new Promise(r=>setTimeout(r,100));}throw Error('Browser condition timed out');};
 (async()=>{
   await wait(()=>document.querySelector('#demo-provider').options.length>0);
   const provider=document.querySelector('#demo-provider'),source=document.querySelector('#demo-source');
   provider.value='claude';source.value='recorded';document.querySelector('#demo-count').value='3';source.dispatchEvent(new Event('change'));
   const start=document.querySelector('#demo-start');check(!start.disabled,'Saved-image start is disabled');start.click();
   await wait(()=>document.querySelector('#demo-state').textContent==='complete');
   await wait(()=>document.querySelectorAll('.feature-row').length===16 && document.querySelectorAll('#demo-thumbnails button').length===3);
   check(document.querySelectorAll('#demo-thumbnails button').length===3,'Three demo images missing');
   check(document.querySelectorAll('.feature-row').length===16,'Feature rows missing');
   check(document.querySelectorAll('#graph .node').length>16,'Graph did not update');
   check(document.querySelector('#demo-progress').value===3,'Progress not complete');
   start.click();
   await wait(()=>!document.querySelector('#demo-stop').disabled);
   document.querySelector('#demo-stop').click();
   await wait(()=>document.querySelector('#demo-state').textContent==='stopped');
   check(!start.disabled,'Start was not re-enabled after stop');
   window.demoSmoke={passed:true,checks:['browser start','selected provider','three images','unknown features','automatic graph update','browser stop']};
 })().catch(e=>{window.demoSmoke={passed:false,error:e.message};});
 return true;
})()'''

outcome={'passed':False}
with tempfile.TemporaryDirectory(prefix='specimen-browser-demo-') as root:
    store=Store(Path(root)/'store');store.specimen('test:browser','Synthetic browser specimen')
    raw=io.BytesIO();Image.new('RGB',(100,80),'white').save(raw,'PNG')
    for _ in range(3):store.add_image('test:browser',raw.getvalue())
    choices=[dict(id=p,label=p,available=True,model='synthetic',detail='Synthetic browser fixture') for p in ('codex','claude')]
    with patch.object(providers,'catalog',return_value=choices),patch.object(providers,'observe',side_effect=observe),patch('specimen_lab.hardware.capture',side_effect=AssertionError('Browser smoke must never access hardware')):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(store,DemoManager(store)))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        window=Gtk.Window();window.set_default_size(1400,1000)
        view=WebKit2.WebView();window.add(view)
        def finished(widget,res,_):
            try:
                result=json.loads(widget.evaluate_javascript_finish(res).to_string())
                if result:
                    outcome.update(result);window.destroy();Gtk.main_quit()
            except Exception as exc:
                outcome['error']=str(exc);window.destroy();Gtk.main_quit()
        def inspect():
            view.evaluate_javascript('JSON.stringify(window.demoSmoke||null)',-1,None,None,None,finished,None)
            return True
        def loaded(widget,event):
            if event==WebKit2.LoadEvent.FINISHED:
                view.evaluate_javascript(script,-1,None,None,None,None,None)
                GLib.timeout_add(500,inspect)
        def timeout():
            outcome['error']='Browser demo timed out';window.destroy();Gtk.main_quit();return False
        view.connect('load-changed',loaded);view.load_uri(f'http://127.0.0.1:{server.server_port}')
        window.show_all();GLib.timeout_add_seconds(35,timeout);Gtk.main()
        server.shutdown();server.server_close();worker.join()
        outcome['provider_calls']=calls
        if outcome['passed'] and calls!=[('claude',3),('claude',3)]:outcome.update(passed=False,error='Unexpected provider calls')
print(json.dumps(outcome),flush=True)
raise SystemExit(0 if outcome['passed'] else 1)
