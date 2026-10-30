"""Automatic capture -> model -> graph demonstration with durable progress."""
import fcntl
import json
import threading
from pathlib import Path

from . import hardware, providers, route, roi, pipeline
from .motion import DEMO_TRAVEL_STEPS
from .store import canonical, now, uid, write_new

TERMINAL={'complete','stopped','failed','interrupted'}


class StopSignal:
    def __init__(self,path,*other_paths):self.path=path;self.paths=(path,*other_paths)
    def is_set(self):return any(p.exists() for p in self.paths)


def tour_choice(options,round_number,directions=None):
    directions=directions if directions is not None else {'Y':'PLUS','Z':'PLUS'}
    axis='Y' if round_number%2 else 'Z'
    preferred=(50,25,10)[((round_number-1)//2)%3] if axis=='Y' else 50
    sizes=list(dict.fromkeys([preferred,25,10,50]))
    for a in (axis,'Z' if axis=='Y' else 'Y'):
        for sign in (directions[a],'MINUS' if directions[a]=='PLUS' else 'PLUS'):
            for size in sizes:
                view=f'{a}_{sign}_{size}_STEPS'
                if view in options:
                    directions[a]=sign
                    return view
    return None


class DemoManager:
    def __init__(self,store):
        self.store=store
        self.root=store.root/'demos';self.root.mkdir(exist_ok=True)
        self.mutex=threading.RLock()
        # A process lost during a demo is never automatically resumed.
        with (self.root/'owner.lock').open('a') as probe:
            try:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            for job in self.list():
                if job['state'] not in TERMINAL:
                    self.update(job,state='interrupted',message='Previous worker ended; inspect records before another live demo')

    def list(self):
        jobs=[json.loads(p.read_text()) for p in self.root.glob('demo_*/progress.json')]
        for job in jobs:job['stop_requested']=(self.root/job['id']/'STOP').exists()
        return sorted(jobs,key=lambda d:d['created'])

    def get(self,job_id):
        if not isinstance(job_id,str) or not job_id.startswith('demo_') or not job_id[5:].isalnum():
            raise ValueError('Invalid demo ID')
        path=self.root/job_id/'progress.json'
        if not path.exists():raise ValueError('Unknown demo')
        return json.loads(path.read_text())

    def update(self,job,**changes):
        with self.mutex:
            job.update(changes,updated=now())
            folder=self.root/job['id'];folder.mkdir(exist_ok=True)
            temp=folder/'progress.tmp'
            temp.write_text(canonical(job)+'\n');temp.replace(folder/'progress.json')
            with (folder/'events.jsonl').open('a') as out:
                out.write(canonical({'at':job['updated'],'state':job['state'],'message':job['message'],
                                     'views_completed':job['views_completed'],'current_run_id':job.get('current_run_id')})+'\n')

    def start(self,specimen_id,provider_id,source='live',mode='tour',max_views=40,attended=False,resume_job_id=None,analysis_workers=None):
        selected=providers.provider(provider_id)
        if analysis_workers is None:analysis_workers=4 if provider_id=='codex' and mode=='tour' else 1
        if type(analysis_workers) is not int or analysis_workers not in (1,2,4):raise ValueError('Analysis workers must be 1, 2 or 4')
        if source not in {'live','recorded'} or mode not in {'tour','adaptive'} or type(max_views) is not int or not 1<=max_views<=40:
            raise ValueError('Invalid demo options')
        if source=='live' and attended is not True:raise ValueError('Live demos require the operator beside the scanner')
        if source=='live' and (self.store.root/'STOP').exists():raise ValueError('Live capture is paused; inspect the reported limit before clearing STOP')
        snapshot=self.store.snapshot()
        if not any(s['id']==specimen_id for s in snapshot['specimens']):raise ValueError('Select a specimen first')
        if source=='recorded':
            available=sum(i['specimen_id']==specimen_id and i['role']=='original' for i in snapshot['images'])
            if available<max_views:raise ValueError(f'Only {available} saved photos are available; choose fewer photos or use the live scanner')
        calibrated=route.load_if_available() if source=='live' else None
        if calibrated and mode!='tour':raise ValueError('Use Automatic tour for the mapped scan route')
        with self.mutex:
            lock=(self.root/'owner.lock').open('a')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close();raise ValueError('A demo is already running')
            try:
                if calibrated:
                    if resume_job_id:
                        calibrated=route.verify_frozen(self.get(resume_job_id)['calibrated_route'],calibrated)
                    else:
                        calibrated=route.from_position(calibrated,hardware.reference_position(self.store.snapshot()['hardware_actions'],calibrated['guard']))
                reused,travel=self.continuation(resume_job_id,specimen_id,source,calibrated,max_views)
                roi_profile=roi.load_profile(specimen_id)
            except Exception:
                lock.close();raise
            job=dict(id=uid('demo'),created=now(),specimen_id=specimen_id,provider_id=provider_id,
                     provider_label=selected['label'],model=selected['model'],source=source,mode=mode,
                     max_views=max_views,attended=attended,state='queued',message='Starting demo',
                     views_completed=0,views_captured=0,image_ids=[],original_ids=[],run_ids=[],current_run_id=None,
                     batch_size=5 if mode=='tour' else 1,batches_completed=0,batches_failed=0,views_attempted=0,tour_policy='mixed-40-photos-v1',
                     resume_job_id=resume_job_id,reused_original_ids=reused,roi_profile=roi_profile,roi_records=[],
                     pipeline='capture-first-v2' if mode=='tour' and (calibrated or source=='recorded') else 'legacy-sequential-v1',
                     analysis_workers=analysis_workers,
                     travel_steps=travel,max_travel_steps=DEMO_TRAVEL_STEPS,qualification='method_demonstration_unverified_observations')
            job['session_id']=job['id'].replace('_','-')
            if calibrated:
                job.update(calibrated_route=calibrated,tour_policy='operator-mapped-eight-by-five-v1',
                           batch_size=4 if provider_id=='claude' else 5)
            folder=self.root/job['id'];folder.mkdir()
            write_new(folder/'request.json',(canonical(job)+'\n').encode())
            self.update(job)
            thread=threading.Thread(target=self.run,args=(job,lock),name=job['id'],daemon=True)
            thread.start()
            return self.get(job['id'])

    def continuation(self,job_id,specimen_id,source,calibrated,max_views):
        """Explicit new job using an immutable photo prefix; never replay its moves."""
        if job_id is None:return [],0
        if source!='live' or not calibrated:raise ValueError('Continuation requires the mapped live route')
        previous=self.get(job_id)
        terminal=self.root/job_id/'terminal.json'
        if previous['state'] not in TERMINAL or not terminal.exists():raise ValueError('Previous demo is not terminal')
        previous=json.loads(terminal.read_text())
        if previous['specimen_id']!=specimen_id or previous.get('calibrated_route')!=calibrated:
            raise ValueError('Continuation specimen or calibration differs')
        ids=previous['original_ids'];count=len(ids)
        if not 0<count<max_views or len(set(ids))!=count:raise ValueError('Continuation needs a unique incomplete photo prefix')
        for index,image_id in enumerate(ids):
            image=self.store.image(image_id);pose=image['pose']
            if (image['role']!='original' or image['specimen_id']!=specimen_id or pose.get('route_photo')!=index+1
                or pose.get('calibration_sha256')!=calibrated['guard']['sha256']
                or pose.get('firmware_position')!=calibrated['plan']['photos'][index]['target']):
                raise ValueError('Saved photo does not match the mapped prefix')
            self.store.image_path(image_id,verify=True)
        expected=calibrated['plan']['photos'][count-1]['target']
        actions=self.store.snapshot()['hardware_actions']
        if hardware.reference_position(actions,calibrated['guard'])!=expected:
            raise ValueError('Position changed after the last saved photo; continuation cannot move')
        completed_images={a['terminal'].get('image_id') for a in actions if a.get('terminal',{}).get('state')=='completed'}
        if not set(ids)<=completed_images:raise ValueError('Saved prefix lacks completed capture actions')
        travel=sum(abs(move['steps']) for photo in calibrated['plan']['photos'][:count] for move in photo['transit_commands'])
        if previous['travel_steps']!=travel:raise ValueError('Travel after the last photo is unresolved')
        return ids,travel

    def stop(self,job_id):
        with self.mutex:
            job=self.get(job_id)
            if job['state'] in TERMINAL:return job
            (self.root/job_id/'STOP').touch(exist_ok=True)
            return dict(job,stop_requested=True)

    def run(self,job,lock):
        if job.get('pipeline')!='capture-first-v2':
            return self.run_legacy(job,lock)
        stop=StopSignal(self.root/job['id']/'STOP',*([self.store.root/'STOP'] if job['source']=='live' else []))
        try:
            pipeline.execute(self,job,stop)
            self.update(job,state='stopped' if stop.is_set() else 'complete',finished=now(),
                message='Demo stopped; images and responses saved' if stop.is_set() else
                (f'Capture complete · {job["views_completed"]}/{job["views_captured"]} photos have validated observations · {job["batches_failed"]} failed batches retained'
                 if job['batches_failed'] else 'Demo complete · all photos saved before analysis · graph updated'))
        except Exception as exc:
            self.update(job,state='stopped' if stop.is_set() else 'failed',finished=now(),
                message='Demo stopped; no further movement' if stop.is_set() else str(exc))
        finally:
            try:write_new(self.root/job['id']/'terminal.json',(canonical(job)+'\n').encode())
            finally:lock.close()

    def run_legacy(self,job,lock):
        stop=StopSignal(self.root/job['id']/'STOP')
        try:
            if job['source']=='recorded':
                records=sorted([i for i in self.store.snapshot()['images'] if i['specimen_id']==job['specimen_id'] and i['role']=='original'],key=lambda i:i['created'])[-job['max_views']:]
                if not records:raise ValueError('No saved specimen images available')
            next_move='CURRENT';directions={'Y':'PLUS','Z':'PLUS'};pending=[]
            for index in range(job['max_views']):
                if stop.is_set():break
                if index<len(job.get('reused_original_ids',[])):
                    self.update(job,state='capturing',message=f'Cropping saved photo {index+1}/{len(job["reused_original_ids"])} · no movement')
                    original=self.store.image(job['reused_original_ids'][index])
                elif job['source']=='live':
                    if job.get('calibrated_route'):
                        photo=job['calibrated_route']['plan']['photos'][index]
                        self.update(job,state='capturing',message=f'Capturing photo {index+1}/{job["max_views"]} · tilt position {photo["tilt_band"]}/5')
                        original=route.capture_photo(self.store,job['specimen_id'],job['calibrated_route'],index,job['session_id'],stop,
                            on_move=lambda steps:self.update(job,travel_steps=job['travel_steps']+steps))
                    else:
                        original=self.capture_legacy(job,index,next_move)
                else:
                    if index>=len(records):break
                    self.update(job,state='capturing',message=f'Loading saved view {index+1}/{len(records)}')
                    original=records[index]
                job['original_ids'].append(original['id'])
                model_image,roi_record=roi.prepare(self.store,original,job.get('roi_profile'))
                image_id=model_image['id']
                job['roi_records'].append(dict(roi_record,image_id=image_id,photo=index+1))
                job['image_ids'].append(image_id);pending.append(image_id)
                self.update(job,views_captured=index+1)
                if stop.is_set():break
                choices=[]
                if job['source']=='live' and not job.get('calibrated_route') and index+1<job['max_views']:
                    choices=hardware.movement_choices(self.store,job['session_id'],max_travel_steps=job['max_travel_steps'])['views']
                next_move=tour_choice(choices,index+1,directions)
                last_view=index+1==job['max_views'] or (job['source']=='recorded' and index+1==len(records)) or (job['source']=='live' and not job.get('calibrated_route') and not next_move)
                if len(pending)<job['batch_size'] and not last_view:continue
                offered=choices if job['mode']=='adaptive' else []
                self.update(job,state='observing',message=f'{job["provider_label"]} is observing photos {index+2-len(pending)}–{index+1}/{job["max_views"]}')
                result=providers.observe(self.store,job['specimen_id'],list(pending),job['provider_id'],
                    next_views=offered,timeout=240,cancel=stop,
                    on_started=lambda run_id:self.update(job,current_run_id=run_id))
                job['run_ids'].append(result['id'])
                if stop.is_set():break
                batch_count=len(pending)
                if result['state']!='usable':
                    if job['mode']=='adaptive':raise ValueError('Model response failed validation; raw response retained in '+result['id'])
                    # A fixed tour has no model-derived motion. Keep failed evidence
                    # and continue the planned photos without retrying any movement.
                    pending=[]
                    self.update(job,batches_failed=job['batches_failed']+1,views_attempted=job['views_attempted']+batch_count,
                                message='Model batch failed validation; response retained · continuing planned photos')
                    if last_view:break
                    continue
                pending=[]
                self.update(job,state='updating_graph',views_completed=job['views_completed']+batch_count,
                            views_attempted=job['views_attempted']+batch_count,batches_completed=job['batches_completed']+1,
                            message=f'{job["views_completed"]+batch_count} photos observed · graph updated')
                if job['mode']=='adaptive':next_move=result['result']['next_view_id']
                if index+1==job['max_views']:self.update(job,message='Demo complete · unknown features are retained')
                if job['source']=='live' and not job.get('calibrated_route') and not next_move:break
            self.update(job,state='stopped' if stop.is_set() else 'complete',finished=now(),
                        message='Demo stopped; no further movement' if stop.is_set() else
                        (f'Capture complete · {job["views_completed"]}/{job["views_captured"]} photos have validated observations · {job["batches_failed"]} failed model batches retained'
                         if job['batches_failed'] else 'Demo complete · ROI observations and graph saved'))
        except Exception as exc:
            self.update(job,state='stopped' if stop.is_set() else 'failed',finished=now(),
                        message='Demo stopped; no further movement' if stop.is_set() else str(exc))
        finally:
            try:write_new(self.root/job['id']/'terminal.json',(canonical(job)+'\n').encode())
            finally:lock.close()

    def capture_legacy(self,job,index,next_move):
        if index:
            if not next_move:
                raise ValueError('No further legacy movement available')
            parent=job['run_ids'][-1] if job['run_ids'] else None
            write_new(self.root/job['id']/f'view-{index+1}-decision.json',
                      (canonical({'parent_run_id':parent,'view':next_move,'selected_by':job['mode'],'at':now()})+'\n').encode())
        self.update(job,state='capturing',message=f'Capturing view {index+1}/{job["max_views"]}'+(f' · {next_move}' if index else ' · current position'))
        original=hardware.capture(self.store,job['specimen_id'],next_move,attended=True,session_id=job['session_id'],max_travel_steps=job['max_travel_steps'])
        job['travel_steps']+=abs(hardware.VIEWS[next_move][1])
        return original
