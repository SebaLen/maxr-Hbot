#!/usr/bin/env python3
"""
strategy.py - Strategic situation assessment for the MAXR bot.

Computes, once per turn, all strategically relevant quantities from the
filtered state (fog-of-war-safe) and bundles them into a StrategyReport object.
This object is the single entry point for the phase orchestrator (bot_run.py) -
it reads from it which modes should be active and which concrete actions are
prioritised.

STRUCTURE:
  1. HeatMap layers (from heat_map_calc.py):
       danger       - enemy combat range weighted by damage
       observed     - enemy scan radius (we are being seen)
       own_strength - own combat range weighted by damage

  2. Individual strategic values (directly from GameState):
       metal_fill_ratio    - metal storage fill level (0..1)
       energy_load_ratio   - energy supply load (0..1+)
       map_explored_pct    - fraction of explored map fields (%)
       net_connected       - supply network connected (bool)
       next_expansion_goal - best next expansion goal or None

  3. Tactical findings (HeatMap + GameState combined):
       units_under_threat  - own units on fields with danger > 0
       safe_expansion_candidates - explored deposits WITHOUT enemy threat
       scout_safe_targets  - free map fields with minimal observed value
       danger_zones        - fields with very high threat value (avoid)

Usage in bot_run.py:
    from strategy import compute_strategy
    report = compute_strategy(gs, _ENERGY_LOAD_RATIO, _METAL_FILL_HISTORY,
                              _EXPANSION_REJECTED)
    if report.units_under_threat:
        # retreat logic...
    if report.safe_expansion_candidates:
        # expand to the safest deposit...
"""

import math
from typing import NamedTuple, Optional

from heat_map_calc import (
    HeatMaps,
    compute_heatmaps,
    danger_at,
    is_observed,
    threat_at,
    safest_neighbor,
    safest_path_target,
    high_threat_zones,
)


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

class ThreatUnit(NamedTuple):
    """An own unit located on or near a dangerous field."""
    unit_id:    int
    pos:        tuple   # (x, y)
    danger:     float   # danger value at the current field
    threat:     float   # threat value (danger - own_strength) at the current field
    retreat_to: Optional[tuple]  # safest neighbouring field or None


class ExpansionCandidate(NamedTuple):
    """An explored resource deposit suitable as an expansion goal."""
    pos:          tuple   # (x, y) - strongest single field of the deposit
    res_type:     str     # "metal" | "oil" | "gold"
    amount:       int     # richness of the field
    score:        float   # score from expansion_target (higher = better)
    danger:       float   # danger value at the deposit field
    threat:       float   # threat value at the deposit field
    route_threat: float   # max threat on the straight-line route from the base


class ScoutTarget(NamedTuple):
    """A target field for scouts - little observed, far from the base."""
    pos:          tuple   # (x, y)
    observed_val: float   # observed value (low = safer for scouts)
    danger:       float   # danger value (combat risk)


class StrategyReport(NamedTuple):
    """Complete strategic situation report for one turn."""

    # --- Raw HeatMap layers -------------------------------------------------
    heatmaps: HeatMaps

    # --- Economic situation -------------------------------------------------
    metal_fill_ratio:   float   # 0..1, metal storage fill incl. under construction
    energy_load_ratio:  float   # 0..1+, energy load incl. under construction
    map_explored_pct:   float   # 0..100, fraction of explored fields
    net_connected:      bool    # True = supply network is connected
    next_expansion_goal: Optional[tuple]  # (x,y,type,amount,score) or None

    # --- Tactical findings --------------------------------------------------
    units_under_threat:         list   # [ThreatUnit] own combat units in danger
    safe_expansion_candidates:  list   # [ExpansionCandidate] sorted: safest first
    scout_safe_targets:         list   # [ScoutTarget] sorted: least observed first
    danger_zones:               list   # [(x,y,threat)] very dangerous fields

    # --- Flags (true/false decision helpers) ---------------------------------
    needs_metal_storage:    bool    # >= 3 turns metal > 80% full
    needs_energy_station:   bool    # energy load >= 90%
    is_emergency:           bool    # emergency mode active
    emergency_reasons:      list    # [str] reasons

    # --- Builder minimum stock (mine-coupled, REPLACES the old metal formulas) ---
    mine_count:             int     # number of own mines
    required_constructors:  int     # target constructors = ceil(0.5 * mine_count)
    required_pioneers:      int     # target pioneers      = 2 * mine_count
    constructor_deficit:    int     # missing constructors (>0 = deficit), incl. production
    pioneer_deficit:        int     # missing pioneers (>0 = deficit), incl. production
    builder_priority:       list    # ordered build recommendation, e.g. ["constructor","pioneer"]
                                    # constructor ALWAYS before pioneer. Empty = no builder deficit.


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

