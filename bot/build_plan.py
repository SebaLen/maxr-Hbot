"""Build plan (build-order board) - the BUILD MECHANICS beneath the modes.

The MODES (emergency, expansion) decide WHAT is built where and in which order -
those are the proven rules. The build plan is the tool beneath them: it holds the
assigned build tasks, assigns each to EXACTLY ONE builder (no second one goes
there) and reconciles against the state (remove built ones, release dead
assignments).

TASK CHAINS (across turns): A builder can receive SEVERAL consecutive tasks
(e.g. one pioneer all 4 water platforms of a mine area). These fields are then
BLOCKED for other builders. The builder works through its chain; the material for
the WHOLE chain is loaded once (no return trips). Processing order: depending on
the task (platforms by proximity, connector chains by order). If a task fails
(bridge rejects), ONLY that one is released, the rest of the chain remains.

A task (BuildTask):
  - field:     (x, y) build field (for 2x2: top-left corner)
  - sid:       building type (secondPart)
  - builder:   ID of the assigned builder
  - status:    'assigned' | 'building' | 'done'
  - order:     order within the builder's chain (small first)
  - by_distance: True = order by proximity to the builder (platforms),
                 False = fixed order (connector chain from inside out)
"""


class BuildTask:
    __slots__ = ("field", "sid", "builder", "status", "order",
                 "metal_budget", "name", "by_distance",
                 "state", "last_path_cost", "no_progress_turns")

    # Explicit task states (basis for the path-cost check):
    #   IDLE              no active task (placeholder)
    #   WAITING_RESOURCES builder has too little material and loads/waits for priority
    #   EN_ROUTE          builder has enough material and drives to the build site
    #   BUILDING          builder is building
    # The path-cost stuck check applies ONLY in state EN_ROUTE - in
    # WAITING_RESOURCES the unit is not (yet) under way at all.
    S_IDLE = "IDLE"
    S_WAITING = "WAITING_RESOURCES"
    S_EN_ROUTE = "EN_ROUTE"
    S_BUILDING = "BUILDING"

    def __init__(self, field, sid, builder, order=0, metal_budget=0, name=None,
                 by_distance=False):
        self.field = tuple(field)
        self.sid = sid
        self.builder = builder
        self.status = "assigned"
        self.order = order
        self.metal_budget = metal_budget   # ore for THIS task (build cost)
        self.name = name
        self.by_distance = by_distance
        self.state = "IDLE"                 # explicit state (see above)
        self.last_path_cost = None          # last measured path cost to the field
        self.no_progress_turns = 0          # EN_ROUTE turns without path-cost progress

    def __repr__(self):
        return (f"Task({self.field}, {self.name or self.sid}, builder={self.builder}, "
                f"erz={self.metal_budget}, {self.status}, {self.state})")


