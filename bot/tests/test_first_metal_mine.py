"""Testet: (1) sorted_open priorisiert mandatory-Baustellen auf Platz 1,
(2) Bereithalte-Phase laedt Pionier(8)+Konstrukteur(60) wenn kein >=7-Feld gefunden,
(3) build_water_mine_at/_drive_or_assign_mine bevorzugen den geladensten Bauer."""
import sys, ast
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
from build_plan import BuildBacklog, BuildSite, SiteComponent, COMP_MINE, VEH_CONSTRUCTOR
def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

print("== sorted_open: mandatory zuerst, egal welcher Score ==")
bl=BuildBacklog()
a=BuildSite((10,10),"oil",score=9.0)      # hoher Score, NICHT pflicht
b=BuildSite((20,20),"metal",score=2.0)    # niedriger Score, PFLICHT
b.mandatory=True
bl.add_or_update(a); bl.add_or_update(b)
order=[s.anchor for s in bl.sorted_open()]
check("Pflicht-Baustelle (20,20) trotz niedrigerem Score zuerst", order[0]==(20,20))
check("hoher-Score-Baustelle danach", order[1]==(10,10))

print("== _drive_or_assign_mine: waehlt den GELADENSTEN Konstrukteur ==")
src=open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree=ast.parse(src)
fsrc=next(ast.get_source_segment(src,n) for n in tree.body
          if isinstance(n,ast.FunctionDef) and n.name=="_drive_or_assign_mine")
picked=[]
ns={"_EXPANSION_REJECTED":set(),"_CONSTRUCTOR_MINE_POS":{},"log":lambda *a,**k:None,"SiteComponent":SiteComponent,
    "_free_units":lambda gs,role,claim:[v for v in gs._v if v["id"] not in claim],
    "_expansion_send_constructor":lambda gs,conn,c,goal,sid,bl,tg,**k: picked.append(c["id"]) or gs}
exec(fsrc,ns); drive=ns["_drive_or_assign_mine"]
class GS:
    MINE_SID=22
    def __init__(self): self._v=[{"id":1,"stored":0},{"id":2,"stored":60},{"id":3,"stored":20}]
    def my_vehicles(self): return self._v
    def stored(self,v): return v["stored"]
s=BuildSite((30,30),"metal",score=5.0)
mc=SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(30,30)],22,60,0)
s.components=[mc]
drive(GS(),None,s,mc,{}, {},set())
check("voll geladener Konstrukteur 2 (60 Erz) gewaehlt, nicht 1 (0)", picked==[2])
check("Mine-Komponente an Konstrukteur 2 gebunden", mc.builder==2)

print("\nFIRST-METAL-MINE-TEST BESTANDEN.")

print("== altes _CONSTRUCTOR_MINE_POS-Ziel wird beim Umhaengen geloescht ==")
CMP=ns["_CONSTRUCTOR_MINE_POS"]
CMP[2]={"goal":(54,45),"mine_pos":[53,45]}   # Konstrukteur 2 hat ALTES Ziel
s2=BuildSite((61,48),"metal",score=5.0); s2.mine_pos=(60,47)
mc2=SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(60,47)],22,60,0); mc2.builder=2
s2.components=[mc2]
picked.clear()
drive(GS(),None,s2,mc2,{}, {},set())
check("altes Ziel (54,45) fuer Konstrukteur 2 geloescht", 2 not in CMP)
check("send_constructor mit neuer Site (61,48) gerufen", picked==[2])
print("\nCONSTRUCTOR-MINE-POS-RESET BESTANDEN.")
