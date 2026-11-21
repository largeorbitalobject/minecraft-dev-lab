#!/bin/bash
# Phase 2 of the 2026-11-15 dossier night (rewritten 10:47Z): after the main quick-set runner, per host:
# rep 3 of q-basepack for rc.160 + rc.159 (their rep 1 ran 60 fps-capped), Atlas-off x2, census build, then extras.
P=/root/minecraft-dev-lab-config/perfsuite
# per-host wait (12:00Z change): each host starts as soon as its own last main job (q-cave rc.160 rep 2) is logged
done_main(){ jq -e --arg h $1 'select(.host==$h and .scenario=="q-cave" and .build=="rc.160" and .rep==2 and .arm=="base")' $P/queue-log.jsonl >/dev/null 2>&1; }
( while ! done_main fd; do sleep 30; done
  python3 $P/run.py --builds rc.160,rc.159 --hosts fd --scenarios q-basepack --reps 1 --rep-start 3
  python3 $P/run.py --builds rc.160 --hosts fd --scenarios q-basepack --reps 2 --arm b400
  python3 $P/run.py --builds rc.160 --hosts fd --scenarios q-basepack --reps 2 --arm off
  python3 $P/run.py --builds perfsuite-census1 --hosts fd --scenarios q-basepack,q-cave --reps 1 --jfr --arm diagx
  python3 $P/run.py --builds rc.160 --hosts fd --scenarios n-mixed --reps 1 --jfr ) > $P/logs-p2-fd.txt 2>&1 &
( while ! done_main gpc; do sleep 30; done
  python3 $P/run.py --builds rc.160,rc.159 --hosts gpc --scenarios q-basepack --reps 1 --rep-start 3
  python3 $P/run.py --builds rc.160 --hosts gpc --scenarios q-basepack --reps 2 --arm off
  python3 $P/run.py --builds rc.160 --hosts gpc --scenarios q-basepack --reps 2 --arm cap120
  python3 $P/run.py --builds perfsuite-census1 --hosts gpc --scenarios q-basepack,q-cave --reps 1 --jfr --arm diagx
  python3 $P/run.py --builds rc.160 --hosts gpc --scenarios n-mixed --reps 1 ) > $P/logs-p2-gpc.txt 2>&1 &
wait
