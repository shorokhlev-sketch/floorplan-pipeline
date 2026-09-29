#!/usr/bin/env python3
"""Собирает DATA для place-labels.js из rooms-<unit>.json: {unit: [{label, x, y}]}.
Использование: python3 tools/plan-room-labels-data.py <unit> ... > out.json ; --table печатает сводку."""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fpconfig
ROOMS = os.path.join(str(fpconfig.WORK), 'plan-studio', 'v3', 'figma', 'final', 'rooms')
args = [a for a in sys.argv[1:] if not a.startswith('--')]
table = '--table' in sys.argv
data, rows = {}, []
for u in args:
    p = os.path.join(ROOMS, f'rooms-{u}.json')
    if not os.path.exists(p):
        rows.append(f'{u}: нет rooms-{u}.json'); continue
    r = json.load(open(p))
    data[u] = [{'label': room['label'], 'x': room['x'], 'y': room['y']} for room in r['rooms']]
    areas = ', '.join(f"{room['area_m2']:.1f}" for room in r['rooms'])
    flag = ' ! ' + '; '.join(r['warnings']) if r['warnings'] else ''
    coll = sum(1 for room in r['rooms'] if room.get('label_collision'))
    rows.append(f"{u}: {len(r['rooms'])} комн. [{areas}] Σ {r['sum_m2']} / living {r['living']} ({r['diff_pct']:+.1f}%){' коллизий ' + str(coll) if coll else ''}{flag}")
if table:
    print('\n'.join(rows))
else:
    json.dump(data, sys.stdout, ensure_ascii=False)
