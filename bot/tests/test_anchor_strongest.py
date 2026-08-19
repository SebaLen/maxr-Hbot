"""Testet: nach derive_components liegt site.anchor auf dem STAERKSTEN Rohstofffeld
der gewaehlten 2x2 (z. B. dem 11er-Metallfeld), nicht auf einem schwaecheren Feld."""
import sys
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
from build_plan import BuildSite, COMP_MINE
def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

# Terrain-Stub: liefert mine_pos und Feld-Rohstoffmengen.
class Terrain:
    MINE_SID=22
    def __init__(self, amounts, mine_pos):
        self._amt=amounts; self._mp=mine_pos
    def build_cost(self,sid): return 12
    def mine_covering(self,f): return None
    def mine_build_position(self,anchor,target_type=None): return self._mp
    def mine_build_position_with_platforms(self,anchor,target_type=None): return self._mp
    def platform_fields_needed(self,mp): return []   # Land, keine Plattformen
    def field_resource_amount(self,x,y): return self._amt.get((x,y),0)
    def rubble_on_fields(self,f): return []

print("== Anker wandert auf das staerkste Feld der 2x2 ==")
# 2x2 = (59,49),(60,49),(59,50),(60,50). 11-Metall liegt auf (59,50).
# Urspruenglicher Anker absichtlich FALSCH auf (60,49) mit nur 2.
amounts={(59,49):3,(60,49):2,(59,50):11,(60,50):4}
s=BuildSite((60,49),"metal",amount=2,score=1.0)
t=Terrain(amounts, mine_pos=(59,49))
ok=s.derive_components(t, 22, 25, 4)
check("derive_components erfolgreich", ok)
check("mine_pos = (59,49)", s.mine_pos==(59,49))
check("Anker auf staerkstem Feld (59,50)=11", s.anchor==(59,50))

print("== leeres Vorkommen aendert Anker nicht (kein Crash) ==")
amounts2={}
s2=BuildSite((10,10),"metal",amount=0,score=0.1)
t2=Terrain(amounts2, mine_pos=(10,10))
s2.derive_components(t2,22,25,4)
check("Anker bleibt (10,10) wenn nirgends Rohstoff", s2.anchor==(10,10))

print("\nANCHOR-STRONGEST-TEST BESTANDEN.")