METAL_FILL_TRIGGER      = 0.80   # metal storage >= 80% full -> new storage
METAL_FILL_ROUNDS       = 3      # over how many turns >= 80% must be met
ENERGY_LOAD_TRIGGER     = 0.90   # energy load >= 90% -> energy station
DANGER_ZONE_THRESHOLD   = 50.0   # threat value from which a field counts as a 'danger zone'
THREAT_UNIT_MIN_DANGER  = 1.0    # from this danger value a unit counts as threatened
SCOUT_CANDIDATE_COUNT   = 8      # how many scout targets are proposed at most
EXPANSION_CANDIDATE_MAX = 10     # how many expansion candidates at most

# Builder minimum stock, coupled to the mine count (REPLACES the old
# metal-income formulas target_pioneers/target_constructors).
PIONEERS_PER_MINE       = 1.0    # target pioneers      = 1   * mine count
CONSTRUCTORS_PER_MINE   = 0.5    # target constructors = 0.5 * mine count (rounded up)


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def builder_requirements(gs):
    """Lean builder-demand calculation WITHOUT HeatMap (for the hot production
    path in bot_run). Returns the same values as the corresponding block in
    compute_strategy, but without the expensive HeatMap computation.

    Rule (REPLACES target_pioneers/target_constructors):
      required_pioneers     = 2   * mine count
      required_constructors = ceil(0.5 * mine count)   (1 mine -> 1, not 0)
    Deficit = target - actual(incl. production), >0 = some are missing.
    builder_priority: missing builders in BUILD ORDER, constructor ALWAYS first.

    Returns dict: {mine_count, required_constructors, required_pioneers,
                constructor_deficit, pioneer_deficit, builder_priority}.

    NOTE: This is now a thin wrapper around take_inventory() - the inventory
    is centralised (phase 1). Existing callers remain unchanged and runnable.
    """
    inv = take_inventory(gs)
    return {
        "mine_count": inv["mine_count"],
        "required_constructors": inv["constructor"]["need"],
        "required_pioneers": inv["pioneer"]["need"],
        "constructor_deficit": inv["constructor"]["deficit"],
        "pioneer_deficit": inv["pioneer"]["deficit"],
        "builder_priority": inv["builder_priority"],
    }


