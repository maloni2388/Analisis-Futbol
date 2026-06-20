"""
backend.py — Prototipo personal de análisis estadístico de fútbol
Inspirado en GalaxyStats (MLB), pero adaptado a fútbol vía football-data.org API v4.

USO PERSONAL / ANÁLISIS. No es asesoramiento financiero ni una herramienta de apuestas.

Soporta tanto ligas de clubes (con tabla de posiciones simple) como torneos de
selecciones nacionales (con fase de grupos + eliminación directa), por ejemplo
el Mundial 2026 (en curso) y la Eurocopa.

Endpoints:
  GET /api/competitions                        -> todas las competiciones soportadas
  GET /api/standings/<code>                     -> tabla de posiciones (o grupos, si aplica)
  GET /api/matches/<code>                       -> partidos recientes (terminados)
  GET /api/distribution/<code>                  -> distribución de resultados (1X2, over/under, BTTS)
  GET /api/live/<code>                          -> partidos en vivo / del día (LIVE, IN_PLAY, PAUSED)
  GET /api/upcoming/<code>                      -> próximos partidos programados
  GET /api/knockout/<code>                      -> bracket de eliminación directa (mundiales/euros)

  -- Extensiones (ver stats_extra.py) --
  GET /api/team-trends/<code>?team=&last_n=       -> tendencia de goles de un equipo (football-data.org)
  GET /api/apisports/status                        -> chequea si la key de api-sports.io está configurada
  GET /api/apisports/search-team?name=             -> busca ID de equipo en api-sports.io
  GET /api/apisports/search-league?name=           -> busca ID de liga en api-sports.io
  GET /api/match-stats/<fixture_id>                -> remates/corners/tarjetas de un partido (api-sports.io)
  GET /api/team-stats/<league_id>/<team_id>        -> stats agregadas de un equipo (api-sports.io)

Ejemplos:
  /api/standings/PL
  /api/standings/WC          -> standings por grupo (Mundial 2026)
  /api/matches/PL?limit=20
  /api/distribution/PL?last_n=50
  /api/live/WC
  /api/upcoming/WC?limit=10
  /api/knockout/WC
"""

import os
from collections import Counter
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

from stats_extra import extra_bp, APISPORTS_KEY
from qualifiers import qualifiers_bp

app = Flask(__name__)
CORS(app)  # permite que el frontend (otro puerto/origen) consuma la API libremente
app.register_blueprint(extra_bp)
app.register_blueprint(qualifiers_bp)

# ----------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------

API_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_TOKEN}

# Competiciones soportadas (código football-data.org -> metadata)
# "kind": "league" = liga de clubes con tabla simple
#         "cup_groups" = torneo de selecciones con fase de grupos + knockout
COMPETITIONS = {
    "PL":  {"name": "Premier League (Inglaterra)", "kind": "league"},
    "PD":  {"name": "La Liga (España)", "kind": "league"},
    "SA":  {"name": "Serie A (Italia)", "kind": "league"},
    "BL1": {"name": "Bundesliga (Alemania)", "kind": "league"},
    "FL1": {"name": "Ligue 1 (Francia)", "kind": "league"},
    "DED": {"name": "Eredivisie (Países Bajos)", "kind": "league"},
    "PPL": {"name": "Primeira Liga (Portugal)", "kind": "league"},
    "ELC": {"name": "Championship (Inglaterra, 2da div.)", "kind": "league"},
    "BSA": {"name": "Campeonato Brasileiro Série A", "kind": "league"},
    "CL":  {"name": "Champions League", "kind": "cup_groups"},
    "CLI": {"name": "Copa Libertadores", "kind": "cup_groups"},
    "WC":  {"name": "FIFA World Cup (Selecciones)", "kind": "cup_groups"},
    "EC":  {"name": "Eurocopa (Selecciones)", "kind": "cup_groups"},
}

# Status de partido considerados "en vivo" por la API
LIVE_STATUSES = {"LIVE", "IN_PLAY", "PAUSED"}

# Orden lógico de fases eliminatorias para mostrar el bracket ordenado
STAGE_ORDER = [
    "LAST_64", "LAST_32", "LAST_16",
    "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL",
]

