"""Testet _sync_backlog_state: real fertige Komponenten -> S_DONE + builder None,
due_component rueckt zur naechsten weiter."""
import sys, ast
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
from build_plan import (BuildBacklog, BuildSite, SiteComponent, COMP_PLATFORM,
                        COMP_MINE, COMP_CONNECTOR, VEH_ENGINEER, VEH_CONSTRUCTOR)

# _sync_backlog_state aus bot_run isoliert laden, _BACKLOG injizieren
src = open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree = ast.parse(src)
fsrc = next(ast.get_source_segment(src,n) for n in tree.body
            if isinstance(n,ast.FunctionDef) and n.name=="_sync_backlog_state")
bl = BuildBacklog()
ns = {"_BACKLOG":bl,"SiteComponent":SiteComponent,"COMP_PLATFORM":COMP_PLATFORM,
      "COMP_MINE":COMP_MINE,"COMP_CONNECTOR":COMP_CONNECTOR}
exec(fsrc, ns)
sync = ns["_sync_backlog_state"]

def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

class MockGS:
    def __init__(self, plats_done, mine, networked):
        self._pd=plats_done; self._mine=mine; self._net=networked
    def mine_covering(self, field): return self._mine
    def platform_fields_needed(self, mp): return [] if self._pd else [(0,0)]
    def mine_is_networked(self, m): return self._net

print("== Plattform gebaut -> Plattform DONE, builder frei, Mine wird faellig ==")
s = BuildSite((52,43),"oil",score=3.0); s.mine_pos=(51,42)
plat = SiteComponent(COMP_PLATFORM,VEH_ENGINEER,[(52,42)],25,2,0); plat.builder=15
mine = SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(51,42)],22,60,1)
conn = SiteComponent(COMP_CONNECTOR,VEH_ENGINEER,[(51,42)],33,6,2)
s.components=[plat,mine,conn]
bl.add_or_update(s)
n = sync(MockGS(plats_done=True, mine=None, networked=False))
check("1 Komponente synchronisiert", n==1)
check("Plattform ist DONE", plat.is_done())
check("Plattform-builder freigegeben", plat.builder is None)
check("due_component ist jetzt die MINE", s.due_component().kind==COMP_MINE)

print("== Mine gebaut -> Mine DONE, Kopplung wird faellig ==")
n = sync(MockGS(plats_done=True, mine={"id":99}, networked=False))
check("Mine als fertig erkannt", mine.is_done())
check("due_component ist jetzt CONNECTOR", s.due_component().kind==COMP_CONNECTOR)

print("== Kopplung fertig (Mine angebunden) -> alles DONE ==")
n = sync(MockGS(plats_done=True, mine={"id":99}, networked=True))
check("Kopplung als fertig erkannt", conn.is_done())
check("due_component ist None (alles fertig)", s.due_component() is None)

print("== Nichts fertig -> kein Sync, Plattform bleibt faellig ==")
s2 = BuildSite((60,60),"oil",score=2.0); s2.mine_pos=(60,60)
p2 = SiteComponent(COMP_PLATFORM,VEH_ENGINEER,[(60,60)],25,2,0)
s2.components=[p2, SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(60,60)],22,60,1)]
bl2 = BuildBacklog(); bl2.add_or_update(s2)
ns["_BACKLOG"]=bl2; exec(fsrc, ns); sync2=ns["_sync_backlog_state"]
n = sync2(MockGS(plats_done=False, mine=None, networked=False))
check("0 synchronisiert (nichts real fertig)", n==0)
check("Plattform bleibt faellig", s2.due_component().kind==COMP_PLATFORM)

print("\nSTATE-SYNC-TEST BESTANDEN.")
