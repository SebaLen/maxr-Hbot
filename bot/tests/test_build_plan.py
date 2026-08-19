"""Isolierter Test der Baustellen-Backlog-Strukturen - ohne laufendes Spiel.
Mockt das schmale 'terrain'-Interface, das derive_components erwartet."""
import sys
sys.path.insert(0, "/home/claude")
from build_plan_new import (
    BuildPlan, BuildTask,                       # alte API muss erhalten sein
    SiteComponent, BuildSite, BuildBacklog, MetalReservation,
    VEH_ENGINEER, VEH_CONSTRUCTOR, VEH_BULLDOZER,
    COMP_PLATFORM, COMP_MINE, COMP_CONNECTOR, COMP_CLEAR,
)

MINE_SID, PLAT_SID, CONN_SID = 22, 40, 41
COST = {MINE_SID: 60, PLAT_SID: 2, CONN_SID: 2}


class MockTerrain:
    """Minimaler Stand-in fuer GameState. Steuert ueber Konstruktor, ob das
    Vorkommen Land/Wasser ist, wie viele Plattformen fehlen, und ob schon eine
    Mine steht."""
    def __init__(self, land_pos=None, water_pos=None, platforms_needed=None,
                 mine_present=False, rubble=None):
        self._land = land_pos          # (ox,oy) oder None
        self._water = water_pos        # (ox,oy) oder None
        self._pf = platforms_needed    # Liste Felder oder None
        self._mine = mine_present
        self._rubble = set(rubble or [])  # Felder mit Schrott

    def rubble_on_fields(self, fields):
        return [f for f in fields if tuple(f) in self._rubble]

    def mine_build_position(self, anchor, target_type=None):
        return self._land

    def mine_build_position_with_platforms(self, anchor, target_type=None):
        return self._water

    def platform_fields_needed(self, mine_pos):
        return self._pf

    def build_cost(self, sid):
        return COST.get(sid, 0)

    def mine_covering(self, field):
        return {"id": 999} if self._mine else None


def check(name, cond):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")
    assert cond, name


print("== Alte API erhalten ==")
p = BuildPlan()
t = p.assign((5, 5), 13, builder=1)
check("BuildPlan.assign liefert BuildTask", isinstance(t, BuildTask))
check("tasks_for findet ihn", len(p.tasks_for(1)) == 1)

print("== Landmine: nur Mine + Kopplung ==")
site = BuildSite((10, 10), "metal", amount=9, score=5.0)
ok = site.derive_components(MockTerrain(land_pos=(9, 9)),
                            MINE_SID, PLAT_SID, CONN_SID)
check("derive erfolgreich", ok)
check("mine_pos fixiert", site.mine_pos == (9, 9))
kinds = [c.kind for c in sorted(site.components, key=lambda c: c.order)]
check(f"Komponenten = [mine, connector] (ist {kinds})",
      kinds == [COMP_MINE, COMP_CONNECTOR])
check("kein Plattform-Pionier noetig",
      VEH_ENGINEER in site.required_vehicle_types()  # Kopplung
      and not any(c.kind == COMP_PLATFORM for c in site.components))
mine_comp = next(c for c in site.components if c.kind == COMP_MINE)
check("Mine kostet 60 Erz", mine_comp.metal_cost == 60)
check("Mine baut der Konstrukteur", mine_comp.vehicle_type == VEH_CONSTRUCTOR)

print("== Wassermine: 4 Plattformen + Mine + Kopplung ==")
pf = [(20, 20), (21, 20), (20, 21), (21, 21)]
wsite = BuildSite((20, 20), "metal", amount=8, score=4.0)
ok = wsite.derive_components(
    MockTerrain(land_pos=None, water_pos=(20, 20), platforms_needed=pf),
    MINE_SID, PLAT_SID, CONN_SID)
check("derive erfolgreich", ok)
kinds = [c.kind for c in sorted(wsite.components, key=lambda c: c.order)]
check(f"Reihenfolge = 4x platform, mine, connector (ist {kinds})",
      kinds == [COMP_PLATFORM]*4 + [COMP_MINE, COMP_CONNECTOR])
check("Plattformen baut der Pionier",
      all(c.vehicle_type == VEH_ENGINEER
          for c in wsite.components if c.kind == COMP_PLATFORM))
check("braucht Pionier UND Konstrukteur",
      wsite.required_vehicle_types() == {VEH_ENGINEER, VEH_CONSTRUCTOR})

print("== Teil-plattformiert: nur 1 Plattform fehlt ==")
psite = BuildSite((30, 30), "oil", score=3.0)
ok = psite.derive_components(
    MockTerrain(water_pos=(30, 30), platforms_needed=[(31, 31)]),
    MINE_SID, PLAT_SID, CONN_SID)
kinds = [c.kind for c in sorted(psite.components, key=lambda c: c.order)]
check(f"genau 1 Plattform (ist {kinds})",
      kinds == [COMP_PLATFORM, COMP_MINE, COMP_CONNECTOR])

