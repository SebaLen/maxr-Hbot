"""Prueft _site_build_pos: Mine -> freies 8er-Nachbarfeld (nicht Footprint),
Plattform/Clear -> eigenes Feld (auf dem gebaut wird)."""
import sys, ast, types
sys.path.insert(0, "/home/claude/maxr/src/botbridge/Bot_code")
from build_plan import (BuildSite, SiteComponent, COMP_PLATFORM, COMP_MINE,
                        COMP_CLEAR, COMP_CONNECTOR, VEH_ENGINEER, VEH_CONSTRUCTOR, VEH_BULLDOZER)

# _site_build_pos aus bot_run extrahieren
src = open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree = ast.parse(src)
fsrc = next(ast.get_source_segment(src, n) for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_site_build_pos")
ns = {"COMP_PLATFORM":COMP_PLATFORM,"COMP_CLEAR":COMP_CLEAR}
exec(fsrc, ns)
_site_build_pos = ns["_site_build_pos"]

def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

class MockGS:
    def __init__(self, free): self._free=set(free)  # erreichbare freie Felder
    def free_neighbors8(self, x, y):
        return [(nx,ny) for nx in (x-1,x,x+1) for ny in (y-1,y,y+1)
                if (nx,ny)!=(x,y) and (nx,ny) in self._free]

print("== Mine: liefert freies Nachbarfeld, NICHT das Footprint-Feld ==")
s = BuildSite((20,20),"metal",score=5.0); s.mine_pos=(20,20)
s.components=[SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(20,20)],22,60,0)]
# Footprint = (20,20),(21,20),(20,21),(21,21). Frei daneben: (19,20).
gs = MockGS(free=[(19,20),(19,21)])
pos = _site_build_pos(gs, s, s.components[0])
fp = {(20,20),(21,20),(20,21),(21,21)}
check(f"Mine-Ziel ist Nachbarfeld {pos}, nicht im Footprint", pos not in fp)
check("Mine-Ziel ist ein freies Feld", pos in {(19,20),(19,21)})

print("== Plattform: eigenes Feld (wird dort gebaut) ==")
s2 = BuildSite((30,30),"metal",score=4.0); s2.mine_pos=(30,30)
pc = SiteComponent(COMP_PLATFORM,VEH_ENGINEER,[(30,29)],40,2,0)
s2.components=[pc]
pos2 = _site_build_pos(MockGS([]), s2, pc)
check("Plattform-Ziel = Plattformfeld selbst", pos2==(30,29))

print("== Schrott: eigenes Feld ==")
cc = SiteComponent(COMP_CLEAR,VEH_BULLDOZER,[(31,31)],None,0,-100)
check("Clear-Ziel = Schrottfeld selbst",
      _site_build_pos(MockGS([]), s2, cc)==(31,31))

print("\n_SITE_BUILD_POS-TEST BESTANDEN.")