# Cache simple en memoria para no gastar las 10 requests/min del plan free
_cache = {}
CACHE_TTL_SECONDS = 60
LIVE_CACHE_TTL_SECONDS = 15  # los partidos en vivo se refrescan más seguido


def _cache_get(key, ttl=CACHE_TTL_SECONDS):
    entry = _cache.get(key)
    if not entry:
        return None
    value, ts = entry
    if (datetime.now(timezone.utc) - ts).total_seconds() > ttl:
        return None
    return value


def _cache_set(key, value):
    _cache[key] = (value, datetime.now(timezone.utc))


# ----------------------------------------------------------------------------
# Helpers de acceso a la API externa
# ----------------------------------------------------------------------------

def fetch_from_api(path, params=None, ttl=CACHE_TTL_SECONDS):
    """Llama a football-data.org y devuelve (json, error)."""
    cache_key = f"{path}::{params}"
    cached = _cache_get(cache_key, ttl=ttl)
    if cached is not None:
        return cached, None

    url = f"{BASE_URL}{path}"
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
    except requests.RequestException as e:
        return None, f"Error de conexión con football-data.org: {e}"

    if resp.status_code == 429:
        return None, "Límite de requests excedido (plan free: 10/min). Esperá un momento."
    if resp.status_code == 403:
        return None, "Token inválido o sin permiso para este recurso."
    if resp.status_code == 404:
        return None, "Recurso no encontrado (revisá el código de competición)."
    if resp.status_code != 200:
        return None, f"Error inesperado de la API: {resp.status_code} - {resp.text[:200]}"

    data = resp.json()
    _cache_set(cache_key, data)
    return data, None


def validate_competition(code):
    code = code.upper()
    if code not in COMPETITIONS:
        valid = ", ".join(COMPETITIONS.keys())
        return None, jsonify({
            "error": f"Competición '{code}' no soportada. Usá una de: {valid}"
        }), 400
    return code, None, None


def get_active_season_year(code):
    """
    football-data.org devuelve por default la temporada 'actual' del calendario,
    que puede no tener partidos jugados todavía (ej: en el receso de verano,
    o un Mundial/Eurocopa que todavía no arrancó). Esta función encuentra la
    temporada más reciente que SÍ tiene al menos un partido jugado o en curso.
    """
    data, err = fetch_from_api(f"/competitions/{code}")
    if err:
        return None, err

    seasons = data.get("seasons", [])
    if not seasons:
        return None, "La competición no tiene temporadas registradas."

    # seasons viene ordenado del más reciente al más viejo
    for season in seasons:
        start = season["startDate"][:4]
        check, err = fetch_from_api(
            f"/competitions/{code}/matches",
            params={"season": start, "status": "FINISHED"},
        )
        if err:
            continue
        if check.get("resultSet", {}).get("played", 0) > 0:
            return start, None
        # Si no hay finalizados, puede que el torneo esté EN CURSO ahora mismo
        # (ej: Mundial recién arrancando: 0 finalizados pero hay partidos LIVE).
        # OJO: no alcanza con que existan partidos programados (eso pasa
        # también con la temporada futura de cualquier liga en pretemporada);
        # tiene que haber al menos uno con status realmente en vivo.
        check_live, err2 = fetch_from_api(
            f"/competitions/{code}/matches",
            params={"season": start, "status": "LIVE,IN_PLAY,PAUSED"},
        )
        if not err2 and check_live.get("matches"):
            return start, None

    return None, "No se encontró ninguna temporada con partidos disponibles."


def serialize_match(m):
    return {
        "id": m["id"],
        "date": m["utcDate"],
        "status": m["status"],
        "stage": m.get("stage"),
        "group": m.get("group"),
        "matchday": m.get("matchday"),
        "homeTeam": m["homeTeam"]["name"],
        "awayTeam": m["awayTeam"]["name"],
        "homeCrest": m["homeTeam"].get("crest"),
        "awayCrest": m["awayTeam"].get("crest"),
        "homeGoals": m["score"]["fullTime"]["home"],
        "awayGoals": m["score"]["fullTime"]["away"],
        "homeGoalsHT": m["score"]["halfTime"]["home"],
        "awayGoalsHT": m["score"]["halfTime"]["away"],
        "winner": m["score"]["winner"],  # HOME_TEAM / AWAY_TEAM / DRAW / null
        "minute": m.get("minute"),  # solo presente en partidos en vivo
    }


# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------

@app.route("/api/competitions", methods=["GET"])
def list_competitions():
    leagues = {k: v["name"] for k, v in COMPETITIONS.items() if v["kind"] == "league"}
    national = {k: v["name"] for k, v in COMPETITIONS.items() if v["kind"] == "cup_groups"}
    return jsonify({
        "leagues_de_clubes": leagues,
        "selecciones_y_copas": national,
    })


@app.route("/api/standings/<code>", methods=["GET"])
def standings(code):
    code, err_resp, status = validate_competition(code)
    if err_resp:
        return err_resp, status

    kind = COMPETITIONS[code]["kind"]
    season = request.args.get("season")

    # Particularidad de la API: para torneos con fase de grupos (cup_groups),
    # pasar ?season=YYYY explícito hace que la API devuelva una tabla combinada
    # (TOTAL/HOME/AWAY) en vez de separar por grupo. Por eso acá NO resolvemos
    # ni mandamos season salvo que el usuario la pida a propósito.
    if kind == "league" and not season:
        season, err = get_active_season_year(code)
        if err:
            return jsonify({"error": err}), 502

    params = {"season": season} if season else None
    data, err = fetch_from_api(f"/competitions/{code}/standings", params=params)
    if err:
        return jsonify({"error": err}), 502

    raw_standings = data.get("standings", [])

    def serialize_table(table):
        return [
            {
                "position": row["position"],
                "team": row["team"]["name"],
                "shortName": row["team"].get("shortName") or row["team"]["name"],
                "crest": row["team"].get("crest"),
                "played": row["playedGames"],
                "won": row["won"],
                "draw": row["draw"],
                "lost": row["lost"],
                "points": row["points"],
                "goalsFor": row["goalsFor"],
                "goalsAgainst": row["goalsAgainst"],
                "goalDifference": row["goalDifference"],
                "form": row.get("form"),
            }
            for row in table
        ]

    if kind == "league":
        table = raw_standings[0]["table"] if raw_standings else []
        result = {
            "competition": COMPETITIONS[code]["name"],
            "kind": kind,
            "season": data.get("season", {}),
            "table": serialize_table(table),
        }
    else:
        # cup_groups: puede haber varios grupos (Mundial: A, B, C...) o ninguno
        # todavía (si el torneo está 100% en fase de eliminación directa).
        groups = []
        for grp in raw_standings:
            if grp.get("type") != "TOTAL" or not grp.get("group"):
                continue
            groups.append({
                "group": grp.get("group"),
                "table": serialize_table(grp["table"]),
            })
        result = {
            "competition": COMPETITIONS[code]["name"],
            "kind": kind,
            "season": data.get("season", {}),
            "groups": groups,
        }

    return jsonify(result)


@app.route("/api/matches/<code>", methods=["GET"])
def matches(code):
    code, err_resp, status = validate_competition(code)
    if err_resp:
        return err_resp, status

    season = request.args.get("season")
    limit = int(request.args.get("limit", 20))
    stage_filter = request.args.get("stage")  # ej: GROUP_STAGE, FINAL, etc.
    group_filter = request.args.get("group")  # ej: GROUP_A

    if not season:
        season, err = get_active_season_year(code)
        if err:
            return jsonify({"error": err}), 502

    data, err = fetch_from_api(
        f"/competitions/{code}/matches",
        params={"season": season, "status": "FINISHED"},
    )
    if err:
        return jsonify({"error": err}), 502

    raw_matches = data.get("matches", [])
    if stage_filter:
        raw_matches = [m for m in raw_matches if m.get("stage") == stage_filter.upper()]
    if group_filter:
        raw_matches = [m for m in raw_matches if m.get("group") == group_filter.upper()]

    raw_matches.sort(key=lambda m: m["utcDate"], reverse=True)
    raw_matches = raw_matches[:limit]

    result = {
        "competition": COMPETITIONS[code]["name"],
        "count": len(raw_matches),
        "matches": [serialize_match(m) for m in raw_matches],
    }
    return jsonify(result)


