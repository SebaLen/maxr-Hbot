#!/usr/bin/env python3
"""
heat_map_calc.py - Heatmap computation for the MAXR bot.

VISIBILITY (important, verified in the MAXR code):
  The heatmap is based on the state FILTERED by the bridge. The bridge filters
  every enemy unit through cPlayer::canSeeUnit() (botbridge.cpp
  filterUnitArrayByVisibility). canSeeUnit() checks the player's scanMap - the
  AGGREGATED sight of ALL own units (cPlayer::refreshScanMaps adds the scan of
  every vehicle AND every building). Consequence: if an enemy appears in the
  state at all, it is visible to the PLAYER - detected by some own unit (scout,
  radar, ...), NOT necessarily by the unit that is about to react. A builder unit
  with scan 0/1 therefore does not need to check anything itself: every enemy in
  the heatmap is already an enemy seen by the player. NO per-unit isVisible check
  is needed.

Produces three overlay layers per turn from the filtered state (fog-of-war-safe):

  LAYER 1 - danger   (float >= 0)
      How dangerous is a field? Every VISIBLE enemy combat unit radiates danger
      into its weapon range (range) - weighted by damage value (damage). The
      closer to the enemy, the higher the value.
      Geometry: CIRCULAR (l2NormSquared <= 4*r^2, identical to
      cRangeMap::isInRange in rangemap.cpp with square=false).

  LAYER 2 - observed (float >= 0)
      Which fields are OBSERVED by visible enemy units (scan radius)?
      A field in this layer means: 'we are seen if we stand there'.
      Scan value comes from unit["data"]["scan"] (cDynamicUnitData, includes
      upgrade bonuses). Same circular geometry.

  LAYER 3 - own_strength (float >= 0)
      Own combat power in the area. Own combat units radiate their strength
      (range * damage) into their weapon range - as a counterpart to 'danger'.

From these three layers a derived layer is computed:

  DERIVED - threat (float, positive = threat, negative = own strength dominates)
      threat[x][y] = danger[x][y] - own_strength[x][y]
      Positive: enemy units dominate -> dangerous.
      Negative: own units dominate -> safe / own control.
      Zero: untouched (no combat value in range).

WHY THIS STRUCTURE? (game-theoretic foundation)
----------------------------------------------------
Classic RTS AI uses "influence maps" (Dave Mark, "Game AI Pro" 2013):
units radiate their combat value radially into the terrain - the bot decides
by the resulting field strength, not by individual unit conflicts.
This scales: 1 or 50 enemies, the logic stays the same.

Concrete uses for the MAXR bot:
  1. SCOUT ROUTING: scouts should travel in 'observed'-poor areas (low detection
     risk) instead of straight towards enemy positions.
  2. EXPANSION ROUTING: constructors/pioneers avoid fields with high 'threat'
     (own combat strength is not enough, the enemy would destroy the build).
  3. RETREAT: own units with little HP on fields with danger > 0 should flee
     towards the negative threat gradient.
  4. ATTACK PLANNING (later): attack where threat is negative and large
     (own superiority), avoid fields with high positive threat.

MAXR CODE VERIFICATION:
  - scan: cDynamicUnitData::scan (unitdata.h line 433, NVP line 402)
          In the state: unit["data"]["scan"]
          Upgrade-aware: yes (cDynamicUnitData contains current values)
  - range: cDynamicUnitData::range (unitdata.h line 401)
           In the state: unit["data"]["range"]
  - damage: cDynamicUnitData::damage (unitdata.h, NVP as well)
            In the state: unit["data"]["damage"]
  - canAttack: cStaticUnitData::canAttack (unitdata.h line 101)
               In the state: staticUnitData[sid]["canAttack"] (> 0 = can attack)
  - circle geometry: cRangeMap::isInRange (rangemap.cpp):
        delta2x = (pos - center) * 2 - unitSize + 1
        circular: delta2x.l2NormSquared() <= 4 * range^2
        simplified for unitSize=1 (all vehicles 1x1):
        (2*(dx) + 0)^2 + (2*(dy) + 0)^2 <= 4*r^2
        -> dx^2 + dy^2 <= r^2  (standard circle check)
  - building unitSize: 1x1 (isBig=false) or 2x2 (isBig=true)
        For 2x2 buildings: unitSize=2, the delta2x formula shifts.
        Simplified here: centre = position + (0.5, 0.5) for 2x2.
        Conservative: range check from the nearest corner field (worst case).
"""