def take_inventory(gs, required_surveyors=5, mines_per_station=4,
                   cover_target=None, radar_cover_target=1):
    """CENTRAL INVENTORY (phase 1): once per turn 'what do we have? what do we
    need?' for ALL builder/unit types. Actual counts each include production
    (in factory buildList / under construction), so that nothing is endlessly
    rebuilt. The target sources are UNCHANGED compared to the earlier, scattered
    logic - only bundled in ONE place:
      pioneer     : target = 1   * mines           (PIONEERS_PER_MINE)
      constructor : target = ceil(0.5 * mines)     (CONSTRUCTORS_PER_MINE, rounded up)
      surveyor    : target = required_surveyors    (fixed value, passed from bot_run)
      bulldozer   : target from rubble situation   (>10 rubble -> 2, >0 -> 1, else 0)
      station     : target = ceil(mines / N)       (1 energy station per N mines)
    Each entry: {"have": actual incl. production, "need": target, "deficit": max(0,need-have)}.
    builder_priority: missing builders in build order (constructor first, then pioneer).

    DEFENSE SCREEN: cover_target (target coverage degree per screen field, from the
    personality) and radar_cover_target are - if passed - returned in the 'defense'
    entry. The actual gap evaluation (which screen fields are < target degree
    covered) needs the coverage count maps and happens in the placement stage;
    here only the TARGET DEGREE is anchored so that all stages use the same value.
    None -> no screen demand in this inventory.
    """
    mine_count = len(gs.my_mines())

    def entry(have, need):
        return {"have": have, "need": need, "deficit": max(0, need - have)}

    # --- pioneer / constructor (mine-based) ---
    pioneer = entry(gs.count_pioneers_incl_production(),
                    int(PIONEERS_PER_MINE * mine_count))
    constructor = entry(gs.count_constructors_incl_production(),
                        math.ceil(CONSTRUCTORS_PER_MINE * mine_count))

    # --- surveyor (fixed target) ---
    surveyor = entry(gs.count_surveyors_incl_production(), required_surveyors)

    # --- bulldozer (from rubble situation) ---
    rubble = gs.count_rubble()
    dozer_need = 2 if rubble > 10 else (1 if rubble > 0 else 0)
    bulldozer = entry(gs.count_bulldozers_incl_production(), dozer_need)

    # --- energy station (1 per mines_per_station mines) ---
    station_need = (max(1, math.ceil(mine_count / mines_per_station))
                    if mine_count > 0 else 0)
    station = entry(gs.station_count_incl_construction(), station_need)

    builder_priority = []
    if constructor["deficit"] > 0:
        builder_priority.append("constructor")
    if pioneer["deficit"] > 0:
        builder_priority.append("pioneer")

    # --- defense screen: anchor target degree (personality) ---
    # Only the TARGET DEGREE; the gap evaluation happens in the placement stage
    # with the coverage count maps. cover_target None -> no screen demand.
    defense = {
        "cover_target": cover_target,
        "radar_cover_target": radar_cover_target,
        "active": cover_target is not None,
    }

    return {
        "mine_count": mine_count,
        "rubble_count": rubble,
        "pioneer": pioneer,
        "constructor": constructor,
        "surveyor": surveyor,
        "bulldozer": bulldozer,
        "station": station,
        "defense": defense,
        "builder_priority": builder_priority,
    }


def compute_strategy(
    gs,
    energy_load_ratio:   float,
    metal_fill_history:  list,
    expansion_rejected:  set,
) -> StrategyReport:
    """Computes the complete strategic situation report.

    Parameters:
        gs                  GameState from maxr_bot_lib
        energy_load_ratio   _ENERGY_LOAD_RATIO from bot_run (already computed)
        metal_fill_history  _METAL_FILL_HISTORY from bot_run (list <= 3 floats)
        expansion_rejected  _EXPANSION_REJECTED from bot_run (set of rejected goals)

    Returns StrategyReport.
    """
    # 1. compute HeatMaps (most expensive operation - once per turn)
    hm = compute_heatmaps(gs)
    terrain = gs.terrain

    # 2. economic situation -------------------------------------------------
    metal_fill   = gs.metal_fill_ratio()
    map_explored = gs.map_explored_fraction() * 100.0
    net_gap      = gs.network_gap_target()
    net_connected = net_gap is None

    next_goal = gs.expansion_target(blocked_fields=expansion_rejected)

    # 3. flags ----------------------------------------------------------------
    needs_metal_storage = (
        len(metal_fill_history) >= METAL_FILL_ROUNDS
        and all(r >= METAL_FILL_TRIGGER for r in metal_fill_history)
    )
    needs_energy_station = energy_load_ratio >= ENERGY_LOAD_TRIGGER

    is_emerg, emerg_reasons = gs.is_emergency()

    # 3b. BUILDER MINIMUM STOCK (mine-coupled) --------------------------------
    # REPLACES the old formulas target_pioneers()/target_constructors().
    # Computed in builder_requirements() (also used directly by bot_run).
    _br = builder_requirements(gs)
    mine_count            = _br["mine_count"]
    required_constructors = _br["required_constructors"]
    required_pioneers     = _br["required_pioneers"]
    constructor_deficit   = _br["constructor_deficit"]
    pioneer_deficit       = _br["pioneer_deficit"]
    builder_priority      = _br["builder_priority"]

    # 4. tactical findings ---------------------------------------------------
    units_under_threat       = _find_units_under_threat(gs, hm, terrain)
    safe_expansion_candidates = _rank_expansion_candidates(gs, hm, expansion_rejected)
    scout_safe_targets       = _find_scout_targets(gs, hm, terrain)
    dz                       = high_threat_zones(hm, threshold=DANGER_ZONE_THRESHOLD)

    return StrategyReport(
        heatmaps              = hm,
        metal_fill_ratio      = metal_fill,
        energy_load_ratio     = energy_load_ratio,
        map_explored_pct      = map_explored,
        net_connected         = net_connected,
        next_expansion_goal   = next_goal,
        units_under_threat    = units_under_threat,
        safe_expansion_candidates = safe_expansion_candidates,
        scout_safe_targets    = scout_safe_targets,
        danger_zones          = dz,
        needs_metal_storage   = needs_metal_storage,
        needs_energy_station  = needs_energy_station,
        is_emergency          = is_emerg,
        emergency_reasons     = emerg_reasons,
        mine_count            = mine_count,
        required_constructors = required_constructors,
        required_pioneers     = required_pioneers,
        constructor_deficit   = constructor_deficit,
        pioneer_deficit       = pioneer_deficit,
        builder_priority      = builder_priority,
    )


