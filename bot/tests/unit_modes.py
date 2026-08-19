# -*- coding: utf-8 -*-
"""
unit_modes.py
=============

Einheiten-Verhaltensmodi (Konzeptdokument Abschnitt 9). Jede eigene Einheit kann
in einen Modus gesetzt werden; der Modus bestimmt, welche gegnerische Reichweite
gemieden wird und wie reagiert wird.

Modi:
    PASSIVES_BAUEN     - meide max(scan,attack); bei Gefahr Bau abbrechen + Rueckzug
    PASSIVE_AUFKLAERUNG- meide max(scan,attack); auf sicheres Feld, Aufklaerung weiter
    AKTIVES_STALKING   - meide max(scan,attack), aber Feind in eigener Scan-Range halten
    ROBUSTE_AUFKLAERUNG- meide nur Attack; Sicht ignorieren
    HALTEN_DEFENSIV    - bleibt auf Position, weicht nur bei akutem Beschuss aus
    KONFLIKT           - ignoriert beide; sucht aktiv Ziele (eigene vs. gegn. Range)

Grundlagen (verifiziert):
    - HeatMap-Layer aus heat_map_calc.py: avoid (max(scan,attack)), enemy_attack,
      enemy_scan, danger, threat, own_strength.
    - Erreichbarkeit ueber die pathCost-Query der Bruecke (Suchraum EINES Zuges,
      begrenzt durch speedCur).
    - speedCur, hitpointsCur/hitpointsMax aus unit["data"] (cDynamicUnitData).
    - canDriveAndFire, canAttack aus _static_by_sid (cStaticUnitData).

Diese Datei trifft KEINE strategischen Entscheidungen (welche Einheit welchen
Modus bekommt) - das fuellt spaeter die Strategieschicht. Hier ist nur die
Mechanik je Modus. Fuer Tests wird der Modus pauschal pro Einheitentyp gesetzt.
"""

import math
import heat_map_calc as hmc

# --- Modus-Konstanten -------------------------------------------------------
PASSIVES_BAUEN      = "PassivesBauen"
PASSIVE_AUFKLAERUNG = "PassiveAufklaerung"
AKTIVES_STALKING    = "AktivesStalking"
ROBUSTE_AUFKLAERUNG = "RobusteAufklaerung"
HALTEN_DEFENSIV     = "HaltenDefensiv"
KONFLIKT            = "Konflikt"

ALL_MODES = (PASSIVES_BAUEN, PASSIVE_AUFKLAERUNG, AKTIVES_STALKING,
             ROBUSTE_AUFKLAERUNG, HALTEN_DEFENSIV, KONFLIKT)

# HP-Override-Schwelle (Abschnitt 9.4): unter diesem Anteil immer Rueckzug.
HP_OVERRIDE_RATIO = 0.25


# ---------------------------------------------------------------------------
# Einheiten-Helfer
# ---------------------------------------------------------------------------
def unit_speed_cur(unit):
    return (unit.get("data", {}).get("speedCur", 0) or 0)


def unit_hp_ratio(unit):
    """hitpointsCur / hitpointsMax (0..1). 1.0 wenn keine Daten vorhanden."""
    d = unit.get("data", {})
    cur = d.get("hitpointsCur")
    mx = d.get("hitpointsMax")
    if not mx or mx <= 0 or cur is None:
        return 1.0
    return max(0.0, min(1.0, cur / mx))


def hp_override_active(unit):
    """True, wenn die Einheit kritisch wenig HP hat (Abschnitt 9.4)."""
    return unit_hp_ratio(unit) < HP_OVERRIDE_RATIO


def _static(gs, unit):
    fp = gs.unit_first(unit)
    sp = gs.unit_type(unit)
    return gs._static_by_sid.get((fp, sp), {})


def unit_attack_range(unit):
    """Eigene Attack-Reichweite (range) aus den dynamischen Daten."""
    return (unit.get("data", {}).get("range", 0) or 0)


