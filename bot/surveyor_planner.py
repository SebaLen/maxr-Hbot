"""1:1 port of the MAXR surveyor pathfinding algorithm (cSurveyorAi).

IMPORTANT: This module contains ONLY the PLANNING algorithm (where should the
surveyor go next). It executes NO actions and does NOT talk to the bridge. The
bot calls `plan_surveyor_path(...)` and then steers the surveyor MANUALLY via a
normal move action - exactly like the builder units. The client-side auto-move
AI (setAutoMove) is thus NO longer used; that was a desync source (it plans
continuously on a possibly divergent intermediate state).

Source: MAXR release-0.2.17, src/lib/game/logic/surveyorai.cpp + .h.
All constants and formulas are taken over 1:1.
"""

import math

# --- tuning constants (1:1 from surveyorai.cpp) ---------------------------
FIELD_BLOCKED = -10000.0
ACTION_TIMEOUT = 50          # (not used in the bot - the turn cycle controls there)

A = 1.5     # how important to explore as many fields as possible per turn
B = 1.3     # how important to stay near the operation point
C = 9.0     # how important to keep distance from other surveyors
G = 2.0     # how important to move towards already found resources
EXP = -1.0  # influence of other surveyors falls off with dist^EXP

# planLongMove (when there is nothing left to explore next to the surveyor):
D = 1.0     # rather close to the operation point
E = 3.0     # rather close to its position
EXP2 = -1.0
F = 100.0   # rather far from other surveyors

MAX_DISTANCE_OP = 19   # if the distance to the OP exceeds this value, the OP is moved
DISTANCE_NEW_OP = 7    # new OP lies between surveyor and old OP, distance DISTANCE_NEW_OP


# ==========================================================================
# Helper functions (port of the MAXR map/path-calculator semantics)
# ==========================================================================
def _l2(ax, ay, bx, by):
    dx = ax - bx
    dy = ay - by
    return math.sqrt(dx * dx + dy * dy)


def _l2sq(ax, ay, bx, by):
    dx = ax - bx
    dy = ay - by
    return dx * dx + dy * dy


def _collect_around(gs, x, y):
    """cStaticMap::collectAroundPositions for a SMALL (1x1) unit:
    the 8 neighbours, filtered to valid map positions."""
    res = []
    for dx, dy in ((-1, -1), (0, -1), (1, -1),
                   (-1, 0),           (1, 0),
                   (-1, 1), (0, 1), (1, 1)):
        nx, ny = x + dx, y + dy
        if gs.in_bounds(nx, ny):
            res.append((nx, ny))
    return res


def _calc_next_cost(gs, surveyor, sx, sy, dx, dy):
    """cPathCalculator::calcNextCost 1:1 (for a land unit/surveyor).
    Base 4 points * terrain factor; road makes it cheaper; diagonal *1.5."""
    f = gs.unit_factors(surveyor)
    fg = f["ground"]
    fs = f["sea"]
    fc = f["coast"]
    fa = f["air"]

    has_bridge_or_platform = (dx, dy) in gs.non_blocking_building_fields()

    if fa > 0:
        costs = int(4 * fa)
    elif gs.terrain_is_water(dx, dy) and not (has_bridge_or_platform and fg > 0):
        costs = int(4 * fs)
    elif gs.terrain_is_coast(dx, dy) and not (has_bridge_or_platform and fg > 0):
        costs = int(4 * fc)
    else:
        costs = int(4 * fg)

    # road (modifiesSpeed) makes it cheaper - land units only
    spd_mod = gs.road_speed_modifier(dx, dy)
    if spd_mod and spd_mod != 0 and fg > 0:
        costs = int(costs * spd_mod)

    # diagonal: *1.5
    if sx != dx and sy != dy:
        costs = int(costs * 1.5)
    return costs


def _possible_place(gs, surveyor, x, y, ignore_moving):
    """map.possiblePlace(vehicle, pos, checkPlayer) ~ unit-specific walkability.
    Mapped in the bot via is_free_for_unit."""
    return gs.is_free_for_unit(surveyor, x, y)


def _has_resource_explored(gs, x, y):
    return gs.has_resource_explored(x, y)


def _has_resource_at(gs, x, y):
    """map.getResource(pos).typ != None AND explored by the player."""
    return gs.resource_at_explored(x, y)