import math
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

class HeatMaps(NamedTuple):
    """All layers plus metadata as an immutable result."""
    width: int
    height: int
    # main layers (2D list [y][x], float)
    danger: list       # enemy combat range, weighted by damage
    observed: list     # enemy sight radius (we are being seen)
    own_strength: list # own combat range, weighted by damage
    # derived layer
    threat: list       # danger - own_strength (positive = threat)
    # binary range layers (0/1) for the behaviour modes (section 9):
    avoid: list        # 1 = field in scan OR attack of an enemy (max(scan,attack))
    enemy_attack: list # 1 = field in enemy attack range (union of Air+Ground)
    enemy_scan: list   # 1 = field in enemy scan range
    # category-separated attack layers (per MAXR cPlayer::addToSentryMap):
    enemy_attack_air: list    # 1 = threatens AIR targets (attackers with canAttack&Air)
    enemy_attack_ground: list # 1 = threatens GROUND targets (canAttack&Ground; selectTarget)
    # defense screen: COUNT maps of own defense buildings (int >= 0).
    # Per field: by HOW MANY own defense buildings it is covered.
    # 0 = gap, 1 = covered, >=2 = overlapping (robustness). Separated by
    # threat, because air and land/sea defense cover different targets:
    own_defense_ground: list # +1 per own building with canAttack&(Ground|Sea) in weapon range
    own_defense_air: list    # +1 per own building with canAttack&Air (gun_aa) in weapon range
    own_radar_scan: list     # +1 per own radar (scan>0, range==0) in scan range


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _in_circle(cx: float, cy: float, r: int, x: int, y: int) -> bool:
    """Checks whether (x,y) is within the CIRCULAR radius r around (cx,cy).

    Corresponds to cRangeMap::isInRange (square=false) for unitSize=1:
        delta2x = (field - center) * 2  (unitSize=1 -> -unitSize+1 = 0)
        delta2x.l2NormSquared() <= 4 * r * r
    Equivalent: (x - cx)^2 + (y - cy)^2 <= r^2
    """
    dx = x - cx
    dy = y - cy
    return dx * dx + dy * dy <= r * r


def _unit_center(pos: tuple, is_big: bool) -> tuple:
    """Centre of a unit. 1x1 units: pos itself.
    2x2 buildings: centre at (pos.x + 0.5, pos.y + 0.5).
    For the circle check we use the centre - this matches the MAXR behaviour
    (unitSize=2 -> delta2x offset centred on 2x2)."""
    if is_big:
        return (pos[0] + 0.5, pos[1] + 0.5)
    return (float(pos[0]), float(pos[1]))


def _add_circle(layer: list, cx: float, cy: float, r: int,
                weight: float, w: int, h: int) -> None:
    """Adds 'weight' to all fields within circle radius r around (cx,cy).
    Weighting: linearly decreasing with distance (1 - dist/r) at the edge -> 0,
    full value 'weight' at the centre. This makes the core of the range more
    dangerous than the edge (realistic: the enemy shoots from the middle)."""
    x0 = max(0, int(cx - r))
    x1 = min(w - 1, int(cx + r) + 1)
    y0 = max(0, int(cy - r))
    y1 = min(h - 1, int(cy + r) + 1)
    r2 = r * r
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            dx = x - cx
            dy = y - cy
            d2 = dx * dx + dy * dy
            if d2 <= r2:
                # linear distance weighting: near the centre = full value
                dist = math.sqrt(d2)
                falloff = 1.0 - (dist / (r + 1e-6))
                layer[y][x] += weight * max(0.0, falloff)


def _mark_circle(layer: list, cx: float, cy: float, r: int,
                 w: int, h: int) -> None:
    """Sets all fields within circle radius r around (cx,cy) to 1 (binary mark).
    Unlike _add_circle (weighted/decreasing) this is a hard 0/1 mask - for the
    avoidance ranges of the behaviour modes (a field is avoided or not, without
    gradation)."""
    x0 = max(0, int(cx - r))
    x1 = min(w - 1, int(cx + r) + 1)
    y0 = max(0, int(cy - r))
    y1 = min(h - 1, int(cy + r) + 1)
    r2 = r * r
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            dx = x - cx
            dy = y - cy
            if dx * dx + dy * dy <= r2:
                layer[y][x] = 1


