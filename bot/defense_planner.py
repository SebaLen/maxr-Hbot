# -*- coding: utf-8 -*-
"""
defense_planner.py
==================

Placement of the DEFENSE SCREEN (building block 3). Pure geometry/planning
module without side effects: it reads the GameState and returns site proposals.
The actual building (commissioning a pioneer) is done by the caller in bot_run.

CONCEPT (agreed spec, see skill architecture.md "Defense shield - FINAL"):

  SCREEN ZONE = ring at distance 5-8 fields OUTSIDE the network edge (main_component).
    NOT 1-8: no attacks come from within the network, the enemy is intercepted in
    the 5-8 band. Zone and build ring coincide (both 5-8).

  GROUP instead of single building. A position is ALWAYS a fixed group of four,
    placed like a 2x2 mine block (four neighbouring fields, but four SEPARATE
    buildings - not a 2x2 big building):
      radar, gun_ari (artillery), gun_aa (anti-air), gun_missel (missiles).
    Build order: RADAR, gun_ari, gun_aa, gun_missel (radar first -> the position
    sees immediately; each group brings its own radar, so the radar underlay is
    automatically satisfied per group).

  COVERAGE MEASURE = gun_ari range (r8). The artillery is the MIDDLE of the three
    weapon ranges (gun_aa r8, gun_ari r8, gun_missel r11). If the zone is covered
    densely/overlapping at ARTILLERY range, the equally far-reaching anti-air (r8)
    is just as dense and the farther-reaching missiles (r11) even more so.
    Artillery range is therefore the binding measure. cover_target (1/2/3) counts
    by how many GROUPS (via artillery circle) a zone field is hit. Circle centre =
    CENTRE of the group's 2x2 block.

  PLACEMENT: A new group is built at the 2x2 site (in the 5-8 ring, buildable as
    2x2, network-connectable) that raises the most still-under-cover_target zone
    fields (overlap as tie-break).

  REBUILD on loss: If ONE member of a group is lost, that specific building type
    is rebuilt at the nearest possible buildable field (measured from the 2x2
    centre) - NOT a whole new group.

The circle geometry is IDENTICAL to the engine/heatmap: l2NormSquared <= 4*r^2,
here simplified for unitSize 1 to dx*dx+dy*dy <= r*r (see heat_map_calc._in_circle).
"""

# Group composition and fixed build order (building names as in unitsData;
# sid is resolved at runtime via building_sid_by_name).
GROUP_MEMBERS = ("radar", "gun_ari", "gun_aa", "gun_missel")
GROUP_BUILD_ORDER = ("radar", "gun_ari", "gun_aa", "gun_missel")

# Coverage measure: artillery range. Read at runtime from the unit data
# (read_group_ranges); this value is only a fallback for tests.
_ARI_RANGE_FALLBACK = 8

# Ring depth (distance from network edge): zone and build sites 5-8 fields outside.
ZONE_MIN_DIST = 5
ZONE_MAX_DIST = 8


def _in_circle(cx, cy, r, x, y):
    """Engine-exact circle check (unitSize 1): is (x,y) within radius r around (cx,cy)?
    dx*dx+dy*dy <= r*r, identical to heat_map_calc._in_circle / cRangeMap::isInRange."""
    dx = x - cx
    dy = y - cy
    return dx * dx + dy * dy <= r * r


def zone_fields(gs, main_cells=None):
    """The SCREEN ZONE: all fields at distance ZONE_MIN_DIST..ZONE_MAX_DIST (Chebyshev,
    8-neighbour spread) from the network edge (main_component). Return: set of (x,y).

    Distance via BFS in 8-neighbourhood from the network: network fields have
    distance 0, their neighbours 1, etc. Fields with 5<=d<=8 form the zone. Fields
    that belong to the network themselves are excluded (distance 0)."""
    if main_cells is None:
        main_cells = gs.main_component()
    if not main_cells:
        return set()
    # BFS distance from the network (8-neighbourhood -> Chebyshev distance).
    dist = {cell: 0 for cell in main_cells}
    frontier = list(main_cells)
    d = 0
    while frontier and d < ZONE_MAX_DIST:
        d += 1
        nxt = []
        for (x, y) in frontier:
            for (nx, ny) in gs.neighbors8(x, y):
                if (nx, ny) in dist:
                    continue
                if not gs.in_bounds(nx, ny):
                    continue
                dist[(nx, ny)] = d
                nxt.append((nx, ny))
        frontier = nxt
    return {cell for cell, dd in dist.items() if ZONE_MIN_DIST <= dd <= ZONE_MAX_DIST}


