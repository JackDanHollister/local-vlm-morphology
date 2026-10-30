'use strict';
const $ = id => document.getElementById(id);
let data, graph, run, selectedFeature = null, currentImage = null;
let demoJob=null,demoProviders=[],demoSignature='',demoPolling=false,liveBlocked=false;
let graphBox = [0, 0, 1400, 950], defaultBox = [...graphBox];
const valueText = o => o.status === 'observed' ? (Array.isArray(o.value) ? o.value.join(', ') : String(o.value)) : o.status.replace('_', ' ');
function element(tag, text, className) { const e = document.createElement(tag); if(text !== undefined)e.textContent = text; if(className)e.className = className; return e; }
function options(select, items, previous) { select.replaceChildren(...items.map(([value, label]) => {const e = element('option',label); e.value=value; return e;})); if(items.some(i=>i[0]===previous)) select.value=previous; }
async function api(url, options) { const r=await fetch(url, options); const value=await r.json(); if(!r.ok)throw new Error(value.error||'Request failed'); return value; }
async function refresh() {
  try {
    [data,graph]=await Promise.all([api('/api/snapshot'),api('/api/graph')]);
    options($('specimen'),data.specimens.map(s=>[s.id,s.label]),$('specimen').value);
    chooseSpecimen();
  } catch(e){$('run-status').textContent=e.message; $('run-status').className='error';}
}
function chooseSpecimen() {
  const runs=data.runs.filter(r=>r.specimen_id===$('specimen').value);
  options($('run'),runs.map(r=>[r.id,`${r.model} · ${r.id.slice(-6)} · ${r.state}`]),run?.id);
  if(!run && runs.length)$('run').value=runs.at(-1).id;
  chooseRun();
}
function chooseRun() {
  run=data.runs.find(r=>r.id===$('run').value);
  const images=data.images.filter(i=>i.specimen_id===$('specimen').value && (!run || run.image_ids.includes(i.id)));
  options($('image-select'),images.map(i=>[i.id,`${i.role} · ${i.width} × ${i.height} · ${i.id.slice(-6)}`]),currentImage);
  if(!currentImage && images.some(i=>i.role==='crop'))$('image-select').value=images.find(i=>i.role==='crop').id;
  $('summary').textContent=run?.result?.summary || '';
  const decision=data.decisions?.find(d=>d.terminal?.child_run_id===run?.id);
  const parent=decision && data.runs.find(r=>r.id===decision.parent_run_id);
  const changes=parent?.result && run?.result ? run.result.observations.filter(o=>{const old=parent.result.observations.find(p=>p.feature_id===o.feature_id);return old && valueText(old)!==valueText(o);}).map(o=>`${o.feature_id}: ${valueText(parent.result.observations.find(p=>p.feature_id===o.feature_id))} → ${valueText(o)}`):[];
  $('comparison').textContent=parent ? `After requested view ${decision.requested_view}: ${changes.length?changes.join('; '):'no feature values changed'}. Differences may reflect both the additional view and model variability; they are not verified improvements.`:'';
  $('run-status').textContent=run ? `${run.state} · ${run.errors?.join('; ') || (run.state==='usable' ? 'Schema passed; biological accuracy unverified' : 'Waiting for model response')}` : 'Import a specimen image to begin.';
  $('run-status').className=run?.state==='failed'?'error':'';
  $('feature-list').replaceChildren();
  for(const o of run?.result?.observations || []) {
    const b=element('button',undefined,'feature-row '+o.status); b.dataset.feature=o.feature_id;
    b.append(element('span',data.features[o.feature_id].label),element('span',valueText(o),'badge'),element('span',o.feature_id,'tag'));
    b.onclick=()=>selectFeature(o.feature_id); $('feature-list').append(b);
  }
  chooseImage();
  selectFeature(selectedFeature || run?.result?.observations[0]?.feature_id);
  renderGraph();
}
function chooseImage() {
  currentImage=$('image-select').value;
  const im=data.images.find(i=>i.id===currentImage);
  if(!im){$('evidence-image').removeAttribute('src'); $('image-info').textContent='No images'; return;}
  $('evidence-image').src='/api/images/'+im.id;
  $('image-info').textContent=`${im.id} · SHA-256 ${im.sha256.slice(0,16)}… · ${im.role==='crop' ? `Crop [${im.crop_box.join(', ')}] from ${im.parent_id}` : 'Preserved original'} · ${im.pose.kind}`;
  if(im.pose.firmware_position)$('image-info').textContent+=` · Y ${im.pose.firmware_position.Y}, Z ${im.pose.firmware_position.Z} firmware units · ${im.pose.axis} ${im.pose.relative_steps_this_action>0?'+':''}${im.pose.relative_steps_this_action} steps`;
  drawBoxes();
}
function drawBoxes() {
  $('overlay').replaceChildren();
  const obs=run?.result?.observations.find(o=>o.feature_id===selectedFeature);
  for(const evidence of obs?.evidence || [])if(evidence.image_id===currentImage){
    const rect=document.createElementNS('http://www.w3.org/2000/svg','rect');
    ['x','y','width','height'].forEach((key,i)=>rect.setAttribute(key,evidence.bbox[i]*1000));
    const title=document.createElementNS(rect.namespaceURI,'title'); title.textContent=evidence.description; rect.append(title); $('overlay').append(rect);
  }
}
function selectFeature(key) {
  selectedFeature=key;
  document.querySelectorAll('.feature-row').forEach(e=>e.classList.toggle('selected',e.dataset.feature===key));
  const o=run?.result?.observations.find(o=>o.feature_id===key);
  $('detail').replaceChildren(); $('review-form').hidden=!o;
  if(!o){$('detail').append(element('h3','No usable observation selected')); drawBoxes(); return;}
  $('detail').append(element('h3',`${data.features[key].label}: ${valueText(o)}`),element('p',data.features[key].definition,'muted'),element('p',o.note),element('p',`Visibility: ${o.visibility.replaceAll('_',' ')} · Self-reported confidence: ${o.confidence??'not given'} (uncalibrated)`,'muted'));
  for(const ev of o.evidence){
    const button=element('button',`${ev.instance_id?ev.instance_id+' · ':''}${ev.description}`,'evidence-link');
    button.onclick=()=>{$('image-select').value=ev.image_id; chooseImage();}; $('detail').append(button);
  }
  const reviews=data.reviews.filter(r=>r.run_id===run.id && r.feature_id===key);
  for(const r of reviews)$('detail').append(element('p',`Review: ${r.verdict} — ${r.note} (${r.created})`,'muted'));
  if(!reviews.length)$('detail').append(element('p','No human review recorded.','muted'));
  if(o.evidence.length && !o.evidence.some(e=>e.image_id===currentImage)){
    $('image-select').value=o.evidence[0].image_id; chooseImage();
  }
  $('review-message').textContent=''; drawBoxes();
  document.querySelectorAll('#graph .node').forEach(n=>n.classList.toggle('selected',n.dataset.feature===key));
}
function renderGraph() {
  const svg=$('graph'); svg.replaceChildren();
  const specimen='specimen:'+$('specimen').value;
  const availableImages=new Set(data.images.filter(i=>i.specimen_id===$('specimen').value && (!run || run.image_ids.includes(i.id))).map(i=>i.id));
  // Keep originals reachable from cited crops while limiting this panel to the
  // selected run. The complete cross-run graph remains in the exports.
  for(const id of availableImages){const parent=data.images.find(i=>i.id===id)?.parent_id;if(parent)availableImages.add(parent);}
  const actionIds=new Set(data.hardware_actions?.filter(a=>availableImages.has(a.terminal?.image_id)).map(a=>a.action_id));
  const nodes=graph.nodes.filter(n=>n.id===specimen || n.run_id===run?.id || n.kind==='feature' && run?.result || availableImages.has(n.id) || actionIds.has(n.id));
  const columns={specimen:80,observation:420,feature:800,image:1160,model_run:1160,review:1160,capture_action:1160};
  const counts={}; const positions={};
  for(const n of nodes){const bucket=['image','model_run','review','capture_action'].includes(n.kind)?'sources':n.kind; const count=counts[bucket]||0; counts[bucket]=count+1; positions[n.id]=[columns[n.kind],n.kind==='specimen'?400:45+count*53];}
  for(const edge of graph.edges){
    const a=positions[edge.source],b=positions[edge.target]; if(!a||!b)continue;
    const line=document.createElementNS(svg.namespaceURI,'line'); Object.entries({x1:a[0],y1:a[1],x2:b[0],y2:b[1]}).forEach(([k,v])=>line.setAttribute(k,v));
    const title=document.createElementNS(svg.namespaceURI,'title'); title.textContent=edge.label; line.append(title);svg.append(line);
  }
  for(const n of nodes){
    const g=document.createElementNS(svg.namespaceURI,'g'); g.setAttribute('class','node'); g.setAttribute('transform',`translate(${positions[n.id].join(',')})`);g.setAttribute('tabindex','0');g.setAttribute('role','button');g.setAttribute('aria-label',`${n.kind}: ${n.label}`);g.dataset.feature=n.feature_id||'';
    const circle=document.createElementNS(svg.namespaceURI,'circle'); circle.setAttribute('r',n.kind==='specimen'?16:9);circle.setAttribute('fill',({specimen:'#234e50',observation:n.status==='unknown'?'#bb9456':'#4a9684',feature:'#627d93',image:'#a083a6',model_run:'#333f49',review:'#bc764b',capture_action:'#b68f55'})[n.kind]);
    const text=document.createElementNS(svg.namespaceURI,'text');text.setAttribute('x','17');text.setAttribute('y','4');text.textContent=n.label.length>36?n.label.slice(0,34)+'…':n.label;
    const activate=()=>{$('graph-info').textContent=`${n.kind.replaceAll('_',' ')} · ${n.id}`;if(n.feature_id)selectFeature(n.feature_id);if(n.image_id){if(!Array.from($('image-select').options).some(o=>o.value===n.image_id))$('image-select').append(new Option('Source original · '+n.image_id.slice(-6),n.image_id));$('image-select').value=n.image_id;chooseImage();}};
    g.onclick=activate;g.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();activate();}};g.append(circle,text);svg.append(g);
  }
  defaultBox=[0,0,1460,Math.max(900,...Object.values(counts).map(c=>c*53+60))];graphBox=[...defaultBox];setGraphBox();
}
function setGraphBox(){$('graph').setAttribute('viewBox',graphBox.join(' '));}
$('graph').addEventListener('wheel',e=>{e.preventDefault();const scale=e.deltaY>0?1.1:0.9;const w=Math.min(4000,Math.max(400,graphBox[2]*scale));const h=w*graphBox[3]/graphBox[2];graphBox=[graphBox[0]+(graphBox[2]-w)/2,graphBox[1]+(graphBox[3]-h)/2,w,h];setGraphBox();},{passive:false});
let drag=null;
$('graph').onpointerdown=e=>{if(e.target.closest('.node'))return;drag=[e.clientX,e.clientY,...graphBox];$('graph').setPointerCapture(e.pointerId);};
$('graph').onpointermove=e=>{if(!drag)return;const rect=$('graph').getBoundingClientRect();const scale=Math.max(drag[4]/rect.width,drag[5]/rect.height);graphBox=[drag[2]-(e.clientX-drag[0])*scale,drag[3]-(e.clientY-drag[1])*scale,drag[4],drag[5]];setGraphBox();};
$('graph').onpointerup=()=>drag=null;$('graph').onpointercancel=()=>drag=null;
$('graph-reset').onclick=()=>{graphBox=[...defaultBox];setGraphBox();};
$('specimen').onchange=chooseSpecimen;$('run').onchange=chooseRun;$('image-select').onchange=chooseImage;$('refresh').onclick=refresh;
$('review-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/reviews',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_id:run.id,feature_id:selectedFeature,verdict:$('verdict').value,note:$('review-note').value})});$('review-note').value='';await refresh();$('review-message').textContent='Review saved; original observation retained.';}catch(error){$('review-message').textContent=error.message;}};
function demoControls(){
  const busy=demoJob && !['complete','stopped','failed','interrupted'].includes(demoJob.state);
  if(busy){
    $('demo-provider').value=demoJob.provider_id;$('demo-source').value=demoJob.source;
    $('demo-mode').value=demoJob.mode;$('demo-count').value=String(demoJob.max_views);
    $('demo-attended').checked=demoJob.attended;
  }
  const live=$('demo-source').value==='live';
  const provider=demoProviders.find(p=>p.id===$('demo-provider').value);
  $('demo-provider-note').textContent=live && liveBlocked ? 'Live capture paused for the reported limit check. Saved images remain available.' : provider?.detail||'';
  for(const id of ['demo-provider','demo-source','demo-mode','demo-count','demo-attended'])$(id).disabled=!!busy;
  $('demo-mode').disabled=!!busy || !live;
  $('demo-attended').parentElement.hidden=!live;
  $('demo-start').disabled=!!busy || !provider?.available || (live && (liveBlocked || !$('demo-attended').checked));
  $('demo-stop').disabled=!busy || !!demoJob?.stop_requested;
}
async function pollDemo(){
  if(demoPolling)return;demoPolling=true;
  try{
    const response=await api('/api/demos');demoProviders=response.providers;liveBlocked=!!response.live_blocked;
    const current=$('demo-provider').value;
    options($('demo-provider'),demoProviders.map(p=>[p.id,p.label+(p.available?'':' — not configured')]),current);
    for(const option of $('demo-provider').options)option.disabled=!demoProviders.find(p=>p.id===option.value)?.available;
    demoJob=response.jobs.at(-1)||null;demoControls();
    if(demoJob){
      $('demo-state').textContent=demoJob.state.replaceAll('_',' ');
      $('demo-message').textContent=demoJob.stop_requested && !['complete','stopped','failed','interrupted'].includes(demoJob.state) ? 'Stopping; no further movement will be started…' : demoJob.message;
      $('demo-progress').max=demoJob.max_views;$('demo-progress').value=demoJob.views_captured??demoJob.image_ids.length;
      $('demo-counts').textContent=`${demoJob.views_captured??demoJob.image_ids.length}/${demoJob.max_views} photos captured · ${demoJob.views_completed} analysed · ${demoJob.batches_completed??demoJob.run_ids.length} valid model responses${demoJob.batches_failed ? ` · ${demoJob.batches_failed} failed batches retained` : ''}${demoJob.roi_profile ? ' · specimen ROI' : ''}`;
      const duration=seconds=>`${Math.floor(seconds/60)}m ${Math.round(seconds%60)}s`;
      if(demoJob.acquisition_wall_seconds!==undefined)$('demo-counts').textContent+=` · capture ${duration(demoJob.acquisition_wall_seconds)}`;
      if(demoJob.analysis_wall_seconds!==undefined)$('demo-counts').textContent+=` · analysis ${duration(demoJob.analysis_wall_seconds)}`;
      const signature=JSON.stringify([demoJob.id,demoJob.state,demoJob.views_completed,demoJob.current_run_id,demoJob.image_ids]);
      if(signature!==demoSignature){
        demoSignature=signature;
        await refresh();
        $('specimen').value=demoJob.specimen_id;chooseSpecimen();
        const selected=[...demoJob.run_ids].reverse().find(id=>data.runs.some(r=>r.id===id && r.state==='usable')) || demoJob.current_run_id;
        if(selected && data.runs.some(r=>r.id===selected)){ $('run').value=selected;chooseRun(); }
        $('demo-thumbnails').replaceChildren(...demoJob.image_ids.map((id,i)=>{
          const button=element('button');const img=element('img');img.src='/api/images/'+id;img.alt='Demo view '+(i+1);
          button.append(img,element('span','View '+(i+1)));button.onclick=()=>{
            if(!Array.from($('image-select').options).some(o=>o.value===id))$('image-select').append(new Option('Demo view '+(i+1),id));
            $('image-select').value=id;chooseImage();
          };return button;
        }));
      }
    }
  }catch(e){$('demo-message').textContent=e.message;}
  finally{demoPolling=false;}
}
for(const id of ['demo-provider','demo-source','demo-attended'])$(id).onchange=demoControls;
$('demo-start').onclick=async()=>{
  $('demo-start').disabled=true;
  try{
    demoJob=await api('/api/demos/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      specimen_id:$('specimen').value,provider_id:$('demo-provider').value,source:$('demo-source').value,
      mode:$('demo-mode').value,max_views:Number($('demo-count').value),attended:$('demo-attended').checked})});
    demoSignature='';await pollDemo();
  }catch(e){$('demo-message').textContent=e.message;demoControls();}
};
$('demo-stop').onclick=async()=>{
  if(!demoJob)return;
  $('demo-stop').disabled=true;
  try{await api('/api/demos/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:demoJob.id})});$('demo-message').textContent='Stopping; no further movement will be started…';}
  catch(e){$('demo-message').textContent=e.message;}
};
refresh().then(pollDemo);setInterval(pollDemo,1500);
