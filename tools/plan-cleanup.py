"""One-off cleanup check: PDF vectors of a unit frame against the room/balcony outline of its plan document.
Writes debug output to $FLOORPLAN_TMP (default WORK/tmp)."""
import re,json,math,collections,pymupdf,sys,os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fpconfig
ROOT=str(fpconfig.WORK)+'/'
S=os.environ.get('FLOORPLAN_TMP') or str(fpconfig.WORK/'tmp'); os.makedirs(S, exist_ok=True)
doc=pymupdf.open(str(fpconfig.PDF))
def rgb(c): return None if c is None else '#%02X%02X%02X'%tuple(int(round(x*255)) for x in c[:3])
def load(unit):
    d=json.load(open(ROOT+f'plan-studio/v3/plans/unit-{unit}.json')); bb=d['bbox']
    fd=json.load(open(ROOT+f"plan-studio/data/floor-{d['floor']}.json")); fx0,fy0=fd['bbox'][0],fd['bbox'][1]
    clip=pymupdf.Rect(fx0+bb['x0'],fy0+bb['y0'],fx0+bb['x0']+bb['w'],fy0+bb['y0']+bb['h'])
    K=7.05; u=fd['units'][unit]
    px=lambda poly:[((x-bb['x0'])*K,(y-bb['y0'])*K) for x,y in poly]
    polys=[px(a['poly']) for a in d['areas'] if a['kind'] in ('room','balcony')]   # контур из документа v3: комната + все балконы
    def inside(pt,poly):
        x,y=pt; n=len(poly); ins=False
        for i in range(n):
            x1,y1=poly[i]; x2,y2=poly[(i+1)%n]
            if (y1>y)!=(y2>y) and x<(x2-x1)*(y-y1)/(y2-y1)+x1: ins=not ins
        return ins
    def dist(pt,poly):
        x,y=pt; best=1e9
        for i in range(len(poly)):
            x1,y1=poly[i]; x2,y2=poly[(i+1)%len(poly)]
            dx,dy=x2-x1,y2-y1; L=dx*dx+dy*dy; t=0 if L==0 else max(0,min(1,((x-x1)*dx+(y-y1)*dy)/L))
            best=min(best,math.hypot(x-(x1+t*dx),y-(y1+t*dy)))
        return best
    near=lambda pt,m: any(inside(pt,p) or dist(pt,p)<=m for p in polys)
    items=[]
    for dr in doc[fd['page']-1].get_drawings():
        r=dr['rect']
        if r.x1<clip.x0 or r.x0>clip.x1 or r.y1<clip.y0 or r.y0>clip.y1: continue
        corners=[((r.x0-clip.x0)*K,(r.y0-clip.y0)*K),((r.x1-clip.x0)*K,(r.y1-clip.y0)*K)]
        m=min((999, *[mm for mm in (10,25,45,70) if all(near(p,mm) for p in corners)]))
        # пересечение с контуром: сетка точек по bbox элемента
        xs=[(r.x0-clip.x0)*K + i*((r.x1-r.x0)*K)/6 for i in range(7)]; ys=[(r.y0-clip.y0)*K + i*((r.y1-r.y0)*K)/6 for i in range(7)]
        mi=min((999, *[mm for mm in (10,25,45) if any(near((x,y),mm) for x in xs for y in ys)]))
        cx=((r.x0+r.x1)/2-clip.x0)*K; cy=((r.y0+r.y1)/2-clip.y0)*K
        cd=min((-dist((cx,cy),p) if inside((cx,cy),p) else dist((cx,cy),p)) for p in polys)   # центр: <0 внутри контура
        kinds=[it[0] for it in dr['items']]
        items.append(dict(seq=str(dr['seqno']), lay=dr.get('layer','') or '(none)', stroke=rgb(dr.get('color')) if 's' in dr['type'] else None,
                          fill=rgb(dr.get('fill')) if 'f' in dr['type'] else None, npts=len(kinds), curve='c' in kinds, m=m, mi=mi, cd=cd,
                          w=(r.x1-r.x0)*K, h=(r.y1-r.y0)*K))
    return items
KEEP_LAYERS={'ავეჯი სრული':25,'Structural - Bearing':45,'შახტა':45,'დიაფრაგმა':45,'ალუკაბონდის პანელები':45,'კოლონები':45,'ხანძარმედეგობა':45,'სველიწერტილების ფილი':25,'ვიტრაჟები':45,'კედლები':45,'ფასადის პანელები ფერადი':45}
def rule(it):
    if it['stroke'] in ('#DCB900','#0000FF','#80C2FF','#DF0000'): return False
    if it['fill'] in ('#DF0000','#E8E8E8'): return False
    if it['lay']=='ბინების კვადრატულობა':
        return it['stroke'] in ('#999999','#000000') and it['npts']<=2 and it['m']<=10
    lim=KEEP_LAYERS.get(it['lay'])
    if lim is None: return False
    if it['lay'] in ('Structural - Bearing','დიაფრაგმა','შახტა','კოლონები','ალუკაბონდის პანელები','ვიტრაჟები','კედლები','ფასადის პანელები ფერადი'):
        if it['mi']>lim: return False      # стены и т.п.: достаточно пересечения с контуром (+45 см)
    elif it['m']>lim: return False
    if it['stroke']=='#FF6600' and not it['curve'] and it['npts']<=2 and it['lay']!='Structural - Bearing': return False   # оранжевая штриховка; дуги дверей и линии проёмов остаются
    if it['lay']=='კოლონები' and it['stroke']=='#A80F02' and it['npts']<=2: return False   # тёмно-красная штриховка колонн
    if it['lay']=='სველიწერტილების ფილი' and it['stroke'] in ('#AAAAAA','#CCCCCC') and it['npts']<=2: return False   # плитка санузлов
    return True
if __name__=='__main__':
    unit=sys.argv[1]; items=load(unit)
    if len(sys.argv)>2 and sys.argv[2]=='eval':
        kept=set(open(S+'/kept201b.txt').read().strip().split(','))
        tp=fp=fn=tn=0; fpc=collections.Counter(); fnc=collections.Counter()
        for it in items:
            k=it['seq'] in kept; r=rule(it)
            if r and k: tp+=1
            elif r and not k: fp+=1; fpc[(it['lay'],it['stroke'],it['fill'],'2pt' if it['npts']<=2 else 'poly', 'm%d'%it['m'])]+=1
            elif not r and k: fn+=1; fnc[(it['lay'],it['stroke'],it['fill'],'2pt' if it['npts']<=2 else 'poly','m%d'%it['m'])]+=1
            else: tn+=1
        print(f'keep ok {tp}, wrongly kept {fp}, wrongly dropped {fn}, drop ok {tn}, agreement {(tp+tn)/(tp+fp+fn+tn):.3f}')
        print('wrongly kept:'); [print('  ',v,k) for k,v in fpc.most_common(14)]
        print('wrongly dropped:'); [print('  ',v,k) for k,v in fnc.most_common(14)]
    else:
        drop=[it['seq'] for it in items if not rule(it)]
        print(json.dumps(drop))
