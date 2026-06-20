"""
qualifiers.py — Eliminatorias mundialistas por confederación + amistosos
internacionales, vía API-Football (api-sports.io). football-data.org NO
cubre esto (solo tiene el torneo final del Mundial/Eurocopa), por eso esta
parte usa exclusivamente api-sports.io. Requiere APISPORTS_KEY configurada
(ver stats_extra.py).

ALCANCE: SOLO fútbol masculino, SOLO selección absoluta/adulta.
- Los league_id de CONFEDERATIONS abajo corresponden específicamente a las
  ligas "World Cup - Qualification <Confederación>" (sin "Women" en el
  nombre) — api-sports.io tiene IDs completamente separados para las
  versiones femeninas (ej: id 880 = "World Cup - Women - Qualification
  Europe"), así que no hay riesgo de mezcla ahí.
- Los amistosos (/api/friendlies) sí vienen mezclados en una sola liga
  ("Friendlies", id 10) con categorías juveniles, femenino y combinados
  olímpicos, así que se filtran activamente por nombre de equipo (ver
  is_senior_men) antes de devolver cualquier resultado. Este filtro es
  incondicional, no hay parámetro para desactivarlo.

Particularidad importante de esta API: cada confederación etiqueta su
eliminatoria con un "season" (año) distinto, porque cada una arrancó y
terminó sus partidos en ventanas de tiempo distintas:
  - Sudamérica, Asia, CONCACAF, Oceanía: temporada "2026" (terminaron más
    cerca del propio Mundial 2026)
  - Europa: temporada "2024" (la fase de grupos UEFA se jugó en 2024-2025)
  - África: temporada "2023" (arrancó y se definió antes)
En vez de hardcodear esto (puede no aplicar a futuros ciclos, ej. 2030),
resolvemos la temporada activa dinámicamente probando candidatos, igual
que hacemos con football-data.org en backend.py.

Endpoints:
  GET /api/qualifiers/confederations                  -> lista de confederaciones soportadas
  GET /api/qualifiers/standings/<confed>               -> tabla(s) de la eliminatoria
  GET /api/qualifiers/matches/<confed>?limit=20         -> partidos recientes de la eliminatoria
  GET /api/friendlies?limit=20&team=                    -> amistosos internacionales recientes (solo masculino adulto)
  GET /api/friendlies/upcoming?limit=20&team=           -> próximos amistosos programados (solo masculino adulto)

Ejemplos:
  /api/qualifiers/standings/south_america
  /api/qualifiers/matches/europe?limit=10
  /api/friendlies?team=Argentina&limit=10
"""

from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from stats_extra import fetch_apisports, is_senior_men

qualifiers_bp = Blueprint("qualifiers", __name__)

# IDs de liga en api-sports.io para cada eliminatoria mundialista por
# confederación, + temporadas candidatas a probar en orden (la más probable
# primero, para minimizar requests).
CONFEDERATIONS = {
    "europe": {
        "league_id": 32,
        "name": "Eliminatoria mundialista — UEFA (Europa)",
        "season_candidates": [2024, 2025, 2023, 2026],
    },
    "south_america": {
        "league_id": 34,
        "name": "Eliminatoria mundialista — CONMEBOL (Sudamérica)",
        "season_candidates": [2026, 2025, 2024, 2023],
    },
    "africa": {
        "league_id": 29,
        "name": "Eliminatoria mundialista — CAF (África)",
        "season_candidates": [2023, 2024, 2025, 2026],
    },
    "asia": {
        "league_id": 30,
        "name": "Eliminatoria mundialista — AFC (Asia)",
        "season_candidates": [2026, 2025, 2024, 2023],
    },
    "concacaf": {
        "league_id": 31,
        "name": "Eliminatoria mundialista — CONCACAF (Norte/Centroamérica y Caribe)",
        "season_candidates": [2026, 2025, 2024, 2023],
    },
    "oceania": {
        "league_id": 33,
        "name": "Eliminatoria mundialista — OFC (Oceanía)",
        "season_candidates": [2026, 2025, 2024, 2023],
    },
}

FRIENDLIES_LEAGUE_ID = 10  # "Friendlies" (selecciones mayores) en api-sports.io

_season_cache = {}  # confed -> season resuelta, para no re-probar en cada request


