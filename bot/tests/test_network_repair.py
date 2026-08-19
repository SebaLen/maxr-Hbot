"""Testet _run_network_repair (Mehr-Pionier-Netzreparatur, Spec 6.3):
- Strecke in 5er-Bloecke, Helfer nur wenn reach_turns-Vorpruefung erfuellt
- begrenzt durch repair_quota
- Helfer mit unerreichbarem Startfeld (None) wird NICHT eingesetzt
- kein gap -> nichts."""
import sys, ast
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
src=open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree=ast.parse(src)
fsrc=next(ast.get_source_segment(src,n) for n in tree.body
          if isinstance(n,ast.FunctionDef) and n.name=="_run_network_repair")
built=[]   # (pio_id, goal)
ns={"_free_units":lambda gs,role,claim:[v for v in gs._v if v["id"] not in claim],
    "log":lambda *a,**k:None,
    "_reget":lambda gs,conn,uid:(gs,next((v for v in gs._v if v["id"]==uid),None)),
    "_expansion_build_connector":lambda gs,conn,pio,goal,bl,tg,connect_to=None: built.append((pio["id"],tuple(goal))) or gs}
exec(fsrc,ns); repair=ns["_run_network_repair"]
def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

class GS:
    def __init__(self,segs,gap=True,reach=None):
        self._segs=segs; self._gap=gap
        self._v=[{"id":1,"pos":(0,0)},{"id":2,"pos":(0,0)},{"id":3,"pos":(0,0)}]
        self._reach=reach or {}
    def network_gap_target(self): return ((0,0),(20,0),400) if self._gap else None
    def main_component(self): return {(0,0)}
    def repair_route_segments(self,a,b): return self._segs
    def pos(self,v): return v["pos"]
    def reach_turns(self,uid,target): return self._reach.get((uid,tuple(target)), 0)

print("== kurze Strecke (3 Felder) -> nur 1 Pionier, keine Helfer ==")
built.clear()
segs=[(1,0),(2,0),(3,0)]
repair(GS(segs),None,{}, {},set(),repair_quota=3)
check("genau 1 Bau (Spitze)", len(built)==1)
check("Pionier 1 baut Richtung Insel", built[0][0]==1)

print("== lange Strecke (12 Felder), Helfer erreichbar+lohnend -> mehrere Pioniere ==")
built.clear()
segs=[(i,0) for i in range(1,13)]   # 12 Felder -> ceil(12/5)=3 Bloecke
# Helfer-Startfelder: Block2=segs[5]=(6,0), Block3=segs[10]=(11,0). reach klein -> lohnt.
reach={(2,(6,0)):0,(3,(11,0)):0,(2,(11,0)):0,(3,(6,0)):0,(1,(1,0)):0}
repair(GS(segs,reach=reach),None,{}, {},set(),repair_quota=3)
ids={b[0] for b in built}
check("mehr als 1 Pionier eingesetzt", len(ids)>1)
check("hoechstens 3 (Quote)", len(ids)<=3)

print("== Helfer-Startfeld unerreichbar (reach=None) -> nur Spitze ==")
built.clear()
segs=[(i,0) for i in range(1,13)]
reach={(1,(1,0)):0}   # nur Pionier 1, Helfer-Startfelder unerreichbar (default None? nein -> 0)
# Erzwinge None fuer Helfer:
class GS2(GS):
    def reach_turns(self,uid,target):
        if uid==1: return 0
        return None   # Helfer kommen nicht hin
repair(GS2(segs),None,{}, {},set(),repair_quota=3)
check("nur 1 Pionier (Spitze), keine Helfer", len({b[0] for b in built})==1)

print("== quota=1 -> nur Spitze, egal wie lang ==")
built.clear()
repair(GS([(i,0) for i in range(1,13)],reach={(1,(1,0)):0}),None,{}, {},set(),repair_quota=1)
check("nur 1 Pionier bei Quote 1", len({b[0] for b in built})==1)

print("== kein gap -> nichts gebaut ==")
built.clear()
repair(GS([(1,0)],gap=False),None,{}, {},set(),repair_quota=3)
check("kein Bau ohne gap", len(built)==0)

print("\nNETWORK-REPAIR-TEST BESTANDEN.")
