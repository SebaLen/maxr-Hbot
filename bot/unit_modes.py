# -*- coding: utf-8 -*-
"""
unit_modes.py
=============

Unit behaviour modes (concept document section 9). Every own unit can be set to
a mode; the mode determines which enemy range is avoided and how the unit reacts.

Modes:
    PASSIVES_BAUEN     - avoid max(scan,attack); on danger abort build + retreat
    PASSIVE_AUFKLAERUNG- avoid max(scan,attack); move to a safe field, keep scouting
    AKTIVES_STALKING   - avoid max(scan,attack), but keep enemy in own scan range
    ROBUSTE_AUFKLAERUNG- avoid attack only; ignore sight
    HALTEN_DEFENSIV    - stays on position, only evades on acute fire
    KONFLIKT           - ignores both; actively seeks targets (own vs. enemy range)

Foundations (verified):
    - HeatMap layers from heat_map_calc.py: avoid (max(scan,attack)), enemy_attack,
      enemy_scan, danger, threat, own_strength.
    - Reachability via the bridge's pathCost query (search space of ONE turn,
      limited by speedCur).
    - speedCur, hitpointsCur/hitpointsMax from unit["data"] (cDynamicUnitData).
    - canDriveAndFire, canAttack from _static_by_sid (cStaticUnitData).

This file makes NO strategic decisions (which unit gets which mode) - the strategy
layer fills that in later. Here is only the mechanics per mode. For tests the mode
is set uniformly per unit type.
"""

import math
import heat_map_calc as hmc

# --- mode constants ---------------------------------------------------------
PASSIVES_BAUEN      = "PassivesBauen"
PASSIVE_AUFKLAERUNG = "PassiveAufklaerung"
AKTIVES_STALKING    = "AktivesStalking"
ROBUSTE_AUFKLAERUNG = "RobusteAufklaerung"
HALTEN_DEFENSIV     = "HaltenDefensiv"
KONFLIKT            = "Konflikt"

ALL_MODES = (PASSIVES_BAUEN, PASSIVE_AUFKLAERUNG, AKTIVES_STALKING,
             ROBUSTE_AUFKLAERUNG, HALTEN_DEFENSIV, KONFLIKT)

# HP override threshold (section 9.4): below this ratio always retreat.
HP_OVERRIDE_RATIO = 0.25


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------
def unit_speed_cur(unit):
    return (unit.get("data", {}).get("speedCur", 0) or 0)


def unit_hp_ratio(unit):
    """hitpointsCur / hitpointsMax (0..1). 1.0 if no data is available."""
    d = unit.get("data", {})
    cur = d.get("hitpointsCur")
    mx = d.get("hitpointsMax")
    if not mx or mx <= 0 or cur is None:
        return 1.0
    return max(0.0, min(1.0, cur / mx))


def hp_override_active(unit):
    """True if the unit has critically low HP (section 9.4)."""
    return unit_hp_ratio(unit) < HP_OVERRIDE_RATIO


def _static(gs, unit):
    fp = gs.unit_first(unit)
    sp = gs.unit_type(unit)
    return gs._static_by_sid.get((fp, sp), {})


def unit_attack_range(unit):
    """Own attack range (range) from the dynamic data."""
    return (unit.get("data", {}).get("range", 0) or 0)


def unit_scan_range(unit):
    return (unit.get("data", {}).get("scan", 0) or 0)


def can_attack(gs, unit):
    return (_static(gs, unit).get("canAttack") or 0) > 0


def can_drive_and_fire(gs, unit):
    return bool(_static(gs, unit).get("canDriveAndFire", False))


# ---------------------------------------------------------------------------
# Target category & target-type-dependent danger (section 9.1a)
# ---------------------------------------------------------------------------
# canAttack is an eTerrainFlag bitfield: Air=1, Sea=2, Ground=4, AreaSub=16.
# MAXR splits the attack range by these bits (cPlayer::addToSentryMap):
# anti-air (canAttack&Air) threatens only aircraft, ground/sea weapons only ground/sea.
# The target category of the OWN unit (what it IS, i.e. how it is hit) we derive
# from the mobility factors - the surfacePosition value in the data.json is
# unreliable (aircraft are sometimes listed there as "Ground").
TARGET_AIR = "air"
TARGET_GROUND = "ground"   # covers ground AND sea surface (MAXR sentry split)
TARGET_SUB = "sub"         # cloaked submarine under water (hittable only via AreaSub)


