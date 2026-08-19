"""Testet _object_present: erkennt ein IM BAU befindliches grosses Gebaeude."""
import sys, ast
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
src=open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree=ast.parse(src)
fsrc=next(ast.get_source_segment(src,n) for n in tree.body
          if isinstance(n,ast.FunctionDef) and n.name=="_object_present")
ns={}; exec(fsrc,ns); _object_present=ns["_object_present"]

def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

class MockGS:
    def __init__(self, buildings, vehicles): self._b=buildings; self._v=vehicles
    def my_buildings(self): return self._b
    def unit_type(self, u): return u["type"]
    def is_big_building_type(self, sid): return sid==11  # 11=bigfactory(2x2)
    def pos(self, u): return tuple(u["pos"])
    def footprint(self, pos, big):
        x,y=pos
        return {(x,y),(x+1,y),(x,y+1),(x+1,y+1)} if big else {(x,y)}
    def vehicles_of_type(self, role):
        return [v for v in self._v if v.get("role")==role]

BF=11
print("== bigfactory IM BAU (Konstrukteur baut, noch nicht in my_buildings) ==")
con = {"id":13,"role":"constructor","pos":[53,45],"isBuilding":True,
       "buildingTyp":{"secondPart":BF}}
gs = MockGS(buildings=[], vehicles=[con])
check("Feld (53,45) als belegt erkannt (im Bau)", _object_present(gs,(53,45),BF))
check("Footprint-Feld (54,46) auch belegt (2x2)", _object_present(gs,(54,46),BF))
check("Fremdes Feld (10,10) NICHT belegt", not _object_present(gs,(10,10),BF))

print("== bigfactory FERTIG (in my_buildings) ==")
gs2 = MockGS(buildings=[{"type":BF,"pos":[53,45]}], vehicles=[])
check("fertige Fabrik erkannt", _object_present(gs2,(53,45),BF))

print("== Bauer baut ANDERES sid -> nicht als dieses Gebaeude zaehlen ==")
con2 = {"id":13,"role":"constructor","pos":[53,45],"isBuilding":True,
        "buildingTyp":{"secondPart":99}}
gs3 = MockGS(buildings=[], vehicles=[con2])
check("anderes sid zaehlt nicht", not _object_present(gs3,(53,45),BF))

print("== Bauer baut NICHT (isBuilding False) -> nicht zaehlen ==")
con3 = {"id":13,"role":"constructor","pos":[53,45],"isBuilding":False,
        "buildingTyp":{"secondPart":BF}}
gs4 = MockGS(buildings=[], vehicles=[con3])
check("nicht-bauender Bauer zaehlt nicht", not _object_present(gs4,(53,45),BF))

print("\n_OBJECT_PRESENT-TEST BESTANDEN.")