def _count_circle(layer: list, cx: float, cy: float, r: int,
                  w: int, h: int) -> None:
    """Adds +1 to all fields within circle radius r around (cx,cy) (COUNT).
    Like _mark_circle, but instead of setting to 1 it counts up - so the map
    measures by HOW MANY sources a field is covered (0=gap, 1=covered,
    >=2=overlapping). Basis of the defense screen.
    Same engine-exact circle geometry as _mark_circle/_add_circle."""
    x0 = max(0, int(cx - r))
    x1 = min(w - 1, int(cx + r) + 1)
    y0 = max(0, int(cy - r))
    y1 = min(h - 1, int(cy + r) + 1)
    r2 = r * r
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            dx = x - cx
            dy = y - cy
            if dx * dx + dy * dy <= r2:
                layer[y][x] += 1


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def compute_heatmaps(gs, enemy_ranges=None) -> HeatMaps:
    """Computes all heatmap layers for the current game state.

    Parameters:
        gs: GameState object from maxr_bot_lib (must have terrain, model, me).
        enemy_ranges: optional result of the bridge query 'enemyRangeMaps'
            {"width","height","scan":[[x,y],...],"attackAir":[[x,y],...],
             "attackGround":[[x,y],...]}. attackAir/attackGround are separated by
            target category (MAXR cPlayer::addToSentryMap: canAttack&Air -> Air,
            &Ground -> Ground; via selectTarget). If present,
            enemy_scan/enemy_attack/enemy_attack_air/enemy_attack_ground/avoid are
            filled EXACTLY from it (real cRangeMap, correct unitSize/2x2 geometry).
            If missing (old bridge / test without bridge), the function falls back
            to the internal circle emulation (_process_unit), which reproduces the
            same Air/Ground split.

    Returns HeatMaps. If the terrain map is missing, a minimum size is estimated
    from the visible unit positions.

    IMPORTANT (visibility): enemies are only in the state if the PLAYER sees them
    (the bridge filters via canSeeUnit = aggregated sight of ALL own units). A
    builder unit with scan 0 does not need to check anything itself. With
    enemy_ranges even the range computation uses the exact game logic.

    Data access of the internal emulation (verified against MAXR code):
        unit["data"]["scan"]   -> sight radius (cDynamicUnitData, upgrade-aware)
        unit["data"]["range"]  -> weapon range (cDynamicUnitData)
        unit["data"]["damage"] -> damage value (cDynamicUnitData)
        canAttack              -> from _static_by_sid[(firstPart, secondPart)]
        isBig                  -> from _static_by_sid, True for 2x2 buildings
    """
    # --- determine map size -------------------------------------------------
    terrain = gs.terrain
    if enemy_ranges and enemy_ranges.get("width") and enemy_ranges.get("height"):
        # the bridge provides the authoritative map size.
        w = enemy_ranges["width"]
        h = enemy_ranges["height"]
    elif terrain:
        w = terrain.get("width", 64)
        h = terrain.get("height", 64)
    else:
        # fallback: estimate from visible unit positions
        all_pos = (
            [gs.pos(v) for v in gs.my_vehicles()]
            + [gs.pos(b) for b in gs.my_buildings()]
        )
        if all_pos:
            w = max(p[0] for p in all_pos) + 20
            h = max(p[1] for p in all_pos) + 20
        else:
            w, h = 64, 64

    # --- initialise layers --------------------------------------------------
    def _zero():
        return [[0.0] * w for _ in range(h)]

    danger       = _zero()
    observed     = _zero()
    own_strength = _zero()
    avoid        = [[0] * w for _ in range(h)]
    enemy_attack = [[0] * w for _ in range(h)]
    enemy_scan   = [[0] * w for _ in range(h)]
    enemy_attack_air    = [[0] * w for _ in range(h)]
    enemy_attack_ground = [[0] * w for _ in range(h)]

    # defense screen count maps (int, own defense buildings only)
    own_defense_ground = [[0] * w for _ in range(h)]
    own_defense_air    = [[0] * w for _ in range(h)]
    own_radar_scan     = [[0] * w for _ in range(h)]

    # --- determine own ID ---------------------------------------------------
    me_id = gs.me.get("id") if gs.me else None

    # --- iterate over all players -------------------------------------------
    for player in gs.model.get("players", []):
        is_self = (player.get("id") == me_id)

        for unit_list, are_vehicles in (
            (player.get("vehicles", []), True),
            (player.get("buildings", []), False),
        ):
            for unit in unit_list:
                _process_unit(unit, are_vehicles, is_self, gs,
                              danger, observed, own_strength,
                              avoid, enemy_attack, enemy_scan,
                              enemy_attack_air, enemy_attack_ground,
                      own_defense_ground, own_defense_air, own_radar_scan, w, h)

    # neutral units (visible neutral buildings/vehicles)
    for unit in gs.model.get("neutralVehicles", []):
        _process_unit(unit, True, False, gs,
                      danger, observed, own_strength,
                      avoid, enemy_attack, enemy_scan,
                      enemy_attack_air, enemy_attack_ground,
                      own_defense_ground, own_defense_air, own_radar_scan, w, h)
    for unit in gs.model.get("neutralBuildings", []):
        _process_unit(unit, False, False, gs,
                      danger, observed, own_strength,
                      avoid, enemy_attack, enemy_scan,
                      enemy_attack_air, enemy_attack_ground,
                      own_defense_ground, own_defense_air, own_radar_scan, w, h)

    # --- threat = danger - own_strength ------------------------------------
    threat = [
        [danger[y][x] - own_strength[y][x] for x in range(w)]
        for y in range(h)
    ]

    # --- binary range layers: EXACTLY from the bridge (path A) --------------
    # if the bridge provided enemy_ranges, the exact cRangeMap fields replace the
    # internal circle emulation of _process_unit. avoid = union of scan and attack
    # (max(scan,attack) per enemy unit yields exactly the union of the two range
    # sets over all enemies).
    if enemy_ranges is not None:
        enemy_scan          = [[0] * w for _ in range(h)]
        enemy_attack        = [[0] * w for _ in range(h)]
        enemy_attack_air    = [[0] * w for _ in range(h)]
        enemy_attack_ground = [[0] * w for _ in range(h)]
        avoid               = [[0] * w for _ in range(h)]
        for cell in enemy_ranges.get("scan", []):
            x, y = cell[0], cell[1]
            if 0 <= x < w and 0 <= y < h:
                enemy_scan[y][x] = 1
                avoid[y][x] = 1
        for cell in enemy_ranges.get("attackAir", []):
            x, y = cell[0], cell[1]
            if 0 <= x < w and 0 <= y < h:
                enemy_attack_air[y][x] = 1
                enemy_attack[y][x] = 1
                avoid[y][x] = 1
        for cell in enemy_ranges.get("attackGround", []):
            x, y = cell[0], cell[1]
            if 0 <= x < w and 0 <= y < h:
                enemy_attack_ground[y][x] = 1
                enemy_attack[y][x] = 1
                avoid[y][x] = 1

    return HeatMaps(
        width=w, height=h,
        danger=danger, observed=observed,
        own_strength=own_strength, threat=threat,
        avoid=avoid, enemy_attack=enemy_attack, enemy_scan=enemy_scan,
        enemy_attack_air=enemy_attack_air,
        enemy_attack_ground=enemy_attack_ground,
        own_defense_ground=own_defense_ground,
        own_defense_air=own_defense_air,
        own_radar_scan=own_radar_scan,
    )