def unit_target_category(gs, unit):
    """Target category of the unit for hittability (from mobility + cloaking, NOT
    surfacePosition - that is unreliable):
      'air'    -> flies (factorAir>0)
      'sub'    -> sea-cloaked submarine (factorSea>0, factorGround==0,
                  isStealthOn & Sea) - hittable only with the AreaSub bit (selectTarget)
      'ground' -> everything else (ground, surface ship, building); hittable via
                  the Ground bit.
    For the threat to the OWN unit 'sub' counts practically like an own diving
    target - the bot rarely builds such units; the category is relevant mainly for
    the hittability of ENEMY targets."""
    st = _static(gs, unit)
    if (st.get("factorAir") or 0) > 0:
        return TARGET_AIR
    fs = st.get("factorSea") or 0
    fg = st.get("factorGround") or 0
    stealth = st.get("isStealthOn") or 0
    if fs > 0 and fg == 0 and (stealth & 2):   # Sea-Tarnung -> U-Boot
        return TARGET_SUB
    return TARGET_GROUND


def in_enemy_attack_for(hm, x, y, category):
    """Does (x,y) lie within enemy attack range AGAINST this target category?
    Uses the category-separated layers of the heatmap (from the bridge). A ground
    unit (category='ground') thus ignores the range of pure flak."""
    if category == TARGET_AIR:
        layer = getattr(hm, "enemy_attack_air", None)
    else:
        layer = getattr(hm, "enemy_attack_ground", None)
    if layer is None:
        # older heatmap without separated layers -> generic enemy_attack fallback.
        layer = hm.enemy_attack
    if 0 <= y < hm.height and 0 <= x < hm.width:
        return layer[y][x] == 1
    return False


def enemy_can_hit_category(attacker_canattack_bits, category):
    """Can an attacker with this canAttack bitfield hit the target category?
    From cAttackJob::selectTarget (verified):
      air            <- Air bit (1)
      ground/ship    <- Ground bit (4)   [Ground ONLY; the Sea bit has no own
                        branch in selectTarget - sub/corvet (Sea+Sub) therefore
                        threaten NO ground/ship target in the vehicle slot]
      sub (submarine)<- AreaSub bit (16)  [cloaked submarine under water]
    Usable symmetrically: 'can attacker (enemy OR own unit) hit category'."""
    c = attacker_canattack_bits or 0
    if category == TARGET_AIR:
        return bool(c & 1)
    if category == TARGET_SUB:
        return bool(c & 16)
    return bool(c & 4)


def _attack_avoid_mode(gs, unit):
    """avoid_mode string for attack avoidance matching the unit's target category:
    'attack_air' for aircraft, 'attack_ground' for ground/sea. This way the
    retreat only avoids the ranges that actually threaten the unit."""
    if unit_target_category(gs, unit) == TARGET_AIR:
        return "attack_air"
    return "attack_ground"


