import sys
sys.path.insert(0, "/home/claude/maxr/src/botbridge/Bot_code")
from build_plan import BuildBacklog, BuildSite

# Nachbildung der dedup-Logik aus mark_expansion_backlog
def mark(gs, backlog, rejected):
    mine_sid, plat_sid, conn_sid = gs.MINE_SID, 40, 41
    backlog.prune_done(gs)
    cands = gs.expansion_candidates(blocked_fields=rejected)
    existing_minepos = {s.mine_pos for s in backlog.sorted_open() if s.mine_pos is not None}
    best = {}
    for (ax, ay, atyp, aamt, score) in cands:
        if backlog.site_at((ax, ay)) is not None: continue
        cand = BuildSite((ax, ay), atyp, amount=aamt, score=score)
        if not cand.derive_components(gs, mine_sid, plat_sid, conn_sid): continue
        mp = cand.mine_pos
        if mp in existing_minepos: continue
        if mp not in best or score > best[mp][0]:
            best[mp] = (score, cand)
    for (score, site) in best.values():
        backlog.add_or_update(site)
    backlog.resolve_overlaps()

class MockGS:
    MINE_SID = 22
    def __init__(self, cands, land): self._c, self._l = cands, land
    def building_sid_by_name(self, n): return {"platform":40,"connector":41}.get(n)
    def expansion_candidates(self, blocked_fields=None): return self._c
    def mine_covering(self, f): return None
    def mine_build_position(self, a, target_type=None): return self._l.get(tuple(a))
    def mine_build_position_with_platforms(self, a, target_type=None): return None
    def platform_fields_needed(self, p): return None
    def build_cost(self, sid): return {22:60,40:2,41:2}.get(sid,0)
    def rubble_on_fields(self, f): return []

# Zwei Anker (52,37) score 1.23 und (53,36) score 1.53 -> beide mine_pos (52,36)
gs = MockGS(cands=[(52,37,"oil",5,1.23),(53,36,"metal",9,1.53),(10,10,"metal",9,5.0)],
            land={(52,37):(52,36),(53,36):(52,36),(10,10):(10,10)})
bl = BuildBacklog()
mark(gs, bl, set())
def check(n,c): print(f"  [{'OK ' if c else 'FAIL'}] {n}"); assert c,n
check("nur 2 Baustellen (52,36 dedupliziert + 10,10)", len(bl)==2)
sites = {s.mine_pos: s for s in bl.sorted_open()}
check("fuer mine_pos (52,36) gewann der hoehere Score (1.53)",
      sites[(52,36)].score==1.53 and sites[(52,36)].anchor==(53,36))
# Idempotenz: erneut markieren erzeugt KEINE neuen + verwirft KEINE
n_before=len(bl)
mark(gs, bl, set())
check("idempotent: keine neuen Sites, kein Flackern", len(bl)==n_before==2)
check("(52,36) bleibt bei Anker (53,36)", sites[(52,36)].anchor==(53,36))
print("\nDEDUP-TEST BESTANDEN.")
