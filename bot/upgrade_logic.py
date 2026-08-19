# -*- coding: utf-8 -*-
"""
upgrade_logic.py
================

Thin wrappers around the bridge's UPGRADE endpoints. NO upgrade logic is
reimplemented in Python - the whole computation (available upgrades, prices,
step sizes) is provided by the MAXR client through the bridge. This file only
offers convenient calls and keeps the credit balance as a global variable.

DEPENDENCY (bridge): These functions expect two endpoints in the botbridge
(see botbridge_upgrades.patch.cpp) that pass through the existing MAXR
functions:
  - QUERY  {"query":"upgrades", "unitId":[fp,sp]}      -> available upgrades
  - ACTION {"type":"buyUpgrades","unitId":[fp,sp],
            "purchases":[{"type":<attr>,"steps":<n>}]}  -> buy

CREDITS: The own credit balance is in the state under me["credits"] (the bridge
keeps 'credits' for the own player, only deletes it for foreign ones). The
upgrades query additionally returns the credit balance directly.

UPGRADE ATTRIBUTES (names as used by the bridge/MAXR):
    damage, shots, range, ammo, armor, hitpoints, scan, speed
"""

UPGRADE_ATTRS = ("damage", "shots", "range", "ammo",
                 "armor", "hitpoints", "scan", "speed")


# ---------------------------------------------------------------------------
# Global variable: current credit balance (analogous to the resource types).
# ---------------------------------------------------------------------------
CREDITS = 0


def refresh_credits(gs):
    """Take the credit balance from the state (me["credits"]) into the global
    variable and return it."""
    global CREDITS
    me = getattr(gs, "me", None)
    if me:
        CREDITS = int(me.get("credits", 0) or 0)
    return CREDITS


def get_credits(gs=None):
    """Return the current credit balance (with gs, refreshed from the state first)."""
    if gs is not None:
        return refresh_credits(gs)
    return CREDITS


# ---------------------------------------------------------------------------
# Type resolution: name -> sID [firstPart, secondPart]
# ---------------------------------------------------------------------------
def _resolve_unit_id(gs, type_name=None, first_part=None, second_part=None):
    """Resolves a type into [firstPart, secondPart]. Either directly, or via a
    name (vehicle first, then building). Returns [fp, sp] or None."""
    if first_part is not None and second_part is not None:
        return [int(first_part), int(second_part)]
    if type_name is not None:
        sp = gs.vehicle_sid_by_name(type_name)
        if sp is not None:
            return [0, sp]
        sp = gs.building_sid_by_name(type_name)
        if sp is not None:
            return [1, sp]
    return None


# ---------------------------------------------------------------------------
# FUNCTION 1: read the available upgrades of a type (via the bridge).
# ---------------------------------------------------------------------------
def read_upgrades(conn, gs, type_name=None, first_part=None, second_part=None):
    """Asks the bridge for the available upgrades of a unit OR building type.
    The computation is done by the MAXR client (cUnitUpgrade::init).

    conn   Conn object (with .query()).
    gs     current GameState (to resolve the name into a sID).
    type_name / first_part+second_part: the type.

    Return (exactly as the bridge answers) or None on error:
      {
        "result": "upgrades",
        "unitId": [fp, sp],
        "credits": <int>,
        "upgrades": [
          {"type": "damage", "curValue": <int>, "nextPrice": <int>,
           "purchased": <int>, "affordable": <bool>}, ...
        ]
      }
    Also updates the global CREDITS from the answer as a side effect.
    """
    global CREDITS
    unit_id = _resolve_unit_id(gs, type_name, first_part, second_part)
    if unit_id is None:
        return None
    rep = conn.query({"query": "upgrades", "unitId": unit_id})
    if not rep or rep.get("result") != "upgrades":
        return None
    if "credits" in rep:
        CREDITS = int(rep.get("credits", 0) or 0)
    return rep


def upgrade_options(conn, gs, type_name=None, first_part=None, second_part=None):
    """Convenient access: only the list of upgrade entries (or [])."""
    rep = read_upgrades(conn, gs, type_name, first_part, second_part)
    return rep.get("upgrades", []) if rep else []


# ---------------------------------------------------------------------------
# FUNCTION 2: perform an upgrade (via the bridge).
# ---------------------------------------------------------------------------
def buy_upgrade(conn, gs, attribute, steps,
                type_name=None, first_part=None, second_part=None):
    """Buys 'steps' upgrade steps of an attribute for a type. The purchase is
    executed by the MAXR client (cClient::buyUpgrades -> cActionBuyUpgrades); the
    cost is computed and checked by the host (credits). If they are not enough,
    the host rejects.

    attribute  one of UPGRADE_ATTRS.
    steps      number of purchase steps (>=1). Each step raises the value by the
               MAXR step size; the price per step increases.
    type_name / first_part+second_part: the type.

    Return: (ok: bool, reason: str|None) - directly the result of conn.do().
    """
    if attribute not in UPGRADE_ATTRS:
        return (False, f"unbekanntes Attribut '{attribute}' "
                       f"(erlaubt: {', '.join(UPGRADE_ATTRS)})")
    if steps is None or steps < 1:
        return (False, "steps muss >= 1 sein")
    unit_id = _resolve_unit_id(gs, type_name, first_part, second_part)
    if unit_id is None:
        return (False, "Typ nicht gefunden")
    action = {
        "type": "buyUpgrades",
        "unitId": unit_id,
        "purchases": [{"type": attribute, "steps": int(steps)}],
    }
    return conn.do(action)


def buy_upgrades(conn, gs, purchases,
                 type_name=None, first_part=None, second_part=None):
    """Like buy_upgrade, but several attributes in ONE command.
    purchases: list of (attribute, steps) tuples OR dicts
               {"type":..,"steps":..}. All for the same type.
    Return: (ok, reason)."""
    unit_id = _resolve_unit_id(gs, type_name, first_part, second_part)
    if unit_id is None:
        return (False, "Typ nicht gefunden")
    norm = []
    for p in purchases:
        if isinstance(p, dict):
            attr, steps = p.get("type"), p.get("steps", 0)
        else:
            attr, steps = p[0], p[1]
        if attr not in UPGRADE_ATTRS:
            return (False, f"unbekanntes Attribut '{attr}'")
        if steps is None or steps < 1:
            return (False, f"steps fuer '{attr}' muss >= 1 sein")
        norm.append({"type": attr, "steps": int(steps)})
    if not norm:
        return (False, "keine purchases angegeben")
    return conn.do({"type": "buyUpgrades", "unitId": unit_id, "purchases": norm})