# ---------------------------------------------------------------------------
# Reachability: search space of ONE turn (section 9.3 / design question F)
# ---------------------------------------------------------------------------
def reachable_fields(gs, conn, unit, path_cost_to, max_candidates=40,
                     hm=None, avoid_mode=None):
    """Returns the fields reachable in ONE turn for the unit as a list [(x,y)].

    Procedure:
      1. Local candidate space: all walkable fields in the square radius speedCur
         around the unit (a field costs at least 1 movement point, so nothing
         beyond speedCur steps is reachable).
      2. Real reachability per candidate via the pathCost query (the game's own
         pathfinder): reachable AND cost <= speedCur.

    Limited to max_candidates so that the number of pathCost queries stays bounded.
    The current field is always included (staying put is valid).

    IMPORTANT (hm/avoid_mode): Without a heatmap the candidates are capped purely
    by proximity. That is fatal for a RETREAT - the nearest fields are often all
    still in the danger zone, and the few slots crowd out the more distant SAFE
    fields. With hm + avoid_mode SAFE fields (not avoided for the mode) are
    therefore PREFERRED in the cap, then by proximity. This way there is always a
    safe reachable field in the candidates, if one exists.
    """
    speed = unit_speed_cur(unit)
    x0, y0 = gs.pos(unit)
    here = (x0, y0)
    if speed <= 0:
        return [here]

    # 1. local candidate space (walkable, within speedCur steps Chebyshev)
    raw = []
    for dy in range(-speed, speed + 1):
        for dx in range(-speed, speed + 1):
            if dx == 0 and dy == 0:
                continue
            x, y = x0 + dx, y0 + dy
            if not gs.in_bounds(x, y):
                continue
            if not gs.is_free_for_unit(unit, x, y):
                continue
            raw.append((x, y))

    if hm is not None and avoid_mode is not None:
        # safe fields first (0 = safe), then by proximity. This way the cap
        # always contains reachable safe fields, if any exist.
        def _key(p):
            unsafe = 1 if hmc._field_avoided_for_mode(hm, p[0], p[1], avoid_mode) else 0
            return (unsafe, (p[0] - x0) ** 2 + (p[1] - y0) ** 2)
        raw.sort(key=_key)
    else:
        # sort by proximity (saves pathCost queries)
        raw.sort(key=lambda p: (p[0] - x0) ** 2 + (p[1] - y0) ** 2)
    raw = raw[:max_candidates]

    # 2. real reachability via pathCost
    result = [here]
    for cand in raw:
        rep = path_cost_to(conn, unit["id"], cand)
        if not rep:
            continue
        if rep.get("reachable") and (rep.get("cost", 0) or 0) <= speed:
            result.append(cand)
    return result


# ---------------------------------------------------------------------------
# Retreat / evasion: shared core for the avoidance modes
# ---------------------------------------------------------------------------
def _retreat(gs, conn, unit, hm, path_cost_to, avoid_mode,
             require_enemy_in_own_scan=False, safe_only=False):
    """Moves the unit to the best safe/least-dangerous field (9.3).
    Returns (moved: bool, target|None, reason).

    avoid_mode: "max" (max(scan,attack)) or "attack" (fire only).
    require_enemy_in_own_scan: only for AktivesStalking.
    safe_only: no idea-B fallback (None if no safe field) - for stalking.
    """
    cands = reachable_fields(gs, conn, unit, path_cost_to,
                             hm=hm, avoid_mode=avoid_mode)
    own_centers = None
    if require_enemy_in_own_scan:
        own_centers = _enemy_positions_with_own_scan(gs, unit, hm)
    target = hmc.select_safe_target(
        hm, cands, avoid_mode=avoid_mode,
        require_enemy_in_own_scan=require_enemy_in_own_scan,
        own_scan_centers=own_centers,
        safe_only=safe_only,
    )
    if target is None:
        return (False, None, "kein Zielfeld")
    if target == gs.pos(unit):
        return (False, target, "bereits auf bestem Feld")
    ok, reason = conn.do({"type": "move", "unitId": unit["id"],
                          "target": list(target)})
    return (ok, target, reason)


def _enemy_positions_with_own_scan(gs, unit, hm):
    """List [((ex,ey), own_scan_radius), ...] of all visible enemies, each paired
    with the OWN scan range of the stalking unit. For AktivesStalking (keep the
    enemy in own scan range)."""
    own_scan = unit_scan_range(unit)
    me_id = gs.me.get("id") if gs.me else None
    out = []
    for p in gs.model.get("players", []):
        if p.get("id") == me_id:
            continue
        for v in p.get("vehicles", []):
            out.append((tuple(gs.pos(v)), own_scan))
        for b in p.get("buildings", []):
            out.append((tuple(gs.pos(b)), own_scan))
    return out