def _process_unit(unit, is_vehicle: bool, is_self: bool, gs,
                  danger, observed, own_strength,
                  avoid, enemy_attack, enemy_scan,
                  enemy_attack_air, enemy_attack_ground,
                  own_defense_ground, own_defense_air, own_radar_scan,
                  w: int, h: int) -> None:
    """Processes ONE unit and adds its values into the layers."""
    pos      = gs.pos(unit)
    dyn      = unit.get("data", {})
    scan_r   = (dyn.get("scan") or 0)
    weapon_r = (dyn.get("range") or 0)
    damage   = (dyn.get("damage") or 0)

    # canAttack and isBig from staticUnitData (immutable unit data)
    fp  = gs.unit_first(unit)
    sp  = gs.unit_type(unit)
    st  = gs._static_by_sid.get((fp, sp), {})
    can_attack_bits = (st.get("canAttack") or 0)   # eTerrainFlag bitfield
    can_attack = can_attack_bits > 0
    is_big     = bool(st.get("isBig", False))

    cx, cy = _unit_center(pos, is_big)

    # a unit can attack if it has canAttack AND range/damage.
    is_combat = can_attack and weapon_r > 0 and damage > 0

    # --- defense screen: COUNT maps of own defense BUILDINGS -----------------
    # own buildings only (the screen consists of fixed installations, not
    # vehicles). Radar (scan>0, no weapon) -> scan count map. Weapon buildings
    # -> separated by canAttack bit into air resp. land/sea count map.
    # IMPORTANT (own view, not enemy view): for OWN land/sea coverage, Ground OR
    # Sea applies (canAttack & 4 OR & 2) - that is "what my building can hit".
    # This is deliberately different from the enemy layers (Ground bit only,
    # selectTarget).
    if is_self and not is_vehicle:
        # radar / pure scan source (has scan, no weapon) -> count scan underlay.
        if scan_r > 0 and weapon_r == 0:
            _count_circle(own_radar_scan, cx, cy, scan_r, w, h)
        # weapon building -> by target category into the matching coverage count map.
        if is_combat:
            if can_attack_bits & 1:               # Air -> anti-air (gun_aa)
                _count_circle(own_defense_air, cx, cy, weapon_r, w, h)
            if can_attack_bits & (4 | 2):          # Ground OR Sea -> land/sea defense
                _count_circle(own_defense_ground, cx, cy, weapon_r, w, h)

    # --- enemy binary range layers (enemies only) ---------------------------
    if not is_self:
        # enemy_scan: pure sight radius
        if scan_r > 0:
            _mark_circle(enemy_scan, cx, cy, scan_r, w, h)
        # enemy_attack + category-separated layers: only if really attackable.
        # threat semantics from cAttackJob::selectTarget (verified):
        #   Air bit (1)    -> hits aircraft
        #   Ground bit (4) -> hits ground/buildings/surface ships (the
        #                     vehicle/building target depends ONLY on the Ground bit).
        # The Sea bit (2) has no own branch in selectTarget -> do NOT count it as
        # a ground threat (sub/corvet=Sea+Sub do not threaten a ground target).
        # multiple bits -> into ALL affected maps (independent ifs).
        if is_combat:
            _mark_circle(enemy_attack, cx, cy, weapon_r, w, h)
            if can_attack_bits & 1:            # Air
                _mark_circle(enemy_attack_air, cx, cy, weapon_r, w, h)
            if can_attack_bits & 4:            # Ground
                _mark_circle(enemy_attack_ground, cx, cy, weapon_r, w, h)
        # avoid: max(scan, attack) - the larger of the two ranges.
        # A scout (scan 9 / attack 0) -> 9; a gun (scan 4 / attack 6) -> 6.
        avoid_r = max(scan_r, weapon_r if is_combat else 0)
        if avoid_r > 0:
            _mark_circle(avoid, cx, cy, avoid_r, w, h)

    # --- LAYER observed: enemy scan (weighted, enemies only) ----------------
    if not is_self and scan_r > 0:
        _add_circle(observed, cx, cy, scan_r, 1.0, w, h)

    # --- LAYER danger / own_strength: combat units --------------------------
    if not is_combat:
        return  # not a combat unit -> no contribution to combat layers

    # weight: range * damage (large range + high damage = very dangerous)
    weight = float(weapon_r * damage)

    if is_self:
        _add_circle(own_strength, cx, cy, weapon_r, weight, w, h)
    else:
        _add_circle(danger, cx, cy, weapon_r, weight, w, h)