print("== Mine steht schon: nur Kopplung ==")
msite = BuildSite((40, 40), "metal", score=2.0)
ok = msite.derive_components(MockTerrain(mine_present=True),
                             MINE_SID, PLAT_SID, CONN_SID)
check("derive erfolgreich", ok)
check("nur Kopplung",
      [c.kind for c in msite.components] == [COMP_CONNECTOR])

print("== Nicht erschliessbar: derive False ==")
nsite = BuildSite((50, 50), "metal", score=1.0)
ok = nsite.derive_components(MockTerrain(),  # weder Land noch Wasser
                             MINE_SID, PLAT_SID, CONN_SID)
check("derive False", ok is False)

print("== due_component: harte Abhaengigkeit ==")
c0 = sorted(wsite.components, key=lambda c: c.order)[0]
check("faellig = erste Plattform", wsite.due_component() is c0)
c0.state = SiteComponent.S_DONE
check("nach DONE -> naechste Plattform",
      wsite.due_component().order == 1)

print("== Schrott auf Landflaeche: Bulldozer-Komponente vorgelagert ==")
# Mine-Pos (9,9) -> 2x2 = (9,9),(10,9),(9,10),(10,10). Schrott auf (10,9).
rsite = BuildSite((10, 10), "metal", amount=9, score=6.0)
ok = rsite.derive_components(
    MockTerrain(land_pos=(9, 9), rubble=[(10, 9)]),
    MINE_SID, PLAT_SID, CONN_SID)
check("derive erfolgreich", ok)
ordered = sorted(rsite.components, key=lambda c: c.order)
kinds = [c.kind for c in ordered]
check(f"clear ZUERST, dann mine, connector (ist {kinds})",
      kinds == [COMP_CLEAR, COMP_MINE, COMP_CONNECTOR])
clear_comp = ordered[0]
check("Schrott raeumt der Bulldozer", clear_comp.vehicle_type == VEH_BULLDOZER)
check("Schrott-Komponente auf dem richtigen Feld", clear_comp.fields == [(10, 9)])
check("Bulldozer in den benoetigten Fahrzeugtypen",
      VEH_BULLDOZER in rsite.required_vehicle_types())

print("== Schrott auf Wasserflaeche: clear + platforms + mine + connector ==")
pf2 = [(20, 20), (21, 20)]
wrsite = BuildSite((20, 20), "metal", score=5.0)
ok = wrsite.derive_components(
    MockTerrain(water_pos=(20, 20), platforms_needed=pf2, rubble=[(20, 21)]),
    MINE_SID, PLAT_SID, CONN_SID)
ordered = sorted(wrsite.components, key=lambda c: c.order)
kinds = [c.kind for c in ordered]
check(f"clear vor platforms vor mine (ist {kinds})",
      kinds == [COMP_CLEAR, COMP_PLATFORM, COMP_PLATFORM, COMP_MINE, COMP_CONNECTOR])

print("== Rueckwaertskompatibel: terrain OHNE rubble_on_fields ==")
class NoRubbleTerrain:
    def mine_build_position(self, a, target_type=None): return (9, 9)
    def mine_build_position_with_platforms(self, a, target_type=None): return None
    def platform_fields_needed(self, p): return None
    def build_cost(self, sid): return COST.get(sid, 0)
    def mine_covering(self, f): return None
nsite2 = BuildSite((10, 10), "metal", score=4.0)
ok = nsite2.derive_components(NoRubbleTerrain(), MINE_SID, PLAT_SID, CONN_SID)
check("ohne rubble_on_fields kein clear (nur mine+connector)",
      [c.kind for c in sorted(nsite2.components, key=lambda c: c.order)]
      == [COMP_MINE, COMP_CONNECTOR])

print("== Backlog: Prioritaet + prune ==")
bl = BuildBacklog()
bl.add_or_update(site)    # score 5
bl.add_or_update(wsite)   # score 4
bl.add_or_update(msite)   # score 2
check("3 Baustellen", len(bl) == 3)
check("hoechste zuerst", bl.sorted_open()[0].score == 5.0)
check("add_or_update am selben Anker fuegt NICHT doppelt hinzu",
      bl.add_or_update(BuildSite((10, 10), "metal")) is False and len(bl) == 3)
# erledigte Baustelle (Mine steht) wird gepruned
bl.prune_done(MockTerrain(mine_present=False))
check("ohne Mine bleibt alles", len(bl) == 3)

print("== MetalReservation: A vor B, Freigabe bei EN_ROUTE ==")
res = MetalReservation()
bl2 = BuildBacklog()
A = BuildSite((10, 10), "metal", score=5.0)
A.derive_components(MockTerrain(water_pos=(10, 10), platforms_needed=[(10, 10)]),
                    MINE_SID, PLAT_SID, CONN_SID)
B = BuildSite((60, 60), "metal", score=3.0)
B.derive_components(MockTerrain(land_pos=(60, 60)), MINE_SID, PLAT_SID, CONN_SID)
bl2.add_or_update(A); bl2.add_or_update(B)

