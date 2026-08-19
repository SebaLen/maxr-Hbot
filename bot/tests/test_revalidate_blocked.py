"""Testet _revalidate_blocked_sites: blockierte mine_pos (Gebaeude/fremd) ->
(a) Anker noch ueber andere 2x2 bebaubar -> neu fixiert; (b) keine Alternativlage
-> Baustelle verworfen, Feld gesperrt, Konstrukteur frei; (c) eigene mobile Einheit
zaehlt NICHT als Blocker; (d) freie Flaeche -> nichts."""
import sys, ast
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
from build_plan import BuildBacklog, BuildSite, SiteComponent, COMP_MINE, COMP_CONNECTOR, VEH_CONSTRUCTOR, VEH_ENGINEER
src=open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree=ast.parse(src)
fsrc=next(ast.get_source_segment(src,n) for n in tree.body
          if isinstance(n,ast.FunctionDef) and n.name=="_revalidate_blocked_sites")
BL=BuildBacklog(); REJ=set()
ns={"_BACKLOG":BL,"_EXPANSION_REJECTED":REJ,"COMP_MINE":COMP_MINE,
    "SiteComponent":SiteComponent,"log":lambda *a,**k:None}
exec(fsrc,ns); reval=ns["_revalidate_blocked_sites"]
def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

def mk_site(anchor, mine_pos, builder=99):
    s=BuildSite(anchor,"oil",score=1.0); s.mine_pos=mine_pos
    mc=SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[mine_pos],22,12,1); mc.builder=builder
    cc=SiteComponent(COMP_CONNECTOR,VEH_ENGINEER,[anchor],4,2,2)
    s.components=[mc,cc]; return s,mc

class GS:
    MINE_SID=22
    def __init__(self, occ, alt=None):
        self._occ=set(occ); self._alt=alt
    def building_sid_by_name(self,n): return {"platform":25,"connector":4}.get(n)
    def occupied_fields_for_mine(self): return self._occ
    def mine_covering(self,f): return None
    def mine_build_position(self,anchor,target_type=None): return self._alt
    def mine_build_position_with_platforms(self,anchor,target_type=None): return None
    def pos(self,v): return (0,0)

print("== (b) blockiert durch Gebaeude, keine Alternativlage -> verworfen ==")
BL._sites=[]; REJ.clear()
s,mc=mk_site((54,45),(53,45)); BL.add_or_update(s)
# (53,45) liegt im occ (Gebaeude), keine Alternativlage (alt=None)
refixed,dropped=reval(GS(occ={(53,45)}, alt=None))
check("Baustelle verworfen", dropped==1 and len(BL)==0)
check("Feld gesperrt", (54,45) in REJ)
check("Konstrukteur-Bindung geloest", mc.builder is None)

print("== (c) eigene mobile Einheit ist KEIN Blocker (nicht in occ) -> nichts ==")
BL._sites=[]; REJ.clear()
s,mc=mk_site((54,45),(53,45)); BL.add_or_update(s)
# occ leer (occupied_fields_for_mine schliesst eigene mobile Einheiten aus)
refixed,dropped=reval(GS(occ=set(), alt=None))
check("nicht verworfen", dropped==0 and len(BL)==1)
check("Bindung bleibt", mc.builder==99)

print("== (d) Flaeche frei -> nichts ==")
BL._sites=[]; REJ.clear()
s,mc=mk_site((10,10),(10,10)); BL.add_or_update(s)
refixed,dropped=reval(GS(occ={(99,99)}, alt=None))
check("nichts passiert", refixed==0 and dropped==0 and len(BL)==1)

print("\nREVALIDATE-BLOCKED-TEST BESTANDEN.")