# ==========================================================================
# cSurveyorAi - state per surveyor (operation point is preserved)
# ==========================================================================
class SurveyorPlanner:
    """Holds the persistent state of a surveyor (operation point), analogous to a
    cSurveyorAi object. One instance per surveyor ID; the bot keeps it across
    turns."""

    def __init__(self, start_x, start_y):
        self.op_x = start_x   # operationPoint
        self.op_y = start_y

    # --- changeOP (1:1) ---------------------------------------------------
    def _change_op(self, vx, vy):
        sq = _l2sq(vx, vy, self.op_x, self.op_y)
        if sq > MAX_DISTANCE_OP * MAX_DISTANCE_OP:
            # operationPoint = pos + ((op - pos) * DISTANCE_NEW_OP) / MAX_DISTANCE_OP
            self.op_x = vx + ((self.op_x - vx) * DISTANCE_NEW_OP) // MAX_DISTANCE_OP
            self.op_y = vy + ((self.op_y - vy) * DISTANCE_NEW_OP) // MAX_DISTANCE_OP

    # --- positionHasBeenSurveyedByPath (1:1) ------------------------------
    @staticmethod
    def _surveyed_by_path(px, py, path):
        # path: list (x,y); l2NormSquared <= 2
        for (ax, ay) in path:
            if _l2sq(ax, ay, px, py) <= 2:
                return True
        return False

    # --- hasAdjacentResources (1:1) ---------------------------------------
    @staticmethod
    def _has_adjacent_resources(gs, cx, cy):
        for (px, py) in _collect_around(gs, cx, cy):
            if _has_resource_explored(gs, px, py) and _has_resource_at(gs, px, py):
                return True
        return False

    # --- calcScoreDistToOtherSurveyor (1:1) -------------------------------
    def _score_dist_other(self, vx, vy, others, px, py, e):
        """others: list (x,y) of the OTHER own surveyors (excluding this one).
        res += dist^e over all others."""
        res = 0.0
        for (ox, oy) in others:
            dist = _l2(px, py, ox, oy)
            if dist == 0:
                # dist^-1 -> inf; MAXR: powf(0,-1)=inf. In practice: very large
                # repulsion. We mirror this with a large value.
                res += 1e6
            else:
                res += math.pow(dist, e)
        return res

    # --- calcFactor (1:1) -------------------------------------------------
    def _calc_factor(self, gs, surveyor, px, py, path, others):
        if not _possible_place(gs, surveyor, px, py, True):
            return FIELD_BLOCKED

        nr_surv = 0.0
        nr_adj_res = 0.0
        for (ax, ay) in _collect_around(gs, px, py):
            if self._surveyed_by_path(ax, ay, path):
                continue
            if not _has_resource_explored(gs, ax, ay):
                nr_surv += 1
                if self._has_adjacent_resources(gs, ax, ay):
                    nr_adj_res += 1

        # diagonal: scale down NrSurvFields
        fx, fy = path[0]
        if _l2sq(px, py, fx, fy) > 1:
            nr_surv /= 1.5
            nr_adj_res /= 1.5

        new_dist_op = _l2(px, py, self.op_x, self.op_y)
        new_dist_surv = self._score_dist_other(0, 0, others, px, py, EXP)

        if nr_surv == 0:
            return FIELD_BLOCKED

        factor = A * nr_surv + G * nr_adj_res - B * new_dist_op - C * new_dist_surv
        return max(factor, FIELD_BLOCKED)

    # --- planMove (1:1, rekursiv) -----------------------------------------
    def _plan_move(self, gs, surveyor, path, remaining_mp, others):
        px, py = path[0]
        best_pos = None
        best_factor = FIELD_BLOCKED
        best_cost = 0

        for (nx, ny) in _collect_around(gs, px, py):
            cost = _calc_next_cost(gs, surveyor, px, py, nx, ny)
            if cost > remaining_mp:
                continue
            f = self._calc_factor(gs, surveyor, nx, ny, path, others)
            if f > best_factor:
                best_factor = f
                best_pos = (nx, ny)
                best_cost = cost

        if best_factor > FIELD_BLOCKED and best_pos is not None:
            path.insert(0, best_pos)   # push_front
            self._plan_move(gs, surveyor, path, remaining_mp - best_cost, others)

    # --- planLongMove (1:1) -----------------------------------------------
    def _plan_long_move(self, gs, surveyor, others):
        """Searches for a far-away, not-yet-explored target. Returns the target
        field (x,y) or None (= 'confused', end auto-move)."""
        sx, sy = gs.pos(surveyor)
        w, h = gs.map_size()
        best = None
        min_val = 0.0

        for x in range(w):
            for y in range(h):
                if not _possible_place(gs, surveyor, x, y, False):
                    continue
                if _has_resource_explored(gs, x, y):
                    continue
                dist_surv_score = self._score_dist_other(0, 0, others, x, y, EXP2)
                dist_op = _l2(x, y, self.op_x, self.op_y)
                dist_surv = _l2(x, y, sx, sy)
                factor = D * dist_op + E * dist_surv + F * dist_surv_score
                if (factor < min_val) or (min_val == 0):
                    min_val = factor
                    best = (x, y)
        if min_val == 0:
            return None
        return best

    # --- run (1:1 der Planungslogik) --------------------------------------
    def plan(self, gs, surveyor, others):
        """Plans the surveyor's next move. Returns:
          - ("move", [path...])   : path (list (x,y), WITHOUT start field) ->
                                    execute via move action with stopOnResource.
          - ("long", (x,y))       : far-away target -> steer towards it via a
                                    move action (stopOnResource).
          - ("confused", None)    : nothing left to explore -> auto-survey off.
        others: list (x,y) of the OTHER own surveyors.
        """
        vx, vy = gs.pos(surveyor)
        self._change_op(vx, vy)

        # movement points as in the original:
        #   movePoints = speed; if speed < speedMax: movePoints += speedMax
        data = surveyor.get("data", {})
        speed = data.get("speedCur", 0) or 0
        speed_max = data.get("speedMax", 0) or 0
        move_points = speed
        if speed < speed_max:
            move_points += speed_max

        path = [(vx, vy)]   # start point for the planning (at the front)
        self._plan_move(gs, surveyor, path, move_points, others)

        # planMove returns the path with the last waypoint at the front -> reverse
        path.reverse()
        # remove start point (not needed in a move path)
        path.pop(0)

        if path:
            return ("move", path)
        # nothing in range -> long move
        target = self._plan_long_move(gs, surveyor, others)
        if target is None:
            return ("confused", None)
        return ("long", target)
