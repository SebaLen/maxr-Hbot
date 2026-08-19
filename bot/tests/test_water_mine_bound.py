"""Testet build_water_mine_at mit Site-gebundenen Bauern (Variante b):
- Pionier wird an ALLE Plattform-Komponenten gebunden, kein zweiter wird geschnappt.
- Mine erst, wenn die 2x2 KOMPLETT plattformiert ist (platform_fields_needed==[]).
- Baustelle bleibt gebunden, auch wenn der Pionier zwischendurch keinen _PLAN-Task hat."""
import sys, ast
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
from build_plan import (BuildSite, SiteComponent, COMP_PLATFORM, COMP_MINE,
                        VEH_ENGINEER, VEH_CONSTRUCTOR)
src=open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree=ast.parse(src)
def grab(name):
    return next(ast.get_source_segment(src,n) for n in tree.body
               if isinstance(n,ast.FunctionDef) and n.name==name)
calls=[]
class FakePlan:
    def __init__(self): self.tasks={}
    def tasks_for(self,i): return self.tasks.get(i,[])
    def assign_chain(self,i,items,by_distance=False): self.tasks[i]=list(items)
PLAN=FakePlan()
ns={"COMP_PLATFORM":COMP_PLATFORM,"COMP_MINE":COMP_MINE,"SiteComponent":SiteComponent,
    "_EXPANSION_REJECTED":set(),"log":lambda *a,**k:None,"_PLAN":PLAN,
    "_free_units":lambda gs,role,claim:[v for v in gs._veh.get(role,[]) if v["id"] not in claim and not v.get("isBuilding")],
    "_build_platform_chain":lambda gs,conn,p: calls.append(("plat_chain",p["id"]))or gs,
    "_expansion_send_constructor":lambda gs,conn,c,goal,sid,bl,tg,**k: calls.append(("send_con",c["id"]))or gs,
}
exec(grab("_drive_or_assign_mine"),ns)
exec(grab("build_water_mine_at"),ns)
bwm=ns["build_water_mine_at"]

def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

class MockGS:
    MINE_SID=22
    def __init__(self,pf): self._veh={"engineer":[{"id":21},{"id":22}],"constructor":[{"id":30}]}; self._pf=pf; self._mine=None
    def mine_covering(self,f): return self._mine
    def my_vehicles(self): return self._veh["engineer"]+self._veh["constructor"]
    def vehicles_of_type(self,r): return self._veh.get(r,[])
    def platform_fields_needed(self,pos): return list(self._pf)
    def building_sid_by_name(self,n): return 25
    def build_cost(self,sid): return 2

def make_site():
    s=BuildSite((53,42),"metal",score=5.0); s.mine_pos=(53,41)
    s.components=[SiteComponent(COMP_PLATFORM,VEH_ENGINEER,[(53,41)],25,2,0),
                  SiteComponent(COMP_PLATFORM,VEH_ENGINEER,[(54,41)],25,2,1),
                  SiteComponent(COMP_PLATFORM,VEH_ENGINEER,[(53,42)],25,2,2),
                  SiteComponent(COMP_PLATFORM,VEH_ENGINEER,[(54,42)],25,2,3),
                  SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(53,41)],22,60,4)]
    return s

print("== 2x2 unvollstaendig (4 Felder fehlen): Pionier wird gebunden, baut Kette ==")
s=make_site(); gs=MockGS(pf=[(53,41),(54,41),(53,42),(54,42)]); claim=set(); calls.clear()
bwm(gs,None,s,{}, {},claim)
plat_pids={c.builder for c in s.components if c.kind==COMP_PLATFORM}
check("genau EIN Pionier an alle Plattform-Komp gebunden", plat_pids=={21})
check("plat_chain fuer Pionier 21 gerufen", ("plat_chain",21) in calls)
check("Mine NICHT gebaut (2x2 unvollstaendig)", not any(c[0]=="send_con" for c in calls))

print("== naechste Runde, 2 Felder noch offen: KEIN zweiter Pionier, nur 21 weiter ==")
gs._pf=[(53,42),(54,42)]; claim=set(); calls.clear()
bwm(gs,None,s,{}, {},claim)
check("immer noch nur Pionier 21 gebunden", {c.builder for c in s.components if c.kind==COMP_PLATFORM}=={21})
check("Pionier 22 NICHT geschnappt", 22 not in claim)
check("plat_chain wieder fuer 21", ("plat_chain",21) in calls)

print("== 2x2 KOMPLETT (pf==[]): Plattformen DONE, Konstrukteur baut Mine ==")
gs._pf=[]; claim=set(); calls.clear()
bwm(gs,None,s,{}, {},claim)
check("alle Plattform-Komp DONE", all(c.state==SiteComponent.S_DONE for c in s.components if c.kind==COMP_PLATFORM))
check("Konstrukteur 30 an Mine gebunden", next(c for c in s.components if c.kind==COMP_MINE).builder==30)
check("send_con fuer Konstrukteur 30 gerufen", ("send_con",30) in calls)

print("== Pionier verliert _PLAN-Task, 2x2 noch offen: Kette wird neu angelegt, 21 baut weiter ==")
s2=make_site(); s2.components[0].builder=21; s2.components[1].builder=21
s2.components[2].builder=21; s2.components[3].builder=21
for c in s2.components:
    if c.kind==COMP_PLATFORM: c.state=SiteComponent.S_BUILDING
gs2=MockGS(pf=[(54,42)]); PLAN.tasks={}  # 21 hat KEINEN Task mehr
claim=set(); calls.clear()
bwm(gs2,None,s2,{}, {},claim)
check("Pionier 21 bleibt gebunden (kein neuer)", {c.builder for c in s2.components if c.kind==COMP_PLATFORM}=={21})
check("Kette fuer 21 neu angelegt", 21 in PLAN.tasks and len(PLAN.tasks[21])>=1)
check("plat_chain fuer 21 gerufen", ("plat_chain",21) in calls)

print("\nWATER-MINE-BOUND-TEST BESTANDEN.")
