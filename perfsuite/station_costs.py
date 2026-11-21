#!/usr/bin/env python3
"""Report individual q-basepack station costs with the original full causal realism interval.

Use original archives. Each window must lie wholly inside the selected view interval.
This tool leaves the suite's independent whole-run verdict untouched.
"""
import argparse
import json
from pathlib import Path
import re
from statistics import median
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'bin'))
import harness_realism
import score

def read_windows(archive):
    # The native per-view logger clears a histogram at emission. If BOTH the
    # previous and current emissions are within a station, no prior view
    # frames can enter that station's next histogram.
    windows = []
    previous = {}
    step = None
    trips = 0
    with (archive/'latest.log').open(errors='replace') as stream:
        for line in stream:
            if '[MCDevLab]' not in line and 'Orbital Atlas stopped after an error in ' in line:
                trips += 1
            if '[MCDevLab] step ' in line:
                anchor = re.search(r'\[MCDevLab\] step (\d+) start:', line)
                if anchor:
                    step = int(anchor[1])
            if step is None or 'mapPerf ' not in line:
                continue
            row = score.parse_mapperf_line(line)
            stamp = harness_realism.clock_ms(line)
            if row and stamp is not None:
                view = row['view']
                if view in previous:
                    start, last_time = previous[view]
                    if start <= step and 0 < stamp-last_time <= 15000:
                        windows.append((start,step,row))
                previous[view] = step,stamp
    return windows,trips

def collect(archive):
    result = json.loads((archive/'result.json').read_text())
    meta = json.loads((archive/'meta.json').read_text())
    if not re.fullmatch(r'atlas-ps-q-basepack-(mac|fd|gpc|ct224)',result['scenario']['name']):
        raise ValueError('A canonical q-basepack archive is required')
    raw = harness_realism.analyze_run(archive)
    windows,trips = read_windows(archive)
    steps = result['steps']
    indices = [s['index'] for s in steps]
    if indices != list(range(len(steps))):
        raise ValueError('Nonconsecutive step indices')
    samples,problem = harness_realism.read_samples(archive, steps)
    settings = dict(v.split('=',1) for v in meta['props'].splitlines() if '=' in v)
    fixture = meta['fixture']
    if isinstance(fixture,dict):
        fixture = fixture.get('fixture')
    receipt_path=archive/'recipe-receipt.json'
    receipt=json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
    qualified = (score.effective_verdict(result,receipt) in ('PASS','WARN_SLOW_EXIT') and
                 not result['scenario']['name'].endswith('-ct224') and not problem and trips==0 and
                 str(fixture).endswith('-consistent') and all(settings.get('orbitalatlas.'+k)=='true'
                 for k in ('playProfile','diag','mapPerfLog','harnessCensusLite')))
    def summarize(name,view,first,stop,station):
        relevant=[row for start,end,row in windows if row['view']==view and first<=start<=end<stop]
        metric={}
        for field in ('atlas_cpu_p95','frame_p95'):
            values=[row[field] for row in relevant if row.get(field) is not None]
            metric[field]=median(values) if values else None
        return dict(label=name,view=view,start_step=first,stop_before_step=stop,
                    captured=station['captured_chunks'],changed=station['changed_chunks'],
                    requests=station['recapture_requests'],representative=station['representative'],
                    contained_windows=len(relevant),observed_ms=metric,
                    eligible=bool(qualified and station['representative'] is True and all(v is not None for v in metric.values())))
    rows=[]
    for station in raw['stations']:
        first=station['open_map_step']
        stop=next((i for i in range(first+1,len(steps)) if steps[i]['type'] in (
            'orbitalatlas:open_map','orbitalatlas:close_map','quit') or harness_realism.is_teleport(steps[i])),len(steps))
        shots=[s['args']['name'] for s in steps[first:stop] if s['type']=='screenshot']
        measures=[s['args']['label'] for s in steps[first:stop] if s['type']=='measure']
        name=(shots or measures or [f'close_{first}'])[0]
        rows.append(summarize(name,'worldmap',first,stop,station))
    # A ten-second span occasionally emits only one full native window. The
    # prespecified contiguous 64+128 warm sweep also admits windows crossing
    # the span transition, provided BOTH complete stations passed realism.
    warm=[r for r in rows if r['label'] in ('wm2_64','wm2_128')]
    if (len(warm)==2 and warm[0]['label']=='wm2_64' and
            warm[0]['stop_before_step']==warm[1]['start_step']):
        last=next(s for s in raw['stations'] if s['open_map_step']==warm[1]['start_step'])
        combined=dict(last,representative=all(r['representative'] is True for r in warm))
        rows.append(summarize('wm2_64_128','worldmap',warm[0]['start_step'],warm[1]['stop_before_step'],combined))
    # Minimap landing/revisit census is anchored by the same scorer as the
    # per-run table. Include all work since world entry or its prior real
    # map close, including every travel and settle step before this station.
    for phase in ('land','revisit'):
        indices=[s['index'] for s,ty,args,t,ctx in score.walk_steps(result)
                 if ctx['phase']==phase and s['type']=='orbitalatlas:minimap_state']
        if not indices:
            continue
        start=indices[0]
        anchor='screenshot' if phase=='land' else 'teleport'
        first=next((i for i in range(start,-1,-1) if steps[i]['type']==anchor),None)
        if first is None:
            continue
        prior=next((s for s in reversed(steps[:first]) if s['type'] in ('open_world','orbitalatlas:close_map')),None)
        if not prior:
            continue
        t0=prior['started_ms']+(prior['duration_ms'] if prior['type']=='orbitalatlas:close_map' else 0)
        last=steps[indices[-1]]
        station=harness_realism.interval(samples,t0,last['started_ms']+last['duration_ms'])
        rows.append(summarize(phase,'minimap',first,indices[-1]+1,station))
    return dict(archive=str(archive),pin=meta.get('candidate_sha256'),fixture=fixture,
                whole_run_representative=raw['detail_timing_representative'],map_failsafe_trips=trips,
                telemetry_error=problem,stations=rows)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive',type=Path)
    print(json.dumps(collect(parser.parse_args().archive),indent=2))
