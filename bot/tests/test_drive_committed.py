"""Testet _drive_committed_component: ruft je comp.kind die richtige Bauroutine
mit dem committeten Bauer auf (Mine->send_constructor, Plattform->platform_chain,
Kopplung->build_connector)."""
import sys, ast, types
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
from build_plan import (BuildSite, SiteComponent, COMP_MINE, COMP_PLATFORM,
                        COMP_CONNECTOR, COMP_CLEAR, VEH_CONSTRUCTOR, VEH_ENGINEER, VEH_BULLDOZER)

src=open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree=ast.parse(src)
fsrc=next(ast.get_source_segment(src,n) for n in tree.body
          if isinstance(n,ast.FunctionDef) and n.name=="_drive_committed_component")

calls=[]
ns={
 "COMP_PLATFORM":COMP_PLATFORM,"COMP_MINE":COMP_MINE,"COMP_CONNECTOR":COMP_CONNECTOR,
 "_EXPANSION_REJECTED":set(),
 "_build_platform_chain":lambda gs,conn,b: calls.append(("platform",b["id"])) or gs,
 "_expansion_send_constructor":lambda gs,conn,b,goal,sid,bl,tg,**k: calls.append(("mine",b["id"],goal))or gs,
 "_expansion_build_connector":lambda gs,conn,b,goal,bl,tg: calls.append(("connector",b["id"],goal))or gs,
}
exec(fsrc,ns); drive=ns["_drive_committed_component"]

def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

class MockGS:
    MINE_SID=22
    def __init__(self): self._v=[{"id":10},{"id":15},{"id":17}]
    def my_vehicles(self): return self._v

gs=MockGS(); claim=set()

print("== Mine committed an Konstrukteur 10 -> send_constructor(10) ==")
s=BuildSite((54,47),"metal",score=5.0); s.mine_pos=(54,47)
mc=SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(54,47)],22,60,0); mc.builder=10
s.components=[mc]
calls.clear(); drive(gs,None,s,mc,{}, {},claim)
check("send_constructor mit Bauer 10 gerufen", calls and calls[0][0]=="mine" and calls[0][1]==10)
check("Ziel = site.anchor (54,47)", calls[0][2]==(54,47))
check("Bauer 10 in claim", 10 in claim)

print("== Plattform committed an Pionier 15 -> platform_chain(15) ==")
s2=BuildSite((52,43),"oil",score=3.0); s2.mine_pos=(51,42)
pc=SiteComponent(COMP_PLATFORM,VEH_ENGINEER,[(52,42)],25,2,0); pc.builder=15
s2.components=[pc]
calls.clear(); drive(gs,None,s2,pc,{}, {},claim)
check("platform_chain mit Bauer 15 gerufen", calls and calls[0]==("platform",15))

print("== Kopplung committed an Pionier 17 -> build_connector(17) ==")
s3=BuildSite((60,60),"metal",score=2.0); s3.mine_pos=(60,60)
cc=SiteComponent(COMP_CONNECTOR,VEH_ENGINEER,[(60,60)],33,6,0); cc.builder=17
s3.components=[cc]
calls.clear(); drive(gs,None,s3,cc,{}, {},claim)
check("build_connector mit Bauer 17 gerufen", calls and calls[0][0]=="connector" and calls[0][1]==17)

print("== Toter Bauer (nicht in my_vehicles) -> kein Aufruf, kein Absturz ==")
s4=BuildSite((70,70),"metal",score=1.0); s4.mine_pos=(70,70)
mc4=SiteComponent(COMP_MINE,VEH_CONSTRUCTOR,[(70,70)],22,60,0); mc4.builder=999
s4.components=[mc4]
calls.clear(); drive(gs,None,s4,mc4,{}, {},claim)
check("kein Routinen-Aufruf fuer toten Bauer", not calls)

print("\nDRIVE-COMMITTED-TEST BESTANDEN.")
