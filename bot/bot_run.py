#!/usr/bin/env python3
"""
bot_run.py - turn orchestrator (phase model) for the MAXR bot.

Implements ALGORITHMUS.md. Uses maxr_bot_lib (GameState + Conn).

PHASE MODEL per turn (universal for all modes):
  Phase 1  inventory (before movement) + emergency check            [mandatory]
  Phase 2  planning                                                 [mandatory]
  Phase 3  execution 1 (main execution): active modes by priority   [mandatory]
  Phase 4  inventory 2 (after execution) + emergency check          [mandatory]
  Phase 5  execution 2 (final execution, reacts broadly)            [optional]
  Phase 6  inventory 3                                              [optional]
  Phase 7  reward/statistics (no emergency check)                   [mandatory]
  Phase 8  end of turn                                              [mandatory]

MODES are non-exclusive priority layers (emergency=0 top, then defensive/
offensive/expansion) that feed into the execution phases; on a conflict over
units/ore the priority decides. Currently only 'emergency' is filled.

Everything OPTIMISTIC: try the action, the bridge filters ("rejected" -> react).

Invocation: python bot_run.py [host] [port] [player_name]
"""
import sys
import os
import datetime
import math
from maxr_bot_lib import GameState, Conn
from build_plan import (BuildPlan, BuildBacklog, BuildSite, MetalReservation,
                        SiteComponent, COMP_PLATFORM, COMP_MINE, COMP_CONNECTOR,
                        COMP_CLEAR)
from strategy import builder_requirements, take_inventory
import upgrade_logic
import heat_map_calc
import unit_modes
import defense_planner

# Persistent build plan: the modes (emergency/expansion) provide the PRIORITY; the
# build-planning phase fixes complete orders (builder, building, place, ore budget)
# and writes them in here; the build phase executes them. Lives across the phases
# of a turn.
_PLAN = BuildPlan()

# --- BUILD-SITE BACKLOG (Dungeon-Keeper model) ---------------------------
# Persistent, priority-sorted wishlist of all expansion sites. Marked/updated in
# phase 2 (planning) and (later) consumed in phase 3. SWITCH: as long as
# _USE_BACKLOG is False, the backlog runs ONLY PASSIVELY (marks + logs), the
# assignment is still done by the old mode_expansion. This lets the recognition be
# checked in-game before the build behaviour switches. Set to True -> mode_expansion
# becomes the consumer.
_USE_BACKLOG = True
_BACKLOG = BuildBacklog()
_RESERVATION = MetalReservation()

# --- File logging: one new log file per bot start (game session) with a --------
#     timestamp in the name, so the name stays unique. Located in the subfolder
#     'logs' next to bot_run.py.
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
try:
    os.makedirs(_LOG_DIR, exist_ok=True)
    _LOG_PATH = os.path.join(
        _LOG_DIR,
        "bot_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
    _LOG_FILE = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)
except Exception as _e:
    _LOG_FILE = None
    _LOG_PATH = None
    print(f"[bot] WARN: Konnte Logdatei nicht anlegen ({_e}) - nur Konsole.")

# Builds are built at MAX SPEED: the bot sends speed=-1, the bridge resolves that
# to the highest available level (cheap buildings like storage have no x4 level ->
# automatic fallback to x2/x1).
BUILD_SPEED_MAX = -1

# Target amounts for the stabilisation (level 1)
TARGET_STORAGE_METAL = 3
TARGET_STORAGE_OIL = 2
METAL_STORE_TARGET = 40
OIL_STORE_TARGET = 40

# Target stock of reconnaissance units (light factory).
# Priority ranking in the light factory:
#   pioneer > surveyor > bulldozer > scout.
#   - surveyor : 5 pieces at ALL times (for area exploration).
#   - bulldozer: only if rubble is visible. 1 piece from >0 rubble,
#                2 pieces from >10 visible rubble fields.
#   - scout    : ENDLESS, once surveyor and bulldozer demand is covered.
#                Only interrupted if pioneer/surveyor/bulldozer are missing,
#                then automatically resumed.
REQUIRED_SURVEYORS = 5

# === PIONEER CORE-TASK QUOTAS (allocation logic) ========================
# Each pioneer is assigned a CORE TASK by ID (see _CORE_TASK + allocate_core_tasks).
# There are three pots; 1/3 of the pioneer pool each is intended as a MINIMUM
# reservation for a pot ("at least 1, provided the pot has work"). 1/3 (instead of
# 0.30) chosen so that with 3 pots the "at least 1 per working pot" calculation
# works out exactly.
#   - net repair    : coupling stretches to separated islands.
#   - platform build: water platforms for water mines.
#   - base expansion: defense/storage etc. - LOGIC NOT YET BUILT; the pot currently
#     NEVER has work (has_work=False), so it reserves nothing.
# PRIORITY of the core tasks: emergency > core task > other tasks. Among the core
# tasks, NET REPAIR has priority over platform build (floating free pioneers go
# preferentially to the repair). A pioneer with running, STARTED build work
# (_UNIT_ALLOC started=True) keeps its core task mandatorily - it may ONLY change if
# it is not allocated to a concrete build job.
_CORE_QUOTA = 1.0 / 3.0           # 1/3 of the pioneer pool per pot (min. 1 if there is work)
_CORE_REPAIR = "net_repair"       # pot tag = _UNIT_ALLOC task (same identifiers)
_CORE_PLATFORM = "platform_chain"
_CORE_BASE = "base_expansion"     # placeholder pot (no work yet)
_CORE_TASK: dict = {}             # pid -> core-task tag (_CORE_REPAIR/_PLATFORM/_BASE)

# Current location of the defense group under construction (2x2 corner (ox,oy)).
# Held until the group of four stands complete; then re-chosen for the next gap.
# None = no group currently under construction.
_DEFENSE_SITE = {"corner": None}

# STRUCTURAL ENERGY RULE: at least 1 power station per N started mines. Checked
# proactively in the inventory - this way the energy deadlock (mines off for lack
# of power -> no metal -> station unaffordable) does not arise in the first place.
# Target = ceil(mines / N), actual = stations incl. construction + in the plan.
_MINES_PER_STATION = 4

# DAMPING OF THE DEMAND DENOMINATOR per resource type (saturation exponent 0..1)
# for the mine site scoring (weighted_yield / _demand_factor):
#   demand(type) = DEMAND_TARGET[type] / (1 + already_extracted[type]) ** SAT[type]
# 1.0 = as before (demand falls strongly once a lot is extracted). Smaller = the
# type saturates SLOWER and stays in demand even at high own output. Metal is the
# expansion bottleneck (only metal builds buildings) -> small value, so the bot
# clearly prefers metal deposits and does not switch to gold/oil crumbs because of
# high metal output. Set into the GameState at startup.
_DEMAND_SATURATION = {"metal": 0.3, "oil": 1.0, "gold": 1.0}


# PERSONALITY (defense screen). Controls the TARGET COVERAGE DEGREE per screen
# field: as long as a screen field is covered by FEWER than cover_target own
# defense buildings, building continues (within the budget).
#   aggressive = 1  (each field covered once is enough; the rest goes into attack)
#   neutral    = 2  (double coverage)
#   defensive  = 3  (triple overlap; the screen survives the loss of installations)
# Applies to land/sea AND air equally (one screen geometry). The RADAR scan underlay
# has a FIXED target degree of 1 (one radar is enough to see; scan overlap brings -
# unlike overlapping firepower - little).
# The budget for now remains only an upper limit (option A); the coverage degree is
# the actual driver. Set into the GameState at startup.
_PERSONALITY = "neutral"   # "aggressive" | "neutral" | "defensive"

_COVER_TARGET_BY_PERSONALITY = {"aggressive": 1, "neutral": 2, "defensive": 3}
_RADAR_COVER_TARGET = 1    # fixed: one radar is enough to see a screen field

# METAL THRESHOLD FOR DEFENSE BUILDING. Defense buildings (gun_*, radar) may only
# be built once the economy can bear them - otherwise metal is pulled from the mine
# expansion too early and the expansion is choked. Released from 3 mines OR gross
# metal income >= 30 (metal_income = sum of metalProd of all mines). STORAGE
# (metal/oil/gold) is EXEMPT - it keeps running purely on demand and is not affected
# by this threshold.
_DEFENSE_MIN_MINES = 3
_DEFENSE_MIN_METAL_INCOME = 30


def defense_build_allowed(gs):
    """True if defense buildings (gun_*, radar) may be built:
    from _DEFENSE_MIN_MINES mines OR gross metal income >= _DEFENSE_MIN_METAL_INCOME.
    Does NOT apply to storage (which runs on demand, without this threshold)."""
    return (len(gs.my_mines()) >= _DEFENSE_MIN_MINES
            or gs.metal_income() >= _DEFENSE_MIN_METAL_INCOME)


def cover_target_for(personality):
    """Target coverage degree (weapon screen) for a personality. Unknown personality
    falls back to 'neutral'."""
    return _COVER_TARGET_BY_PERSONALITY.get(personality, 2)


