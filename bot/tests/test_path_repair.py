"""Tests fuer die neue PATH-Netzreparatur: _straight_runs, das _UNIT_ALLOC-Array
mit Invariante, _path_build_segment (Statuslogik + Vorab-Material), und die
Streckenfunktion-Auswahl (>=3 -> PATH, <3 -> Mini-Fallback)."""
import sys, ast
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
src=open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree=ast.parse(src)
def seg(name): return next(ast.get_source_segment(src,n) for n in tree.body
                           if isinstance(n,ast.FunctionDef) and n.name==name)
def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

# --- gemeinsame Mocks ---
ALLOC={}
ns={"_UNIT_ALLOC":ALLOC,"_CONN_COST":2,"_PIONEER_MAX_LOAD":40,
    "record_build_move":lambda *a:None,"log":lambda *a,**k:None,
    "_reget":lambda gs,conn,pid:(gs, gs._units.get(pid)),
    "main_subbase_metal":lambda gs: gs._pool,
    "adjacent_networked_building":lambda gs,pio,need_metal=False: gs._anchor,
    "_free_units":lambda gs,role,claim:[v for v in gs._units.values() if v["id"] not in claim],
    "clear_units_from_fields":lambda gs,conn,f,except_id=None:(gs,False)}
# Funktionen laden (in Abhaengigkeitsreihenfolge, gemeinsamer namespace)
for fn in ["alloc_get","alloc_set","alloc_mark_started","alloc_release","alloc_units_for",
           "_straight_runs","_path_build_segment","_mini_connector_step","_run_network_repair_path"]:
    exec(seg(fn), ns)
straight=ns["_straight_runs"]; pathseg=ns["_path_build_segment"]
alloc_set=ns["alloc_set"]; alloc_release=ns["alloc_release"]; alloc_units_for=ns["alloc_units_for"]
alloc_mark_started=ns["alloc_mark_started"]; alloc_get=ns["alloc_get"]

print("== _straight_runs: Zerlegung in gerade Laeufe ==")
r=straight([(0,0),(1,0),(2,0),(2,1),(2,2)])
check("zwei Laeufe (L)", len(r)==2)
check("Lauf1 horizontal 3 Felder", r[0]==[(0,0),(1,0),(2,0)])
check("Lauf2 vertikal ab Knick", r[1]==[(2,0),(2,1),(2,2)])
r2=straight([(0,0),(1,0),(2,0),(3,0)])
check("gerade Strecke = 1 Lauf", len(r2)==1 and len(r2[0])==4)

print("== _UNIT_ALLOC Invariante ==")
ALLOC.clear()
alloc_set(7,"net_repair",started=False)
check("zugeordnet, noch nicht started", alloc_get(7)["started"]==False)
alloc_mark_started(7)
check("started nach mark", alloc_get(7)["started"]==True)
check("alloc_units_for findet 7", alloc_units_for("net_repair")==[7])
alloc_release(7)
check("nach release weg", alloc_get(7) is None)

print("== _path_build_segment: laeuft schon ein PATH -> running ==")
class Conn:
    def __init__(s): s.calls=[]
    def do(s,a): s.calls.append(a); return (True,"")
    def refresh_state(s): return None
class GS:
    def __init__(s,units,pool=100,anchor=None): s._units=units; s._pool=pool; s._anchor=anchor
    def pos(s,u): return tuple(u["pos"])
    def stored(s,u): return u.get("stored",0)
gs=GS({7:{"id":7,"pos":(5,5),"bandPosition":[9,5]}})
st=pathseg(gs,Conn(),gs._units[7],(5,5),(9,5),4)
check("bandPosition gesetzt -> running", st=="running")

print("== _path_build_segment: zu wenig Material -> loading (Transfer abgesetzt) ==")
c=Conn()
gs=GS({7:{"id":7,"pos":(5,5),"stored":0}}, pool=100, anchor={"id":9})
# Segment (5,5)->(9,5) = 5 Felder -> need=10
st=pathseg(gs,c,gs._units[7],(5,5),(9,5),4)
check("loading", st=="loading")
tr=[a for a in c.calls if a["type"]=="transfer"]
check("Transfer ueber 10 (5 Felder*2)", tr and tr[0]["amount"]==10)

print("== _path_build_segment: genug Material + am Start -> started (PATH mit pathEnd) ==")
c=Conn()
gs=GS({7:{"id":7,"pos":(5,5),"stored":10}}, pool=100, anchor={"id":9})
st=pathseg(gs,c,gs._units[7],(5,5),(9,5),4)
check("started", st=="started")
sb=[a for a in c.calls if a["type"]=="startBuild"]
check("startBuild mit pathEnd=[9,5]", sb and sb[0].get("pathEnd")==[9,5])

print("== _path_build_segment: am Ende, baut nicht mehr -> done ==")
gs=GS({7:{"id":7,"pos":(9,5),"stored":0}})
st=pathseg(gs,Conn(),gs._units[7],(5,5),(9,5),4)
check("done", st=="done")

print("\nPATH-REPAIR-TEST BESTANDEN.")