def group_centers(gs):
    """Centres of all already built own defense GROUPS, derived from the existing
    gun_ari buildings (the artillery is the coverage anchor). Each artillery stands
    for one group; its field is (close enough) the 2x2 centre for the r8 coverage.
    Return: list of (x,y)."""
    centers = []
    ari_sid = gs.building_sid_by_name("gun_ari")
    if ari_sid is None:
        return centers
    for b in gs.my_buildings():
        if gs.unit_type(b) == ari_sid and gs.unit_first(b) == 1:
            centers.append(gs.pos(b))
    return centers


def coverage_count(gs, zone, centers, ari_range):
    """Artillery coverage count per zone field: how many group centres (centers)
    cover the field with radius ari_range. Return: dict (x,y)->count."""
    cov = {}
    for (x, y) in zone:
        c = 0
        for (cx, cy) in centers:
            if _in_circle(cx, cy, ari_range, x, y):
                c += 1
        cov[(x, y)] = c
    return cov


def has_gap(gs, cover_target, main_cells=None, ari_range=None):
    """True if the screen zone has a gap: some zone field is hit by FEWER than
    cover_target group artillery circles. cover_target None/0 -> no screen demand
    (False)."""
    if not cover_target or cover_target < 1:
        return False
    zone = zone_fields(gs, main_cells)
    if not zone:
        return False
    if ari_range is None:
        ari_range = _resolve_ari_range(gs)
    centers = group_centers(gs)
    cov = coverage_count(gs, zone, centers, ari_range)
    return any(c < cover_target for c in cov.values())


def best_group_site(gs, cover_target, main_cells=None, ari_range=None, occ=None,
                    water_ok=None):
    """Best 2x2 site for a NEW group: the one that raises the most under-target zone
    fields. Return: (ox,oy) top-left corner of the 2x2 block, or None.

    Scoring: for each candidate 2x2 block in the ring (centre at distance 5-8, all
    four fields buildable) the gain = number of zone fields with coverage <
    cover_target that the new artillery circle (around the block centre) additionally
    covers. Tie-break: higher total overlap (also over already covered fields), so
    that dense positions are preferred."""
    zone = zone_fields(gs, main_cells)
    if not zone:
        return None
    if ari_range is None:
        ari_range = _resolve_ari_range(gs)
    if main_cells is None:
        main_cells = gs.main_component()
    if occ is None:
        occ = gs.occupied_fields_for_mine()
    if water_ok is None:
        water_ok = gs.water_walkable_fields()

    centers = group_centers(gs)
    cov = coverage_count(gs, zone, centers, ari_range)
    under = {cell for cell, c in cov.items() if c < cover_target}
    if not under:
        return None

    mine_sid = gs.MINE_SID   # 2x2 land building buildability like a mine

    # candidate blocks: 2x2 placements whose CENTRE lies in the zone (5-8) and that
    # are buildable as 2x2 + network-connectable. We derive candidates from the
    # zone fields (each zone field can be the top-left corner).
    best = None  # (gain, overlap_sum, (ox,oy))
    seen = set()
    for (zx, zy) in zone:
        for (ox, oy) in ((zx, zy), (zx - 1, zy), (zx, zy - 1), (zx - 1, zy - 1)):
            if (ox, oy) in seen:
                continue
            seen.add((ox, oy))
            cells = [(ox, oy), (ox + 1, oy), (ox, oy + 1), (ox + 1, oy + 1)]
            # All four fields buildable (as a land building, like a mine)?
            if not _block_buildable(gs, mine_sid, cells, occ, water_ok):
                continue
            # network connection: at least one block field borders the network OR is
            # bridgeable via a connector. Conservative: orthogonal proximity to the
            # network is NOT required (ring is 5-8 away); the connector build provides
            # the connection. Here only buildability. (Connector chain is handled by
            # the build handler.)
            mcx = ox + 0.5
            mcy = oy + 0.5
            gain = sum(1 for (x, y) in under
                       if _in_circle(mcx, mcy, ari_range, x, y))
            if gain == 0:
                continue
            overlap = sum(1 for (x, y) in zone
                          if _in_circle(mcx, mcy, ari_range, x, y))
            key = (gain, overlap)
            if best is None or key > best[0]:
                best = (key, (ox, oy))
    return best[1] if best else None


def _block_buildable(gs, building_sid, cells, occ, water_ok):
    """All four 2x2 fields in bounds and buildable for a land building?"""
    for (cx, cy) in cells:
        if not gs.in_bounds(cx, cy):
            return False
        if not gs.is_buildable_for_building(building_sid, cx, cy,
                                            occ=occ, water_ok=water_ok):
            return False
    return True


