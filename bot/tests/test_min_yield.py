"""Testet den Mindestwert-Filter in _expansion_candidates_scored: 2x2-Flaechen mit
Gesamtausbeute <= 2 (z. B. nur 1-2 Oel) werden NICHT als Kandidat angeboten;
Flaechen mit >2 schon."""
import sys, ast, types
sys.path.insert(0,"/home/claude/maxr/src/botbridge/Bot_code")
import maxr_bot_lib as lib

def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n

# Minimaler GameState-Stub: nur was _expansion_candidates_scored braucht.
GS=lib.GameState.__new__(lib.GameState)
def mk(lookup):
    g=lib.GameState.__new__(lib.GameState)
    g._lookup=lookup
    g.EXPANSION_K=5
    g._resource_lookup=lambda: lookup
    g.base_reference_field=lambda:(0,0)
    g.my_mines=lambda:[]
    g.mine_is_networked=lambda m:False
    g._demand_factor=lambda:{"metal":1.0,"oil":1.0,"gold":1.0}
    g.footprint=lambda pos,big:set()
    g.pos=lambda u:(0,0)
    g.mine_build_position=lambda a,target_type=None:(a[0],a[1])
    g.mine_build_position_with_platforms=lambda a,target_type=None:(a[0],a[1])
    return g

impl=lib.GameState._expansion_candidates_scored

print("== Feld mit nur 2 Oel -> KEIN Kandidat ==")
g=mk({(10,10):{"type":"oil","amount":2}})
res=impl(g)
check("kein Kandidat bei Gesamtausbeute 2", len(res)==0)

print("== Feld mit nur 1 Oel -> KEIN Kandidat ==")
g=mk({(10,10):{"type":"oil","amount":1}})
check("kein Kandidat bei 1 Oel", len(impl(g))==0)

print("== Feld mit 3 Oel -> Kandidat ==")
g=mk({(10,10):{"type":"oil","amount":3}})
check("Kandidat bei 3 Oel", len(impl(g))>0)

print("== Feld mit 2 Oel + 1 Gold (Summe 3) -> Kandidat ==")
g=mk({(10,10):{"type":"oil","amount":2},(11,10):{"type":"gold","amount":1}})
check("Kandidat bei Summe 3", len(impl(g))>0)

print("== force_type bleibt unberuehrt (eigene Schwellen) ==")
g=mk({(10,10):{"type":"oil","amount":2}})
check("force_type oil: 2-Oel-Feld weiterhin Kandidat (kein <=2-Filter)",
      len(impl(g,force_type="oil"))>0)

print("\nMIN-YIELD-TEST BESTANDEN.")