# ---------------------------------------------------------------------------
# Query helpers (for the bot)
# ---------------------------------------------------------------------------

def danger_at(hm: HeatMaps, x: int, y: int) -> float:
    """Enemy combat presence on field (x, y). > 0 = enemy in range."""
    if 0 <= x < hm.width and 0 <= y < hm.height:
        return hm.danger[y][x]
    return 0.0


def is_observed(hm: HeatMaps, x: int, y: int, threshold: float = 0.1) -> bool:
    """True if (x, y) lies within the sight radius of a visible enemy unit."""
    if 0 <= x < hm.width and 0 <= y < hm.height:
        return hm.observed[y][x] >= threshold
    return False


def threat_at(hm: HeatMaps, x: int, y: int) -> float:
    """Threat balance: positive = enemy dominates, negative = we dominate."""
    if 0 <= x < hm.width and 0 <= y < hm.height:
        return hm.threat[y][x]
    return 0.0


def is_avoided(hm: HeatMaps, x: int, y: int) -> bool:
    """True if (x,y) lies within scan OR attack of an enemy (max(scan,attack)).
    Fields outside the map count as NOT avoided (there is no known danger there)
    - reachability/validity is checked separately."""
    if 0 <= x < hm.width and 0 <= y < hm.height:
        return hm.avoid[y][x] == 1
    return False


