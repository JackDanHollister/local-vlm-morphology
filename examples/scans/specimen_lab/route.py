"""Validated operator-mapped photo route. Never infer motor limits from model output."""
import hashlib
import json
import copy

from . import hardware
from .motion import check_target,validate_limits,DEMO_TRAVEL_STEPS
from .store import canonical


def validate(calibration,plan):
    if calibration.get('schema')!='scanner-operator-calibration-v1' or plan.get('schema')!='scanner-40-photo-plan-v1':
        raise ValueError('Unknown calibrated route schema')
    if calibration['steps_per_firmware_unit']!=80 or calibration['Y']['commanded_steps_per_turn']!=200:
        raise ValueError('Unexpected operator rotation calibration')
    if plan['start_position']!=calibration['counter_frame']['current_position']:
        raise ValueError('Photo route and calibration have different starting references')
    photos=plan['photos']
    if len(photos)!=40 or plan['photo_count']!=40:raise ValueError('Expected 40 photo positions')
    lo,hi=calibration['Z']['operator_viewing_range']
    margin=calibration['Z']['demo_endpoint_margin_steps']
    if type(margin) is not int or margin<10:raise ValueError('Z endpoints require a margin of at least 10 steps')
    photo_lo,photo_hi=lo+margin/80,hi-margin/80
    start=plan['start_position'];y_start=start['Y']
    limits=validate_limits({'Y':[y_start,y_start+175/80],'Z':[lo,photo_hi]})
    current=dict(start);total=0;seen=set()
    for index,photo in enumerate(photos):
        if photo['photo']!=index+1:raise ValueError('Nonsequential photo route')
        band=index//8
        phase=(index%8 if band%2==0 else 7-index%8)*25
        target={'Y':y_start+phase/80,'Z':photo_lo+(photo_hi-photo_lo)*band/4}
        if photo['target']!=target or photo['tilt_band']!=band+1 or photo['Y_phase_degrees']!=phase*1.8:
            raise ValueError('Photo route differs from the eight-angle/five-tilt layout')
        if not photo['transit_commands']:raise ValueError('Expected a bounded transit before each photo')
        for move in photo['transit_commands']:
            if set(move)!={'axis','steps'}:raise ValueError('Unknown transit fields')
            current=check_target(current,move['axis'],move['steps'],limits)
            total+=abs(move['steps'])
        if current!=target:raise ValueError('Transit does not reach its recorded photo target')
        if not photo_lo<=current['Z']<=photo_hi:raise ValueError('Photo is too close to a Z endpoint')
        seen.add(tuple(current.values()))
    if len(seen)!=40 or total!=plan['total_commanded_travel_steps'] or total>DEMO_TRAVEL_STEPS:
        raise ValueError('Duplicate photo positions or travel budget mismatch')
    return limits


def load_if_available():
    folder=hardware.PROJECT/'config'
    cal_path=folder/'scanner_calibration.json'
    if not cal_path.exists():return None
    raw=cal_path.read_bytes();cal=json.loads(raw)
    plan_raw=(folder/'specimen_40_photo_plan.json').read_bytes();plan=json.loads(plan_raw)
    limits=validate(cal,plan)
    return dict(plan=plan,calibration=cal,guard=dict(limits=limits,reference_position=plan['start_position'],
        recorded_at=cal['recorded_at'],sha256=hashlib.sha256(raw).hexdigest()),
        plan_sha256=hashlib.sha256(plan_raw).hexdigest())


def from_position(base, position):
    """Reuse the same 40 mapped targets in either direction, without a return jog."""
    if position == base['plan']['start_position']:
        return copy.deepcopy(base)
    photos = base['plan']['photos']
    if position == photos[-1]['target']:
        reverse = True
    elif position == photos[0]['target']:
        reverse = False
    else:
        raise ValueError('Current position is not a mapped route endpoint; inspect before a new scan')
    result = copy.deepcopy(base)
    ordered = list(reversed(photos)) if reverse else photos
    result['plan']['photos'] = []
    current = dict(position)
    total = 0
    for index, source in enumerate(ordered):
        photo = copy.deepcopy(source)
        if index == 0:
            moves = []
        elif reverse:
            moves = [dict(axis=m['axis'], steps=-m['steps']) for m in reversed(ordered[index-1]['transit_commands'])]
        else:
            moves = copy.deepcopy(source['transit_commands'])
        for move in moves:
            current = check_target(current, move['axis'], move['steps'], base['guard']['limits'])
            total += abs(move['steps'])
        if current != photo['target']:
            raise ValueError('Derived route transit mismatch')
        photo.update(photo=index+1, source_photo=source['photo'], transit_commands=moves)
        result['plan']['photos'].append(photo)
    if total > DEMO_TRAVEL_STEPS:
        raise ValueError('Derived route exceeds movement budget')
    result['plan'].update(schema='scanner-40-photo-plan-v2', start_position=position,
        direction='reverse' if reverse else 'forward', total_commanded_travel_steps=total)
    result['source_plan_sha256'] = base['plan_sha256']
    result['plan_sha256'] = hashlib.sha256(canonical(result['plan']).encode()).hexdigest()
    return result


def verify_frozen(candidate, base):
    if candidate != from_position(base, candidate['plan']['start_position']):
        raise ValueError('Frozen route differs from the current validated mapping')
    return candidate


def capture_photo(store,specimen_id,route,photo_index,session_id,stop,on_move=None,capture=None):
    photo=route['plan']['photos'][photo_index];moves=photo['transit_commands'];actions=[]
    planned_start=route['plan']['start_position'] if photo_index==0 else route['plan']['photos'][photo_index-1]['target']
    recorded=hardware.reference_position(store.snapshot()['hardware_actions'],route['guard'])
    if recorded!=planned_start:raise ValueError('Recorded position differs from the next planned photo; no transit started')
    capture = capture or hardware.capture
    # A repeated scan starts by photographing its current endpoint, with no jog.
    moves = moves or [dict(axis=None, steps=0)]
    for index,move in enumerate(moves):
        if stop.is_set():raise ValueError('Demo stopped before next transit step')
        view=f'{move["axis"]}_{"PLUS" if move["steps"]>0 else "MINUS"}_{abs(move["steps"])}_STEPS' if move['steps'] else 'CURRENT'
        final=index+1==len(moves)
        result=capture(store,specimen_id,view,attended=True,session_id=session_id,
            max_travel_steps=DEMO_TRAVEL_STEPS,calibration=route['guard'],take_photo=final,cancel=stop,
            pose_extra=dict(route_photo=photo_index+1,tilt_band=photo['tilt_band'],
                Y_phase_degrees=photo['Y_phase_degrees'],calibration_sha256=route['guard']['sha256'],
                transit_action_ids=list(actions),photo_target=photo['target']))
        if not final:actions.append(result['action_id'])
        if on_move:on_move(abs(move['steps']))
    if result['pose']['firmware_position']!=photo['target']:raise ValueError('Photo position does not match calibrated route')
    return result
