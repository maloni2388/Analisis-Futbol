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


# ----------------------------------------------------------------------------
# The Odds API — cuotas en tiempo real de +70 casas de apuestas
# Requiere variable de entorno ODDS_API_KEY (gratis en the-odds-api.com)
# ----------------------------------------------------------------------------

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE_URL = "https://api.the-odds-api.com/v4"

# Mapeo de códigos internos a sport keys de The Odds API
ODDS_SPORT_MAP = {
    "WC":  "soccer_fifa_world_cup",
    "EC":  "soccer_uefa_euro",
    "CL":  "soccer_uefa_champs_league",
    "PL":  "soccer_epl",
    "PD":  "soccer_spain_la_liga",
    "SA":  "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
}

# Mercados soportados por The Odds API que mapeamos a nombres legibles
ODDS_MARKETS = {
    "h2h":       "Resultado 1X2",
    "totals":    "Over/Under goles",
    "btts":      "Ambos equipos anotan",
}


@app.route("/api/odds/<code>", methods=["GET"])
def get_odds(code):
    """
    Devuelve cuotas en tiempo real para los próximos partidos de una competición.
    Requiere ODDS_API_KEY configurada (gratis en the-odds-api.com, 500 req/mes).

    Parámetros opcionales:
      ?markets=h2h,totals,btts   (default: h2h,totals,btts)
      ?bookmakers=draftkings,betmgm  (default: todos)
      ?team=Arsenal              (filtra por equipo)
    """
    if not ODDS_API_KEY:
        return jsonify({
            "error": "Falta configurar ODDS_API_KEY. Registrate gratis en the-odds-api.com y agregá la variable de entorno.",
            "url": "https://the-odds-api.com"
        }), 503

    code = code.upper()
    sport_key = ODDS_SPORT_MAP.get(code)
    if not sport_key:
        supported = ", ".join(ODDS_SPORT_MAP.keys())
        return jsonify({
            "error": f"Competición '{code}' no soportada para cuotas. Soportadas: {supported}"
        }), 400

    markets = request.args.get("markets", "h2h,totals")
    bookmakers = request.args.get("bookmakers", "")
    team_filter = request.args.get("team", "").lower().strip()

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us,eu,uk",
        "markets": markets,
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    if bookmakers:
        params["bookmakers"] = bookmakers

    cache_key = f"odds::{sport_key}::{markets}"
    cached = _cache_get(cache_key, ttl=120)  # cache 2 min para cuotas
    if cached is not None:
        data = cached
    else:
        try:
            resp = requests.get(
                f"{ODDS_BASE_URL}/sports/{sport_key}/odds",
                params=params,
                timeout=10
            )
            if resp.status_code == 401:
                return jsonify({"error": "ODDS_API_KEY inválida."}), 401
            if resp.status_code == 422:
                return jsonify({"error": "Parámetros inválidos para The Odds API."}), 422
            if resp.status_code != 200:
                return jsonify({"error": f"Error de The Odds API: {resp.status_code}"}), 502
            data = resp.json()
            _cache_set(cache_key, data)
        except requests.RequestException as e:
            return jsonify({"error": f"Error de conexión con The Odds API: {e}"}), 502

    # Filtrar por equipo si se pidió
    if team_filter:
        data = [
            ev for ev in data
            if team_filter in ev.get("home_team", "").lower()
            or team_filter in ev.get("away_team", "").lower()
        ]

    # Serializar y simplificar la respuesta
    events = []
    for ev in data:
        bookmaker_odds = []
        for bk in ev.get("bookmakers", []):
            markets_data = {}
            for mkt in bk.get("markets", []):
                key = mkt["key"]
                outcomes = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                markets_data[key] = {
                    "label": ODDS_MARKETS.get(key, key),
                    "outcomes": outcomes
                }
            bookmaker_odds.append({
                "bookmaker": bk["title"],
                "markets": markets_data
            })

        # Calcular el promedio de cuotas entre todas las casas (consensus odds)
        consensus = {}
        for mkt_key in ["h2h", "totals"]:
            all_outcomes = {}
            count = 0
            for bk in bookmaker_odds:
                mkt = bk["markets"].get(mkt_key, {})
                for outcome, price in mkt.get("outcomes", {}).items():
                    all_outcomes.setdefault(outcome, []).append(price)
                    count += 1
            if all_outcomes:
                consensus[mkt_key] = {
                    "label": ODDS_MARKETS.get(mkt_key, mkt_key),
                    "outcomes": {
                        k: round(sum(v) / len(v))
                        for k, v in all_outcomes.items()
                    }
                }

        events.append({
            "match": f"{ev['home_team']} vs {ev['away_team']}",
            "home_team": ev["home_team"],
            "away_team": ev["away_team"],
            "commence_time": ev.get("commence_time"),
            "sport": ev.get("sport_title"),
            "consensus_odds": consensus,
            "bookmakers": bookmaker_odds,
            "bookmakers_count": len(bookmaker_odds),
        })

    # Ordenar por fecha
    events.sort(key=lambda e: e.get("commence_time") or "")

    return jsonify({
        "competition": ODDS_SPORT_MAP.get(code, code),
        "matches_found": len(events),
        "team_filter": team_filter or None,
        "events": events,
        "note": "Cuotas en formato americano. Consenso = promedio entre todas las casas disponibles."
    })