def in_enemy_attack(hm: HeatMaps, x: int, y: int) -> bool:
    """True if (x,y) lies within enemy attack range (risk of being shot)."""
    if 0 <= x < hm.width and 0 <= y < hm.height:
        return hm.enemy_attack[y][x] == 1
    return False


def in_enemy_scan(hm: HeatMaps, x: int, y: int) -> bool:
    """True if (x,y) lies within enemy scan range (visibility)."""
    if 0 <= x < hm.width and 0 <= y < hm.height:
        return hm.enemy_scan[y][x] == 1
    return False


def safest_neighbor(hm: HeatMaps, x: int, y: int,
                    terrain=None) -> tuple | None:
    """Returns the neighbouring field (8-neighbourhood) with the lowest threat value.
    Fields outside the map and blocked terrain fields are avoided.
    Returns None if all neighbours are outside or blocked.

    Usage: retreat decision for threatened units.
    """
    best_pos   = None
    best_threat = float("inf")
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            nx, ny = x + dx, y + dy
            if not (0 <= nx < hm.width and 0 <= ny < hm.height):
                continue
            # terrain check: avoid blocked fields
            if terrain is not None:
                idx = ny * hm.width + nx
                data = terrain.get("data", "")
                if 0 <= idx < len(data) and data[idx] == "#":
                    continue
            t = hm.threat[ny][nx]
            if t < best_threat:
                best_threat = t
                best_pos = (nx, ny)
    return best_pos


def safest_path_target(hm: HeatMaps, from_pos: tuple,
                        candidates: list, terrain=None) -> tuple | None:
    """Selects, from a list of target positions, the target whose ROUTE
    (straight line, as an approximation) has the lowest maximum threat value.

    Simple heuristic for scout routing and expansion planning:
    among all candidates the bot takes the one whose path (roughly: midpoint
    between start and target) is safest.

    Returns None if 'candidates' is empty.
    """
    if not candidates:
        return None

    def _route_danger(target):
        # check some support points on the line start->target
        fx, fy = from_pos
        tx, ty = target
        max_t = 0.0
        steps = max(abs(tx - fx), abs(ty - fy), 1)
        for i in range(steps + 1):
            t = i / steps
            px = int(round(fx + t * (tx - fx)))
            py = int(round(fy + t * (ty - fy)))
            max_t = max(max_t, threat_at(hm, px, py))
        return max_t

    return min(candidates, key=_route_danger)


def high_threat_zones(hm: HeatMaps, threshold: float = 50.0) -> list:
    """Returns all fields where threat >= threshold (dangerous zones).
    Useful for debugging/visualisation and for expansion planning (expansion
    avoids fields in this list)."""
    zones = []
    for y in range(hm.height):
        for x in range(hm.width):
            if hm.threat[y][x] >= threshold:
                zones.append((x, y, hm.threat[y][x]))
    return zones


# ---------------------------------------------------------------------------
# Mode target-field selection (section 9.3): reachability-based
# ---------------------------------------------------------------------------

