"""Supplement a canonical q-texture scorecard with bounds on distinct selected tile addresses.

rc.172 detT and surfT count selected native and surface *addresses*, respectively.
detS counts visible native sections. The two address sets may overlap. Neither
selection nor a visible section measures actual textured screen pixels.
"""
import argparse
import json
import math
from pathlib import Path
import re

def active_census(message):
    route=re.search(r'\bregional=(true|false)\b',message)
    mode=re.search(r'\bwmMode=(ordinary|regional)\b',message)
    if not route or not mode or (route[1]=='true')!=(mode[1]=='regional'):
        return None
    if not all(s in message for s in (' dim=minecraft:overworld ', ' situation=OUTDOORS ',
                                       ' interior=none ', 'section[place=false on=false ')):
        return None
    name='lastRegional' if route[1]=='true' else 'lastMap'
    scene=re.search(r'\b'+name+r'=(\S+)',message)
    if not scene:
        return None
    fields=dict(item.split(':',1) for item in scene[1].split(',') if ':' in item)
    if not all(re.fullmatch(r'\d+',fields.get(k,'')) for k in ('view','detT','detS','surfT')):
        return None
    return mode[1],scene[1],int(fields['detT']),int(fields['surfT']),int(fields['detS'])

REVIEWED_TILE_SELECTION = {
    '3bc7a5f8bc884f2f85a92c5dc8e4d407f000cc5d',  # rc.172
    '3493aa6910c1f60467302faa4fb37da6d87cae74',  # physical native readiness cache, same draw selection and counter paths
    'b10ced7c0b1113a30e5a5c5c1ae78e4996891fbf',  # opening ownership and budget accounting
    'e17adfd510e87473d6abf8291271fc6adabc1e6c',  # stored-detail service
    '05006e10451b2937d26929f8a59784dbe3d9f854',  # exact cohort memo
    'e48785833930475b49fcc666d839483b9f2c35f0',  # protected native trim, same selected-address and direct paths
}

def disjoint_models(archive, meta):
    # rc.172 selects one base representation per L0 address. Only the
    # independent direct-native path can reinsert a key selected as surface.
    if meta.get('source_commit') not in REVIEWED_TILE_SELECTION:
        return False
    flags=set()
    permanently_refused=False
    try:
        with (Path(archive)/'latest.log').open(errors='replace') as stream:
            for line in stream:
                if '[OrbitalAtlasDiag] flags' in line:
                    flags.update(re.findall(r'\bdirectVisualPublication=(true|false)\b',line))
                if 'uploadGate="attempted/unavailable/GL vendor \'AMD\' is not NVIDIA"' in line:
                    permanently_refused=True
    except OSError:
        return False
    return flags == {'false'} or permanently_refused

def measure(result, card, disjoint=False):
    rows=[]
    answer=dict(basis='active exterior world-map selected-address bounds, not pixel coverage',
                legacy_verdict=card.get('verdict'), disjoint_model_sets=disjoint, samples=rows,crossings={})
    steps=result.get('steps') or []
    opens=[i for i,s in enumerate(steps) if s.get('type')=='orbitalatlas:open_map']
    if len(opens)!=1:
        answer['unavailable']='Expected one successful span-128 map opening'
        return answer
    opened=steps[opens[0]]
    origin=opened.get('started_ms')
    if ((opened.get('args') or {}).get('span')!=128 or opened.get('status')!='PASS' or
            not isinstance(origin,(int,float)) or not math.isfinite(origin)):
        answer['unavailable']='Invalid span-128 map opening or timestamp'
        return answer
    elapsed={s['since_open_ms'] for s in card.get('samples',[])}
    last_mode=last_signature=None
    fresh=False
    for s in steps[opens[0]+1:]:
        if s['type'] in ('orbitalatlas:close_map','quit','teleport') or (s['type']=='command' and
                re.search(r'(?:^|\s)/?(tp|teleport)\s',s.get('args',{}).get('cmd',''))):
            break
        if s['type'].startswith('orbitalatlas:') and s['type']!='orbitalatlas:map_state':
            answer['unavailable']='Another Atlas action interrupted the fixed station'
            return answer
        if s['type']!='orbitalatlas:map_state':
            continue
        start=s.get('started_ms')
        duration=s.get('duration_ms')
        t=start-origin if isinstance(start,(int,float)) else None
        if t not in elapsed or s.get('status')!='PASS' or not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<0:
            fresh=False
            last_mode=last_signature=None
            continue
        active=active_census(s.get('message',''))
        row={'ms':t,'completed_ms':t+duration,'fresh':False}
        rows.append(row)
        if active is None:
            last_mode=last_signature=None
            fresh=False
            continue
        mode,signature,native,surface,sections=active
        if mode!=last_mode:
            fresh=False
        elif signature!=last_signature:
            # A fresh scene publication has happened during this same active
            # world-map mode. Initial retained HUD/old-map census is ambiguous.
            fresh=True
        last_mode,last_signature=mode,signature
        row.update(mode=mode,fresh=fresh,native=native,surface=surface,visible_native_sections=sections,
                   unique_low=native+surface if disjoint else max(native,surface),unique_high=native+surface)
    valid=[r for r in rows if r['fresh']]
    answer['attributed_polls']=len(valid)
    answer['max_unique_low']=max((r['unique_low'] for r in valid),default=None)
    answer['max_unique_high']=max((r['unique_high'] for r in valid),default=None)
    for n in (1,100):
        possible=next((r for r in valid if r['unique_high']>=n),None)
        proven=next((r for r in valid if r['unique_low']>=n),None)
        below=[r for r in valid if possible and r['ms']<possible['ms'] and r['unique_high']<n]
        answer['crossings'][str(n)]=dict(first_possible_observed_ms=possible and possible['ms'],
                                          first_proven_observed_ms=proven and proven['ms'],
                                          previous_clean_below_ms=below[-1]['ms'] if below else None,
                                          first_proven_completed_ms=proven and proven['completed_ms'])
    return answer

if __name__=='__main__':
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from texture_gate import score_archive
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive',type=Path)
    p=parser.parse_args().archive
    card=score_archive(p)
    if card is None: parser.error('Canonical q-texture archive required')
    meta=json.loads((p/'meta.json').read_text())
    print(json.dumps(measure(json.loads((p/'result.json').read_text()),card,disjoint_models(p,meta)),indent=2))