def unit_scan_range(unit):
    return (unit.get("data", {}).get("scan", 0) or 0)


def can_attack(gs, unit):
    return (_static(gs, unit).get("canAttack") or 0) > 0


def can_drive_and_fire(gs, unit):
    return bool(_static(gs, unit).get("canDriveAndFire", False))


# ---------------------------------------------------------------------------
# Zielkategorie & zieltyp-abhaengige Gefahr (Abschnitt 9.1a)
# ---------------------------------------------------------------------------
# canAttack ist ein eTerrainFlag-Bitfeld: Air=1, Sea=2, Ground=4, AreaSub=16.
# MAXR teilt die Attack-Reichweite nach diesen Bits auf (cPlayer::addToSentryMap):
# Luftabwehr (canAttack&Air) bedroht nur Flieger, Boden/See-Waffen nur Boden/See.
# Die Zielkategorie der EIGENEN Einheit (was sie IST, also wie sie getroffen wird)
# leiten wir aus den Beweglichkeitsfaktoren ab - der surfacePosition-Wert in den
# data.json ist unzuverlaessig (Flieger stehen dort teils als "Ground").
TARGET_AIR = "air"
TARGET_GROUND = "ground"   # umfasst Boden UND See-Oberflaeche (MAXR-Sentry-Split)
TARGET_SUB = "sub"         # getarntes U-Boot unter Wasser (nur via AreaSub treffbar)


def unit_target_category(gs, unit):
    """Zielkategorie der Einheit fuer die Treffbarkeit (aus Beweglichkeit +
    Tarnung, NICHT surfacePosition - der ist unzuverlaessig):
      'air'    -> fliegt (factorAir>0)
      'sub'    -> Sea-getarntes U-Boot (factorSea>0, factorGround==0,
                  isStealthOn & Sea) - nur mit AreaSub-Bit treffbar (selectTarget)
      'ground' -> alles andere (Boden, Oberflaechenschiff, Gebaeude); via
                  Ground-Bit treffbar.
    Fuer die Bedrohung der EIGENEN Einheit zaehlt 'sub' praktisch wie ein eigenes
    Tauchziel - der Bot baut solche Einheiten kaum; relevant ist die Kategorie vor
    allem fuer die Treffbarkeit FEINDLICHER Ziele."""
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
    """Liegt (x,y) in feindlicher Attack-Reichweite GEGEN diese Zielkategorie?
    Nutzt die kategorie-getrennten Layer der Heatmap (aus der Bruecke). Eine
    Bodeneinheit (category='ground') ignoriert so die Reichweite reiner Flak."""
    if category == TARGET_AIR:
        layer = getattr(hm, "enemy_attack_air", None)
    else:
        layer = getattr(hm, "enemy_attack_ground", None)
    if layer is None:
        # Aeltere Heatmap ohne getrennte Layer -> generischer enemy_attack-Fallback.
        layer = hm.enemy_attack
    if 0 <= y < hm.height and 0 <= x < hm.width:
        return layer[y][x] == 1
    return False


def enemy_can_hit_category(attacker_canattack_bits, category):
    """Kann ein Angreifer mit diesem canAttack-Bitfeld die Zielkategorie treffen?
    Aus cAttackJob::selectTarget (verifiziert):
      air            <- Air-Bit (1)
      ground/schiff  <- Ground-Bit (4)   [NUR Ground; das Sea-Bit hat in
                        selectTarget keinen eigenen Zweig - sub/corvet (Sea+Sub)
                        bedrohen daher KEIN Boden-/Schiffsziel im vehicle-slot]
      sub (U-Boot)   <- AreaSub-Bit (16)  [getarntes U-Boot unter Wasser]
    Symmetrisch nutzbar: 'kann Angreifer (Feind ODER eigene Einheit) Kategorie
    treffen'."""
    c = attacker_canattack_bits or 0
    if category == TARGET_AIR:
        return bool(c & 1)
    if category == TARGET_SUB:
        return bool(c & 16)
    return bool(c & 4)