def _resolve_confed_season(confed_key):
    """
    Encuentra la temporada que realmente tiene datos para esta confederación,
    probando los candidatos en orden. Cachea el resultado en memoria del
    proceso (no expira: la temporada de un ciclo de eliminatorias ya jugado
    no cambia retroactivamente).
    """
    if confed_key in _season_cache:
        return _season_cache[confed_key], None

    meta = CONFEDERATIONS[confed_key]
    for season in meta["season_candidates"]:
        data, err = fetch_apisports(
            "/standings", params={"league": meta["league_id"], "season": season}
        )
        if err:
            continue
        if data.get("results", 0) > 0:
            _season_cache[confed_key] = season
            return season, None

        # Algunas confederaciones (ej: Oceanía) no exponen standings con
        # esta estructura de grupos, pero sí tienen fixtures. Confirmamos
        # con fixtures antes de descartar la temporada.
        fx_data, fx_err = fetch_apisports(
            "/fixtures", params={"league": meta["league_id"], "season": season}
        )
        if not fx_err and fx_data.get("results", 0) > 0:
            _season_cache[confed_key] = season
            return season, None

    return None, (
        f"No se encontró una temporada con datos para la eliminatoria de "
        f"{meta['name']}. Puede que api-sports.io todavía no haya cargado "
        f"esta eliminatoria para el ciclo actual."
    )


def _validate_confed(confed_key):
    confed_key = confed_key.lower().replace("-", "_").replace(" ", "_")
    if confed_key not in CONFEDERATIONS:
        valid = ", ".join(CONFEDERATIONS.keys())
        return None, jsonify({
            "error": f"Confederación '{confed_key}' no reconocida. Usá una de: {valid}"
        }), 400
    return confed_key, None, None


@qualifiers_bp.route("/api/qualifiers/confederations", methods=["GET"])
def list_confederations():
    return jsonify({
        key: {"name": meta["name"], "league_id": meta["league_id"]}
        for key, meta in CONFEDERATIONS.items()
    })


@qualifiers_bp.route("/api/qualifiers/standings/<confed>", methods=["GET"])
def qualifiers_standings(confed):
    """
    Tabla de posiciones de la eliminatoria mundialista de una confederación.
    Puede venir en un solo grupo (ej: Sudamérica, que juega todos contra
    todos) o en varios grupos (ej: algunas eliminatorias asiáticas/africanas
    por zonas). Devolvemos siempre una lista de grupos para ser consistentes,
    aunque tenga uno solo.
    """
    confed, err_resp, status = _validate_confed(confed)
    if err_resp:
        return err_resp, status

    season = request.args.get("season")
    if not season:
        season, err = _resolve_confed_season(confed)
        if err:
            return jsonify({"error": err}), 502
    else:
        season = int(season)

    meta = CONFEDERATIONS[confed]
    data, err = fetch_apisports(
        "/standings", params={"league": meta["league_id"], "season": season}
    )
    if err:
        return jsonify({"error": err}), 502

    response = data.get("response", [])
    if not response:
        return jsonify({
            "error": (
                f"Esta confederación no tiene tabla de posiciones en formato "
                f"de grupos para la temporada {season} (puede que se dispute "
                f"como liguilla sin standings publicados — probá "
                f"/api/qualifiers/matches/{confed} para ver los partidos)."
            )
        }), 404

    raw_groups = response[0]["league"].get("standings", [])

    def serialize_row(row):
        return {
            "position": row["rank"],
            "team": row["team"]["name"],
            "crest": row["team"].get("logo"),
            "played": row["all"]["played"],
            "won": row["all"]["win"],
            "draw": row["all"]["draw"],
            "lost": row["all"]["lose"],
            "goalsFor": row["all"]["goals"]["for"],
            "goalsAgainst": row["all"]["goals"]["against"],
            "goalDifference": row["goalsDiff"],
            "points": row["points"],
            "form": row.get("form"),
            "note": row.get("description"),
        }

    groups = [
        {
            "group": grp[0].get("group") if grp else None,
            "table": [serialize_row(r) for r in grp],
        }
        for grp in raw_groups
    ]

    return jsonify({
        "confederation": meta["name"],
        "season": season,
        "groups": groups,
    })