class BuildPlan:
    """List of assigned build tasks. A builder can have a CHAIN (several tasks);
    each task belongs to exactly one builder."""

    def __init__(self):
        self._tasks = []   # list of BuildTask

    # ---- filling (by the modes) --------------------------------------------
    def assign(self, field, sid, builder, order=0, metal_budget=0, name=None):
        """Assigns ONE task to a builder and replaces any existing chain of that
        builder (single assignment, as before)."""
        self._tasks = [t for t in self._tasks if t.builder != builder]
        t = BuildTask(field, sid, builder, order=order,
                      metal_budget=metal_budget, name=name)
        self._tasks.append(t)
        return t

    def assign_chain(self, builder, items, by_distance=False):
        """Assigns a CHAIN of tasks to a builder (replaces its old one).
        items: list of (field, sid, metal_budget, name). The fields are then
        blocked for other builders. by_distance controls the processing order.
        Returns the list of tasks."""
        self._tasks = [t for t in self._tasks if t.builder != builder]
        created = []
        for i, item in enumerate(items):
            field, sid, mb, name = item
            t = BuildTask(field, sid, builder, order=i, metal_budget=mb,
                          name=name, by_distance=by_distance)
            self._tasks.append(t)
            created.append(t)
        return created

    def set_budget(self, builder, metal_budget):
        """Re-sets the ore budget for a builder's task(s). Allows the planning phase
        to redistribute the available ore ANEW every turn across all (including
        existing) tasks, instead of freezing it at creation time. For a chain the
        first (next) task gets the budget."""
        chain = sorted([t for t in self._tasks if t.builder == builder],
                       key=lambda t: t.order)
        if not chain:
            return False
        chain[0].metal_budget = metal_budget
        return True

    def task_for_builder(self, builder):
        """The next (smallest-order) task of a builder or None."""
        chain = sorted([t for t in self._tasks if t.builder == builder],
                       key=lambda t: t.order)
        return chain[0] if chain else None

    # ---- queries -----------------------------------------------------------
    def tasks_for(self, builder):
        """All (still open) tasks of a builder (its chain)."""
        return [t for t in self._tasks if t.builder == builder]

    def next_task_for(self, builder, builder_pos=None):
        """Next task to process in a builder's chain (or None).
        Order: by_distance -> nearest field to the builder; otherwise by order."""
        chain = self.tasks_for(builder)
        if not chain:
            return None
        if chain[0].by_distance and builder_pos is not None:
            return min(chain, key=lambda t: (t.field[0]-builder_pos[0])**2
                                            + (t.field[1]-builder_pos[1])**2)
        return min(chain, key=lambda t: t.order)

    def task_for(self, builder):
        """Compatibility: any task of this builder (or None)."""
        chain = self.tasks_for(builder)
        return chain[0] if chain else None

    def chain_metal_needed(self, builder, build_cost_fn):
        """Total ore for the whole (still open) chain of a builder."""
        return sum(build_cost_fn(t.sid) or 0 for t in self.tasks_for(builder))

    def builders_with_tasks(self):
        """IDs of all builders that have at least one task."""
        return {t.builder for t in self._tasks}

    def field_taken(self, field, footprint_fn=None):
        """Is this field (or its footprint) already taken by a task?"""
        f = tuple(field)
        for t in self._tasks:
            if t.field == f:
                return True
            if footprint_fn is not None and f in footprint_fn(t.field, t.sid):
                return True
        return False

    def all_tasks(self):
        return list(self._tasks)

    # ---- maintenance against the state -------------------------------------
    def reconcile(self, gs, object_present_fn, builder_exists_fn):
        """Remove built tasks; remove tasks of dead builders."""
        keep = []
        for t in self._tasks:
            if object_present_fn(t.field, t.sid):
                continue
            if not builder_exists_fn(t.builder):
                continue
            keep.append(t)
        self._tasks = keep

    def release(self, builder):
        """Releases ALL tasks of a builder (whole chain)."""
        self._tasks = [t for t in self._tasks if t.builder != builder]

    def release_task(self, builder, field, sid):
        """Releases ONLY ONE task of the chain (on rejection) - the rest remains."""
        f = tuple(field)
        self._tasks = [t for t in self._tasks
                       if not (t.builder == builder and t.field == f and t.sid == sid)]

    def mark_done(self, builder, field=None, sid=None):
        """Done: without field -> remove the whole chain of the builder; with field ->
        only this one task (the rest of the chain remains)."""
        if field is None:
            self._tasks = [t for t in self._tasks if t.builder != builder]
        else:
            f = tuple(field)
            self._tasks = [t for t in self._tasks
                           if not (t.builder == builder and t.field == f
                                   and (sid is None or t.sid == sid))]

    def __len__(self):
        return len(self._tasks)


