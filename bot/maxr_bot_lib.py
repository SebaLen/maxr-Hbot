#!/usr/bin/env python3
"""
maxr_bot_lib.py - Reusable building blocks for MAXR bots.

Object-oriented, modelled on MAXR's own design (cMap/cMapView/cModel encapsulate
data + queries). The central class GameState encapsulates a received state and
offers the queries a bot needs - in particular the COLLISION CHECK (terrain +
units + buildings) that the bot should use BEFORE every move and for the
finishBuild evasion.

IMPORTANT (simultaneous game): These checks are a PLANNING AID, not a promise.
Opponents can occupy fields at the same time, stealth units only evade on entry.
Every action can be rejected by the bridge with "rejected" - the bot must react to
that (try alternatives), not trust blindly.

Usage:
    from maxr_bot_lib import GameState, Conn
    conn = Conn("127.0.0.1", 5001, player_name="ClaudeBot")
    for gs in conn.turns():            # yields one GameState per turn
        for act in decide(gs):
            ok, reason = conn.do(act)  # send one action, read the result
        conn.end_turn()
"""
import json
import socket


# Terrain codes from the bridge (terrainMap.data)
T_LAND = "."
T_WATER = "~"
T_COAST = "c"
T_BLOCKED = "#"

# surfacePosition values that make water walkable for land units
# (bridge=AboveSea, platform/road=Base). As names in staticUnitData.
WATER_WALKABLE_BUILDINGS = {"bridge", "platform", "road"}

# PASSABLE (non-blocking) buildings: connector (Above), platform/road (Base),
# bridge (AboveSea). They block NO other builds - e.g. a water platform can be
# built on a coupling and vice versa (different surfacePosition, no conflict). For
# the build-site choice they do NOT count as occupation.
NON_BLOCKING_BUILDINGS = {"connector", "bridge", "platform", "road"}

# Buildings that PRODUCE UNITS (canBuild != empty) or UNLOAD/STORE
# (storageUnitsMax > 0). They need free neighbouring fields to unload, so they
# must not be built in. Per unload category the required unload-field TERRAIN
# (verified from the unit factorGround/Sea/Air):
#   "ground" -> ground units (SmallGroundVehicle/BigGroundVehicle/Human)
#   "sea"    -> ships (Ship)
#   "air"    -> aircraft (Plane/Alien): IGNORED - no building blocks the air.
# The mapping is not hardwired but derived at runtime from canBuild /
# storeUnitsTypes (see unload_category_of_building), so that the rule also applies
# if the build planning later builds further such buildings.
UNLOAD_GROUND_TOKENS = {"SmallGroundVehicle", "BigGroundVehicle", "Human", "Ground"}
UNLOAD_SEA_TOKENS    = {"Ship"}
UNLOAD_AIR_TOKENS    = {"Plane", "Alien"}   # air -> ignored (blocks nothing)
# Minimum number of free unload fields that must remain for such a building
# (of the 12 fields of the full diagonal neighbourhood of a 2x2 block).
MIN_FREE_EXIT_FIELDS = 2


def _xy(p):
    """Position from state ({'X','Y'} or [x,y]) -> (x,y)."""
    if isinstance(p, dict):
        return (int(p["X"]), int(p["Y"])) if "X" in p else (int(p["x"]), int(p["y"]))
    return (int(p[0]), int(p[1]))