# ---------------------------------------------------------------------------
# Finding 1: own units under threat
# ---------------------------------------------------------------------------

def _find_units_under_threat(gs, hm: HeatMaps, terrain) -> list:
    """Finds ALL own vehicles whose current field has danger > THREAT_UNIT_MIN_DANGER
    (they are within enemy weapon range).

    For each threatened unit the safest retreat field is computed immediately
    (safest_neighbor from heat_map_calc). The bot can use this value directly as
    a movement target without further computation.

    Vehicles only - buildings cannot flee.
    Sorted by descending danger value (most threatened first).
    """
    result = []
    for v in gs.my_vehicles():
        x, y = gs.pos(v)
        d = danger_at(hm, x, y)
        if d < THREAT_UNIT_MIN_DANGER:
            continue
        t = threat_at(hm, x, y)
        retreat = safest_neighbor(hm, x, y, terrain=terrain)
        result.append(ThreatUnit(
            unit_id    = v["id"],
            pos        = (x, y),
            danger     = d,
            threat     = t,
            retreat_to = retreat,
        ))
    result.sort(key=lambda u: -u.danger)
    return result


# ---------------------------------------------------------------------------
# Finding 2: safe expansion candidates
# ---------------------------------------------------------------------------

def _rank_expansion_candidates(gs, hm: HeatMaps, expansion_rejected: set) -> list:
    """Rates all explored resource deposits by two criteria:
       (a) in-game score (from expansion_target - amount, demand, distance)
       (b) safety: danger value at the target field + max threat on the route

    The result lets the bot choose, among several good deposits, the one that is
    BOTH strategically valuable AND safely reachable.

    Deposits on completely safe fields (danger=0, route_threat<=0) come first;
    among equally safe ones the in-game score decides.

    Returns up to EXPANSION_CANDIDATE_MAX candidates.
    """
    resources = gs.explored_resources()
    if not resources:
        return []

    base = gs.base_reference_field()
    result = []

    # deduplicate resource fields (there can be many fields per deposit)
    # we take all explored fields and compute one candidate per field.
    # skip fields in expansion_rejected or already covered by a mine.
    seen_fields = set()
    for r in resources:
        x, y = r.get("x", 0), r.get("y", 0)
        pos = (x, y)
        if pos in expansion_rejected:
            continue
        if pos in seen_fields:
            continue
        if gs.mine_covering(pos) is not None:
            continue
        seen_fields.add(pos)

        d = danger_at(hm, x, y)
        t = threat_at(hm, x, y)

        # route threat: max threat on straight line base -> target
        route_t = _route_max_threat(hm, base, pos)

        # get in-game score from expansion_target (expensive but precise)
        # we use the single-field score as an approximation: amount / (dist + k)
        amount  = r.get("amount", 0) or 0
        dist    = math.sqrt((x - base[0])**2 + (y - base[1])**2)
        k       = 3.0
        score   = amount / (dist + k) if amount > 0 else 0.0

        result.append(ExpansionCandidate(
            pos          = pos,
            res_type     = r.get("type", "metal"),
            amount       = amount,
            score        = score,
            danger       = d,
            route_threat = route_t,
            threat       = t,
        ))

    # sorting: safest route first (route_threat ascending),
    # then lowest danger at the target, then highest score.
    result.sort(key=lambda c: (c.route_threat, c.danger, -c.score))
    return result[:EXPANSION_CANDIDATE_MAX]