# ===========================================================================
# BUILD-SITE BACKLOG (Dungeon-Keeper model) - layer ON TOP OF BuildPlan.
# ===========================================================================
# A BuildSite is ONE development of a deposit. It consists of several ORDERED,
# TYPED components (SiteComponent) with hard dependencies: component N+1 only
# starts once N (in the state!) is finished.
#
# The component list is NOT a fixed template, but is DERIVED ONCE at creation
# from the terrain (derive_components) and then kept stable. Land mine -> only
# mine + coupling. Water mine -> N platforms in front. Mine already stands,
# unconnected -> only coupling.
#
# IMPORTANT (testability / no circular import): This file does NOT import
# GameState. The derivation receives the terrain queries via a thin object
# 'terrain' that provides exactly these methods:
#   mine_build_position(anchor, target_type) -> (ox,oy) | None
#   mine_build_position_with_platforms(anchor, target_type) -> (ox,oy) | None
#   platform_fields_needed(mine_pos) -> [ (x,y), ... ] | None
#   build_cost(building_sid) -> int
#   mine_covering(field) -> building | None      (None = no mine yet)
# GameState already fulfils this interface. In tests it is mocked.

# Vehicle types of a component (strings = names as in specialVehicles).
VEH_ENGINEER = "engineer"      # pioneer: platforms + coupling (SmallBuilding)
VEH_CONSTRUCTOR = "constructor"  # constructor: mine (BigBuilding 2x2)
VEH_BULLDOZER = "bulldozer"    # bulldozer: clear rubble (clear)
VEH_COMBAT = "combat"          # combat unit: destroy enemy/neutral unit

# Component kinds. Order = rough build order (blockers first).
COMP_CLEAR = "clear"           # clear rubble (bulldozer) - upstream
COMP_DESTROY = "destroy"       # destroy blocker unit (offensive) - upstream
COMP_PLATFORM = "platform"     # water platform (pioneer) - upstream
COMP_MINE = "mine"             # the mine itself (constructor)
COMP_CONNECTOR = "connector"   # connection to the network (pioneer)


class SiteComponent:
    """A component of a build site: a typed build step with its own vehicle type,
    own fields, own ore demand and own load/build state. Order via 'order'
    (small first); the dependency is implicit (the previous component must be
    DONE)."""
    __slots__ = ("kind", "vehicle_type", "fields", "sid", "metal_cost",
                 "order", "state", "builder")

    # states of the component life cycle (cf. BuildTask):
    S_WISHED = "WISHED"            # marked, no builder yet
    S_WAITING = "WAITING_RESOURCES"  # builder assigned, loading metal
    S_EN_ROUTE = "EN_ROUTE"       # loaded, driving to the field
    S_BUILDING = "BUILDING"       # building
    S_DONE = "DONE"               # verified finished in the state

    def __init__(self, kind, vehicle_type, fields, sid, metal_cost, order):
        self.kind = kind
        self.vehicle_type = vehicle_type
        self.fields = [tuple(f) for f in fields]
        self.sid = sid
        self.metal_cost = metal_cost
        self.order = order
        self.state = self.S_WISHED
        self.builder = None        # ID of the assigned builder (or None)

    def is_done(self):
        return self.state == self.S_DONE

    def is_active(self):
        """Builder assigned and not yet finished (loads/drives/builds)."""
        return self.builder is not None and self.state != self.S_DONE

    def is_en_route_or_building(self):
        return self.state in (self.S_EN_ROUTE, self.S_BUILDING)

    def __repr__(self):
        return (f"Comp({self.kind}, {self.vehicle_type}, "
                f"felder={len(self.fields)}, erz={self.metal_cost}, "
                f"{self.state}, bauer={self.builder})")


