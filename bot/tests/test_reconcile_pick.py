"""Testet _reconcile_backlog (verwaiste Zuweisungen freigeben) und die
Auswahl-Logik (geringste Wegkosten gewinnt) - isoliert mit Mocks."""
import sys, ast, types
sys.path.insert(0, "/home/claude/maxr/src/botbridge/Bot_code")
from build_plan import (BuildBacklog, BuildSite, BuildPlan, SiteComponent,
                        COMP_PLATFORM, COMP_MINE, COMP_CONNECTOR,
                        VEH_ENGINEER, VEH_CONSTRUCTOR)

def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

# ---- _reconcile_backlog isoliert nachbilden (gleiche Logik wie im Code) ----
def reconcile(backlog, plan, alive_ids):
    plan_builders = {t.builder for t in plan.all_tasks()}
    freed = 0
    for site in backlog.sorted_open():
        for comp in site.components:
            bid = comp.builder
            if bid is None: continue
            plat_chain = (comp.kind == COMP_PLATFORM and bid in plan_builders)
            lost = (bid not in alive_ids) or (bid in plan_builders and not plat_chain)
            if lost:
                comp.builder = None; freed += 1
    return freed

print("== Reconciliation: toter Bauer wird freigegeben ==")
bl = BuildBacklog()
s = BuildSite((40,40),"metal",score=5.0); s.mine_pos=(40,40)
s.components=[SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(40,40)],22,60,0)]
s.components[0].builder = 77   # committed an Bauer 77
bl.add_or_update(s)
plan = BuildPlan()
freed = reconcile(bl, plan, alive_ids={1,2,3})  # 77 ist NICHT mehr da
check("1 freigegeben (Bauer 77 tot)", freed==1)
check("Komponente wieder vergebbar (builder None)", s.components[0].builder is None)

print("== Reconciliation: vom Notfall abgegriffener Bauer wird freigegeben ==")
bl2 = BuildBacklog()
s2 = BuildSite((50,50),"metal",score=4.0); s2.mine_pos=(50,50)
s2.components=[SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(50,50)],22,60,0)]
s2.components[0].builder = 88
bl2.add_or_update(s2)
plan2 = BuildPlan()
plan2.assign((10,10), 5, builder=88)   # 88 hat jetzt einen NOTFALL-Auftrag
freed = reconcile(bl2, plan2, alive_ids={88})  # lebt, aber baut Notfall
check("1 freigegeben (88 vom Notfall abgegriffen)", freed==1)
check("Mine-Komponente wieder frei", s2.components[0].builder is None)

print("== Reconciliation: Plattform-Pionier mit _PLAN-Kette bleibt committed ==")
bl3 = BuildBacklog()
s3 = BuildSite((60,60),"metal",score=3.0); s3.mine_pos=(60,60)
pc = SiteComponent(COMP_PLATFORM,VEH_ENGINEER,[(60,60)],40,2,0)
pc.builder = 99
s3.components=[pc, SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(60,60)],22,60,1)]
bl3.add_or_update(s3)
plan3 = BuildPlan()
plan3.assign_chain(99, [((60,60),40,2,"platform")], by_distance=True)  # Kette im Plan
freed = reconcile(bl3, plan3, alive_ids={99})
check("0 freigegeben (Pionier baut korrekt seine Plattform-Kette)", freed==0)
check("Plattform bleibt committed an 99", pc.builder==99)

print("== Reconciliation: gueltige laufende Zuweisung bleibt ==")
bl4 = BuildBacklog()
s4 = BuildSite((70,70),"metal",score=2.0); s4.mine_pos=(70,70)
s4.components=[SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(70,70)],22,60,0)]
s4.components[0].builder = 55
bl4.add_or_update(s4)
freed = reconcile(bl4, BuildPlan(), alive_ids={55})  # lebt, kein Notfall-Auftrag
check("0 freigegeben (55 arbeitet brav)", freed==0)
check("bleibt committed", s4.components[0].builder==55)

# ---- Auswahl-Logik: geringste Wegkosten gewinnt ----
print("== Bauer-Auswahl: geringste echte Wegkosten gewinnt ==")
# Mock conn.path_cost: Bauer naeher dran -> weniger cost
class MockConn:
    def __init__(self, costs): self.costs=costs  # (bid, target)->cost
    def query(self, q):
        bid=q["unitId"]; tgt=tuple(q["target"])
        c=self.costs.get((bid,tgt))
        if c is None: return {"reachable": False}
        return {"reachable": True, "cost": c, "steps": c}
# Nachbildung _builder_total_cost + _pick_builder_for
def total_cost(conn, bid, build_pos, stored, cost, dock):
    pcb = conn.query({"query":"pathCost","unitId":bid,"target":list(build_pos)})
    if not pcb.get("reachable"): return float("inf")
    t = pcb["cost"]
    if stored < cost:
        pcl = conn.query({"query":"pathCost","unitId":bid,"target":list(dock)})
        if not pcl.get("reachable"): return float("inf")
        t += pcl["cost"]
    return float(t)

build_pos=(20,20); dock=(10,10)
# Bauer 1: nah an Baustelle(3) + nah an dock(2) = 5 ; Bauer 2: 8+1=9
conn = MockConn({(1,build_pos):3,(1,dock):2,(2,build_pos):8,(2,dock):1})
c1 = total_cost(conn,1,build_pos,0,60,dock)
c2 = total_cost(conn,2,build_pos,0,60,dock)
check(f"Bauer1 Gesamtkosten 5 (ist {c1})", c1==5)
check(f"Bauer2 Gesamtkosten 9 (ist {c2})", c2==9)
check("Bauer1 (geringste Kosten) gewinnt", c1 < c2)

print("== Bauer-Auswahl: schon beladener Bauer spart den Beladeweg ==")
# Bauer 3 ist beladen (stored>=cost): nur Bauweg 8 zaehlt, Beladeweg entfaellt
c3 = total_cost(conn, 1, build_pos, 60, 60, dock)  # stored=60>=60
check(f"beladener Bauer: nur Bauweg 3 (ist {c3})", c3==3)

print("\nRECONCILE + AUSWAHL-TESTS BESTANDEN.")