def _route_max_threat(hm: HeatMaps, from_pos: tuple, to_pos: tuple) -> float:
    """Maximum threat value on the straight line from from_pos to to_pos.
    Support points: one step each along the line (Bresenham approximation)."""
    fx, fy = from_pos
    tx, ty = to_pos
    steps = max(abs(tx - fx), abs(ty - fy), 1)
    max_t = 0.0
    for i in range(steps + 1):
        t = i / steps
        px = int(round(fx + t * (tx - fx)))
        py = int(round(fy + t * (ty - fy)))
        max_t = max(max_t, threat_at(hm, px, py))
    return max_t


# ---------------------------------------------------------------------------
# Finding 3: safe scout targets
# ---------------------------------------------------------------------------

def _find_scout_targets(gs, hm: HeatMaps, terrain) -> list:
    """Finds fields for scouts: little observed (enemy scan) AND little dangerous
    (danger near 0).

    Strategy: the map is split into a coarse grid. For each grid cell the
    representative field (centre) is evaluated. Cells outside the map or on
    blocked terrain are skipped. Cells near the own base are preferentially
    avoided (scouts explore UNKNOWN areas, not the own base).

    Sorted by ascending observed value (least observed first).
    Returns up to SCOUT_CANDIDATE_COUNT candidates.
    """
    w, h = hm.width, hm.height
    base = gs.base_reference_field()
    bx, by = base

    # grid size: ~10% of the map width, at least 4 fields
    step = max(4, w // 10)

    candidates = []
    terrain_data = terrain.get("data", "") if terrain else ""

    for gy in range(step // 2, h, step):
        for gx in range(step // 2, w, step):
            # terrain check: no blocked field
            idx = gy * w + gx
            if terrain_data and 0 <= idx < len(terrain_data):
                if terrain_data[idx] == "#":
                    continue

            # skip too close to the base (scouts should explore)
            dist_base = math.sqrt((gx - bx)**2 + (gy - by)**2)
            if dist_base < step * 1.5:
                continue

            obs = hm.observed[gy][gx]
            d   = hm.danger[gy][gx]

            candidates.append(ScoutTarget(
                pos          = (gx, gy),
                observed_val = obs,
                danger       = d,
            ))

    # sorting: least observed first, on a tie least dangerous
    candidates.sort(key=lambda s: (s.observed_val, s.danger))
    return candidates[:SCOUT_CANDIDATE_COUNT]


# ---------------------------------------------------------------------------
# Helper function: textual summary (for log)
# ---------------------------------------------------------------------------

def report_summary(report: StrategyReport) -> str:
    """Creates a compact log line from the StrategyReport."""
    lines = []

    # economy
    lines.append(
        f"Metall {report.metal_fill_ratio:.0%} "
        f"| Energie {report.energy_load_ratio:.0%} "
        f"| Karte {report.map_explored_pct:.0f}% erkundet"
        f" | Netz {'OK' if report.net_connected else 'GETRENNT'}"
    )

    # flags
    flags = []
    if report.is_emergency:
        flags.append(f"NOTFALL({len(report.emergency_reasons)})")
    if report.needs_metal_storage:
        flags.append("SPEICHER_NOETIG")
    if report.needs_energy_station:
        flags.append("ENERGIE_NOETIG")
    if flags:
        lines.append("Flags: " + ", ".join(flags))

    # builder minimum stock (mine-coupled)
    if report.builder_priority:
        lines.append(
            f"Bauer-Mangel (Minen={report.mine_count}): "
            f"Konstrukteur {report.constructor_deficit} fehlen "
            f"(Soll {report.required_constructors}), "
            f"Pionier {report.pioneer_deficit} fehlen "
            f"(Soll {report.required_pioneers}) -> Reihenfolge {report.builder_priority}"
        )

    # threats
    if report.units_under_threat:
        ids = [str(u.unit_id) for u in report.units_under_threat[:3]]
        lines.append(
            f"Bedroht: {len(report.units_under_threat)} Einheit(en)"
            f" (IDs: {', '.join(ids)}{'...' if len(report.units_under_threat) > 3 else ''})"
        )

    # expansion
    if report.safe_expansion_candidates:
        best = report.safe_expansion_candidates[0]
        lines.append(
            f"Expansion: {best.res_type} @ {best.pos} "
            f"score={best.score:.2f} danger={best.danger:.1f} route={best.route_threat:.1f}"
        )

    # danger zones
    if report.danger_zones:
        lines.append(f"Gefahrenzonen: {len(report.danger_zones)} Felder")

    return " | ".join(lines)