# A: Pionier (Plattform) zugewiesen, laedt noch; Konstrukteur zugewiesen, wartet.
A_plat = next(c for c in A.components if c.kind == COMP_PLATFORM)
A_mine = next(c for c in A.components if c.kind == COMP_MINE)
A_plat.builder = 100; A_plat.state = SiteComponent.S_WAITING
A_mine.builder = 101; A_mine.state = SiteComponent.S_WAITING
# B: Konstrukteur zugewiesen, wartet.
B_mine = next(c for c in B.components if c.kind == COMP_MINE)
B_mine.builder = 200; B_mine.state = SiteComponent.S_WAITING

check("A haelt die Reservierung", res.holding_site(bl2) is A)
check("A-Plattform-Pionier (100) darf laden",
      res.builder_may_load(bl2, 100) is True)
check("A-Konstrukteur (101) darf NICHT laden (Pionier first)",
      res.builder_may_load(bl2, 101) is False)
check("B-Konstrukteur (200) darf NICHT laden (A hat Vorrang)",
      res.builder_may_load(bl2, 200) is False)

# Pionier geladen + losgefahren:
A_plat.state = SiteComponent.S_EN_ROUTE
check("jetzt darf A-Konstrukteur laden",
      res.builder_may_load(bl2, 101) is True)
check("B immer noch nicht (A noch nicht ganz EN_ROUTE)",
      res.builder_may_load(bl2, 200) is False)

# Konstrukteur auch losgefahren -> A komplett EN_ROUTE -> Freigabe:
A_mine.state = SiteComponent.S_EN_ROUTE
check("A alle aktiven Bauer EN_ROUTE", A.all_active_builders_en_route() is True)
check("Reservierung wechselt zu B", res.holding_site(bl2) is B)
check("jetzt darf B-Konstrukteur (200) laden",
      res.builder_may_load(bl2, 200) is True)

print("\nALLE TESTS BESTANDEN.")


# ============ Ueberlappungs-Aufloesung (WISHED vs committed) ============
print("== resolve_overlaps: zwei Vorschlaege, hoeherer Score gewinnt ==")
def mk_site(anchor, mine_pos, score):
    s = BuildSite(anchor, "metal", score=score)
    s.mine_pos = tuple(mine_pos)
    # eine simple mine-Komponente, damit footprint_cells die 2x2 liefert
    mf = [mine_pos, (mine_pos[0]+1, mine_pos[1]),
          (mine_pos[0], mine_pos[1]+1), (mine_pos[0]+1, mine_pos[1]+1)]
    s.components = [SiteComponent(COMP_MINE, VEH_CONSTRUCTOR, mf, MINE_SID, 60, 0)]
    return s

blo = BuildBacklog()
hi = mk_site((10, 10), (10, 10), 5.0)   # ueberlappt mit lo
lo = mk_site((11, 10), (10, 10), 2.0)   # gleiche mine_pos -> ueberlappt
blo.add_or_update(hi); blo.add_or_update(lo)
dropped = blo.resolve_overlaps()
check("1 verworfen", dropped == 1)
check("hoeherer Score (5.0) bleibt", blo.site_at((10, 10)) is not None)
check("schwaecherer (2.0) verworfen", blo.site_at((11, 10)) is None)

print("== resolve_overlaps: committet schlaegt besseren Vorschlag ==")
blo2 = BuildBacklog()
committed = mk_site((20, 20), (20, 20), 1.0)   # niedriger Score, ABER committet
committed.components[0].builder = 99           # -> is_committed() True
proposal = mk_site((21, 20), (20, 20), 9.0)    # hoeher, aber nur Vorschlag
blo2.add_or_update(committed); blo2.add_or_update(proposal)
dropped = blo2.resolve_overlaps()
check("1 verworfen", dropped == 1)
check("committete (Score 1.0) bleibt trotz niedrigerem Score",
      blo2.site_at((20, 20)) is not None)
check("besserer Vorschlag (9.0) verworfen weil er committete ueberlappt",
      blo2.site_at((21, 20)) is None)

print("== resolve_overlaps: zwei committete bleiben beide ==")
blo3 = BuildBacklog()
c1 = mk_site((30, 30), (30, 30), 3.0); c1.components[0].builder = 1
c2 = mk_site((31, 30), (30, 30), 2.0); c2.components[0].builder = 2
blo3.add_or_update(c1); blo3.add_or_update(c2)
dropped = blo3.resolve_overlaps()
check("0 verworfen (committete loeschen sich nicht)", dropped == 0)
check("beide bleiben", len(blo3) == 2)

print("== resolve_overlaps: kein Ueberlapp -> nichts verworfen ==")
blo4 = BuildBacklog()
blo4.add_or_update(mk_site((40, 40), (40, 40), 5.0))
blo4.add_or_update(mk_site((50, 50), (50, 50), 4.0))
dropped = blo4.resolve_overlaps()
check("0 verworfen", dropped == 0)
check("beide bleiben", len(blo4) == 2)

print("\nUEBERLAPPUNGS-TESTS BESTANDEN.")