@qualifiers_bp.route("/api/qualifiers/matches/<confed>", methods=["GET"])
def qualifiers_matches(confed):
    """Partidos finalizados recientes de la eliminatoria de una confederación."""
    confed, err_resp, status = _validate_confed(confed)
    if err_resp:
        return err_resp, status

    season = request.args.get("season")
    limit = int(request.args.get("limit", 20))

    if not season:
        season, err = _resolve_confed_season(confed)
        if err:
            return jsonify({"error": err}), 502
    else:
        season = int(season)

    meta = CONFEDERATIONS[confed]
    data, err = fetch_apisports(
        "/fixtures",
        params={"league": meta["league_id"], "season": season, "status": "FT"},
    )
    if err:
        return jsonify({"error": err}), 502

    fixtures = data.get("response", [])
    fixtures.sort(key=lambda f: f["fixture"]["date"], reverse=True)
    fixtures = fixtures[:limit]

    matches = [
        {
            "id": f["fixture"]["id"],
            "date": f["fixture"]["date"],
            "round": f["league"].get("round"),
            "homeTeam": f["teams"]["home"]["name"],
            "awayTeam": f["teams"]["away"]["name"],
            "homeCrest": f["teams"]["home"].get("logo"),
            "awayCrest": f["teams"]["away"].get("logo"),
            "homeGoals": f["goals"]["home"],
            "awayGoals": f["goals"]["away"],
            "homeGoalsHT": f["score"]["halftime"]["home"],
            "awayGoalsHT": f["score"]["halftime"]["away"],
        }
        for f in fixtures
    ]

    return jsonify({
        "confederation": meta["name"],
        "season": season,
        "count": len(matches),
        "matches": matches,
    })


def _serialize_friendly(f):
    return {
        "id": f["fixture"]["id"],
        "date": f["fixture"]["date"],
        "status": f["fixture"]["status"]["short"],
        "homeTeam": f["teams"]["home"]["name"],
        "awayTeam": f["teams"]["away"]["name"],
        "homeCrest": f["teams"]["home"].get("logo"),
        "awayCrest": f["teams"]["away"].get("logo"),
        "homeGoals": f["goals"]["home"],
        "awayGoals": f["goals"]["away"],
    }


@qualifiers_bp.route("/api/friendlies", methods=["GET"])
def friendlies_recent():
    """
    Amistosos internacionales recientes (finalizados), SIEMPRE filtrados a
    selección absoluta masculina — se excluyen categorías juveniles,
    femenino y combinados olímpicos sin excepción.
    Filtrá por equipo con ?team=Argentina.
    """
    limit = int(request.args.get("limit", 20))
    team_filter = request.args.get("team", "").strip().lower()
    season = request.args.get("season", str(datetime.now(timezone.utc).year))

    data, err = fetch_apisports(
        "/fixtures",
        params={"league": FRIENDLIES_LEAGUE_ID, "season": season, "status": "FT"},
    )
    if err:
        return jsonify({"error": err}), 502

    fixtures = data.get("response", [])
    fixtures.sort(key=lambda f: f["fixture"]["date"], reverse=True)

    fixtures = [
        f for f in fixtures
        if is_senior_men(f["teams"]["home"]["name"]) and is_senior_men(f["teams"]["away"]["name"])
    ]
    matches = [_serialize_friendly(f) for f in fixtures]
    if team_filter:
        matches = [
            m for m in matches
            if team_filter in m["homeTeam"].lower() or team_filter in m["awayTeam"].lower()
        ]

    matches = matches[:limit]

    return jsonify({
        "season": season,
        "count": len(matches),
        "matches": matches,
    })


@qualifiers_bp.route("/api/friendlies/upcoming", methods=["GET"])
def friendlies_upcoming():
    """
    Próximos amistosos internacionales programados, SIEMPRE filtrados a
    selección absoluta masculina (mismo criterio que /api/friendlies).
    """
    limit = int(request.args.get("limit", 20))
    team_filter = request.args.get("team", "").strip().lower()
    season = request.args.get("season", str(datetime.now(timezone.utc).year))

    data, err = fetch_apisports(
        "/fixtures",
        params={"league": FRIENDLIES_LEAGUE_ID, "season": season, "status": "NS-TBD"},
    )
    if err:
        return jsonify({"error": err}), 502

    fixtures = data.get("response", [])
    fixtures.sort(key=lambda f: f["fixture"]["date"])

    fixtures = [
        f for f in fixtures
        if is_senior_men(f["teams"]["home"]["name"]) and is_senior_men(f["teams"]["away"]["name"])
    ]
    matches = [_serialize_friendly(f) for f in fixtures]
    if team_filter:
        matches = [
            m for m in matches
            if team_filter in m["homeTeam"].lower() or team_filter in m["awayTeam"].lower()
        ]

    matches = matches[:limit]

    return jsonify({
        "season": season,
        "count": len(matches),
        "matches": matches,
    })