def _field_avoided_for_mode(hm: HeatMaps, x: int, y: int, avoid_mode: str) -> bool:
    """Is (x,y) to be avoided for the given avoidance mode?
        avoid_mode = "max"            -> max(scan,attack) (avoid layer)
        avoid_mode = "attack"         -> total attack range (Air+Ground)
        avoid_mode = "attack_air"     -> attack against AIR targets only
        avoid_mode = "attack_ground"  -> attack against GROUND targets only
    The category-separated modes use the layers from the bridge (MAXR sentry
    split). This way a ground unit does NOT avoid pure flak range.
    """
    if avoid_mode == "attack_air":
        layer = getattr(hm, "enemy_attack_air", None) or hm.enemy_attack
        if 0 <= y < hm.height and 0 <= x < hm.width:
            return layer[y][x] == 1
        return False
    if avoid_mode == "attack_ground":
        layer = getattr(hm, "enemy_attack_ground", None) or hm.enemy_attack
        if 0 <= y < hm.height and 0 <= x < hm.width:
            return layer[y][x] == 1
        return False
    if avoid_mode == "attack":
        return in_enemy_attack(hm, x, y)
    return is_avoided(hm, x, y)


def select_safe_target(hm: HeatMaps, candidates: list,
                       avoid_mode: str = "max",
                       require_enemy_in_own_scan: bool = False,
                       own_scan_centers: list = None,
                       safe_only: bool = False) -> tuple | None:
    """Selects, from reachable candidate fields, the best retreat/evasion target
    per section 9.3.

    Parameters:
      candidates  list of reachable fields [(x,y), ...]. Reachability (in ONE
                  turn, via pathCost) is determined by the CALLER (bot_run); this
                  helper does not know the bridge.
      avoid_mode  "max" (max(scan,attack), default) or "attack" (fire only).
      require_enemy_in_own_scan  For active stalking: only fields from which at
                  least one enemy position remains in OWN scan range.
      own_scan_centers  list [((ex,ey), own_scan_radius), ...] - enemy positions
                  to be held, with the own scan range.
      safe_only   If True, NO idea-B fallback (least-dangerous) is done: if there
                  is no safe field, None is returned. Used for active stalking
                  (no safe ring field -> fallback in the caller).

    Selection (9.3):
      1. Prefers SAFE fields (not avoided for the mode).
      2. Among the safe ones: lowest threat (idea A - towards own strength).
      3. If there is NO safe field (idea B): least-dangerous reachable field
         (minimal avoid/danger), also by threat secondarily.
         (Omitted with safe_only=True.)
      For stalking (require_enemy_in_own_scan) fields without an enemy in own scan
      range are discarded beforehand; if nothing remains, the function returns None
      (the caller then falls back to passive reconnaissance).

    Return: (x,y) or None.
    """
    if not candidates:
        return None

    # stalking filter: only fields from which an enemy stays in own scan range.
    pool = candidates
    if require_enemy_in_own_scan:
        if not own_scan_centers:
            return None
        filtered = []
        for (x, y) in candidates:
            for (ex, ey), own_r in own_scan_centers:
                dx, dy = ex - x, ey - y
                if dx * dx + dy * dy <= own_r * own_r:
                    filtered.append((x, y))
                    break
        if not filtered:
            return None
        pool = filtered

    safe = [(x, y) for (x, y) in pool
            if not _field_avoided_for_mode(hm, x, y, avoid_mode)]

    if safe:
        # idea A: lowest threat (towards own strength).
        return min(safe, key=lambda p: threat_at(hm, p[0], p[1]))

    if safe_only:
        return None

    # idea B: no safe field -> least-dangerous reachable field.
    # "danger" here = danger value (fire counts more than pure sight),
    # secondarily threat.
    return min(pool, key=lambda p: (danger_at(hm, p[0], p[1]),
                                    threat_at(hm, p[0], p[1])))


# ---------------------------------------------------------------------------
# Example integration (not executed on import)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # minimal smoke test without a real state
    print("heat_map_calc.py geladen. Kein Standalone-Betrieb - als Modul verwenden.")
    print("Verwendungsbeispiel im Bot:")
    print("  from heat_map_calc import compute_heatmaps, threat_at, safest_neighbor")
    print("  hm = compute_heatmaps(gs)")
    print("  for sv in scouts:")
    print("      if threat_at(hm, *gs.pos(sv)) > 0:")
    print("          retreat = safest_neighbor(hm, *gs.pos(sv), terrain=gs.terrain)")
    print("          if retreat: conn.do({'type':'move','unitId':sv['id'],'target':list(retreat)})")