class BuildSite:
    """A build site: development of ONE deposit at the anchor (ax, ay) with an
    ordered component list. The list is derived from the terrain via
    derive_components. The site counts as done once a (connected) mine stands at
    the anchor."""
    __slots__ = ("anchor", "target_type", "amount", "score",
                 "mine_pos", "components", "mandatory")

    def __init__(self, anchor, target_type, amount=0, score=0.0):
        self.anchor = tuple(anchor)
        self.target_type = target_type    # "metal" / "oil" / "gold"
        self.amount = amount               # amount of the target resource (anchor)
        self.score = score                 # priority score (demand x yield)
        self.mine_pos = None               # fixed 2x2 corner (ox,oy), stable
        self.components = []               # ordered list of SiteComponent
        self.mandatory = False             # mandatory priority (first ore mine): always
                                           # first in sorted_open, regardless of score

    # ---- derivation of the components from the terrain --------------------
    def derive_components(self, terrain, mine_sid, platform_sid, connector_sid):
        """Derives the component list ONCE from the terrain and fixes mine_pos.
        Returns True on success, False if the deposit is (currently) not
        developable at all. Order:
          (optional) N platforms -> mine -> coupling.
        Land: no platforms. Mine already stands: only coupling."""
        comps = []
        order = 0
        mine_cost = terrain.build_cost(mine_sid)

        # Does a mine already stand at the anchor? Then the mine part is done;
        # at most the coupling is still missing (network repair handles that
        # separately anyway - noted here only as a component).
        existing = terrain.mine_covering(self.anchor)
        if existing is not None:
            # mine present -> the site consists only of the coupling.
            comps.append(SiteComponent(COMP_CONNECTOR, VEH_ENGINEER,
                                       [self.anchor], connector_sid,
                                       terrain.build_cost(connector_sid), order))
            self.components = comps
            return True

        # 1) directly buildable on land?
        pos = terrain.mine_build_position(self.anchor, target_type=self.target_type)
        if pos is not None:
            self.mine_pos = tuple(pos)
            # no platform component needed (land mine)
        else:
            # 2) only buildable via water platforms?
            pos = terrain.mine_build_position_with_platforms(
                self.anchor, target_type=self.target_type)
            if pos is None:
                return False   # currently not developable at all
            self.mine_pos = tuple(pos)
            needed = terrain.platform_fields_needed(self.mine_pos)
            if needed is None:
                return False   # area invalid
            plat_cost = terrain.build_cost(platform_sid)
            # one separate component per platform, all buildable by the same
            # pioneer (the consumer forms a chain from them).
            for f in needed:
                comps.append(SiteComponent(COMP_PLATFORM, VEH_ENGINEER,
                                           [f], platform_sid, plat_cost, order))
                order += 1

        # determine the 2x2 fields of the mine.
        mfields = [self.mine_pos,
                   (self.mine_pos[0] + 1, self.mine_pos[1]),
                   (self.mine_pos[0], self.mine_pos[1] + 1),
                   (self.mine_pos[0] + 1, self.mine_pos[1] + 1)]

        # set the ANCHOR to the STRONGEST resource field of the finally chosen 2x2.
        # mine_build_position may choose a different 2x2 placement than the one the
        # original anchor came from; the anchor must ALWAYS lie on the highest
        # resource value of the ACTUAL mine area (otherwise it points e.g. next to
        # the strong metal field). Only apply this if the terrain provides the
        # amounts (field_resource_amount); otherwise leave the anchor unchanged.
        if hasattr(terrain, "field_resource_amount"):
            best = max(mfields, key=lambda f: terrain.field_resource_amount(f[0], f[1]))
            if terrain.field_resource_amount(best[0], best[1]) > 0:
                self.anchor = tuple(best)

        # 0) RUBBLE on the mine area? -> upstream clear component (bulldozer).
        # Rubble blocks the mine build; the bulldozer clears it BEFORE the
        # constructor builds. The terrain interface optionally provides
        # rubble_on_fields(fields)->[(x,y),...]; if missing, we assume no rubble
        # (backward compatible). The 'order' of the clear components is NEGATIVE,
        # so that they lie BEFORE platforms (blockers first).
        rubble = []
        if hasattr(terrain, "rubble_on_fields"):
            rubble = terrain.rubble_on_fields(mfields) or []
        for i, f in enumerate(rubble):
            comps.append(SiteComponent(COMP_CLEAR, VEH_BULLDOZER, [f],
                                       None, 0, order=-100 + i))

        # mine (constructor) on the fixed 2x2 area.
        comps.append(SiteComponent(COMP_MINE, VEH_CONSTRUCTOR, mfields,
                                   mine_sid, mine_cost, order))
        order += 1
        # coupling (pioneer) - field is determined by the consumer/network repair.
        comps.append(SiteComponent(COMP_CONNECTOR, VEH_ENGINEER, [self.anchor],
                                   connector_sid, terrain.build_cost(connector_sid),
                                   order))
        self.components = comps
        return True

    # ---- queries ----------------------------------------------------------
    def due_component(self):
        """Next not-yet-finished component in order sequence (the 'due' one) or None
        if all are finished. Hard dependency: only the FIRST not-finished one is
        due - nothing behind it."""
        for c in sorted(self.components, key=lambda c: c.order):
            if not c.is_done():
                return c
        return None

    def required_vehicle_types(self):
        """Which vehicle types this site needs in total (set)."""
        return {c.vehicle_type for c in self.components}

    def metal_needed(self, vehicle_type=None):
        """Total ore for all (not-yet-finished) components, optionally filtered to
        one vehicle type."""
        return sum(c.metal_cost for c in self.components
                   if not c.is_done()
                   and (vehicle_type is None or c.vehicle_type == vehicle_type))

    def all_active_builders_en_route(self):
        """Are ALL active (assigned, not finished) builders of this site already
        loaded + under way/building? Basis for the RELEASE of the material
        reservation (throughput rule)."""
        active = [c for c in self.components if c.is_active()]
        if not active:
            return False
        return all(c.is_en_route_or_building() for c in active)

    def is_committed(self):
        """COMMITTED = the site has been handed over to construction: at least one
        component has a builder assigned (loads/drives/builds). A committed site
        is FROZEN - its fields no longer change and it is not displaced by a
        better proposal until it is finished. Without an assigned builder it is a
        WISHED proposal that is checked against better alternatives every turn."""
        return any(c.builder is not None for c in self.components)

    def footprint_cells(self):
        """All fields this site physically claims: the 2x2 mine area plus the
        component fields (platforms lie within the 2x2 anyway). Basis of the
        overlap check between sites."""
        cells = set()
        if self.mine_pos is not None:
            ox, oy = self.mine_pos
            cells |= {(ox, oy), (ox + 1, oy), (ox, oy + 1), (ox + 1, oy + 1)}
        for c in self.components:
            cells |= {tuple(f) for f in c.fields}
        return cells

    def overlaps(self, other):
        """True if the claimed fields of two sites intersect."""
        return bool(self.footprint_cells() & other.footprint_cells())

    def is_done(self, terrain):
        """Site done = on the PLANNED 2x2 area (mine_pos) stands an own mine. Do NOT
        check at the anchor: with densely packed deposits a NEIGHBOURING mine also
        covers the anchor -> the site would wrongly count as done and the bound
        builder would be released (symptom: constructor jumps between areas).
        'Finished' only if the mine is confirmed to stand EXACTLY on the planned
        area. As long as mine_pos is not yet fixed (land direct build without a set
        mine_pos: fallback to the anchor)."""
        if self.mine_pos is not None:
            mfields = [self.mine_pos,
                       (self.mine_pos[0] + 1, self.mine_pos[1]),
                       (self.mine_pos[0], self.mine_pos[1] + 1),
                       (self.mine_pos[0] + 1, self.mine_pos[1] + 1)]
            mine = terrain.mine_covering(self.mine_pos)
            if mine is None:
                return False
            # the covering mine must REALLY lie on the planned area (its footprint
            # == the planned 4 fields), not an overlapping neighbouring mine that
            # happens to share a corner.
            mpos = terrain.pos(mine) if hasattr(terrain, "pos") else None
            if mpos is not None and tuple(mpos) != tuple(self.mine_pos):
                return False
            return True
        return terrain.mine_covering(self.anchor) is not None

    def __repr__(self):
        return (f"Site({self.anchor}, {self.target_type}, score={self.score:.2f}, "
                f"mine_pos={self.mine_pos}, comps={len(self.components)})")