@app.route("/api/live/<code>", methods=["GET"])
def live_matches(code):
    """Partidos en vivo o programados para hoy. Útil durante un Mundial/Eurocopa."""
    code, err_resp, status = validate_competition(code)
    if err_resp:
        return err_resp, status

    season = request.args.get("season")
    if not season:
        season, err = get_active_season_year(code)
        if err:
            return jsonify({"error": err}), 502

    # Traemos todos los partidos de la temporada y filtramos localmente
    # por status en vivo, para tener cache corto y no golpear el rate limit.
    data, err = fetch_from_api(
        f"/competitions/{code}/matches",
        params={"season": season},
        ttl=LIVE_CACHE_TTL_SECONDS,
    )
    if err:
        return jsonify({"error": err}), 502

    raw_matches = data.get("matches", [])
    live = [m for m in raw_matches if m["status"] in LIVE_STATUSES]
    live.sort(key=lambda m: m["utcDate"])

    today = datetime.now(timezone.utc).date().isoformat()
    excluded_statuses = {"FINISHED"} | LIVE_STATUSES
    today_matches = [
        m for m in raw_matches
        if m["utcDate"][:10] == today and m["status"] not in excluded_statuses
    ]
    today_matches.sort(key=lambda m: m["utcDate"])

    result = {
        "competition": COMPETITIONS[code]["name"],
        "live_count": len(live),
        "live": [serialize_match(m) for m in live],
        "today_scheduled_count": len(today_matches),
        "today_scheduled": [serialize_match(m) for m in today_matches],
    }
    return jsonify(result)


@app.route("/api/upcoming/<code>", methods=["GET"])
def upcoming_matches(code):
    code, err_resp, status = validate_competition(code)
    if err_resp:
        return err_resp, status

    season = request.args.get("season")
    limit = int(request.args.get("limit", 10))

    if not season:
        season, err = get_active_season_year(code)
        if err:
            return jsonify({"error": err}), 502

    data, err = fetch_from_api(
        f"/competitions/{code}/matches",
        params={"season": season, "status": "SCHEDULED,TIMED"},
    )
    if err:
        return jsonify({"error": err}), 502

    raw_matches = data.get("matches", [])
    raw_matches.sort(key=lambda m: m["utcDate"])
    raw_matches = raw_matches[:limit]

    result = {
        "competition": COMPETITIONS[code]["name"],
        "count": len(raw_matches),
        "matches": [serialize_match(m) for m in raw_matches],
    }
    return jsonify(result)


@app.route("/api/knockout/<code>", methods=["GET"])
def knockout_bracket(code):
    """
    Bracket de eliminación directa (octavos, cuartos, semis, final), pensado
    para Mundiales/Eurocopas. En una liga de clubes normal devuelve vacío.
    """
    code, err_resp, status = validate_competition(code)
    if err_resp:
        return err_resp, status

    if COMPETITIONS[code]["kind"] != "cup_groups":
        return jsonify({
            "error": f"'{code}' es una liga de clubes sin fase de eliminación directa estándar."
        }), 400

    season = request.args.get("season")
    if not season:
        season, err = get_active_season_year(code)
        if err:
            return jsonify({"error": err}), 502

    data, err = fetch_from_api(
        f"/competitions/{code}/matches",
        params={"season": season},
    )
    if err:
        return jsonify({"error": err}), 502

    raw_matches = data.get("matches", [])
    knockout_matches = [m for m in raw_matches if m.get("stage") in STAGE_ORDER]

    bracket = {}
    for m in knockout_matches:
        bracket.setdefault(m["stage"], []).append(serialize_match(m))

    for stage in bracket:
        bracket[stage].sort(key=lambda m: m["date"])

    ordered_bracket = [
        {"stage": stage, "matches": bracket[stage]}
        for stage in STAGE_ORDER
        if stage in bracket
    ]

    result = {
        "competition": COMPETITIONS[code]["name"],
        "stages": ordered_bracket,
    }
    return jsonify(result)


