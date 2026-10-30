"""Optional real-browser read-only smoke against a running viewer (GTK WebKit)."""
import json
import sys
import gi

gi.require_version('Gtk','3.0')
gi.require_version('WebKit2','4.1')
from gi.repository import Gtk, WebKit2, GLib

window=Gtk.Window();window.set_default_size(1400,1000)
view=WebKit2.WebView();window.add(view)
outcome={'passed':False}
script=r'''(() => {
 const check=(ok,message)=>{if(!ok)throw Error(message);};
 const runs=Array.from(document.querySelector('#run').options);
 const usable=runs.filter(r=>r.text.includes('usable')).at(-1);
 check(usable,'No usable run');
 const select=document.querySelector('#run');select.value=usable.value;select.dispatchEvent(new Event('change'));
 check(document.querySelectorAll('.feature-row').length===16,'Feature rows missing');
 const row=document.querySelector('[data-feature="legs.visible_count"]');row.click();
 check(document.querySelector('#detail h3').textContent.includes('legs'),'Row did not select detail');
 check(document.querySelectorAll('#overlay rect').length>0,'Evidence overlay missing');
 const node=document.querySelector('#graph .node[data-feature="antennae.present"]');node.dispatchEvent(new MouseEvent('click',{bubbles:true}));
 check(document.querySelector('#detail h3').textContent.includes('Antennae'),'Graph node did not select evidence');
 const graph=document.querySelector('#graph'),before=graph.getAttribute('viewBox');
 graph.dispatchEvent(new WheelEvent('wheel',{deltaY:-100,cancelable:true}));
 check(graph.getAttribute('viewBox')!==before,'Graph zoom did not change');
 document.querySelector('#graph-reset').click();
 check(graph.getAttribute('viewBox')===before,'Graph reset failed');
 return JSON.stringify({passed:true,run_id:usable.value,features:16,graph_nodes:document.querySelectorAll('#graph .node').length,checks:['feature selection','image overlay','graph selection','zoom','reset']});
})()'''


def done(widget,result,_):
    try:
        value=widget.evaluate_javascript_finish(result)
        outcome.update(json.loads(value.to_string()))
    except Exception as e:outcome['error']=str(e)
    print(json.dumps(outcome),flush=True);window.destroy();Gtk.main_quit()


def evaluate():
    view.evaluate_javascript(script,-1,None,None,None,done,None)
    return False


def loaded(widget,event):
    if event==WebKit2.LoadEvent.FINISHED:GLib.timeout_add(1800,evaluate)


def timeout():
    print(json.dumps({'passed':False,'error':'Browser smoke timed out'}),flush=True)
    window.destroy();Gtk.main_quit();return False


view.connect('load-changed',loaded)
view.load_uri(sys.argv[1] if len(sys.argv)>1 else 'http://127.0.0.1:8765')
window.show_all();GLib.timeout_add_seconds(20,timeout);Gtk.main()
raise SystemExit(0 if outcome['passed'] else 1)