class BuildBacklog:
    """Persistent, priority-sorted list of open sites (the invisible map marking).
    Lives across turns. Filled/updated in phase 2 (planning) and consumed in
    phase 3 (execution)."""

    def __init__(self):
        self._sites = []   # list of BuildSite, kept descending by score

    def site_at(self, anchor):
        a = tuple(anchor)
        for s in self._sites:
            if s.anchor == a:
                return s
        return None

    def add_or_update(self, site):
        """Adds a site or does NOT replace the existing one at the same anchor
        (fields stay stable!). Only new anchors are added."""
        if self.site_at(site.anchor) is None:
            self._sites.append(site)
            return True
        return False

    def prune_done(self, terrain):
        """Removes finished sites (mine stands)."""
        self._sites = [s for s in self._sites if not s.is_done(terrain)]

    def discard_site(self, site):
        """Removes a specific site (e.g. become unbuildable, area blocked by a
        building). Returns True if it was in the list."""
        if site in self._sites:
            self._sites.remove(site)
            return True
        return False

    def resolve_overlaps(self):
        """Resolves footprint overlaps between sites. Rule (two life phases, opposite
        behaviour):
          - COMMITTED (builder assigned/loads/drives/builds) is FROZEN and wins
            EVERY overlap. An overlapping WISHED proposal is discarded, NEVER the
            running site.
          - If BOTH are only WISHED proposals, the higher score wins; the weaker
            one is discarded (its place becomes free).
        Committed sites do NOT displace each other (they were assigned at a time
        when they did not overlap - the consumer should ensure that; here they are
        not touched).
        Returns the number of discarded sites."""
        # order of consideration: committed first, then highest score - so the
        # 'stronger' site is always already marked as a keeper when a weaker
        # overlapping one is checked.
        order = sorted(self._sites,
                       key=lambda s: (s.is_committed(), s.score), reverse=True)
        keep = []
        dropped = 0
        for s in order:
            conflict = False
            for k in keep:
                if s.overlaps(k):
                    # k is 'stronger' (committed or higher score, because sorted
                    # in first). only discard s if s is NOT committed - two
                    # committed ones must not delete each other.
                    if not s.is_committed():
                        conflict = True
                    break
            if conflict:
                dropped += 1
            else:
                keep.append(s)
        self._sites = keep
        return dropped

    def sorted_open(self):
        """Open sites, highest priority first. MANDATORY sites (mandatory, e.g. the
        first ore mine) ALWAYS come before all others, regardless of score - they
        occupy place 1 until they are built."""
        return sorted(self._sites,
                      key=lambda s: (s.mandatory, s.score), reverse=True)

    def __len__(self):
        return len(self._sites)