@app.route("/api/odds/status", methods=["GET"])
def odds_status():
    """Verifica si The Odds API está configurada y devuelve los créditos restantes."""
    if not ODDS_API_KEY:
        return jsonify({
            "configured": False,
            "message": "Falta ODDS_API_KEY. Registrate gratis en the-odds-api.com"
        })
    try:
        resp = requests.get(
            f"{ODDS_BASE_URL}/sports",
            params={"apiKey": ODDS_API_KEY},
            timeout=8
        )
        remaining = resp.headers.get("x-requests-remaining", "?")
        used = resp.headers.get("x-requests-used", "?")
        if resp.status_code == 200:
            return jsonify({
                "configured": True,
                "requests_remaining": remaining,
                "requests_used": used,
                "message": f"The Odds API conectada. Requests restantes: {remaining}/500"
            })
        return jsonify({
            "configured": False,
            "message": f"Key inválida o error: {resp.status_code}"
        })
    except Exception as e:
        return jsonify({"configured": False, "message": str(e)})


# ----------------------------------------------------------------------------
# Análisis de valor: cruza cuotas de la casa con historial de Pizarra
# ----------------------------------------------------------------------------

@app.route("/api/value-analysis/<code>", methods=["GET"])
def value_analysis(code):
    """
    Cruza las cuotas de The Odds API con el análisis histórico de Pizarra.
    Devuelve para cada partido los mercados con mejor 'valor' real vs cuota.

    Parámetros:
      ?team=Ecuador   (analiza un equipo específico)
    """
    if not ODDS_API_KEY:
        return jsonify({
            "error": "Falta ODDS_API_KEY para este endpoint."
        }), 503

    # Primero traemos las cuotas
    code_upper = code.upper()
    sport_key = ODDS_SPORT_MAP.get(code_upper)
    if not sport_key:
        return jsonify({"error": f"Competición '{code_upper}' no soportada."}), 400

    team = request.args.get("team", "").strip()

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us,eu,uk",
        "markets": "h2h,totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    try:
        resp = requests.get(
            f"{ODDS_BASE_URL}/sports/{sport_key}/odds",
            params=params,
            timeout=10
        )
        if resp.status_code != 200:
            return jsonify({"error": f"Error de The Odds API: {resp.status_code}"}), 502
        events = resp.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    if team:
        tl = team.lower()
        events = [
            ev for ev in events
            if tl in ev.get("home_team", "").lower()
            or tl in ev.get("away_team", "").lower()
        ]

    def american_to_prob(odds):
        """Convierte cuota americana a probabilidad implícita."""
        if odds is None:
            return None
        if odds > 0:
            return round(100 / (odds + 100) * 100, 1)
        else:
            return round(abs(odds) / (abs(odds) + 100) * 100, 1)

    results = []
    for ev in events[:10]:  # max 10 partidos para no sobrecargar
        home = ev.get("home_team")
        away = ev.get("away_team")

        # Calcular probabilidades implícitas del consenso de casas
        mkt_h2h = {}
        mkt_totals = {}
        mkt_btts = {}

        for bk in ev.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt["key"] == "h2h":
                    for o in mkt.get("outcomes", []):
                        mkt_h2h.setdefault(o["name"], []).append(o["price"])
                elif mkt["key"] == "totals":
                    for o in mkt.get("outcomes", []):
                        label = f"{o['name']} {o.get('point', '')}"
                        mkt_totals.setdefault(label, []).append(o["price"])
                elif mkt["key"] == "btts":
                    for o in mkt.get("outcomes", []):
                        mkt_btts.setdefault(o["name"], []).append(o["price"])

        def avg_odds(d):
            return {k: round(sum(v)/len(v)) for k, v in d.items()}

        def odds_to_probs(d):
            return {k: american_to_prob(v) for k, v in avg_odds(d).items()}

        # Advertencias de mercados a evitar
        warnings = []
        recommendations = []

        h2h_probs = odds_to_probs(mkt_h2h)
        totals_probs = odds_to_probs(mkt_totals)
        btts_probs = odds_to_probs(mkt_btts)

        # Regla 1: si ambos anotan tiene prob >65% → recomendado
        btts_yes = btts_probs.get("Yes", 0) or 0
        if btts_yes >= 65:
            recommendations.append({
                "market": "Ambos equipos anotan — Sí",
                "implied_prob": btts_yes,
                "note": "Alta probabilidad según consenso de casas"
            })

        # Regla 2: over 2.5 con prob >60% → recomendado
        for label, prob in totals_probs.items():
            if "Over" in label and prob and prob >= 60:
                recommendations.append({
                    "market": f"Más de {label.replace('Over ', '')} goles",
                    "implied_prob": prob,
                    "note": "La mayoría de las casas lo ven probable"
                })

        # Advertencia: NO apostar a jugadores individuales
        warnings.append({
            "type": "jugador_individual",
            "message": "⚠️ Evitá mercados de remates/goles de jugadores específicos — alta varianza, destruyen parlays aunque el equipo gane"
        })

        # Advertencia: si el favorito tiene prob >80% la cuota paga muy poco
        for team_name, prob in h2h_probs.items():
            if prob and prob >= 80:
                warnings.append({
                    "type": "favorito_extremo",
                    "message": f"⚠️ {team_name} tiene {prob}% probabilidad implícita — cuota muy baja, poco valor en parlay"
                })

        results.append({
            "match": f"{home} vs {away}",
            "home_team": home,
            "away_team": away,
            "commence_time": ev.get("commence_time"),
            "implied_probabilities": {
                "h2h": h2h_probs,
                "totals": totals_probs,
                "btts": btts_probs,
            },
            "recommendations": recommendations,
            "warnings": warnings,
            "bookmakers_count": len(ev.get("bookmakers", [])),
        })

    return jsonify({
        "competition": code_upper,
        "team_filter": team or None,
        "matches_analyzed": len(results),
        "results": results,
        "meta": {
            "note": "Las probabilidades implícitas incluyen el margen de la casa (~5-8%). La probabilidad real es algo mayor.",
            "avoid": "Remates/goles de jugadores individuales específicos — mercado de altísima varianza",
            "prefer": "Ambos anotan, Over/Under goles totales, Total tarjetas con árbitro conocido"
        }
    })


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