# ---------------------------------------------------------------------------
# The individual modes
# ---------------------------------------------------------------------------
def apply_mode(gs, conn, unit, mode, hm, path_cost_to, log=None):
    """Executes the mode for ONE unit. Returns a result dict:
        {"unit": id, "mode": mode, "action": <str>, "target": (x,y)|None,
         "hp_override": bool}
    'action' briefly describes what was done (for console/test output).
    """
    def _log(msg):
        if log:
            log(msg)

    uid = unit["id"]
    x, y = gs.pos(unit)
    hp_or = hp_override_active(unit)

    # --- HP override (9.4): below threshold ALWAYS retreat, even in conflict ---
    if hp_or:
        moved, tgt, reason = _retreat(gs, conn, unit, hm, path_cost_to,
                                      avoid_mode="max")
        act = f"HP-Override Rueckzug -> {tgt}" if moved else f"HP-Override: {reason}"
        _log(f"[Mode {mode}] Einheit {uid} @ ({x},{y}) HP<25% -> {act}")
        return {"unit": uid, "mode": mode, "action": act, "target": tgt,
                "hp_override": True}

    # --- mode-specific ------------------------------------------------------
    if mode == PASSIVES_BAUEN:
        return _mode_passives_bauen(gs, conn, unit, hm, path_cost_to, _log)
    if mode == PASSIVE_AUFKLAERUNG:
        return _mode_passive_aufklaerung(gs, conn, unit, hm, path_cost_to, _log)
    if mode == AKTIVES_STALKING:
        return _mode_aktives_stalking(gs, conn, unit, hm, path_cost_to, _log)
    if mode == ROBUSTE_AUFKLAERUNG:
        return _mode_robuste_aufklaerung(gs, conn, unit, hm, path_cost_to, _log)
    if mode == HALTEN_DEFENSIV:
        return _mode_halten_defensiv(gs, conn, unit, hm, path_cost_to, _log)
    if mode == KONFLIKT:
        return _mode_konflikt(gs, conn, unit, hm, path_cost_to, _log)

    _log(f"[Mode ?] Einheit {uid}: unbekannter Modus '{mode}'")
    return {"unit": uid, "mode": mode, "action": "unbekannter Modus",
            "target": None, "hp_override": False}


def _result(uid, mode, action, target):
    return {"unit": uid, "mode": mode, "action": action,
            "target": target, "hp_override": False}


def _mode_passives_bauen(gs, conn, unit, hm, path_cost_to, log):
    """Avoid ONLY the attack range of DANGEROUS enemies (enemy_attack layer, which
    contains only units with canAttack). Harmless enemies - enemy
    surveyors/construction vehicles (canAttack=0, no attack range) - are IGNORED:
    a builder unit should not abort its build because of a harmless surveyor
    driving by. If the unit is within enemy ATTACK range: abort the build
    IMMEDIATELY (finishBuild detaches from the build, the build is sacrificed) and
    retreat. Otherwise do nothing (it builds/stands safely)."""
    uid = unit["id"]
    x, y = gs.pos(unit)
    cat = unit_target_category(gs, unit)
    if not in_enemy_attack_for(hm, x, y, cat):
        log(f"[PassivesBauen] Einheit {uid} @ ({x},{y}) ausser Beschussreichweite -> baut weiter")
        return _result(uid, PASSIVES_BAUEN, "sicher, baut weiter", None)

    # within fire range: if the unit is building, detach from the build (sacrifice it).
    if unit.get("isBuilding"):
        # determine the retreat target first (out of attack range), so that
        # finishBuild has an escapePosition.
        am = _attack_avoid_mode(gs, unit)
        cands = reachable_fields(gs, conn, unit, path_cost_to,
                                 hm=hm, avoid_mode=am)
        tgt = hmc.select_safe_target(hm, cands, avoid_mode=am)
        esc = tgt if tgt and tgt != (x, y) else None
        if esc is None:
            # no safe field -> detach from the build on the spot, neighbour as escape
            for nb in gs.free_neighbors8(x, y, unit=unit):
                esc = nb
                break
        if esc is not None:
            ok, reason = conn.do({"type": "finishBuild", "unitId": uid,
                                  "escapePosition": list(esc)})
            act = (f"Bau abgebrochen, Rueckzug -> {esc}" if ok
                   else f"Bauabbruch fehlgeschlagen ({reason})")
            log(f"[PassivesBauen] Einheit {uid} @ ({x},{y}) BESCHUSSGEFAHR -> {act}")
            return _result(uid, PASSIVES_BAUEN, act, esc if ok else None)

    # not building (any more) -> simply retreat out of the attack range.
    moved, tgt, reason = _retreat(gs, conn, unit, hm, path_cost_to,
                                  _attack_avoid_mode(gs, unit))
    act = f"Rueckzug -> {tgt}" if moved else f"kein Rueckzug ({reason})"
    log(f"[PassivesBauen] Einheit {uid} @ ({x},{y}) BESCHUSSGEFAHR -> {act}")
    return _result(uid, PASSIVES_BAUEN, act, tgt)


