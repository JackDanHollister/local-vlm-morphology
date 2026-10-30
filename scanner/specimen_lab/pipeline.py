"""Capture the complete image set, close hardware, then run bounded observers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import time

from . import providers, roi, route
from .session import CaptureSession
from .store import now


def execute(manager, job, stop):
    store = manager.store
    started = time.monotonic()
    manager.update(job, state='capturing', acquisition_started=now(),
                   message='Capturing the full image set before analysis')
    records = []
    if job['source'] == 'recorded':
        records = sorted([i for i in store.snapshot()['images'] if i['specimen_id'] == job['specimen_id']
                          and i['role'] == 'original'], key=lambda i: i['created'])[-job['max_views']:]
    context = (CaptureSession(store, job['specimen_id'], job['session_id'], job['calibrated_route']['guard'], stop)
               if job['source'] == 'live' else nullcontext(None))
    with context as session:
        for index in range(job['max_views']):
            if stop.is_set():
                break
            if index < len(job['reused_original_ids']):
                original = store.image(job['reused_original_ids'][index])
            elif job['source'] == 'recorded':
                original = records[index]
            else:
                photo = job['calibrated_route']['plan']['photos'][index]
                manager.update(job, message=f'Capturing photo {index+1}/{job["max_views"]} · tilt position {photo["tilt_band"]}/5')
                original = route.capture_photo(store, job['specimen_id'], job['calibrated_route'], index,
                    job['session_id'], stop, capture=session.capture,
                    on_move=lambda steps: manager.update(job, travel_steps=job['travel_steps']+steps))
            job['original_ids'].append(original['id'])
            model_image, decision = roi.prepare(store, original, job.get('roi_profile'))
            job['image_ids'].append(model_image['id'])
            job['roi_records'].append(dict(decision, image_id=model_image['id'], photo=index+1))
            manager.update(job, views_captured=index+1)
    manager.update(job, acquisition_finished=now(), acquisition_wall_seconds=round(time.monotonic()-started, 3),
                   hardware_closed=True, message='Image set saved · hardware closed')
    if stop.is_set():
        return
    if len(job['image_ids']) != job['max_views']:
        raise ValueError('Incomplete image set; analysis not started')
    batches = [dict(index=i//job['batch_size']+1, image_ids=job['image_ids'][i:i+job['batch_size']],
                    state='queued', run_id=None) for i in range(0, len(job['image_ids']), job['batch_size'])]
    manager.update(job, state='observing', batches=batches, active_run_ids=[], analysis_started=now(),
                   message=f'All {job["max_views"]} photos saved · analysing {len(batches)} batches')
    started = time.monotonic()

    def started_run(batch, run_id):
        with manager.mutex:
            batch.update(state='running', run_id=run_id, started=now())
            manager.update(job, current_run_id=run_id,
                active_run_ids=[b['run_id'] for b in batches if b['state']=='running'],
                run_ids=[b['run_id'] for b in batches if b['run_id']],
                message=f'{job["views_completed"]}/{job["max_views"]} photos analysed · {sum(b["state"]=="running" for b in batches)} batches running')

    def observe_batch(batch):
        if stop.is_set():
            with manager.mutex:
                batch.update(state='cancelled', finished=now())
                manager.update(job)
            return
        result = None
        error = None
        try:
            result = providers.observe(store, job['specimen_id'], batch['image_ids'], job['provider_id'],
                next_views=[], timeout=240, cancel=stop,
                on_started=lambda run_id: started_run(batch, run_id))
        except Exception as exc:
            error = str(exc)
            # Complete any request created before a transport/launcher exception.
            if batch['run_id'] and store.run(batch['run_id'])['state'] == 'running':
                raw_path = store.root / 'runs' / batch['run_id'] / 'emitted-response.txt'
                result = store.finish_run(batch['run_id'], raw_path.read_text() if raw_path.exists() else '',
                                          execution_error=error)
        with manager.mutex:
            if result is not None:
                batch['run_id'] = result['id']
            usable = result is not None and result['state'] == 'usable'
            cancelled = stop.is_set() and not usable
            batch.update(state='usable' if usable else 'cancelled' if cancelled else 'failed', finished=now(), error=error)
            manager.update(job,
                views_attempted=job['views_attempted']+len(batch['image_ids']),
                views_completed=job['views_completed']+(len(batch['image_ids']) if usable else 0),
                batches_completed=job['batches_completed']+int(usable),
                batches_failed=job['batches_failed']+int(not usable and not cancelled),
                run_ids=[b['run_id'] for b in batches if b['run_id']],
                active_run_ids=[b['run_id'] for b in batches if b['state']=='running'],
                message=f'{job["views_completed"]+(len(batch["image_ids"]) if usable else 0)}/{job["max_views"]} photos analysed · {sum(b["state"]=="running" for b in batches)} batches running')

    with ThreadPoolExecutor(max_workers=job['analysis_workers'], thread_name_prefix=job['id']+'-observe') as pool:
        futures = [pool.submit(observe_batch, batch) for batch in batches]
        for future in futures:
            future.result()
    manager.update(job, analysis_finished=now(), analysis_wall_seconds=round(time.monotonic()-started, 3))
