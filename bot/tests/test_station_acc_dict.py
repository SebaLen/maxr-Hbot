"""Regression: der Station-Trigger in mode_expansion_backlog ruft next_task_for_role mit
einem dict-AKKUMULATOR auf (nicht dem BuildPlan-Objekt _PLAN). Der Aufruf darf NIE crashen,
auch wenn keine Station gebaut wird und der constructor-Pfad zu Fabriken/Gold weiterlaeuft
(der nutzt plan[...] hart). Frueher: plan=_PLAN -> AttributeError 'BuildPlan' has no 'get'."""
import ast, sys, math
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
src=open("/home/claude/maxr/src/botbridge/Bot_code/bot_run.py").read()
tree=ast.parse(src)
def seg(n): return next(ast.get_source_segment(src,x) for x in tree.body
                        if isinstance(x,ast.FunctionDef) and x.name==n)
def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

# 1. Code-Pfad: mode_expansion_backlog uebergibt KEIN _PLAN an next_task_for_role,
#    sondern ein dict mit allen noetigen Keys.
mb=seg("mode_expansion_backlog")
check("kein next_task_for_role(..., _PLAN)", "next_task_for_role(gs, \"constructor\", _PLAN)" not in mb)
check("nutzt _acc_station dict", "_acc_station" in mb and "next_task_for_role(gs, \"constructor\", _acc_station)" in mb)
for key in ["smallfactory","bigfactory","generators","radar","storage_metal",
            "storage_oil","storage_gold","gold_refinery","stations"]:
    check(f"_acc_station hat Key '{key}'", f'"{key}": 0' in mb.split("_acc_station =")[1][:300] or f'"{key}":0' in mb)

# 2. Laufzeit: next_task_for_role mit dem dict darf NICHT crashen (kein Station-Fall
#    -> laeuft bis zu den Fabriken weiter, die plan[...] hart lesen).
ns={"math":math,"_MINES_PER_STATION":4,"_ENERGY_LOAD_RATIO":0.0,
    "emergency_metal_pool":lambda gs:0,  # 0 -> Station NICHT (zwingt Weiterlauf)
    "_plan_energy":lambda gs,plan:999,"log":lambda *a,**k:None}
exec(seg("next_task_for_role"), ns)
nf=ns["next_task_for_role"]
class GS:
    MINE_ENERGY_NEED=1; GOLD_STORE_RES_TYPE=2
    def building_sid_by_name(s,n): return {"smallfactory":13,"bigfactory":33,"Energy_Big":7}.get(n)
    def mine_count(s): return 6
    def station_count_incl_construction(s): return 0
    def fuel_for_station_ok(s): return True
    def station_viable_by_freeing_generators(s): return (False,0)
    def build_cost(s,sid): return 8
    def energy_potential_need(s): return 0
    def gold_refinery_sid(s): return None
    def gold_income(s): return 0
    def factory_available(s,n): return True
    def count_gold_refineries_incl_construction(s): return 0
acc={"storage_metal":0,"storage_oil":0,"generators":0,"stations":0,"radar":0,
     "smallfactory":0,"bigfactory":0,"storage_gold":0,"gold_refinery":0}
try:
    r=nf(GS(),"constructor",acc)
    check("kein Crash mit dict-Akkumulator (Station unfinanzierbar -> Weiterlauf)", True)
    check("liefert None (Fabriken verfuegbar, kein Bau noetig)", r is None)
except Exception as e:
    check(f"kein Crash (war: {type(e).__name__}: {e})", False)

print("\nSTATION-ACC-DICT-TEST BESTANDEN.")