def log(msg):
    # empty string -> real blank line (without [bot] prefix), for visual separation
    # between the turns in the console/file output.
    line = "" if msg == "" else f"[bot] {msg}"
    print(line)
    if _LOG_FILE is not None:
        try:
            _LOG_FILE.write(line + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Task determination: what should the engineer / constructor build next?
# Returns (building_name, building_sid) or None respectively.
# Considers buildings under construction (count_with_construction).
# ---------------------------------------------------------------------------
def _plan_metal_cap(gs, plan):
    """Planned ore storage capacity = real+underConstruction + assigned this turn."""
    return gs.storage_capacity_incl_construction(1) + plan["storage_metal"] * 50

def _plan_oil_cap(gs, plan):
    return gs.storage_capacity_incl_construction(2) + plan["storage_oil"] * 50

def _plan_energy(gs, plan):
    # generator (energy small) provides 1, station (Energy_Big) provides 6.
    return (gs.energy_production_incl_construction()
            + plan["generators"] * 1 + plan.get("stations", 0) * 6)


def next_task_for_role(gs, role, plan):
    """Next open build project for a builder of this role, TAKING INTO ACCOUNT the
    builds already assigned this turn (plan). This way several identical builders
    distribute automatically: as soon as an assigned build satisfies the threshold
    (predictively), the next builder takes the next open project. What counts is the
    CAPACITY, not the count. Returns (name, sid) or None."""
    if role == "engineer":
        sid_sm = gs.building_sid_by_name("storage-metal")
        sid_so = gs.building_sid_by_name("storage-oil")
        sid_gen = gs.building_sid_by_name("energy small")
        sid_station = gs.building_sid_by_name("Energy_Big")
        sid_radar = gs.building_sid_by_name("radar")
        # METAL-STORAGE TRIGGER RULE: build a new storage if the fill ratio of the
        # last 3 turns was continuously >= 80% (history complete and all values
        # >= 0.80). Started storages are already in the denominator
        # (storage_capacity_incl_construction), so they lower the fill ratio
        # immediately - as long as a storage is under construction, this condition is
        # typically no longer permanently satisfied. If SEVERAL storages are needed
        # (fill ratio stays high despite a new storage), at most one per builder is
        # planned per turn (the plan accumulator already counts assigned ones:
        # _plan_metal_cap).
        _metal_full_3 = (
            len(_METAL_FILL_HISTORY) >= 3
            and all(r >= 0.80 for r in _METAL_FILL_HISTORY)
        )
        if sid_sm is not None and _metal_full_3 and plan.get("storage_metal", 0) == 0:
            return ("storage-metal", sid_sm)
        if sid_so is not None and _plan_oil_cap(gs, plan) < OIL_STORE_TARGET:
            return ("storage-oil", sid_so)
        # ENERGY (pioneer share): the pioneer builds the 1x1 GENERATOR (energy
        # small, 2 oil). This is the way to build up energy step by step from LITTLE
        # oil (breaks the deadlock: waiting mines need energy but only deliver oil
        # then). BUT: from MAX_GENERATORS (2) NO further generators - otherwise many
        # inefficient generators eat all the oil (oil trap) and no reserve remains
        # for the efficient station. Instead the bot then saves up for the station
        # (the constructor builds it).
        if sid_gen is not None \
                and (gs.generator_count() + plan["generators"]) < gs.MAX_GENERATORS \
                and _plan_energy(gs, plan) < gs.energy_potential_need() + gs.MINE_ENERGY_NEED \
                and gs.fuel_for_energy_ok():
            station_cost = gs.build_cost(sid_station) if sid_station is not None else 24
            station_viable = (sid_station is not None
                              and gs.fuel_for_station_ok()
                              and emergency_metal_pool(gs) >= station_cost)
            if not station_viable:
                return ("energy small", sid_gen)
        if sid_radar is not None and (gs.count_with_construction(sid_radar) + plan["radar"]) < 1:
            return ("radar", sid_radar)
        # GOLD STORAGE: we normally build NO gold storage. Rule:
        #  (1) As soon as the first GOLD MINE exists OR IS BEING BUILT (mine with at
        #      least 1 gold in the 2x2 area), ONE gold storage enters the backlog
        #      (gold_mine_exist: 'under construction counts as built').
        #  (2) From then on: if the gold storage capacity is >=50% filled, ANOTHER
        #      one comes. Etc. A freshly built (empty) storage lowers the fill ratio
        #      immediately, so the chain regulates itself.
        # plan-aware: at most ONE per turn (plan['storage_gold']), so that at 50% not
        # several are planned at once.
        sid_gold = gs.building_sid_by_name("storage-gold")
        if sid_gold is not None and gs.gold_mine_exist() and plan.get("storage_gold", 0) == 0:
            gold_cap = gs.storage_capacity_incl_construction(gs.GOLD_STORE_RES_TYPE)
            if gold_cap <= 0:
                return ("storage-gold", sid_gold)          # (1) first gold storage
            if gs.gold_storage_fill_ratio() >= 0.5:
                return ("storage-gold", sid_gold)          # (2) another from 50% full
        return None
    if role == "constructor":
        sid_sf = gs.building_sid_by_name("smallfactory")
        sid_bf = gs.building_sid_by_name("bigfactory")
        sid_station = gs.building_sid_by_name("Energy_Big")
        # ENERGY (constructor share): preferentially build the efficient 2x2 STATION
        # as soon as energy is missing and the fuel is enough. Before the factories,
        # because without energy neither factory nor mine runs. The station is also
        # built if the oil is currently scarce but would become free by switching off
        # surplus generators (oil-trap resolution) - the generators are switched off
        # ONLY AFTER the station build (no energy drop).
        station_fuel = gs.fuel_for_station_ok() \
            or gs.station_viable_by_freeing_generators()[0]
        # STATION TRIGGER: three independent occasions (OR-linked):
        #  (A) overcapacity check: production is not enough for current demand + next
        #      mine (previous emergency logic).
        #  (B) 90% load rule (expansion): energy load >= 90% in the last preparation
        #      phase -> proactively build a station before the emergency occurs.
        #      _ENERGY_LOAD_RATIO already counts under-construction stations in the
        #      denominator -> no multiple triggers while the first is under
        #      construction.
        #  (C) STRUCTURAL: at least one station per _MINES_PER_STATION started mines.
        #      Target = ceil(mines / N); actual = stations incl. construction + in the
        #      plan. Prevents the energy deadlock proactively, BEFORE mines are
        #      switched off for lack of power (then no metal -> station unaffordable).
        stations_have = gs.station_count_incl_construction() + plan.get("stations", 0)
        stations_want = (max(1, math.ceil(gs.mine_count() / _MINES_PER_STATION))
                         if gs.mine_count() > 0 else 0)
        _too_few_stations = stations_have < stations_want
        _station_needed = (
            _plan_energy(gs, plan) < gs.energy_potential_need() + gs.MINE_ENERGY_NEED
            or _ENERGY_LOAD_RATIO >= 0.90
            or _too_few_stations
        )
        # multiple protection: do not trigger a NEW station if one is already
        # assigned in THIS plan (one per turn). The target/actual comparison (C)
        # covers multiple stations across the turns.
        if sid_station is not None \
                and _station_needed \
                and station_fuel \
                and emergency_metal_pool(gs) >= (gs.build_cost(sid_station) or 24) \
                and plan.get("stations", 0) == 0:
            return ("station", sid_station)
        # GOLD REFINERY: coupled to the network gold production. A refinery converts
        # 5 gold/turn (convertsGold=5, verified). Rule:
        #   TARGET refineries = floor(gold production / 5) + 1, BUT only if the gold
        #   production > 0 (at least one mine extracts >= 1 gold).
        #   At 0 gold production: NO refinery (it would only eat energy).
        # Example: prod 0 -> 0 | prod 5 -> 2 | prod 7 -> 2 (floor) | prod 10 -> 3.
        # Actual number incl. under construction (count_..._incl_construction) -> no
        # multiple builds; plan-aware (plan['gold_refinery']) -> at most ONE per turn.
        sid_refinery = gs.gold_refinery_sid()
        gold_prod = gs.gold_income()
        if sid_refinery is not None and gold_prod > 0:
            required_refineries = (gold_prod // 5) + 1
            have_refineries = (gs.count_gold_refineries_incl_construction()
                               + plan.get("gold_refinery", 0))
            if have_refineries < required_refineries:
                return ("gold_refinery", sid_refinery)
        # light factory: open if not available/under construction AND none assigned
        # yet this turn
        if sid_sf is not None and not gs.factory_available("smallfactory") \
                and plan["smallfactory"] == 0:
            return ("smallfactory", sid_sf)
        if sid_bf is not None and not gs.factory_available("bigfactory") \
                and plan["bigfactory"] == 0:
            return ("bigfactory", sid_bf)
        return None
    return None


def plan_add(plan, task_name):
    """Advance the plan accumulator by one assigned build task."""
    if task_name == "storage-metal":   plan["storage_metal"] += 1
    elif task_name == "storage-oil":   plan["storage_oil"] += 1
    elif task_name == "energy small":  plan["generators"] += 1
    elif task_name == "station":       plan["stations"] = plan.get("stations", 0) + 1
    elif task_name == "radar":         plan["radar"] += 1
    elif task_name == "storage-gold":  plan["storage_gold"] = plan.get("storage_gold", 0) + 1
    elif task_name == "gold_refinery": plan["gold_refinery"] = plan.get("gold_refinery", 0) + 1
    elif task_name == "smallfactory":  plan["smallfactory"] += 1
    elif task_name == "bigfactory":    plan["bigfactory"] += 1


# ---------------------------------------------------------------------------
# Resource priority: orders competing build projects.
# storage > energy > factory > radar. Only open conditions are represented here at
# all (engineer_task/constructor_task return only open ones).
# ---------------------------------------------------------------------------
PRIORITY = {
    "storage-metal": 0, "storage-oil": 1,    # storage
    "station": 2, "energy small": 2,          # energy (station like generator)
    "gold_refinery": 2,                       # gold refinery (like energy - economy)
    "smallfactory": 3, "bigfactory": 3,       # factory
    "radar": 4,                               # radar
}


def base_metal_available(gs):
    """Ore ready for transfer in the network (SubBase). Approximation: sum of
    storageResCur over own buildings (mine + storages share the SubBase, but
    storageResCur per building is a usable approximation of the stock)."""
    return sum(b.get("storageResCur", 0) or 0 for b in gs.my_buildings())


def main_subbase_metal(gs):
    """Ore stock of the MAIN COMPONENT (SubBase of the base) from which a docking
    builder can transfer. MAXR (actiontransfer.cpp) draws on a building->vehicle
    transfer from subBase->getResourcesStored() - i.e. the TOTAL stock of the
    SubBase, NOT from the single anchor building. The bot must therefore size the
    load amount by this total stock, otherwise the builder only loads the (often
    small) storageResCur of the one anchor per turn and barely makes progress.
    Sums storageResCur of all metal-storing buildings at the main component."""
    main = gs.main_component()
    total = 0
    for b in gs.my_buildings():
        st = gs._static_by_sid.get((gs.unit_first(b), gs.unit_type(b))) or {}
        if st.get("storeResType") == 1 and st.get("storageResMax", 0) > 0:
            big = gs.is_big_building_type(gs.unit_type(b))
            cells = gs.footprint(gs.pos(b), big)
            if any(gs.is_connected_to_main(cx, cy, main) for cx, cy in cells):
                total += b.get("storageResCur", 0) or 0
    return total


def ore_available_for(gs, builder_id):
    """Ore stock of the main component that THIS builder may load.

    The former 'hard ore lock' for the first ore-mine project is REMOVED - it was
    coupled to the emergency and blocked the expansion. The first mine is now secured
    solely via the backlog priority (mandatory, place 1) and the preloaded reserve
    (_FIRST_MINE_RESERVE), not via an ore lock. Therefore: always the normal pot."""
    return main_subbase_metal(gs)


def emergency_metal_pool(gs):
    """Ore pot for the EMERGENCY build phase (plan_emergency/execute_plan). The hard
    ore lock formerly effective here (pot 0, while the ore-mine project collects) is
    REMOVED - the emergency no longer builds a mine and is no longer starved by a
    loading mine builder. Always the normal stock."""
    return base_metal_available(gs)


def plan_reserved_fields(gs, plan, exclude_builder=None):
    """ALL fields that the ONE build plan has already reserved for build projects
    (footprints of all orders). Basis of the cross-mode priority: a lower-priority
    project (expansion) may NEVER build on a field that a higher-priority one
    (emergency) has already taken - the ONE planner knows all reservations.
    exclude_builder: exclude the orders of this builder (e.g. when it is itself being
    re-planned)."""
    reserved = set()
    for t in plan.all_tasks():
        if exclude_builder is not None and t.builder == exclude_builder:
            continue
        reserved |= gs.footprint(t.field, gs.is_big_building_type(t.sid))
    return reserved


def footprint_collides_with_plan(gs, plan, field, is_big, exclude_builder=None):
    """True if the (1x1 or 2x2) footprint at 'field' overlaps with ANY field already
    reserved in the plan. This lets the one planner prevent an expansion project
    (mine/platform/connector) from being placed on a build site already reserved for
    the emergency."""
    cells = gs.footprint(field, is_big)
    reserved = plan_reserved_fields(gs, plan, exclude_builder=exclude_builder)
    return bool(cells & reserved)


def path_cost_to(conn, builder_id, target):
    """Real PATH COST of a builder to a target field - via the bridge's pathCost
    query (the game's own pathfinder, same logic as for the move). Returns a dict
    {reachable, cost, steps, speedMax} or None on error. 'cost' is the summed
    movement cost over the real terrain (not straight line) - the correct measure for
    whether a unit under way is making progress."""
    rep = conn.query({"query": "pathCost", "unitId": builder_id,
                      "target": list(target)})
    if not rep or rep.get("error"):
        return None
    return rep


def record_build_move(conn, gs, builder, target):
    """Remembers that 'builder' has SET OFF in the first build phase towards its
    build site 'target' (instead of already building), together with the number of
    fields ACTUALLY driven this turn. From this the pause before the second inventory
    is later computed. The unit drives this turn only as far as its current movement
    points (speedCur) reach - waiting for more fields would be pointless (it does not
    get further anyway)."""
    pc = path_cost_to(conn, builder["id"], target)
    if not pc:
        return
    steps = pc.get("steps", 0) or 0
    cost = pc.get("cost", 0) or 0
    if steps <= 0 or cost <= 0:
        return
    speed_cur = speed_left(gs, builder)
    # portion of the path covered this turn with the available movement points
    # (cost = total cost over all steps fields).
    if cost <= speed_cur:
        driven = steps                       # whole path doable -> reaches goal
        _PENDING_BUILD_MOVE[builder["id"]] = driven
    # if the builder does NOT reach the goal this turn (path longer than movement
    # points), a pause brings nothing - it arrives only later anyway and cannot build
    # this turn. Then plan NO pause (no time loss for a build that does not happen
    # anyway).


def factory_reserve_cost(conn, builder_id, sid):
    """Ore demand for the BEST POSSIBLE build speed of a building - from the 'price
    list' (buildSpeeds query of the bridge). Returns the cost of the highest valid
    turbo level (more speed costs more ore). Falls back to the x1 base cost
    (baseCost) if no options are delivered. This way the bot knows IN ADVANCE how
    much ore it must reserve."""
    rep = conn.query({"query": "buildSpeeds", "unitId": builder_id,
                      "buildingId": [1, sid]})
    if not rep:
        return None
    opts = rep.get("options") or []
    if opts:
        # highest level (speed=2 > 1 > 0) = best possible speed
        best = max(opts, key=lambda o: o.get("speed", 0))
        return best.get("cost", rep.get("baseCost", 0))
    return rep.get("baseCost", 0)


# ---------------------------------------------------------------------------
# Execution of a build task for a unit (optimistic).
# Returns True if ore was still "consumed"/reserved this turn (for the allocation
# bookkeeping).
# ---------------------------------------------------------------------------
def _reget(gs, conn, uid):
    """Fetch a fresh state and return (gs, unit). Like a player looking at the screen
    after an action. Falls back to the old gs if the query fails."""
    fresh = conn.refresh_state()
    if fresh is None:
        return gs, refresh_unit(gs, uid)
    return fresh, refresh_unit(fresh, uid)


def run_builder(gs, conn, builder, task, metal_budget, blocked=None, targets=None,
                may_reload=True):
    """Returns (result, gs). result=True = action executed (progress).
    blocked: set of build_pos that this unit should avoid.
    targets: dict unitId->build_pos (intention memory).
    may_reload: may this unit fetch ore now? (whitelisting/reservation).
    If False, the reload is skipped - the unit waits."""
    if blocked is None:
        blocked = set()
    if targets is None:
        targets = {}
    name, sid = task
    bid = builder["id"]
    bpos = gs.pos(builder)
    stored = gs.stored(builder)
    cap = gs.store_max(builder)
    cost = gs.build_cost(sid) if sid is not None else 0

    # 1. is a build already running? -> wait or finishBuild (get the unit off the field)
    if builder.get("isBuilding"):
        rem = builder.get("buildTurns", 0)
        if rem and rem > 0:
            log(f"  {name}: Bau laeuft (Rest {rem}) - warte.")
            return (False, gs)
        # build finished: the unit stands ON the new building and MUST get off.
        # Only once it is really gone (isBuilding=False in the fresh state) is it free
        # for new tasks. We verify that and otherwise try the next evasion field - do
        # not blindly trust the 'ok'.
        goal = base_center(gs)
        for esc in gs.escape_candidates(bpos, unit=builder, toward=goal):
            ok, reason = conn.do({"type": "finishBuild", "unitId": bid,
                                  "escapePosition": list(esc)})
            if not ok:
                continue  # field invalid/occupied -> next candidate
            g2 = conn.refresh_state()
            nb = (refresh_unit(g2, bid) if g2 else None)
            if g2 is not None and nb is not None and not nb.get("isBuilding"):
                log(f"  {name}: fertiggestellt, Einheit verlaesst Feld nach {g2.pos(nb)}.")
                return (True, g2)
            # 'ok' reported, but the unit is still building -> evasion did not work
            # (server rejected). Try the next field.
            if g2 is not None:
                gs = g2
                builder = nb or builder
                bpos = gs.pos(builder)
        log(f"  {name}: fertig, aber Einheit kommt nicht vom Feld - naechster Zug.")
        return (False, gs)

    # 2. too little ore? -> RELOAD, then continue to the build in the same turn.
    if stored < cost:
        # whitelisting (ore reservation): if this unit may not reload right now
        # (e.g. because a factory constructor has priority and its ore is reserved),
        # skip the reload - it waits.
        if not may_reload:
            log(f"  {name}: Nachladen gesperrt (Erz reserviert) - warte.")
            return (False, gs)
        anchor = adjacent_networked_building(gs, builder, need_metal=True)
        if anchor is None:
            dock = dock_field_at_base(gs, builder)
            if dock is None:
                log(f"  {name}: zu wenig Erz und kein Andockplatz.")
                return (False, gs)
            if bpos != dock:
                log(f"  {name}: zu wenig Erz ({stored}<{cost}) - fahre andocken {bpos}->{dock}.")
                ok_mv, _ = conn.do({"type": "move", "unitId": bid, "target": list(dock)})
                if ok_mv:
                    gs, builder = _reget(gs, conn, bid)
                    if builder is None:
                        return (False, gs)
                    bpos = gs.pos(builder)
                anchor = adjacent_networked_building(gs, builder, need_metal=True)
        if anchor is not None:
            # limit against the REAL SubBase pool, not just the budget (set at turn
            # start) - otherwise the bot sends 'transfer N' although the pool is
            # meanwhile empty, and the bridge rejects with 'source base has only 0'.
            # min(budget, real pool).
            want = min(cap - gs.stored(builder), metal_budget["base"],
                       main_subbase_metal(gs))
            if want > 0:
                ok, reason = conn.do({"type": "transfer", "unitId": anchor["id"],
                                      "targetId": bid, "amount": want, "resource": "metal"})
                if ok:
                    metal_budget["base"] -= want
                    gs, builder = _reget(gs, conn, bid)
                    if builder is None:
                        return (False, gs)
                    stored = gs.stored(builder); bpos = gs.pos(builder)
                    log(f"  {name}: nachgeladen, jetzt {stored} Erz.")
                else:
                    log(f"  {name}: Nachladen abgelehnt ({reason}).")
            else:
                log(f"  {name}: Netz hat kein Erz frei.")
        if stored < cost:
            log(f"  {name}: immer noch zu wenig Erz ({stored}<{cost}).")
            return (False, gs)

    # 3. enough ore -> choose build site (prefer the remembered target), drive there, build.
    build_pos = targets.get(bid)
    if build_pos is None or build_pos in blocked:
        # first look for an ADJACENT place (compact base preferred). If there is
        # none (e.g. narrow start island full), fall back to the NEAREST free place -
        # the pioneers connect the island thus created via the network connectivity
        # (network_gap_target). This way the constructor need not necessarily stick
        # to the base; "build where there is space" also applies in the emergency.
        build_pos = gs.find_build_position(builder, sid, avoid=blocked,
                                           require_connected=True)
        if build_pos is None:
            build_pos = gs.find_build_position(builder, sid, avoid=blocked,
                                               require_connected=False)
        if build_pos is None:
            log(f"  {name}: kein freier Bauplatz (auch nicht abgesetzt).")
            targets.pop(bid, None)
            return (False, gs)
        targets[bid] = build_pos   # remember the target
    if bpos != build_pos:
        log(f"  {name}: fahre {bpos} -> {build_pos} (Bauplatz).")
        ok_mv, _ = conn.do({"type": "move", "unitId": bid, "target": list(build_pos)})
        if not ok_mv:
            # drive rejected = target unreachable/blocked (e.g. meanwhile built on).
            # As with a rejected build: block the place and discard the target, so
            # that next time a DIFFERENT build site is chosen (otherwise the builder
            # sticks to the same unreachable target for turns).
            log(f"  {name}: Fahrt abgelehnt -> Platz {build_pos} gesperrt, neues Ziel.")
            blocked.add(build_pos)
            targets.pop(bid, None)
            return (True, gs)
        gs, builder = _reget(gs, conn, bid)
        if builder is None:
            return (False, gs)
        bpos = gs.pos(builder)
        if bpos != build_pos:
            log(f"  {name}: noch unterwegs ({bpos}) zu {build_pos}.")
            # the builder has SET OFF, wants to build -> remember for the pause
            # before the second build phase (so that it arrives there and builds,
            # instead of losing a turn).
            record_build_move(conn, gs, builder, build_pos)
            return (False, gs)  # target remembered; the second build phase builds after arrival

    ok, reason = conn.do({"type": "startBuild", "unitId": bid,
                          "buildingId": [1, sid], "speed": -1,
                          "position": list(build_pos)})
    if ok:
        log(f"  {name}: Bau gestartet auf {build_pos} (max speed).")
        targets.pop(bid, None)   # target reached, clear memory
        g2 = conn.refresh_state()
        return (True, g2 or gs)
    else:
        r = (reason or "").lower()
        if "moving" in r:
            log(f"  {name}: noch in Bewegung, Bau folgt ({reason}).")
            return (False, gs)
        log(f"  {name}: Bau abgelehnt: {reason} -> Platz {build_pos} gesperrt.")
        blocked.add(build_pos)
        targets.pop(bid, None)   # this target discarded, choose anew
        return (True, gs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def refresh_unit(gs, uid):
    for v in gs.my_vehicles():
        if v["id"] == uid:
            return v
    return None


def base_center(gs):
    """Centroid of the own buildings (for directed evasion towards the base)."""
    bs = gs.my_buildings()
    if not bs:
        return None
    xs = [gs.pos(b)[0] for b in bs]
    ys = [gs.pos(b)[1] for b in bs]
    return (sum(xs) // len(xs), sum(ys) // len(ys))


def adjacent_networked_building(gs, builder, need_metal=False):
    """Transfer SOURCE for ore reloading. The server (cBuilding::canTransferTo)
    requires: (1) the source must ITSELF carry the same storeResType as the builder
    (builder stores metal -> source metal-storing, storeResType==1); (2) the builder
    must stand next to a building of the SAME SubBase as the source (isNextTo, 8).
    The SubBase of the base source is the MAIN COMPONENT. Therefore the builder must
    stand next to a building that is connected TO THE MAIN COMPONENT - NOT next to
    any building. If it only stands next to a freshly built, NOT-yet-connected mine
    (own/no SubBase), the transfer fails and the unit hangs. Returns a valid source
    or None."""
    bx, by = gs.pos(builder)
    main = gs.main_component()

    # does the builder stand next to a building that hangs ON THE MAIN COMPONENT?
    # Only then is it in the SubBase of the base and can transfer from there.
    docked_to_main = False
    for b in gs.my_buildings():
        big = gs.is_big_building_type(gs.unit_type(b))
        cells = gs.footprint(gs.pos(b), big)
        if not any(gs.is_connected_to_main(cx, cy, main) for cx, cy in cells):
            continue   # this building does not hang on the main component
        for c in cells:
            if (bx, by) in gs.neighbors8(*c):
                docked_to_main = True
                break
        if docked_to_main:
            break
    if not docked_to_main:
        return None

    # find a metal-storing source at the MAIN COMPONENT (storeResType==1,
    # capacity>0). A not-connected/switched-off mine does not count.
    for b in gs.my_buildings():
        st = gs._static_by_sid.get((gs.unit_first(b), gs.unit_type(b))) or {}
        if st.get("storeResType") == 1 and st.get("storageResMax", 0) > 0:
            big = gs.is_big_building_type(gs.unit_type(b))
            cells = gs.footprint(gs.pos(b), big)
            if any(gs.is_connected_to_main(cx, cy, main) for cx, cy in cells):
                return b
    return None


def dock_field_at_base(gs, builder):
    """A free field in orthogonal neighbourhood to the MAIN COMPONENT of the base
    that the builder can enter (for docking/reloading). Nearest to the builder.
    IMPORTANT: only dock to the main component (not to an isolated, not-yet-connected
    mine) - otherwise the builder docks next to an island from which no transfer is
    possible (canTransferTo: same SubBase needed), and gets stuck."""
    occ = gs.occupied_fields()
    ww = gs.water_walkable_fields()
    main = gs.main_component()
    # use only base fields that belong to the main component as docking anchor.
    base_cells = set(gs.base_footprint()) & set(main) if main else set(gs.base_footprint())
    bx, by = gs.pos(builder)
    cands = []
    for (cx, cy) in base_cells:
        for n in gs.neighbors4(cx, cy):
            if n in base_cells:
                continue
            if gs.is_free_for_unit(builder, *n, occ=occ, water_ok=ww, ignore={(bx, by)}):
                cands.append(n)
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0]-bx)**2 + (c[1]-by)**2)
    return cands[0]


_SURVEYOR_PLANNERS = {}  # surveyor-id -> SurveyorPlanner (holds operation point)
_FRESH_UNITS = set()     # unit IDs created THIS turn (do not move immediately)
_KNOWN_SURVEYORS = set() # surveyor IDs that already existed in the PREVIOUS turn
_KNOWN_BULLDOZERS = set()# bulldozer IDs that already existed in the PREVIOUS turn
_BULLDOZER_TARGET = {}   # bulldozer-id -> remembered rubble target (x,y). Held
                         # against flutter: once chosen nearby rubble stays the
                         # target until it is cleared/gone.
_MODE_MAP = {}           # unit-id -> behaviour mode (section 9). Units with a mode
                         # are steered by the mode phase; the normal exploration
                         # (steer_surveyors) leaves them alone, so that not TWO move
                         # commands hit the same unit in the same turn (OOS).
# Builders that have SET OFF in the FIRST build phase towards their build site
# (instead of already building), with the number of fields actually driven this
# turn. From this the bot-side pause before the second inventory is computed, so
# that the host tick can process the drive and the unit arrives at its target in the
# SECOND build phase and builds there. Pause per builder: ceil(fields*0.18 + 0.5),
# max 2 s; globally the maximum over all set-off builders is taken (one pause covers
# them all, since they drive in parallel). Cleared at the start of each turn.
_PENDING_BUILD_MOVE = {}  # builder-id -> fields driven (int) in phase 3

# ACTIVITY NOTE for the stuck-clear (phase 8). Every build/load/drive action that a
# builder received THIS turn enters its ID here (_touch). The idle check skips every
# builder that was touched this turn - it IS working (even if it is currently waiting
# for material and looks idle at turn end). Only one that was NEVER touched the WHOLE
# turn is really stuck. Cleared at turn start.
_TOUCHED_THIS_TURN = set()  # builder-ids that had an action/effort this turn

# CENTRAL INVENTORY (phase 1): filled once per turn via take_inventory(gs).
# 'What do we have? What do we need?' for all builder/unit types (have/need/deficit).
# All later phases (production, station rule, constructor cap) READ from here instead
# of counting/computing themselves. Empty dict as long as phase 1 has not yet run.
_INVENTORY: dict = {}


def _touch(uid):
    """Notes that 'uid' received an action/effort this turn (build, load, drive,
    deliberate waiting for material). Protects against the stuck-clear."""
    if uid is not None:
        _TOUCHED_THIS_TURN.add(uid)

# HARD ORE LOCK for the ore-mine project. As soon as a surveyor has found the ore
# field and the mine routine runs (target fixed), the project is stored here.
# CAPTURES BOTH builders of the same project, IN ORDER:
#   1. the platform pioneer ("pio") gets its ore FIRST (~8 for the 4 platforms) and
#      sets off;
#   2. THEN the constructor ("con") its full 60 ore.
# Effect while set:
#   - NO other system gets ore (ore_available_for/emergency_metal_pool = 0 for
#     everyone except the currently privileged builder of this project);
#   - the EMERGENCY stays active (active_modes enforces it) until the constructor is
#     full. Applies to water AND land mines (for land only the pioneer part is
#     dropped, because no platforms are needed).
# Cleared at turn start and re-set in the mine routine.
#   Format: {"pio": int|None, "con": int|None}
_ORE_MINE_CONSTRUCTOR_LOCK = {}


def steer_surveyors(gs, conn):
    """Let surveyors explore MANUALLY - the bot steers them like build units. The
    pathfinding comes 1:1 from the ported MAXR algorithm (surveyor_planner), the
    movement is executed via a move action (stopOnResource). NO more client-side
    auto-move AI (setAutoMove) - it planned continuously on a possibly divergent
    intermediate state and was a desync source."""
    from surveyor_planner import SurveyorPlanner

    surveyors = gs.vehicles_of_type("surveyor")
    if not surveyors:
        return

    # positions of ALL own surveyors (for the distance scores).
    all_positions = {sv["id"]: gs.pos(sv) for sv in surveyors}
    live_ids = set(all_positions.keys())
    # clean up the planners: keep only living surveyors.
    for dead in [i for i in _SURVEYOR_PLANNERS if i not in live_ids]:
        del _SURVEYOR_PLANNERS[dead]

    # newly appeared surveyors (not there in the previous turn) were freshly built
    # this turn -> do NOT move them this turn (otherwise the movement starts offset
    # on host and bridge -> recurring ~26-tick OOS). Timing-independent: the moment
    # of first appearance counts, not the refresh timing after finishBuild.
    new_surveyors = live_ids - _KNOWN_SURVEYORS
    if new_surveyors:
        _FRESH_UNITS.update(new_surveyors)
        for nid in new_surveyors:
            # switch off the client-side auto-survey of the fresh unit immediately
            sv = next((s for s in surveyors if s["id"] == nid), None)
            if sv and sv.get("surveyorAutoMoveActive"):
                conn.do({"type": "setAutoMove", "unitId": nid, "active": False})
            log(f"  Surveyor {nid} frisch gebaut - bleibt diese Runde stehen.")
    for sv in surveyors:
        sid = sv["id"]
        vx, vy = gs.pos(sv)
        # clean up old state: if this surveyor still has the client-side auto-move
        # flag (from earlier runs / before the switch to bot control), it MUST be
        # off. Otherwise the full resync (recreateSurveyorMoveJobs) creates a
        # client-side cSurveyorAi that competes with the manual move control over the
        # same surveyor -> two move jobs -> assertion (vehicleId) / crash.
        if sv.get("surveyorAutoMoveActive"):
            conn.do({"type": "setAutoMove", "unitId": sid, "active": False})
            log(f"  Surveyor {sid}: altes Auto-Move abgeschaltet (Bot steuert jetzt).")
        # a surveyor that is currently moving / has an active job: do not re-plan
        # it this turn (it drives its path). Matches the original, which only
        # re-plans for a free surveyor.
        if sv.get("moving") or sv.get("jobActive"):
            log(f"  Surveyor {sid} @ ({vx},{vy}) faehrt bereits.")
            continue
        # IMPORTANT: also catch a WAITING move job. 'moving'/'jobActive' do NOT show
        # a waiting job; a second move would decouple the old job -> orphaned job ->
        # OUT OF SYNC.
        if gs.has_movejob(sid):
            log(f"  Surveyor {sid} @ ({vx},{vy}) hat bereits einen Move-Job.")
            continue
        # do NOT move a unit freshly created THIS turn: it only exists on host and
        # bridge from the finishBuild tick; a move in the same turn starts the
        # movement offset on both sides -> pixelToMove diverges for the first field
        # -> recurring ~26-tick OOS. OK from the next turn.
        if sid in _FRESH_UNITS:
            log(f"  Surveyor {sid} @ ({vx},{vy}) frisch gebaut - faehrt ab naechstem Zug.")
            continue
        # no movement points left this turn -> nothing to do.
        if (sv.get("data", {}).get("speedCur", 0) or 0) <= 0:
            log(f"  Surveyor {sid} @ ({vx},{vy}) keine Bewegungspunkte.")
            continue

        planner = _SURVEYOR_PLANNERS.get(sid)
        if planner is None:
            planner = SurveyorPlanner(vx, vy)
            _SURVEYOR_PLANNERS[sid] = planner

        others = [pos for i, pos in all_positions.items() if i != sid]
        kind, payload = planner.plan(gs, sv, others)

        if kind == "confused":
            log(f"  Surveyor {sid} @ ({vx},{vy}): nichts mehr zu erkunden.")
            continue

        if kind == "move":
            # payload = path (list (x,y), without start field). Target = last point.
            target = payload[-1]
        else:  # "long"
            target = payload

        ok, reason = conn.do({"type": "move", "unitId": sid,
                              "target": [target[0], target[1]],
                              "stopOnResource": True})
        log(f"  Surveyor {sid} @ ({vx},{vy}) -> {target} "
            f"({'ok' if ok else reason})")


def _bulldozer_pc(conn, uid, target):
    """Path-cost wrapper for reachable_fields (same signature as _pc)."""
    return path_cost_to(conn, uid, target)


def steer_bulldozers(gs, conn, hm=None):
    """Steer bulldozers MANUALLY (no MAXR auto - there is none for the bulldozer
    anyway). Behaves like a build unit:
      - rubble NEAR the own position first (not the most valuable, not across the
        map) -> no flutter. The chosen target is held (_BULLDOZER_TARGET) until it is
        cleared/gone.
      - avoid DANGEROUS enemy ground units (enemy_attack_ground), like build units: a
        rubble target in the danger zone is skipped; if the bulldozer itself stands in
        danger, it retreats to a safe nearby field.
      - if the bulldozer stands ON rubble -> send clear (clears 1 turn for small /
        4 turns for big). Do NOT interrupt a running clear (isClearing).
    hm: precomputed heatmap (from _run_unit_modes). If missing, it is fetched here -
    the enemy avoidance needs the category-separated attack map.
    """
    sid_dozer = gs.vehicle_sid_by_name("bulldozer")
    if sid_dozer is None:
        return gs
    dozers = [v for v in gs.my_vehicles() if gs.unit_type(v) == sid_dozer]
    if not dozers:
        return gs

    # do NOT move freshly built bulldozers this turn (OOS protection, as
    # everywhere): they only exist on host/bridge from the finishBuild tick.
    live_ids = {d["id"] for d in dozers}
    new_dozers = live_ids - _KNOWN_BULLDOZERS
    if new_dozers:
        _FRESH_UNITS.update(new_dozers)
        for nid in new_dozers:
            log(f"  Bulldozer {nid} frisch gebaut - bleibt diese Runde stehen.")
    # clean up memory: keep only living bulldozers.
    for dead in [i for i in _BULLDOZER_TARGET if i not in live_ids]:
        del _BULLDOZER_TARGET[dead]

    # obtain the heatmap for the enemy avoidance, if not passed in.
    if hm is None:
        er = conn.query({"query": "enemyRangeMaps"})
        if not er or er.get("result") != "enemyRangeMaps":
            er = None
        hm = heat_map_calc.compute_heatmaps(gs, enemy_ranges=er)

    # current rubble situation (positions) - once per call.
    rubble = gs.rubble_fields()
    rubble_by_pos = {r["pos"]: r for r in rubble}

    for d in dozers:
        did = d["id"]
        dpos = gs.pos(d)
        # 1. is the clearing already running? -> do NOT touch (do not abort the clear).
        if d.get("isClearing") or (d.get("clearingTurns", 0) or 0) > 0:
            log(f"  Bulldozer {did} @ {dpos} raeumt gerade "
                f"({d.get('clearingTurns', 0)} Runden).")
            continue
        # 2. moving / has a (waiting) move job -> leave it this turn.
        if d.get("moving") or d.get("jobActive") or gs.has_movejob(did):
            log(f"  Bulldozer {did} @ {dpos} faehrt bereits.")
            continue
        # 3. freshly built -> from the next turn.
        if did in _FRESH_UNITS:
            log(f"  Bulldozer {did} @ {dpos} frisch gebaut - faehrt ab naechstem Zug.")
            continue
        # 4. own danger: if the bulldozer stands within range of a dangerous GROUND
        #    weapon -> retreat (the bulldozer is a ground unit).
        cat = unit_modes.unit_target_category(gs, d)
        if unit_modes.in_enemy_attack_for(hm, dpos[0], dpos[1], cat):
            cands = unit_modes.reachable_fields(gs, conn, d, _bulldozer_pc,
                                                hm=hm, avoid_mode="attack_ground")
            safe = heat_map_calc.select_safe_target(hm, cands,
                                                    avoid_mode="attack_ground")
            if safe is not None and tuple(safe) != tuple(dpos):
                ok, _ = conn.do({"type": "move", "unitId": did, "target": list(safe)})
                log(f"  Bulldozer {did} @ {dpos} in Gefahr -> Rueckzug {safe} "
                    f"({'ok' if ok else 'abgelehnt'}).")
                gs = conn.refresh_state() or gs
            else:
                log(f"  Bulldozer {did} @ {dpos} in Gefahr, kein sicheres Feld - bleibt.")
            continue
        # 5. if the bulldozer stands ON rubble -> clear.
        if tuple(dpos) in rubble_by_pos:
            ok, reason = conn.do({"type": "clear", "unitId": did})
            if ok:
                log(f"  Bulldozer {did} @ {dpos} raeumt Schrott (clear).")
                _BULLDOZER_TARGET.pop(did, None)
                gs = conn.refresh_state() or gs
            else:
                log(f"  Bulldozer {did} @ {dpos} clear abgelehnt ({reason}).")
            continue
        # 6. otherwise: choose the nearest SAFE rubble target. Prefer the held
        #    target (no flutter) as long as it exists and is safe.
        held = _BULLDOZER_TARGET.get(did)
        target = None
        if (held is not None and held in rubble_by_pos
                and not unit_modes.in_enemy_attack_for(
                    hm, held[0], held[1], unit_modes.TARGET_GROUND)):
            target = held
        if target is None:
            safe_rubble = [r["pos"] for r in rubble
                           if not unit_modes.in_enemy_attack_for(
                               hm, r["pos"][0], r["pos"][1],
                               unit_modes.TARGET_GROUND)]
            if not safe_rubble:
                log(f"  Bulldozer {did} @ {dpos}: kein sicheres Schrottziel.")
                continue
            target = min(safe_rubble,
                         key=lambda p: (p[0] - dpos[0]) ** 2 + (p[1] - dpos[1]) ** 2)
            _BULLDOZER_TARGET[did] = target
        if tuple(dpos) == tuple(target):
            continue
        ok, reason = conn.do({"type": "move", "unitId": did, "target": list(target)})
        log(f"  Bulldozer {did} @ {dpos} -> Schrott {target} "
            f"({'ok' if ok else reason})")
        if ok:
            gs = conn.refresh_state() or gs
    return gs


# ---------------------------------------------------------------------------
# Main decision per turn
# ---------------------------------------------------------------------------
def speed_left(gs, unit):
    return (unit or {}).get("data", {}).get("speedCur", 0) or 0


def is_idle(gs, unit):
    """True if 'unit' is CURRENTLY doing nothing AND is still able to act this turn
    (unspent movement points). Basis for the 'clear stuck unit' cleanup: units that
    have fallen out of their assignment stand around idle - these can be recognised
    at turn start and re-planned.

    Only flags are checked that the engine serialises per vehicle (vehicle.h /
    unit.h NVP) and that arrive in the state:
      - isBuilding         currently building a building (multi-turn)
      - bandPosition       running PATH build (the engine builds the stretch itself)
      - moving             driving NOW (a waiting MoveJob does NOT count as moving!)
      - isClearing         clearing rubble
      - layMines/clearMines lays/clears mines
      - attacking          executing an attack
    DELIBERATELY inactive states are EXCLUDED (they are not "stuck"):
      - sentryActive            sentry mode (deliberately holds position, fires)
      - surveyorAutoMoveActive  surveyor auto-exploration continues conceptually
    Additional condition: speedCur > 0 - a unit without movement points is idle but
    can do nothing more this turn (no sensible re-order).

    Note 'moving': is only true during the actual movement; a unit with a planned,
    not-yet-executed MoveJob looks idle here."""
    if speed_left(gs, unit) <= 0:
        return False
    if unit.get("sentryActive") or unit.get("surveyorAutoMoveActive"):
        return False   # deliberately inactive -> do NOT treat as stuck
    return (not unit.get("isBuilding")
            and not unit.get("bandPosition")
            and not unit.get("moving")
            and not unit.get("isClearing")
            and not unit.get("layMines")
            and not unit.get("clearMines")
            and not unit.get("attacking"))


def release_idle_pioneers(gs):
    """CLEAR STUCK UNIT (pioneers only): at TURN END (phase 8) check every pioneer
    that is CURRENTLY idle (is_idle: no activity, movement points free, no
    sentry/AutoMove), still holds a _UNIT_ALLOC binding AND was NOT touched this turn
    (_TOUCHED_THIS_TURN). A pioneer whose task processed it this turn (build/load/
    drive/legitimate waiting for material) IS working - it only looks idle at turn
    end but is not. Only one that was NEVER touched the WHOLE turn has really fallen
    out of its assignment and is released from the allocation, so that it is free
    again next turn. Returns the number released."""
    released = 0
    for v in gs.vehicles_of_type("engineer"):
        vid = v["id"]
        if vid in _UNIT_ALLOC and vid not in _TOUCHED_THIS_TURN and is_idle(gs, v):
            a = _UNIT_ALLOC.get(vid)
            alloc_release(vid)
            released += 1
            log(f"  [StuckClear] Pionier {vid} idle (unberuehrt), alloc="
                f"{a.get('task') if a else None}/started={a.get('started') if a else None}"
                f" -> aus Allokation geloest (wieder frei).")
    return released


def release_idle_constructors(gs):
    """CLEAR STUCK UNIT (constructors): at TURN END (phase 8) check every constructor
    that is idle (is_idle), was NOT touched this turn (_TOUCHED_THIS_TURN) AND still
    holds a BACKLOG binding (it is the builder of an open mine component). Unlike
    pioneers, constructors keep NO _UNIT_ALLOC - their binding is mine_comp.builder +
    _CONSTRUCTOR_MINE_POS. A constructor that stands idle at a finished but
    unconnected island and did nothing this turn has fallen out of its assignment:
    release the binding (builder=None + clear target memory), so that the mine
    component is reassigned next turn and the constructor is free. Returns the number
    released.

    Touch protection as with the pioneer: one that was processed this turn (build/
    load/drive/legitimate waiting) is working and stays bound."""
    released = 0
    idle_ids = {v["id"] for v in gs.vehicles_of_type("constructor")
                if v["id"] not in _TOUCHED_THIS_TURN and is_idle(gs, v)}
    if not idle_ids:
        return 0
    try:
        sites = list(_BACKLOG.sorted_open())
    except Exception:
        sites = []
    for site in sites:
        for comp in site.components:
            if (comp.kind == COMP_MINE and comp.builder in idle_ids
                    and not comp.is_done()):
                cid = comp.builder
                comp.builder = None
                comp.state = SiteComponent.S_WISHED
                _CONSTRUCTOR_MINE_POS.pop(cid, None)
                released += 1
                log(f"  [StuckClear] Konstrukteur {cid} idle (unberuehrt) an Site "
                    f"{site.anchor} -> Backlog-Bindung geloest (wieder frei).")
                idle_ids.discard(cid)
    return released


def finish_all_builders(gs, conn, blocked, targets, metal_budget):
    """MODE-INDEPENDENT: gets EVERY finished-built unit off the field (isBuilding &
    buildTurns==0). This is a duty in every mode (even if no emergency exists any
    more and no new tasks are found) - a finished unit must not stick to its build
    site. Repeated until no finished unit remains or no more progress is possible.
    Returns (gs, any) (any=True if at least one unit was moved)."""
    moved_any = False
    for _ in range(20):
        finished = [v for role in ("engineer", "constructor")
                    for v in gs.vehicles_of_type(role)
                    if v.get("isBuilding") and (v.get("buildTurns", 0) or 0) == 0]
        if not finished:
            break
        progress = False
        for v in finished:
            bt = v.get("buildingTyp", {})
            sid = bt.get("secondPart") if isinstance(bt, dict) else None
            res, newgs = run_builder(gs, conn, v, ("(fertig)", sid), metal_budget,
                                     blocked=blocked.setdefault(v["id"], set()),
                                     targets=targets)
            if newgs is not None:
                gs = newgs
            if res:
                progress = True
                moved_any = True
        if not progress:
            break
    return gs, moved_any


def plan_emergency(gs, conn, plan, claim):
    """EMERGENCY BUILD PLANNING. The mode priority says: emergency active -> energy/
    storage/factory have priority. This function sees the state and fixes COMPLETE
    orders for the available builders (builder, building, build site, ore budget) and
    writes them into the plan. It does NOT BUILD - that is done by the build phase
    (execute_plan).

    Ore distribution by priority (core of the reservation): the ore available in the
    network is distributed sequentially over the orders - the most important builder
    first (factory constructor that needs the expensive ore for the heavy factory),
    until it has its FULL budget; only then does the next one get some. If the ore is
    not enough, lower-ranked ones get budget 0 (they wait) until the higher-ranked one
    is satisfied.

    claim: units that the emergency claims (expansion avoids them afterwards)."""
    metal_avail = emergency_metal_pool(gs)
    log(f"  [Plan] NOTFALL. Erz im Netz: {metal_avail}")

    sid_sf = gs.building_sid_by_name("smallfactory")
    sid_bf = gs.building_sid_by_name("bigfactory")

    # look-ahead accumulator: counts what is already PLANNED/UNDER CONSTRUCTION, so
    # that next_task_for_role does not assign the same project twice (e.g. a second
    # heavy factory while the first is still under construction).
    acc = {"storage_metal": 0, "storage_oil": 0, "generators": 0,
           "stations": 0, "radar": 0, "smallfactory": 0, "bigfactory": 0}

    def _count_into_acc(sid):
        """assigns a sid to the acc entry (for already planned/running ones)."""
        name = gs._static_by_sid.get((1, sid), {}).get("name", "")
        if name == "storage-metal":   acc["storage_metal"] += 1
        elif name == "storage-oil":   acc["storage_oil"] += 1
        elif name == "energy small":  acc["generators"] += 1
        elif name == "Energy_Big":    acc["stations"] += 1
        elif name == "radar":         acc["radar"] += 1
        elif name == "smallfactory":  acc["smallfactory"] += 1
        elif name == "bigfactory":    acc["bigfactory"] += 1

    # 1. KEEP EXISTING plan assignments (across turns!). A builder with a valid
    #    order keeps its build site - no recomputing, no wandering. Count these
    #    projects into the acc. IMPORTANT: add the builder to the claim - otherwise
    #    it counts as free for the (lower-ranked) expansion and is snatched away from
    #    it in the middle of its emergency task (otherwise builders constantly switch
    #    tasks and finish none). An assigned builder STAYS at its build site until it
    #    is finished/impossible.
    avoid = set()
    assigned_builders = set()
    for t in plan.all_tasks():
        avoid |= gs.footprint(t.field, gs.is_big_building_type(t.sid))
        assigned_builders.add(t.builder)
        claim.add(t.builder)   # binding against the expansion
        _count_into_acc(t.sid)

    # 2. buildings UNDER CONSTRUCTION (isBuilding builders) also into the acc -
    #    otherwise e.g. a second factory is planned because the first is not yet
    #    'finished' (factory_available False).
    for role in ("constructor", "engineer"):
        for v in gs.vehicles_of_type(role):
            if v.get("isBuilding"):
                bt = v.get("buildingType") or v.get("buildingTyp")
                bsid = None
                if isinstance(bt, dict):
                    bsid = bt.get("secondPart")
                if bsid is not None:
                    _count_into_acc(bsid)
                claim.add(v["id"])   # building builder is occupied

    # order of the FREE builders: factory constructors first (expensive ore), then
    # idle, then under way.
    def _rank(rv):
        role, v = rv
        if role == "constructor":
            sf_open = sid_sf is not None and not gs.factory_available("smallfactory") and acc["smallfactory"] == 0
            bf_open = sid_bf is not None and not gs.factory_available("bigfactory") and acc["bigfactory"] == 0
            if sf_open or bf_open:
                return 0
        if v.get("moving"):
            return 2
        return 1

    # collect candidates. IMPORTANT for the global allocation: a builder firmly
    # bound to a task via _UNIT_ALLOC (platform_chain / net_repair) does NOT count as
    # regularly free for the emergency. The emergency first takes all TRULY free
    # builders; only if NO free one remains for an emergency site does it reach for a
    # bound one as a replacement and then EXPLICITLY recall it (alloc_release).
    # Therefore: free ones first, bound ones at the back.
    free_cands, bound_cands = [], []
    for role in ("constructor", "engineer"):
        for v in gs.vehicles_of_type(role):
            if v.get("isBuilding"):
                continue
            if v["id"] in assigned_builders:
                continue   # already has a plan order -> keep it
            if v["id"] in _UNIT_ALLOC:
                bound_cands.append((role, v))
            else:
                free_cands.append((role, v))
    free_cands.sort(key=_rank)
    bound_cands.sort(key=_rank)
    ordered = free_cands + bound_cands   # bound ones only AFTER all free ones

    remaining_metal = metal_avail   # ore pot, distributed sequentially

    for role, v in ordered:
        bid = v["id"]
        # WHAT should this builder build? (existing decision logic, acc already
        # counts planned/running -> no double assignment)
        t = next_task_for_role(gs, role, acc)
        if not t:
            continue
        name, sid = t
        # WHERE? fix the build site - emergency buildings (station/generator/storage/
        # factory) MUST connect to the MAIN COMPONENT, not to an isolated island
        # (otherwise they stand uselessly in the middle of nowhere). NO unconnected
        # fallback in the emergency.
        build_pos = gs.find_build_position(v, sid, avoid=avoid,
                                           require_connected=True, connect_to_main=True)
        if build_pos is None:
            # no place connected to the main base for this builder -> do not plan
            # this builder this time (another builder standing closer to the main
            # base gets the order).
            continue
        # if this builder is globally bound (replacement), the emergency now recalls
        # it EXPLICITLY - it is really needed for this emergency site.
        _al = _UNIT_ALLOC.get(bid)
        if _al is not None:
            alloc_release(bid)
            log(f"  [Plan] NOTFALL beruft Bauer {bid} aus '{_al.get('task')}' ab "
                f"(kein freier Bauer fuer {name}).")
        # create the order - the BUDGET is NOT yet assigned here (0), but centrally
        # distributed right after over ALL orders (existing + new) in rank order.
        # This way an existing order too (e.g. the constructor for the heavy factory)
        # gets fresh ore every turn - otherwise its budget freezes at the initial
        # value (often 0) and it never collects (problem B).
        plan.assign(build_pos, sid, bid, metal_budget=0, name=name)
        acc_add(acc, name)
        avoid |= gs.footprint(build_pos, gs.is_big_building_type(sid))
        claim.add(bid)
        log(f"  [Plan] {name} -> Bauer {bid} @ {build_pos} (Auftrag angelegt).")

    # === CENTRAL BUDGET DISTRIBUTION (problem B + pioneer lock) ==============
    # The ONE planner distributes the available base ore ANEW every turn over ALL
    # orders (existing + new) in the RANK ORDER that the emergency mode dictates.
    # Core (not hardcoded): the highest-ranked OPEN order first gets its fully needed
    # ore, only the rest goes to lower-ranked ones. If the constructor for the heavy
    # factory sucks up everything, 0 remains for pioneers -> they wait (the pioneer
    # lock arises automatically, is no special mechanism).
    #
    # RANK of an order (smaller = more important):
    #   0  factory constructor still collecting (factory open) - small before big,
    #      because the emergency mode demands the light factory first; once it is
    #      under construction (factory_available True), the heavy one is the
    #      highest-ranked rest.
    #   1+ all others by PRIORITY (storage/energy/radar) - relative to each other.
    # The DYNAMIC demand (e.g. 60 for the heavy factory at max speed) comes from
    # factory_reserve_cost (buildSpeeds query), NOT hardcoded. For non-factories
    # build_cost suffices.
    def _task_rank(t):
        nm = t.name or ""
        # ENERGY FIRST: in an (energy) emergency the power station / generator must
        # get ore BEFORE the factories - otherwise the expensive heavy factory (32+)
        # sucks up the scarce ore and the cheap power station (8) never crosses the
        # threshold, the energy emergency never ends.
        if nm in ("station", "energy small"):
            return 0
        if nm in ("smallfactory", "bigfactory") and not gs.factory_available(nm):
            return 1 if nm == "smallfactory" else 2   # factories after, small<big
        return 3 + PRIORITY.get(nm, 99)               # rest behind, by PRIORITY

    def _task_need(t):
        v = refresh_unit(gs, t.builder)
        already = gs.stored(v) if v is not None else 0
        # factories: dynamic max-speed demand (buildSpeeds), otherwise x1 build cost.
        cost = None
        if t.name in ("smallfactory", "bigfactory"):
            cost = factory_reserve_cost(conn, t.builder, t.sid)
        if cost is None:
            cost = gs.build_cost(t.sid) or 0
        return max(0, cost - already), cost

    # do NOT budget chain builds (platforms) here: a builder with a CHAIN (more than
    # one task) loads its material itself via chain_metal_needed and transfer
    # (_build_platform_chain). If the emergency budget loop counted them, it would
    # burden the ore pot multiple times per chain link and set_budget would only hit
    # the first link anyway. Only emergency SINGLE orders (exactly one task per
    # builder) get a budget here.
    builder_task_count = {}
    for t in plan.all_tasks():
        builder_task_count[t.builder] = builder_task_count.get(t.builder, 0) + 1

    single_tasks = [t for t in plan.all_tasks() if builder_task_count.get(t.builder, 0) == 1]
    ordered_tasks = sorted(single_tasks, key=_task_rank)
    for t in ordered_tasks:
        need, cost = _task_need(t)
        grant = min(need, remaining_metal)
        remaining_metal -= grant
        plan.set_budget(t.builder, grant)
        log(f"  [Budget] {t.name} -> Bauer {t.builder} "
            f"Erz-Budget {grant}/{cost} (Rest im Topf {remaining_metal}).")

    return gs


def acc_add(acc, name):
    """Advance the look-ahead accumulator (like plan_add)."""
    if name == "storage-metal":   acc["storage_metal"] += 1
    elif name == "storage-oil":   acc["storage_oil"] += 1
    elif name == "energy small":  acc["generators"] += 1
    elif name == "station":       acc["stations"] = acc.get("stations", 0) + 1
    elif name == "radar":         acc["radar"] += 1
    elif name == "smallfactory":  acc["smallfactory"] += 1
    elif name == "bigfactory":    acc["bigfactory"] += 1


def execute_plan(gs, conn, plan):
    """BUILD PHASE: executes the plan's orders. Each builder loads its assigned ore
    budget, drives to the field, builds. Get finished builders off the field
    beforehand. If the bridge client rejects ('does not work'), the order is released
    - the next planning phase corrects that (new place/assignment). Returns gs."""
    # 1. get finished builders off the field (mandatory, else the turn is not endable)
    gs = _finish_finished_builders(gs, conn, plan)
    # 2. execute orders
    for t in list(plan.all_tasks()):
        v = refresh_unit(gs, t.builder)
        if v is None:
            plan.release(t.builder)
            continue
        if v.get("isBuilding"):
            continue   # already building (this or a previous order is running)
        gs = _execute_one(gs, conn, v, t, plan)
    return gs


def _finish_finished_builders(gs, conn, plan=None):
    """Gets finished-built builders (isBuilding & buildTurns==0) off the field via
    finishBuild and reports their order as done. Duty in EVERY turn (mode-
    independent): as long as a finished builder sticks to its field, the turn is not
    cleanly endable and the builder is free for nothing new. plan optional."""
    for _ in range(20):
        finished = [v for role in ("engineer", "constructor")
                    for v in gs.vehicles_of_type(role)
                    if v.get("isBuilding") and (v.get("buildTurns", 0) or 0) == 0]
        if not finished:
            break
        progress = False
        for v in finished:
            vpos = gs.pos(v)
            done = False
            for esc in gs.escape_candidates(vpos, unit=v, toward=base_center(gs)):
                ok, _ = conn.do({"type": "finishBuild", "unitId": v["id"],
                                 "escapePosition": list(esc)})
                if ok:
                    g2 = conn.refresh_state()
                    nb = refresh_unit(g2, v["id"]) if g2 else None
                    if g2 is not None and nb is not None and not nb.get("isBuilding"):
                        gs = g2
                        if plan is not None:
                            plan.mark_done(v["id"])
                        progress = True
                        done = True
                        break
                    if g2 is not None:
                        gs = g2
            if not done:
                log(f"  finishBuild: {v['id']} @{vpos} fertig, kommt nicht vom Feld "
                    f"(Kandidaten erschoepft) - naechster Zug.")
        if not progress:
            break
    return gs


def _execute_one(gs, conn, builder, task, plan):
    """Executes ONE plan order: load ore up to the budget, to the field, build."""
    bid = builder["id"]
    sid = task.sid
    spot = task.field
    name = task.name or str(sid)
    cost = gs.build_cost(sid) or 0
    is_big = gs.is_big_building_type(sid)

    # already built? -> done
    if _object_present(gs, task.field, sid):
        plan.mark_done(bid)
        return gs

    # 1. load ore up to the build cost - but only up to the assigned budget.
    stored = gs.stored(builder)
    if stored < cost:
        task.state = task.S_WAITING   # waiting/loading - NOT under way
        task.last_path_cost = None    # reset the path-cost history
        task.no_progress_turns = 0
        budget = task.metal_budget
        if budget <= 0:
            log(f"  Bauphase: {name} (Bauer {bid}) wartet (Erz reserviert fuer Vorrang).")
            return gs
        anchor = adjacent_networked_building(gs, builder, need_metal=True)
        if anchor is None:
            dock = dock_field_at_base(gs, builder)
            if dock is not None and gs.pos(builder) != dock:
                ok_mv, _ = conn.do({"type": "move", "unitId": bid, "target": list(dock)})
                if ok_mv:
                    gs, builder = _reget(gs, conn, bid)
                    if builder is None:
                        return gs
            return gs
        want = min(cost - stored, budget, ore_available_for(gs, bid))
        if want > 0:
            ok, _ = conn.do({"type": "transfer", "unitId": anchor["id"], "targetId": bid,
                             "amount": want, "resource": "metal"})
            if ok:
                gs, builder = _reget(gs, conn, bid)
                if builder is None:
                    return gs
                stored = gs.stored(builder)
                log(f"  Bauphase: {name} (Bauer {bid}) nachgeladen, jetzt {stored} Erz.")
        if stored < cost:
            return gs   # continue loading next turn

    # 2. to the build site.
    bpos = gs.pos(builder)

    # a build site is NOT discarded just because an OWN MOBILE unit is in the way -
    # it moves aside (or next turn, if it has no movement points). It is only
    # discarded if the target is blocked by REAL blockers (terrain, fixed buildings,
    # foreign units). Measure: if the field is buildable WITHOUT own mobile units
    # (occupied_fields_for_mine), it is in principle reachable. NO progress/stuck
    # abort here: a bound builder stays at its build site (platform chains in
    # particular stand several turns at the same point - that is normal, not being
    # stuck).
    task.state = task.S_EN_ROUTE
    if bpos != spot and (not is_big or bpos not in gs.footprint(spot, True)):
        target_cells = gs.footprint(spot, is_big)
        buildable_wo_own = all(
            gs.is_buildable_for_building(sid, cx, cy,
                                         occ=gs.occupied_fields_for_mine(),
                                         ignore={bpos})
            for (cx, cy) in target_cells)
        if not buildable_wo_own:
            pc = path_cost_to(conn, bid, spot)
            if pc is not None and not pc.get("reachable", True):
                # REALLY unreachable (not just own mobile units) -> discard.
                log(f"  Bauphase: {name} (Bauer {bid}) Ziel {spot} echt unerreichbar "
                    f"(Terrain/feste Blocker) -> Auftrag frei.")
                plan.release(bid)
                return gs
        # does only an own mobile unit stand on the build site? Then send it away
        # now (it moves aside; if it has no movement points, it works next turn). The
        # build site stays - we discard nothing.
        if buildable_wo_own:
            gs, _cl = clear_units_from_fields(gs, conn, target_cells, except_id=bid)
            gs, builder = _reget(gs, conn, bid)
            if builder is None:
                return gs
            bpos = gs.pos(builder)
    if is_big:
        cells = gs.footprint(spot, True)
        next_to = any(abs(bpos[0]-cx) <= 1 and abs(bpos[1]-cy) <= 1 for cx, cy in cells)
        if not next_to and bpos not in cells:
            approach = None
            for (cx, cy) in cells:
                for n in gs.neighbors8(cx, cy):
                    if n not in cells and gs.is_free_for_unit(builder, n[0], n[1], ignore={bpos}):
                        approach = n
                        break
                if approach:
                    break
            if approach is not None:
                ok_mv, _ = conn.do({"type": "move", "unitId": bid, "target": list(approach)})
                if ok_mv:
                    gs, builder = _reget(gs, conn, bid)
                    if builder is None:
                        return gs
                    if gs.pos(builder) != approach:
                        record_build_move(conn, gs, builder, approach)
                        return conn.refresh_state() or gs
            else:
                return gs
        gs, _cl = clear_units_from_fields(gs, conn, cells, except_id=bid)
        gs, builder = _reget(gs, conn, bid)
        if builder is None:
            return gs
    else:
        if bpos != spot:
            ok_mv, _ = conn.do({"type": "move", "unitId": bid, "target": list(spot)})
            if ok_mv:
                gs, builder = _reget(gs, conn, bid)
                if builder is None:
                    return gs
                if gs.pos(builder) != spot:
                    record_build_move(conn, gs, builder, spot)
                    return conn.refresh_state() or gs
            else:
                # drive rejected -> release the order, the next planning corrects it
                log(f"  Bauphase: {name} Fahrt zu {spot} abgelehnt -> Auftrag frei.")
                plan.release(bid)
                return gs

    # 3. build.
    ok, reason = conn.do({"type": "startBuild", "unitId": bid,
                          "buildingId": [1, sid], "speed": -1, "position": list(spot)})
    if ok:
        task.state = task.S_BUILDING
        task.no_progress_turns = 0
        log(f"  Bauphase: {name} Bau gestartet auf {spot}.")
        return conn.refresh_state() or gs
    r = (reason or "").lower()
    if "moving" in r:
        return gs
    # 'does not work' -> release the order; the next planning phase chooses anew.
    log(f"  Bauphase: {name} Bau abgelehnt ({reason}) auf {spot} -> Auftrag frei.")
    plan.release(bid)
    return gs


def _object_present(gs, field, sid):
    """Does an own building of type sid stand on the field (resp. cover it)? Counts
    ALSO a building currently UNDER CONSTRUCTION: a big building (e.g. bigfactory)
    does NOT yet exist in my_buildings() during its multi-turn build time, but the
    building constructor stands on the build area and carries the target sid in
    buildingTyp.secondPart. Without this detection the associated _PLAN order would
    stay 'open' for the whole build time -> the emergency would re-plan the same
    building turn after turn and never end (verified in the log 2026-06-15: bigfactory
    builder 13 re-planned over 20 turns). isBuilding + buildingTyp are present in the
    state (finish_all_builders uses them too)."""
    f = tuple(field)
    for b in gs.my_buildings():
        if gs.unit_type(b) != sid:
            continue
        big = gs.is_big_building_type(sid)
        if f in gs.footprint(gs.pos(b), big) or gs.pos(b) == f:
            return True
    # under construction: an own builder is building this sid and covers the field.
    for role in ("constructor", "engineer"):
        for v in gs.vehicles_of_type(role):
            if not v.get("isBuilding"):
                continue
            bt = v.get("buildingTyp") or v.get("buildingType")
            bsid = bt.get("secondPart") if isinstance(bt, dict) else None
            if bsid != sid:
                continue
            big = gs.is_big_building_type(sid)
            if f in gs.footprint(gs.pos(v), big) or gs.pos(v) == f:
                return True
    return False


def mode_emergency(gs, conn, blocked, targets, claim=None):
    """MODE HANDLER 'emergency' (top priority). Runs the emergency/start build-up:
    storage, energy, factories - with ore reservation (whitelisting) and sequential
    processing. Called by the phase orchestrator when an emergency exists. Returns the
    (fresh) gs.
    claim: SHARED occupation note (set of unit IDs). Every builder the emergency uses
    is entered, so that the following expansion does not also plan it (coexistence:
    the emergency takes only what it needs, the rest expands). Precondition: the
    caller has already checked is_emergency()."""
    if claim is None:
        claim = set()
    metal_budget = {"base": emergency_metal_pool(gs)}
    log(f"  Erz im Netz verfuegbar: {metal_budget['base']}")

    # --- ore reservation for factories (whitelisting) -------------------------
    # Rule: if there are at least as many constructors as open factory projects, the
    # ore is reserved for the factories, so that the expensive heavy factory does not
    # starve. Sequential: first the light factory (its constructor may load as the
    # ONLY one), once it has enough ore for the best possible level, the heavy-factory
    # constructor joins, until it too has enough. After that whitelisting off.
    # 'reserve' holds the needed ore amount per factory.
    constructors = gs.vehicles_of_type("constructor")
    sid_sf = gs.building_sid_by_name("smallfactory")
    sid_bf = gs.building_sid_by_name("bigfactory")
    open_factories = []
    if sid_sf is not None and not gs.factory_available("smallfactory"):
        open_factories.append(("smallfactory", sid_sf))
    if sid_bf is not None and not gs.factory_available("bigfactory"):
        open_factories.append(("bigfactory", sid_bf))
    whitelist_active = len(constructors) >= len(open_factories) and len(open_factories) > 0

    # determine the reservation demand per factory (best possible level) once.
    reserve_cost = {}
    if whitelist_active:
        for fname, fsid in open_factories:
            # take one constructor as a reference for the price list
            ref = constructors[0]["id"]
            c = factory_reserve_cost(conn, ref, fsid)
            if c:
                reserve_cost[fname] = c
        log(f"  Erz-Reservierung aktiv fuer Fabriken: {reserve_cost}")

    # 3. STRICTLY SEQUENTIAL (MAXR is not Diplomacy): process exactly ONE unit
    #    completely, then fetch a FRESH state, then the next. This way a second unit
    #    can never move onto the field just occupied by a first. We iterate in passes
    #    over all builders and handle one action per unit per pass; we repeat as long
    #    as progress is still possible. IMPORTANT: as long as a finished-built unit
    #    still stands on its field (isBuilding & buildTurns==0), the turn is NOT over
    #    - it still has movement points and must first get off.
    # blocked/targets come from the orchestrator (persist across phase 3+5).
    MAX_STEPS = 60
    for _ in range(MAX_STEPS):
        progress = False

        # plan fresh per pass (look-ahead distribution of several builders).
        plan = {"storage_metal": 0, "storage_oil": 0, "generators": 0,
                "stations": 0, "radar": 0, "smallfactory": 0, "bigfactory": 0}

        # 3a. first: get every finished-built unit off the field (mandatory,
        #     mode-independent - same function as in the non-emergency path).
        gs, moved = finish_all_builders(gs, conn, blocked, targets, metal_budget)
        if moved:
            progress = True

        # 3b. assign/execute new build orders SEQUENTIALLY. The ORDER of the units is
        #     decisive (scarce ore goes to the most important first):
        #       (a) FACTORY constructors still collecting ore  -> very first
        #           (critical for ending the emergency; partial loading is ok, but
        #            this builder must reach the scarce ore BEFORE all others),
        #       (b) otherwise idle  -> then moving  -> lastly building.
        #     This way no engineer/storage build can take the ore from the factory
        #     constructor, even if the whitelisting is currently lifted.
        sid_sf2 = gs.building_sid_by_name("smallfactory")
        sid_bf2 = gs.building_sid_by_name("bigfactory")

        def _is_factory_constructor_loading(role, v):
            if role != "constructor":
                return False
            bt = v.get("buildingTyp", {})
            # not yet building and is a constructor that should build a factory: we
            # recognise this by the fact that a factory is still open at all.
            if v.get("isBuilding"):
                return False
            sf_open = sid_sf2 is not None and not gs.factory_available("smallfactory")
            bf_open = sid_bf2 is not None and not gs.factory_available("bigfactory")
            return sf_open or bf_open

        def _state_rank(rv):
            role, v = rv
            if _is_factory_constructor_loading(role, v):
                return 0                          # factory constructor -> very first
            if v.get("isBuilding"):   return 3    # building -> last
            if v.get("moving"):       return 2    # under way
            return 1                              # other idle

        ordered = []
        for role in ("engineer", "constructor"):
            for v in gs.vehicles_of_type(role):
                ordered.append((role, v))
        ordered.sort(key=_state_rank)

        # determine the whitelisting state for THIS pass: which factories are
        # 'saturated' (enough ore for the best possible level OR already under
        # construction/finished)? Sequential release: light first, then heavy.
        def _factory_saturated(fname, fsid):
            if gs.factory_available(fname):   # already under construction/finished -> done
                return True
            need = reserve_cost.get(fname)
            if not need:
                return True   # no price info -> do not block
            # a constructor that has loaded enough ore for this factory?
            return any(gs.stored(c) >= need for c in gs.vehicles_of_type("constructor"))

        sf_sat = _factory_saturated("smallfactory", sid_sf) if sid_sf is not None else True
        bf_sat = _factory_saturated("bigfactory", sid_bf) if sid_bf is not None else True
        # whitelisting lifts as soon as both factories are saturated.
        wl_on = whitelist_active and not (sf_sat and bf_sat)

        def _may_reload(role, v, task_name):
            if not wl_on:
                return True   # no reservation -> free
            # only the respectively unlocked factory constructor may load.
            if task_name == "smallfactory":
                return True                      # light factory always (phase 1)
            if task_name == "bigfactory":
                return sf_sat                    # heavy only when light is satisfied
            # all other builders: locked as long as the reservation runs
            return False

        for role, v in ordered:
            if v.get("isBuilding"):
                continue
            if speed_left(gs, v) <= 0:
                continue
            t = next_task_for_role(gs, role, plan)
            if not t:
                continue
            claim.add(v["id"])     # claimed by the emergency -> expansion avoids it
            plan_add(plan, t[0])   # project counts as assigned (distribution)
            res, newgs = run_builder(gs, conn, v, t, metal_budget,
                                     blocked=blocked.setdefault(v["id"], set()),
                                     targets=targets,
                                     may_reload=_may_reload(role, v, t[0]))
            if newgs is not None:
                gs = newgs
            if res:
                progress = True

        # abort only if NOTHING works any more AND no finished unit still sticks to
        # its field (otherwise the turn must not end at all).
        stuck_finished = any(
            v.get("isBuilding") and (v.get("buildTurns", 0) or 0) == 0
            for role, v in [(r, x) for r in ("engineer", "constructor")
                            for x in gs.vehicles_of_type(r)])
        if not progress and not stuck_finished:
            break
    return gs


def _free_units(gs, role, claim, for_task=None):
    """Builders of a role available for the task 'for_task'. Excludes: those claimed
    by the emergency (claim), currently building (isBuilding), and those bound to
    ANOTHER ACTUAL task (_UNIT_ALLOC with started -> concretely building another
    stretch/chain).

    IMPORTANT (core task != actual task): the CORE TASK (_CORE_TASK) is only a POOL
    assignment, it does NOT lock a pioneer. A pioneer is locked only if it is assigned
    to an ACTUAL task (_UNIT_ALLOC started=True -> it is concretely building). A
    pioneer that only has "core task platform" but is currently building NO platform
    chain is NOT busy and may be lent out - otherwise a lent-out pioneer sticks
    permanently in a pool without work and can never leave it. The core task acts only
    as a PREFERENCE in the selection order (see _pick_free_for), not as a hard lock
    here. for_task is the caller's _UNIT_ALLOC task (a unit bound exactly for it still
    counts as free)."""
    def _bound_to_real_other_task(uid):
        a = _UNIT_ALLOC.get(uid)
        # only STARTED (actually running) build work on ANOTHER task locks. A
        # not-started reservation does not lock.
        return (a is not None and a.get("started")
                and a.get("task") != for_task)
    return [v for v in gs.vehicles_of_type(role)
            if v["id"] not in claim and not v.get("isBuilding")
            and not _bound_to_real_other_task(v["id"])]


def _pick_free_for(gs, role, claim, for_task):
    """Like _free_units, but sorted by CORE-TASK PREFERENCE: pioneers whose core task
    == for_task first; then floating ones (no core task); then those that actually
    belong to another pot (stand-in). This way the task preferentially gets its own
    pioneers but can borrow free/idle foreign ones instead of going empty."""
    free = _free_units(gs, role, claim, for_task=for_task)
    def rank(v):
        core = _CORE_TASK.get(v["id"])
        if core == for_task:
            return 0
        if core is None:
            return 1
        return 2
    return sorted(free, key=rank)


def _pioneer_total(gs):
    """Total number of pioneers for the 30% rule: finished + those that will be done
    in <=3 turns (from the factory buildLists). This way an almost-finished pioneer
    already counts and does not distort the calculation."""
    total = len(gs.vehicles_of_type("engineer"))
    pio_sid = gs.special_vehicles.get("engineer")
    for b in gs.my_buildings():
        for i, job in enumerate(b.get("buildList", []) or []):
            jid = job.get("type", {}) if isinstance(job, dict) else {}
            sp = jid.get("secondPart") if isinstance(jid, dict) else None
            if sp == pio_sid:
                # only the frontmost item is in progress; remaining turns roughly
                # from remainingMetal/needsMetal - simplifying: the first item counts
                # if it has <=3 turns. Without an exact remaining time: the first
                # item counts.
                if i == 0:
                    rounds = job.get("remainingTurns")
                    if rounds is None or rounds <= 3:
                        total += 1
    return total


_EXPANSION_REJECTED = set()   # discarded targets (x,y) persisting across turns
_HELD_EXPANSION_GOAL = {}     # held expansion goal {'goal': (x,y)} as long as a
                              # platform chain runs there (against goal wandering)
                              # plus 'platmine': {'goal':(x,y),'pos':[x,y]} - the
                              # fixed 2x2 mine area (persistent across turns,
                              # otherwise it wanders and the platform chain never
                              # ends). Used ONLY by the legacy path
                              # (mode_expansion_legacy); the backlog path binds the
                              # area to site.mine_pos.
_CONSTRUCTOR_MINE_POS = {}    # per constructor ID the fixed mine area:
                              # {cid: {'goal':(x,y), 'mine_pos':[x,y]}}. Persistent
                              # across turns (the per-turn 'targets' dict would forget
                              # it each turn -> the area wanders).
_FIRST_MINE_RESERVE = {"con": None, "pio": None}
                              # EXACTLY ONE constructor (60 ore) + ONE pioneer (8 ore)
                              # as a RESERVE for the first >=7-metal mandatory mine.
                              # Fixed IDs (do not wander). Stay fully loaded + bound
                              # and do NOTHING until a >=7 field is found (then they
                              # build it immediately) or mine 2 stands. All OTHER
                              # builders may build weaker mines meanwhile.
# Fill-ratio history for the metal-storage trigger rule (max. 3 entries).
# Each entry = metal_fill_ratio() at turn start (after preparation).
# If all 3 entries >= 0.80: build a new metal storage.
# Started storages lower the denominator immediately
# (storage_capacity_incl_construction), thus preventing double triggering while the
# first is not yet finished.
_METAL_FILL_HISTORY: list = []   # [float, ...] last seen, oldest first
# Energy load of the last preparation phase (single float, no ring).
# = energy_potential_need() / energy_production_incl_construction()
# Basis for the 90% station rule in expansion mode.
_ENERGY_LOAD_RATIO: float = 0.0
_SURVEYORS_AT_TURN_START = set()  # (no longer used; kept as a placeholder)

# ===========================================================================
# GLOBAL UNIT ALLOCATION ARRAY (_UNIT_ALLOC)
# ---------------------------------------------------------------------------
# General persistence concept for the allocation of units to tasks. NOT intended
# only for the PATH network repair - also for future projects (combat-unit groups,
# multi-stage builds, anything that must "stick" to a unit across turns).
#
# Structure:  _UNIT_ALLOC[unit_id] = {
#     "task":    str,      # task tag, e.g. "net_repair", later "attack", ...
#     "started": bool,     # has the unit already BEGUN to work for this allocation?
#                          #   (first build/first action issued)
#     "payload": dict,     # task-specific data (e.g. remaining-field list, target)
# }
#
# INVARIANT (applies to ALL task types):
#   - As long as started=False the allocation may be freely re-planned/lifted.
#   - Once started=True the unit is BOUND: it is NOT removed from the allocation
#     until the task is completed - EXCEPT the EMERGENCY MODE recalls it (it calls
#     alloc_release(uid) and re-orders it). No other path deletes a started
#     allocation.
# ===========================================================================
_UNIT_ALLOC: dict = {}

def alloc_get(uid):
    """Allocation of a unit (or None)."""
    return _UNIT_ALLOC.get(uid)

def alloc_set(uid, task, payload=None, started=False):
    """Creates/updates an allocation. An existing started allocation is NOT
    overwritten, except explicitly (the emergency calls alloc_release first)."""
    _UNIT_ALLOC[uid] = {"task": task, "started": started, "payload": payload or {}}
    return _UNIT_ALLOC[uid]

def alloc_mark_started(uid):
    """Marks: the unit has begun to work for its allocation (bound from now on, only
    the emergency may release it via alloc_release)."""
    a = _UNIT_ALLOC.get(uid)
    if a is not None:
        a["started"] = True

def alloc_release(uid):
    """Releases an allocation. The ONLY legitimate way to remove a started allocation
    is via this function - and only the emergency mode (or the completion of the task
    itself) calls it for a started unit."""
    return _UNIT_ALLOC.pop(uid, None)

def alloc_units_for(task):
    """All unit IDs currently assigned to the given task."""
    return [uid for uid, a in _UNIT_ALLOC.items() if a.get("task") == task]


def _platform_work_open(gs):
    """Is there open platform work NOW? (a water-mine site whose 2x2 is not yet
    completely platformed). Source: the build-site backlog."""
    try:
        for site in _BACKLOG.sorted_open():
            if site.mine_pos is None:
                continue
            plat_comps = [c for c in site.components if c.kind == COMP_PLATFORM]
            if not plat_comps:
                continue
            if gs.platform_fields_needed(site.mine_pos) != []:
                return True   # at least one platform field is still missing
    except Exception:
        pass
    return False


def _empty_plan_acc():
    """Empty plan accumulator (all build counters 0), as next_task_for_role expects
    it. Complete key set (superset for engineer AND constructor), so that every role
    query runs safely."""
    return {"storage_metal": 0, "storage_oil": 0, "generators": 0,
            "stations": 0, "radar": 0, "smallfactory": 0, "bigfactory": 0,
            "storage_gold": 0, "gold_refinery": 0}


def base_expansion_has_work(gs):
    """Does the third pioneer pot (base_expansion) have work? True if EITHER storage
    demand exists OR defense demand (and the metal threshold is met). Order in the
    pot: STORAGE before defense/radar.

    Storage: via the existing engineer task rule (next_task_for_role returns
    storage-metal/-oil if demand exists) - NOT affected by the metal threshold.
    Defense: only if defense_build_allowed(gs) (>=3 mines or income>=30) AND the
    screen has a gap (coverage < cover_target). The gap check is hooked into the
    placement stage (building block 3); until then _defense_shield_has_gap(gs)
    conservatively reports False, so that the behaviour only changes with the built
    placement."""
    # 1. storage demand (exempt from the metal threshold).
    if _storage_need_open(gs):
        return True
    # 2. defense demand behind the metal threshold.
    if defense_build_allowed(gs) and _defense_shield_has_gap(gs):
        return True
    return False


def _storage_need_open(gs):
    """True if a storage (metal/oil) is due by the existing rules. Uses the same logic
    as the engineer task choice, with an empty plan accumulator (pure demand
    question, without builds already assigned this turn)."""
    plan = _empty_plan_acc()
    task = next_task_for_role(gs, "engineer", plan)
    return task is not None and task[0] in ("storage-metal", "storage-oil")


def _defense_shield_has_gap(gs):
    """True if the defense screen has a gap: some zone field (ring 5-8 outside the
    network) is hit by fewer than cover_target group artillery circles. cover_target
    comes from gs.COVER_TARGET (personality)."""
    ct = getattr(gs, "COVER_TARGET", None)
    if not ct:
        return False
    try:
        return defense_planner.has_gap(gs, cover_target=ct)
    except Exception as e:
        log(f"  [Schirm] has_gap-Fehler: {e}")
        return False



def allocate_core_tasks(gs):
    """CORE-TASK ALLOCATION (at turn start). Assigns each pioneer a core task by ID
    (_CORE_TASK): net repair, platform build or base expansion.
    Rules (see config block _CORE_QUOTA):
      - A pioneer with running, STARTED build work (_UNIT_ALLOC started=True) keeps
        its core task MANDATORILY - it is not reassigned.
      - 1/3 of the pool per pot as a MINIMUM reservation, but ONLY if the pot has
        work. A pot without work reserves NOTHING (no mutual blocking).
      - PRIORITY among the core tasks: repair > platform build > base expansion.
        Floating (surplus) free pioneers go preferentially to the repair.
      - Base expansion currently NEVER has work (logic not yet built) -> no pioneers
        are assigned to it at present.
    Rewrites _CORE_TASK. Changes NOTHING in _UNIT_ALLOC (that is the concrete build
    work; the core task is the superordinate assignment)."""
    pios = gs.vehicles_of_type("engineer")
    pio_ids = [v["id"] for v in pios]
    pio_by_id = {v["id"]: v for v in pios}
    # remove dead entries from _CORE_TASK.
    for pid in list(_CORE_TASK.keys()):
        if pid not in pio_ids:
            del _CORE_TASK[pid]
    # SAFETY NET against "idle pioneer permanently locked as platform_chain":
    # A platform_chain allocation is ORPHANED if its pioneer is not (any longer)
    # building (isBuilding False) AND no longer serves an open platform site (it is
    # the builder of no open COMP_PLATFORM component). This happens when the state
    # sync cleared the site. Release such allocations here, so that the pioneer
    # becomes free again (otherwise _free_units locks it forever).
    def _serves_open_platform(pid):
        try:
            for site in _BACKLOG.sorted_open():
                for c in site.components:
                    if (c.kind == COMP_PLATFORM and c.builder == pid
                            and not c.is_done()):
                        return True
        except Exception:
            pass
        return False
    for pid in list(_UNIT_ALLOC.keys()):
        a = _UNIT_ALLOC.get(pid)
        if a is None or a.get("task") != "platform_chain":
            continue
        v = pio_by_id.get(pid)
        if v is None:
            alloc_release(pid)   # pioneer gone
            continue
        if not v.get("isBuilding") and not _serves_open_platform(pid):
            alloc_release(pid)   # orphaned -> release
    if not pio_ids:
        return

    total = len(pio_ids)
    min_per_pot = max(1, int(total * _CORE_QUOTA + 1e-9))

    # determine work per pot.
    repair_work = gs.network_gap_target() is not None
    platform_work = _platform_work_open(gs)
    base_work = base_expansion_has_work(gs)   # storage and/or defense (threshold)

    # 1. STARTED bound pioneers keep their core task (mandatory). We derive their
    #    core task from their running _UNIT_ALLOC build work.
    fixed = {}   # pid -> task (not reassignable)
    for pid in pio_ids:
        a = _UNIT_ALLOC.get(pid)
        if a is not None and a.get("started"):
            t = a.get("task")
            if t in (_CORE_REPAIR, _CORE_PLATFORM, _CORE_BASE):
                fixed[pid] = t
                _CORE_TASK[pid] = t

    free_pids = [p for p in pio_ids if p not in fixed]

    # 2. demand per pot = minimum quota minus already fixed, but only if the pot has
    #    work. Order = priority (repair first).
    def fixed_in(task):
        return sum(1 for t in fixed.values() if t == task)

    need = {}
    need[_CORE_REPAIR] = max(0, min_per_pot - fixed_in(_CORE_REPAIR)) if repair_work else 0
    need[_CORE_PLATFORM] = max(0, min_per_pot - fixed_in(_CORE_PLATFORM)) if platform_work else 0
    need[_CORE_BASE] = max(0, min_per_pot - fixed_in(_CORE_BASE)) if base_work else 0

    # 3. assign free pioneers to the pots - in priority order first cover the
    #    minimum quotas (repair, then platform build, then base).
    assigned = {}
    pool = list(free_pids)
    for task in (_CORE_REPAIR, _CORE_PLATFORM, _CORE_BASE):
        for _ in range(need[task]):
            if not pool:
                break
            pid = pool.pop(0)
            assigned[pid] = task

    # 4. remaining (floating) free pioneers: preferentially to the repair
    #    (priority) as long as there is work there; otherwise platform build (if
    #    work); otherwise they stay WITHOUT a core task (free for "other tasks"/normal
    #    expansion).
    for pid in pool:
        if repair_work:
            assigned[pid] = _CORE_REPAIR
        elif platform_work:
            assigned[pid] = _CORE_PLATFORM
        else:
            _CORE_TASK.pop(pid, None)   # no core task -> floats free
            continue
    for pid, task in assigned.items():
        _CORE_TASK[pid] = task



def _build_platform_chain(gs, conn, pio):
    """Works off a pioneer's platform CHAIN from the build plan. Material for the
    WHOLE still-open chain is loaded once (no return trips), then the next field (by
    proximity) is built. One build step per call. If a field fails (the bridge
    rejects), ONLY this order is released."""
    pid = pio["id"]
    ppos = gs.pos(pio)
    task = _PLAN.next_task_for(pid, builder_pos=ppos)
    if task is None:
        return gs
    sid_plat = task.sid
    cost = gs.build_cost(sid_plat) or 2
    # load material for the WHOLE open chain (up to capacity).
    need_total = min(_PLAN.chain_metal_needed(pid, gs.build_cost), gs.store_max(pio))
    if gs.stored(pio) < need_total:
        anchor = adjacent_networked_building(gs, pio, need_metal=True)
        pool = ore_available_for(gs, pid)
        if anchor is not None and pool > 0:
            want = min(need_total - gs.stored(pio), pool)
            if want > 0:
                conn.do({"type": "transfer", "unitId": anchor["id"], "targetId": pid,
                         "amount": want, "resource": "metal"})
                gs, pio = _reget(gs, conn, pid)
                if pio is None:
                    return gs
                ppos = gs.pos(pio)
    if gs.stored(pio) < cost:
        return gs   # not even for one platform - next turn
    spot = task.field
    if ppos != spot:
        # does an OWN unit stand on the platform field (e.g. another pioneer parked
        # there that does not move away by itself)? Then it blocks the last build
        # field -> deadlock (symptom: the last of the 4 platforms is never built,
        # "drive to platform rejected" repeats endlessly). First send the blocking
        # unit away (like run_builder for normal build sites), then drive.
        # except_id=pid, so that the pioneer does not try to send itself away.
        occ_now = gs.occupied_fields()
        if spot in occ_now and gs.pos(pio) != spot:
            blocker = next((v for v in gs.my_vehicles()
                            if v["id"] != pid and gs.pos(v) == spot), None)
            if blocker is not None:
                gs, _moved = clear_units_from_fields(gs, conn, {spot}, except_id=pid)
                gs, pio = _reget(gs, conn, pid)
                if pio is None:
                    return gs
                ppos = gs.pos(pio)
        ok_mv, mv_reason = conn.do({"type": "move", "unitId": pid, "target": list(spot)})
        if ok_mv:
            gs, pio = _reget(gs, conn, pid)
            if pio is None:
                return gs
            if gs.pos(pio) != spot:
                record_build_move(conn, gs, pio, spot)
                return conn.refresh_state() or gs
        else:
            # 'unit building' / 'unit moving' = the pioneer is only BUSY (e.g. it is
            # currently finishing the previous platform of the chain, the build runs
            # over several ticks). This is NOT a failed order - the order MUST be
            # kept so that it continues building next turn. Only on a REAL drive error
            # (no path etc.) release the order.
            if mv_reason and ("building" in mv_reason or "moving" in mv_reason):
                return gs   # beschaeftigt -> Auftrag behalten, naechste Runde weiter
            log(f"  Expansion: Fahrt zu Plattform {spot} abgelehnt ({mv_reason}) "
                f"-> Auftrag frei.")
            _PLAN.release_task(pid, spot, sid_plat)
            return gs
    ok, reason = conn.do({"type": "startBuild", "unitId": pid,
                          "buildingId": [1, sid_plat], "speed": -1,
                          "position": list(spot)})
    if ok:
        log(f"  Expansion: Wasserplattform gebaut auf {spot} (Kette).")
        _PLAN.mark_done(pid, spot, sid_plat)   # only this order, the rest stays
        # the platform build takes >= 1 turn (buildTurns). The unit is now
        # isBuilding and stays so until the next turn change - continuing to the next
        # chain link in the SAME turn is not possible (the bridge rejects with
        # 'unit building'). Next turn the function start drives to the next link (with
        # a travel-time pause) and builds.
        return conn.refresh_state() or gs
    if reason and "moving" not in reason:
        log(f"  Expansion: Plattform-Bau abgelehnt ({reason}) auf {spot} -> Auftrag frei.")
        _PLAN.release_task(pid, spot, sid_plat)
    return gs


def _defense_group_member_build(gs, conn, pio, ox, oy):
    """Builds ONE due member of the defense group at the site (ox,oy). One build step
    per call (like _build_platform_chain): determine the next member (order
    radar->gun_ari->gun_aa->gun_missel), load material, drive to the field,
    startBuild. Returns fresh gs. 'building'/'moving' = busy (keep the order)."""
    pid = pio["id"]
    nxt = defense_planner.next_member_to_build(gs, ox, oy)
    if nxt is None:
        return gs   # group complete (or no field) - the caller chooses a new site
    member, sid, field = nxt
    cost = gs.build_cost(sid) or 8
    # load material (one building is enough; no chain preloading needed).
    if gs.stored(pio) < cost:
        anchor = adjacent_networked_building(gs, pio, need_metal=True)
        pool = ore_available_for(gs, pid)
        if anchor is not None and pool > 0:
            want = min(cost - gs.stored(pio), pool)
            if want > 0:
                conn.do({"type": "transfer", "unitId": anchor["id"], "targetId": pid,
                         "amount": want, "resource": "metal"})
                gs, pio = _reget(gs, conn, pid)
                if pio is None:
                    return gs
    if gs.stored(pio) < cost:
        return gs   # material not enough - next turn
    ppos = gs.pos(pio)
    if ppos != field:
        occ_now = gs.occupied_fields()
        if field in occ_now and gs.pos(pio) != field:
            blocker = next((v for v in gs.my_vehicles()
                            if v["id"] != pid and gs.pos(v) == field), None)
            if blocker is not None:
                gs, _moved = clear_units_from_fields(gs, conn, {field}, except_id=pid)
                gs, pio = _reget(gs, conn, pid)
                if pio is None:
                    return gs
        ok_mv, mv_reason = conn.do({"type": "move", "unitId": pid, "target": list(field)})
        if ok_mv:
            gs, pio = _reget(gs, conn, pid)
            if pio is None:
                return gs
            if gs.pos(pio) != field:
                record_build_move(conn, gs, pio, field)
                return conn.refresh_state() or gs
        else:
            if mv_reason and ("building" in mv_reason or "moving" in mv_reason):
                return gs   # busy -> continue next turn
            log(f"  [Schirm] Fahrt zu {field} fuer {member} abgelehnt ({mv_reason}).")
            return gs
    ok, reason = conn.do({"type": "startBuild", "unitId": pid,
                          "buildingId": [1, sid], "speed": -1,
                          "position": list(field)})
    if ok:
        log(f"  [Schirm] {member} gebaut auf {field} (Gruppe {ox},{oy}).")
        return conn.refresh_state() or gs
    if reason and "moving" not in reason:
        log(f"  [Schirm] Bau von {member} auf {field} abgelehnt ({reason}).")
    return gs


def run_defense_expansion(gs, conn, claim):
    """CONSUMER of the base_expansion pot for the DEFENSE SCREEN. Gets a pioneer
    (core task base_expansion preferred), chooses/holds a group site (2x2 in ring 5-8
    with the largest coverage gain) and builds the next group member. Precondition
    (checked by the caller): storage has NO priority any more and
    defense_build_allowed(gs) is met. Returns fresh gs."""
    ct = getattr(gs, "COVER_TARGET", None)
    if not ct:
        return gs

    # 1. determine/hold the site. A held site stays until the group is complete;
    #    then (or if none held) choose the best new one.
    corner = _DEFENSE_SITE.get("corner")
    if corner is not None and defense_planner.group_complete(gs, *corner):
        corner = None
    if corner is None:
        if not defense_planner.has_gap(gs, cover_target=ct):
            return gs   # screen complete - nothing to do
        corner = defense_planner.best_group_site(gs, cover_target=ct)
        if corner is None:
            return gs   # no buildable site found
        _DEFENSE_SITE["corner"] = corner
        log(f"  [Schirm] neue Gruppe geplant bei 2x2-Ecke {corner} "
            f"(cover_target={ct}).")

    # 2. get and bind a pioneer.
    pio = None
    for uid, a in list(_UNIT_ALLOC.items()):
        if a.get("task") == "base_expansion" and a.get("started"):
            v = next((x for x in gs.my_vehicles() if x["id"] == uid), None)
            if v is not None:
                pio = v
                break
            else:
                alloc_release(uid)   # pioneer gone
    if pio is None:
        free = _pick_free_for(gs, "engineer", claim, for_task="base_expansion")
        if not free:
            return gs
        prank = {v["id"]: i for i, v in enumerate(free)}
        pio = sorted(free, key=lambda v: (prank[v["id"]], -gs.stored(v)))[0]
        alloc_set(pio["id"], "base_expansion", started=True,
                  payload={"corner": corner})
    claim.add(pio["id"])
    _touch(pio["id"])

    # 3. build one member. If the group becomes complete, release the allocation so
    #    that the pioneer becomes free for the next group (or other work).
    gs = _defense_group_member_build(gs, conn, pio, *corner)
    if defense_planner.group_complete(gs, *corner):
        log(f"  [Schirm] Gruppe {corner} komplett.")
        alloc_release(pio["id"])
        _DEFENSE_SITE["corner"] = None
    return gs


def _site_build_pos(gs, site, comp):
    """The field whose REACHABILITY counts for the due component - basis of the
    path-cost estimate. IMPORTANT (verified in the MAXR code):
      - platform/rubble (small building / clear): built ON the own field
        (buildPosition==getPosition) -> the builder MUST reach this field -> return
        the component field.
      - mine (big 2x2 building): the constructor builds from NEXT TO the area and is
        also loaded only in 8-neighbourhood (cUnit::isNextTo / canTransferTo). It need
        NOT stand on mine_pos - this field may even be unenterable (water without a
        platform), while a neighbour field is perfectly reachable. Therefore measure
        the reachability to a FREE 8-NEIGHBOUR field of the 2x2, not to the occupied
        footprint field. (Former bug: path measured to mine_pos -> wrongly
        'unreachable'.)
    Returns (x,y) or None."""
    if comp.kind in (COMP_PLATFORM, COMP_CLEAR) and comp.fields:
        return tuple(comp.fields[0])
    if site.mine_pos is not None:
        mx, my = site.mine_pos
        mcells = [(mx, my), (mx + 1, my), (mx, my + 1), (mx + 1, my + 1)]
        # free 8-neighbour fields of the 2x2 area enterable for a builder (there the
        # constructor stands to build/load). Exclude the footprint cells themselves.
        fp = set(mcells)
        cands = []
        for (cx, cy) in mcells:
            for n in gs.free_neighbors8(cx, cy):
                if n not in fp:
                    cands.append(tuple(n))
        if cands:
            return cands[0]
        # no free neighbour field known -> anchor as an approximation (better than nothing).
        return tuple(site.anchor)
    return tuple(site.anchor)


def _builder_total_cost(gs, conn, builder, comp, build_pos):
    """REAL total path cost (movement points) that 'builder' needs to arrive LOADED
    at 'build_pos'. Smaller = better. Uses the bridge's pathCost query
    (cPathCalculator - the same path computation as the real MAXR client, real
    terrain, NO straight line). Returns float('inf') if unreachable.

    Two cases:
      - builder already has enough material (stored >= cost): only the build path
        counts -> path_cost(builder -> build_pos).
      - builder must load: load path + build path
        -> path_cost(builder -> nearest network load point)
         + path_cost(builder -> build_pos).
        Both summands are REAL bridge paths from the builder's CURRENT position. The
        second summand measures the direct path to the build site (not the detour via
        the load point) - the pathCost query can only compute from the unit's current
        position (like the real client; a start-field argument would be a feature the
        normal MAXR client does NOT have, so we keep the bridge dumb). As a selection
        measure this ranks the builders correctly: close to the network AND the build
        site wins.

    === TODO (LATER, NOT NOW): UNCONVENTIONAL LOGISTICS BY TRANSPORT PLANE ===
    The variant computed here is the CONVENTIONAL way: the builder moves itself (load,
    drive, build) over the terrain. There is a faster, UNCONVENTIONAL way, to be
    computed here later as a SECOND cost variant and compared against the conventional
    one - the cheaper wins:

      A TRANSPORT PLANE flies the complete logistics cycle, ideally ALL IN ONE TURN:
        1. load the builder (transporter drives to the builder / builder is already
           loaded),
        2. drop it at the nearest network load point -> builder loads material,
        3. pick the builder up again,
        4. fly to the build site and drop it there.
      This is often the FASTEST build-site logistics, because the transport plane:
        - has significantly MORE movement points than a builder,
        - flies STRAIGHT LINE and IGNORES obstacles (water, terrain, units),
        - is above all massively superior over WATER (builders barely/hardly make
          progress there).
      Only if the build site is very far away can the conventional way keep up in an
      individual case - therefore compute BOTH variants and choose the one with the
      lowest total "turn cost" (possibly turns instead of movement points, since the
      transporter does everything in one turn). Precondition: a free transport plane
      with sufficient load capacity is available. The pathCost query then applies to
      the transporter (flies straight line); the builder barely moves itself in this
      variant.
    """
    bid = builder["id"]
    cost = comp.metal_cost or 0
    # build path (always needed): real path from the position to the build site.
    pc_build = path_cost_to(conn, bid, build_pos)
    if pc_build is None or not pc_build.get("reachable", False):
        return float("inf")
    total = pc_build.get("cost", 0) or 0

    # does the builder still need to load? Then add the load path to the nearest network point.
    if gs.stored(builder) < cost:
        dock = dock_field_at_base(gs, builder)
        if dock is None:
            return float("inf")   # cannot load -> unusable
        pc_load = path_cost_to(conn, bid, dock)
        if pc_load is None or not pc_load.get("reachable", False):
            return float("inf")
        total += pc_load.get("cost", 0) or 0
    return float(total)


def _pick_builder_for(gs, conn, candidates, comp, build_pos):
    """Chooses from 'candidates' (free builders of the matching type) the one that
    arrives LOADED at 'build_pos' FASTEST (lowest real total path cost). Returns
    (builder, cost) or (None, inf) if none is reachable.

    Pre-filter against too many bridge queries: with many candidates first sort
    roughly by straight line to the build site and evaluate only the nearest K via a
    real pathCost query (the exact route arises only at the move anyway)."""
    if not candidates:
        return None, float("inf")
    bx, by = build_pos
    ordered = sorted(candidates,
                     key=lambda v: (gs.pos(v)[0] - bx) ** 2 + (gs.pos(v)[1] - by) ** 2)
    K = 6   # cap: check at most 6 candidates for real (2 queries per candidate)
    best, best_cost = None, float("inf")
    for v in ordered[:K]:
        c = _builder_total_cost(gs, conn, v, comp, build_pos)
        if c < best_cost:
            best, best_cost = v, c
    return best, best_cost


def build_water_mine_at(gs, conn, site, blocked, targets, claim):
    """PROVEN, goal-bound water-mine build sequence (from mode_expansion_legacy) with
    PERSISTENT BUILDER BINDING via the SiteComponent.builder fields. A site stays in
    the backlog until the mine REALLY stands confirmed (mine_covering on site.mine_pos
    - checked by _sync_backlog_state / phase-2 pruning), and once it is
    InConstruction it belongs to EXACTLY ONE pioneer (platform phase) resp.
    constructor (mine phase). As long as a builder is bound, ONLY THIS one is driven
    on - NO other free builder is snatched. This prevents scattering over dozens of
    areas (symptom: 1 platform everywhere, never a complete 2x2).

    Steps (exactly one progress per turn):
      1. directly (land) buildable? -> constructor builds the mine.
      2. otherwise: 2x2 platform area = site.mine_pos (fixed ONCE, stable).
      3. as long as the 2x2 is NOT COMPLETELY platformed (platform_fields_needed
         (site.mine_pos) != [] - i.e. EXACTLY the 4 fields of the PLANNED area, not 3,
         not offset): the BOUND pioneer builds the chain on (_build_platform_chain).
         No builder bound -> choose and bind one.
      4. ONLY when the 2x2 is complete: the BOUND constructor builds the mine.
    Returns the fresh gs."""
    mine_sid = gs.MINE_SID
    tx, ty = site.anchor

    # already a mine at the target? Then the build part is done (coupling runs
    # separately). _sync_backlog_state / pruning then clears the site.
    if gs.mine_covering((tx, ty)) is not None:
        return gs

    plat_comps = [c for c in site.components if c.kind == COMP_PLATFORM]
    mine_comp = next((c for c in site.components if c.kind == COMP_MINE), None)

    # --- land mine (no platform components) -----------------------------
    if not plat_comps:
        if mine_comp is not None:
            gs = _drive_or_assign_mine(gs, conn, site, mine_comp, blocked,
                                       targets, claim)
        return gs

    # --- water mine: first platform the 2x2 COMPLETELY -----------------
    # site.mine_pos is the ONCE-fixed, stable 2x2 corner. The platform phase is done
    # when for EXACTLY this area no fields are missing any more.
    pf = gs.platform_fields_needed(site.mine_pos) if site.mine_pos else None
    plats_complete = (pf == [])

    if not plats_complete:
        # find the bound pioneer of this site (at some platform component).
        pid = next((c.builder for c in plat_comps if c.builder is not None), None)
        pio = None
        if pid is not None:
            pio = next((v for v in gs.my_vehicles() if v["id"] == pid), None)
            if pio is None:
                # pioneer dead -> release local AND global binding, re-bind.
                alloc_release(pid)
                for c in plat_comps:
                    c.builder = None
                pid = None
            elif pio.get("isBuilding"):
                # is it building a PLATFORM (this chain)? Then keep it. Is it
                # building something else (harnessed by the emergency) -> not
                # available, re-bind. If the emergency recalled it, its platform_chain
                # allocation is already gone via alloc_release; for safety release it
                # here too (harmless if already removed), so that no ghost entry
                # remains.
                bt = pio.get("buildingTyp") or pio.get("buildingType")
                bsid = bt.get("secondPart") if isinstance(bt, dict) else None
                if bsid != gs.building_sid_by_name("platform"):
                    alloc_release(pid)
                    for c in plat_comps:
                        c.builder = None
                    pio = None
                    pid = None
        if pio is None:
            # no (living) pioneer bound yet -> choose one and bind it to ALL platform
            # components of this site (one chain, one pioneer).
            free = _pick_free_for(gs, "engineer", claim, for_task="platform_chain")
            if not free:
                return gs
            sid_plat = gs.building_sid_by_name("platform")
            items = [(tuple(f), sid_plat, gs.build_cost(sid_plat) or 2, "platform")
                     for c in plat_comps for f in c.fields]
            if not items:
                return gs
            # core-task preference dominates (own platform pioneers first, then
            # floating, then stand-in); within the preferred rank take the most LOADED
            # one (preloaded -> immediately ready to build, no return trip). Stable
            # sort: first -stored, then preference rank.
            prank = {v["id"]: i for i, v in enumerate(free)}
            pio = sorted(free, key=lambda v: (prank[v["id"]], -gs.stored(v)))[0]
            _PLAN.assign_chain(pio["id"], items, by_distance=True)
            for c in plat_comps:
                c.builder = pio["id"]
                c.state = SiteComponent.S_BUILDING
            # GLOBAL allocation: the pioneer is now firmly bound to the platform
            # chain (started=True) until the 2x2 stands COMPLETE. This way neither the
            # net repair nor the normal assignment tears it away (_free_units excludes
            # it). Only the emergency may explicitly recall it (alloc_release) if it
            # otherwise has no free builder.
            alloc_set(pio["id"], "platform_chain", started=True,
                      payload={"site": tuple(site.anchor)})
            log(f"  [Backlog] Site {site.anchor}: Pionier {pio['id']} bekommt "
                f"{len(items)} Plattform(en) als Kette (2x2 auf {site.mine_pos}).")
        claim.add(pio["id"])
        _touch(pio["id"])   # platform build processes this pioneer this turn (even
                            # if it only drives/loads/waits) -> stuck-clear skips it.
        # the pioneer has no open _PLAN tasks any more (e.g. release after reject),
        # but the 2x2 is not yet complete -> create the chain anew so that it
        # continues building instead of standing still.
        if not _PLAN.tasks_for(pio["id"]):
            sid_plat = gs.building_sid_by_name("platform")
            items = [(tuple(f), sid_plat, gs.build_cost(sid_plat) or 2, "platform")
                     for f in (pf or [])]
            if items:
                _PLAN.assign_chain(pio["id"], items, by_distance=True)
        gs = _build_platform_chain(gs, conn, pio)
        return gs

    # --- 2x2 complete -> mark the platform components as done ----------
    # The pioneer has done its task (exactly the 4 platforms of the 2x2). Release the
    # local binding AND the global platform_chain allocation - it is now free again
    # (the mine is built by the CONSTRUCTOR, not the pioneer).
    for c in plat_comps:
        if not c.is_done():
            if c.builder is not None:
                alloc_release(c.builder)
            c.state = SiteComponent.S_DONE
            c.builder = None

    # --- build the mine (bound constructor) --------------------------------
    if mine_comp is not None:
        gs = _drive_or_assign_mine(gs, conn, site, mine_comp, blocked,
                                   targets, claim)
    return gs


def _drive_or_assign_mine(gs, conn, site, mine_comp, blocked, targets, claim):
    """Drives the constructor bound to the mine on, or binds a new one if none is
    (any longer) bound. Exactly ONE constructor per mine."""
    tx, ty = site.anchor
    cid = mine_comp.builder
    con = None
    if cid is not None:
        con = next((v for v in gs.my_vehicles() if v["id"] == cid), None)
        # release the binding if the constructor is gone OR building something OTHER
        # than this mine (e.g. harnessed by the emergency for a factory/station). A
        # constructor currently building THIS mine is also isBuilding - it stays
        # bound. Distinction: if it builds another sid / does not stand on the mine
        # area, it is NOT available -> choose anew.
        if con is None:
            mine_comp.builder = None
            cid = None
        elif con.get("isBuilding"):
            bt = con.get("buildingTyp") or con.get("buildingType")
            bsid = bt.get("secondPart") if isinstance(bt, dict) else None
            on_mine = site.mine_pos is not None and gs.pos(con) in [
                tuple(site.mine_pos),
                (site.mine_pos[0]+1, site.mine_pos[1]),
                (site.mine_pos[0], site.mine_pos[1]+1),
                (site.mine_pos[0]+1, site.mine_pos[1]+1)]
            if not (bsid == gs.MINE_SID and on_mine):
                # building something else -> not available, release the binding
                mine_comp.builder = None
                con = None
                cid = None
    if con is None:
        free = _free_units(gs, "constructor", claim)   # excludes isBuilding
        if not free:
            return gs
        # take the most LOADED constructor (preloaded first -> immediately ready to
        # build, no return trip to reload).
        con = max(free, key=lambda v: gs.stored(v))
        mine_comp.builder = con["id"]
        mine_comp.state = SiteComponent.S_BUILDING
        log(f"  [Backlog] Site {site.anchor}: Konstrukteur {con['id']} baut Mine "
            f"(Ziel {site.target_type}).")
    claim.add(con["id"])
    _touch(con["id"])   # mine build processes this constructor this turn (even if it
                        # only drives/loads/waits) -> stuck-clear skips it.
    # THE NEW SOURCE IS THE TRUTH: site.anchor/site.mine_pos. The older memory
    # _CONSTRUCTOR_MINE_POS bound to the constructor ID (from
    # _expansion_send_constructor) may still hold an EARLIER goal (the constructor was
    # re-hung here from an old site). If it points to a different goal than this site,
    # delete it - otherwise _expansion_send_constructor keeps driving to the old goal
    # and discards it every turn ("no valid build site") while the finished platforms
    # here stay unused.
    mem = _CONSTRUCTOR_MINE_POS.get(con["id"])
    if mem is not None and tuple(mem.get("goal", ())) != (tx, ty):
        _CONSTRUCTOR_MINE_POS.pop(con["id"], None)
    gs = _expansion_send_constructor(gs, conn, con, (tx, ty), gs.MINE_SID,
                                     blocked, targets,
                                     rejected_targets=_EXPANSION_REJECTED,
                                     target_type=site.target_type)
    return gs




def _drive_committed_component(gs, conn, site, comp, blocked, targets, claim):
    """Drives an ALREADY COMMITTED component (comp.builder set) one step further each
    turn until it is done. This is the consequence of the consumer ACCEPTING build
    orders: it must also COMPLETE them, not just trigger them once. Former bug:
    committed sites were skipped with 'continue' -> the builder never drove on after
    the first trigger, the mine was never finished. The build routines are continuing
    (own memory: _CONSTRUCTOR_MINE_POS resp. _PLAN chain), a renewed call each turn
    drives the builder on. Returns the fresh gs."""
    bid = comp.builder
    builder = next((v for v in gs.my_vehicles() if v["id"] == bid), None)
    if builder is None:
        return gs   # builder gone -> the reconciliation releases the component
    claim.add(bid)   # occupied, so that no other mode grabs it
    tx, ty = site.anchor
    if comp.kind == COMP_PLATFORM:
        gs = _build_platform_chain(gs, conn, builder)
    elif comp.kind == COMP_MINE:
        gs = _expansion_send_constructor(
            gs, conn, builder, (tx, ty), gs.MINE_SID, blocked, targets,
            rejected_targets=_EXPANSION_REJECTED, target_type=site.target_type)
    elif comp.kind == COMP_CONNECTOR:
        gs = _expansion_build_connector(gs, conn, builder, (tx, ty),
                                        blocked, targets)
    # COMP_CLEAR: autonomous bulldozer control, no driving on needed.
    return gs


def _revalidate_blocked_sites(gs):
    """Checks per still-unfinished mine site whether its fixed 2x2 area (mine_pos) is
    meanwhile blocked by a BUILDING or a FOREIGN/neutral unit. derive_components fixes
    mine_pos only ONCE; if the emergency later builds e.g. a factory on this area, the
    mine site otherwise stands forever and its constructor gets stuck ("build position
    blocked (2x2)").

    Own MOBILE units do NOT count as blockers (drive-to-the-side) -
    occupied_fields_for_mine already excludes them.

    On blockage: first check whether the ANCHOR is still buildable via ANY valid 2x2
    (land directly or via water platforms); if yes, re-fix mine_pos to this position
    (derive components fresh); if no, DISCARD the site and block its field
    (_EXPANSION_REJECTED) so that the bound constructor becomes free. Returns
    (refixed, dropped)."""
    mine_sid = gs.MINE_SID
    plat_sid = gs.building_sid_by_name("platform")
    conn_sid = gs.building_sid_by_name("connector")
    # IMPORTANT: blocking_fields_for_mine (NOT occupied_fields_for_mine) - own
    # PASSABLE buildings (platform/coupling/bridge/road) do NOT count as blockers
    # here. Otherwise the first self-built water platform of the 2x2 would make the
    # own site appear "blocked" and it would be wrongly discarded (symptom: pioneer
    # aborts after 1-2 platforms). Only real blockers (own/foreign REAL buildings,
    # foreign/neutral units) count.
    occ = gs.blocking_fields_for_mine()
    refixed = dropped = 0
    for site in list(_BACKLOG.sorted_open()):
        if site.mine_pos is None:
            continue
        mine_comp = next((c for c in site.components if c.kind == COMP_MINE), None)
        if mine_comp is None or mine_comp.is_done():
            continue   # no open mine component -> nothing to check
        mfields = [tuple(site.mine_pos),
                   (site.mine_pos[0] + 1, site.mine_pos[1]),
                   (site.mine_pos[0], site.mine_pos[1] + 1),
                   (site.mine_pos[0] + 1, site.mine_pos[1] + 1)]
        # does THIS mine already stand here (under construction/finished)? Then nothing is blocked.
        if gs.mine_covering(tuple(site.mine_pos)) is not None:
            continue
        blocked_fields = [f for f in mfields if f in occ]
        if not blocked_fields:
            continue   # area free -> all good
        # the area seems blocked. AUTHORITATIVE is the official build-site logic
        # (mine_build_position / _with_platforms) - it knows land/water/platforms and
        # the real blockers. If it returns a valid position, the anchor is buildable:
        #   - different position than before -> re-fix (mine_pos wandered).
        #   - THE SAME position -> the area is NOT permanently blocked (the field
        #     marked in blocking_fields_for_mine is e.g. a not-yet-platformed water
        #     field of the own 2x2 or a temporary state) -> do NOTHING, keep the site.
        # Only if the official logic returns NO position at all (None) is the anchor
        # really not developable -> discard.
        alt = gs.mine_build_position(site.anchor, target_type=site.target_type)
        if alt is None:
            alt = gs.mine_build_position_with_platforms(
                site.anchor, target_type=site.target_type)
        if alt is not None and tuple(alt) == tuple(site.mine_pos):
            continue   # still buildable at the same place -> keep
        if alt is not None:
            # new position -> derive the site fresh (mine_pos, components anew).
            site.mine_pos = None
            for c in site.components:
                c.builder = None
            if site.derive_components(gs, mine_sid, plat_sid, conn_sid):
                refixed += 1
                log(f"  [Backlog] Site {site.anchor}: 2x2 {tuple(blocked_fields)} "
                    f"blockiert - neu auf {site.mine_pos} fixiert.")
                continue
        # no alternative position THIS TURN -> do NOT discard the site permanently.
        # A blockage is a snapshot: if e.g. only a passing foreign/neutral mobile
        # unit stands on the only valid 2x2, mine_build_position finds no position
        # this turn - next turn, when the blocker is gone, the (lucrative) spot is
        # buildable again. Therefore: only release the bound builder and block the
        # field TURN-LOCALLY (prevents the same builder from immediately choosing it
        # again the same turn), but leave the site IN THE BACKLOG. It is re-evaluated
        # every turn via the live buildability.
        for c in site.components:
            c.builder = None
        _EXPANSION_REJECTED.add(tuple(site.anchor))   # only this turn (cleared at
                                                      # turn start in mode_expansion)
        dropped += 1
        log(f"  [Backlog] Site {site.anchor}: 2x2 {tuple(blocked_fields)} diese Runde "
            f"blockiert, keine andere bebaubare 2x2 - Bauer frei, Site bleibt im "
            f"Backlog (naechste Runde neu geprueft).")
    return refixed, dropped


def _sync_backlog_state(gs):
    """STATE SYNC from the REAL game state. The build routines report completion only
    to the _PLAN (mark_done), but NOT to the SiteComponent - whose state otherwise
    stays forever S_WISHED with builder set, so that due_component never advances past
    the component (symptom: platform built, but the mine never becomes due; the
    consumer falls silent). This function derives the done status per component from
    the REAL state (no self-set flag):
      - platform: all platform fields of the 2x2 stand (platform_fields_needed empty).
      - mine: a mine stands at mine_pos (mine_covering).
      - coupling: the mine is connected to the network (mine_is_networked).
    Finished components -> state=S_DONE, builder=None, so that due_component advances
    to the next component. Returns the number of newly done-marked components."""
    synced = 0
    for site in _BACKLOG.sorted_open():
        # check the mine at the PLANNED 2x2 area, NOT at the anchor: with densely
        # packed deposits a neighbour mine covers the anchor too -> otherwise the mine
        # component would wrongly count as finished (the constructor jumps).
        check_field = site.mine_pos if site.mine_pos is not None else site.anchor
        mine = gs.mine_covering(check_field) if check_field else None
        if mine is not None and site.mine_pos is not None:
            mpos = gs.pos(mine)
            if tuple(mpos) != tuple(site.mine_pos):
                mine = None   # overlapping neighbour mine, not THIS one
        plats_done = (site.mine_pos is not None
                      and gs.platform_fields_needed(site.mine_pos) == [])
        for comp in site.components:
            if comp.is_done():
                continue
            done = False
            if comp.kind == COMP_PLATFORM:
                done = plats_done
            elif comp.kind == COMP_MINE:
                done = mine is not None
            elif comp.kind == COMP_CONNECTOR:
                done = mine is not None and gs.mine_is_networked(mine)
            if done:
                # the platform builder is a pioneer with a global platform_chain
                # allocation (started=True). If its component is set to S_DONE here,
                # the global _UNIT_ALLOC MUST also be released - otherwise the pioneer
                # stays permanently marked "building platform", stands idle and NO
                # task (not even the net repair) can ever take it again. (That was the
                # cause of idle pioneers + starving repair.)
                if comp.kind == COMP_PLATFORM and comp.builder is not None:
                    a = _UNIT_ALLOC.get(comp.builder)
                    if a is not None and a.get("task") == "platform_chain":
                        # only release if the WHOLE 2x2 is finished (the pioneer
                        # builds the complete chain; a single finished platform does
                        # not mean it is done).
                        if plats_done:
                            alloc_release(comp.builder)
                comp.state = SiteComponent.S_DONE
                comp.builder = None
                synced += 1
    return synced


def _reconcile_backlog(gs):
    """Releases orphaned component assignments (counterpart to _reconcile_plan for the
    emergency). A component holds a builder ID (committed). This function checks per
    committed component whether the builder is STILL working on THIS task; if not,
    comp.builder is set to None -> the component is assignable again.

    Release if the builder ...
      - no longer exists (dead / lost), OR
      - lives but has an EMERGENCY order in the _PLAN (the emergency grabbed it - e.g.
        pioneer to the radar). Then it no longer belongs to the site.
    NO redistribution - running, valid assignments stay untouched (no pull model, no
    wandering). Returns the number of releases."""
    alive = {v["id"] for v in gs.my_vehicles()}
    plan_builders = {t.builder for t in _PLAN.all_tasks()}
    freed = 0
    for site in _BACKLOG.sorted_open():
        for comp in site.components:
            bid = comp.builder
            if bid is None:
                continue
            # platform chains run via the _PLAN - a pioneer that has its platform
            # tasks in the _PLAN is still working correctly at the site.
            plat_chain = (comp.kind == COMP_PLATFORM and bid in plan_builders)
            lost = (bid not in alive) or (bid in plan_builders and not plat_chain)
            if lost:
                comp.builder = None
                freed += 1
    return freed


def mode_expansion(gs, conn, blocked, targets, claim=None):
    """MODE HANDLER 'expansion' (priority after emergency). DISPATCHER:
      - _USE_BACKLOG True  -> build-site backlog consumer (Dungeon-Keeper).
      - _USE_BACKLOG False -> proven legacy path (mode_expansion_legacy).
    This lets you switch/compare directly between old and new in-game.

    TURN-LOCAL REJECTION: _EXPANSION_REJECTED is CLEARED here at turn start. A blockage
    is a snapshot at the moment of the build decision ("do I get there NOW and can
    build?"), NOT a permanent property of a site. Within THIS turn the set prevents a
    field just recognised as unreachable from being chosen again immediately (endless
    loop in the same turn); next turn it starts empty, so that every spot is
    re-evaluated fresh via the live buildability (mine_build_position, checked every
    turn). This way a lucrative spot blocked only by a passing (foreign/neutral) mobile
    unit comes back automatically once the blocker is gone - no permanent lock."""
    _EXPANSION_REJECTED.clear()
    if _USE_BACKLOG:
        return mode_expansion_backlog(gs, conn, blocked, targets, claim)
    return mode_expansion_legacy(gs, conn, blocked, targets, claim)


def _preload_first_mine_builders(gs, conn, claim):
    """RESERVE + STANDBY PHASE for the first >=7-metal mandatory mine. As long as the
    bot has only the starting mine (mine_count<=1) AND no >=7-ore area is explored yet,
    the target is unknown - the build cannot start yet. So that on the find building
    can start IMMEDIATELY, EXACTLY ONE constructor (60 ore, mine) and ONE pioneer
    (8 ore = 4 water platforms at 2 each) are remembered as a RESERVE by fixed ID
    (_FIRST_MINE_RESERVE), kept fully loaded and BOUND (claim) - they otherwise do
    NOTHING. Nobody knows whether the field will be water or land; preparation is for
    the more expensive water case (land -> pioneer later superfluous, material stays).
    ALL OTHER builders stay free and may build weaker mines. The reserve is released as
    soon as a >=7 field is found (then the mandatory site builds immediately with
    exactly these loaded builders) or from mine 2. Returns the fresh gs."""
    # dissolve the reserve as soon as no longer needed (mine 2 reached OR >=7 field
    # found -> the backlog now builds the mandatory mine regularly with these builders).
    field_found = (gs.mine_count() <= 1 and gs.expansion_target(
        blocked_fields=_EXPANSION_REJECTED, force_type="metal", min_metal=7) is not None)
    if gs.mine_count() > 1 or field_found:
        # no longer bind - the loaded reserve builders are now available to the
        # backlog/expansion (the most loaded is preferentially chosen anyway).
        _FIRST_MINE_RESERVE["con"] = None
        _FIRST_MINE_RESERVE["pio"] = None
        return gs

    def _reserve(role, key, want_total):
        nonlocal gs
        # check the existing reserve: is it still alive AND free (not building)? A
        # constructor remembered as a reserve may meanwhile have been harnessed by the
        # emergency for a factory/station (isBuilding) - then it is NOT available and
        # the reserve must be re-chosen to a really free builder (otherwise the reserve
        # points to a building one that cannot drive to the mine, and the actually
        # free one is never taken).
        rid = _FIRST_MINE_RESERVE.get(key)
        b = None
        if rid is not None:
            b = next((v for v in gs.vehicles_of_type(role) if v["id"] == rid), None)
            if b is None or b.get("isBuilding"):
                _FIRST_MINE_RESERVE[key] = None
                rid = None
                b = None
        if rid is None:
            # choose a new reserve builder (one that is NOT already otherwise occupied
            # and does NOT build - _free_units excludes isBuilding).
            free = _free_units(gs, role, claim)
            if not free:
                return
            b = max(free, key=lambda v: gs.stored(v))
            _FIRST_MINE_RESERVE[key] = b["id"]
            rid = b["id"]
        # ALWAYS bind the reserve (it does nothing but load until the field is there).
        claim.add(rid)
        cap = gs.store_max(b)
        target_amt = min(want_total, cap)
        if gs.stored(b) >= target_amt:
            return   # full - ready, waits bound
        # not yet full -> reload.
        anchor = adjacent_networked_building(gs, b, need_metal=True)
        if anchor is None:
            dock = dock_field_at_base(gs, b)
            if dock is not None and gs.pos(b) != dock:
                ok_mv, _ = conn.do({"type": "move", "unitId": rid, "target": list(dock)})
                if ok_mv:
                    gs, b = _reget(gs, conn, rid)
                    if b is None:
                        return
                    anchor = adjacent_networked_building(gs, b, need_metal=True)
        if anchor is not None:
            want = min(target_amt - gs.stored(b), ore_available_for(gs, rid))
            if want > 0:
                ok, _ = conn.do({"type": "transfer", "unitId": anchor["id"],
                                 "targetId": rid, "amount": want, "resource": "metal"})
                if ok:
                    gs, _b = _reget(gs, conn, rid)
                    log(f"  [Backlog] Erste-Erzmine-RESERVE: {role} {rid} laedt vor "
                        f"(Ziel {target_amt} Erz) - wartet auf >=7-Feld.")

    _reserve("constructor", "con", 60)
    _reserve("engineer", "pio", 8)
    return gs


def mode_expansion_backlog(gs, conn, blocked, targets, claim=None):
    """CONSUMER of the build-site backlog (Dungeon-Keeper). The _BACKLOG was filled +
    prioritised + overlap-cleaned in phase 2 (mark_expansion_backlog). Here ASSIGNMENT
    happens: per site (highest priority first) the DUE component to a free builder of
    the matching vehicle type, provided:
      - the material reservation allows it (priority A before B; within A first
        pioneer chains, then constructor; release as soon as all builders of the site
        are EN_ROUTE),
      - the build site does NOT collide with the higher-priority emergency plan.
    Once assigned, the site is committed (the component holds the builder ID) and is no
    longer displaced by better proposals in phase 2. Uses only builders the emergency
    has not claimed (claim).

    The NET REPAIR (coupling of separated islands) still runs via the proven legacy
    part A - it is independent of the site assignment and has its own, proven logic. We
    call it first."""
    if claim is None:
        claim = set()

    # STATE SYNC first: set really finished components (platform stands / mine
    # stands / coupling connected) to S_DONE and release their builder, so that
    # due_component advances to the next component (otherwise a built platform stays
    # 'due+committed' forever and the mine is never assigned).
    # REVALIDATION: re-fix blocked mine areas (e.g. the emergency built a factory on
    # the once-fixed 2x2) or discard the site, BEFORE the state sync runs - otherwise
    # the constructor hangs forever on a permanently blocked area ("build position
    # blocked (2x2)").
    refixed, dropped = _revalidate_blocked_sites(gs)
    if refixed or dropped:
        log(f"  [Backlog] Revalidierung: {refixed} Flaeche(n) neu fixiert, "
            f"{dropped} Baustelle(n) verworfen (blockiert).")

    synced = _sync_backlog_state(gs)
    if synced:
        log(f"  [Backlog] {synced} Komponente(n) als fertig erkannt (State-Sync).")

    # STANDBY PHASE: as long as the >=7-ore field of the first mandatory mine is not
    # yet found, preload pioneer (8) + constructor (60) so that on the find building
    # can start IMMEDIATELY. Once they are full, the function releases them (expansion
    # runs normally). If the field is found, it does nothing -> the mandatory site
    # (place 1 via mandatory) is built immediately below regularly via
    # build_water_mine_at.
    gs = _preload_first_mine_builders(gs, conn, claim)

    # RECONCILIATION: release orphaned component assignments (builder dead or grabbed
    # by the emergency), so that blocked sites become assignable again. Releases only
    # invalid assignments - running ones stay.
    freed = _reconcile_backlog(gs)
    if freed:
        log(f"  [Backlog] {freed} verwaiste Zuweisung(en) freigegeben "
            f"(Bauer tot/abgegriffen).")

    # NET REPAIR via ENGINE PATH (encapsulated). Binds a pioneer persistently via
    # _UNIT_ALLOC ("net_repair") to the stretch, builds straight pieces >=3 fields via
    # the engine PATH (startBuild with pathEnd), short pieces via the mini fallback.
    # (The old _run_network_repair including the 30% repair reserve and _REPAIR_LEAD
    # was removed - the persistence is taken over by _UNIT_ALLOC.)
    _gap_dbg = gs.network_gap_target()
    log(f"  [NetzDiag] Reparatur-Sektion erreicht: gap={'JA '+str(_gap_dbg[:2]) if _gap_dbg else 'None'}, "
        f"alloc={alloc_units_for('net_repair')}, claim={sorted(claim)}.")
    if _gap_dbg is not None:
        gs = _run_network_repair_path(gs, conn, claim)

    # POWER STATION BEFORE MINE BUILD (your directive: priority after emergency,
    # before mine build). The structural rule "at least one station per
    # _MINES_PER_STATION mines" sits in next_task_for_role (role=constructor, occasion
    # C). In the backlog expansion path this constructor trigger is otherwise NOT
    # reached (the loop below goes directly to the mine build) - the energy deadlock
    # (mines off for lack of power -> no metal -> station unaffordable) would not arise
    # in the first place if a station is pulled forward here in time. Exactly ONE
    # station per turn; only a free, not-already-claimed constructor.
    # next_task_for_role expects a dict ACCUMULATOR (counts what is already planned in
    # THIS pass) - NOT the BuildPlan object _PLAN. It must contain ALL keys that the
    # constructor path reads hard via plan[...] (otherwise KeyError if the station
    # build does not apply and the path continues to factories/gold). Fresh zero
    # accumulator: nothing has been planned in THIS call yet; the multiple-build
    # protection runs via claim + *_incl_construction.
    _acc_station = {"storage_metal": 0, "storage_oil": 0, "generators": 0,
                    "stations": 0, "radar": 0, "smallfactory": 0, "bigfactory": 0,
                    "storage_gold": 0, "gold_refinery": 0}
    _st = next_task_for_role(gs, "constructor", _acc_station)
    if _st is not None and _st[0] == "station":
        _free_cons = _free_units(gs, "constructor", claim)
        if _free_cons:
            # take the most loaded constructor (fastest ready to build)
            _con = max(_free_cons, key=lambda v: gs.stored(v))
            claim.add(_con["id"])
            log(f"  [Backlog] ENERGIE VOR MINE: Station -> Konstrukteur {_con['id']} "
                f"(Regel 1 Station / {_MINES_PER_STATION} Minen).")
            _res, _newgs = run_builder(gs, conn, _con, _st, {"base": 9999},
                                       blocked=blocked.setdefault(_con["id"], set()),
                                       targets=targets, may_reload=True)
            if _newgs is not None:
                gs = _newgs

    # ASSIGNMENT: go through the sites by priority. The actual building of a mine
    # (incl. the necessary water platforms) is done by the PROVEN, goal-bound sequence
    # build_water_mine_at - no longer the error-prone individual assignment of
    # platform/mine as separate components. The backlog provides the STRATEGY (which
    # deposit, priority, no emergency collision), the legacy sequence the BUILDING
    # (platforms first, then mine - coordinated).
    # ASSIGNMENT ORDER: sites that ALREADY have a builder bound (committed, in
    # progress) come FIRST - so that their builders are driven on and claimed this
    # turn BEFORE new sites grab the still-free pioneers. Otherwise a new,
    # higher-scored site could pull off a pioneer that actually has to finish its
    # started 2x2 - the result would be started-everywhere, finished-nowhere platform
    # blocks (the "puzzle"). Within both groups the priority (mandatory, score) is
    # preserved.
    def _has_builder(s):
        return any(c.builder is not None for c in s.components)
    _open_sites = _BACKLOG.sorted_open()
    _ordered = ([s for s in _open_sites if _has_builder(s)]
                + [s for s in _open_sites if not _has_builder(s)])
    for site in _ordered:
        comp = site.due_component()
        if comp is None:
            continue   # all components finished (pruned in phase 2)

        # (Formerly here: collision check against the emergency _PLAN, which skipped
        # the mine area if it overlapped with an emergency build site. Removed: the
        # emergency no longer reserves/builds a mine - this check blocked the first
        # mandatory mine for no reason, although the emergency only plans base
        # buildings. The first mine belongs to the backlog alone.)

        tx, ty = site.anchor
        if comp.kind in (COMP_PLATFORM, COMP_MINE):
            # GOAL-BOUND build sequence with persistent builder binding: builds the
            # 2x2 COMPLETELY (exactly the planned area site.mine_pos), then the mine.
            # As long as a builder is bound, it drives only this one on - no
            # scattering. The site stays in the backlog until the mine really stands
            # confirmed.
            gs = build_water_mine_at(gs, conn, site, blocked, targets, claim)
        elif comp.kind == COMP_CONNECTOR:
            # coupling: only if a mine already stands at the target. A free pioneer
            # builds towards the main component (the net repair above covers the
            # separated-island case anyway).
            if comp.builder is not None:
                gs = _drive_committed_component(gs, conn, site, comp, blocked,
                                                targets, claim)
                continue
            free = _free_units(gs, "engineer", claim)
            if not free:
                continue
            bpos = _site_build_pos(gs, site, comp)
            builder, bcost = _pick_builder_for(gs, conn, free, comp, bpos)
            if builder is None:
                continue
            claim.add(builder["id"])
            comp.builder = builder["id"]
            comp.state = SiteComponent.S_EN_ROUTE
            log(f"  [Backlog] Site {site.anchor}: Pionier {builder['id']} "
                f"baut Kopplung (Wegkosten {bcost:.0f}).")
            gs = _expansion_build_connector(gs, conn, builder, (tx, ty),
                                            blocked, targets)
        elif comp.kind == COMP_CLEAR:
            comp.state = SiteComponent.S_WAITING
            log(f"  [Backlog] Site {site.anchor}: Schrott auf {comp.fields} "
                f"- wartet auf Bulldozer (autonom).")

    return gs


def mode_expansion_legacy(gs, conn, blocked, targets, claim=None):
    """LEGACY MODE HANDLER 'expansion' (before the backlog rebuild). Kept for A/B
    comparison; active as long as _USE_BACKLOG is False (mode_expansion then delegates
    here).

    Chooses the best explored deposit (score = amount/(straight line+k)) and sends a
    constructor to the mine and pioneers to the coupling IN PARALLEL. Uses only
    builders the emergency has not claimed (claim)."""
    if claim is None:
        claim = set()

    # === PART A: NETWORK CONNECTIVITY (repair BEFORE new build) =====================
    # On every execution, check whether the supply network is connected. Separated
    # islands arise through gaps in the coupling (pioneers stood in the way of one
    # another), destroyed connectors (enemy action) or a not-yet-connected new mine.
    # ALL these cases are handled by the same mechanic: (re)build the shortest
    # connection between the main island and the separated island - on the first build
    # the order is identical (the unfinished stretch is "repaired").
    #
    # PIONEER SPLIT: repair and expansion each get a 30% quota (min. 1) from the TOTAL
    # POOL. Repair is served FIRST - on scarcity (e.g. only ONE pioneer) this
    # automatically means: repair BEFORE expansion. Any remainder (beyond both quotas)
    # helps, if no emergency is pending, preferentially the repair, otherwise the
    # expansion.
    pio_total = max(1, _pioneer_total(gs))
    quota = max(1, int(pio_total * 0.30))   # 30% (min. 1) per task
    no_emergency = not gs.is_emergency()[0]

    gap = gs.network_gap_target()
    has_expansion = gs.expansion_target(blocked_fields=_EXPANSION_REJECTED) is not None
    if gap is not None:
        from_cell, to_cell, _d = gap
        log(f"  [Modus] EXPANSION/NETZ: Netz getrennt - repariere Strecke "
            f"{from_cell} -> {to_cell}.")
        repair_pios = _free_units(gs, "engineer", claim)
        if repair_pios:
            # the coupling chain grows LINEARLY from the SUPPLYING base (main
            # component) towards the island: each new piece attaches to a field
            # already connected to the main network (from_cell side) and is
            # immediately self-supplied. The pioneer loads material ONLY from the main
            # base (same SubBase - MAXR canTransferTo) and builds forward from there.
            # Therefore choose the pioneer NEAREST to the connection point
            # "from_cell" - a distant pioneer would have to shuttle endlessly between
            # build site and main base (cannot load at the island). ONLY ONE pioneer
            # ever builds one piece per turn (otherwise a connector fan).
            pio = min(repair_pios,
                      key=lambda v: (gs.pos(v)[0] - from_cell[0]) ** 2
                                    + (gs.pos(v)[1] - from_cell[1]) ** 2)
            claim.add(pio["id"])
            gs = _expansion_build_connector(gs, conn, pio, to_cell, blocked, targets,
                                            connect_to=gs.main_component())
        # on scarcity the repair has occupied the pioneer -> part B finds none ->
        # repair BEFORE expansion.

    # === PART B: NEW EXPANSION (new build) =====================================
    # PRIORITY (chief-build-planner model): the expansion is NOT paused when an
    # emergency is active, but DE-PRIORITISED. It keeps running in parallel and uses
    # ONLY the builders and material the emergency leaves over (via "claim" /
    # _free_units - the emergency has already claimed its builders). This matters
    # exactly when the emergency reports "too little output": the SOLUTION is more
    # mines, i.e. exactly expansion. The 30% pioneer quota and the claim keep
    # throttling the expansion so that it takes nothing from the emergency. (Formerly
    # it was hard-paused here - that prevented the development of new deposits although
    # free builders and material were available.)
    if not no_emergency:
        log("  [Modus] EXPANSION: laeuft nachrangig (Notfall aktiv - nutzt nur "
            "freie Bauer/Material).")

    # if the fuel for the energy production is not enough, more energy brings nothing
    # (it would not run for lack of oil). Then force the next target onto an OIL FIELD
    # (best by score / k value) to raise the output.
    force = None
    # MANDATORY ORE MINE: after the starting mine (i.e. as long as the bot has only 1
    # mine) the next mine MUST be a strong ORE mine - a 2x2 area with at least 9 ore
    # total output. Only after that (from mine 2) does the normal demand/score logic
    # take effect. Background: formerly the bot focused too early on gold/oil, the ore
    # output stayed too weak (ore famine, all builders wait for lack of material).
    # This constraint has PRIORITY over the oil redirection.
    force_min_metal = 0
    if gs.mine_count() <= 1:
        metal_target = gs.expansion_target(blocked_fields=_EXPANSION_REJECTED,
                                           force_type="metal", min_metal=7)
        if metal_target is not None:
            force = "metal"
            force_min_metal = 7
            log("  [Modus] EXPANSION: erste Expansion ERZWUNGEN auf starke Erz-Mine "
                "(>=7 Erz) - Pflicht nach der Startmine.")
        else:
            log("  [Modus] EXPANSION: keine Erz-Flaeche mit >=7 Erz erkundet - "
                "Surveyor muss weiter erkunden (kein Ausweichen auf Gold/Oel).")
            _HELD_EXPANSION_GOAL.pop("goal", None)
            _HELD_EXPANSION_GOAL.pop("platmine", None)
            return gs   # build NO weaker/other mine until 9+ ore is found
    # if the fuel for the energy production is not enough, more energy brings nothing
    # (it would not run for lack of oil). Then force the next target onto an OIL FIELD
    # (best by score / k value) to raise the output. Takes effect ONLY if the
    # mandatory ore mine already stands (force still None).
    if force is None and not gs.fuel_for_energy_ok():
        oil_target = gs.expansion_target(blocked_fields=_EXPANSION_REJECTED, force_type="oil")
        if oil_target is not None:
            force = "oil"
            log("  [Modus] EXPANSION: Treibstoff fuer Energie knapp -> Ziel auf "
                "Oelfeld umgelenkt.")

    # HOLD THE GOAL: if a platform chain is already running (a pioneer has platform
    # orders in the plan), THEIR deposit stays the goal - otherwise the goal wanders
    # on by score, a new chain is started and none is finished (scattered, unfinished
    # platforms). The held goal applies until the platforms+mine stand there.
    # Persists across turns (_PLAN-global).
    sid_plat = gs.building_sid_by_name("platform")
    held = _HELD_EXPANSION_GOAL.get("goal")
    chain_running = sid_plat is not None and any(
        t.sid == sid_plat for t in _PLAN.all_tasks())
    # hold the goal ONLY as long as the platform chain runs - so that the platforms do
    # not "wander" between turns (scattered, unfinished platforms). If the platforms
    # are FINISHED, the goal is NO longer forced: the mine build site is a SEPARATE,
    # independent decision re-evaluated every turn against ALL candidates. Platform
    # building only creates walkable land - it is NO commitment to the mine site. If a
    # better (land) field has meanwhile been discovered, it wins; the finished
    # platformed area competes as a normal land candidate and is built as soon as it
    # is (by demand/score) again the best target.
    if held is not None and chain_running and gs.mine_covering(tuple(held)) is None:
        tx, ty = held
        ttype, tamount = "metal", 0
        for r in gs.explored_resources():
            if (r.get("x"), r.get("y")) == (tx, ty):
                ttype, tamount = r.get("type", "metal"), r.get("amount", 0)
                break
        log(f"  [Modus] EXPANSION. Ziel ({tx},{ty}) {ttype} "
            f"(gehalten - Plattform-Kette laeuft).")
    else:
        target = gs.expansion_target(blocked_fields=_EXPANSION_REJECTED,
                                     force_type=force, min_metal=force_min_metal)
        if target is None:
            _HELD_EXPANSION_GOAL.pop("goal", None)
            _HELD_EXPANSION_GOAL.pop("platmine", None)
            return gs
        tx, ty, ttype, tamount, tscore = target
        # target change -> discard the remembered mine area of the old target,
        # otherwise a stale platmine area would hang on to the new target.
        if _HELD_EXPANSION_GOAL.get("goal") != (tx, ty):
            _HELD_EXPANSION_GOAL.pop("platmine", None)
        _HELD_EXPANSION_GOAL["goal"] = (tx, ty)
        log(f"  [Modus] EXPANSION. Ziel ({tx},{ty}) {ttype} Menge={tamount} score={tscore:.2f}")

    mine_sid = gs.MINE_SID

    # WATER-BLOCKED DEPOSIT: can NO mine be built directly at the target (normal
    # mine_build_position = None), but could a 2x2 position be made buildable by WATER
    # PLATFORMS? Then a pioneer first builds the missing platforms on the water fields
    # of the (fixed) mine area, before the constructor builds the mine.
    direct_pos = gs.mine_build_position((tx, ty), target_type=ttype)
    platform_fields = []
    if direct_pos is None:
        # keep the ONCE-chosen mine area STABLE across TURNS until the platforms are
        # finished. IMPORTANT: do not store it in the per-turn dict 'targets' (that is
        # re-initialised every turn in run_turn -> memory gone -> the area is
        # re-guessed and WANDERS between turns, already-built platforms no longer fit,
        # the chain never closes, the platform count oscillates 4->3->2->1->4...).
        # Instead store it in the persistent _HELD_EXPANSION_GOAL["platmine"], coupled
        # to the current target.
        held_pm = _HELD_EXPANSION_GOAL.get("platmine")
        plat_mine_pos = None
        if (isinstance(held_pm, dict)
                and tuple(held_pm.get("goal", ())) == (tx, ty)
                and held_pm.get("pos") is not None):
            plat_mine_pos = tuple(held_pm["pos"])
        # do NOT recompute just because platform_fields_needed temporarily returns
        # None (e.g. a field is currently occupied by an own unit). The held area is
        # only given up if a mine already stands there (mine_covering) - then the
        # platform part is done anyway.
        if plat_mine_pos is not None and gs.mine_covering(plat_mine_pos) is not None:
            plat_mine_pos = None
            _HELD_EXPANSION_GOAL.pop("platmine", None)
        if plat_mine_pos is None:
            plat_mine_pos = gs.mine_build_position_with_platforms((tx, ty), target_type=ttype)
        if plat_mine_pos is not None:
            pf = gs.platform_fields_needed(plat_mine_pos)
            if pf:
                platform_fields = pf
                _HELD_EXPANSION_GOAL["platmine"] = {"goal": (tx, ty),
                                                    "pos": list(plat_mine_pos)}
                log(f"  [Modus] EXPANSION: Ziel ({tx},{ty}) durch Wasser blockiert "
                    f"-> {len(pf)} Wasserplattform(en) noetig auf {plat_mine_pos}.")
            elif pf == []:
                # all platforms stand - remember the area so that the constructor
                # builds the mine exactly there (do not re-guess the area).
                _HELD_EXPANSION_GOAL["platmine"] = {"goal": (tx, ty),
                                                    "pos": list(plat_mine_pos)}
            # pf is None (temporarily invalid): do NOT discard the area, simply do
            # nothing this turn - check again next turn.

    # does a mine (possibly not yet connected) already stand at the target? Then the
    # constructor part is done - only the coupling chain is missing (which part A
    # covers as net repair anyway). Do NOT send a second constructor.
    mine_here = gs.mine_covering((tx, ty))

    # --- platform build (before the constructor), if water-blocked -----------
    # A pioneer is assigned ALL missing platforms as a CHAIN (the fields are thereby
    # blocked for others). It loads the material for the whole chain once and builds
    # them one after another - no return trips.
    if platform_fields and mine_here is None:
        # is a pioneer already assigned to this platform chain?
        sid_plat = gs.building_sid_by_name("platform")
        chain_pio = None
        for v in gs.vehicles_of_type("engineer"):
            if _PLAN.tasks_for(v["id"]) and any(
                    t.sid == sid_plat for t in _PLAN.tasks_for(v["id"])):
                chain_pio = v
                break
        if chain_pio is None:
            plat_pios = _free_units(gs, "engineer", claim)
            if plat_pios:
                chain_pio = plat_pios[0]
                items = [(f, sid_plat, gs.build_cost(sid_plat) or 2, "platform")
                         for f in platform_fields]
                _PLAN.assign_chain(chain_pio["id"], items, by_distance=True)
                log(f"  [Modus] EXPANSION: Pionier {chain_pio['id']} bekommt "
                    f"{len(items)} Plattformen als Kette (Material fuer alle).")
        if chain_pio is not None:
            claim.add(chain_pio["id"])
            gs = _build_platform_chain(gs, conn, chain_pio)

        # PARALLEL: while the pioneer builds the platforms, already prepare a free
        # constructor (load fully + drive after it to the area), so that it builds the
        # mine WITHOUT delay as soon as the platforms stand.
        free_cons_water = _free_units(gs, "constructor", claim)
        if free_cons_water:
            con_w = free_cons_water[0]
            claim.add(con_w["id"])
            gs = emergency_construction_water(gs, conn, con_w, (tx, ty), mine_sid,
                                              rejected_targets=_EXPANSION_REJECTED,
                                              target_type=ttype)

    # --- decision 1: load the constructor fully and send it to the target ---------
    # Only if NO platforms are open any more (otherwise the mine cannot stand).
    if mine_here is None and not platform_fields:
        free_cons = _free_units(gs, "constructor", claim)
        if free_cons:
            con = free_cons[0]
            claim.add(con["id"])
            gs = _expansion_send_constructor(gs, conn, con, (tx, ty), mine_sid,
                                             blocked, targets,
                                             rejected_targets=_EXPANSION_REJECTED,
                                             target_type=ttype)

    # --- decision 2: pioneers for the coupling chain ----------------------
    # ONLY if no platforms are open any more. As long as water platforms are still
    # missing, the target field is open water and UNREACHABLE - a coupling "towards
    # the target" never finds the target, builds a different edge field every turn and
    # produces a proliferating connector mesh (instead of a line). The actual
    # connection is done by the net repair (part A) anyway, once the platformed area /
    # the mine exists as its own island.
    # Expansion quota: 30% (min. 1) of what is still free after the repair (part A).
    # On scarcity the repair has already occupied the pioneers -> none remains here ->
    # repair before expansion.
    free_pios = _free_units(gs, "engineer", claim)
    if free_pios and not platform_fields:
        allowed = quota
        # surplus ones may help if no emergency is pending
        if no_emergency:
            allowed = len(free_pios)
        use = free_pios[:max(1, allowed)]
        for pio in use:
            claim.add(pio["id"])
            gs = _expansion_build_connector(gs, conn, pio, (tx, ty), blocked, targets)

    return gs


def clear_units_from_fields(gs, conn, fields, except_id=None):
    """CONSTRUCTOR PRIORITY: sends own MOBILE units (mainly pioneers) standing on one
    of the 'fields' away to a free neighbour field - even if they are currently
    building (then first finishBuild/abort, then evade). The constructor has priority
    in movement and building; pioneers must give way.
    except_id: do NOT send this unit (the building constructor itself) away.
    Returns (gs, moved_any)."""
    fields = set(fields)
    moved = False
    for v in list(gs.my_vehicles()):
        if v["id"] == except_id:
            continue
        vpos = gs.pos(v)
        if vpos not in fields:
            continue
        # unit is in the way. Look for a free neighbour field OUTSIDE the blocked
        # fields (unit-specifically walkable).
        occ = gs.occupied_fields()
        target = None
        for (nx, ny) in gs.neighbors8(*vpos):
            if (nx, ny) in fields or not gs.in_bounds(nx, ny):
                continue
            if gs.is_free_for_unit(v, nx, ny, occ=occ, ignore={vpos}):
                target = (nx, ny)
                break
        if target is None:
            continue
        # is the unit currently building? Then first detach from the build
        # (finishBuild), otherwise drive away normally.
        if v.get("isBuilding") and (v.get("buildTurns", 0) or 0) == 0:
            for esc in gs.escape_candidates(vpos, unit=v):
                if esc in fields:
                    continue
                ok, _ = conn.do({"type": "finishBuild", "unitId": v["id"],
                                 "escapePosition": list(esc)})
                if ok:
                    moved = True
                    gs = conn.refresh_state() or gs
                    break
        else:
            ok, _ = conn.do({"type": "move", "unitId": v["id"], "target": list(target)})
            if ok:
                log(f"  Konstrukteur-Vorrang: Einheit {v['id']} weicht {vpos}->{target}.")
                moved = True
                gs = conn.refresh_state() or gs
    return gs, moved


def emergency_construction_water(gs, conn, con, goal, mine_sid,
                                 rejected_targets=None, target_type=None):
    """ENCAPSULATED & REUSABLE: prepare and keep ready a constructor for a WATER mine
    in parallel to the platform chain.

    Idea: while a pioneer builds the missing water platforms of the mine area, the
    constructor should NOT wait idle at the base, but already load its material (full
    60 ore) and - once full - drive after it to the mine area and stand there ready.
    Once the platforms are finished (platform_fields_needed empty), it builds the mine
    WITHOUT delay (no more load-first-then-drive).

    Constructor and pioneer are amphibious (factorSea>0), so the constructor can
    already drive over water up to the (still-unplatformed) area. It positions itself
    on an approach field NEXT TO the area (never on a platform/mine field, to avoid
    blocking the pioneer).

    Parameters like _expansion_send_constructor. Returns the fresh gs.
    The caller has already claimed the constructor via claim."""
    if rejected_targets is None:
        rejected_targets = set()
    cid = con["id"]
    cap = gs.store_max(con)

    # determine the target 2x2 area (buildable with platforms) and fix it for this
    # constructor - the same memory logic as send_constructor, so that the area does
    # not wander while the surveyor keeps exploring.
    mem = _CONSTRUCTOR_MINE_POS.get(cid)
    if mem is not None and tuple(mem.get("goal", ())) == tuple(goal):
        mine_pos = tuple(mem["mine_pos"])
    else:
        mine_pos = gs.mine_build_position_with_platforms(goal, target_type=target_type)
        if mine_pos is None:
            log(f"  Notfall-Wasserbau: keine plattformierbare Minenflaeche an "
                f"{goal} - Ziel verworfen.")
            rejected_targets.add(goal)
            return gs
        _CONSTRUCTOR_MINE_POS[cid] = {"goal": tuple(goal), "mine_pos": list(mine_pos)}

    # (Formerly here: set the hard ore lock. Removed - the first mine is secured via
    # the backlog priority + reserve, not via an emergency lock.)

    # are platforms still outstanding? Then this is the PARALLEL standby phase.
    pf = gs.platform_fields_needed(mine_pos)
    if pf == []:
        # platforms finished (or area directly buildable) -> from here the normal
        # build logic takes over (load/approach/build). One call, one source of truth
        # - no duplication of the startBuild steps.
        return _expansion_send_constructor(gs, conn, con, goal, mine_sid,
                                           blocked=None, targets=None,
                                           rejected_targets=rejected_targets,
                                           target_type=target_type)
    if pf is None:
        # area currently invalid (e.g. a field briefly occupied) - force nothing this
        # turn, check again next turn. Do NOT discard the target.
        return gs

    mcells = [(mine_pos[0], mine_pos[1]), (mine_pos[0]+1, mine_pos[1]),
              (mine_pos[0], mine_pos[1]+1), (mine_pos[0]+1, mine_pos[1]+1)]
    cpos = gs.pos(con)
    stored = gs.stored(con)

    # 1. NOT YET FULL -> reload at the base (or at the dock). Exactly like
    #    send_constructor: at the ore nest there is no network connection, so load
    #    fully BEFORE the drive, otherwise the mine cannot be built later.
    if stored < cap:
        anchor = adjacent_networked_building(gs, con, need_metal=True)
        if anchor is None:
            dock = dock_field_at_base(gs, con)
            if dock is not None and cpos != dock:
                ok_mv, _ = conn.do({"type": "move", "unitId": cid, "target": list(dock)})
                if ok_mv:
                    gs, con = _reget(gs, conn, cid)
                    if con is None:
                        return gs
                    cpos = gs.pos(con)
                    anchor = adjacent_networked_building(gs, con, need_metal=True)
        if anchor is not None:
            want = min(cap - gs.stored(con), ore_available_for(gs, cid))
            if want > 0:
                ok, _ = conn.do({"type": "transfer", "unitId": anchor["id"],
                                 "targetId": cid, "amount": want, "resource": "metal"})
                if ok:
                    gs, con = _reget(gs, conn, cid)
                    if con is None:
                        return gs
                    stored = gs.stored(con); cpos = gs.pos(con)
                    log(f"  Notfall-Wasserbau: Konstrukteur laedt vor (jetzt {stored}/"
                        f"{cap}) - faehrt der Plattform-Kette hinterher.")
        # while not full: keep collecting at the base (no premature setting off).
        if stored < cap:
            return gs

    # 2. FULLY LOADED -> already drive to the mine area now and stand ready NEXT TO
    #    it (never step onto a platform/mine field). Once the platforms are finished,
    #    the next call (pf==[]) builds the mine immediately.
    next_to = any(abs(cpos[0]-cx) <= 1 and abs(cpos[1]-cy) <= 1 for cx, cy in mcells)
    if next_to:
        # already in position - ready. Do nothing more this turn (platforms still
        # missing); the pioneer builds, the constructor waits ready for action.
        return gs
    cands = []
    for (cx, cy) in mcells:
        for (nx, ny) in gs.neighbors8(cx, cy):
            if (nx, ny) in mcells or not gs.in_bounds(nx, ny):
                continue
            if gs.is_free_for_unit(con, nx, ny):
                cands.append((nx, ny))
    if not cands:
        # no free approach field (all water without platform / occupied) - the
        # constructor cannot get close yet. Do not discard, just wait.
        return gs
    cands.sort(key=lambda c: (c[0]-cpos[0])**2 + (c[1]-cpos[1])**2)
    approach = cands[0]
    ok_mv, _ = conn.do({"type": "move", "unitId": cid, "target": list(approach)})
    if ok_mv:
        log(f"  Notfall-Wasserbau: Konstrukteur faehrt voll geladen schon zu "
            f"{approach} (neben Mine {mine_pos}), wartet auf fertige Plattformen.")
        record_build_move(conn, gs, con, approach)
        return conn.refresh_state() or gs
    return gs


def _expansion_send_constructor(gs, conn, con, goal, mine_sid, blocked, targets,
                                rejected_targets=None, target_type=None):
    """Load the constructor fully (60), drive next to the mine, build the mine.
    goal: the ore-deposit field. target_type: target resource (metal/oil/gold) - the
    mine is placed so that the sum of this resource over its 2x2 area is maximal (on a
    tie the max. total sum). If the target is unreachable, it is entered into
    rejected_targets (discarded)."""
    if rejected_targets is None:
        rejected_targets = set()
    cid = con["id"]
    cap = gs.store_max(con)
    stored = gs.stored(con)
    cpos = gs.pos(con)

    # determine the best 2x2 mine position ONCE (max. yield) and hold it in the
    # targets memory. Do not recompute every turn: while the constructor drives, the
    # surveyor keeps exploring and the "best" position could otherwise wander. The
    # identified 2x2 with the highest yield stays the build goal until the build
    # succeeds or the target is discarded.
    mem = _CONSTRUCTOR_MINE_POS.get(cid)
    if mem is not None and tuple(mem.get("goal", ())) == tuple(goal):
        mine_pos = tuple(mem["mine_pos"])
    else:
        mine_pos = gs.mine_build_position(goal, builder=con, target_type=target_type)
        if mine_pos is None:
            # not directly (land) buildable - but if the water platforms already
            # stand, the area is buildable via mine_build_position_with_platforms. Do
            # NOT reject the constructor just because the land variant returns None
            # (water mine on finished platforms).
            mine_pos = gs.mine_build_position_with_platforms(goal, target_type=target_type)
        if mine_pos is None or gs.platform_fields_needed(mine_pos):
            # really no build site OR platforms are still missing -> the constructor
            # cannot build now. Do not discard if only platforms are missing (the
            # pioneers build them) - then do nothing this turn. Only discard if there
            # is NO valid area at all.
            if mine_pos is None:
                log(f"  Expansion: kein gueltiger Minen-Bauplatz an {goal} - Ziel verworfen.")
                rejected_targets.add(goal)
            return gs
        _CONSTRUCTOR_MINE_POS[cid] = {"goal": tuple(goal),
                                      "mine_pos": list(mine_pos)}
        log(f"  Expansion: Minen-Bauplatz {mine_pos} fuer max. Ausbeute fixiert "
            f"(Ziel {goal} {target_type}).")

    # (Formerly here: set the hard ore lock. Removed - the first mine is secured via
    # the backlog priority + reserve, not via an emergency lock.)

    # CROSS-MODE PRIORITY (one planner): the mine (2x2) must NOT be placed on a build
    # site that a higher-priority project (emergency) has already reserved in the
    # plan. Otherwise the lower-priority expansion displaces e.g. the small factory
    # from its field (problem A). If the planned mine area collides with the plan ->
    # discard the target, the emergency keeps the field. (The constructor is thereby
    # free for emergency tasks this turn.)
    # (Formerly here: check whether the mine area collides with the emergency _PLAN,
    # and discard the target with emergency priority. Removed - the emergency no
    # longer builds/reserves a mine; this check discarded the first mine for no
    # reason.)

    # 1. determine the position relative to the planned 2x2 mine area.
    mcells = [(mine_pos[0], mine_pos[1]), (mine_pos[0]+1, mine_pos[1]),
              (mine_pos[0], mine_pos[1]+1), (mine_pos[0]+1, mine_pos[1]+1)]
    on_footprint = cpos in mcells   # stands ON the area -> must get off
    next_to = (not on_footprint) and any(
        abs(cpos[0]-cx) <= 1 and abs(cpos[1]-cy) <= 1 for cx, cy in mcells)

    # 1a. if the constructor stands ON the mine area, the mine cannot be built there
    #     (the builder blocks its own build field). First step onto a free neighbour
    #     field of the area, then build next turn.
    if on_footprint:
        step = None
        for (cx, cy) in mcells:
            for (nx, ny) in gs.neighbors8(cx, cy):
                if (nx, ny) in mcells or not gs.in_bounds(nx, ny):
                    continue
                if gs.is_free_for_unit(con, nx, ny, ignore={cpos}):
                    step = (nx, ny)
                    break
            if step:
                break
        if step is not None:
            ok_mv, _ = conn.do({"type": "move", "unitId": cid, "target": list(step)})
            if ok_mv:
                log(f"  Expansion: Konstrukteur tritt von Minenflaeche auf {step}.")
                return conn.refresh_state() or gs
        log(f"  Expansion: Konstrukteur steht auf Minenflaeche {mine_pos}, "
            f"kein freies Nachbarfeld - Ziel verworfen.")
        rejected_targets.add(goal)
        _CONSTRUCTOR_MINE_POS.pop(cid, None)
        return gs

    mine_cost = gs.build_cost(mine_sid) or 24
    if next_to and stored >= mine_cost:
        # CONSTRUCTOR PRIORITY: do own pioneers/units stand on the mine area (e.g.
        # because they build couplings there)? First send them away, then build -
        # otherwise the build fails on "build position blocked".
        gs, cleared = clear_units_from_fields(gs, conn, mcells, except_id=cid)
        if cleared:
            gs, con = _reget(gs, conn, cid)
            if con is None:
                return gs
        ok, reason = conn.do({"type": "startBuild", "unitId": cid,
                              "buildingId": [1, mine_sid], "speed": -1,
                              "position": list(mine_pos)})
        if ok:
            log(f"  Expansion: Mine-Bau gestartet auf {mine_pos} (max speed).")
            _CONSTRUCTOR_MINE_POS.pop(cid, None)
            return conn.refresh_state() or gs
        if reason and "moving" not in reason:
            # build rejected. If we JUST sent own units away from the area (cleared)
            # OR the area would be buildable without own mobile units, the blockage is
            # only TEMPORARY (the units clear the field - possibly finished only this
            # turn). Then do NOT discard: keep the order, try again next turn. The
            # planner monitors the task every turn; a momentary "build position
            # blocked" by own evading units must not destroy the target. Only discard
            # on a REAL, remaining blocker (terrain/fixed building/foreign unit).
            occ_no_own = gs.occupied_fields_for_mine()
            buildable_wo_own = all(
                gs.is_buildable_for_building(mine_sid, cx, cy, occ=occ_no_own)
                for (cx, cy) in mcells)
            if cleared or buildable_wo_own:
                log(f"  Expansion: Mine-Bau auf {mine_pos} momentan blockiert "
                    f"(eigene Einheit weicht) - warte, Ziel bleibt.")
                return gs
            log(f"  Expansion: Mine-Bau abgelehnt ({reason}) - Ziel verworfen.")
            rejected_targets.add(goal)
            _CONSTRUCTOR_MINE_POS.pop(cid, None)
            return gs
        log(f"  Expansion: Mine-Bau abgelehnt ({reason}).")
        return gs

    # 2. not yet full? -> reload at the base (full = cap).
    if stored < cap:
        anchor = adjacent_networked_building(gs, con, need_metal=True)
        if anchor is None:
            dock = dock_field_at_base(gs, con)
            if dock is not None and cpos != dock:
                ok_mv, _ = conn.do({"type": "move", "unitId": cid, "target": list(dock)})
                if ok_mv:
                    gs, con = _reget(gs, conn, cid)
                    if con is None:
                        return gs
                    cpos = gs.pos(con)
                    anchor = adjacent_networked_building(gs, con, need_metal=True)
        if anchor is not None:
            want = min(cap - gs.stored(con), ore_available_for(gs, cid))
            if want > 0:
                ok, _ = conn.do({"type": "transfer", "unitId": anchor["id"],
                                 "targetId": cid, "amount": want, "resource": "metal"})
                if ok:
                    gs, con = _reget(gs, conn, cid)
                    if con is None:
                        return gs
                    stored = gs.stored(con); cpos = gs.pos(con)
                    log(f"  Expansion: Konstrukteur geladen, jetzt {stored}.")

    # 3. drive next to the mine area - but ONLY when fully loaded (cap=60).
    #    Reason: at the ore nest there is no network connection to reload, and the
    #    mine costs up to ~60 metal at max speed. If it sets off half-full, it can
    #    never build the mine and gets stuck. While not full -> wait at the base and
    #    keep collecting (step 2 reloads every turn).
    if stored >= cap and not next_to:
        approach = None
        cands = []
        for (cx, cy) in mcells:
            for (nx, ny) in gs.neighbors8(cx, cy):
                if (nx, ny) in mcells:
                    continue
                if not gs.in_bounds(nx, ny):
                    continue
                if gs.is_free_for_unit(con, nx, ny):
                    cands.append((nx, ny))
        if cands:
            cands.sort(key=lambda c: (c[0]-cpos[0])**2 + (c[1]-cpos[1])**2)
            approach = cands[0]
        if approach is None:
            log(f"  Expansion: kein Anfahrfeld an Mine {mine_pos} - Ziel verworfen.")
            rejected_targets.add(goal)
            _CONSTRUCTOR_MINE_POS.pop(cid, None)
            return gs
        ok_mv, _ = conn.do({"type": "move", "unitId": cid, "target": list(approach)})
        if ok_mv:
            log(f"  Expansion: Konstrukteur faehrt zu {approach} (neben Mine {mine_pos}).")
            # drive-pause build (like the pioneer): if the constructor reaches the
            # approach field still THIS turn, remember the travel-time pause so that
            # the second build phase (phase 5) lets it build the mine after arrival in
            # the SAME turn - instead of losing a whole turn just for driving there.
            gs, c2 = _reget(gs, conn, cid)
            if c2 is not None and tuple(gs.pos(c2)) != tuple(approach):
                record_build_move(conn, gs, c2, approach)
            return conn.refresh_state() or gs
        log(f"  Expansion: Konstrukteur-Fahrt zu {approach} abgelehnt - Ziel verworfen.")
        rejected_targets.add(goal)
        _CONSTRUCTOR_MINE_POS.pop(cid, None)
    return gs


def _expansion_build_connector(gs, conn, pio, goal, blocked, targets, connect_to=None):
    """Pioneer builds one coupling piece towards the target (load only 2 metal, 1
    piece/turn). Approaches the target via the nearest free field at the base edge
    towards goal. Simplified route logic (refinement later). connect_to: what counts
    as 'connected network' (passed on to find_connector_toward). For the net repair
    the main component, so that the chain does not connect to an isolated island;
    otherwise default (base_footprint)."""
    pid = pio["id"]
    ppos = gs.pos(pio)
    CONN_COST = 2
    # 1. enough metal (2) for one piece? Otherwise reload. If the loading fails (no
    #    anchor reachable / transfer rejected), do NOT build - otherwise the bridge
    #    rejects with "insufficient resources" and the pioneer blocks the quota
    #    without ever fetching material.
    if gs.stored(pio) < CONN_COST:
        anchor = adjacent_networked_building(gs, pio, need_metal=True)
        if anchor is None or main_subbase_metal(gs) < CONN_COST:
            # no ore anchor reachable OR the real SubBase pool is empty (the bridge
            # draws from the pool, not from the single anchor) -> dock back to the
            # base, load next turn. Prevents the endless 'source base has only 0'
            # rejection.
            dock = _dock_to_metal_source(gs, pio)
            if dock is not None and gs.pos(pio) != dock:
                ok_mv, _ = conn.do({"type": "move", "unitId": pid, "target": list(dock)})
                if ok_mv:
                    gs, _p = _reget(gs, conn, pid)
            return gs
        ok_tr, _ = conn.do({"type": "transfer", "unitId": anchor["id"], "targetId": pid,
                            "amount": CONN_COST, "resource": "metal"})
        gs, pio = _reget(gs, conn, pid)
        if pio is None:
            return gs
        # authoritative is the bridge's REPLY (ok_tr), NOT the re-read stock: in
        # lockstep the transfer is accepted, but the state tick with the pioneer's new
        # storageCur may not be through yet. Checking gs.stored wrongly reported
        # "could not load material" and wasted a turn although the transfer had
        # worked. Only if the transfer was REALLY rejected (ok_tr False) AND the stock
        # is still too small, do not build this turn. (The 2-metal load amount stays
        # unchanged.)
        if not ok_tr and gs.stored(pio) < CONN_COST:
            log(f"  Expansion: Pionier {pid} konnte kein Material laden (warte).")
            return gs
    # 2. build site for the next connector piece.
    conn_sid = gs.building_sid_by_name("connector")
    if conn_sid is None:
        return gs
    # target island = the network component that contains 'goal' (all its fields).
    # These fields are NEVER a coupling build field - the chain ends NEXT TO them and
    # connects the island. Otherwise the chain builds on the island building / the
    # mine build site and collides with the constructor (coupling on (50,49) towards
    # (50,49)).
    stop_island = set()
    for c in gs.network_components():
        if tuple(goal) in c:
            stop_island = c
            break
    spot = gs.find_connector_toward(pio, goal, avoid=blocked.setdefault(pid, set()),
                                    base_cells=connect_to, stop_island=stop_island)
    if spot is None:
        return gs
    # 3. drive there / build.
    if ppos != spot:
        ok_mv, mv_reason = conn.do({"type": "move", "unitId": pid, "target": list(spot)})
        if ok_mv:
            gs, pio = _reget(gs, conn, pid)
            if pio is None:
                return gs
            if gs.pos(pio) != spot:
                # set off, but not there yet -> movement pause (as elsewhere), so
                # that the pioneer arrives in the second build phase and builds.
                record_build_move(conn, gs, pio, spot)
                return conn.refresh_state() or gs
        else:
            # 'unit building'/'unit moving' = only BUSY (e.g. finishing the previous
            # coupling of the chain) - do NOT remember the field as blocked, otherwise
            # the pioneer permanently avoids its own next chain field. Only on a REAL
            # drive error (no path etc.) block the field.
            if mv_reason and ("building" in mv_reason or "moving" in mv_reason):
                return gs
            blocked.setdefault(pid, set()).add(spot)
            return gs
    ok, reason = conn.do({"type": "startBuild", "unitId": pid,
                          "buildingId": [1, conn_sid], "speed": -1,
                          "position": list(spot)})
    if ok:
        log(f"  Expansion: Kopplung gebaut auf {spot} Richtung {goal}.")
        # the build takes >= 1 turn; the unit is isBuilding afterwards. No driving on
        # in the same turn (the bridge rejects with 'unit building').
        return conn.refresh_state() or gs
    # rejection: a MATERIAL/TIMING reason (insufficient resources, the transfer tick
    # was not through yet; or still moving/building) is NOT a build-site error - the
    # field stays good, next turn it works. Only on a REAL build-site error (blocked
    # etc.) block the field so that another is chosen. Otherwise a brief material
    # timing error would permanently burn the coupling field.
    r = (reason or "").lower()
    transient = ("moving" in r or "building" in r or "resource" in r
                 or "insufficient" in r or "metal" in r)
    if reason and not transient:
        log(f"  Expansion: Kopplung abgelehnt ({reason}) auf {spot}.")
        blocked.setdefault(pid, set()).add(spot)
    return gs


# ===========================================================================
# NET REPAIR via ENGINE PATH (encapsulated; replaces the removed _run_network_repair)
# ---------------------------------------------------------------------------
_CONN_COST = 2          # material per coupling
_PIONEER_MAX_LOAD = 40  # pioneer carrying capacity -> max. 20 couplings at a stretch


def _straight_runs(fields):
    """Decomposes an ORTHOGONALLY connected field list into maximal STRAIGHT runs
    (same axis direction between consecutive fields). Each run is a list of fields; a
    new run begins at the bend (axis change). Example: [(0,0),(1,0),(2,0),(2,1),(2,2)]
    -> [[(0,0),(1,0),(2,0)], [(2,0),(2,1),(2,2)]] (the bend point (2,0) belongs to
    both, so that the runs connect seamlessly)."""
    if len(fields) < 2:
        return [list(fields)] if fields else []
    runs = []
    cur = [fields[0]]
    cur_dir = None
    for i in range(1, len(fields)):
        px, py = fields[i - 1]
        x, y = fields[i]
        d = (x - px, y - py)
        if cur_dir is None or d == cur_dir:
            cur.append((x, y))
            cur_dir = d
        else:
            runs.append(cur)
            cur = [(px, py), (x, y)]   # bend point starts the new run
            cur_dir = d
    runs.append(cur)
    return runs


def _path_build_segment(gs, conn, pio, seg_start, seg_end, conn_sid):
    """SUB-ENCAPSULATED PATH function: lets the pioneer build ONE straight,
    orthogonal segment from seg_start to seg_end via the engine PATH build.

    Flow (across turns):
      1. is a PATH already running (bandPosition set)? -> "running", do nothing.
      2. pioneer not on seg_start? -> drive there (travel-time pause), "moving".
      3. enough material for the WHOLE segment? A PATH can NOT reload in between (the
         engine aborts on empty material). So load len(segment)*_CONN_COST in advance
         (capped at _PIONEER_MAX_LOAD and at the real pool). If not enough -> load,
         "loading".
      4. issue the PATH: startBuild with pathEnd=seg_end. -> "started".

    Returns a status string: "running" | "moving" | "loading" | "started" | "done" |
    "blocked". "done" = pioneer stands at seg_end and no PATH runs any more. "blocked"
    = could not start (no material/pool)."""
    pid = pio["id"]
    seg_start = tuple(seg_start)
    seg_end = tuple(seg_end)
    # segment length in fields (number of couplings to build along the straight).
    seg_len = abs(seg_end[0] - seg_start[0]) + abs(seg_end[1] - seg_start[1]) + 1

    # 1. is a PATH already running for this unit?
    if pio.get("bandPosition"):
        return "running"

    # PATH finished/off: does the pioneer stand at the segment end and no longer build?
    if tuple(gs.pos(pio)) == seg_end and not pio.get("isBuilding"):
        return "done"

    # 2. drive to the segment start, if not there yet.
    if tuple(gs.pos(pio)) != seg_start:
        ok_mv, mv_reason = conn.do({"type": "move", "unitId": pid,
                                    "target": list(seg_start)})
        if ok_mv:
            gs, p = _reget(gs, conn, pid)
            if p is not None and tuple(gs.pos(p)) != seg_start:
                record_build_move(conn, gs, p, seg_start)   # real travel-time pause
        return "moving"

    # 3. secure material for the WHOLE segment (no reloading during PATH). The
    #    transfer is an IMMEDIATE action - afterwards the PATH can start in the SAME
    #    turn (step 4 falls straight through). NO early 'return loading' if the
    #    material is enough after the transfer: otherwise the pioneer stays
    #    loaded-idle (no _PENDING_BUILD_MOVE on a pure transfer -> no second build
    #    phase) and the idle check (phase 8) wrongly pulls it out of its assignment.
    need = min(seg_len * _CONN_COST, _PIONEER_MAX_LOAD)
    have = gs.stored(pio)
    if have < need:
        anchor = adjacent_networked_building(gs, pio, need_metal=True)
        if anchor is None:
            return "blocked"   # no ore anchor reachable -> nothing this turn
        want = min(need - have, main_subbase_metal(gs))
        if want <= 0:
            return "blocked"   # pool empty -> wait (no shuttling, stays allocated)
        conn.do({"type": "transfer", "unitId": anchor["id"], "targetId": pid,
                 "amount": want, "resource": "metal"})
        gs, p = _reget(gs, conn, pid)
        if p is not None:
            pio = p
        if gs.stored(pio) < need:
            return "loading"   # not enough yet (pool gave less) -> next turn

    # 4. issue the PATH: the engine builds the whole straight segment itself.
    ok, _r = conn.do({"type": "startBuild", "unitId": pid,
                      "buildingId": [1, conn_sid], "speed": -1,
                      "position": list(seg_start), "pathEnd": list(seg_end)})
    if ok:
        log(f"  PATH: Kopplungs-Segment {seg_start}->{seg_end} ({seg_len} Felder) gestartet.")
        return "started"
    return "blocked"


def _run_network_repair_path(gs, conn, claim):
    """NEW STRETCH FUNCTION (encapsulated): builds the network connection via the
    engine PATH build wherever a straight piece is >= 3 fields long; short pieces
    (< 3) via the mini field-by-field fallback.

    - Persistence: the assigned pioneer stays BOUND to the task "net_repair" via
      _UNIT_ALLOC until the connection stands or the emergency releases it.
    - Commit: a once-started PATH segment is NOT re-decided until it is finished (no
      back-and-forth).
    - Obstacle: own mobile units on the stretch are cleared aside in advance (the
      engine does not clear on PATH). Own buildings are part of the stretch
      (repair_route_segments). Foreign -> the stretch is recomputed (the next call
      fetches fresh segments)."""
    gap = gs.network_gap_target()
    if gap is None:
        # network connected -> task done, release allocation(s).
        held = alloc_units_for("net_repair")
        if held:
            log(f"  [NetzDiag] gap=None -> Netz verbunden, loese Allokation(en) {held}.")
        for uid in held:
            alloc_release(uid)
        return gs
    from_cell, to_cell, _d = gap
    conn_sid = gs.building_sid_by_name("connector")

    # ordered, orthogonal remaining-field list via the EXISTING logic.
    segs = gs.repair_route_segments(from_cell, to_cell)
    log(f"  [NetzDiag] gap {from_cell}->{to_cell}, segs={len(segs)} {segs[:8]}"
        f"{'...' if len(segs) > 8 else ''}, alloc={alloc_units_for('net_repair')}")
    if not segs:
        log(f"  [NetzDiag] segs leer -> keine baubare Strecke, nichts zu tun "
            f"(Allokation bleibt).")
        return gs

    # determine the pioneer: reuse an already-assigned one (persistence), otherwise
    # newly assign the nearest free one.
    pid = None
    for uid in alloc_units_for("net_repair"):
        _g, p = _reget(gs, conn, uid)
        if p is not None:
            pid = uid
            break
        else:
            log(f"  [NetzDiag] allokierter Pionier {uid} nicht mehr im State.")
    if pid is None:
        free = _pick_free_for(gs, "engineer", claim, for_task="net_repair")
        if not free:
            # diagnostic: why is no pioneer free? Show the reason per pioneer.
            detail = []
            for v in gs.vehicles_of_type("engineer"):
                vid = v["id"]
                a = _UNIT_ALLOC.get(vid)
                detail.append(f"{vid}(build={bool(v.get('isBuilding'))},"
                              f"claim={vid in claim},"
                              f"alloc={a.get('task') if a else None}/"
                              f"started={a.get('started') if a else None},"
                              f"core={_CORE_TASK.get(vid)})")
            log(f"  [NetzDiag] kein freier Pionier (claim={sorted(claim)}) -> warten. "
                f"Pioniere: {'; '.join(detail) if detail else 'KEINE'}")
            return gs
        # within the core-task preference (own first, then floating, then stand-in)
        # choose the nearest. Stable sort: first distance, then preference rank ->
        # preference dominates, distance decides on a tie.
        rank = {v["id"]: i for i, v in enumerate(free)}
        free.sort(key=lambda v: (rank[v["id"]],
                                 (gs.pos(v)[0] - segs[0][0]) ** 2
                                 + (gs.pos(v)[1] - segs[0][1]) ** 2))
        pid = free[0]["id"]
        alloc_set(pid, "net_repair", payload={}, started=False)
        log(f"  [NetzDiag] neuer Pionier {pid} fuer net_repair zugeordnet "
            f"(Kernaufgabe {_CORE_TASK.get(pid)}).")
    claim.add(pid)
    _touch(pid)   # the repair processes this pioneer this turn (even if it only
                  # loads/waits) -> the stuck-clear (phase 8) leaves it alone.

    gs, pio = _reget(gs, conn, pid)
    if pio is None:
        alloc_release(pid)
        return gs

    # clear own mobile units on the remaining stretch aside in advance (otherwise a
    # PATH aborts at the first own vehicle). The pioneer itself excepted.
    gs, _moved = clear_units_from_fields(gs, conn, set(segs), except_id=pid)
    gs, pio = _reget(gs, conn, pid)
    if pio is None:
        alloc_release(pid)
        return gs

    # decompose into straight runs and process the FIRST still-open run.
    runs = _straight_runs(segs)
    if not runs:
        return gs
    run0 = runs[0]
    # material limit: cap a PATH segment at max. _PIONEER_MAX_LOAD/_CONN_COST fields
    # (very rarely relevant - 20 couplings).
    max_fields = _PIONEER_MAX_LOAD // _CONN_COST
    if len(run0) > max_fields:
        run0 = run0[:max_fields]

    log(f"  [NetzDiag] Pionier {pid} @ {tuple(gs.pos(pio))}, isBuilding="
        f"{bool(pio.get('isBuilding'))}, bandPos={pio.get('bandPosition')}, "
        f"run0={run0} ({'PATH' if len(run0) >= 3 else 'MINI'}).")

    if len(run0) >= 3:
        # >= 3 straight fields -> engine PATH. seg_start = first, seg_end = last.
        status = _path_build_segment(gs, conn, pio, run0[0], run0[-1], conn_sid)
        log(f"  [NetzDiag] PATH-Segment-Status: {status}.")
        if status in ("started", "running", "moving", "loading"):
            alloc_mark_started(pid)   # bound from now on
        return gs
    else:
        # < 3 fields -> mini field-by-field (own lean variant, no coupling to the old
        # logic). Builds the network-nearest open field and drives on to the next
        # field of the run in the same turn (travel-time pause), so that the second
        # build phase builds it - consistent with the PATH/platform build.
        gs = _mini_connector_step(gs, conn, pio, run0, conn_sid)
        alloc_mark_started(pid)
        return gs


def _mini_connector_step(gs, conn, pio, run, conn_sid):
    """MINI FALLBACK for short (< 3) runs: builds the coupling fields of 'run' (field
    list, network-near -> island-near) field by field, ONE build start per turn.

    Flow across turns (one field needs: drive there + 1 turn build time):
      - is the unit still isBuilding (the previous turn's build runs)? -> wait.
      - otherwise look for the first field WITHOUT a coupling. Not there? -> drive
        there (travel-time pause record_build_move), build only after arrival.
      - there and material < _CONN_COST? -> reload 2 material at the network edge.
      - there and material present? -> startBuild (single field). The build takes >= 1
        turn; the unit is isBuilding afterwards. NO driving on in the same turn (the
        bridge rejects that with 'unit building') - only the NEXT turn, when the
        coupling is finished, does it go on to the next field.
    Self-contained (no old logic). Returns gs."""
    pid = pio["id"]
    if pio.get("isBuilding") or pio.get("bandPosition"):
        return gs   # busy -> next turn

    # current build field = first field of the run on which NO coupling stands yet.
    spot = None
    for f in run:
        if not _connector_present(gs, tuple(f)):
            spot = tuple(f)
            break
    if spot is None:
        return gs   # run finished

    # drive there
    if tuple(gs.pos(pio)) != spot:
        ok_mv, mv_reason = conn.do({"type": "move", "unitId": pid, "target": list(spot)})
        if ok_mv:
            gs, p = _reget(gs, conn, pid)
            if p is not None and tuple(gs.pos(p)) != spot:
                record_build_move(conn, gs, p, spot)
        return gs

    # secure material for ONE field - and build directly in the SAME turn. The
    # transfer is an IMMEDIATE action (no turn build), afterwards the material is
    # there and the build can follow immediately (just like the PATH: loading ->
    # started in the same turn cycle). NO return between loading and building -
    # otherwise a whole turn is lost just for loading (observed at a 90-degree corner:
    # drive/load/build over three turns instead of one).
    if gs.stored(pio) < _CONN_COST:
        anchor = adjacent_networked_building(gs, pio, need_metal=True)
        if anchor is None or main_subbase_metal(gs) < _CONN_COST:
            return gs
        conn.do({"type": "transfer", "unitId": anchor["id"], "targetId": pid,
                 "amount": _CONN_COST, "resource": "metal"})
        gs, p = _reget(gs, conn, pid)
        if p is not None:
            pio = p
        if gs.stored(pio) < _CONN_COST:
            return gs   # transfer did not arrive after all -> next turn

    # build (single field, NO pathEnd)
    ok, _r = conn.do({"type": "startBuild", "unitId": pid,
                      "buildingId": [1, conn_sid], "speed": -1,
                      "position": list(spot)})
    if ok:
        log(f"  PATH-Fallback: Einzelkopplung auf {spot} gebaut.")
        # the coupling build takes >= 1 turn (buildTurns). The unit is now isBuilding
        # and stays so until the next turn change - driving on in the SAME turn is not
        # possible (the bridge rejects with 'unit building'). So only refresh the
        # state and return; next turn the function start looks for the next open
        # field, drives there and builds.
        gs = conn.refresh_state() or gs
    return gs


def _connector_present(gs, cell):
    """True if an own coupling (or a connectable own building) already stands on
    'cell' - then there is nothing more to build there."""
    cell = tuple(cell)
    me_id = gs.me.get("id") if gs.me else None
    for p in gs.model.get("players", []):
        if p.get("id") != me_id:
            continue
        for b in p.get("buildings", []):
            big = gs.is_big_building_type(gs.unit_type(b))
            if cell in gs.footprint(gs.pos(b), big):
                name = gs._static_by_sid.get(
                    (gs.unit_first(b), gs.unit_type(b)), {}).get("name", "")
                if name not in WATER_WALKABLE_BUILDINGS:
                    return True   # connector/connectable building stands here
    return False


def _dock_to_metal_source(gs, unit):
    """Finds a free field next to an own METAL-storing building (storage/mine) that
    the unit can dock to in order to load ore. Returns (x,y) or None. Prefers the
    nearest."""
    upos = gs.pos(unit)
    best = None
    for b in gs.my_buildings():
        st = gs._static_by_sid.get((gs.unit_first(b), gs.unit_type(b))) or {}
        if st.get("storeResType") != 1 or st.get("storageResMax", 0) <= 0:
            continue
        big = gs.is_big_building_type(gs.unit_type(b))
        for c in gs.footprint(gs.pos(b), big):
            for n in gs.neighbors8(*c):
                if gs.is_free_for_unit(unit, n[0], n[1]):
                    d = (n[0]-upos[0])**2 + (n[1]-upos[1])**2
                    if best is None or d < best[0]:
                        best = (d, n)
    return best[1] if best else None


def active_modes(gs):
    """MODE INTERFACE: returns the active modes with priority (smaller = higher).
    Modes are NOT exclusive (several can run at once) and feed into the phases; on a
    conflict over units/ore the priority decides (emergency always top, then
    expansion). defensive/offensive follow. Returns a list sorted by priority
    [(prio, name, payload), ...]."""
    modes = []
    emergency, reasons = gs.is_emergency()
    # HARD ORE LOCK - part "emergency stays active" and early lock-setting. Relies on
    # PERSISTENT memories (survive turns):
    #   - _CONSTRUCTOR_MINE_POS : the ore-mine constructor with a fixed target
    #   - _PLAN (platform chain): the pioneer that builds the water platforms
    # As long as the CONSTRUCTOR does not yet have a full 60 ore, the emergency stays
    # active. The lock is set here already (phase 1, before the emergency build phase
    # in phase 3), so that no other system gets ore this turn. Order in
    # ore_available_for: pioneer (platforms) first, then constructor. Takes effect
    # only with a FIXED target (the surveyor has found the field).
    # The EMERGENCY no longer builds a mine (it is bad at building). The first metal
    # mine after the starting mine is built by the new backlog logic, prioritised
    # there as a mandatory site on place 1 (ensure_first_metal_mine). Therefore NO
    # loading mine constructor keeps the emergency active any more - otherwise the
    # emergency stood forever if such a constructor never became full (symptom in the
    # log: 'ore-mine project still collecting ore', expansion starves).
    if emergency:
        modes.append((0, "emergency", reasons))
    # expansion is active as soon as any deposits are explored OR the network is
    # separated (then part A = net repair must run, even if nothing was explored yet -
    # e.g. when a constructor built a factory detached on a narrow island and the
    # pioneers first have to connect it). Priority 3.
    if gs.explored_resources() or gs.network_gap_target() is not None:
        modes.append((3, "expansion", None))
    # PLACEHOLDER: defensive (prio 1), offensive (prio 2)
    modes.sort(key=lambda m: m[0])
    return modes


def _open_finished_builders(gs):
    """Are there still finished-built builders sticking to their field?"""
    return any(v.get("isBuilding") and (v.get("buildTurns", 0) or 0) == 0
               for role in ("engineer", "constructor")
               for v in gs.vehicles_of_type(role))


def _reconcile_plan(gs):
    """Reconciles the persistent build plan against the current state: finished-built
    orders drop out, orders of dead builders drop out. Updates the plan WITHOUT
    executing actions. Called FIRST at turn start (preparation) - before the finished
    builders are pulled off the field, so that the plan knows which orders are already
    done."""
    own_ids = {v["id"] for v in gs.my_vehicles()}
    _PLAN.reconcile(gs,
                    object_present_fn=lambda f, s: _object_present(gs, f, s),
                    builder_exists_fn=lambda b: b in own_ids)


def _run_active_modes(gs, conn, modes, blocked, targets):
    """Runs the active mode handlers by priority (emergency first, then expansion).
    'claim' is the SHARED occupation note: unit IDs that a higher-priority mode has
    already claimed. This way the expansion takes only builders the emergency does not
    need (coexistence, priority decides). Returns the fresh gs."""
    claim = set()   # claimed unit IDs (cross-mode, this turn)

    # reconcile the plan against reality before re-planning.
    _reconcile_plan(gs)

    for prio, name, payload in modes:
        if name == "emergency":
            log("  [Modus] NOTFALL. Offene Gruende: " + "; ".join(payload))
            gs = plan_emergency(gs, conn, _PLAN, claim)   # BUILD PLANNING (fills _PLAN)
            gs = execute_plan(gs, conn, _PLAN)            # BUILD PHASE (executes)
        elif name == "expansion":
            gs = mode_expansion(gs, conn, blocked, targets, claim=claim)
            # defense screen: subordinate to expansion/storage, in the base_expansion
            # pot. Storage has priority (mode_expansion builds it before this point).
            # Only build if the metal threshold is met AND the third pot has any
            # defense work at all (gap in the screen). No open storage demand -> then
            # the pot may go into the defense.
            if (defense_build_allowed(gs)
                    and not _storage_need_open(gs)
                    and _defense_shield_has_gap(gs)):
                gs = run_defense_expansion(gs, conn, claim)
        # further modes (defensive/offensive) follow here later
    return gs


def shed_energy_load(gs, conn):
    """LOAD SHEDDING on an energy deficit (like a human player):
    If the running consumers need MORE energy than is produced, lower consumption
    instead of waiting pointlessly:
      1. First switch off energy-hungry FACTORIES (stopWork) - their build pauses
         until the energy situation is fixed.
      2. If that is not enough, switch off MINES - the ones with the LOWEST oil
         output first, so that the fuel-rich ones keep running and deliver oil for the
         later energy.
    Lowers the demand until demand <= production. Returns the (fresh) gs.
    Note: the server partly switches off power-less buildings itself; this logic gives
    the bot CONTROL over WHAT is switched off (oil-poor mines first).
    """
    prod = gs.energy_production()
    # running consumers (needsEnergy>0 and isWorking)
    consumers = []
    for b in gs.my_buildings():
        st = gs._static_by_sid.get((gs.unit_first(b), gs.unit_type(b)))
        ne = (st.get("needsEnergy", 0) if st else 0) or 0
        if ne > 0 and b.get("isWorking"):
            is_mine = (gs.unit_type(b) == gs.MINE_SID)
            consumers.append((b, ne, is_mine, b.get("oilProd", 0) or 0))
    need = sum(ne for _b, ne, _m, _o in consumers)
    if need <= prod:
        return gs   # no deficit

    # shutoff order: first CONSUMERS WITHOUT YIELD (factories, research, refineries -
    # cost energy, produce nothing), then MINES by ascending oil output (oil-poor
    # first). Mines are income sources and are switched off last.
    non_mines = [c for c in consumers if not c[2]]
    mines = sorted([c for c in consumers if c[2]], key=lambda c: c[3])
    order = non_mines + mines

    deficit = need - prod
    for (b, ne, is_mine, _oil) in order:
        if deficit <= 0:
            break
        ok, _ = conn.do({"type": "stopWork", "unitId": b["id"]})
        if ok:
            kind = "Mine" if is_mine else "Verbraucher"
            log(f"  Lastabwurf: {kind} {b['id']} abgeschaltet (spart {ne} Energie).")
            deficit -= ne
            gs = conn.refresh_state() or gs
    return gs


def restore_energy_load(gs, conn):
    """Counterpart to load shedding: switches switched-off consumers back on
    (startWork) - but ONLY if the energy is DURABLY enough. Otherwise an oscillation
    arises: shed switches the factory off (saves energy), restore sees the resulting
    'surplus' and switches it back on, shed off again, ... endless loop, the factory
    never produces.
    Solution: a switched-off consumer is only reactivated if the production covers the
    demand of the already RUNNING consumers PLUS this one. Since switching on/off does
    not change the production (only the consumption), that means: after switching on
    no deficit remains -> no renewed load shedding -> no oscillation. The energy
    emergency has priority: first enough energy is BUILT, then the consumers start up
    again. Mines with the HIGHEST oil output first (bring the most fuel)."""
    prod = gs.energy_production()
    used = 0
    stopped = []
    for b in gs.my_buildings():
        st = gs._static_by_sid.get((gs.unit_first(b), gs.unit_type(b)))
        ne = (st.get("needsEnergy", 0) if st else 0) or 0
        if ne > 0:
            if b.get("isWorking"):
                used += ne
            else:
                is_mine = (gs.unit_type(b) == gs.MINE_SID)
                stopped.append((b, ne, is_mine, b.get("oilProd", 0) or 0))
    if not stopped:
        return gs
    # reactivate ONLY MINES: mines are real on/off energy consumers and at the same
    # time income sources - they should run again as soon as possible.
    # factories/research/refineries are NOT reactivated here via startWork, but start
    # up again via their production logic (changeBuildList) once there is enough
    # energy - otherwise an on/off oscillation arises.
    mines = [c for c in stopped if c[2]]
    mines.sort(key=lambda c: -c[3])   # oil-richest mine first
    for (b, ne, is_mine, _oil) in mines:
        # only switch on if the production STABLY covers the demand of the
        # already-running consumers PLUS this mine (otherwise a deficit again at once).
        if prod < used + ne:
            continue
        ok, _ = conn.do({"type": "startWork", "unitId": b["id"]})
        if ok:
            log(f"  Energie zurueck: Mine {b['id']} wieder eingeschaltet.")
            used += ne
            gs = conn.refresh_state() or gs
    return gs


def retire_surplus_generators(gs, conn):
    """OIL-TRAP resolution, step 2: as soon as a STATION runs, switch off surplus
    GENERATORS (stopWork - do not demolish!). This saves oil (2 per generator each),
    which the efficient station (6 energy from 6 oil) uses much better. Only as many
    generators are switched off as the energy still covers the running demand (no
    collapse). Order in the turn: the station is built/runs first, THEN this step
    takes effect."""
    # is at least one station running?
    sid_station = gs.building_sid_by_name("Energy_Big")
    sid_gen = gs.building_sid_by_name("energy small")
    if sid_station is None or sid_gen is None:
        return gs
    stations_working = [b for b in gs.my_buildings()
                        if gs.unit_type(b) == sid_station and b.get("isWorking")]
    if not stations_working:
        return gs
    gens_on = [b for b in gs.my_buildings()
               if gs.unit_type(b) == sid_gen and b.get("isWorking")]
    if not gens_on:
        return gs
    prod = gs.energy_production()
    # running demand (consumers with needsEnergy>0, isWorking)
    need = 0
    for b in gs.my_buildings():
        st = gs._static_by_sid.get((gs.unit_first(b), gs.unit_type(b)))
        ne = (st.get("needsEnergy", 0) if st else 0) or 0
        if ne > 0 and b.get("isWorking"):
            need += ne
    # margin = production - demand. This many generators (1 energy each) can go
    # without dropping below the demand. Keeping one generator as a buffer is not
    # needed - the station carries the load.
    surplus = prod - need
    for g in gens_on:
        if surplus <= 0:
            break
        ok, _ = conn.do({"type": "stopWork", "unitId": g["id"]})
        if ok:
            log(f"  Oel sparen: Generator {g['id']} abgeschaltet (Station traegt die Last).")
            surplus -= 1
            gs = conn.refresh_state() or gs
    return gs


def check_energy_load(gs, conn):
    """LOAD CHECK (belongs in every stock-taking): adapts the energy consumption to the
    production. First load shedding on a deficit (shed), then switch off surplus
    generators (save oil if a station runs), then switch mines back on on a surplus
    (restore). Called in both check phases, because the energy situation can change
    through build/loss/movement."""
    gs = shed_energy_load(gs, conn)
    gs = retire_surplus_generators(gs, conn)
    gs = restore_energy_load(gs, conn)
    return gs


def manage_unit_production(gs, conn):
    """UNIT PRODUCTION (construction vehicles). Runs as a mode-independent step,
    because the factories produce in parallel to everything else.
    Order:
      1. unload finished vehicles from the factories (activate) - otherwise they block
         the factory and cannot be used.
      2. EXISTENCE SAFEGUARD (highest priority): is there NO constructor but a heavy
         factory -> immediately order a constructor. Analogously pioneer/light factory.
         Without these builders nothing works at all.
      3. FORMULA: target values constructors = output/10, pioneers = output/7.5
         (rounded down). If the number (incl. production) is below the target, set the
         matching factory to continuous production (repeat) of the builder.
    Returns the (fresh) gs."""
    # --- 1. unload finished vehicles ---------------------------------------
    used_spots = set()   # unloading fields already occupied in THIS pass
    # capture the fields of ALL own vehicles separately - a freshly unloaded own
    # surveyor that has not yet driven off blocks the field. If unloaded onto again,
    # the client and server view of the field can diverge (possiblePlaceVehicle is
    # sight-dependent, canSeeAt) -> the server may not create the unit, the client
    # does -> OUT OF SYNC.
    own_vehicle_fields = set()
    _meid = gs.me.get("id") if gs.me else None
    for _p in gs.model.get("players", []):
        if _p.get("id") != _meid:
            continue
        for _v in _p.get("vehicles", []):
            own_vehicle_fields.add(tuple(gs.pos(_v)))

    def _free_exit_spot(b, verify_factory=False):
        """Unambiguously free ground field adjacent to the factory (8-neighbourhood
        around the footprint). Excluded: occupied fields (occupied_fields), fields
        with an own vehicle (own_vehicle_fields), fields already used in this pass
        (used_spots) as well as water/coast without a platform and blocked terrain
        (is_free_for_ground).

        verify_factory=True (for factory unloading): EACH candidate field is
        additionally checked via the bridge query 'canExitVehicle' against the exact
        server logic (possiblePlaceVehicle of the type to be produced). This way the
        bot never chooses a field the bridge would reject - instead of trying the same
        blocked field in vain over many turns. The simplified bot check
        (is_free_for_ground) can deviate from the server view; the query is the
        truth."""
        bpos = gs.pos(b)
        big = gs.is_big_building_type(gs.unit_type(b))
        foot = gs.footprint(bpos, big)
        cand = set()
        for (fx, fy) in foot:
            for n in gs.neighbors8(fx, fy):
                if n not in foot:
                    cand.add(n)
        occ = set(gs.occupied_fields()) | used_spots | own_vehicle_fields
        # do NOT exclude fields with PASSABLE buildings (coupling/road/bridge/
        # platform): a vehicle may stand there, so such a (even diagonal) neighbour
        # field is a valid unloading spot. occupied_fields counts these buildings;
        # remove them again here. The final canExitVehicle query checks against the
        # server truth anyway.
        occ -= gs.non_blocking_building_fields()
        for n in cand:
            if n in own_vehicle_fields or n in used_spots:
                continue
            if not gs.is_free_for_ground(*n, occ=occ):
                continue
            if verify_factory:
                # Server-Wahrheit abfragen: Ist n eine gueltige Ausladeposition?
                rep = conn.query({"query": "canExitVehicle",
                                  "buildingId": b["id"], "position": list(n)})
                if not (rep and rep.get("ok")):
                    continue
            return n
        return None

    # (a) vehicles STORED in the transporter/depot (storedUnitIds) -> 'activate'.
    for b, vid in gs.stored_vehicle_ids():
        spot = _free_exit_spot(b)
        if spot is None:
            continue
        ids_before = {v["id"] for v in gs.my_vehicles()}
        ok, _ = conn.do({"type": "activate", "containingUnitId": b["id"],
                         "activatedVehicleId": vid, "position": list(spot)})
        if ok:
            used_spots.add(spot)
            log(f"  Produktion: Fahrzeug {vid} ausgeladen nach {spot}.")
            gs = conn.refresh_state() or gs
            # remember the freshly created ID(s) - do NOT move them this turn.
            fresh = {v["id"] for v in gs.my_vehicles()} - ids_before
            _FRESH_UNITS.update(fresh)
            # send setAutoMove ONLY for surveyors (canSurvey=true). Other units
            # (scout, pioneer etc.) would reject 'setAutoMove' with "unit cannot
            # survey" - and a rejected command to a freshly created unit ID in the
            # same tick triggers OOS, because server and client see the action
            # sequence differently.
            sid_sur = gs.special_vehicles.get("surveyor")
            for fid in fresh:
                fv = next((v for v in gs.my_vehicles() if v["id"] == fid), None)
                if fv and sid_sur is not None and gs.unit_type(fv) == sid_sur:
                    conn.do({"type": "setAutoMove", "unitId": fid, "active": False})

    # (b) vehicles freshly PRODUCED in a FACTORY (buildList head finished) ->
    #     'finishBuild' with the factory as the unit. These are NOT in storedUnitIds;
    #     without this step e.g. surveyors never leave the factory (never get onto
    #     auto-survey). AT MOST ONE unloading per factory per pass; the unloading
    #     field is checked for 'free' beforehand (incl. used_spots), so that no
    #     blocked finishBuild action is sent (unloading multiple times onto the
    #     same/blocked field drove client and server apart -> OUT OF SYNC).
    for b in gs.factories_with_finished_unit():
        spot = _free_exit_spot(b, verify_factory=True)
        if spot is None:
            log(f"  Produktion: Fabrik {b['id']} hat fertige Einheit, aber kein "
                f"freies Nachbarfeld - warte.")
            continue
        ids_before = {v["id"] for v in gs.my_vehicles()}
        ok, reason = conn.do({"type": "finishBuild", "unitId": b["id"],
                              "escapePosition": list(spot)})
        if ok:
            used_spots.add(spot)
            log(f"  Produktion: Einheit aus Fabrik {b['id']} ausgeladen nach {spot}.")
            gs = conn.refresh_state() or gs
            # remember the freshly built ID(s) - do NOT move them this turn
            # (otherwise the movement starts staggered on host/bridge -> OUT OF SYNC).
            fresh = {v["id"] for v in gs.my_vehicles()} - ids_before
            _FRESH_UNITS.update(fresh)
            # do NOT send setAutoMove blanket to all fresh units. Reason:
            # addVehicle() assigns nextUnitId++; every action the client sends on the
            # new ID in the same GameTime tick can arrive at server and client at
            # different times -> CRC divergence -> OUT OF SYNC. Surveyors are steered
            # in _preparation_phase (steer_surveyors) anyway. Non-surveyors (scout,
            # pioneer) have canSurvey=false -> the bridge would reject setAutoMove with
            # "unit cannot survey" anyway, but the bot client would still incorporate
            # the action into its state -> cumulative CRC error with many units.
        elif reason:
            log(f"  Produktion: Ausladen aus Fabrik {b['id']} abgelehnt ({reason}).")

    # process SIMULATED repeat: for each factory on soft-repeat whose buildList is
    # empty, set a new single order (repeat=False). Must run AFTER the unloading
    # (above), so that a just-finished unit is unloaded before we trigger resupply.
    #
    # PRIORITY PRE-CHECK: if there is a pioneer deficit (mine rule), the light factory
    # must NOT push a new scout via soft-repeat NOW - otherwise a fresh scout occupies
    # the slot and the pioneer switch is delayed by a whole scout build time. So take
    # it out of the soft-repeat beforehand (does not abort a running build - clear
    # only stops the re-triggering).
    _smallfac_pre = gs.first_building_factory("smallfactory")
    _sid_pio_pre = gs.special_vehicles.get("engineer")
    if _smallfac_pre is not None and _sid_pio_pre is not None:
        _pio_deficit_pre = _INVENTORY["pioneer"]["deficit"]
        if _pio_deficit_pre > 0:
            conn.clear_soft_repeat(_smallfac_pre["id"])

    # SAME PRE-CHECK for the HEAVY factory / the CONSTRUCTOR: without it
    # tick_soft_repeat() re-triggers the constructor build endlessly (buildList empty
    # -> new constructor) until far too many stand around (observed: 10 pieces at 6
    # mines, target 3). If the constructor deficit (from the central stock-taking) is
    # covered, clear the heavy factory's soft-repeat - that stops only the
    # re-triggering, a running build stays.
    _bigfac_pre = gs.first_building_factory("bigfactory")
    _sid_con_pre = gs.special_vehicles.get("constructor")
    if _bigfac_pre is not None and _sid_con_pre is not None:
        _con_deficit_pre = _INVENTORY["constructor"]["deficit"]
        if _con_deficit_pre <= 0:
            conn.clear_soft_repeat(_bigfac_pre["id"])

    retriggered = conn.tick_soft_repeat(gs)
    if retriggered:
        gs = conn.refresh_state() or gs
        for _fid in retriggered:
            log(f"  Produktion: simuliertes Repeat - Fabrik {_fid} neu beauftragt.")

    sid_con = gs.special_vehicles.get("constructor")
    sid_pio = gs.special_vehicles.get("engineer")
    bigfac = gs.first_building_factory("bigfactory")
    smallfac = gs.first_building_factory("smallfactory")

    def order_factory(factory, vehicle_sid, label):
        """Makes the factory build the desired builder in continuous production
        (repeat). Three cases:
          1. already runs correctly (right type + isWorking) -> do nothing.
          2. PAUSED with a financed order (right type, remainingMetal>0, but
             isWorking=False, e.g. after an energy emergency) -> only startWork, the
             half-finished build progress is preserved.
          3. STUCK/empty/wrong type (no order, remainingMetal<=0 or another type) ->
             set the order ANEW (changeBuildList finances fresh and starts the factory
             automatically)."""
        nonlocal gs
        bl = factory.get("buildList", []) or []
        if bl:
            t0 = bl[0].get("type", {}) if isinstance(bl[0], dict) else {}
            rem = bl[0].get("remainingMetal", 0) if isinstance(bl[0], dict) else 0
            right_type = isinstance(t0, dict) and t0.get("secondPart") == vehicle_sid
            if right_type and factory.get("isWorking"):
                return  # case 1: runs correctly
            if right_type and (rem or 0) > 0 and not factory.get("isWorking"):
                # case 2: paused with a financed order -> only switch on
                ok, reason = conn.do({"type": "startWork", "unitId": factory["id"]})
                if ok:
                    log(f"  Produktion: {label}-Fabrik wieder angeschaltet (Auftrag laeuft weiter).")
                    gs = conn.refresh_state() or gs
                else:
                    log(f"  Produktion: {label}-Anschalten abgelehnt ({reason}).")
                return
        # case 3: set the order ANEW - WITHOUT MAXR repeat (that causes OOS).
        # Instead: single order (repeat=False) + enter the factory into the simulated
        # repeat, so that tick_soft_repeat() re-triggers on an empty buildList.
        ok, reason = conn.do({"type": "changeBuildList",
                              "buildingId": factory["id"],
                              "buildList": [[0, vehicle_sid]],
                              "buildSpeed": 0, "repeat": False})
        if ok:
            conn.set_soft_repeat(factory["id"], [[0, vehicle_sid]])
            log(f"  Produktion: {label}-Fabrik auf simuliertes Repeat gesetzt "
                f"(Einzelauftrag + soft-repeat, kein MAXR-repeat).")
            gs = conn.refresh_state() or gs
        else:
            log(f"  Produktion: {label}-Auftrag abgelehnt ({reason}).")

    # --- 2. EXISTENCE SAFEGUARD (highest priority) --------------------------------
    have_con = len(gs.vehicles_of_type("constructor")) > 0 \
        or (sid_con is not None and gs.count_constructors_incl_production() > 0)
    have_pio = len(gs.vehicles_of_type("engineer")) > 0 \
        or (sid_pio is not None and gs.count_pioneers_incl_production() > 0)
    if not have_con and bigfac is not None and sid_con is not None:
        order_factory(bigfac, sid_con, "schwere")
        return gs
    if not have_pio and smallfac is not None and sid_pio is not None:
        order_factory(smallfac, sid_pio, "leichte")
        return gs

    # --- 3. ENERGIE-GATE ------------------------------------------------------
    # ENERGY GATE: the additional builder/surveyor production runs ONLY if the energy
    # carries it. If the energy is not enough (energy emergency), the factory stays off
    # - the energy emergency has priority, energy is built first. Otherwise an
    # oscillation arises: load shedding switches the factory off, production
    # switches it back on. (The existence safeguard above is exempt from this -
    # without a builder nothing works, it always runs.)
    if not gs.energy_overcapacity_ok():
        log("  Produktion: pausiert (Energie-Notfall hat Vorrang).")
        return gs

    # --- 4. BUILDER MINIMUM STOCK (mine-coupled) ---------------------------
    # REPLACES the old formulas target_constructors()/target_pioneers().
    #   TARGET constructors = ceil(0.5 * mine count)
    #   TARGET pioneers     = 2   * mine count
    # This builder production has the HIGHEST priority in expansion mode, before
    # scout/surveyor. The constructor is served BEFORE the pioneer (shared ore budget;
    # separate factories: constructor=bigfac, pioneer=smallfac). The emergency is NOT
    # in here - it already aborted above (energy_overcapacity_ok / existence
    # safeguard), so it keeps absolute priority. Read from the central stock-taking
    # (phase 1) - do not recompute.
    _inv = _INVENTORY
    con_deficit = _inv["constructor"]["deficit"]
    pio_deficit = _inv["pioneer"]["deficit"]
    if _inv["builder_priority"]:
        log(f"  Produktion: Bauer-Bedarf (Minen={_inv['mine_count']}): "
            f"Konstrukteur fehlt {con_deficit} (Soll {_inv['constructor']['need']}), "
            f"Pionier fehlt {pio_deficit} (Soll {_inv['pioneer']['need']}) "
            f"-> Reihenfolge {_inv['builder_priority']}.")

    # constructor FIRST (heavy factory). Only the heavy factory can build it.
    if con_deficit > 0 and bigfac is not None and sid_con is not None:
        order_factory(bigfac, sid_con, "schwere")

    # light factory: FIRST pioneers until the target is met (mine rule), THEN scouts.
    # pioneers have priority over scouts - without them expansion/repair stalls.
    if smallfac is not None:
        if pio_deficit > 0 and sid_pio is not None:
            # pioneer shortage -> the light factory should build pioneers.
            # OPTION A (never abort a build): if the factory is currently building a
            # FOREIGN TYPE (e.g. a scout), it is NOT switched over - otherwise
            # changeBuildList would discard the running scout (type change -> material
            # lost). Instead only switch off the scout soft-repeat (no more resupply);
            # the running scout finishes. Only when the buildList is empty (scout
            # unloaded) is the pioneer ordered.
            conn.clear_soft_repeat(smallfac["id"])
            _bl_small = smallfac.get("buildList", []) or []
            _head_is_pio = False
            if _bl_small:
                _t0 = _bl_small[0].get("type", {}) if isinstance(_bl_small[0], dict) else {}
                _head_is_pio = isinstance(_t0, dict) and _t0.get("secondPart") == sid_pio
            if not _bl_small or _head_is_pio:
                # buildList empty OR already building a pioneer -> order (continue)
                # the pioneer now. order_factory case 1/2/3 does the right thing.
                order_factory(smallfac, sid_pio, "leichte")
            else:
                log("  Produktion: leichte Fabrik baut noch fertig "
                    "(kein Abbruch), danach Umschalten auf Pionier.")
        else:
            # no pioneer deficit -> reconnaissance/cleanup. Ranking:
            #   surveyor  : target REQUIRED_SURVEYORS (at all times).
            #   bulldozer : only if rubble is visible. 1 piece from >0 rubble,
            #               2 pieces from >10 visible rubble fields.
            #   scout     : ENDLESS, once surveyor (and bulldozer demand) is covered.
            # All use the same SIMULATED repeat (no MAXR repeat -> no OOS). The pioneer
            # interruption is further up (pio_deficit > 0 -> clear_soft_repeat +
            # pioneer build); once no pioneer is missing any more, the code lands here
            # again and takes up the production.
            sid_sur = gs.special_vehicles.get("surveyor")
            sid_dozer = gs.vehicle_sid_by_name("bulldozer")
            sur_have = _INVENTORY["surveyor"]["have"]

            # bulldozer target from the central stock-taking (rubble situation).
            dozer_target = _INVENTORY["bulldozer"]["need"]
            dozer_have = _INVENTORY["bulldozer"]["have"]

            def _drive_light_factory(vehicle_sid, label):
                """Sets the light factory via soft-repeat to a reconnaissance type
                and immediately triggers the first single order if the buildList is
                empty. NEVER aborts a running build."""
                nonlocal gs
                if conn._soft_repeat.get(smallfac["id"]) != [[0, vehicle_sid]]:
                    conn.set_soft_repeat(smallfac["id"], [[0, vehicle_sid]])
                    log(f"  Produktion: leichte Fabrik auf SIMULIERTES "
                        f"{label}-Repeat gesetzt (kein MAXR-repeat).")
                _bl = smallfac.get("buildList", []) or []
                if not _bl:
                    ok, reason = conn.do({"type": "changeBuildList",
                                          "buildingId": smallfac["id"],
                                          "buildList": [[0, vehicle_sid]],
                                          "buildSpeed": 0, "repeat": False})
                    if ok:
                        log(f"  Produktion: erster {label}-Einzelauftrag gestartet.")
                        gs = conn.refresh_state() or gs

            if sid_sur is not None and _INVENTORY["surveyor"]["deficit"] > 0:
                # surveyor deficit -> build a surveyor (priority over everything else).
                _drive_light_factory(sid_sur, "Surveyor")
            elif sid_dozer is not None and dozer_have < dozer_target:
                # rubble visible and bulldozer target not yet reached ->
                # build a bulldozer (priority over scout).
                _drive_light_factory(sid_dozer, "Bulldozer")
            else:
                # surveyor and bulldozer demand covered -> NO further resupply. The
                # ENDLESS scout build is SWITCHED OFF (otherwise it burns material
                # needed for mines/constructors). Clear soft-repeat (no new order), but
                # do NOT abort a running build. The coupling of unit building to the
                # ore production follows in the next step; until then the light factory
                # stands still once surveyor/bulldozer are covered and no pioneer is
                # missing.
                conn.clear_soft_repeat(smallfac["id"])
    return gs


def manage_scouts(gs, conn):
    """Drive scouts that have already been unloaded away from the factory.

    IMPORTANT: freshly unloaded units are in _FRESH_UNITS (set by
    manage_unit_production in the same turn). These are NOT moved - the bot would
    otherwise send a movement in the same turn in which the server only just placed
    the unit, which drives client and server view apart (OUT OF SYNC). They are steered
    only in the next turn.

    Scout facts from MAXR data.json (verified):
      firstPart=0, secondPart=27, buildAs=SmallGroundVehicle,
      canAttack=6, canDriveAndFire=true, speedMax=12,
      factorGround=1.0, factorSea=3.0 (water 3x slower).
    No attack: the bot sends no 'attack' action. changeSentry and setManualFire are
    not set - scouts only attack if the bot explicitly attacks, which we do not do
    here.

    Goal: scouts leave the factory area immediately in the next turn, so that factory
    neighbour fields stay free for the next unloading.
    """
    sid_scout = gs.vehicle_sid_by_name("scout")
    if sid_scout is None:
        return gs

    scouts = [v for v in gs.my_vehicles() if gs.unit_type(v) == sid_scout]
    if not scouts:
        return gs

    terrain = gs.terrain
    if terrain:
        map_w = terrain.get("width", 64)
        map_h = terrain.get("height", 64)
    else:
        all_pos = [gs.pos(v) for v in gs.my_vehicles()] +                   [gs.pos(b) for b in gs.my_buildings()]
        map_w = max((p[0] for p in all_pos), default=64) + 10
        map_h = max((p[1] for p in all_pos), default=64) + 10

    import random

    # base centroid: scouts should drive AWAY from the factory.
    base_cells = gs.base_footprint()
    if base_cells:
        base_xs = [c[0] for c in base_cells]
        base_ys = [c[1] for c in base_cells]
        base_cx = sum(base_xs) // len(base_xs)
        base_cy = sum(base_ys) // len(base_ys)
    else:
        base_cx, base_cy = map_w // 2, map_h // 2

    for sv in scouts:
        sid = sv["id"]

        # units with a behaviour mode (section 9) are steered by the mode phase - do
        # NOT touch here (no second move in the same turn -> OOS).
        if _MODE_MAP.get(sid) is not None:
            continue

        # freshly unloaded scouts may drive IMMEDIATELY - the waiting was only needed
        # as long as the MAXR repeat command caused the OOS. With the simulated repeat
        # (no repeat flag) the problem is fixed; the scout can be moved directly in the
        # first turn after the build.

        # already moving or no movement points -> do not touch.
        if sv.get("isUnitMoving"):
            continue
        speed_cur = sv.get("data", {}).get("speedCur", 0) or 0
        if speed_cur <= 0:
            continue

        svpos = gs.pos(sv)

        # target: random point far from the base (no water, no blocked field).
        target = None
        for _ in range(12):
            tx = random.randint(0, map_w - 1)
            ty = random.randint(0, map_h - 1)
            if terrain:
                data = terrain.get("data", "")
                idx = ty * map_w + tx
                if 0 <= idx < len(data) and data[idx] in ("#", "~"):
                    continue  # blocked or water (scouts prefer land)
            if abs(tx - base_cx) + abs(ty - base_cy) < 10:
                continue  # too close to the base
            if (tx, ty) == svpos:
                continue
            target = (tx, ty)
            break

        if target is None:
            # fallback: most distant map corner
            corners = [
                (0, 0), (map_w - 1, 0),
                (0, map_h - 1), (map_w - 1, map_h - 1),
            ]
            corners.sort(
                key=lambda c: abs(c[0] - base_cx) + abs(c[1] - base_cy),
                reverse=True,
            )
            target = corners[0]

        ok, reason = conn.do({"type": "move", "unitId": sid,
                              "target": list(target)})
        log(f"  Scout {sid} @ {svpos} -> {target}: {'ok' if ok else reason}")

    return gs


def manage_mine_allocation(gs, conn):
    """Adapts the mines' EXTRACTION DISTRIBUTION to the situation. Each mine has a
    shared extraction budget (canMineMaxRes) distributed over ore/oil/gold. The
    default at build time is ore>gold>oil - often not optimal.

    STABLE demand value (avoids oscillation): the oil target value is the potential
    oil consumption of the energy buildings plus a generator buffer (for the next
    energy level). This value does NOT depend on the current extraction, so there is
    no back-and-forth between oil and ore priority. Each mine extracts as much oil as
    needed (up to the maximum), the rest in ore. Sends only on a change.
    """
    # total oil demand of the base: what the RUNNING energy buildings actually
    # consume + a small buffer (GENERATOR_OIL_NEED), so that there is some reserve for
    # the next energy level. IMPORTANT: NOT the potential consumption of ALL (also
    # switched-off) energy buildings - otherwise the mines extract oil for buildings
    # that are not running at all, and the ore extraction collapses (constructors
    # cannot reload). No oil is extracted for energy that is not consumed (except for
    # the buffer).
    oil_need_total = gs.energy_oil_consumption(potential=False) + gs.GENERATOR_OIL_NEED
    changed = False
    # distribute over the mines: each covers in turn a part of the oil demand.
    remaining_oil = oil_need_total
    for mine in gs.my_mines():
        target = gs.demand_mine_allocation(mine, oil_target=remaining_oil)
        remaining_oil = max(0, remaining_oil - target["oil"])
        cur = gs.mine_prod(mine)
        if target != cur:
            ok, _ = conn.do({"type": "setResourceDistribution", "unitId": mine["id"],
                             "metal": target["metal"], "oil": target["oil"],
                             "gold": target["gold"]})
            if ok:
                log(f"  Foerderung Mine {mine['id']} angepasst: {cur} -> {target} "
                    f"(Oel-Bedarf gesamt {oil_need_total}).")
                changed = True
    if changed:
        gs = conn.refresh_state() or gs
    return gs


def _preparation_phase(gs, conn, label="Phase 0"):
    """PREPARATION PHASE: all mode-INDEPENDENT mandatory actions that run in EVERY
    turn, no matter which mode is active. The order matters:
      1. UPDATE THE BUILD PLAN (reconcile): finished-built orders + dead assignments
         drop out of the plan. FIRST - so that the plan knows what is already done
         BEFORE the builders are pulled off the field.
      2. fetch finished-built builders off the field (finishBuild) - otherwise they
         stick.
      3. surveyors onto auto-exploration, mine extraction by situation, energy load.
    Returns the fresh gs."""
    _reconcile_plan(gs)                        # 1. reconcile build plan against state
    gs = _finish_finished_builders(gs, conn)   # 2. finished builders off the field (mandatory)
    _reconcile_plan(gs)                        #    reconcile again after pulling
    steer_surveyors(gs, conn)                  # 3. surveyor exploration (bot-steered)
    gs = steer_bulldozers(gs, conn)            #    bulldozer rubble (bot-steered)
    gs = manage_mine_allocation(gs, conn)      #    mine extraction by situation
    gs = check_energy_load(gs, conn)           #    energy load
    # assign the pioneers' core tasks (net repair / platform build / base expansion)
    # by 1/3 quota - per ID, repair priority. Once per turn (Phase0), before the modes,
    # so that the repair and platform assignment know the core tasks.
    if label == "Phase0":
        allocate_core_tasks(gs)
    # 4. update the metal fill-level history (basis for the 80% storage rule).
    #    numerator = currently stored ore; denominator = capacity incl. under
    #    construction. At most 3 values, oldest falls out. ONCE per turn cycle (Phase0
    #    only), not again in Phase4 - otherwise the same turn would be counted twice.
    global _METAL_FILL_HISTORY
    if label == "Phase0":
        ratio = gs.metal_fill_ratio()
        _METAL_FILL_HISTORY.append(ratio)
        if len(_METAL_FILL_HISTORY) > 3:
            _METAL_FILL_HISTORY.pop(0)
        log(f"  Metall-Fuellgrad: {ratio:.0%} (History: {[f'{r:.0%}' for r in _METAL_FILL_HISTORY]})")
    # 5. compute the energy utilisation (basis for the 90% station rule).
    #    Once per turn cycle (Phase0 only), incl. stations under construction.
    global _ENERGY_LOAD_RATIO
    if label == "Phase0":
        _ENERGY_LOAD_RATIO = gs.energy_load_ratio()
        log(f"  Energie-Auslastung: {_ENERGY_LOAD_RATIO:.0%}")
    log(f"ok {label} (Vorbereitung)")
    return gs


def _test_upgrade_scout_scan(gs, conn):
    """TEST rule: upgrade the scout unit type by ONE scan-range point (scan) at every
    opportunity, as soon as the credits are enough. Pure test logic for the upgrade
    pipeline - the strategic decision (which upgrade on which type) comes into the
    strategy later.

    Scout is a UNIT (vehicle); read_upgrades/buy_upgrade resolve the name 'scout' via
    vehicle_sid_by_name. Upgrade attribute: 'scan'.

    Returns the possibly refreshed gs.
    """
    sid_scout = gs.vehicle_sid_by_name("scout")
    if sid_scout is None:
        return gs
    # read the possible upgrades of the scout type via the bridge (MAXR computes).
    rep = upgrade_logic.read_upgrades(conn, gs, type_name="scout")
    if not rep:
        return gs
    credits = rep.get("credits", 0)
    scan_entry = None
    for u in rep.get("upgrades", []):
        if u.get("type") == "scan":
            scan_entry = u
            break
    if scan_entry is None:
        log("Performing Upgrade check for upgrade scan@scout..... "
            "Result: scout has no scan upgrade slot.")
        return gs
    cost = scan_entry.get("nextPrice", -1)
    if cost is None or cost <= 0:
        log(f"Performing Upgrade check for upgrade scan@scout..... "
            f"Result: not upgradable further (no price), current balance {credits}.")
        return gs
    if cost > credits:
        log(f"Performing Upgrade check for upgrade scan@scout..... "
            f"Result: Cost {cost} Credits, current balance {credits} "
            f"--> not enough credits")
        return gs
    # enough credits -> perform the upgrade.
    log(f"Performing Upgrade check for upgrade scan@scout..... "
        f"Result: Cost {cost} Credits, current balance {credits} "
        f"--> commencing upgrade")
    # CONFLICT AVOIDANCE (statistics): conn.do() counts stat_ok/stat_rejected. But
    # phase 4 triggers the second planning pass (phase 5/6) on stat_rejected > 0, and
    # phase 7 uses the statistics as an RL signal for BUILD/MOVEMENT actions. An
    # upgrade purchase does not belong in these statistics (it has nothing to do with
    # build orders to be re-planned). Therefore save and restore the counters around
    # the purchase.
    _ok_before, _rej_before = conn.stat_ok, conn.stat_rejected
    ok, reason = upgrade_logic.buy_upgrade(conn, gs, "scan", 1, type_name="scout")
    conn.stat_ok, conn.stat_rejected = _ok_before, _rej_before
    if ok:
        log(f"  Upgrade scan@scout durchgefuehrt (-{cost} Credits).")
        gs = conn.refresh_state() or gs
    else:
        log(f"  Upgrade scan@scout abgelehnt ({reason}).")
    return gs


def _assign_test_modes(gs):
    """TEST mode assignment (section 9.6): blanket per unit type. Later the
    strategy/major layer fills the dict {unit_id: mode}. For the test we set ONE type
    to ONE mode, to check it in isolation.

    Current test: SCOUT -> AktivesStalking (the scouts explore, should keep the enemy
    in their own scan range, but out of the enemy's max(scan,attack)). Surveyors do
    NOT take part - they run in auto-survey and keep clearing passively (undiscovered
    resource fields), even on enemy contact.
    """
    modes = {}
    sid_scout = gs.vehicle_sid_by_name("scout")
    if sid_scout is not None:
        for v in gs.my_vehicles():
            if gs.unit_type(v) == sid_scout:
                modes[v["id"]] = unit_modes.AKTIVES_STALKING
    return modes


def _run_unit_modes(gs, conn, mode_map):
    """MODE PHASE (test): computes the heatmap and applies the assigned behaviour mode
    per unit. Only units with a mode in the mode_map are steered. Freshly built units
    (_FRESH_UNITS) are NOT moved this turn (OOS protection, as everywhere). Returns the
    (fresh) gs.
    """
    if not mode_map:
        return gs
    # fetch the enemy ranges EXACTLY from the bridge (real cRangeMap, aggregated
    # player view). If the query fails (old bridge), the heatmap recomputes internally
    # (circle emulation).
    er = conn.query({"query": "enemyRangeMaps"})
    if not er or er.get("result") != "enemyRangeMaps":
        er = None
    hm = heat_map_calc.compute_heatmaps(gs, enemy_ranges=er)

    def _pc(c, uid, target):
        return path_cost_to(c, uid, target)

    for v in list(gs.my_vehicles()):
        uid = v["id"]
        mode = mode_map.get(uid)
        if mode is None:
            continue
        if uid in _FRESH_UNITS:
            log(f"  Modus {mode}: Einheit {uid} frisch gebaut - diese Runde nicht bewegt.")
            continue
        # already moving -> do not touch (OOS protection, one move per turn)
        if v.get("isUnitMoving"):
            continue
        unit_modes.apply_mode(gs, conn, v, mode, hm, _pc, log=log)
        gs = conn.refresh_state() or gs
    return gs


def mark_expansion_backlog(gs):
    """PHASE 2 (planning) - MARKING (Dungeon-Keeper). Fills the persistent _BACKLOG
    with ALL developable deposits - builder-INDEPENDENT. For each new area a BuildSite
    is created and its components are derived from the terrain (derive_components:
    possibly clear rubble -> platforms -> mine -> coupling). Existing sites keep their
    fixed fields (no wandering). Completed ones (mine stands) are removed.

    Runs ALWAYS (even with _USE_BACKLOG=False) - then PASSIVELY: only mark + log, the
    assignment is still done by the old mode_expansion. Returns nothing (works on the
    global _BACKLOG)."""
    mine_sid = gs.MINE_SID
    plat_sid = gs.building_sid_by_name("platform")
    conn_sid = gs.building_sid_by_name("connector")

    # 1) delete completed sites (mine stands).
    before = len(_BACKLOG)
    _BACKLOG.prune_done(gs)

    # 2) fetch all developable candidates (WITHOUT a hard ore-mandatory filter: the
    # mandatory/urgency acts in the backlog as a SCORE, not as a gate).
    cands = gs.expansion_candidates(blocked_fields=_EXPANSION_REJECTED)

    # DEDUP BEFORE CREATING: expansion_candidates returns one anchor per resource
    # field; neighbouring anchors can point to the SAME 2x2 mine area. So that not
    # several competing sites of the same area are created + immediately discarded
    # again every turn (work/terrain queries in vain), make only the BEST (highest
    # score) candidate per mine_pos into a BuildSite. mine_pos already in the backlog
    # stay untouched (stability - no displacement of existing sites here).
    existing_minepos = {s.mine_pos for s in _BACKLOG.sorted_open()
                        if s.mine_pos is not None}
    best_per_minepos = {}   # mine_pos -> (score, site)
    for (ax, ay, atyp, aamt, score) in cands:
        if _BACKLOG.site_at((ax, ay)) is not None:
            continue   # anchor already in the backlog -> keep the fields stable
        cand = BuildSite((ax, ay), atyp, amount=aamt, score=score)
        if not cand.derive_components(gs, mine_sid, plat_sid, conn_sid):
            continue   # not developable
        mp = cand.mine_pos
        if mp in existing_minepos:
            continue   # this area already has a site in the backlog
        prev = best_per_minepos.get(mp)
        if prev is None or score > prev[0]:
            best_per_minepos[mp] = (score, cand)

    new_count = 0
    for (score, site) in best_per_minepos.values():
        if _BACKLOG.add_or_update(site):
            new_count += 1

    # resolve footprint overlaps: committed sites are frozen and win every overlap;
    # among pure proposals the higher score wins (the loser gives up its place). This
    # way the bot wanders to optimal build sites as long as nobody builds, without
    # disturbing running sites.
    overlap_dropped = _BACKLOG.resolve_overlaps()

    # MANDATORY ORE MINE: as long as the bot has only the starting mine
    # (mine_count<=1), the next mine must be a strong ore mine (2x2 with >=7 ore) - it
    # occupies place 1 (mandatory) in the backlog until it is built. The emergency no
    # longer builds a mine; this mandatory prioritisation replaces that. Marking on
    # the site of the best >=7-ore metal area; if none is explored yet, nothing stays
    # marked (then the standby phase in ensure_first_metal_mine runs).
    for s in _BACKLOG.sorted_open():
        s.mandatory = False
    if gs.mine_count() <= 1:
        mt = gs.expansion_target(blocked_fields=_EXPANSION_REJECTED,
                                 force_type="metal", min_metal=7)
        if mt is not None:
            mtx, mty = mt[0], mt[1]
            site = _BACKLOG.site_at((mtx, mty))
            if site is not None:
                site.mandatory = True
                log(f"  [Backlog] PFLICHT-Erzmine (>=7 Erz) auf Platz 1: "
                    f"Site ({mtx},{mty}).")

    if new_count or before != len(_BACKLOG) or overlap_dropped:
        log(f"  [Backlog] {len(_BACKLOG)} Baustelle(n) "
            f"(+{new_count} neu, {before - len(_BACKLOG) + new_count - overlap_dropped} erledigt, "
            f"{overlap_dropped} Ueberlapp verworfen). "
            f"{'PASSIV' if not _USE_BACKLOG else 'AKTIV'}.")
    # detail line per top site (to check the detection in the game log).
    for s in _BACKLOG.sorted_open()[:5]:
        kinds = [c.kind for c in sorted(s.components, key=lambda c: c.order)]
        log(f"    Site {s.anchor} {s.target_type} score={s.score:.2f} "
            f"mine_pos={s.mine_pos} comps={kinds}")


def run_turn(gs, conn):
    """PHASE ORCHESTRATOR (mode-based).
    Phase 0 PREPARATION: mode-independent mandatory actions (fetch finished builders,
            surveyor auto, energy load) - run ALWAYS.
    Phase 1 STOCK-TAKING (determine modes).
    Phase 2 UPGRADES: check and implement possible upgrades. Runs AFTER the
            stock-taking and BEFORE the planning/production - so the unit/building
            stock is known before credits flow into upgrades, and the planning sees
            the already-updated stock.
    Phase 3 PLANNING/PRODUCTION + execute active modes (emergency > expansion).
    Phase 4 preparation again + re-determine the situation (correction).
    Phase 5/6 second pass if builders/tasks are still open.
    Phase 8 CLEANUP PHASE: runs ALWAYS (even if 5/6 skipped). Stuck-clear: release
            idle pioneers (is_idle at turn end) from the allocation, so that they are
            free again next turn. No new actions.
    Phase 9 TURN END: statistics.

    Order (core): 1. stock-taking  2. upgrades  3. planning.

    blocked/targets: memory preserved across the phases (this turn).
    """
    conn.reset_stats()
    _FRESH_UNITS.clear()   # new turn: units built last turn may now drive
    _PENDING_BUILD_MOVE.clear()   # new turn: pause bookkeeping of the first build phase
    _TOUCHED_THIS_TURN.clear()    # new turn: activity note for the stuck-clear
    # baseline: which surveyors existed BEFORE this turn. A surveyor newly appearing
    # this turn thus counts as fresh in BOTH preparation phases.
    global _KNOWN_SURVEYORS, _KNOWN_BULLDOZERS
    _KNOWN_SURVEYORS = {v["id"] for v in gs.vehicles_of_type("surveyor")}
    _sid_dozer = gs.vehicle_sid_by_name("bulldozer")
    _KNOWN_BULLDOZERS = ({v["id"] for v in gs.my_vehicles()
                          if gs.unit_type(v) == _sid_dozer}
                         if _sid_dozer is not None else set())
    blocked = {}
    targets = {}

    # --- Phase 0: PREPARATION (mode-independent mandatory actions) -----------
    gs = _preparation_phase(gs, conn, label="Phase0")

    # --- Phase 1: STOCK-TAKING (determine modes) -------------------------
    # CENTRAL: once per turn 'what do we have? What do we need?' for all
    # builder/unit types. All later phases read from _INVENTORY.
    global _INVENTORY
    _INVENTORY = take_inventory(gs, required_surveyors=REQUIRED_SURVEYORS,
                                mines_per_station=_MINES_PER_STATION,
                                cover_target=getattr(gs, "COVER_TARGET", None),
                                radar_cover_target=getattr(gs, "RADAR_COVER_TARGET", 1))
    _inv = _INVENTORY
    log("  [Bestand] Minen={m}, Schrott={r} | "
        "Pionier {p[have]}/{p[need]}, Konstrukteur {c[have]}/{c[need]}, "
        "Surveyor {s[have]}/{s[need]}, Bulldozer {b[have]}/{b[need]}, "
        "Station {st[have]}/{st[need]}".format(
            m=_inv["mine_count"], r=_inv["rubble_count"],
            p=_inv["pioneer"], c=_inv["constructor"], s=_inv["surveyor"],
            b=_inv["bulldozer"], st=_inv["station"]))
    modes = active_modes(gs)
    log(f"ok Phase1 (Modi: {[m[1] for m in modes]})")

    # --- Phase 2: UPGRADES --------------------------------------------------
    # after the stock-taking, BEFORE the planning: check and implement possible
    # upgrades. This way credits flow into upgrades before the planning runs, and the
    # planning already sees the updated stock.
    # (TEST: upgrade the scout scan range as soon as possible.)
    gs = _test_upgrade_scout_scan(gs, conn)
    log("ok Phase2 (Upgrades)")

    # --- Phase 3: PLANNING/PRODUCTION + modes ---------------------------------
    gs = manage_unit_production(gs, conn)
    # mark the build-site backlog (Dungeon-Keeper). Runs along passively as long as
    # _USE_BACKLOG is False - marks + logs without changing the assignment.
    mark_expansion_backlog(gs)
    # TEST: behaviour modes (heatmap-based). Mode assignment blanket per type. Runs
    # BEFORE manage_scouts, so that units with a mode are skipped there (no second
    # move in the same turn -> OOS protection).
    global _MODE_MAP
    _MODE_MAP = _assign_test_modes(gs)
    gs = _run_unit_modes(gs, conn, _MODE_MAP)
    gs = manage_scouts(gs, conn)
    gs = _run_active_modes(gs, conn, modes, blocked, targets)
    log("ok Phase3")

    # --- wait for the arrival of the builders that set off (fact instead of estimate) -
    # did builders in phase 3 SET OFF to their build site? Then no longer wait an
    # estimated time (old: time.sleep after driven fields), but ask the bridge WHICH
    # builders have completed their movement. The bridge observes each unit's MoveJob
    # and reports per poll the finished ones (driven OR demonstrably not driven) with
    # the current position; "pending" says how many are still on the way. We poll
    # until pending==0 - then ALL builder drives started this turn are finished and the
    # second build phase can build at the target. The units drive in parallel; so we
    # only wait until the slowest stands. The short sleep per pass is pure poll
    # throttling (gives the frame-driven gameLoop time to tick) - NO time estimate any
    # more. A timeout protects against hanging if the bridge unexpectedly does not
    # answer.
    if _PENDING_BUILD_MOVE:
        import time
        log(f"  warte auf Ankunft von {len(_PENDING_BUILD_MOVE)} losgefahrenen "
            f"Bauer(n) (Bruecke meldet Abschluss; zweite Bauphase baut am Ziel).")
        deadline = time.time() + 10.0   # safety timeout (s) against hanging
        while True:
            rep = conn.query({"query": "finishedMoves"})
            # bridge unreachable / unexpected answer -> do not block forever.
            if not isinstance(rep, dict) or rep.get("result") != "finishedMoves":
                log("  finishedMoves: keine gueltige Antwort - breche Warten ab.")
                break
            if int(rep.get("pending", 0)) <= 0:
                break
            if time.time() >= deadline:
                log(f"  finishedMoves: Timeout, {rep.get('pending')} Bauer noch "
                    f"unterwegs - fahre fort (zweite Bauphase prueft selbst).")
                break
            time.sleep(0.02)   # short poll throttling, NO estimate pause

    # --- Phase 4: preparation again + situation anew (correction) ----------------
    gs2 = conn.refresh_state()
    if gs2 is not None:
        gs = gs2
    gs = _preparation_phase(gs, conn, label="Phase4")
    modes = active_modes(gs)
    # trigger the second pass ONLY if it really has to correct something:
    #   - there are still finished-built builders on their field (must get off), OR
    #   - an action was REJECTED this turn (stat_rejected) -> re-plan, OR
    #   - a builder set off to its build site in phase 3 (_PENDING_BUILD_MOVE) -> it
    #     may now (after the pause) have arrived and should build.
    # A rejection is never "normal" (the unit still has movement points / the order
    # was not feasible). Exactly then the second planning phase should take effect and
    # assign another build site/task.
    still_busy = (_open_finished_builders(gs) or conn.stat_rejected > 0
                  or bool(_PENDING_BUILD_MOVE))

    # --- Phase 5/6: second pass ---------------------------------------
    if still_busy:
        gs = _run_active_modes(gs, conn, modes, blocked, targets)
        log("ok Phase5")
        gs3 = conn.refresh_state()
        if gs3 is not None:
            gs = gs3
        # after the second pass fetch finished builders again (mandatory)
        gs = _finish_finished_builders(gs, conn)
        log("ok Phase6")

    # --- Phase 8: CLEANUP PHASE -----------------------------------
    # own phase AFTER the (optional) second pass - runs ALWAYS, even if phase 5/6 were
    # skipped. No new tasks/actions are assigned here; only cleanup happens.
    # STUCK-CLEAR (pioneers only): every pioneer had the opportunity this turn (phase 3
    # + possibly phase 5) to be occupied by its task. Whoever is STILL idle NOW
    # (is_idle: no activity, movement points free, no sentry/AutoMove) has done NOTHING
    # up to here -> it has fallen out of its assignment and is released from the
    # allocation, so that it counts as free again next turn (phase 0:
    # allocate_core_tasks) and can be re-planned. An actively working pioneer is now
    # isBuilding/moving -> not idle, so the running work is NOT torn apart.
    gs_se = conn.refresh_state()
    if gs_se is not None:
        gs = gs_se
    n_freed = release_idle_pioneers(gs)
    if n_freed:
        log(f"  [StuckClear] {n_freed} idle Pionier(e) aus Allokation geloest.")
    n_freed_c = release_idle_constructors(gs)
    if n_freed_c:
        log(f"  [StuckClear] {n_freed_c} idle Konstrukteur(e) aus Backlog-Bindung geloest.")
    log("ok Phase8 (Cleanup)")

    # --- Phase 9: TURN END / statistics ---------------------------------------
    # cleanup check: surveyors freshly unloaded this turn are now (after unloading +
    # synchronisation by the bridge) server-confirmed and can be set to auto-survey -
    # no waiting until the next turn needed. The bridge now allows the activation
    # (client in sync); if a surveyor was not ready yet, the bridge rejects and the
    # next turn catches it up.
    # Freshly unloaded surveyors are captured and steered by the next turn. NO second
    # steer_surveyors call here: a second move command in the same turn hits a unit
    # that already has a WAITING move job from the first call (moving=false -> the
    # guard does not take effect). addMoveJob then decouples the old job (vehicleId is
    # deleted) -> orphaned move job whose pixel state enters the checksum -> OUT OF
    # SYNC. Only ONE steering pass per turn (in the preparation phase).

    log(f"  Statistik: Aktionen ok={conn.stat_ok} rejected={conn.stat_rejected}")
    log("ok Phase9 (Zugende)")
    return gs


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5001
    name = sys.argv[3] if len(sys.argv) > 3 else "ClaudeBot"

    if _LOG_PATH is not None:
        log(f"Logdatei: {_LOG_PATH}")
    log(f"verbinde zu {host}:{port} ...")
    conn = Conn(host, port, player_name=name)
    # write every action sent to the bridge into the same log (file+console) - with
    # unit, from/to and acceptance/rejection+reason.
    conn.action_logger = log
    try:
        for gs in conn.turns():
            # pass a tuning knob from bot_run through into the GameState: the
            # mine-site evaluation (_demand_factor) reads self.DEMAND_SATURATION.
            gs.DEMAND_SATURATION = _DEMAND_SATURATION
            # personality + target coverage level for the defense screen.
            gs.PERSONALITY = _PERSONALITY
            gs.COVER_TARGET = cover_target_for(_PERSONALITY)
            gs.RADAR_COVER_TARGET = _RADAR_COVER_TARGET
            log("")
            log("")
            log("================================================================")
            log(f"=== Runde {gs.turn} (Terrain: {'ja' if gs.terrain else 'nein'}) ===")
            log("================================================================")
            a = gs.assess()
            log(f"  Lage: Erz+{a['metal_income']} Oel+{a['oil_income']} "
                f"Energie {a['energy_production']} Speicher M{a['storage_metal']}/O{a['storage_oil']} "
                f"Fab L:{a['light_factory']} S:{a['heavy_factory']}")
            run_turn(gs, conn)
            report = conn.end_turn()
            log(f"  Zugende (Phase 8): {report}")
    finally:
        conn.close()
    log("beendet.")


if __name__ == "__main__":
    main()