class GameState:
    """Encapsulates a received state + offers bot queries.

    terrain (the static map) is passed the first time and afterwards forwarded by
    the Conn object, since the bridge only sends it once.
    """

    def __init__(self, raw_state, player_name, terrain=None):
        self.player_name = player_name
        self.model = raw_state.get("model", raw_state)
        self.raw = raw_state
        self.terrain = terrain  # dict {width,height,data} or None

        ud = self.model.get("unitsData", {})
        self.special_vehicles = ud.get("specialVehicles", {})
        self.special_buildings = ud.get("specialBuildings", {})
        self._static_by_sid = {}  # secondPart(building) -> staticUnitData
        for u in ud.get("staticUnitData", []):
            idv = u.get("ID", {})
            if isinstance(idv, dict):
                self._static_by_sid[(idv.get("firstPart"), idv.get("secondPart"))] = u

        self.me = self._find_me()

    # ---------- basic accessors ----------
    def _find_me(self):
        for p in self.model.get("players", []):
            if p.get("player", {}).get("name") == self.player_name:
                return p
        return None

    @property
    def turn(self):
        return self.model.get("turnCounter", {}).get("turn", "?")

    @property
    def game_time(self):
        return self.model.get("gameTime", 0)

    def my_vehicles(self):
        return self.me.get("vehicles", []) if self.me else []

    def my_buildings(self):
        return self.me.get("buildings", []) if self.me else []

    @staticmethod
    def unit_type(u):
        """secondPart of the sID (unit type identifier)."""
        return u.get("data", {}).get("id", {}).get("secondPart")

    @staticmethod
    def unit_first(u):
        return u.get("data", {}).get("id", {}).get("firstPart")

    @staticmethod
    def pos(u):
        return _xy(u["position"])

    @staticmethod
    def stored(u):
        return u.get("storageResCur", u.get("data", {}).get("storageResCur", 0))

    def store_max(self, u):
        """Max. load capacity from the static unit data (not in the instance)."""
        first = self.unit_first(u)
        second = self.unit_type(u)
        st = self._static_by_sid.get((first, second))
        return st.get("storageResMax", 0) if st else 0

    def vehicles_of_type(self, type_name):
        """Own vehicles of a special type ('engineer'/'constructor'/'surveyor')."""
        sid = self.special_vehicles.get(type_name)
        return [v for v in self.my_vehicles() if self.unit_type(v) == sid]

    def vehicle_ids_with_movejob(self):
        """IDs of all vehicles that already have a move job in the model - ACTIVE OR
        WAITING. Important: the per-vehicle flag 'moving'/'jobActive' does NOT show
        a WAITING job (a waiting job does not count as 'moving' in MAXR). A second
        move command on a unit with a waiting job decouples the old job in MAXR
        (addMoveJob -> removeVehicle) -> an orphaned move job whose pixel state
        enters the checksum and causes OUT OF SYNC. Therefore check here BEFORE
        every move."""
        ids = set()
        for mj in self.model.get("moveJobs", []) or []:
            vid = mj.get("vehicleId")
            if vid is not None:
                ids.add(vid)
        return ids

    def has_movejob(self, unit_id):
        return unit_id in self.vehicle_ids_with_movejob()

    def first_building_factory(self, factory_type_name):
        """First FULLY BUILT factory of the type ('smallfactory'/'bigfactory') as a
        building object, or None. For unit production (changeBuildList needs an
        existing building - a factory under construction cannot produce anything
        yet)."""
        sid = None
        for (fp, sp), st in self._static_by_sid.items():
            if fp == 1 and st.get("name") == factory_type_name:
                sid = sp
                break
        if sid is None:
            return None
        for b in self.my_buildings():
            if self.unit_type(b) == sid:
                return b
        return None

    def first_vehicle_of_type(self, type_name):
        vs = self.vehicles_of_type(type_name)
        return vs[0] if vs else None

    def buildings_of_type(self, type_name):
        sid = self.special_buildings.get(type_name)
        return [b for b in self.my_buildings() if self.unit_type(b) == sid]

    def building_sid_by_name(self, name):
        """sID secondPart of a building type by its name in staticUnitData
        (e.g. 'smallfactory'->13, 'storage-metal'->32). None if unknown."""
        for (fp, sp), st in self._static_by_sid.items():
            if fp == 1 and st.get("name") == name:
                return sp
        return None

    def vehicle_sid_by_name(self, name):
        """sID secondPart of a vehicle type by its name in staticUnitData
        (firstPart==0). Analogous to building_sid_by_name, but for vehicles.
        Example: 'scout' -> 27 (from the data.json of the scout vehicle, verified).
        None if unknown."""
        for (fp, sp), st in self._static_by_sid.items():
            if fp == 0 and st.get("name") == name:
                return sp
        return None

    def base_footprint(self):
        """All fields of the SUPPLY NETWORK: occupied by own buildings that connect to
        the network (connectsToBase=true). NOT included: platform/bridge/road
        (connectsToBase=false) - they only make the field walkable for land units
        but do NOT hold the supply network together. A mine on a platform is only
        connected once a CONNECTOR establishes the link. Used for network
        connectivity and 'connected' checks."""
        cells = set()
        for b in self.my_buildings():
            name = self._static_by_sid.get(
                (self.unit_first(b), self.unit_type(b)), {}).get("name", "")
            if name in WATER_WALKABLE_BUILDINGS:
                continue   # platform/bridge/road: connectsToBase=false
            big = self.is_big_building_type(self.unit_type(b))
            cells |= self.footprint(self.pos(b), big)
        return cells

    def occupied_base_footprint(self):
        """All fields occupied by own buildings INCL. platform/bridge/road - for
        physical occupation/build-site checks (not network)."""
        cells = set()
        for b in self.my_buildings():
            big = self.is_big_building_type(self.unit_type(b))
            cells |= self.footprint(self.pos(b), big)
        return cells

    def is_connected_field(self, x, y, base_cells=None):
        """Does (x,y) border the existing base orthogonally (4-neighbourhood)? = connected."""
        if base_cells is None:
            base_cells = self.base_footprint()
        for nx, ny in self.neighbors4(x, y):
            if (nx, ny) in base_cells:
                return True
        return False

    def main_component(self):
        """The largest connected network island (= main base). Empty set if no
        buildings. Important for emergency builds: power station, storage,
        generator MUST connect to the main base, not to an isolated island
        (e.g. a not-yet-networked outer mine), otherwise they stand useless."""
        comps = self.network_components()
        return comps[0] if comps else set()

    def is_connected_to_main(self, x, y, main_cells=None):
        """Does (x,y) border the MAIN COMPONENT orthogonally (not some isolated
        island)?"""
        if main_cells is None:
            main_cells = self.main_component()
        for nx, ny in self.neighbors4(x, y):
            if (nx, ny) in main_cells:
                return True
        return False

    def _resource_lookup(self):
        """Fast access to the explored yield per field:
        {(x,y): {'type': ..., 'amount': ...}}. Explored fields only."""
        if getattr(self, "_res_lookup_cache", None) is None:
            d = {}
            for r in self.explored_resources():
                d[(r.get("x"), r.get("y"))] = {"type": r.get("type"),
                                               "amount": r.get("amount", 0)}
            self._res_lookup_cache = d
        return self._res_lookup_cache

    def mine_build_position(self, goal_field, builder=None, avoid=None,
                            target_type=None):
        """Best 2x2 build position (top-left corner) for a mine whose footprint covers
        the deposit goal_field. A mine sums the resources of ALL four fields of its
        2x2 area. Among all valid placements that contain goal_field, the one with
        the HIGHEST sum of the target resource (target_type) is chosen; on a tie the
        one with the highest total sum of all resources. This way the mine extracts
        the maximum from the (explored) deposits.
        Returns (ox,oy) or None. avoid: set of corners to be avoided."""
        if avoid is None:
            avoid = set()
        gx, gy = goal_field
        # do NOT count own mobile units (surveyor/constructor/pioneer) as a block
        # - they drive on. Only buildings and foreign units block.
        occ = self.occupied_fields_for_mine()
        water_ok = self.water_walkable_fields()
        lookup = self._resource_lookup()

        def field_amount(cell, only_type=None):
            r = lookup.get(cell)
            if not r:
                return 0
            if only_type is not None and r["type"] != only_type:
                return 0
            return r["amount"] or 0

        # all 2x2 placements that contain (gx,gy) (corner = one of the 4 fields).
        best = None  # (target_sum, total_sum, (ox,oy))
        for (ox, oy) in [(gx, gy), (gx - 1, gy), (gx, gy - 1), (gx - 1, gy - 1)]:
            if (ox, oy) in avoid:
                continue
            cells = [(ox, oy), (ox + 1, oy), (ox, oy + 1), (ox + 1, oy + 1)]
            # validity: all 4 fields in bounds and buildable for the BUILDING
            # (mine, land building). Do NOT use is_free_for_unit - the constructor
            # is amphibious and would accept water fields on which the (land) mine
            # cannot stand at all.
            valid = True
            for (cx, cy) in cells:
                if not self.in_bounds(cx, cy):
                    valid = False
                    break
                if not self.is_buildable_for_building(self.MINE_SID, cx, cy,
                                                      occ=occ, water_ok=water_ok):
                    valid = False
                    break
            if not valid:
                continue
            target_sum = sum(field_amount(c, target_type) for c in cells)
            total_sum = sum(field_amount(c) for c in cells)
            key = (target_sum, total_sum)
            if best is None or key > best[0]:
                best = (key, (ox, oy))
        return best[1] if best else None

    def mine_build_position_with_platforms(self, goal_field, avoid=None, target_type=None):
        """Like mine_build_position, but water fields are ALLOWED if they can be made
        buildable via water platforms. Returns (ox,oy) of the best 2x2 placement
        whose fields are either already buildable OR pure water fields (that can be
        platformed) - NO coast/blocked fields. For deposits blocked by water: first
        platforms, then mine. Returns (ox,oy) or None."""
        if avoid is None:
            avoid = set()
        gx, gy = goal_field
        # connectors and other passable buildings do NOT block the mine/platform.
        occ = self.blocking_fields_for_mine()
        water_ok = self.water_walkable_fields()
        lookup = self._resource_lookup()

        def field_amount(cell, only_type=None):
            r = lookup.get(cell)
            if not r:
                return 0
            if only_type is not None and r["type"] != only_type:
                return 0
            return r["amount"] or 0

        def platformable(cx, cy):
            """Field is USABLE for a (land) mine, possibly after building a platform:
            already buildable (land or existing platform/bridge) OR pure water
            without occupation (then a platform is possible)."""
            if not self.in_bounds(cx, cy):
                return False
            if (cx, cy) in occ:
                return False
            if self.is_buildable_for_building(self.MINE_SID, cx, cy,
                                              occ=occ, water_ok=water_ok):
                return True
            # not directly buildable -> only accept if WATER or COAST (both
            # platformable; MAXR: isWaterOrCoast allows AboveSea platforms on water
            # AND coast). Land/blocked NOT.
            return self.terrain_at(cx, cy) in (T_WATER, T_COAST)

        # DEMAND-WEIGHTED area choice - the same scoring as expansion_target, so
        # that the pioneers platform the fields most PRODUCTIVE by current demand
        # (not just the raw amount). sum(amount(type)*demand(type)).
        demand = self._demand_factor()

        def weighted_yield(cells):
            sbt = {"metal": 0, "oil": 0, "gold": 0}
            for c in cells:
                r = lookup.get(c)
                if r and r.get("type") in sbt:
                    sbt[r["type"]] += r.get("amount", 0) or 0
            return sum(sbt[t] * demand[t] for t in sbt)

        best = None
        for (ox, oy) in [(gx, gy), (gx - 1, gy), (gx, gy - 1), (gx - 1, gy - 1)]:
            if (ox, oy) in avoid:
                continue
            cells = [(ox, oy), (ox + 1, oy), (ox, oy + 1), (ox + 1, oy + 1)]
            if not all(platformable(cx, cy) for cx, cy in cells):
                continue
            # primarily demand-weighted yield, secondarily raw total amount as a
            # tiebreaker (stable choice at equal demand value).
            total_sum = sum(field_amount(c) for c in cells)
            key = (weighted_yield(cells), total_sum)
            if best is None or key > best[0]:
                best = (key, (ox, oy))
        return best[1] if best else None

    def platform_fields_needed(self, mine_pos):
        """Which fields of the 2x2 mine area still need a water platform so that the
        (land) mine can be built there?
        Return:
          - list of the pure water fields WITHOUT an existing platform/bridge (that
            still need to be platformed). Empty list = mine directly buildable.
          - None if the area is INVALID (a field is neither buildable nor
            platformable water, e.g. coast/blocked/occupied) - then this 2x2
            placement is unsuitable and must be re-chosen."""
        ox, oy = mine_pos
        cells = [(ox, oy), (ox + 1, oy), (ox, oy + 1), (ox + 1, oy + 1)]
        water_ok = self.water_walkable_fields()
        # PASSABLE buildings (connector etc.) do NOT count as blockers - a
        # platform may be built on a coupling.
        occ = self.blocking_fields_for_mine()
        need = []
        for (cx, cy) in cells:
            if not self.in_bounds(cx, cy):
                return None
            # already buildable for the mine (land or existing platform/bridge)?
            if self.is_buildable_for_building(self.MINE_SID, cx, cy,
                                              occ=occ, water_ok=water_ok):
                continue
            # otherwise only ok if WATER or COAST without a BLOCKING building
            # (connector allowed) - both platformable (MAXR: isWaterOrCoast).
            if self.terrain_at(cx, cy) in (T_WATER, T_COAST) and (cx, cy) not in occ:
                need.append((cx, cy))
            else:
                return None   # land(unbuildable)/blocked -> area invalid
        return need

    def find_connector_toward(self, builder, goal, avoid=None, occ=None, water_ok=None,
                              base_cells=None, stop_island=None):
        """Next coupling build field on the SHORTEST ORTHOGONAL path from the base to
        the goal. Uses route_fields (A*, 4-neighbourhood) and returns the first
        still-to-build field that is connected to the network - this way the chain
        grows straight and without superfluous connectors towards the goal.
        base_cells: what counts as 'connected network'. Default base_footprint (all
        buildings). For NETWORK REPAIR main_component is passed - otherwise the
        chain would wrongly connect to an ISOLATED island (whose fields are in
        base_footprint but do not hang on the main network), and the actual gap
        would never be bridged.
        Returns (x,y) or None."""
        if avoid is None:
            avoid = set()
        if occ is None:
            # for the coupling route choice: own platform/bridge/road are NOT an
            # obstacle - a connector can be built on them (coexists,
            # connectsToBase=false). They must therefore not count as occupied.
            # Existing connectors and real buildings remain blockers (no redundant
            # build). So: all occupations MINUS own platform/bridge/road.
            occ = set(self.occupied_fields())
            me_id = self.me.get("id") if self.me else None
            for p in self.model.get("players", []):
                if p.get("id") != me_id:
                    continue
                for b in p.get("buildings", []):
                    name = self._static_by_sid.get(
                        (self.unit_first(b), self.unit_type(b)), {}).get("name", "")
                    if name in WATER_WALKABLE_BUILDINGS:
                        big = self.is_big_building_type(self.unit_type(b))
                        occ -= self.footprint(self.pos(b), big)
        if water_ok is None:
            water_ok = self.water_walkable_fields()
        if base_cells is None:
            base_cells = self.base_footprint()
        if stop_island is None:
            stop_island = set()
        bx, by = self.pos(builder)
        gx, gy = goal

        # start point at the base that is nearest to the goal (anchor for A*).
        if base_cells:
            from_cell = min(base_cells, key=lambda c: (c[0]-gx)**2 + (c[1]-gy)**2)
        else:
            from_cell = (bx, by)
        path = self.route_fields(from_cell, (gx, gy))

        # choose the first path field that is buildable, free and connected to the
        # network. Fields of the TARGET ISLAND (stop_island) are NEVER a build field
        # - the coupling belongs NEXT TO the island building, not on it. Otherwise
        # the chain builds on the mine build site and collides with the constructor.
        for (fx, fy) in path:
            if (fx, fy) in avoid or (fx, fy) in base_cells or (fx, fy) in stop_island:
                continue
            if not self.is_free_for_unit(builder, fx, fy, occ=occ,
                                         water_ok=water_ok, ignore={(bx, by)}):
                continue
            if not self.is_connected_field(fx, fy, base_cells):
                continue   # the chain must grow contiguously from the base
            return (fx, fy)

        # fallback: no A* path usable -> as before, the nearest connected edge
        # field towards the goal (greedy), so that it does not block completely.
        ring = set()
        for (cx, cy) in base_cells:
            for n in self.neighbors4(cx, cy):
                ring.add(n)
        cands = []
        for (fx, fy) in ring:
            if (fx, fy) in base_cells or (fx, fy) in avoid or (fx, fy) in stop_island:
                continue
            if not self.is_free_for_unit(builder, fx, fy, occ=occ,
                                         water_ok=water_ok, ignore={(bx, by)}):
                continue
            if not self.is_connected_field(fx, fy, base_cells):
                continue
            cands.append((fx, fy))
        if not cands:
            return None
        cands.sort(key=lambda c: (c[0] - gx) ** 2 + (c[1] - gy) ** 2)
        return cands[0]

    def repair_route_segments(self, from_cell, to_cell):
        """Ordered list of the still-to-BUILD coupling fields of the repair stretch,
        from the MAIN COMPONENT (from_cell) towards the island (to_cell). Basis for
        the multi-pioneer stretch planning. Build time (1 pioneer) = len(list),
        since a pioneer builds 1 field/turn and loads at the growing network edge
        (no commuting).

        A coupling is NEVER built on an island field (building/site/network) - only
        on the gap IN BETWEEN. The last piece stands NEXT TO the target island and
        connects it. Therefore ALL fields of both involved islands are excluded
        (main + target), not just the one to_cell corner field (a 2x2 mine has four
        footprint fields). Order: near-network -> near-island."""
        path = self.route_fields(from_cell, to_cell)
        if not path:
            return []
        comps = self.network_components()
        main = comps[0] if comps else set()
        # target island = the component that contains to_cell (all its fields, not
        # just the to_cell corner - otherwise the route builds on the other mine fields).
        target_island = set()
        for c in comps[1:]:
            if tuple(to_cell) in c:
                target_island = c
                break
        # blocker/occupied view for the REPAIR:
        # - foreign/neutral objects: block (the route avoids them anyway).
        # - own platform/bridge/road: NOT a network node (connectsToBase=false)
        #   -> a connector must STILL be built there (coexists). So do NOT exclude
        #   as "already occupied".
        # - own CONNECTOR / real connectable building: the connection already
        #   stands there -> no new build field.
        foreign = self.foreign_blocking_fields()
        already_connected = set()   # own connectors + connectable buildings
        me_id = self.me.get("id") if self.me else None
        for p in self.model.get("players", []):
            if p.get("id") != me_id:
                continue
            for b in p.get("buildings", []):
                name = self._static_by_sid.get(
                    (self.unit_first(b), self.unit_type(b)), {}).get("name", "")
                if name in WATER_WALKABLE_BUILDINGS:
                    continue   # platform/bridge/road: stays a build field (connector on top)
                big = self.is_big_building_type(self.unit_type(b))
                already_connected |= self.footprint(_xy(b["position"]), big)
        segs = []
        for (x, y) in path:
            if (x, y) in main:
                continue          # already supplied network -> no coupling needed
            if (x, y) in target_island:
                continue          # belongs to the target island (building/site) - no build
            if (x, y) in foreign:
                continue          # foreign/neutral -> do not build on (route avoids it)
            if (x, y) in already_connected:
                continue          # own connector / connectable building
            segs.append((x, y))
        return segs

    def find_build_position(self, builder, building_sid, occ=None, water_ok=None,
                            require_connected=True, avoid=None, connect_to_main=False):
        """Build-site choice. Returns build_pos (x,y) or None. avoid: set of corners to
        be avoided (previously rejected by the bridge).
        connect_to_main: if True, the build site must connect to the MAIN COMPONENT
        (largest network island), not to some isolated island - for emergency
        builds (station/generator/storage) that must stand at the main base,
        otherwise they are uselessly detached."""
        if avoid is None:
            avoid = set()
        if occ is None:
            # IMPORTANT: own MOBILE units (other pioneers/constructors/surveyors)
            # are NOT permanent build blockers - they can yield to the
            # higher-priority build (clear_units_from_fields sends them away).
            # Therefore the occupation view WITHOUT own vehicles: real blockers
            # remain (all buildings, foreign/neutral units), own mobile units do
            # not count. This way a constructor can build a 2x2 factory at its
            # current position (it is a corner), even if own units still stand
            # there - NO driving needed.
            occ = self.occupied_fields_for_mine()
        if water_ok is None:
            water_ok = self.water_walkable_fields()
        base_cells = self.base_footprint()
        main_cells = self.main_component() if connect_to_main else None
        bx, by = self.pos(builder)
        is_big = self.is_big_building_type(building_sid)

        def _connected(fx, fy):
            if connect_to_main:
                return self.is_connected_to_main(fx, fy, main_cells)
            return self.is_connected_field(fx, fy, base_cells)

        # EFFICIENCY PRIORITY (no driving): if the builder can build where it
        # already STANDS, it should do so - then the approach is saved and the
        # building stands one turn earlier. Only if that fails do we look for the
        # nearest place (below). Applies to 1x1 (building on the own field) as well
        # as 2x2 (an area that includes the builder, so that the server places it
        # on the area without an approach).
        # UNLOAD PROTECTION: a candidate must not push an adjacent unload building
        # (factory/depot/dock/barracks - incl. sites) below the required minimum
        # number of free unload fields. Helper checks this per candidate footprint.
        def _keeps_neighbor_exits(cells):
            return self.build_site_keeps_neighbor_exits(cells, occ=occ,
                                                        water_ok=water_ok)

        # RESOURCE PROTECTION: non-mine buildings must not build over a discovered
        # deposit (coupling/platform/bridge/road excepted - see
        # building_blocks_resource). Checks all footprint cells of a candidate.
        def _no_resource_block(cells):
            return not any(self.building_blocks_resource(building_sid, cx, cy)
                           for cx, cy in cells)

        def _cells_buildable_connected(cells):
            if any((c in avoid) for c in cells):
                return False
            if not all(self.is_buildable_for_building(building_sid, cx, cy,
                       occ=occ, water_ok=water_ok, ignore={(bx, by)})
                       for cx, cy in cells):
                return False
            if require_connected and not any(_connected(cx, cy) for cx, cy in cells):
                return False
            if not _keeps_neighbor_exits(cells):
                return False
            if not _no_resource_block(cells):
                return False
            return True

        if not is_big:
            # 1x1: MAXR builds the building exactly on the field of the builder
            # (buildPosition == vehicle position). If the builder stands on a
            # buildable, networked field -> build right here, no driving.
            if (bx, by) not in avoid \
                    and self.is_buildable_for_building(building_sid, bx, by,
                                                       occ=occ, water_ok=water_ok,
                                                       ignore={(bx, by)}) \
                    and (not require_connected or _connected(bx, by)) \
                    and _keeps_neighbor_exits({(bx, by)}) \
                    and _no_resource_block({(bx, by)}):
                return (bx, by)
        else:
            # 2x2: check the four areas that INCLUDE the builder (builder as
            # top-left, top-right, bottom-left, bottom-right corner). If one of them
            # is fully buildable+networked -> the builder already stands on it, no
            # approach needed. Nearest-corner order does not matter here, since all
            # four contain the builder.
            for (ox, oy) in ((bx, by), (bx - 1, by), (bx, by - 1), (bx - 1, by - 1)):
                cells = [(ox, oy), (ox + 1, oy), (ox, oy + 1), (ox + 1, oy + 1)]
                if _cells_buildable_connected(cells):
                    return (ox, oy)

        if not is_big:
            # 1x1: free fields that border the base.
            ring = set()
            for (cx, cy) in base_cells:
                for n in self.neighbors4(cx, cy):
                    ring.add(n)
            cands = []
            for (fx, fy) in ring:
                if (fx, fy) in base_cells:
                    continue
                if (fx, fy) in avoid:
                    continue
                # the build site must be buildable for the BUILDING (the terrain of
                # the building), not just walkable for the (amphibious) builder.
                if not self.is_buildable_for_building(building_sid, fx, fy,
                                                      occ=occ, water_ok=water_ok,
                                                      ignore={(bx, by)}):
                    continue
                if require_connected and not _connected(fx, fy):
                    continue
                if not _keeps_neighbor_exits({(fx, fy)}):
                    continue
                if not _no_resource_block({(fx, fy)}):
                    continue
                cands.append((fx, fy))
            if not cands:
                return None
            cands.sort(key=lambda c: (c[0]-bx)**2 + (c[1]-by)**2)
            return cands[0]

        # BigBuilding 2x2: top-left corner whose 4 fields are free AND that borders
        # the base.
        best = None
        for radius in range(0, 10):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    ox, oy = bx + dx, by + dy
                    if (ox, oy) in avoid:
                        continue
                    cells = [(ox, oy), (ox+1, oy), (ox, oy+1), (ox+1, oy+1)]
                    if not all(self.is_buildable_for_building(building_sid, cx, cy,
                            occ=occ, water_ok=water_ok, ignore={(bx, by)})
                            for cx, cy in cells):
                        continue
                    if require_connected:
                        if not any(_connected(cx, cy) for cx, cy in cells):
                            continue
                    if not _keeps_neighbor_exits(set(cells)):
                        continue
                    if not _no_resource_block(cells):
                        continue
                    d = dx*dx + dy*dy
                    if best is None or d < best[0]:
                        best = (d, (ox, oy))
            if best is not None:
                return best[1]
        return None

    def is_big_building_type(self, building_sid_secondpart):
        st = self._static_by_sid.get((1, building_sid_secondpart))
        return bool(st and st.get("isBig"))

    def build_cost(self, building_sid_secondpart):
        """Base build cost (ore) of a building type from the player's dynamicUnitsData
        (NOT staticUnitData - costs can vary via upgrades). This is the cost at
        speed x1; higher speed costs more (calcTurboBuild in the server). Usable as
        a minimum requirement for the reload decision."""
        if not self.me:
            return 0
        for d in self.me.get("dynamicUnitsData", []):
            idv = d.get("id", {})
            if idv.get("firstPart") == 1 and idv.get("secondPart") == building_sid_secondpart:
                return d.get("buildCosts", 0) or 0
        return 0

    # ---------- terrain ----------
    def terrain_at(self, x, y):
        if not self.terrain:
            return None
        w, h = self.terrain["width"], self.terrain["height"]
        if x < 0 or y < 0 or x >= w or y >= h:
            return T_BLOCKED
        return self.terrain["data"][y * w + x]

    def in_bounds(self, x, y):
        if not self.terrain:
            return True  # without a map no bounds check is possible
        return 0 <= x < self.terrain["width"] and 0 <= y < self.terrain["height"]

    # ---------- surveyor pathfinding: map/resource helpers ----------
    # (Mirror map.isWater/isCoast, road modifiesSpeed, player.hasResourceExplored,
    #  map.getResource - 1:1 for the ported cSurveyorAi.)
    def terrain_is_water(self, x, y):
        return self.terrain_at(x, y) == T_WATER

    def terrain_is_coast(self, x, y):
        return self.terrain_at(x, y) == T_COAST

    def map_size(self):
        if self.terrain:
            return (self.terrain["width"], self.terrain["height"])
        ms = (self.me or {}).get("mapSize", {})
        return (ms.get("X", 0), ms.get("Y", 0))

    def road_speed_modifier(self, x, y):
        """modifiesSpeed of the road/ground building on (x,y), otherwise 0.
        MAXR: only the BaseBuilding counts; a road makes movement cheaper."""
        for p in self.model.get("players", []):
            for b in p.get("buildings", []):
                if _xy(b["position"]) != (x, y):
                    continue
                st = self._static_by_sid.get(
                    (self.unit_first(b), self.unit_type(b)), {})
                mod = st.get("modifiesSpeed", 0) or 0
                if mod:
                    return mod
        return 0

    def _resource_mask_known(self, x, y):
        """player.hasResourceExplored(pos): True if the own surveyor has surveyed the
        field. MAXR serialises the ResourceMap as 2 hex digits PER FIELD
        (getHexValue of a uint8_t), field offset = x + y*width. Explored = the byte
        at position 2*offset is != '00'."""
        rm = (self.me or {}).get("ResourceMap")
        ms = (self.me or {}).get("mapSize", {})
        w = ms.get("X", 0); h = ms.get("Y", 0)
        if not rm or w <= 0 or h <= 0:
            return False
        if x < 0 or y < 0 or x >= w or y >= h:
            return False
        offset = x + y * w
        i = 2 * offset
        if i < 0 or i + 1 >= len(rm):
            return False
        return rm[i:i + 2] != "00"

    def has_resource_explored(self, x, y):
        return self._resource_mask_known(x, y)

    def _explored_resource_set(self):
        """Cache: set of (x,y) with an explored deposit
        (map.getResource(pos).typ != None AND explored)."""
        cached = getattr(self, "_explored_res_cache", None)
        if cached is not None:
            return cached
        s = set()
        for r in self.explored_resources():
            s.add((r.get("x"), r.get("y")))
        self._explored_res_cache = s
        return s

    def resource_at_explored(self, x, y):
        return (x, y) in self._explored_resource_set()

    def field_resource_amount(self, x, y):
        """Amount of the resource on (x,y) per the explored lookup (0 if none). For the
        anchor choice: the anchor of a mine site should lie on the strongest
        resource field of its 2x2."""
        r = self._resource_lookup().get((x, y))
        return (r.get("amount", 0) or 0) if r else 0

    def building_blocks_resource(self, building_sid, x, y):
        """True if a BUILDING of this type on (x,y) would BUILD OVER a discovered
        deposit - that is forbidden (the field should stay free for a later mine).
        Excepted:
          - the MINE itself (it SHOULD go on the deposit),
          - non-blocking/over-buildable infrastructure: coupling (connector),
            water platform (platform), bridge (bridge), road (road) - they do not
            consume the deposit and may be built on it.
        For all other buildings (storage, generator, radar, station, factories,
        depot, dock, ...) a discovered deposit is taboo."""
        if not self.resource_at_explored(x, y):
            return False                      # no (known) deposit -> irrelevant
        if building_sid == self.MINE_SID:
            return False                      # mine may/should go on the deposit
        name = self._static_by_sid.get((1, building_sid), {}).get("name", "")
        if name in NON_BLOCKING_BUILDINGS:
            return False                      # coupling/platform/bridge/road
        return True                           # otherwise: deposit would be built over

    # ---------- occupation / collision ----------
    def footprint(self, pos, is_big):
        x, y = pos
        if is_big:
            return {(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)}
        return {(x, y)}

    def occupied_fields(self):
        """All fields occupied by VISIBLE units/buildings (all players). Mine (2x2) and
        other big buildings as footprint."""
        occ = set()
        for p in self.model.get("players", []):
            for v in p.get("vehicles", []):
                occ |= self.footprint(_xy(v["position"]), False)
            for b in p.get("buildings", []):
                big = self.is_big_building_type(self.unit_type(b))
                occ |= self.footprint(_xy(b["position"]), big)
        # neutral units/buildings, if present
        for v in self.model.get("neutralVehicles", []):
            occ |= self.footprint(_xy(v["position"]), False)
        for b in self.model.get("neutralBuildings", []):
            occ |= self.footprint(_xy(b["position"]), False)
        return occ

    def occupied_fields_for_mine(self):
        """Like occupied_fields, but WITHOUT the own mobile units (vehicles). For the
        mine build-site choice: a field on which the own surveyor/constructor/
        pioneer currently stands is NOT a permanent obstacle - the unit drives on.
        Real blockers remain: ALL buildings (also own) and FOREIGN/neutral
        vehicles."""
        me_id = self.me.get("id") if self.me else None
        occ = set()
        for p in self.model.get("players", []):
            is_self = (p.get("id") == me_id)
            if not is_self:
                for v in p.get("vehicles", []):
                    occ |= self.footprint(_xy(v["position"]), False)
            for b in p.get("buildings", []):
                big = self.is_big_building_type(self.unit_type(b))
                occ |= self.footprint(_xy(b["position"]), big)
        for v in self.model.get("neutralVehicles", []):
            occ |= self.footprint(_xy(v["position"]), False)
        for b in self.model.get("neutralBuildings", []):
            occ |= self.footprint(_xy(b["position"]), False)
        return occ

    def foreign_blocking_fields(self):
        """ROUTE mask (mask B): fields that block a CONNECTION route and must be
        avoided. ONLY FOREIGN units/buildings and NEUTRAL objects (incl. rubble -
        neutralBuildings with rubbleValue) block. OWN buildings and OWN vehicles are
        NOT included -> they are PASSABLE for the route: own buildings are/become
        part of the network (nothing is built there), own vehicles move aside
        (drive-to-the-side, the field stays a build field). This deliberately
        differs from the BUILD mask (blocking_fields_for_mine, where own buildings
        do block)."""
        me_id = self.me.get("id") if self.me else None
        occ = set()
        for p in self.model.get("players", []):
            if p.get("id") == me_id:
                continue   # OWN buildings/vehicles: no route block
            for v in p.get("vehicles", []):
                occ |= self.footprint(_xy(v["position"]), False)
            for b in p.get("buildings", []):
                big = self.is_big_building_type(self.unit_type(b))
                occ |= self.footprint(_xy(b["position"]), big)
        for v in self.model.get("neutralVehicles", []):
            occ |= self.footprint(_xy(v["position"]), False)
        for b in self.model.get("neutralBuildings", []):
            # neutral objects incl. rubble (rubbleValue > 0) block the route
            big = self.is_big_building_type(self.unit_type(b))
            occ |= self.footprint(_xy(b["position"]), big)
        return occ

    def blocking_fields_for_mine(self):
        """Like occupied_fields_for_mine, but PASSABLE buildings (connector, platform,
        bridge, road) do NOT count as blockers. A platform/mine can be built on a
        coupling (different surfacePosition, no conflict). This way water fields
        with a connector are correctly recognised as platformable instead of
        wrongly as occupied."""
        me_id = self.me.get("id") if self.me else None
        occ = set()
        for p in self.model.get("players", []):
            is_self = (p.get("id") == me_id)
            if not is_self:
                # FOREIGN: everything blocks (vehicles AND buildings, incl. foreign
                # connectors - the bot does not build on enemy territory).
                for v in p.get("vehicles", []):
                    occ |= self.footprint(_xy(v["position"]), False)
                for b in p.get("buildings", []):
                    big = self.is_big_building_type(self.unit_type(b))
                    occ |= self.footprint(_xy(b["position"]), big)
            else:
                # OWN: passable buildings (connector/platform/bridge/road) do NOT
                # count as blockers. Own mobile units likewise not (they drive on) -
                # therefore only buildings here.
                for b in p.get("buildings", []):
                    name = self._static_by_sid.get(
                        (self.unit_first(b), self.unit_type(b)), {}).get("name", "")
                    if name in NON_BLOCKING_BUILDINGS:
                        continue
                    big = self.is_big_building_type(self.unit_type(b))
                    occ |= self.footprint(_xy(b["position"]), big)
        for v in self.model.get("neutralVehicles", []):
            occ |= self.footprint(_xy(v["position"]), False)
        for b in self.model.get("neutralBuildings", []):
            occ |= self.footprint(_xy(b["position"]), False)
        return occ

    def non_blocking_building_fields(self):
        """All fields covered by PASSABLE buildings (connector/road/bridge/platform).
        A vehicle MAY stand on such fields (MAXR: possiblePlaceVehicle allows
        vehicles on these buildings). Used so that, when unloading from factories, a
        diagonally adjacent field on which only a coupling stands is NOT wrongly
        excluded."""
        fields = set()
        for p in self.model.get("players", []):
            for b in p.get("buildings", []):
                name = self._static_by_sid.get(
                    (self.unit_first(b), self.unit_type(b)), {}).get("name", "")
                if name in NON_BLOCKING_BUILDINGS:
                    big = self.is_big_building_type(self.unit_type(b))
                    fields |= self.footprint(_xy(b["position"]), big)
        return fields

    def water_walkable_fields(self):
        """Water fields made walkable for land units by bridge/platform/road (from the
        visible buildings)."""
        walkable = set()
        names = {}
        for sid, st in self._static_by_sid.items():
            if st.get("name") in WATER_WALKABLE_BUILDINGS:
                names[sid[1]] = True
        for p in self.model.get("players", []):
            for b in p.get("buildings", []):
                if names.get(self.unit_type(b)):
                    walkable.add(_xy(b["position"]))
        return walkable

    def unit_factors(self, u):
        """Terrain factors of the concrete unit from the static data.
        factorGround/Sea/Coast/Air: 0 = this terrain NOT traversable.
        Amphibious (e.g. engineer/constructor/surveyor): ground>0 AND sea>0.
        Pure land unit: ground>0, sea=0. Ship: ground=0, sea>0. Air: air>0.
        """
        st = self._static_by_sid.get((self.unit_first(u), self.unit_type(u)))
        if not st:
            return {"ground": 1.0, "sea": 0.0, "coast": 1.0, "air": 0.0}
        return {
            "ground": st.get("factorGround", 0.0) or 0.0,
            "sea": st.get("factorSea", 0.0) or 0.0,
            "coast": st.get("factorCoast", 0.0) or 0.0,
            "air": st.get("factorAir", 0.0) or 0.0,
        }

    def is_free_for_unit(self, u, x, y, occ=None, water_ok=None, ignore=None):
        """Is (x,y) walkable for THIS concrete unit u?
        Considers the individual terrain factors of the unit (amphibious / land only
        / ship / air) as well as occupation. Bridges/platforms make water walkable
        for land units.
        """
        if not self.in_bounds(x, y):
            return False
        f = self.unit_factors(u)

        # air unit: terrain irrelevant (only occupation by other air units counts,
        # which we do not separate here for simplicity - the bridge is the fallback).
        if f["air"] > 0:
            return True

        t = self.terrain_at(x, y)
        if t == T_BLOCKED:
            return False
        if t == T_WATER:
            ww = water_ok if water_ok is not None else self.water_walkable_fields()
            # traversable if the unit can do water OR a bridge/platform is there
            if not (f["sea"] > 0 or (x, y) in ww):
                return False
        elif t == T_COAST:
            # coast: needs the coast factor (some pure land units cannot do this)
            if not (f["coast"] > 0 or f["ground"] > 0 and f["sea"] > 0):
                return False
        else:  # land
            if f["ground"] <= 0:
                return False

        o = occ if occ is not None else self.occupied_fields()
        if (x, y) in o and (ignore is None or (x, y) not in ignore):
            return False
        return True

    def is_buildable_for_building(self, building_sid, x, y, occ=None, water_ok=None, ignore=None):
        """Can a BUILDING of this type stand on (x,y)? Checks the terrain factors of the
        BUILDING (not the mobility of the builder!). Important: an amphibious
        constructor can drive into water, but a land building (factorGround=1,
        factorSea=0, e.g. station/mine/factory) must NOT be built there. Without
        this separation the build-site search picks water fields that the builder
        reaches but on which the build is rejected."""
        if not self.in_bounds(x, y):
            return False
        st = self._static_by_sid.get((1, building_sid))
        fg = (st.get("factorGround", 0) if st else 0) or 0
        fc = (st.get("factorCoast", 0) if st else 0) or 0
        fs = (st.get("factorSea", 0) if st else 0) or 0
        t = self.terrain_at(x, y)
        if t == T_BLOCKED:
            return False
        if t == T_WATER:
            ww = water_ok if water_ok is not None else self.water_walkable_fields()
            # sea building directly; land building only via bridge/platform.
            if not (fs > 0 or (x, y) in ww):
                return False
        elif t == T_COAST:
            # coast: the BUILDING needs factorCoast>0 (NOT factorGround - coast is
            # not land). Mine/station have factorCoast=0 -> not buildable on coast.
            # Matches C++ map.cpp: (coast && factorCoast==0)->false.
            if not (fc > 0 or (x, y) in (water_ok if water_ok is not None
                                         else self.water_walkable_fields())):
                return False
        else:  # land
            if fg <= 0:
                return False
        o = occ if occ is not None else self.occupied_fields()
        if (x, y) in o and (ignore is None or (x, y) not in ignore):
            return False
        return True

    def is_free_for_ground(self, x, y, occ=None, water_ok=None, ignore=None):
        """Simplified check for a generic LAND UNIT (no water except bridge). For
        unit-specific checks use is_free_for_unit! Kept for simple cases."""
        if not self.in_bounds(x, y):
            return False
        t = self.terrain_at(x, y)
        if t == T_BLOCKED:
            return False
        if t == T_WATER:
            ww = water_ok if water_ok is not None else self.water_walkable_fields()
            if (x, y) not in ww:
                return False
        o = occ if occ is not None else self.occupied_fields()
        if (x, y) in o and (ignore is None or (x, y) not in ignore):
            return False
        return True

    def neighbors4(self, x, y):
        return [(x, y - 1), (x + 1, y), (x, y + 1), (x - 1, y)]

    def neighbors8(self, x, y):
        return [(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                if not (dx == 0 and dy == 0)]

    def _free_check(self, unit):
        """Returns the appropriate free function: unit-specific if unit is given,
        otherwise the generic land-unit check."""
        if unit is not None:
            return lambda x, y, **kw: self.is_free_for_unit(unit, x, y, **kw)
        return self.is_free_for_ground

    def free_neighbors4(self, x, y, unit=None, **kw):
        chk = self._free_check(unit)
        return [n for n in self.neighbors4(x, y) if chk(*n, **kw)]

    def free_neighbors8(self, x, y, unit=None, **kw):
        chk = self._free_check(unit)
        return [n for n in self.neighbors8(x, y) if chk(*n, **kw)]

    def free_escape_field(self, from_pos, unit=None, occ=None, water_ok=None):
        """A free evasion field (8-neighbourhood) from from_pos that the unit can also
        ENTER (unit-specific if unit is given). For finishBuild: the unit stands on
        the finished building and must get off. Returns the nearest matching
        neighbour field or None."""
        cands = self.escape_candidates(from_pos, unit=unit, occ=occ, water_ok=water_ok)
        return cands[0] if cands else None

    def escape_candidates(self, from_pos, unit=None, toward=None, occ=None, water_ok=None):
        """ALL free evasion fields that border the unit DIRECTLY (8-neighbourhood
        around the vehicle position) - because the server/bridge validation of
        finishBuild requires isNextTo(esc) RELATIVE TO THE VEHICLE POSITION. Fields
        at distance 2 are rejected by the bridge, even if they are free.
        The just-finished (2x2) building is set as 'ignore' and excluded from the
        candidates (otherwise it wrongly counts as an obstacle). The real build area
        comes from buildBigSavedPosition (the build corner), NOT from the vehicle
        position (the builder may stand on another of the four footprint cells).
        Optionally sorted by proximity to 'toward'.
        Returns [(x,y), ...] (best candidate first)."""
        x, y = from_pos
        if occ is None:
            occ = self.occupied_fields()
        if water_ok is None:
            water_ok = self.water_walkable_fields()
        chk = self._free_check(unit)

        # determine and ignore the real build area (the just-built building).
        ignore = set()
        # IMPORTANT: 'big' must refer to the BUILT BUILDING (e.g. bigfactory =
        # 2x2), NOT to the vehicle type of the builder. A constructor as a vehicle
        # is not 'big', but builds a 2x2 building and occupies its 2x2 footprint
        # during the build. If this is confused, the function suggests evasion
        # fields that lie IN the own build footprint - the bridge then rejects
        # finishBuild (possiblePlace/isNextTo) and the builder sticks for turns.
        big = False
        if unit is not None:
            bt = unit.get("buildingType") or unit.get("buildingTyp")
            if isinstance(bt, dict) and bt.get("secondPart") is not None:
                big = self.is_big_building_type(bt.get("secondPart"))
        if unit is not None:
            saved = unit.get("buildBigSavedPosition")
            if isinstance(saved, dict) and "X" in saved:
                ox, oy = saved["X"], saved["Y"]
                ignore |= {(ox, oy), (ox+1, oy), (ox, oy+1), (ox+1, oy+1)}
            elif big:
                # build corner unknown (buildBigSavedPosition missing): exclude ALL
                # fields that belong to ANY 2x2 area that has the vehicle position as
                # a corner. This way no evasion target lies in the actual build
                # footprint, regardless of which corner the builder is on.
                for (cx, cy) in ((x, y), (x-1, y), (x, y-1), (x-1, y-1)):
                    ignore |= {(cx, cy), (cx+1, cy), (cx, cy+1), (cx+1, cy+1)}
        ignore.add(from_pos)   # own field never as an evasion target

        # candidates: for a 2x2 build the evasion fields must lie OUTSIDE the build
        # footprint and border ANY footprint field (the bridge checks isNextTo
        # relative to the vehicle position as 2x2 - satisfied if the field borders
        # the 2x2 block). For 1x1 as before the direct 8-neighbours of the vehicle
        # position.
        cands = []
        if big:
            # collect the border around the (assumed) 2x2 block. We take the
            # vehicle position as one corner and consider the block that has the
            # most free border fields - in practice: all fields that border the 2x2
            # block around from_pos and are free.
            block = {(x, y), (x+1, y), (x, y+1), (x+1, y+1)}
            ring = set()
            for (cx, cy) in block:
                for n in self.neighbors8(cx, cy):
                    if n not in block and n not in ignore:
                        ring.add(n)
            for n in ring:
                if chk(*n, occ=occ, water_ok=water_ok, ignore=ignore):
                    cands.append(n)
        if not cands:
            # fallback / 1x1: direct 8-neighbours of the vehicle position.
            for n in self.neighbors8(x, y):
                if n in ignore:
                    continue
                if chk(*n, occ=occ, water_ok=water_ok, ignore=ignore):
                    cands.append(n)
        if toward is not None:
            tx, ty = toward
            cands.sort(key=lambda c: (c[0] - tx) ** 2 + (c[1] - ty) ** 2)
        return cands

    # ---------- situation assessment (stage 1 of the high-level algorithm) ----------
    def _sum_building_field(self, field):
        return sum(b.get(field, 0) or 0 for b in self.my_buildings())

    def metal_income(self):
        """Ore income: sum of metalProd of all own buildings (only mines > 0)."""
        return self._sum_building_field("metalProd")

    def oil_income(self):
        return self._sum_building_field("oilProd")

    # ----- builder-vehicle production (constructors/pioneers) --------------------
    # Target values scale with the GROSS metal output (metal_income = sum of
    # metalProd of all mines). Rule of thumb: constructors = output/10,
    # pioneers = output/7.5, each ROUNDED DOWN. Continuous production up to the
    # target; beyond that metal stays free for military/other units.
    CONSTRUCTOR_PER_METAL = 10.0
    PIONEER_PER_METAL = 7.5

    def target_constructors(self):
        return int(self.metal_income() / self.CONSTRUCTOR_PER_METAL)

    def target_pioneers(self):
        return int(self.metal_income() / self.PIONEER_PER_METAL)

    def map_explored_fraction(self):
        """Fraction of the fields SURVEYED (known) by the surveyor out of the whole map
        (0.0-1.0). 'Known' = the bot knows what lies on the field (resource type OR
        'no resource'); 'unknown' = not yet surveyed. Source: the ResourceMap mask
        of the own player ('1' = explored). This is NOT the fog of war and not the
        deposits themselves, but the pure surveyor exploration state."""
        if not self.me:
            return 1.0
        rm = self.me.get("ResourceMap")
        ms = self.me.get("mapSize", {})
        w = ms.get("X", 0); h = ms.get("Y", 0)
        total = w * h
        if not rm or total <= 0:
            return 1.0
        # ResourceMap = 2 hex digits per field; explored = byte pair != '00'.
        known = sum(1 for i in range(0, min(len(rm), 2 * total), 2)
                    if rm[i:i + 2] != "00")
        return known / total

    def target_surveyors(self):
        """Surveyor target count by exploration degree (table). At the start (barely
        explored) many surveyors for fast exploration, fewer with increasing
        exploration:
          <=10% -> 10, <=20% -> 8, from 30% linear 10 - percent/10
          (30%->7, 40%->6, ... 90%->1, 100%->0)."""
        pct = self.map_explored_fraction() * 100.0
        if pct <= 10:
            return 10
        if pct <= 20:
            return 8
        # from here linearly descending: 10 - round(pct/10), capped at >=0
        return max(0, 10 - int(round(pct / 10.0)))

    def _vehicles_in_production(self, vehicle_sid):
        """Counts vehicles of this type that are in any factory buildList (in
        production or scheduled) OR waiting finished in a factory's storedUnits
        (built, not yet unloaded)."""
        count = 0
        for b in self.my_buildings():
            for job in (b.get("buildList", []) or []):
                t = job.get("type", {}) if isinstance(job, dict) else {}
                if isinstance(t, dict) and t.get("secondPart") == vehicle_sid:
                    count += 1
            # finished, still-stored vehicles (storedUnitIds -> check type)
        return count

    def count_constructors_incl_production(self):
        sid = self.special_vehicles.get("constructor")
        live = len(self.vehicles_of_type("constructor"))
        return live + (self._vehicles_in_production(sid) if sid is not None else 0)

    def count_pioneers_incl_production(self):
        sid = self.special_vehicles.get("engineer")
        live = len(self.vehicles_of_type("engineer"))
        return live + (self._vehicles_in_production(sid) if sid is not None else 0)

    def count_surveyors_incl_production(self):
        sid = self.special_vehicles.get("surveyor")
        live = len(self.vehicles_of_type("surveyor"))
        return live + (self._vehicles_in_production(sid) if sid is not None else 0)

    def count_scouts_incl_production(self):
        """Number of scouts: live + in production/queue of the factory. Uses
        vehicle_sid_by_name (firstPart=0, name='scout', secondPart=27) instead of
        special_vehicles, since scout has no special_vehicles entry."""
        sid = self.vehicle_sid_by_name("scout")
        if sid is None:
            return 0
        live = sum(1 for v in self.my_vehicles() if self.unit_type(v) == sid)
        return live + self._vehicles_in_production(sid)

    def count_bulldozers_incl_production(self):
        """Number of bulldozers: live + in production. Bulldozer (firstPart=0,
        name='bulldozer') is the ONLY unit with canClearArea=True (verified)."""
        sid = self.vehicle_sid_by_name("bulldozer")
        if sid is None:
            return 0
        live = sum(1 for v in self.my_vehicles() if self.unit_type(v) == sid)
        return live + self._vehicles_in_production(sid)

    def rubble_fields(self):
        """All VISIBLE rubble objects as a list of dicts [{"id","pos","value","big"}].
        Rubble is in the state a NEUTRAL building with rubbleValue > 0 and the type
        ID firstPart==2 (small={2,1}, big={2,2}, verified). We recognise it
        primarily via rubbleValue > 0 (the real field), the firstPart==2 check
        serves as a safeguard."""
        out = []
        for b in self.model.get("neutralBuildings", []):
            rv = (b.get("rubbleValue", 0) or 0)
            fp = self.unit_first(b)
            if rv > 0 or fp == 2:
                out.append({
                    "id": b.get("id"),
                    "pos": tuple(self.pos(b)),
                    "value": rv,
                    "big": fp == 2 and self.unit_type(b) == 2,  # {2,2} = big
                })
        return out

    def count_rubble(self):
        """Number of visible rubble objects (for bulldozer production:
        >0 -> 1 bulldozer, >10 -> 2 bulldozers)."""
        return len(self.rubble_fields())

    def rubble_on_fields(self, fields):
        """Which of the given fields are covered by rubble? Returns the subset of
        'fields' on which a rubble object stands. For the site planner: rubble on
        the mine area -> upstream bulldozer clear component before the constructor
        builds."""
        want = {tuple(f) for f in fields}
        if not want:
            return []
        rubble_pos = {r["pos"] for r in self.rubble_fields()}
        return [f for f in want if f in rubble_pos]

    def stored_vehicle_ids(self):
        """IDs of all finished-produced vehicles still stored in an own building
        (waiting to be unloaded via 'activate'). Returns a list of
        (building, vehicle_id)."""
        out = []
        for b in self.my_buildings():
            for vid in (b.get("storedUnitIds", []) or []):
                out.append((b, vid))
        return out
    
    def building_can_unload_finished_unit(self, unit_id):
        """True ONLY if unit_id is an own, finished BUILDING that itself builds units
        (canBuild != empty) AND currently has a finished unit ready for the first
        unload. Any other ID (vehicle/nonsense) -> False."""
        b = None
        for cand in self.my_buildings():
            if cand.get("id") == unit_id:
                b = cand
                break
        if b is None:
            return False
        if self.unit_first(b) != 1:                       # is a building
            return False
        st = self._static_by_sid.get((self.unit_first(b), self.unit_type(b))) or {}
        if not (st.get("canBuild") or "").strip():        # can build units
            return False
        bl = b.get("buildList", []) or []
        if not bl:
            return False
        if b.get("isWorking"):
            return False
        head = bl[0] if isinstance(bl[0], dict) else {}
        rem = head.get("remainingMetal")
        if rem is None or rem > 0:
            return False
        return True

    def factories_with_finished_unit(self):
        """Own unit-building buildings that have just finished producing a unit and
        must unload it via 'finishBuild'."""
        return [b for b in self.my_buildings()
                if self.building_can_unload_finished_unit(b.get("id"))]

    # oil consumption per station/generator (verified): energy_big 6 oil/6 energy,
    # energy_small 2 oil/1 energy.
    STATION_OIL_NEED = 6
    GENERATOR_OIL_NEED = 2
    MAX_GENERATORS = 2   # from here no further generators - save up for a station

    def generator_count(self):
        """Number of built generators (energy_small, sid 8), regardless of on/off."""
        sid = self.building_sid_by_name("energy small")
        if sid is None:
            return 0
        return sum(1 for b in self.my_buildings() if self.unit_type(b) == sid)

    def station_count(self):
        """Number of built stations (energy_big / Energy_Big)."""
        sid = self.building_sid_by_name("Energy_Big")
        if sid is None:
            return 0
        return sum(1 for b in self.my_buildings() if self.unit_type(b) == sid)

    def station_count_incl_construction(self):
        """Number of power stations = finished + under construction. Under-construction
        counts so that the '1 station per 4 mines' rule does not trigger another
        station every turn while one is already being built."""
        sid = self.building_sid_by_name("Energy_Big")
        if sid is None:
            return 0
        return self.count_with_construction(sid)

    def energy_oil_consumption(self, potential=False):
        """Oil consumption of the energy buildings (generators/stations) per turn.
        potential=False: only RUNNING (isWorking) energy buildings (actual
        consumption). potential=True: ALL built energy buildings (also waiting ones)
        - what they WOULD consume if they ran. needsOil is static per building."""
        total = 0
        for b in self.my_buildings():
            st = self._static_by_sid.get((self.unit_first(b), self.unit_type(b)))
            if not st:
                continue
            ne = (st.get("needsEnergy", 0) or 0)
            if ne < 0:   # energy producer (generator/station)
                if potential or b.get("isWorking"):
                    total += (st.get("needsOil", 0) or 0)
        return total

    def fuel_for_energy_ok(self):
        """Is the oil OUTPUT enough to cover the oil consumption of the existing energy
        buildings plus ONE more GENERATOR (2 oil)? We measure at the generator
        (smallest building block), NOT at the station (6 oil) - otherwise a deadlock
        arises: just too little oil for a station, but the waiting mines only
        deliver oil once they have energy. With the generator measure the bot can
        build up energy step by step from little oil. If it is not even enough for
        that, the expansion is redirected to an oil field."""
        oil_prod = self.oil_income()
        oil_use = self.energy_oil_consumption(potential=True)
        return oil_prod >= oil_use + self.GENERATOR_OIL_NEED

    def fuel_for_station_ok(self):
        """Can the oil for the efficient STATION (energy_big, 6 oil) be provided? The
        measure is the extractable oil CAPACITY of the mines (maxOilProd), NOT the
        current surplus: a mine's output can be switched to oil at any time. As soon
        as the mines together CAN extract 6 oil, the station is affordable - the
        small, inefficient generators become superfluous afterwards anyway
        (1 energy/2 oil vs. 6 energy/6 oil) and can be switched off. Formerly
        'current output >= consumption + 6' was required; that blocked the station
        permanently (deadlock: energy missing -> emergency -> but the station
        counted as 'too little oil', although the mine would only need to be
        switched)."""
        oil_cap = sum(self.mine_max_prod(m)["oil"] for m in self.my_mines())
        return oil_cap >= self.STATION_OIL_NEED

    def station_viable_by_freeing_generators(self):
        """OIL-TRAP resolution: is the oil enough for a station IF the surplus
        generators are switched off? Several inefficient generators (2 oil/1 energy
        each) eat the oil, so that no reserve remains for the efficient station
        (6 oil/6 energy). Switching generators off frees their oil. This method says
        whether the station would be affordable after freeing up - then the rebuild
        is worth it (first build the station, then the surplus generators off).
        Returns (rebuild_worth_it, number_to_free)."""
        gens = self.generator_count()
        if gens < 2 or self.station_count() > 0:
            return (False, 0)
        # oil balance: output minus consumption of the NON-energy is irrelevant
        # here; relevant is: generators consume GENERATOR_OIL_NEED each. If we switch
        # off g generators, g*2 oil is freed. The station needs 6 oil.
        # oil already free NOW:
        oil_free = self.oil_income() - self.energy_oil_consumption(potential=True)
        need = self.STATION_OIL_NEED - oil_free   # this much oil still needs to be freed
        if need <= 0:
            return (False, 0)   # station already directly affordable (no rebuild needed)
        # how many generators must give way for that? (rounded up)
        free_needed = (need + self.GENERATOR_OIL_NEED - 1) // self.GENERATOR_OIL_NEED
        # there must be enough generators, and after switching off at least 1
        # generator should remain as a transition buffer (until the station runs).
        if free_needed <= gens - 1:
            return (True, free_needed)
        return (False, 0)

    def gold_income(self):
        return self._sum_building_field("goldProd")

    def gold_refinery_sid(self):
        """sID secondPart of the gold refinery, identified via the static field
        convertsGold > 0 (verified: goldraff has convertsGold=5). Does NOT use the
        name string, since it can vary by data package - the converting building is
        uniquely determinable via convertsGold. None if no such building type exists
        in the data."""
        for (fp, sp), st in self._static_by_sid.items():
            if fp == 1 and (st.get("convertsGold", 0) or 0) > 0:
                return sp
        return None

    def count_gold_refineries_incl_construction(self):
        """Number of gold refineries = finished + under construction. Under-construction
        counts so that the bot does not rebuild repeatedly while one is already
        being built."""
        sid = self.gold_refinery_sid()
        if sid is None:
            return 0
        return self.count_with_construction(sid)

    GOLD_STORE_RES_TYPE = 3   # eResourceType: Metal=1, Oil=2, Gold=3 (verified)

    def gold_mine_exist(self):
        """True as soon as a GOLD MINE exists OR IS BEING BUILT - so either a standing
        own mine whose 2x2 area extracts gold (maxGoldProd > 0), OR a mine UNDER
        CONSTRUCTION (builder with isBuilding + buildingType.secondPart == MINE_SID)
        whose 2x2 build area contains at least 1 gold. 'Under construction counts as
        built' - the gold storage should enter the backlog already during the mine
        build, not only when the mine is finished."""
        # (a) standing gold mine
        for m in self.my_mines():
            if (self.mine_max_prod(m).get("gold", 0) or 0) > 0:
                return True
        # (b) mine under construction whose 2x2 build area contains gold
        lookup = self._resource_lookup()
        for v in self.my_vehicles():
            if not v.get("isBuilding"):
                continue
            bt = v.get("buildingType") or v.get("buildingTyp")
            if not (isinstance(bt, dict) and bt.get("secondPart") == self.MINE_SID):
                continue
            for c in self.footprint(self.pos(v), True):
                r = lookup.get(c)
                if r and r.get("type") == "gold" and (r.get("amount", 0) or 0) >= 1:
                    return True
        return False

    def has_gold_mine(self):
        """Alias for gold_mine_exist (backward compatibility)."""
        return self.gold_mine_exist()

    def gold_storage_capacity(self):
        """Total GOLD storage capacity of the base = sum of storageResMax of all own
        buildings with storeResType == 3 (storage-gold). Mines (storeResType=1) do
        NOT count as gold storage."""
        cap = 0
        for b in self.my_buildings():
            st = self._static_by_sid.get((self.unit_first(b), self.unit_type(b)), {})
            if st and st.get("storeResType") == self.GOLD_STORE_RES_TYPE:
                cap += st.get("storageResMax", 0) or 0
        return cap

    def gold_stored(self):
        """Gold currently stored in GOLD storages (storeResType==3)."""
        cur = 0
        for b in self.my_buildings():
            st = self._static_by_sid.get((self.unit_first(b), self.unit_type(b)), {})
            if st and st.get("storeResType") == self.GOLD_STORE_RES_TYPE:
                cur += b.get("storageResCur",
                              b.get("data", {}).get("storageResCur", 0)) or 0
        return cur

    def gold_storage_fill_ratio(self):
        """Fill ratio of the gold storages (0.0..1.0). 0.0 if no gold storage exists
        (capacity 0) - then 'over 50% full' is never satisfied, the first storage is
        triggered via gold_mine_exist, not via the fill ratio."""
        cap = self.gold_storage_capacity()
        if cap <= 0:
            return 0.0
        return self.gold_stored() / cap

    def metal_stored(self):
        """Ore currently stored in metal storages (storeResType==1). Sums storageResCur
        of all own buildings with storeResType==1 (mines + storage-metal share the
        SubBase - both count)."""
        cur = 0
        for b in self.my_buildings():
            st = self._static_by_sid.get((self.unit_first(b), self.unit_type(b)), {})
            if st and st.get("storeResType") == 1:
                cur += b.get("storageResCur",
                              b.get("data", {}).get("storageResCur", 0)) or 0
        return cur

    def metal_fill_ratio(self):
        """Fill ratio of the metal storages incl. storage under construction (0.0..1.0).
        Numerator = ore currently stored. Denominator =
        storage_capacity_incl_construction(1) - this way a storage under
        construction is immediately considered in the denominator and the fill ratio
        drops even before the storage is finished. 0.0 if capacity 0."""
        cap = self.storage_capacity_incl_construction(1)
        if cap <= 0:
            return 0.0
        return self.metal_stored() / cap

    # ----- expansion: explored resources, mines, target scoring --------------
    MINE_SID = 22   # building type 'mine' (secondPart), normal ore/oil/gold mine on land

    def explored_resources(self, res_type=None):
        """List of the deposits EXPLORED by the own surveyor (delivered by the bridge as
        map.exploredResources): [{x,y,type,amount}, ...]. res_type optionally
        'metal'/'oil'/'gold' filters to one type."""
        mp = self.model.get("map", {})
        res = mp.get("exploredResources", []) or []
        if res_type is not None:
            res = [r for r in res if r.get("type") == res_type]
        return res

    def my_mines(self):
        """Own mines (building type secondPart == MINE_SID)."""
        return [b for b in self.my_buildings()
                if self.unit_type(b) == self.MINE_SID]

    def mine_prod(self, mine):
        """Current output of a mine per resource: {'metal','oil','gold'}."""
        return {"metal": mine.get("metalProd", 0) or 0,
                "oil": mine.get("oilProd", 0) or 0,
                "gold": mine.get("goldProd", 0) or 0}

    def mine_max_prod(self, mine):
        """Maximum possible output per resource (from the deposits under the 2x2 area,
        each capped at canMineMaxRes): {'metal','oil','gold'}."""
        return {"metal": mine.get("maxMetalProd", 0) or 0,
                "oil": mine.get("maxOilProd", 0) or 0,
                "gold": mine.get("maxGoldProd", 0) or 0}

    def mine_capacity(self):
        """Total output budget of a mine (canMineMaxRes, sum over all resources). From
        the game data of the mine."""
        st = self._static_by_sid.get((1, self.MINE_SID))
        return (st.get("canMineMaxRes", 16) if st else 16) or 16

    def optimal_mine_allocation(self, mine, priority=("oil", "metal", "gold")):
        """Computes the best output distribution of a mine for a shared budget
        (canMineMaxRes). The 'priority' order determines which resource is served
        first up to its maximum; the rest of the budget goes to the next ones.
        Returns {'metal','oil','gold'}."""
        maxp = self.mine_max_prod(mine)
        budget = self.mine_capacity()
        alloc = {"metal": 0, "oil": 0, "gold": 0}
        for res in priority:
            take = min(maxp.get(res, 0), budget)
            alloc[res] = take
            budget -= take
            if budget <= 0:
                break
        return alloc

    def demand_mine_allocation(self, mine, oil_target):
        """DEMAND-ORIENTED output distribution - avoids the oscillation between oil and
        ore priority. Instead of a tipping yes/no threshold, a STABLE oil target
        value is given (oil_target = how much oil the base needs in total). This mine
        extracts as much oil as needed (up to its maximum), the rest of the budget
        goes into ore, then gold. Since the target value does not depend on the own
        result, no back-and-forth arises.
        Returns {'metal','oil','gold'}."""
        maxp = self.mine_max_prod(mine)
        budget = self.mine_capacity()
        alloc = {"metal": 0, "oil": 0, "gold": 0}
        # 1. oil up to the (capped) target value.
        oil = max(0, min(maxp.get("oil", 0), oil_target, budget))
        alloc["oil"] = oil
        budget -= oil
        # 2. rest into ore (build/production), then gold.
        m = min(maxp.get("metal", 0), budget)
        alloc["metal"] = m
        budget -= m
        alloc["gold"] = min(maxp.get("gold", 0), budget)
        return alloc

    def mine_count(self):
        return len(self.my_mines())

    def mine_covering(self, field):
        """Returns the own mine whose 2x2 footprint covers 'field' (regardless of
        connection), otherwise None. This lets the expansion recognise that a mine
        already stands at the goal (constructor done) and only the coupling chain
        still needs to be completed."""
        for m in self.my_mines():
            if field in self.footprint(self.pos(m), True):
                return m
        return None

    def building_sites_under_construction(self):
        """Footprint fields of buildings currently UNDER CONSTRUCTION (builder with
        isBuilding=True). Such sites are still vehicles in the state, not finished
        buildings - base_footprint therefore does not see them. For network
        connectivity, however, they should count as an island to connect ALREADY
        NOW, so that the coupling chain is built AS SOON AS the building is under
        construction (not only when it is finished). Only connectable buildings
        (connectsToBase) count - platform/bridge/road not."""
        cells = set()
        for v in self.my_vehicles():
            if not v.get("isBuilding"):
                continue
            bt = v.get("buildingType") or v.get("buildingTyp")
            sid = bt.get("secondPart") if isinstance(bt, dict) else None
            if sid is None:
                continue
            name = self._static_by_sid.get((1, sid), {}).get("name", "")
            if name in WATER_WALKABLE_BUILDINGS:
                continue   # platform/bridge/road: connectsToBase=false
            big = self.is_big_building_type(sid)
            cells |= self.footprint(self.pos(v), big)
        return cells

    # ----- unload protection: do not build in factories/depots --------------------
    def unload_category_of_building(self, building_sid):
        """Unload/production category of a BUILDING type from its static data:
          'ground' -> builds/unloads ground units (SmallGroundVehicle/Big/Human)
          'sea'    -> builds/unloads ships (Ship)
          'air'    -> builds/unloads aircraft (Plane/Alien) -> blocks nothing
          None     -> produces/unloads no units (irrelevant for the rule)
        Source: canBuild (producer) resp. storeUnitsTypes (storage/depot). Each of
        these buildings has exactly ONE category (verified)."""
        st = self._static_by_sid.get((1, building_sid))
        if not st:
            return None
        tokens = set()
        cb = (st.get("canBuild") or "").strip()
        if cb:
            tokens.add(cb)
        for t in (st.get("storeUnitsTypes") or []):
            if t:
                tokens.add(t)
        if not tokens:
            return None
        if tokens & UNLOAD_GROUND_TOKENS:
            return "ground"
        if tokens & UNLOAD_SEA_TOKENS:
            return "sea"
        if tokens & UNLOAD_AIR_TOKENS:
            return "air"
        return None

    def unload_buildings_with_footprint(self):
        """All own unit-producing/-unloading buildings AND sites (buildings under
        construction, still vehicles with isBuilding=True in the state) whose unload
        fields must be protected. Returns a list of (footprint:set, category:str) -
        only categories 'ground'/'sea' (air blocks nothing, therefore omitted).
        Sites count with their real 2x2 footprint, so that a factory under
        construction is also not built in."""
        out = []
        me_id = self.me.get("id") if self.me else None
        # 1. finished own buildings
        for b in self.my_buildings():
            cat = self.unload_category_of_building(self.unit_type(b))
            if cat in ("ground", "sea"):
                big = self.is_big_building_type(self.unit_type(b))
                out.append((self.footprint(self.pos(b), big), cat))
        # 2. own sites (vehicle with isBuilding=True -> buildingType)
        for v in self.my_vehicles():
            if not v.get("isBuilding"):
                continue
            bt = v.get("buildingType") or v.get("buildingTyp")
            sid = bt.get("secondPart") if isinstance(bt, dict) else None
            if sid is None:
                continue
            cat = self.unload_category_of_building(sid)
            if cat in ("ground", "sea"):
                big = self.is_big_building_type(sid)
                out.append((self.footprint(self.pos(v), big), cat))
        return out

    def _exit_ring(self, footprint):
        """The full diagonal neighbourhood (ring) around a footprint: all 8-neighbours
        of each footprint cell that do NOT themselves belong to the footprint. For
        2x2 these are exactly 12 fields."""
        ring = set()
        for (fx, fy) in footprint:
            for n in self.neighbors8(fx, fy):
                if n not in footprint:
                    ring.add(n)
        return ring

    def _exit_field_free(self, x, y, category, occ, water_ok):
        """Is (x,y) a valid, free unload field for the category?
          'ground' -> walkable ground field (no water except platform/bridge)
          'sea'    -> water/coast field
        AND not occupied (occ contains building and site footprints)."""
        if not self.in_bounds(x, y):
            return False
        if (x, y) in occ:
            return False
        if category == "ground":
            return self.is_free_for_ground(x, y, occ=occ, water_ok=water_ok)
        if category == "sea":
            return self.terrain_at(x, y) in (T_WATER, T_COAST)
        return False

    def building_keeps_enough_exits(self, footprint, category, occ, water_ok):
        """True if, after the current occupation (occ), the building/site with this
        footprint still has >= MIN_FREE_EXIT_FIELDS free, terrain-matching unload
        fields in the diagonal ring. The 2 fields do NOT have to be adjacent."""
        free = 0
        for (rx, ry) in self._exit_ring(footprint):
            if self._exit_field_free(rx, ry, category, occ, water_ok):
                free += 1
                if free >= MIN_FREE_EXIT_FIELDS:
                    return True
        return free >= MIN_FREE_EXIT_FIELDS

    def build_site_keeps_neighbor_exits(self, new_cells, occ=None, water_ok=None):
        """Check for the build-site choice: would a NEW building on new_cells (footprint
        fields of the candidate) push an ADJACENT, already existing unload
        building/site below MIN_FREE_EXIT_FIELDS free unload fields? Returns True if
        ALL affected neighbour buildings still have enough unload fields afterwards
        (build site allowed), otherwise False.

        'Adjacent' = the diagonal ring of the unload building intersects new_cells.
        Only then can the new build shrink its unload fields."""
        new_cells = set(new_cells)
        if occ is None:
            occ = self.occupied_fields()
        if water_ok is None:
            water_ok = self.water_walkable_fields()
        # the new cells now count as occupied (block unload fields).
        occ_after = set(occ) | new_cells
        for footprint, category in self.unload_buildings_with_footprint():
            # only consider buildings whose unload ring touches the new cells -
            # only they can be affected at all. (Own footprint never overlaps, since
            # nothing can be built there.)
            ring = self._exit_ring(footprint)
            if not (ring & new_cells):
                continue
            if not self.building_keeps_enough_exits(footprint, category,
                                                    occ_after, water_ok):
                return False
        return True

    def network_components(self):
        """Decomposes all own base fields (base_footprint) into connected ISLANDS via
        the 4-neighbourhood (= supply connection). Returns a list of sets
        [{(x,y),...}, ...]. More than one island = the supply network is SEPARATED
        (gap in the coupling, destroyed connectors, etc.). Sorted: largest island
        first (= main base). Sites under construction count IN (as their own island,
        as long as they are not yet connected) - this way the connection is planned
        already during the build, not only after completion."""
        cells = set(self.base_footprint()) | self.building_sites_under_construction()
        seen = set()
        comps = []
        for start in cells:
            if start in seen:
                continue
            stack = [start]
            comp = set()
            while stack:
                p = stack.pop()
                if p in seen:
                    continue
                seen.add(p)
                comp.add(p)
                x, y = p
                for n in ((x, y-1), (x+1, y), (x, y+1), (x-1, y)):
                    if n in cells and n not in seen:
                        stack.append(n)
            comps.append(comp)
        comps.sort(key=len, reverse=True)
        return comps

    def network_gap_target(self):
        """If the network is separated: returns the field pair with the SHORTEST
        distance between the main island (largest) and any other island as
        (from_cell, to_cell, dist). This is the starting point for the repair:
        (re)build the coupling from from_cell towards to_cell. Returns None if the
        network is connected (only one island)."""
        comps = self.network_components()
        if len(comps) < 2:
            return None
        main = comps[0]
        best = None
        for other in comps[1:]:
            for (ax, ay) in main:
                for (bx, by) in other:
                    d = (ax - bx) ** 2 + (ay - by) ** 2
                    if best is None or d < best[2]:
                        best = ((ax, ay), (bx, by), d)
        return best

    def mine_is_networked(self, mine):
        """Is the mine TOPOLOGICALLY connected to the main base? Checks the network
        connectivity components (4-neighbourhood), NOT isWorking. Important:
        isWorking can be False even though the mine is correctly connected (e.g.
        missing ENERGY - that is a separate problem, not a network problem). A mine
        whose footprint lies in the main island counts as connected - the coupling
        chain is then finished and needs NO further connectors."""
        comps = self.network_components()
        if not comps:
            return False
        main = comps[0]   # largest island = main base
        return bool(self.footprint(self.pos(mine), True) & main)

    def find_build_position_for(self, building_sid, avoid=None, require_connected=True):
        """Build site for a building type WITHOUT a concrete builder (for planning).
        Searches a free field buildable for the BUILDING near the base: first
        connected (require_connected), if None detached (nearest free). 1x1 and 2x2
        are distinguished. Returns (x,y) (top-left corner) or None."""
        if avoid is None:
            avoid = set()
        occ = self.occupied_fields()
        water_ok = self.water_walkable_fields()
        base_cells = self.base_footprint()
        if not base_cells:
            return None
        bx = sum(c[0] for c in base_cells) / len(base_cells)
        by = sum(c[1] for c in base_cells) / len(base_cells)
        is_big = self.is_big_building_type(building_sid)

        def cells_for(ox, oy):
            if is_big:
                return [(ox, oy), (ox+1, oy), (ox, oy+1), (ox+1, oy+1)]
            return [(ox, oy)]

        for require in ((True, False) if require_connected else (False,)):
            best = None
            # Suchring um die Basis
            search = set()
            for (cx, cy) in base_cells:
                for dx in range(-3, 4):
                    for dy in range(-3, 4):
                        search.add((cx+dx, cy+dy))
            for (ox, oy) in search:
                if (ox, oy) in avoid:
                    continue
                cells = cells_for(ox, oy)
                if any(c in base_cells for c in cells):
                    continue
                if not all(self.is_buildable_for_building(building_sid, cx, cy,
                           occ=occ, water_ok=water_ok) for cx, cy in cells):
                    continue
                if require and not any(self.is_connected_field(cx, cy, base_cells)
                                       for cx, cy in cells):
                    continue
                if not self.build_site_keeps_neighbor_exits(set(cells), occ=occ,
                                                            water_ok=water_ok):
                    continue
                if any(self.building_blocks_resource(building_sid, cx, cy)
                       for cx, cy in cells):
                    continue   # do not build over a deposit with a non-mine building
                d = (ox - bx)**2 + (oy - by)**2
                if best is None or d < best[0]:
                    best = (d, (ox, oy))
            if best is not None:
                return best[1]
        return None

    def route_fields(self, from_cell, to_cell):
        """Shortest ORTHOGONAL field path from from_cell (at the main island) to to_cell
        (target island) - the fields in between that need a coupling. A* search on
        the grid with 4-neighbourhood (only orthogonal, because the supply network
        only connects orthogonally - diagonal steps would leave gaps). The result is
        a contiguous chain from the base to the goal WITHOUT superfluous fields.
        Occupied/impassable fields are avoided. Order: from the base to the goal.
        Empty list if no path."""
        import heapq
        (x0, y0), (x1, y1) = from_cell, to_cell
        # ROUTE mask: only FOREIGN/neutral objects (incl. rubble) block the route.
        # Own buildings are passable (become part of the network), own vehicles
        # likewise (move aside on arrival). See handover doc 6.1 mask B.
        blockers = self.foreign_blocking_fields()

        def passable(x, y):
            """Field usable for the COUPLING route? A coupling has factorSea=1 (buildable
            directly on open water, WITHOUT a platform), and pioneer/constructor are
            amphibious (factorSea=3 - they drive over water). Therefore water/coast
            is NOT an obstacle for the repair route. Only these block: foreign/
            neutral objects (avoid) and real static blocked terrain (isBlocked /
            T_BLOCKED, e.g. cliffs - impassable regardless of type). Connectors/
            platforms coexist with everything."""
            if not self.in_bounds(x, y):
                return False
            if (x, y) == (x1, y1):
                return True   # target field (island building) always made reachable
            if (x, y) in blockers:
                return False  # foreign/neutral/rubble -> avoid
            t = self.terrain_at(x, y)
            # ONLY real blocked terrain blocks. Water/coast are allowed: the
            # amphibious pioneer reaches them, the coupling is buildable there.
            # (Formerly unplatformed water was wrongly blocked -> water gaps returned
            # empty routes, the connection of water mines failed.)
            if t == T_BLOCKED:
                return False
            return True

        def h(x, y):
            return abs(x - x1) + abs(y - y1)   # Manhattan (orthogonal)

        open_heap = [(h(x0, y0), 0, (x0, y0))]
        came = {(x0, y0): None}
        gscore = {(x0, y0): 0}
        found = False
        guard = 0
        while open_heap and guard < 20000:
            guard += 1
            _f, g, cur = heapq.heappop(open_heap)
            if cur == (x1, y1):
                found = True
                break
            cx, cy = cur
            for nx, ny in ((cx, cy-1), (cx+1, cy), (cx, cy+1), (cx-1, cy)):
                if not passable(nx, ny):
                    continue
                ng = g + 1
                if (nx, ny) not in gscore or ng < gscore[(nx, ny)]:
                    gscore[(nx, ny)] = ng
                    came[(nx, ny)] = cur
                    heapq.heappush(open_heap, (ng + h(nx, ny), ng, (nx, ny)))
        if not found:
            return []
        # reconstruct the path (goal -> start), then reverse.
        path = []
        node = (x1, y1)
        while node is not None:
            path.append(node)
            node = came[node]
        path.reverse()
        # only the fields IN BETWEEN that still need a coupling (not the goal
        # itself, not already in the supplied network). Own buildings/vehicles stay
        # in the path - the finer build-field filtering is done by the caller (mask C).
        main = self.main_component()
        fields = []
        for (x, y) in path:
            if (x, y) == (x1, y1) or (x, y) in main:
                continue
            fields.append((x, y))
        return fields

    def income_for(self, res_type):
        """Output of the last turn for 'metal'/'oil'/'gold'."""
        if res_type == "metal":
            return self.metal_income()
        if res_type == "oil":
            return self.oil_income()
        if res_type == "gold":
            return self.gold_income()
        return 0

    def base_reference_field(self):
        """Reference point of the base for distance measurement: centroid of the own
        buildings (averaged position). Falls back to the first vehicle."""
        bs = self.my_buildings()
        if bs:
            xs = [self.pos(b)[0] for b in bs]
            ys = [self.pos(b)[1] for b in bs]
            return (sum(xs) / len(xs), sum(ys) / len(ys))
        vs = self.my_vehicles()
        if vs:
            return self.pos(vs[0])
        return (0, 0)

    # Target ratio of the standard bot: metal:oil:gold = 12:4:6 = 6:2:3.
    DEMAND_TARGET = {"metal": 6, "oil": 2, "gold": 3}
    # FALLBACK default for the saturation exponent of the demand denominator. The
    # actual knob is at the top of bot_run.py (_DEMAND_SATURATION) and is set into
    # the GameState per turn at startup (gs.DEMAND_SATURATION); this class value only
    # applies if the lib is used without bot_run. 1.0 = demand falls strongly with
    # own output; metal<1 = metal stays in demand longer.
    DEMAND_SATURATION = {"metal": 0.3, "oil": 1.0, "gold": 1.0}
    EXPANSION_K = 3

    def _produced_per_type(self):
        """The 'extracted' resource amount per type already, as a basis for the demand.
        Counted is the FULL theoretical 2x2 yield (total amount of resources on the
        4 fields of the mine area, NOT budget-capped) - both for STANDING mines and
        for mines UNDER CONSTRUCTION (builder with isBuilding and
        buildingType.secondPart == MINE_SID). 'Under construction counts as built',
        analogous to the factory in the emergency acc. Returns
        {'metal':..,'oil':..,'gold':..}."""
        lookup = self._resource_lookup()
        prod = {"metal": 0, "oil": 0, "gold": 0}

        def add_area(corner_cells):
            for c in corner_cells:
                r = lookup.get(c)
                if r and r.get("type") in prod:
                    prod[r["type"]] += r.get("amount", 0) or 0

        # standing mines: their 2x2 footprint.
        for m in self.my_mines():
            add_area(self.footprint(self.pos(m), True))
        # mines under construction: builder with isBuilding + mine type. Build position = footprint.
        for v in self.my_vehicles():
            if not v.get("isBuilding"):
                continue
            bt = v.get("buildingType") or v.get("buildingTyp")
            if isinstance(bt, dict) and bt.get("secondPart") == self.MINE_SID:
                add_area(self.footprint(self.pos(v), True))
        return prod

    def _demand_factor(self, produced=None):
        """demand(type) = target(type) / (1 + already_extracted[type]) ** SAT[type]. The
        more of a type is already extracted, the smaller its demand - but the
        saturation exponent SAT[type] dampens HOW fast the demand falls. SAT=1.0 as
        before; SAT<1 keeps the type in demand longer (metal: bottleneck).
        DEMAND_SATURATION is set at startup from bot_run; falls back to 1.0 if no
        value is present for a type."""
        if produced is None:
            produced = self._produced_per_type()
        sat = getattr(self, "DEMAND_SATURATION", {})
        return {t: self.DEMAND_TARGET[t]
                   / (1.0 + produced.get(t, 0)) ** sat.get(t, 1.0)
                for t in self.DEMAND_TARGET}

    def expansion_target(self, blocked_fields=None, force_type=None, min_metal=0):
        """Best expansion target - AREA-based and DEMAND-DRIVEN (spec 7). Iterates over
        possible 2x2 mine areas (not single fields). Each area is scored once:

            score = SUM_type( amount(type) * demand(type) ) / (distance_centre + k)

        amount(type) = sum of this type over the 4 fields (full 2x2 yield).
        demand(type) = target(6/2/3) / (1 + already_extracted) incl. mines under
        construction. The demand controls ONLY the order; it does NOT inhibit the
        expansion - the best reachable area is always chosen, regardless of type.

        Return contract unchanged: (x, y, type, amount, score), where (x,y) is the
        STRONGEST resource field of the chosen area (anchor for the subsequent
        build-site/platform logic) and 'type'/'amount' its type/amount.
        blocked_fields: (x,y) that should not be chosen.
        force_type: consider only areas that contain this resource (emergency oil
        redirection)."""
        cands = self._expansion_candidates_scored(
            blocked_fields=blocked_fields, force_type=force_type, min_metal=min_metal)
        if not cands:
            return None
        ax, ay, atyp, aamt, score = cands[0]
        return (ax, ay, atyp, aamt, score)

    def expansion_candidates(self, blocked_fields=None, force_type=None, min_metal=0):
        """ALL developable expansion areas, highest score first. Same scoring as
        expansion_target (which only returns the best) - ONE truth, no duplicate.
        Return: list of (x, y, type, amount, score). For the build-planner backlog
        (Dungeon-Keeper) that marks ALL projects, instead of re-guessing only the
        single best one every turn."""
        return self._expansion_candidates_scored(
            blocked_fields=blocked_fields, force_type=force_type, min_metal=min_metal)

    def _expansion_candidates_scored(self, blocked_fields=None, force_type=None,
                                     min_metal=0):
        """Shared scoring loop for expansion_target/expansion_candidates. Scores each
        possible 2x2 mine area exactly once and returns the buildable ones, sorted
        descending by score, as a list (x, y, type, amount, score). (x,y) = strongest
        resource field of the area (anchor). Empty list if no developable area
        exists."""
        blocked_fields = blocked_fields or set()
        bx, by = self.base_reference_field()
        lookup = self._resource_lookup()
        if not lookup:
            return []

        # block fields of CONNECTED mines (TOPOLOGICALLY connected, not isWorking -
        # a connected mine can be isWorking=False for lack of energy).
        blocked_cells = set()
        for m in self.my_mines():
            if self.mine_is_networked(m):
                blocked_cells |= self.footprint(self.pos(m), True)

        demand = self._demand_factor()

        # candidate 2x2 areas from each resource field as a possible corner,
        # deduplicated via the top-left corner.
        corners = set()
        for (fx, fy) in lookup.keys():
            for oc in [(fx, fy), (fx - 1, fy), (fx, fy - 1), (fx - 1, fy - 1)]:
                corners.add(oc)

        results = []  # (score, anchor_x, anchor_y, anchor_type, anchor_amount)
        for (ox, oy) in corners:
            cells = [(ox, oy), (ox + 1, oy), (ox, oy + 1), (ox + 1, oy + 1)]
            if any(c in blocked_cells for c in cells):
                continue
            # determine amounts per type + strongest field (anchor).
            sbt = {"metal": 0, "oil": 0, "gold": 0}
            anchor = None        # strongest field of the area (a, x, y, type)
            anchor_forced = None  # strongest field of the forced type
            for c in cells:
                r = lookup.get(c)
                if not r:
                    continue
                t = r.get("type")
                a = r.get("amount", 0) or 0
                if t in sbt:
                    sbt[t] += a
                if anchor is None or a > anchor[0]:
                    anchor = (a, c[0], c[1], t)
                if force_type is not None and t == force_type:
                    if anchor_forced is None or a > anchor_forced[0]:
                        anchor_forced = (a, c[0], c[1], t)
            if anchor is None:
                continue  # empty area
            # TOO INSIGNIFICANT: areas whose TOTAL yield (all types of the 2x2
            # combined) is <= 2 (e.g. only 1-2 oil and nothing else) do not justify
            # the material and build effort of a mine. Do NOT offer such fields as a
            # site. (A forced mine via force_type/min_metal already has stricter
            # thresholds anyway and is checked below.)
            if force_type is None and sum(sbt.values()) <= 2:
                continue
            if force_type is not None and sbt.get(force_type, 0) <= 0:
                continue  # emergency: only areas with the forced resource
            if min_metal > 0 and sbt.get("metal", 0) < min_metal:
                continue  # minimum ore output of the 2x2 area not reached
                          # (e.g. forced strong ore mine after the starting mine)
            # in an emergency the anchor is the strongest field of the FORCED type,
            # otherwise the strongest field of the area.
            use = anchor_forced if force_type is not None else anchor
            ax, ay, atyp, aamt = use[1], use[2], use[3], use[0]
            if (ax, ay) in blocked_fields:
                continue
            # the area must be buildable (directly or via water platforms),
            # otherwise the expansion sticks to an unreachable goal.
            if (self.mine_build_position((ax, ay), target_type=atyp) is None
                    and self.mine_build_position_with_platforms((ax, ay), target_type=atyp) is None):
                continue
            weighted = sum(sbt[t] * demand[t] for t in sbt)
            cx, cy = ox + 0.5, oy + 0.5
            dist = ((cx - bx) ** 2 + (cy - by) ** 2) ** 0.5
            score = weighted / (dist + self.EXPANSION_K)
            results.append((score, ax, ay, atyp, aamt))
        # highest score first; stable tiebreak via anchor coordinates, so that the
        # order is deterministic across turns.
        results.sort(key=lambda r: (r[0], -r[1], -r[2]), reverse=True)
        return [(ax, ay, atyp, aamt, score)
                for (score, ax, ay, atyp, aamt) in results]

    def energy_balance(self):
        """Energy balance = production - consumption.
        needsEnergy: negative = production (generator), positive = consumption.
        Production always counts (the generator produces), consumption only for
        isWorking buildings. -> balance = -sum(needsEnergy weighted)."""
        prod = 0
        need = 0
        for b in self.my_buildings():
            st = self._static_by_sid.get((self.unit_first(b), self.unit_type(b)))
            ne = (st.get("needsEnergy", 0) if st else 0) or 0
            if ne < 0:           # generator: produces |ne|
                prod += -ne
            elif ne > 0:         # consumer: only if operating
                if b.get("isWorking"):
                    need += ne
        return prod - need

    def energy_production(self):
        """Pure energy production (sum of generator output)."""
        prod = 0
        for b in self.my_buildings():
            st = self._static_by_sid.get((self.unit_first(b), self.unit_type(b)))
            ne = (st.get("needsEnergy", 0) if st else 0) or 0
            if ne < 0:
                prod += -ne
        return prod

    MINE_ENERGY_NEED = 1   # a mine needs 1 energy (verified: needsEnergy 1)

    def energy_potential_need(self):
        """POTENTIAL energy demand of ALL consumers - independent of isWorking.
        Important: a mine that does NOT run for lack of energy (isWorking=False)
        reports no demand in the normal balance, so the emergency would never see
        the demand (vicious circle: no energy -> mine off -> no reported demand ->
        no generator build). Here therefore ALL consumers count (needsEnergy > 0),
        regardless of whether they are currently running."""
        need = 0
        for b in self.my_buildings():
            st = self._static_by_sid.get((self.unit_first(b), self.unit_type(b)))
            ne = (st.get("needsEnergy", 0) if st else 0) or 0
            if ne > 0:
                need += ne
        return need

    def energy_overcapacity_ok(self):
        """Energy OVERCAPACITY: does the production (incl. under construction) cover the
        potential total demand of ALL consumers PLUS the demand of the next planned
        mine? Not measured by generator count or a fixed threshold, but dynamically
        by the balance - this way the energy requirement grows with the base and the
        bot builds energy PROACTIVELY before a new mine joins the network (otherwise
        it would stay a 'waiting' mine)."""
        prod = self.energy_production_incl_construction()
        need = self.energy_potential_need()
        return prod >= need + self.MINE_ENERGY_NEED

    def storage_capacity(self, res_type):
        """Sum of storageResMax of the own buildings with matching storeResType.
        res_type: 1=metal, 2=oil, (gold separately). Mine counts in (stores ore)."""
        total = 0
        for b in self.my_buildings():
            st = self._static_by_sid.get((self.unit_first(b), self.unit_type(b)))
            if not st:
                continue
            if st.get("storeResType") == res_type:
                total += st.get("storageResMax", 0) or 0
        return total

    def base_energy_ok(self):
        """Is the base's energy balance sufficient (>= 0)?"""
        return self.energy_balance() >= 0

    def building_under_construction(self, building_sid_secondpart):
        """Is an own BUILD VEHICLE currently building this building type? Covers BOTH:
        engineer (storage, radar, generator, connector - small buildings) AND
        constructor (factories, mine, large power station). Both are vehicles and
        carry isBuilding=True + buildingTyp (sID) while building. IMPORTANT against
        flutter: a building under construction counts as 'coming', so the bot does
        not start a new one every turn (building takes >= 5 turns)."""
        for v in self.my_vehicles():
            if not v.get("isBuilding"):
                continue
            bt = v.get("buildingTyp", {})
            if isinstance(bt, dict) and bt.get("secondPart") == building_sid_secondpart:
                return True
        return False

    def count_with_construction(self, building_sid_secondpart, require_energy=False):
        """Number of 'effectively present' buildings of a type = finished + under
        construction. Universal for storage/generator/radar/factories/mine. This way
        the bot does not rebuild as long as enough are finished OR under
        construction. require_energy: only relevant for buildings that need energy to
        count (not enforced here - the caller decides)."""
        n = sum(1 for b in self.my_buildings()
                if self.unit_type(b) == building_sid_secondpart)
        for v in self.my_vehicles():
            if v.get("isBuilding"):
                bt = v.get("buildingTyp", {})
                if isinstance(bt, dict) and bt.get("secondPart") == building_sid_secondpart:
                    n += 1
        return n

    def factory_available(self, factory_type_name):
        """Is a factory of the type AVAILABLE OR UNDER CONSTRUCTION?
        - exists as a finished building AND base energy is sufficient, OR
        - is currently being built by a constructor (counts as 'coming').
        (isWorking is unsuitable for factories - depends on the build order.)
        factory_type_name: 'smallfactory' / 'bigfactory'."""
        sid = None
        for (fp, sp), st in self._static_by_sid.items():
            if fp == 1 and st.get("name") == factory_type_name:
                sid = sp
                break
        if sid is None:
            return False
        # under construction? -> counts as available (prevents multiple builds)
        if self.building_under_construction(sid):
            return True
        exists = any(self.unit_type(b) == sid for b in self.my_buildings())
        return exists and self.base_energy_ok()

    def has_rubble(self):
        """Is rubble on the field? (rubbleValue > 0 for buildings/fields)
        Note: rubble often stands as its own object; here a rough approximation via
        own/neutral buildings with rubbleValue. Refine later if needed."""
        for p in self.model.get("players", []):
            for b in p.get("buildings", []):
                if (b.get("rubbleValue", 0) or 0) > 0:
                    return True
        for b in self.model.get("neutralBuildings", []):
            if (b.get("rubbleValue", 0) or 0) > 0:
                return True
        return False

    def assess(self):
        """Bundles the situation assessment (stage 1) into a dict."""
        return {
            "metal_income": self.metal_income(),
            "oil_income": self.oil_income(),
            "gold_income": self.gold_income(),
            "energy_production": self.energy_production(),
            "energy_balance": self.energy_balance(),
            "storage_metal": self.storage_capacity(1),
            "storage_oil": self.storage_capacity(2),
            "light_factory": self.factory_available("smallfactory"),
            "heavy_factory": self.factory_available("bigfactory"),
            "has_rubble": self.has_rubble(),
        }

    def _static_value_for_construction(self, field, predicate=None):
        """Sum of a static field (e.g. storageResMax, needsEnergy) over all buildings
        currently being built by own build vehicles. predicate(st) can filter the
        building type (e.g. by storeResType)."""
        total = 0
        for v in self.my_vehicles():
            if not v.get("isBuilding"):
                continue
            bt = v.get("buildingTyp", {})
            if not isinstance(bt, dict):
                continue
            st = self._static_by_sid.get((bt.get("firstPart"), bt.get("secondPart")))
            if not st:
                continue
            if predicate is not None and not predicate(st):
                continue
            total += st.get(field, 0) or 0
        return total

    def storage_capacity_incl_construction(self, res_type):
        """Storage capacity incl. storage buildings under construction."""
        base = self.storage_capacity(res_type)
        building = self._static_value_for_construction(
            "storageResMax", predicate=lambda st: st.get("storeResType") == res_type)
        return base + building

    def energy_production_incl_construction(self):
        """Energy production incl. generators under construction.
        needsEnergy < 0 = production; we sum |negative needsEnergy|."""
        base = self.energy_production()
        building = -self._static_value_for_construction(
            "needsEnergy", predicate=lambda st: (st.get("needsEnergy", 0) or 0) < 0)
        return base + building

    def energy_load_ratio(self):
        """Load of the energy supply (0.0..1.0+).
        = energy_potential_need() / energy_production_incl_construction()
        Numerator: POTENTIAL demand of all consumers (independent of isWorking), so
        that waiting mines and factories are also counted.
        Denominator: production incl. energy buildings under construction (station or
        generator) - a started station (needsEnergy=-6) lowers the load immediately,
        thus preventing multiple triggers while it is under construction.
        0.0 if no production exists (division by zero avoided)."""
        prod = self.energy_production_incl_construction()
        if prod <= 0:
            return 0.0
        return self.energy_potential_need() / prod

    def is_emergency(self):
        """Stage 2: emergency mode? Thresholds from ALGORITHMUS.md.

        IMPORTANT: Here buildings UNDER CONSTRUCTION count IN - otherwise the bot
        would never leave the emergency (it would see "no factory/too little
        storage" although one is being built). The situation assessment (assess), by
        contrast, stays the actual state.

        The ORE OUTPUT is NO LONGER an emergency reason: the first new mine is
        secured by the backlog prioritisation (mandatory, place 1), not by the
        emergency. The emergency only cares about buildings (energy, storage,
        factories) and ends as soon as these stand - regardless of whether the first
        new mine is already producing. (Formerly 'ore income < 10' kept the emergency
        active until the mine stood - that is now the backlog's job.)
        Returns (bool, [reasons])."""
        reasons = []

        stor_m = self.storage_capacity_incl_construction(1)
        stor_o = self.storage_capacity_incl_construction(2)

        # ENERGY: do not measure by a fixed threshold or generator count, but by
        # OVERCAPACITY. There must ALWAYS be an energy surplus - the production
        # (incl. construction) must cover the potential demand of ALL consumers
        # (also waiting, non-running mines) plus the next planned mine. This way the
        # requirement grows with the base and the bot builds energy proactively
        # before a new mine stands idle for lack of power.
        if not self.energy_overcapacity_ok():
            prod = self.energy_production_incl_construction()
            need = self.energy_potential_need()
            reasons.append(f"Energie-Ueberkapazitaet fehlt (Prod+Bau {prod} < "
                           f"Bedarf {need} + naechste Mine {self.MINE_ENERGY_NEED})")
        if stor_m < 40:
            reasons.append(f"Erz-Speicher(+Bau) {stor_m} < 40")
        if stor_o < 40:
            reasons.append(f"Treibstoff-Speicher(+Bau) {stor_o} < 40")
        if not self.factory_available("smallfactory"):   # already counts construction
            reasons.append("leichte Fabrik nicht verfuegbar/im Bau")
        if not self.factory_available("bigfactory"):
            reasons.append("schwere Fabrik nicht verfuegbar/im Bau")
        return (len(reasons) > 0, reasons)


class Conn:
    """Socket connection + single-action dialogue with the bridge.

    Holds the once-sent terrain map and forwards it to every GameState.
    """

    def __init__(self, host="127.0.0.1", port=5001, player_name="ClaudeBot"):
        self.player_name = player_name
        self.sock = socket.create_connection((host, port))
        self._f = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self._terrain = None
        # action statistics (analogous to the bridge): successful vs. rejected
        # actions. Evaluated in phase 7 (reward/statistics) and the basis for the
        # later RL reward signal. reset_stats() per turn.
        self.stat_ok = 0
        self.stat_rejected = 0
        # SIMULATED REPEAT (FactoriesWhoRepeat): {factory_id: [sID, ...]}
        # The bot sets a factory here to "continuous production", WITHOUT the
        # MAXR/bridge repeat command (which causes OOS). Instead tick_soft_repeat()
        # triggers a new single order (repeat=False) per turn as soon as the factory
        # has emptied its buildList. The value is the order sequence to build as a
        # list of [firstPart, secondPart] pairs (a queue is allowed; for a single
        # type simply one element).
        self._soft_repeat = {}
        # ACTION LOGGING: optional hook that bot_run sets to its log() function at
        # startup (action_logger = log). This way EVERY action sent to the bridge is
        # logged with unit, from/to and acceptance/rejection+reason - into the same
        # log file + console as the rest. If no hook is set, do() falls back to print
        # (always visible).
        self.action_logger = None

    def reset_stats(self):
        self.stat_ok = 0
        self.stat_rejected = 0

    def _recv(self):
        line = self._f.readline()
        return None if not line else line.rstrip("\n").rstrip("\r")

    def _send(self, obj):
        self.sock.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))

    def turns(self):
        """Generator: yields one GameState per turn (with terrain map)."""
        while True:
            line = self._recv()
            if line is None:
                return
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            # terrain map comes once -> remember it.
            if "terrainMap" in raw:
                self._terrain = raw["terrainMap"]
            yield GameState(raw, self.player_name, terrain=self._terrain)

    def query(self, query_obj):
        """Send a query (does not change the state), reply as a dict.
        e.g. {"query":"getState"} or {"query":"buildSpeeds",...}."""
        self._send(query_obj)
        rep = self._recv()
        if rep is None:
            return None
        try:
            return json.loads(rep)
        except json.JSONDecodeError:
            return None

    def reach_turns(self, unit_id, target):
        """How many TURNS does the unit need to reach 'target' (x,y)? Uses the bridge's
        pathCost query (real terrain movement cost) and computes 'turns' =
        ceil(cost / speedMax) - the same estimate the bridge uses. Returns int (>=0)
        or None if unreachable. 0 = already there. For the multi-pioneer repair
        pre-check (approach of a helper vs. the build time pioneer 1 would already
        have contributed by then)."""
        import math
        rep = self.query({"query": "pathCost", "unitId": unit_id,
                          "target": [int(target[0]), int(target[1])]})
        if not rep or rep.get("error"):
            return None
        if not rep.get("reachable", False):
            return None
        cost = rep.get("cost", 0) or 0
        speed_max = rep.get("speedMax", 0) or 0
        if cost <= 0:
            return 0
        if speed_max <= 0:
            return None
        return int(math.ceil(cost / speed_max))

    def refresh_state(self):
        """Fetch a fresh, filtered state from the bridge and return it as a GameState -
        like a player looking at the screen after an action. None on error. The
        terrain map comes updated EVERY turn (platformed water fields become land)
        and is adopted; the cached value only serves as a fallback in case none is
        sent along."""
        r = self.query({"query": "getState"})
        if not r or r.get("result") != "state":
            return None
        raw = r["state"]
        if "terrainMap" in raw:
            self._terrain = raw["terrainMap"]
        return GameState(raw, self.player_name, terrain=self._terrain)

    def _describe_action(self, action):
        """Turns an action into a human-readable sentence for the log, e.g.
        'Bewege Einheit 15 -> (52, 42)' or 'Baue platform mit Einheit 15 @
        (52, 42)'. Falls back to a generic representation."""
        t = action.get("type", "?")
        uid = action.get("unitId")
        pos = action.get("position") or action.get("target")
        poss = f"({pos[0]}, {pos[1]})" if isinstance(pos, (list, tuple)) and len(pos) >= 2 else ""
        if t == "move":
            return f"Bewege Einheit {uid} -> {poss}"
        if t == "startBuild":
            bid = action.get("buildingId")
            sid = bid[1] if isinstance(bid, (list, tuple)) and len(bid) > 1 else bid
            return f"Baue (sid {sid}) mit Einheit {uid} @ {poss}"
        if t == "finishBuild":
            esc = action.get("escapePosition")
            escs = f"({esc[0]}, {esc[1]})" if isinstance(esc, (list, tuple)) and len(esc) >= 2 else ""
            return f"FinishBuild Einheit {uid} -> Ausweichfeld {escs}"
        if t == "transfer":
            return (f"Transfer {action.get('amount')} {action.get('resource')} "
                    f"von {action.get('unitId')} -> {action.get('targetId')}")
        if t == "clear":
            return f"Raeume Schrott mit Einheit {uid}"
        if t == "attack":
            return f"Angriff Einheit {uid} -> {poss}"
        if t in ("startWork", "stopWork", "sentry", "setAutoMove"):
            return f"{t} Einheit {uid}"
        return f"{t} {action}"

    def _log_action(self, msg):
        """Action log via the hook set by bot_run (file+console), otherwise via print
        (always visible)."""
        if self.action_logger is not None:
            try:
                self.action_logger(msg)
                return
            except Exception:
                pass
        print(f"[bot] {msg}")

    def do(self, action):
        """Send an action, read the result. -> (ok: bool, reason: str|None).
        Logs EVERY action with unit, from/to and acceptance/rejection."""
        self._send({"action": action})
        rep = self._recv()
        desc = self._describe_action(action)
        if rep is None:
            self._log_action(f"  AKTION: {desc} ... ABGELEHNT (Verbindung verloren)")
            return (False, "connection lost")
        try:
            r = json.loads(rep)
        except json.JSONDecodeError:
            self._log_action(f"  AKTION: {desc} ... ABGELEHNT (ungueltige Antwort)")
            return (False, "bad reply")
        if r.get("result") == "ok":
            self.stat_ok += 1
            self._log_action(f"  AKTION: {desc} ... ANGENOMMEN")
            return (True, None)
        self.stat_rejected += 1
        reason = r.get("reason", "rejected")
        self._log_action(f"  AKTION: {desc} ... ABGELEHNT (Grund: {reason})")
        return (False, reason)

    def end_turn(self):
        """Send end of turn, read the report. -> dict (RL report) or None."""
        self._send({"endTurn": True})
        rep = self._recv()
        if rep is None:
            return None
        try:
            return json.loads(rep)
        except json.JSONDecodeError:
            return None

    def set_soft_repeat(self, factory_id, build_list):
        """Set a factory to SIMULATED continuous repeat (no MAXR repeat flag!).
        factory_id: ID of the factory building.
        build_list: list of [firstPart, secondPart] - the order sequence that is
                    re-ordered on each resupply. For a single type simply [[0, sid]].
                    Several entries = queue that is set anew completely on each idle.
        NEVER aborts a running build - only sets the desired state.
        Calling again updates the sequence."""
        self._soft_repeat[factory_id] = [list(item) for item in build_list]

    def clear_soft_repeat(self, factory_id):
        """Remove a factory from the simulated repeat. A currently running build is NOT
        aborted - only no further follow-up production is triggered. None-safe: an
        unknown ID is ignored."""
        self._soft_repeat.pop(factory_id, None)

    def is_soft_repeat(self, factory_id):
        """True if the factory is currently on simulated repeat."""
        return factory_id in self._soft_repeat

    def tick_soft_repeat(self, gs):
        """Call ONCE per turn. Goes through all factories in the simulated repeat and
        triggers follow-up production, WITHOUT ever aborting a build:

          - factory no longer exists (destroyed) -> remove from the repeat.
          - factory still has a non-empty buildList (building or has a finished,
            not-yet-unloaded unit) -> do NOTHING, wait.
          - factory has an EMPTY buildList -> set a new single order (the remembered
            sequence) via changeBuildList with repeat=FALSE.

        Returns the list of factory IDs for which a new order was set this turn (for
        logging/diagnosis)."""
        retriggered = []
        # own buildings of this turn as {id: building} for fast access
        own = {b.get("id"): b for b in gs.my_buildings()}
        for fid in list(self._soft_repeat.keys()):
            b = own.get(fid)
            if b is None:
                # factory destroyed or no longer in the (visible) inventory.
                self._soft_repeat.pop(fid, None)
                continue
            bl = b.get("buildList", []) or []
            if bl:
                # still an order (running OR finished, but not yet unloaded) -> do
                # NOT touch, no build is aborted.
                continue
            # buildList empty -> trigger follow-up production (repeat=False!).
            seq = self._soft_repeat.get(fid) or []
            if not seq:
                continue
            ok, _ = self.do({"type": "changeBuildList",
                             "buildingId": fid,
                             "buildList": [list(s) for s in seq],
                             "buildSpeed": 0, "repeat": False})
            if ok:
                retriggered.append(fid)
        return retriggered

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