def _attack_avoid_mode(gs, unit):
    """avoid_mode-String fuer die Attack-Meidung passend zur Zielkategorie der
    Einheit: 'attack_air' fuer Flieger, 'attack_ground' fuer Boden/See. So meidet
    der Rueckzug nur die Reichweiten, die die Einheit wirklich bedrohen."""
    if unit_target_category(gs, unit) == TARGET_AIR:
        return "attack_air"
    return "attack_ground"


# ---------------------------------------------------------------------------
# Erreichbarkeit: Suchraum EINES Zuges (Abschnitt 9.3 / Designfrage F)
# ---------------------------------------------------------------------------
def reachable_fields(gs, conn, unit, path_cost_to, max_candidates=40,
                     hm=None, avoid_mode=None):
    """Liefert die in EINEM Zug erreichbaren Felder der Einheit als Liste [(x,y)].

    Vorgehen:
      1. Lokaler Kandidatenraum: alle begehbaren Felder im Quadrat-Radius
         speedCur um die Einheit (ein Feld kostet mind. 1 Bewegungspunkt, also
         ist nichts jenseits von speedCur Schritten erreichbar).
      2. Echte Erreichbarkeit pro Kandidat ueber die pathCost-Query (spieleigener
         Pathfinder): reachable UND cost <= speedCur.

    Begrenzt auf max_candidates, damit die Zahl der pathCost-Queries beschraenkt
    bleibt. Das aktuelle Feld ist immer dabei (Stehenbleiben ist gueltig).

    WICHTIG (hm/avoid_mode): Ohne Heatmap werden die Kandidaten rein nach Naehe
    gecappt. Das ist bei einem RUECKZUG fatal - die naechsten Felder liegen oft
    alle noch in der Gefahrenzone, und die wenigen Slots verdraengen die
    entfernteren SICHEREN Felder. Mit hm + avoid_mode werden daher SICHERE Felder
    (nicht-avoided fuer den Modus) im Cap BEVORZUGT, danach nach Naehe. So ist
    immer ein sicheres erreichbares Feld in den Kandidaten, falls eines existiert.
    """
    speed = unit_speed_cur(unit)
    x0, y0 = gs.pos(unit)
    here = (x0, y0)
    if speed <= 0:
        return [here]

    # 1. lokaler Kandidatenraum (begehbar, innerhalb speedCur Schritten Chebyshev)
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
        # Sichere Felder zuerst (0 = sicher), dann nach Naehe. So enthaelt der
        # Cap immer erreichbare sichere Felder, falls vorhanden.
        def _key(p):
            unsafe = 1 if hmc._field_avoided_for_mode(hm, p[0], p[1], avoid_mode) else 0
            return (unsafe, (p[0] - x0) ** 2 + (p[1] - y0) ** 2)
        raw.sort(key=_key)
    else:
        # nach Naehe sortieren (spart pathCost-Queries)
        raw.sort(key=lambda p: (p[0] - x0) ** 2 + (p[1] - y0) ** 2)
    raw = raw[:max_candidates]

    # 2. echte Erreichbarkeit via pathCost
    result = [here]
    for cand in raw:
        rep = path_cost_to(conn, unit["id"], cand)
        if not rep:
            continue
        if rep.get("reachable") and (rep.get("cost", 0) or 0) <= speed:
            result.append(cand)
    return result