@app.route("/api/distribution/<code>", methods=["GET"])
def distribution(code):
    """
    Análisis de distribución de resultados sobre los últimos N partidos:
    - 1X2 (local / empate / visitante)
    - Over/Under 2.5 goles
    - BTTS (ambos equipos anotan)
    - Promedio de goles por partido
    """
    code, err_resp, status = validate_competition(code)
    if err_resp:
        return err_resp, status

    season = request.args.get("season")
    last_n = int(request.args.get("last_n", 50))
    stage_filter = request.args.get("stage")
    group_filter = request.args.get("group")

    if not season:
        season, err = get_active_season_year(code)
        if err:
            return jsonify({"error": err}), 502

    data, err = fetch_from_api(
        f"/competitions/{code}/matches",
        params={"season": season, "status": "FINISHED"},
    )
    if err:
        return jsonify({"error": err}), 502

    raw_matches = data.get("matches", [])
    if stage_filter:
        raw_matches = [m for m in raw_matches if m.get("stage") == stage_filter.upper()]
    if group_filter:
        raw_matches = [m for m in raw_matches if m.get("group") == group_filter.upper()]

    raw_matches.sort(key=lambda m: m["utcDate"], reverse=True)
    raw_matches = raw_matches[:last_n]

    if not raw_matches:
        return jsonify({"error": "No hay partidos finalizados para este filtro."}), 404

    winner_counter = Counter()
    total_goals = 0
    over_25 = 0
    btts = 0

    for m in raw_matches:
        home = m["score"]["fullTime"]["home"]
        away = m["score"]["fullTime"]["away"]
        winner_counter[m["score"]["winner"]] += 1

        goals = home + away
        total_goals += goals
        if goals > 2.5:
            over_25 += 1
        if home > 0 and away > 0:
            btts += 1

    n = len(raw_matches)
    result = {
        "competition": COMPETITIONS[code]["name"],
        "sample_size": n,
        "result_distribution": {
            "home_win": winner_counter.get("HOME_TEAM", 0),
            "draw": winner_counter.get("DRAW", 0),
            "away_win": winner_counter.get("AWAY_TEAM", 0),
            "home_win_pct": round(winner_counter.get("HOME_TEAM", 0) / n * 100, 1),
            "draw_pct": round(winner_counter.get("DRAW", 0) / n * 100, 1),
            "away_win_pct": round(winner_counter.get("AWAY_TEAM", 0) / n * 100, 1),
        },
        "goals": {
            "avg_goals_per_match": round(total_goals / n, 2),
            "over_2_5_count": over_25,
            "over_2_5_pct": round(over_25 / n * 100, 1),
            "under_2_5_pct": round((n - over_25) / n * 100, 1),
        },
        "btts": {
            "btts_count": btts,
            "btts_pct": round(btts / n * 100, 1),
        },
    }
    return jsonify(result)


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "ok",
        "message": "Backend de análisis de fútbol corriendo (clubes + selecciones nacionales).",
        "endpoints": [
            "/api/competitions",
            "/api/standings/<code>",
            "/api/matches/<code>?limit=20&stage=&group=",
            "/api/distribution/<code>?last_n=50&stage=&group=",
            "/api/live/<code>",
            "/api/upcoming/<code>?limit=10",
            "/api/knockout/<code>",
            "/api/team-trends/<code>?team=&last_n=",
            "/api/head-to-head/<code>?team1=&team2=&last_n=",
            "/api/apisports/status",
            "/api/apisports/search-team?name=",
            "/api/apisports/search-league?name=",
            "/api/match-stats/<fixture_id>",
            "/api/team-stats/<league_id>/<team_id>?season=",
            "/api/team-match-stats-avg/<league_id>/<team_id>?last_n=",
            "/api/qualifiers/confederations",
            "/api/qualifiers/standings/<confed>",
            "/api/qualifiers/matches/<confed>?limit=20",
            "/api/friendlies?team=&limit=20",
            "/api/friendlies/upcoming?team=&limit=20",
        ],
    })


if __name__ == "__main__":
    if not API_TOKEN:
        print(
            "AVISO: falta la variable de entorno FOOTBALL_DATA_TOKEN. "
            "Los endpoints de ligas/Mundial/Eurocopa van a fallar hasta que la configures."
        )
    if not APISPORTS_KEY:
        print(
            "AVISO: falta la variable de entorno APISPORTS_KEY. "
            "Los endpoints de remates/corners/tarjetas y eliminatorias van a fallar hasta que la configures."
        )
    # Render (y la mayoría de los hosts gratuitos) inyectan el puerto real
    # en la variable de entorno PORT — si no está seteada, usamos 5000 para
    # seguir pudiendo correrlo en local como antes.
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
