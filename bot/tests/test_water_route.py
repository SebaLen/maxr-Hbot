"""Testet den Wasser-Routing-Fix: route_fields findet eine Kopplungsroute auch ueber
offenes (unplattformiertes) Wasser - weil Pionier amphibisch und Kopplung factorSea=1.
Echtes T_BLOCKED-Sperrgelaende blockiert weiterhin."""
import sys, ast
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
import maxr_bot_lib as M
GameState=M.GameState
T_WATER=M.T_WATER; T_BLOCKED=M.T_BLOCKED; T_LAND=getattr(M,"T_LAND","#")
def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

# Minimaler GameState-Stub: nur was route_fields braucht.
class GS(GameState):
    def __init__(s, terrain):
        s._terr=terrain          # dict (x,y)->T_*
        s._blockers=set()
        s._main=set()
    def in_bounds(s,x,y): return 0<=x<10 and 0<=y<10
    def terrain_at(s,x,y): return s._terr.get((x,y), T_LAND)
    def foreign_blocking_fields(s): return s._blockers
    def main_component(s): return s._main

print("== route_fields ueber offenes Wasser: findet Pfad (vorher leer) ==")
# Strecke (2,2)->(5,2): die 3 Felder dazwischen sind WASSER ohne Plattform.
terr={(3,2):T_WATER,(4,2):T_WATER,(5,2):T_WATER}
gs=GS(terr)
path=gs.route_fields((2,2),(5,2))
check("Pfad gefunden (nicht leer)", len(path)>0)
check("Wasserfelder enthalten", (3,2) in path and (4,2) in path)

print("== echtes Sperrgelaende blockiert weiterhin ==")
# Direkter Weg (3,2)..(5,2) gesperrt -> A* muss aussen rum oder scheitern.
terr2={(3,2):T_BLOCKED,(4,2):T_BLOCKED,(5,2):T_WATER,
       (3,1):T_BLOCKED,(4,1):T_BLOCKED,(3,3):T_BLOCKED,(4,3):T_BLOCKED,
       (3,0):T_BLOCKED,(4,0):T_BLOCKED}
gs2=GS(terr2)
# Ziel (5,2) ist hinter einer Sperrmauer -> sollte (bei dieser Mauer) umgangen oder leer.
path2=gs2.route_fields((2,2),(5,2))
# Mindestens darf KEIN T_BLOCKED-Feld im Pfad sein.
no_blocked=all(gs2.terrain_at(x,y)!=T_BLOCKED for (x,y) in path2 if (x,y)!=(5,2))
check("kein T_BLOCKED-Feld im Pfad", no_blocked)

print("== fremdes Objekt wird umgangen ==")
terr3={(3,2):T_WATER,(4,2):T_WATER,(5,2):T_WATER}
gs3=GS(terr3); gs3._blockers={(4,2)}   # fremdes Objekt mitten auf der Geraden
path3=gs3.route_fields((2,2),(5,2))
check("Pfad meidet fremdes Feld (4,2)", (4,2) not in path3)
check("trotzdem Pfad gefunden", len(path3)>0)

print("\nWATER-ROUTE-TEST BESTANDEN.")