# ---------------------------------------------------------------------------
# Rueckzug / Ausweichen: gemeinsamer Kern fuer die Meide-Modi
# ---------------------------------------------------------------------------
def _retreat(gs, conn, unit, hm, path_cost_to, avoid_mode,
             require_enemy_in_own_scan=False, safe_only=False):
    """Bewegt die Einheit auf das beste sichere/least-dangerous Feld (9.3).
    Gibt (moved: bool, target|None, reason) zurueck.

    avoid_mode: "max" (max(scan,attack)) oder "attack" (nur Beschuss).
    require_enemy_in_own_scan: nur fuer AktivesStalking.
    safe_only: kein Idee-B-Fallback (None wenn kein sicheres Feld) - fuer Stalking.
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
    """Liste [((ex,ey), own_scan_radius), ...] aller sichtbaren Feinde, jeweils
    gepaart mit der EIGENEN Scan-Reichweite der stalkenden Einheit. Fuer
    AktivesStalking (Feind in eigener Scan-Range halten)."""
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
# Die einzelnen Modi
# ---------------------------------------------------------------------------
def apply_mode(gs, conn, unit, mode, hm, path_cost_to, log=None):
    """Fuehrt den Modus fuer EINE Einheit aus. Gibt ein Ergebnis-dict zurueck:
        {"unit": id, "mode": mode, "action": <str>, "target": (x,y)|None,
         "hp_override": bool}
    'action' beschreibt knapp, was getan wurde (fuer Konsolen-/Testausgabe).
    """
    def _log(msg):
        if log:
            log(msg)

    uid = unit["id"]
    x, y = gs.pos(unit)
    hp_or = hp_override_active(unit)

    # --- HP-Override (9.4): unter Schwelle IMMER Rueckzug, auch im Konflikt ---
    if hp_or:
        moved, tgt, reason = _retreat(gs, conn, unit, hm, path_cost_to,
                                      avoid_mode="max")
        act = f"HP-Override Rueckzug -> {tgt}" if moved else f"HP-Override: {reason}"
        _log(f"[Mode {mode}] Einheit {uid} @ ({x},{y}) HP<25% -> {act}")
        return {"unit": uid, "mode": mode, "action": act, "target": tgt,
                "hp_override": True}

    # --- Modus-spezifisch ---------------------------------------------------
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
    """Meide NUR die Attack-Reichweite GEFAEHRLICHER Feinde (enemy_attack-Layer,
    der nur Einheiten mit canAttack enthaelt). Ungefaehrliche Feinde - gegnerische
    Surveyor/Baufahrzeuge (canAttack=0, keine Attack-Range) - werden IGNORIERT:
    eine Baueinheit soll wegen eines harmlosen vorbeifahrenden Surveyors nicht
    ihren Bau abbrechen. Steht die Einheit in feindlicher ATTACK-Reichweite:
    Bau SOFORT abbrechen (finishBuild loest vom Bau, Bau wird geopfert) und
    zurueckziehen. Sonst nichts tun (sie baut/steht sicher)."""
    uid = unit["id"]
    x, y = gs.pos(unit)
    cat = unit_target_category(gs, unit)
    if not in_enemy_attack_for(hm, x, y, cat):
        log(f"[PassivesBauen] Einheit {uid} @ ({x},{y}) ausser Beschussreichweite -> baut weiter")
        return _result(uid, PASSIVES_BAUEN, "sicher, baut weiter", None)

    # In Beschussreichweite: falls die Einheit gerade baut, vom Bau loesen (Bau opfern).
    if unit.get("isBuilding"):
        # Rueckzugsziel zuerst bestimmen (raus aus Attack-Reichweite), damit
        # finishBuild eine escapePosition hat.
        am = _attack_avoid_mode(gs, unit)
        cands = reachable_fields(gs, conn, unit, path_cost_to,
                                 hm=hm, avoid_mode=am)
        tgt = hmc.select_safe_target(hm, cands, avoid_mode=am)
        esc = tgt if tgt and tgt != (x, y) else None
        if esc is None:
            # kein sicheres Feld -> auf der Stelle vom Bau loesen, Nachbar als escape
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

    # Baut nicht (mehr) -> einfach raus aus der Attack-Reichweite zurueckziehen.
    moved, tgt, reason = _retreat(gs, conn, unit, hm, path_cost_to,
                                  _attack_avoid_mode(gs, unit))
    act = f"Rueckzug -> {tgt}" if moved else f"kein Rueckzug ({reason})"
    log(f"[PassivesBauen] Einheit {uid} @ ({x},{y}) BESCHUSSGEFAHR -> {act}")
    return _result(uid, PASSIVES_BAUEN, act, tgt)


def _mode_passive_aufklaerung(gs, conn, unit, hm, path_cost_to, log):
    """Meide max(scan,attack). In Gefahr: auf sicheres Feld. Sicher: Aufklaerung
    fortsetzen (hier: das Bewegungsziel ueberlaesst der Aufrufer/Scout-Logik;
    dieser Modus sorgt nur fuer das Ausweichen)."""
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
    """Meide max(scan,attack), aber halte den Feind in EIGENER Scan-Range (Ring).
    Nur wenn ein SICHERES Ring-Feld existiert wird gestalkt; sonst Fallback
    PassiveAufklaerung (sicher zurueckziehen)."""
    uid = unit["id"]
    x, y = gs.pos(unit)
    moved, tgt, reason = _retreat(gs, conn, unit, hm, path_cost_to, "max",
                                  require_enemy_in_own_scan=True, safe_only=True)
    if tgt is not None:
        act = f"Stalk-Reposition -> {tgt}" if moved else f"Stalk haelt Position ({reason})"
        log(f"[AktivesStalking] Einheit {uid} @ ({x},{y}) -> {act}")
        return _result(uid, AKTIVES_STALKING, act, tgt)
    # Kein sicheres Ring-Feld -> Fallback PassiveAufklaerung
    log(f"[AktivesStalking] Einheit {uid} @ ({x},{y}) kein sicheres Ring-Feld -> Fallback PassiveAufklaerung")
    res = _mode_passive_aufklaerung(gs, conn, unit, hm, path_cost_to, log)
    res["mode"] = AKTIVES_STALKING
    res["action"] = "Fallback PassiveAufklaerung: " + res["action"]
    return res


def _mode_robuste_aufklaerung(gs, conn, unit, hm, path_cost_to, log):
    """Meide nur Attack (enemy_attack), Sicht ignorieren. Konflikt mit anderen
    KAMPFeinheiten (Attack-Reichweite) wird vermieden - aber UNGEFAEHRLICHE Ziele
    (canAttack=0: gegnerische Surveyor/Bauer/waffenlose Gebaeude) in eigener
    Reichweite werden angegriffen (gefahrloser Abschuss)."""
    uid = unit["id"]
    x, y = gs.pos(unit)
    # Gelegenheitsabschuss: ungefaehrliches Ziel in eigener Reichweite angreifen.
    tgt_atk = _attack_target_in_range(gs, unit, only_harmless=True)
    if tgt_atk is not None:
        ok, _ = _do_attack(conn, unit, tgt_atk, log, "RobusteAufklaerung")
        if ok:
            return _result(uid, ROBUSTE_AUFKLAERUNG,
                           f"Angriff ungefaehrliches Ziel {tgt_atk['id']}", None)
        # Angriff abgelehnt (z.B. Ziel nicht treffbar) -> normal weiter.
    if not in_enemy_attack_for(hm, x, y, unit_target_category(gs, unit)):
        log(f"[RobusteAufklaerung] Einheit {uid} @ ({x},{y}) kein Beschuss -> Aufklaerung")
        return _result(uid, ROBUSTE_AUFKLAERUNG, "kein Beschuss, Aufklaerung weiter", None)
    moved, tgt, reason = _retreat(gs, conn, unit, hm, path_cost_to,
                                  _attack_avoid_mode(gs, unit))
    act = f"raus aus Beschuss -> {tgt}" if moved else f"kein Ausweichfeld ({reason})"
    log(f"[RobusteAufklaerung] Einheit {uid} @ ({x},{y}) BESCHUSS -> {act}")
    return _result(uid, ROBUSTE_AUFKLAERUNG, act, tgt)


def _mode_halten_defensiv(gs, conn, unit, hm, path_cost_to, log):
    """Bleibt auf Position. Weicht NUR bei akutem Beschuss aus (Feld in
    feindlicher Attack-Reichweite, enemy_attack-Layer), ignoriert reine Sicht.
    Nutzt enemy_attack (konsistent mit der Bruecken-Reichweitenkarte), nicht den
    gewichteten danger-Layer - so reagiert der Modus auch dann korrekt, wenn die
    Reichweiten von der Bruecke kommen."""
    uid = unit["id"]
    x, y = gs.pos(unit)
    # Gelegenheitsabschuss: ungefaehrliches Ziel in eigener Reichweite angreifen,
    # ohne die Position zu verlassen.
    tgt_atk = _attack_target_in_range(gs, unit, only_harmless=True)
    if tgt_atk is not None:
        ok, _ = _do_attack(conn, unit, tgt_atk, log, "HaltenDefensiv")
        if ok:
            return _result(uid, HALTEN_DEFENSIV,
                           f"Angriff ungefaehrliches Ziel {tgt_atk['id']}", None)
        # Angriff abgelehnt -> normal weiter (Position halten / ausweichen).
    if not in_enemy_attack_for(hm, x, y, unit_target_category(gs, unit)):
        log(f"[HaltenDefensiv] Einheit {uid} @ ({x},{y}) ruhig -> haelt Position")
        return _result(uid, HALTEN_DEFENSIV, "haelt Position", None)
    moved, tgt, reason = _retreat(gs, conn, unit, hm, path_cost_to,
                                  _attack_avoid_mode(gs, unit))
    act = f"unter Beschuss, weicht aus -> {tgt}" if moved else f"kein Ausweichfeld ({reason})"
    log(f"[HaltenDefensiv] Einheit {uid} @ ({x},{y}) BESCHUSS -> {act}")
    return _result(uid, HALTEN_DEFENSIV, act, tgt)


def _mode_konflikt(gs, conn, unit, hm, path_cost_to, log):
    """Ignoriert avoid/scan. Greift AKTIV an - JEDES Ziel, keine Unterscheidung
    (gefaehrlich oder nicht). Steht bereits ein Ziel in eigener Attack-Reichweite,
    wird angegriffen. Sonst sucht die Einheit ein Feld, von dem aus sie den Feind
    in eigener Range hat, moeglichst ausserhalb gegnerischer Attack-Range (ich
    feuere, Gegner noch nicht)."""
    uid = unit["id"]
    x, y = gs.pos(unit)
    own_range = unit_attack_range(unit)
    if own_range <= 0 or not can_attack(gs, unit):
        log(f"[Konflikt] Einheit {uid} @ ({x},{y}) keine Waffe -> nichts")
        return _result(uid, KONFLIKT, "keine Waffe", None)

    # 1. Steht ein Ziel (egal ob gefaehrlich) in eigener Reichweite -> angreifen.
    tgt_atk = _attack_target_in_range(gs, unit, only_harmless=False)
    if tgt_atk is not None:
        ok, _ = _do_attack(conn, unit, tgt_atk, log, "Konflikt")
        if ok:
            return _result(uid, KONFLIKT, f"Angriff Ziel {tgt_atk['id']}", None)
        # Angriff abgelehnt -> weiter zur Positionierung.

    enemies = _enemy_positions(gs)
    if not enemies:
        log(f"[Konflikt] Einheit {uid} @ ({x},{y}) kein Feind sichtbar -> haelt")
        return _result(uid, KONFLIKT, "kein Feind sichtbar", None)

    cands = reachable_fields(gs, conn, unit, path_cost_to)
    cat = unit_target_category(gs, unit)
    # ideale Felder: Feind in eigener Range, selbst ausserhalb gegnerischer Attack
    ideal = []
    in_range_only = []
    for (cx, cy) in cands:
        # naechster Feind von diesem Feld
        nearest = min(enemies, key=lambda e: (e[0]-cx)**2 + (e[1]-cy)**2)
        d2 = (nearest[0]-cx)**2 + (nearest[1]-cy)**2
        if d2 <= own_range * own_range:
            if not in_enemy_attack_for(hm, cx, cy, cat):
                ideal.append((cx, cy))
            else:
                in_range_only.append((cx, cy))
    target = None
    if ideal:
        # ideal: nahe ans Feuern, aber sicher. Niedrigster threat als Tie-Break.
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
    """Alle sichtbaren Feindeinheiten UND -gebaeude als Zielinfos:
    [{"id","pos","is_dangerous"}]. Gebaeude eingeschlossen (auch Geschuetztuerme).
    Neutrale werden NICHT als Ziele gefuehrt (kein Gegnerstatus).

    is_dangerous:
      - relative_to_category=None: hat der Feind ueberhaupt canAttack>0
        (altes Verhalten, kategorie-unabhaengig).
      - relative_to_category='air'/'ground': kann der Feind GENAU DIESE Kategorie
        treffen (also Gegenfeuer-Risiko fuer eine eigene Einheit dieser Kategorie).
        Eine Flak (canAttack=Air) ist fuer ein Bodenziel dann is_dangerous=False."""
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
                # Zielkategorie des FEINDES (was er IST) - fuer die Pruefung, ob
                # die eigene Waffe ihn ueberhaupt treffen kann.
                "target_category": unit_target_category(gs, u),
            })
    return out


def _attack_target_in_range(gs, unit, only_harmless):
    """Sucht ein angreifbares Feindziel in EIGENER Attack-Reichweite vom aktuellen
    Feld der Einheit. only_harmless=True -> nur Ziele, die die EIGENE Kategorie
    NICHT zurueckschiessen koennen (gefahrloser Abschuss, kein Gegenfeuer);
    False -> jedes Ziel. Gibt das naechste passende Ziel-dict oder None.
    (Geometrie: l2-Distanz <= eigene range, wie cRangeMap-Kreischeck.)"""
    own_range = unit_attack_range(unit)
    if own_range <= 0 or not can_attack(gs, unit):
        return None
    # "Ungefaehrlich" ist relativ zur Kategorie der eigenen Einheit: ein Ziel ist
    # harmlos, wenn es MEINE Kategorie nicht treffen kann.
    cat = unit_target_category(gs, unit)
    own_bits = _static(gs, unit).get("canAttack") or 0
    x, y = gs.pos(unit)
    best = None
    best_d2 = None
    for t in _enemy_targets(gs, relative_to_category=cat):
        if only_harmless and t["is_dangerous"]:
            continue
        # Nur Ziele angreifen, die die eigene Waffe ueberhaupt treffen kann
        # (z.B. kann eine Bodeneinheit ohne AreaSub-Bit kein getarntes U-Boot
        # treffen). Vermeidet von der Bruecke abgelehnte Leerangriffe.
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
    """Sendet den Angriff auf ein Ziel-dict. Die Bruecke fuehrt client->attack aus
    (Zielvalidierung/Reichweite prueft der Client). Gibt (ok, reason)."""
    ok, reason = conn.do({"type": "attack", "unitId": unit["id"],
                          "targetId": target["id"]})
    tag = "ungefaehrliches" if not target["is_dangerous"] else "Ziel"
    if ok:
        log(f"[{mode_label}] Einheit {unit['id']} greift {tag} Ziel "
            f"{target['id']} @ {target['pos']} an.")
    else:
        log(f"[{mode_label}] Angriff auf {target['id']} abgelehnt ({reason}).")
    return ok, reason
