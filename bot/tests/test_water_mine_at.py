"""Testet build_water_mine_at: zielgebundene Wassermine-Sequenz.
Kern: das pro-Ziel-Plattformgedaechtnis (_PLATMINE_BY_GOAL) haelt fuer ZWEI
gleichzeitige Wasserminen je EIGENE stabile Flaeche - kein Ueberschreiben."""
import sys, ast
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
src=open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree=ast.parse(src)

# build_water_mine_at + Abhaengigkeiten in einen Namespace exec'en
fsrc=next(ast.get_source_segment(src,n) for n in tree.body
          if isinstance(n,ast.FunctionDef) and n.name=="build_water_mine_at")
calls=[]
PLATMINE={}
ns={
 "_PLATMINE_BY_GOAL":PLATMINE,
 "_EXPANSION_REJECTED":set(),
 "log":lambda *a,**k:None,
 "_free_units":lambda gs,role,claim:[v for v in gs._veh.get(role,[]) if v["id"] not in claim and not v.get("isBuilding")],
 "_build_platform_chain":lambda gs,conn,p: calls.append(("plat_chain",p["id"]))or gs,
 "_expansion_send_constructor":lambda gs,conn,c,goal,sid,bl,tg,**k: calls.append(("send_con",c["id"],goal))or gs,
 "_PLAN":type("P",(),{"tasks_for":staticmethod(lambda i:[]),
                     "assign_chain":staticmethod(lambda *a,**k:None)})(),
}
exec(fsrc,ns); bwm=ns["build_water_mine_at"]

def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

class MockGS:
    MINE_SID=22
    def __init__(self): self._veh={"engineer":[{"id":21},{"id":22}],"constructor":[{"id":30}]}
    def mine_build_position(self,goal,target_type=None): return None  # kein Land -> Wasser
    def mine_build_position_with_platforms(self,goal,target_type=None):
        # je Ziel eine ANDERE, stabile Flaeche
        return (goal[0]+10, goal[1]) 
    def platform_fields_needed(self,pos): return [(pos[0],pos[1])]  # 1 Plattform fehlt
    def mine_covering(self,field): return None  # noch keine Mine
    def vehicles_of_type(self,role): return self._veh.get(role,[])
    def build_cost(self,sid): return 2
    def building_sid_by_name(self,n): return 25
    def store_max(self,v): return 60

gs=MockGS(); claim=set()

print("== Ziel A (40,40): Wasser -> Plattformflaeche fixiert, Pionier baut Kette ==")
calls.clear()
bwm(gs,None,40,40,"metal",{}, {},claim)
check("Plattformflaeche fuer A gemerkt", (40,40) in PLATMINE)
posA=tuple(PLATMINE[(40,40)])
check("Flaeche A = (50,40)", posA==(50,40))
check("Pionier-Kette fuer A angestossen", any(c[0]=="plat_chain" for c in calls))

print("== Ziel B (60,60) in DERSELBEN Runde: eigene Flaeche, A bleibt unveraendert ==")
calls.clear()
bwm(gs,None,60,60,"oil",{}, {},claim)
check("Plattformflaeche fuer B gemerkt", (60,60) in PLATMINE)
check("Flaeche B = (70,60)", tuple(PLATMINE[(60,60)])==(70,60))
check("Flaeche A NICHT ueberschrieben (immer noch (50,40))", tuple(PLATMINE[(40,40)])==(50,40))

print("== Erneuter Aufruf A naechste Runde: liest STABILE Flaeche, rechnet nicht neu ==")
# mine_build_position_with_platforms wuerde dieselbe liefern; entscheidend: gemerkt bleibt
calls.clear()
bwm(gs,None,40,40,"metal",{}, {},claim)
check("Flaeche A weiterhin stabil (50,40)", tuple(PLATMINE[(40,40)])==(50,40))

print("\nBUILD-WATER-MINE-AT-TEST BESTANDEN.")
