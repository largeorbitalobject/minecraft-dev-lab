import tempfile
import unittest
from pathlib import Path

import texture_tiles


def poll(native, surface, sections, mode='regional', interior='none'):
    active=f'cand:120,view:120,detT:{native},detS:{sections},surfT:{surface}'
    inactive='cand:500,view:400,detT:300,detS:600,surfT:100'
    ordinary,regional=(inactive,active) if mode=='regional' else (active,inactive)
    return (f'map: regional={str(mode=="regional").lower()} dim=minecraft:overworld '
            f'situation=OUTDOORS section[place=false on=false floor=NaN] interior={interior} '
            f'lastMap={ordinary} lastRegional={regional} wmMode={mode}')


def score(messages, disjoint=False):
    result={'steps':[{'type':'orbitalatlas:open_map','status':'PASS','args':{'span':128},'started_ms':1000}]}
    for i,message in enumerate(messages,1):
        result['steps'].append(dict(type='orbitalatlas:map_state',status='PASS',message=message,
                                    started_ms=1000+i*100,duration_ms=2))
    card={'verdict':'PASS','samples':[{'since_open_ms':i*100} for i in range(1,len(messages)+1)]}
    return texture_tiles.measure(result,card,disjoint)


class ActiveTileCensus(unittest.TestCase):
    def test_inactive_retained_hud_and_sections_do_not_count_as_tiles(self):
        report=score([poll(0,0,0),poll(2,3,150),poll(2,3,150)])
        self.assertEqual(5,report['max_unique_high'])
        self.assertEqual(3,report['max_unique_low'])
        self.assertIsNone(report['crossings']['100']['first_possible_observed_ms'])
        self.assertEqual(200,report['crossings']['1']['first_proven_observed_ms'])

    def test_first_unchanged_census_is_not_fresh(self):
        result=score([poll(150,0,700),poll(150,0,700)])
        self.assertEqual(0,result['attributed_polls'])
        self.assertIsNone(result['max_unique_high'])

    def test_overlapping_sets_and_confirmed_disjoint_sets(self):
        rows=[poll(0,0,0),poll(90,19,200),poll(91,19,240)]
        self.assertIsNone(score(rows)['crossings']['100']['first_proven_observed_ms'])
        exact=score(rows,True)['crossings']['100']
        self.assertEqual(200,exact['first_proven_observed_ms'])
        self.assertEqual(202,exact['first_proven_completed_ms'])

    def test_renderer_switch_must_reestablish_current_publication(self):
        result=score([poll(0,0,0),poll(5,0,7),poll(160,0,180,'ordinary'),poll(160,0,180,'ordinary')])
        self.assertEqual(5,result['max_unique_high'])
        self.assertFalse(result['samples'][-1]['fresh'])

    def test_interior_counter_is_not_exterior_proof(self):
        result=score([poll(0,0,0),poll(20,30,30,interior='CAVE')])
        self.assertEqual(0,result['attributed_polls'])

    def test_runtime_flag_proof_requires_reviewed_build(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            (p/'latest.log').write_text('[OrbitalAtlasDiag] flags directVisualPublication=false\n')
            self.assertFalse(texture_tiles.disjoint_models(p,{'source_commit':'unreviewed'}))
            reviewed=next(iter(texture_tiles.REVIEWED_TILE_SELECTION))
            self.assertTrue(texture_tiles.disjoint_models(p,{'source_commit':reviewed}))
            (p/'latest.log').write_text('[OrbitalAtlasDiag] flags directVisualPublication=true\n')
            self.assertFalse(texture_tiles.disjoint_models(p,{'source_commit':reviewed}))