def _mode_passive_aufklaerung(gs, conn, unit, hm, path_cost_to, log):
    """Avoid max(scan,attack). In danger: move to a safe field. Safe: continue
    scouting (here the movement target is left to the caller/scout logic; this
    mode only handles the evasion)."""
    uid = unit["id"]
    x, y = gs.pos(unit)
    if not hmc.is_avoided(hm, x, y):
        log(f"[PassiveAufklaerung] Einheit {uid} @ ({x},{y}) sicher -> Aufklaerung")
        return _result(uid, PASSIVE_AUFKLAERUNG, "sicher, Aufklaerung weiter", None)
    moved, tgt, reason = _retreat(gs, conn, unit, hm, path_cost_to, "max")
    act = f"Ausweichen -> {tgt}" if moved else f"kein Ausweichfeld ({reason})"
    log(f"[PassiveAufklaerung] Einheit {uid} @ ({x},{y}) GEFAHR -> {act}")
    return _result(uid, PASSIVE_AUFKLAERUNG, act, tgt)


def _mode_aktives_stalking(gs, conn, unit, hm, path_cost_to, log):
    """Avoid max(scan,attack), but keep the enemy in OWN scan range (ring). Only if
    a SAFE ring field exists is stalking done; otherwise fallback
    PassiveAufklaerung (retreat safely)."""
    uid = unit["id"]
    x, y = gs.pos(unit)
    moved, tgt, reason = _retreat(gs, conn, unit, hm, path_cost_to, "max",
                                  require_enemy_in_own_scan=True, safe_only=True)
    if tgt is not None:
        act = f"Stalk-Reposition -> {tgt}" if moved else f"Stalk haelt Position ({reason})"
        log(f"[AktivesStalking] Einheit {uid} @ ({x},{y}) -> {act}")
        return _result(uid, AKTIVES_STALKING, act, tgt)
    # no safe ring field -> fallback PassiveAufklaerung
    log(f"[AktivesStalking] Einheit {uid} @ ({x},{y}) kein sicheres Ring-Feld -> Fallback PassiveAufklaerung")
    res = _mode_passive_aufklaerung(gs, conn, unit, hm, path_cost_to, log)
    res["mode"] = AKTIVES_STALKING
    res["action"] = "Fallback PassiveAufklaerung: " + res["action"]
    return res


def _mode_robuste_aufklaerung(gs, conn, unit, hm, path_cost_to, log):
    """Avoid attack only (enemy_attack), ignore sight. Conflict with other COMBAT
    units (attack range) is avoided - but HARMLESS targets (canAttack=0: enemy
    surveyors/builders/weaponless buildings) within own range are attacked
    (risk-free kill)."""
    uid = unit["id"]
    x, y = gs.pos(unit)
    # opportunistic kill: attack a harmless target within own range.
    tgt_atk = _attack_target_in_range(gs, unit, only_harmless=True)
    if tgt_atk is not None:
        ok, _ = _do_attack(conn, unit, tgt_atk, log, "RobusteAufklaerung")
        if ok:
            return _result(uid, ROBUSTE_AUFKLAERUNG,
                           f"Angriff ungefaehrliches Ziel {tgt_atk['id']}", None)
        # attack rejected (e.g. target not hittable) -> continue normally.
    if not in_enemy_attack_for(hm, x, y, unit_target_category(gs, unit)):
        log(f"[RobusteAufklaerung] Einheit {uid} @ ({x},{y}) kein Beschuss -> Aufklaerung")
        return _result(uid, ROBUSTE_AUFKLAERUNG, "kein Beschuss, Aufklaerung weiter", None)
    moved, tgt, reason = _retreat(gs, conn, unit, hm, path_cost_to,
                                  _attack_avoid_mode(gs, unit))
    act = f"raus aus Beschuss -> {tgt}" if moved else f"kein Ausweichfeld ({reason})"
    log(f"[RobusteAufklaerung] Einheit {uid} @ ({x},{y}) BESCHUSS -> {act}")
    return _result(uid, ROBUSTE_AUFKLAERUNG, act, tgt)


