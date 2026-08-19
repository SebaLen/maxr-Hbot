"""Prueft den Kern des Plattform-Fixes: dass die Plattform-Komponenten einer
Baustelle korrekt als _PLAN-Kette registriert werden, sodass _build_platform_chain
(das aus dem _PLAN liest) eine Aufgabe findet - statt task is None (stiller Abbruch)."""
import sys
sys.path.insert(0, "/home/claude/maxr/src/botbridge/Bot_code")
from build_plan import (BuildPlan, BuildSite, SiteComponent,
                        COMP_PLATFORM, COMP_MINE, COMP_CONNECTOR, VEH_ENGINEER, VEH_CONSTRUCTOR)

def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

# Baustelle mit 2 Plattformen + Mine + Kopplung (Wassermine)
site = BuildSite((55,46), "metal", amount=9, score=0.79)
site.mine_pos = (54,45)
site.components = [
    SiteComponent(COMP_PLATFORM, VEH_ENGINEER, [(54,45)], 40, 2, 0),
    SiteComponent(COMP_PLATFORM, VEH_ENGINEER, [(55,45)], 40, 2, 1),
    SiteComponent(COMP_MINE, VEH_CONSTRUCTOR, [(54,45),(55,45),(54,46),(55,46)], 22, 60, 2),
    SiteComponent(COMP_CONNECTOR, VEH_ENGINEER, [(55,46)], 41, 2, 3),
]

# Nachbildung des Konsumenten-Plattform-Blocks
plan = BuildPlan()
PIO_ID = 7
def build_cost(sid): return {40:2, 41:2, 22:60}.get(sid, 2)

# due_component muss zuerst eine Plattform sein
due = site.due_component()
check("due_component ist Plattform", due.kind == COMP_PLATFORM)

# --- der gefixte Block ---
sid_plat = 40
plat_comps = [c for c in site.components if c.kind == COMP_PLATFORM and not c.is_done()]
items = []
for pc in plat_comps:
    for f in pc.fields:
        items.append((tuple(f), sid_plat, build_cost(sid_plat) or 2, "platform"))
check("2 Plattform-Items gesammelt", len(items) == 2)
plan.assign_chain(PIO_ID, items, by_distance=True)
for pc in plat_comps:
    pc.builder = PIO_ID

# Jetzt das Entscheidende: findet _build_platform_chain eine Aufgabe?
task = plan.next_task_for(PIO_ID, builder_pos=(50,45))
check("next_task_for liefert eine Aufgabe (NICHT None - das war der Bug)", task is not None)
check("Aufgabe ist eine Plattform", task.sid == sid_plat)
check("chain_metal_needed = 4 (2 Plattformen x 2)", plan.chain_metal_needed(PIO_ID, build_cost) == 4)

# Beide Plattform-Komponenten sind jetzt committed -> due_component der Site
# ueberspringt sie und der comp.builder-Check im Konsumenten greift
check("erste Plattform-Komponente committed", site.components[0].builder == PIO_ID)
check("zweite Plattform-Komponente committed", site.components[1].builder == PIO_ID)

# Simuliere: erste Plattform fertig -> due_component wandert zur zweiten, dann Mine
site.components[0].state = SiteComponent.S_DONE
site.components[1].state = SiteComponent.S_DONE
due2 = site.due_component()
check("nach beiden Plattformen ist Mine faellig", due2.kind == COMP_MINE)

print("\nPLATTFORM-FIX-TEST BESTANDEN.")
print("Vorher: _build_platform_chain bekam task=None -> Pionier tat nichts.")
print("Jetzt:  Kette ist im _PLAN registriert -> Pionier baut Plattformen.")
