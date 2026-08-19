"""Testet _preload_first_mine_builders: GENAU EIN Konstrukteur + EIN Pionier als
feste Reserve, immer gebunden (claim), wandern nicht; Freigabe bei >=7-Fund/Mine 2;
andere Bauer bleiben frei."""
import sys, ast
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
src=open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree=ast.parse(src)
fsrc=next(ast.get_source_segment(src,n) for n in tree.body
          if isinstance(n,ast.FunctionDef) and n.name=="_preload_first_mine_builders")
RES={"con":None,"pio":None}
transfers=[]
ns={"_FIRST_MINE_RESERVE":RES,"_EXPANSION_REJECTED":set(),"log":lambda *a,**k:None,
    "_free_units":lambda gs,role,claim:[v for v in gs._v[role] if v["id"] not in claim and not v.get("isBuilding")],
    "adjacent_networked_building":lambda gs,b,need_metal=False:{"id":900},
    "dock_field_at_base":lambda gs,b:None,
    "ore_available_for":lambda gs,bid:100,
    "_reget":lambda gs,conn,uid:(gs,next((v for vs in gs._v.values() for v in vs if v["id"]==uid),None)),
}
exec(fsrc,ns); preload=ns["_preload_first_mine_builders"]
def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

class Conn:
    def do(self,a):
        if a["type"]=="transfer": transfers.append((a["targetId"],a["amount"]))
        return True,None

class GS:
    def __init__(self,mc,field,cons,pios): self._mc=mc; self._f=field; self._v={"constructor":cons,"engineer":pios}
    def mine_count(self): return self._mc
    def expansion_target(self,blocked_fields=None,force_type=None,min_metal=0): return (5,5,"metal",9,1.0) if self._f else None
    def vehicles_of_type(self,r): return self._v[r]
    def store_max(self,b): return 60 if b in self._v["constructor"] else 8
    def stored(self,b): return b.get("stored",0)
    def pos(self,b): return (0,0)

print("== Wartephase: 1 Konstrukteur + 1 Pionier reserviert + gebunden ==")
RES["con"]=RES["pio"]=None
cons=[{"id":15,"stored":0},{"id":18,"stored":0}]; pios=[{"id":19,"stored":0},{"id":20,"stored":0}]
gs=GS(mc=1,field=False,cons=cons,pios=pios); claim=set()
preload(gs,Conn(),claim)
check("genau 1 Konstrukteur als Reserve gemerkt", RES["con"] in (15,18))
check("genau 1 Pionier als Reserve gemerkt", RES["pio"] in (19,20))
check("Reserve-Konstrukteur gebunden", RES["con"] in claim)
check("Reserve-Pionier gebunden", RES["pio"] in claim)
check("der ANDERE Konstrukteur bleibt FREI", any(c["id"] not in claim for c in cons))
check("der ANDERE Pionier bleibt FREI", any(p["id"] not in claim for p in pios))
res_con=RES["con"]; res_pio=RES["pio"]

print("== naechste Runde: Reserve wandert NICHT (gleiche IDs) ==")
claim2=set(); preload(gs,Conn(),claim2)
check("Konstrukteur-Reserve unveraendert", RES["con"]==res_con)
check("Pionier-Reserve unveraendert", RES["pio"]==res_pio)

print("== >=7-Feld gefunden: Reserve wird freigegeben (nicht mehr gebunden) ==")
gs2=GS(mc=1,field=True,cons=cons,pios=pios); claim3=set()
preload(gs2,Conn(),claim3)
check("Konstrukteur-Reserve freigegeben", RES["con"] is None)
check("Pionier-Reserve freigegeben", RES["pio"] is None)
check("nichts gebunden", len(claim3)==0)

print("\nFIRST-MINE-RESERVE-TEST BESTANDEN.")

# --- Zusatz: gemerkte Reserve baut zwischenzeitlich (Notfall-Fabrik) -> verwerfen ---
print("== Reserve-Konstrukteur faengt an zu bauen -> Reserve wechselt auf freien ==")
RES["con"]=RES["pio"]=None
cons=[{"id":20,"stored":60},{"id":21,"stored":0}]; pios=[{"id":24,"stored":8}]
gs=GS(mc=1,field=False,cons=cons,pios=pios); claim=set()
preload(gs,Conn(),claim)
first=RES["con"]
check("zuerst ein freier Konstrukteur als Reserve", first in (20,21))
# Jetzt baut genau dieser Reserve-Konstrukteur (Notfall-Fabrik) -> isBuilding
for c in cons:
    if c["id"]==first: c["isBuilding"]=True
claim2=set()
preload(gs,Conn(),claim2)
check("bauender Reserve-Konstrukteur verworfen", RES["con"]!=first or RES["con"] is None)
check("neuer Reserve-Konstrukteur baut NICHT",
      RES["con"] is None or not next(c for c in cons if c["id"]==RES["con"]).get("isBuilding"))
print("\nRESERVE-ISBUILDING-ZUSATZ BESTANDEN.")