def _mode_halten_defensiv(gs, conn, unit, hm, path_cost_to, log):
    """Stays on position. Evades ONLY on acute fire (field within enemy attack
    range, enemy_attack layer), ignores pure sight. Uses enemy_attack (consistent
    with the bridge range map), not the weighted danger layer - so the mode reacts
    correctly even when the ranges come from the bridge."""
    uid = unit["id"]
    x, y = gs.pos(unit)
    # opportunistic kill: attack a harmless target within own range without
    # leaving the position.
    tgt_atk = _attack_target_in_range(gs, unit, only_harmless=True)
    if tgt_atk is not None:
        ok, _ = _do_attack(conn, unit, tgt_atk, log, "HaltenDefensiv")
        if ok:
            return _result(uid, HALTEN_DEFENSIV,
                           f"Angriff ungefaehrliches Ziel {tgt_atk['id']}", None)
        # attack rejected -> continue normally (hold position / evade).
    if not in_enemy_attack_for(hm, x, y, unit_target_category(gs, unit)):
        log(f"[HaltenDefensiv] Einheit {uid} @ ({x},{y}) ruhig -> haelt Position")
        return _result(uid, HALTEN_DEFENSIV, "haelt Position", None)
    moved, tgt, reason = _retreat(gs, conn, unit, hm, path_cost_to,
                                  _attack_avoid_mode(gs, unit))
    act = f"unter Beschuss, weicht aus -> {tgt}" if moved else f"kein Ausweichfeld ({reason})"
    log(f"[HaltenDefensiv] Einheit {uid} @ ({x},{y}) BESCHUSS -> {act}")
    return _result(uid, HALTEN_DEFENSIV, act, tgt)


def _mode_konflikt(gs, conn, unit, hm, path_cost_to, log):
    """Ignores avoid/scan. Attacks ACTIVELY - ANY target, no distinction (dangerous
    or not). If a target already stands within own attack range, it is attacked.
    Otherwise the unit looks for a field from which it has the enemy in own range,
    ideally outside enemy attack range (I fire, the enemy not yet)."""
    uid = unit["id"]
    x, y = gs.pos(unit)
    own_range = unit_attack_range(unit)
    if own_range <= 0 or not can_attack(gs, unit):
        log(f"[Konflikt] Einheit {uid} @ ({x},{y}) keine Waffe -> nichts")
        return _result(uid, KONFLIKT, "keine Waffe", None)

    # 1. if a target (dangerous or not) is within own range -> attack.
    tgt_atk = _attack_target_in_range(gs, unit, only_harmless=False)
    if tgt_atk is not None:
        ok, _ = _do_attack(conn, unit, tgt_atk, log, "Konflikt")
        if ok:
            return _result(uid, KONFLIKT, f"Angriff Ziel {tgt_atk['id']}", None)
        # attack rejected -> continue to positioning.

    enemies = _enemy_positions(gs)
    if not enemies:
        log(f"[Konflikt] Einheit {uid} @ ({x},{y}) kein Feind sichtbar -> haelt")
        return _result(uid, KONFLIKT, "kein Feind sichtbar", None)

    cands = reachable_fields(gs, conn, unit, path_cost_to)
    cat = unit_target_category(gs, unit)
    # ideal fields: enemy in own range, itself outside enemy attack
    ideal = []
    in_range_only = []
    for (cx, cy) in cands:
        # nearest enemy from this field
        nearest = min(enemies, key=lambda e: (e[0]-cx)**2 + (e[1]-cy)**2)
        d2 = (nearest[0]-cx)**2 + (nearest[1]-cy)**2
        if d2 <= own_range * own_range:
            if not in_enemy_attack_for(hm, cx, cy, cat):
                ideal.append((cx, cy))
            else:
                in_range_only.append((cx, cy))
    target = None
    if ideal:
        # ideal: close to firing, but safe. Lowest threat as tie-break.
        target = min(ideal, key=lambda p: hmc.threat_at(hm, p[0], p[1]))
        kind = "ideal (feuern, Gegner nicht)"
    elif in_range_only:
        target = min(in_range_only, key=lambda p: hmc.threat_at(hm, p[0], p[1]))
        kind = "in Reichweite (Schlagabtausch)"
    if target is None or target == (x, y):
        log(f"[Konflikt] Einheit {uid} @ ({x},{y}) keine bessere Position -> haelt")
        return _result(uid, KONFLIKT, "haelt Position", target)
    ok, reason = conn.do({"type": "move", "unitId": uid, "target": list(target)})
    act = f"{kind} -> {target}" if ok else f"Bewegung abgelehnt ({reason})"
    log(f"[Konflikt] Einheit {uid} @ ({x},{y}) -> {act}")
    return _result(uid, KONFLIKT, act, target if ok else None)