class MetalReservation:
    """Priority-ordered reservation of the scarce network metal per site.
    Generalises the former single-slot _ORE_MINE_CONSTRUCTOR_LOCK.

    Rule:
      - The highest-priority site WITH not-yet-supplied builders holds the
        reservation. Only its builders may load; lower-ranked ones get 0.
      - Within the site: first the platform pioneer(s) (whole chain loaded), then
        the constructor. (Land: no pioneer -> constructor immediately.)
      - RELEASE as soon as ALL active builders of the holding site are loaded +
        EN_ROUTE -> the next site may load in the same turn.
      - No free metal = WAIT, no abort.
    """

    def __init__(self):
        pass

    def holding_site(self, backlog):
        """Which site currently holds the reservation? The highest-priority one whose
        active builders are NOT yet all EN_ROUTE. None if no site is loading right
        now (all supplied or backlog empty)."""
        for s in backlog.sorted_open():
            active = [c for c in s.components if c.is_active()]
            if active and not s.all_active_builders_en_route():
                return s
        return None

    def builder_may_load(self, backlog, builder_id):
        """May this builder currently load metal? Only if it belongs to the holding
        site AND it is its turn in that site's internal order (pioneer chains
        before constructor)."""
        site = self.holding_site(backlog)
        if site is None:
            return True   # nobody holds -> free pot (normal operation)
        # does the builder belong to this site?
        own = [c for c in site.components if c.builder == builder_id]
        if not own:
            return False  # other site -> 0
        # internal order: as long as a platform component of this site is not yet
        # loaded (state==WAITING), only its builder may load.
        waiting_platforms = [c for c in site.components
                             if c.kind == COMP_PLATFORM
                             and c.state == SiteComponent.S_WAITING]
        if waiting_platforms:
            return any(c.builder == builder_id for c in waiting_platforms)
        return True