def _resolve_ari_range(gs):
    """Artillery range from the unit data (static). Fallback if not found."""
    ari_sid = gs.building_sid_by_name("gun_ari")
    if ari_sid is not None:
        st = gs._static_by_sid.get((1, ari_sid), {})
        r = st.get("range")
        if r:
            return int(r)
    return _ARI_RANGE_FALLBACK


# ---------------------------------------------------------------------------
# Group construction site: which of the four buildings already stand, which comes
# next, and on which of the four 2x2 fields. Pure logic (no side effect).
# ---------------------------------------------------------------------------

def group_cells(ox, oy):
    """The four fields of a 2x2 group block (top-left corner ox,oy)."""
    return [(ox, oy), (ox + 1, oy), (ox, oy + 1), (ox + 1, oy + 1)]


def group_midpoint(ox, oy):
    """Centre of the 2x2 block (for ranges and proximity measurement)."""
    return (ox + 0.5, oy + 0.5)


def members_present(gs, ox, oy):
    """Which group members already stand on the four block fields? Return:
    dict member_name -> (x,y) of the already built building (own buildings only)."""
    cells = set(group_cells(ox, oy))
    name_by_sid = {}
    for m in GROUP_MEMBERS:
        sid = gs.building_sid_by_name(m)
        if sid is not None:
            name_by_sid[sid] = m
    present = {}
    for b in gs.my_buildings():
        if gs.unit_first(b) != 1:
            continue
        sid = gs.unit_type(b)
        m = name_by_sid.get(sid)
        if m is None:
            continue
        p = gs.pos(b)
        if p in cells:
            present[m] = p
    return present


def next_member_to_build(gs, ox, oy):
    """Next due group member at the site (ox,oy) in the fixed build order (radar,
    gun_ari, gun_aa, gun_missel). Return: (member_name, sid, field) for the next
    still-missing member on a free block field, or None if the group is complete or
    no free field remains.

    The field is chosen as: the nearest buildable block field to the centre that is
    not yet occupied by a group member. This creates the compact 2x2 block; on
    rebuild the missing member is replaced at the nearest possible field (measured
    from the centre)."""
    present = members_present(gs, ox, oy)
    cells = group_cells(ox, oy)
    used = set(present.values())
    mcx, mcy = group_midpoint(ox, oy)
    occ = gs.occupied_fields_for_mine()
    water_ok = gs.water_walkable_fields()
    for m in GROUP_BUILD_ORDER:
        if m in present:
            continue   # already present
        sid = gs.building_sid_by_name(m)
        if sid is None:
            continue
        # free block fields, buildable for this (land) building, by proximity to the centre
        cands = []
        for (cx, cy) in cells:
            if (cx, cy) in used:
                continue
            if not gs.is_buildable_for_building(sid, cx, cy, occ=occ, water_ok=water_ok):
                continue
            d2 = (cx - mcx) ** 2 + (cy - mcy) ** 2
            cands.append((d2, (cx, cy)))
        if not cands:
            # no free block field for this member -> nearest possible field OUTSIDE
            # the block (rebuild case), by proximity to the centre.
            field = _nearest_buildable_off_block(gs, sid, ox, oy, occ, water_ok)
            if field is None:
                continue
            return (m, sid, field)
        cands.sort(key=lambda t: t[0])
        return (m, sid, cands[0][1])
    return None


def _nearest_buildable_off_block(gs, building_sid, ox, oy, occ, water_ok, max_r=4):
    """Nearest buildable field for a group member, measured from the 2x2 centre, if
    all block fields are occupied (rebuild). Searches ring by ring outwards."""
    mcx, mcy = group_midpoint(ox, oy)
    cands = []
    for dx in range(-max_r, max_r + 1):
        for dy in range(-max_r, max_r + 1):
            x = ox + dx
            y = oy + dy
            if not gs.in_bounds(x, y):
                continue
            if not gs.is_buildable_for_building(building_sid, x, y, occ=occ, water_ok=water_ok):
                continue
            d2 = (x - mcx) ** 2 + (y - mcy) ** 2
            cands.append((d2, (x, y)))
    if not cands:
        return None
    cands.sort(key=lambda t: t[0])
    return cands[0][1]


def group_complete(gs, ox, oy):
    """True if all four group members stand at the site."""
    return len(members_present(gs, ox, oy)) >= len(GROUP_MEMBERS)