def _enemy_positions(gs):
    me_id = gs.me.get("id") if gs.me else None
    out = []
    for p in gs.model.get("players", []):
        if p.get("id") == me_id:
            continue
        for v in p.get("vehicles", []):
            out.append(tuple(gs.pos(v)))
        for b in p.get("buildings", []):
            out.append(tuple(gs.pos(b)))
    return out


def _enemy_targets(gs, relative_to_category=None):
    """All visible enemy units AND buildings as target infos:
    [{"id","pos","is_dangerous"}]. Buildings included (also gun turrets).
    Neutrals are NOT listed as targets (no enemy status).

    is_dangerous:
      - relative_to_category=None: does the enemy have canAttack>0 at all
        (old behaviour, category-independent).
      - relative_to_category='air'/'ground': can the enemy hit EXACTLY THIS
        category (i.e. return-fire risk for an own unit of that category).
        A flak (canAttack=Air) is then is_dangerous=False for a ground target."""
    me_id = gs.me.get("id") if gs.me else None
    out = []
    for p in gs.model.get("players", []):
        if p.get("id") == me_id:
            continue
        for u in p.get("vehicles", []) + p.get("buildings", []):
            bits = _static(gs, u).get("canAttack") or 0
            if relative_to_category is None:
                dangerous = bits > 0
            else:
                dangerous = enemy_can_hit_category(bits, relative_to_category)
            out.append({
                "id": u["id"],
                "pos": tuple(gs.pos(u)),
                "is_dangerous": dangerous,
                # target category of the ENEMY (what it IS) - for checking whether
                # the own weapon can hit it at all.
                "target_category": unit_target_category(gs, u),
            })
    return out


def _attack_target_in_range(gs, unit, only_harmless):
    """Looks for an attackable enemy target within OWN attack range from the unit's
    current field. only_harmless=True -> only targets that CANNOT shoot back at
    the OWN category (risk-free kill, no return fire); False -> any target.
    Returns the nearest matching target dict or None.
    (Geometry: l2 distance <= own range, like the cRangeMap circle check.)"""
    own_range = unit_attack_range(unit)
    if own_range <= 0 or not can_attack(gs, unit):
        return None
    # "harmless" is relative to the category of the own unit: a target is
    # harmless if it cannot hit MY category.
    cat = unit_target_category(gs, unit)
    own_bits = _static(gs, unit).get("canAttack") or 0
    x, y = gs.pos(unit)
    best = None
    best_d2 = None
    for t in _enemy_targets(gs, relative_to_category=cat):
        if only_harmless and t["is_dangerous"]:
            continue
        # only attack targets that the own weapon can actually hit (e.g. a ground
        # unit without the AreaSub bit cannot hit a cloaked submarine). Avoids
        # empty attacks rejected by the bridge.
        if not enemy_can_hit_category(own_bits, t["target_category"]):
            continue
        tx, ty = t["pos"]
        d2 = (tx - x) ** 2 + (ty - y) ** 2
        if d2 <= own_range * own_range:
            if best_d2 is None or d2 < best_d2:
                best_d2 = d2
                best = t
    return best


def _do_attack(conn, unit, target, log, mode_label):
    """Sends the attack on a target dict. The bridge executes client->attack
    (target validation/range is checked by the client). Returns (ok, reason)."""
    ok, reason = conn.do({"type": "attack", "unitId": unit["id"],
                          "targetId": target["id"]})
    tag = "ungefaehrliches" if not target["is_dangerous"] else "Ziel"
    if ok:
        log(f"[{mode_label}] Einheit {unit['id']} greift {tag} Ziel "
            f"{target['id']} @ {target['pos']} an.")
    else:
        log(f"[{mode_label}] Angriff auf {target['id']} abgelehnt ({reason}).")
    return ok, reason
