"""
stats_extra.py — Extensión del backend con:

1) Tendencias de goles por equipo (usa football-data.org, ya disponible con
   el token actual, no requiere nada nuevo):
     GET /api/team-trends/<code>?team=<nombre>&last_n=20
     GET /api/head-to-head/<code>?team1=<nombre>&team2=<nombre>&last_n=10

2) Estadísticas avanzadas por partido y por equipo vía API-Football
   (api-sports.io / v3.football.api-sports.io): remates, corners, tarjetas
   amarillas/rojas, posesión, faltas. Requiere una API key separada
   (variable de entorno APISPORTS_KEY). Mientras no esté configurada,
   estos endpoints devuelven un error claro explicando cómo activarlos.
     GET /api/match-stats/<fixture_id>
     GET /api/team-stats/<league_id>/<team_id>?season=2025

USO PERSONAL / ANÁLISIS. No es asesoramiento financiero.
"""

import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, jsonify, request

extra_bp = Blueprint("stats_extra", __name__)

# ----------------------------------------------------------------------------
# Configuración compartida con backend.py (se re-declara acá para que este
# módulo funcione standalone si hace falta; backend.py importa este Blueprint)
# ----------------------------------------------------------------------------

FD_API_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "")
FD_BASE_URL = "https://api.football-data.org/v4"
FD_HEADERS = {"X-Auth-Token": FD_API_TOKEN}

# API-Football (api-sports.io) — plan free: remates, corners, tarjetas,
# posesión, faltas, todo incluido sin add-ons extra. Pedile a daniel... no,
# perdón, registrate en https://dashboard.api-football.com/register y
# pegá la key acá como variable de entorno: APISPORTS_KEY
APISPORTS_KEY = os.environ.get("APISPORTS_KEY", "")
APISPORTS_BASE_URL = "https://v3.football.api-sports.io"

_cache = {}
CACHE_TTL_SECONDS = 120  # un poco más largo acá: estos datos cambian menos seguido


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


def fetch_fd(path, params=None, ttl=CACHE_TTL_SECONDS):
    """Llama a football-data.org (mismo helper que backend.py, duplicado
    acá a propósito para que este módulo no dependa de imports circulares)."""
    cache_key = f"fd::{path}::{params}"
    cached = _cache_get(cache_key, ttl=ttl)
    if cached is not None:
        return cached, None

    try:
        resp = requests.get(f"{FD_BASE_URL}{path}", headers=FD_HEADERS, params=params, timeout=10)
    except requests.RequestException as e:
        return None, f"Error de conexión con football-data.org: {e}"

    if resp.status_code == 429:
        return None, "Límite de requests excedido (plan free: 10/min). Esperá un momento."
    if resp.status_code != 200:
        return None, f"Error de la API: {resp.status_code} - {resp.text[:200]}"

    data = resp.json()
    _cache_set(cache_key, data)
    return data, None


def fetch_apisports(path, params=None, ttl=CACHE_TTL_SECONDS):
    """Llama a v3.football.api-sports.io. Devuelve (json, error)."""
    if not APISPORTS_KEY:
        return None, (
            "No configuraste APISPORTS_KEY todavía. Registrate gratis en "
            "https://dashboard.api-football.com/register, copiá tu API key, "
            "y seteala como variable de entorno APISPORTS_KEY antes de "
            "levantar el backend (export APISPORTS_KEY=tu_key)."
        )

    cache_key = f"as::{path}::{params}"
    cached = _cache_get(cache_key, ttl=ttl)
    if cached is not None:
        return cached, None

    headers = {"x-apisports-key": APISPORTS_KEY}
    try:
        resp = requests.get(f"{APISPORTS_BASE_URL}{path}", headers=headers, params=params, timeout=10)
    except requests.RequestException as e:
        return None, f"Error de conexión con api-sports.io: {e}"

    if resp.status_code == 429:
        return None, "Límite de requests excedido en api-sports.io. Esperá un momento."
    if resp.status_code == 403:
        return None, "API key de api-sports.io inválida o sin permisos."
    if resp.status_code != 200:
        return None, f"Error de la API: {resp.status_code} - {resp.text[:200]}"

    data = resp.json()
    errors = data.get("errors")
    if errors:
        # api-sports.io devuelve 200 con un campo "errors" no vacío en vez
        # de un status code de error en muchos casos (ej: key inválida,
        # parámetros faltantes).
        return None, f"api-sports.io devolvió un error: {errors}"

    _cache_set(cache_key, data)
    return data, None


# Política de todo el backend: SOLO fútbol masculino, SOLO selección/equipo
# absoluto (adulto) — sin excepciones ni toggles para incluir lo contrario.
# api-sports.io marca categorías como sufijos/palabras sueltas en el nombre:
#   - Femenino: termina en " W" (ej: "Arsenal W", "Brazil W") o contiene "Women"
#   - Juveniles: contiene "U15".."U23" como palabra suelta
#   - Combinados olímpicos: contienen "Olympic"/"Olympics"
import re

_SENIOR_MEN_EXCLUDE_PATTERN = re.compile(
    r"(?:^|\s)(W|U1[5-9]|U2[0-3]|Women|Olympic|Olympics)(?:$|\s)",
    re.IGNORECASE,
)


def is_senior_men(team_name):
    """
    True si el nombre del equipo no trae marcadores de categoría juvenil,
    femenina, o combinado olímpico (que en selecciones suele ser sub-23).
    Usa límites de palabra (no substring suelto) para no descartar nombres
    legítimos que solo contienen esas letras de casualidad (ej: un club
    llamado "United W. F.C." sería un falso positivo aceptable y raro,
    pero "Newcastle" o "Wanderers" NO deben matchear "W").
    Se usa para filtrar resultados de búsqueda y fixtures de competiciones
    que mezclan todas las categorías en una sola liga (ej: "Friendlies").
    """
    if not team_name:
        return True
    return _SENIOR_MEN_EXCLUDE_PATTERN.search(team_name) is None


# ----------------------------------------------------------------------------
# 1) Tendencias de goles por equipo — football-data.org (ya disponible)
# ----------------------------------------------------------------------------

def _get_active_season(code):
    """Versión local de la resolución de temporada activa, igual a la de backend.py."""
    data, err = fetch_fd(f"/competitions/{code}")
    if err:
        return None, err
    seasons = data.get("seasons", [])
    for season in seasons:
        start = season["startDate"][:4]
        check, err2 = fetch_fd(f"/competitions/{code}/matches", params={"season": start, "status": "FINISHED"})
        if err2:
            continue
        if check.get("resultSet", {}).get("played", 0) > 0:
            return start, None
        check_live, err3 = fetch_fd(
            f"/competitions/{code}/matches",
            params={"season": start, "status": "LIVE,IN_PLAY,PAUSED"},
        )
        if not err3 and check_live.get("matches"):
            return start, None
    return None, "No se encontró temporada con partidos disponibles."


def _resolve_fd_team_id(code, team_name):
    """
    Busca el ID numérico de football-data.org para un equipo, dentro de
    una competición dada (el plan free no tiene búsqueda libre de equipos
    por nombre, hay que listar los equipos de una competición y filtrar
    ahí). Devuelve (team_id, team_full_name, error).
    """
    data, err = fetch_fd(f"/competitions/{code}/teams", ttl=3600)
    if err:
        return None, None, err
    team_lower = team_name.strip().lower()
    teams = data.get("teams", [])
    exact = [t for t in teams if t["name"].lower() == team_lower or t.get("shortName", "").lower() == team_lower]
    partial = [t for t in teams if team_lower in t["name"].lower()]
    match = (exact or partial or [None])[0]
    if not match:
        return None, None, f"No se encontró un equipo que coincida con '{team_name}' en {code}."
    return match["id"], match["name"], None


def _fetch_fd_team_matches(team_id, max_n, competition_filter=None):
    """
    Trae los últimos partidos FINALIZADOS de un equipo en football-data.org,
    mezclando TODAS las competiciones donde participa (liga local, copas
    domésticas si las hay, torneo continental) en una sola llamada — usa
    /teams/{id}/matches con rango de fechas, que es el único endpoint que
    no obliga a elegir una competición de antemano.

    competition_filter: código opcional (ej "PL", "CL") para acotar el
    resultado a una sola competición después de traerlas todas.

    OJO: football-data.org limita el rango de fechas a 750 días — para
    equipos con pocos partidos recientes (ej. selecciones que solo juegan
    cada tanto) esto puede no alcanzar a juntar max_n partidos; es una
    limitación de cobertura de datos, no del código.
    """
    today = datetime.now(timezone.utc).date()
    date_from = today - timedelta(days=730)
    data, err = fetch_fd(
        f"/teams/{team_id}/matches",
        params={
            "dateFrom": date_from.isoformat(),
            "dateTo": today.isoformat(),
        },
        ttl=300,
    )
    if err:
        return None, err

    matches = [m for m in data.get("matches", []) if m.get("status") == "FINISHED"]
    if competition_filter:
        cf = competition_filter.strip().upper()
        matches = [m for m in matches if m["competition"]["code"] == cf]

    matches.sort(key=lambda m: m["utcDate"], reverse=True)
    return matches[:max_n], None


def _compute_fd_trends_from_matches(team_matches, team_id):
    """
    Calcula goles a favor/contra, BTTS, clean sheets, récord y goles por
    mitad a partir de una lista de partidos de football-data.org ya
    filtrada (no vuelve a buscar nada). Separado de team_trends() para
    poder reusarlo calculando distintas ventanas de la misma lista de
    partidos (ej: últimos 5 vs últimos 20) sin pedir nada dos veces a la
    API ni duplicar el cálculo.
    """
    scored = []
    conceded = []
    over_25_scored = 0
    over_15_scored = 0
    clean_sheets = 0
    failed_to_score = 0
    ht_goals_scored = []
    st_goals_scored = []
    ht_goals_conceded = []
    st_goals_conceded = []
    btts = 0
    wins = draws = losses = 0
    sample = []

    for m in team_matches:
        is_home = m["homeTeam"]["id"] == team_id
        gf = m["score"]["fullTime"]["home"] if is_home else m["score"]["fullTime"]["away"]
        ga = m["score"]["fullTime"]["away"] if is_home else m["score"]["fullTime"]["home"]
        ht_gf = m["score"]["halfTime"]["home"] if is_home else m["score"]["halfTime"]["away"]
        ht_ga = m["score"]["halfTime"]["away"] if is_home else m["score"]["halfTime"]["home"]

        scored.append(gf)
        conceded.append(ga)

        if gf >= 3:
            over_25_scored += 1
        if gf >= 2:
            over_15_scored += 1
        if ga == 0:
            clean_sheets += 1
        if gf == 0:
            failed_to_score += 1
        if gf > 0 and ga > 0:
            btts += 1

        ht_goals_scored.append(ht_gf if ht_gf is not None else 0)
        st_goals_scored.append((gf - ht_gf) if ht_gf is not None else None)
        ht_goals_conceded.append(ht_ga if ht_ga is not None else 0)
        st_goals_conceded.append((ga - ht_ga) if ht_ga is not None else None)

        winner = m["score"]["winner"]
        if winner == "DRAW":
            draws += 1
        elif (winner == "HOME_TEAM" and is_home) or (winner == "AWAY_TEAM" and not is_home):
            wins += 1
        elif winner is not None:
            losses += 1

        opponent = m["awayTeam"]["name"] if is_home else m["homeTeam"]["name"]
        sample.append({
            "date": m["utcDate"][:10],
            "venue": "local" if is_home else "visitante",
            "opponent": opponent,
            "goalsFor": gf,
            "goalsAgainst": ga,
            "competition": m["competition"]["code"],
        })

    n = len(team_matches)
    valid_st = [g for g in st_goals_scored if g is not None]
    valid_st_conceded = [g for g in st_goals_conceded if g is not None]

    return {
        "sample_size": n,
        "record": {"won": wins, "draw": draws, "lost": losses},
        "goals_for": {
            "avg_per_match": round(sum(scored) / n, 2),
            "matches_with_2plus_goals": over_15_scored,
            "matches_with_2plus_goals_pct": round(over_15_scored / n * 100, 1),
            "matches_with_3plus_goals": over_25_scored,
            "matches_with_3plus_goals_pct": round(over_25_scored / n * 100, 1),
            "failed_to_score_count": failed_to_score,
            "failed_to_score_pct": round(failed_to_score / n * 100, 1),
        },
        "goals_against": {
            "avg_per_match": round(sum(conceded) / n, 2),
            "clean_sheets": clean_sheets,
            "clean_sheets_pct": round(clean_sheets / n * 100, 1),
        },
        "btts_pct": round(btts / n * 100, 1),
        "goals_by_half": {
            "avg_first_half": round(sum(ht_goals_scored) / n, 2),
            "avg_second_half": round(sum(valid_st) / len(valid_st), 2) if valid_st else None,
            "avg_first_half_conceded": round(sum(ht_goals_conceded) / n, 2),
            "avg_second_half_conceded": round(sum(valid_st_conceded) / len(valid_st_conceded), 2) if valid_st_conceded else None,
            "note": "Promedio de goles propios y del rival, anotados/recibidos en cada mitad.",
        },
        "tendency_over_2_5_team_goals": (
            "Sí, suele superar 2.5 goles propios por partido"
            if (sum(scored) / n) > 2.5 else
            "No, normalmente anota 2.5 goles propios o menos por partido"
        ),
        "recent_matches": sample,
    }


@extra_bp.route("/api/team-trends/<code>", methods=["GET"])
def team_trends(code):

    """
    Tendencia de goles de UN equipo: cuántos partidos hace/recibe más de
    2.5 goles, promedio anotado/recibido, BTTS, clean sheets, etc. Sobre
    los últimos N partidos del equipo.

    Por defecto junta TODAS las competiciones donde participa el equipo
    (liga local + copas/torneos continentales que tenga football-data.org
    asociados), no solo la competición pasada en la URL — el código de la
    URL solo se usa para encontrar el ID del equipo. Para acotar a una
    sola competición, usar ?competition=<código> (ej. ?competition=CL).

    NOTA sobre selecciones nacionales: football-data.org no tiene cargadas
    las eliminatorias mundialistas ni amistosos de selecciones, solo el
    torneo final (Mundial/Eurocopa) — así que para selecciones este
    endpoint puede devolver pocos partidos por limitación real de datos,
    no por un error de búsqueda. Para selecciones, el dashboard usa en su
    lugar el motor de api-sports.io (mismo que "Remates & tarjetas"), que
    sí cubre eliminatorias y amistosos.

    Ejemplo: /api/team-trends/PL?team=Arsenal&last_n=20
    Ejemplo con filtro: /api/team-trends/PL?team=Arsenal&last_n=20&competition=CL
    """
    code = code.upper()
    team_name = request.args.get("team", "").strip()
    if not team_name:
        return jsonify({"error": "Falta el parámetro ?team=<nombre del equipo>"}), 400

    last_n = int(request.args.get("last_n", 20))
    competition_filter = request.args.get("competition", "").strip() or None

    team_id, team_full_name, err = _resolve_fd_team_id(code, team_name)
    if err:
        return jsonify({"error": err}), 502

    team_matches, err2 = _fetch_fd_team_matches(team_id, last_n, competition_filter)
    if err2:
        return jsonify({"error": err2}), 502

    if not team_matches:
        msg = (
            f"No se encontraron partidos finalizados recientes para '{team_full_name}'"
            + (f" en la competición {competition_filter}" if competition_filter else "")
            + ". Si es una selección nacional, football-data.org puede no tener "
              "cargadas sus eliminatorias o amistosos — esa cobertura completa "
              "está en la pestaña Remates & tarjetas."
        )
        return jsonify({"error": msg}), 404

    competitions_in_sample = sorted(set(m["competition"]["code"] for m in team_matches))
    team_name = team_full_name  # para que el resto de la función (y la respuesta) use el nombre completo real

    result = _compute_fd_trends_from_matches(team_matches, team_id)
    result["competition_code"] = code
    result["competition_filter_applied"] = competition_filter
    result["competitions_in_sample"] = competitions_in_sample
    result["team_matched"] = team_name

    # Comparación de forma reciente: últimos 5 partidos vs. los últimos 20
    # (o lo que haya disponible), calculados sobre la MISMA lista de
    # partidos ya traída — no pedimos nada extra a la API. Sirve para ver
    # si un equipo está mejor o peor que su promedio "de fondo" últimamente.
    # team_matches ya viene ordenado del más reciente al más viejo.
    if len(team_matches) >= 5:
        short_window = _compute_fd_trends_from_matches(team_matches[:5], team_id)
        long_window = _compute_fd_trends_from_matches(team_matches, team_id)
        result["form_comparison"] = {
            "short_window_size": short_window["sample_size"],
            "long_window_size": long_window["sample_size"],
            "short_window": {
                "goals_for_avg": short_window["goals_for"]["avg_per_match"],
                "goals_against_avg": short_window["goals_against"]["avg_per_match"],
                "record": short_window["record"],
                "btts_pct": short_window["btts_pct"],
                "clean_sheets_pct": short_window["goals_against"]["clean_sheets_pct"],
            },
            "long_window": {
                "goals_for_avg": long_window["goals_for"]["avg_per_match"],
                "goals_against_avg": long_window["goals_against"]["avg_per_match"],
                "record": long_window["record"],
                "btts_pct": long_window["btts_pct"],
                "clean_sheets_pct": long_window["goals_against"]["clean_sheets_pct"],
            },
        }
    else:
        result["form_comparison"] = None

    return jsonify(result)


@extra_bp.route("/api/head-to-head/<code>", methods=["GET"])
def head_to_head(code):
    """
    Historial de enfrentamientos directos entre dos equipos dentro de UNA
    competición (no busca en todas las competiciones a la vez, porque
    football-data.org no tiene un endpoint global de "todos los partidos
    entre el equipo A y B" — hay que acotar a una liga/temporada).

    Ejemplo: /api/head-to-head/PL?team1=Arsenal&team2=Chelsea&last_n=10
    """
    code = code.upper()
    team1_name = request.args.get("team1", "").strip()
    team2_name = request.args.get("team2", "").strip()
    if not team1_name or not team2_name:
        return jsonify({"error": "Faltan parámetros ?team1=<nombre> y ?team2=<nombre>"}), 400

    last_n = int(request.args.get("last_n", 10))
    season = request.args.get("season")

    if not season:
        season, err = _get_active_season(code)
        if err:
            return jsonify({"error": err}), 502

    data, err = fetch_fd(
        f"/competitions/{code}/matches",
        params={"season": season, "status": "FINISHED"},
    )
    if err:
        return jsonify({"error": err}), 502

    raw_matches = data.get("matches", [])
    t1_lower = team1_name.lower()
    t2_lower = team2_name.lower()

    def involves_both(m):
        home = m["homeTeam"]["name"].lower()
        away = m["awayTeam"]["name"].lower()
        has_t1 = t1_lower in home or t1_lower in away
        has_t2 = t2_lower in home or t2_lower in away
        return has_t1 and has_t2

    h2h_matches = [m for m in raw_matches if involves_both(m)]

    if not h2h_matches:
        return jsonify({
            "error": (
                f"No se encontraron enfrentamientos entre equipos que coincidan con "
                f"'{team1_name}' y '{team2_name}' en {code} temporada {season}. "
                f"Esto solo busca dentro de esta competición/temporada — si jugaron "
                f"en otra liga o copa no va a aparecer acá."
            )
        }), 404

    h2h_matches.sort(key=lambda m: m["utcDate"], reverse=True)
    h2h_matches = h2h_matches[:last_n]

    team1_wins = team2_wins = draws = 0
    team1_goals = team2_goals = 0
    sample = []

    for m in h2h_matches:
        home_name = m["homeTeam"]["name"]
        away_name = m["awayTeam"]["name"]
        home_is_t1 = t1_lower in home_name.lower()

        home_goals = m["score"]["fullTime"]["home"]
        away_goals = m["score"]["fullTime"]["away"]
        t1_goals_this_match = home_goals if home_is_t1 else away_goals
        t2_goals_this_match = away_goals if home_is_t1 else home_goals

        team1_goals += t1_goals_this_match
        team2_goals += t2_goals_this_match

        if t1_goals_this_match > t2_goals_this_match:
            team1_wins += 1
        elif t2_goals_this_match > t1_goals_this_match:
            team2_wins += 1
        else:
            draws += 1

        sample.append({
            "date": m["utcDate"][:10],
            "homeTeam": home_name,
            "awayTeam": away_name,
            "homeGoals": home_goals,
            "awayGoals": away_goals,
        })

    n = len(h2h_matches)
    result = {
        "competition_code": code,
        "season": season,
        "team1": team1_name,
        "team2": team2_name,
        "sample_size": n,
        "record": {
            "team1_wins": team1_wins,
            "draws": draws,
            "team2_wins": team2_wins,
        },
        "goals": {
            "team1_total": team1_goals,
            "team2_total": team2_goals,
            "team1_avg": round(team1_goals / n, 2),
            "team2_avg": round(team2_goals / n, 2),
        },
        "matches": sample,
    }
    return jsonify(result)


# ----------------------------------------------------------------------------
# 2) Estadísticas avanzadas — API-Football (api-sports.io)
#    Remates, corners, tarjetas, posesión, faltas.
#    REQUIERE: variable de entorno APISPORTS_KEY (ver fetch_apisports arriba)
# ----------------------------------------------------------------------------

@extra_bp.route("/api/apisports/status", methods=["GET"])
def apisports_status():
    """Chequeo rápido: ¿está configurada la key de api-sports.io?"""
    if not APISPORTS_KEY:
        return jsonify({
            "configured": False,
            "message": (
                "Falta configurar APISPORTS_KEY. Pasos: 1) Registrate gratis en "
                "https://dashboard.api-football.com/register  2) Copiá tu API key "
                "del dashboard  3) Corré el backend con "
                "APISPORTS_KEY=tu_key python3 backend.py (o seteala como variable "
                "de entorno permanente)."
            ),
        }), 200

    data, err = fetch_apisports("/status")
    if err:
        return jsonify({"configured": True, "working": False, "error": err}), 502

    resp = data.get("response", {})
    return jsonify({
        "configured": True,
        "working": True,
        "account": resp.get("account"),
        "subscription": resp.get("subscription"),
        "requests_today": resp.get("requests"),
    })


@extra_bp.route("/api/apisports/search-team", methods=["GET"])
def apisports_search_team():
    """
    Busca el ID interno de un equipo en api-sports.io (lo necesitás antes de
    pedir /api/team-stats, porque esa API identifica equipos por ID numérico,
    no por nombre). SIEMPRE excluye equipos femeninos, juveniles y combinados
    olímpicos — solo masculino adulto/absoluto.
    Ejemplo: /api/apisports/search-team?name=Arsenal
    """
    name = request.args.get("name", "").strip()
    if not name or len(name) < 3:
        return jsonify({"error": "Pasá ?name=<al menos 3 caracteres>"}), 400

    data, err = fetch_apisports("/teams", params={"search": name})
    if err:
        return jsonify({"error": err}), 502

    teams = [
        {
            "id": t["team"]["id"],
            "name": t["team"]["name"],
            "country": t["team"].get("country"),
            "national": t["team"].get("national"),
            "logo": t["team"].get("logo"),
        }
        for t in data.get("response", [])
        if is_senior_men(t["team"]["name"])
    ]
    return jsonify({"results": teams})


@extra_bp.route("/api/apisports/search-league", methods=["GET"])
def apisports_search_league():
    """
    Busca el ID interno de una liga/competición en api-sports.io.
    Ejemplo: /api/apisports/search-league?name=Premier League
    """
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Pasá ?name=<nombre de la liga>"}), 400

    data, err = fetch_apisports("/leagues", params={"name": name})
    if err:
        return jsonify({"error": err}), 502

    leagues = [
        {
            "id": entry["league"]["id"],
            "name": entry["league"]["name"],
            "type": entry["league"].get("type"),
            "country": entry["country"]["name"],
            "seasons_available": [s["year"] for s in entry.get("seasons", [])],
        }
        for entry in data.get("response", [])
    ]
    return jsonify({"results": leagues})


@extra_bp.route("/api/match-stats/<int:fixture_id>", methods=["GET"])
def match_stats(fixture_id):
    """
    Estadísticas detalladas de UN partido específico: remates (totales, a
    puerta, dentro/fuera del área), corners, faltas, tarjetas, posesión.
    Necesitás el fixture_id de api-sports.io (no el id de football-data.org;
    son números distintos en cada API).

    Ejemplo: /api/match-stats/215662
    """
    data, err = fetch_apisports("/fixtures/statistics", params={"fixture": fixture_id})
    if err:
        return jsonify({"error": err}), 502

    teams_stats = data.get("response", [])
    if not teams_stats:
        return jsonify({"error": "No hay estadísticas disponibles para ese fixture_id."}), 404

    def parse_team_block(block):
        stats = {item["type"]: item["value"] for item in block.get("statistics", [])}
        return {
            "team": block["team"]["name"],
            "shots_total": stats.get("Total Shots"),
            "shots_on_goal": stats.get("Shots on Goal"),
            "shots_off_goal": stats.get("Shots off Goal"),
            "shots_inside_box": stats.get("Shots insidebox"),
            "shots_outside_box": stats.get("Shots outsidebox"),
            "corners": stats.get("Corner Kicks"),
            "fouls": stats.get("Fouls"),
            "offsides": stats.get("Offsides"),
            "possession": stats.get("Ball Possession"),
            "yellow_cards": stats.get("Yellow Cards"),
            "red_cards": stats.get("Red Cards"),
            "goalkeeper_saves": stats.get("Goalkeeper Saves"),
        }

    result = {
        "fixture_id": fixture_id,
        "teams": [parse_team_block(b) for b in teams_stats],
    }
    return jsonify(result)


@extra_bp.route("/api/team-stats/<int:league_id>/<int:team_id>", methods=["GET"])
def team_stats(league_id, team_id):
    """
    Estadísticas AGREGADAS de un equipo en una liga/temporada completa:
    promedio de remates, corners, tarjetas por partido, y desglose
    casa/visitante. Esto es lo más útil para responder "este equipo suele
    sacar muchos corners" o "este equipo se va expulsado / amonestado seguido".

    Necesitás league_id y team_id de api-sports.io — usá antes
    /api/apisports/search-league y /api/apisports/search-team para
    encontrarlos.

    Ejemplo: /api/team-stats/39/42?season=2025   (39 = Premier League, 42 = Arsenal)
    """
    season = request.args.get("season", str(datetime.now(timezone.utc).year - 1))

    data, err = fetch_apisports(
        "/teams/statistics",
        params={"league": league_id, "team": team_id, "season": season},
    )
    if err:
        return jsonify({"error": err}), 502

    resp = data.get("response", {})
    if not resp:
        return jsonify({"error": "Sin datos para esa combinación de liga/equipo/temporada."}), 404

    fixtures = resp.get("fixtures", {})
    goals = resp.get("goals", {})
    cards = resp.get("cards", {})

    def avg(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    result = {
        "team": resp.get("team", {}).get("name"),
        "league": resp.get("league", {}).get("name"),
        "season": season,
        "matches_played": fixtures.get("played", {}).get("total"),
        "goals_for_avg": {
            "total": avg(goals.get("for", {}).get("average", {}).get("total")),
            "home": avg(goals.get("for", {}).get("average", {}).get("home")),
            "away": avg(goals.get("for", {}).get("average", {}).get("away")),
        },
        "goals_against_avg": {
            "total": avg(goals.get("against", {}).get("average", {}).get("total")),
            "home": avg(goals.get("against", {}).get("average", {}).get("home")),
            "away": avg(goals.get("against", {}).get("average", {}).get("away")),
        },
        "goals_by_minute_for": goals.get("for", {}).get("minute"),
        "goals_by_minute_against": goals.get("against", {}).get("minute"),
        "cards": {
            "yellow_by_minute": cards.get("yellow"),
            "red_by_minute": cards.get("red"),
        },
        "clean_sheets": resp.get("clean_sheet"),
        "failed_to_score": resp.get("failed_to_score"),
        "biggest": resp.get("biggest"),
        "note": (
            "Para remates/corners promedio por partido, llamá también a "
            "/api/team-match-stats-avg/<league_id>/<team_id> (calculado "
            "iterando los últimos partidos, ya que api-sports.io no lo da "
            "agregado de fábrica)."
        ),
    }
    return jsonify(result)


@extra_bp.route("/api/team-match-stats-avg/<int:league_id>/<int:team_id>", methods=["GET"])
def team_match_stats_avg(league_id, team_id):
    """
    Promedio de remates, corners, tarjetas y faltas por partido de un equipo,
    calculado iterando sus últimos N fixtures finalizados y promediando.
    También arma un ranking de los 5 jugadores con más remates (total y al
    arco) sumados en esos mismos partidos — la API no desglosa remates
    dentro/fuera del área a nivel individual, solo a nivel equipo.

    api-sports.io no ofrece nada de esto agregado directamente, así que lo
    armamos nosotros combinando /fixtures (para encontrar los partidos) +
    /fixtures/statistics (stats de equipo) + /fixtures/players (stats
    individuales) por cada partido.

    OJO: consume 2 requests de cuota por cada partido analizado (stats de
    equipo + stats de jugadores), además de la búsqueda inicial de
    fixtures. Con last_n=10 son ~21 requests.

    Ejemplo: /api/team-match-stats-avg/39/42?last_n=10
    """
    last_n = int(request.args.get("last_n", 10))
    if last_n > 20:
        return jsonify({"error": "last_n máximo permitido: 20 (para cuidar la cuota diaria)."}), 400

    fixtures_data, err = fetch_apisports(
        "/fixtures",
        params={"team": team_id, "league": league_id, "last": last_n, "status": "FT"},
    )
    if err:
        return jsonify({"error": err}), 502

    fixtures = fixtures_data.get("response", [])
    if not fixtures:
        return jsonify({"error": "No se encontraron partidos finalizados para ese equipo/liga."}), 404

    totals = defaultdict(float)
    counts = defaultdict(int)
    per_match = []
    player_shots = defaultdict(lambda: {"shots_total": 0, "shots_on_goal": 0, "matches": 0, "name": "", "photo": None})

    for fx in fixtures:
        fixture_id = fx["fixture"]["id"]
        stats_data, err2 = fetch_apisports("/fixtures/statistics", params={"fixture": fixture_id})
        if err2:
            continue  # si un partido puntual falla, seguimos con el resto

        # Leer ambos bloques (propio y rival) en una sola pasada
        own_stats = {}
        opp_stats = {}
        for block in stats_data.get("response", []):
            blk_stats = {item["type"]: item["value"] for item in block.get("statistics", [])}
            if block["team"]["id"] == team_id:
                own_stats = blk_stats
            else:
                opp_stats = blk_stats

        if not own_stats:
            continue  # partido sin bloque propio (raro, pero posible)

        def num(key, src=own_stats):
            v = src.get(key)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def pct(key):
            # "Ball Possession" y "Passes %" vienen como string "60%",
            # no como número — hay que sacar el símbolo antes de parsear.
            v = own_stats.get(key)
            if v is None:
                return None
            try:
                return float(str(v).replace("%", ""))
            except (TypeError, ValueError):
                return None

        corners_against_val = num("Corner Kicks", opp_stats)

        row = {
            "shots_total": num("Total Shots"),
            "shots_on_goal": num("Shots on Goal"),
            "shots_off_goal": num("Shots off Goal"),
            "shots_blocked": num("Blocked Shots"),
            "shots_inside_box": num("Shots insidebox"),
            "shots_outside_box": num("Shots outsidebox"),
            "corners": num("Corner Kicks"),
            "corners_against": corners_against_val,
            "fouls": num("Fouls"),
            "offsides": num("Offsides"),
            "yellow_cards": num("Yellow Cards"),
            "red_cards": num("Red Cards"),
            "possession_pct": pct("Ball Possession"),
            "goalkeeper_saves": num("Goalkeeper Saves"),
            "expected_goals": num("expected_goals"),
            "goals_prevented": num("goals_prevented"),
        }
        for key, val in row.items():
            if val is not None:
                totals[key] += val
                counts[key] += 1

        per_match.append({
            "date": fx["fixture"]["date"][:10],
            "opponent": (
                fx["teams"]["away"]["name"] if fx["teams"]["home"]["id"] == team_id
                else fx["teams"]["home"]["name"]
            ),
            "goals_for": (
                fx["goals"]["home"] if fx["teams"]["home"]["id"] == team_id
                else fx["goals"]["away"]
            ),
            **row,
        })

        # Remates por jugador en este mismo partido. api-sports.io no
        # desglosa dentro/fuera del área a nivel individual (solo a nivel
        # equipo, ya capturado arriba) — acá solo hay remates totales y al
        # arco por jugador.
        players_data, err3 = fetch_apisports("/fixtures/players", params={"fixture": fixture_id})
        if not err3:
            for block in players_data.get("response", []):
                if block["team"]["id"] != team_id:
                    continue
                for p in block.get("players", []):
                    pstats = p["statistics"][0] if p.get("statistics") else {}
                    shots = pstats.get("shots") or {}
                    s_total = shots.get("total")
                    s_on = shots.get("on")
                    if s_total is None and s_on is None:
                        continue  # jugador sin minutos relevantes o sin datos de remates
                    pid = p["player"]["id"]
                    entry = player_shots[pid]
                    entry["name"] = p["player"]["name"]
                    entry["photo"] = p["player"].get("photo")
                    entry["shots_total"] += s_total or 0
                    entry["shots_on_goal"] += s_on or 0
                    entry["matches"] += 1

    if not per_match:
        return jsonify({
            "error": "No se pudieron obtener estadísticas detalladas para ningún partido (puede que esta liga/temporada no tenga datos de fixtures/statistics disponibles)."
        }), 404

    AVG_KEYS = [
        "shots_total", "shots_on_goal", "shots_off_goal", "shots_blocked",
        "shots_inside_box", "shots_outside_box",
        "corners", "corners_against",
        "fouls", "offsides", "yellow_cards", "red_cards",
        "possession_pct", "goalkeeper_saves", "expected_goals", "goals_prevented",
    ]
    averages = {
        key: round(totals[key] / counts[key], 2) if counts[key] else None
        for key in AVG_KEYS
    }

    sorted_shooters = sorted(
        player_shots.values(), key=lambda p: (p["shots_on_goal"] / max(p["matches"], 1), p["shots_on_goal"]), reverse=True
    )[:5]
    top_shooters = [
        {**p, "shots_on_goal_per_match": round(p["shots_on_goal"] / max(p["matches"], 1), 2)}
        for p in sorted_shooters
    ]

    result = {
        "league_id": league_id,
        "team_id": team_id,
        "matches_analyzed": len(per_match),
        "averages_per_match": averages,
        "per_match_detail": per_match,
        "top_shooters": top_shooters,
    }
    return jsonify(result)


def _fetch_apisports_multi_league_fixtures(team_id, league_ids, last_n):
    """
    Junta fixtures FINALIZADOS de un equipo en VARIAS ligas de api-sports.io
    a la vez, y devuelve los last_n más recientes en el tiempo real, sin
    importar de qué liga vienen. Reutilizado por team_match_stats_multi
    (remates/tarjetas) y team_trends_multi (tendencias de goles) para no
    duplicar la misma búsqueda dos veces.

    Devuelve (lista_de_(league_id, fixture), error).
    """
    all_fixtures = []
    seen_fixture_ids = set()
    for lg_id in league_ids:
        fixtures_data, err = fetch_apisports(
            "/fixtures",
            params={"team": team_id, "league": lg_id, "last": last_n, "status": "FT"},
        )
        if err:
            continue  # esta competición puede no tener datos para este equipo; seguimos con las demás
        for fx in fixtures_data.get("response", []):
            fid = fx["fixture"]["id"]
            if fid in seen_fixture_ids:
                continue
            seen_fixture_ids.add(fid)
            all_fixtures.append((lg_id, fx))

    if not all_fixtures:
        return None, "No se encontraron partidos finalizados para este equipo en ninguna de las competiciones consultadas."

    all_fixtures.sort(key=lambda pair: pair[1]["fixture"]["date"], reverse=True)
    return all_fixtures[:last_n], None


@extra_bp.route("/api/team-match-stats-multi/<int:team_id>", methods=["GET"])
def team_match_stats_multi(team_id):
    """
    Igual que /api/team-match-stats-avg pero buscando en VARIAS ligas a la
    vez y quedándose con los N partidos más RECIENTES en el tiempo, sin
    importar de qué competición vengan. Pensado para selecciones
    nacionales: un equipo puede jugar su partido más reciente en el
    Mundial, mientras que la mayoría de su historial está en la
    eliminatoria continental — y lo que el usuario espera de "últimos N
    partidos" es justamente eso, los más recientes en el calendario real,
    no los más recientes dentro de una sola competición.

    OJO: el costo en requests escala con la cantidad de league_id pasados,
    ya que se consulta /fixtures una vez por cada uno (más 1 request por
    partido finalmente seleccionado para traer sus estadísticas).

    Ejemplo: /api/team-match-stats-multi/2382?league_ids=1,32,34,29,30,31,33,4,10&last_n=10
    """
    last_n = int(request.args.get("last_n", 10))
    if last_n > 20:
        return jsonify({"error": "last_n máximo permitido: 20 (para cuidar la cuota diaria)."}), 400

    league_ids_param = request.args.get("league_ids", "")
    try:
        league_ids = [int(x) for x in league_ids_param.split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "league_ids inválido, esperaba una lista de números separados por coma."}), 400
    if not league_ids:
        return jsonify({"error": "Falta el parámetro ?league_ids=1,32,34,..."}), 400

    selected, err = _fetch_apisports_multi_league_fixtures(team_id, league_ids, last_n)
    if err:
        return jsonify({"error": err}), 404

    # 3) Traer estadísticas de cada uno de esos partidos seleccionados.
    totals = defaultdict(float)
    counts = defaultdict(int)
    per_match = []
    leagues_used = set()
    player_shots = defaultdict(lambda: {"shots_total": 0, "shots_on_goal": 0, "matches": 0, "name": "", "photo": None})

    for lg_id, fx in selected:
        fixture_id = fx["fixture"]["id"]
        stats_data, err2 = fetch_apisports("/fixtures/statistics", params={"fixture": fixture_id})
        if err2:
            continue

        own_stats = {}
        opp_stats = {}
        for block in stats_data.get("response", []):
            blk_stats = {item["type"]: item["value"] for item in block.get("statistics", [])}
            if block["team"]["id"] == team_id:
                own_stats = blk_stats
            else:
                opp_stats = blk_stats

        if not own_stats:
            continue

        def num(key, src=own_stats):
            v = src.get(key)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def pct(key):
            v = own_stats.get(key)
            if v is None:
                return None
            try:
                return float(str(v).replace("%", ""))
            except (TypeError, ValueError):
                return None

        corners_against_val = num("Corner Kicks", opp_stats)

        row = {
            "shots_total": num("Total Shots"),
            "shots_on_goal": num("Shots on Goal"),
            "shots_off_goal": num("Shots off Goal"),
            "shots_blocked": num("Blocked Shots"),
            "shots_inside_box": num("Shots insidebox"),
            "shots_outside_box": num("Shots outsidebox"),
            "corners": num("Corner Kicks"),
            "corners_against": corners_against_val,
            "fouls": num("Fouls"),
            "offsides": num("Offsides"),
            "yellow_cards": num("Yellow Cards"),
            "red_cards": num("Red Cards"),
            "possession_pct": pct("Ball Possession"),
            "goalkeeper_saves": num("Goalkeeper Saves"),
            "expected_goals": num("expected_goals"),
            "goals_prevented": num("goals_prevented"),
        }
        for key, val in row.items():
            if val is not None:
                totals[key] += val
                counts[key] += 1

        leagues_used.add(lg_id)
        per_match.append({
            "date": fx["fixture"]["date"][:10],
            "opponent": (
                fx["teams"]["away"]["name"] if fx["teams"]["home"]["id"] == team_id
                else fx["teams"]["home"]["name"]
            ),
            "goals_for": (
                fx["goals"]["home"] if fx["teams"]["home"]["id"] == team_id
                else fx["goals"]["away"]
            ),
            "league_id": lg_id,
            **row,
        })

        # Remates por jugador, mismo criterio que team_match_stats_avg.
        players_data, err4 = fetch_apisports("/fixtures/players", params={"fixture": fixture_id})
        if not err4:
            for block in players_data.get("response", []):
                if block["team"]["id"] != team_id:
                    continue
                for p in block.get("players", []):
                    pstats = p["statistics"][0] if p.get("statistics") else {}
                    shots = pstats.get("shots") or {}
                    s_total = shots.get("total")
                    s_on = shots.get("on")
                    if s_total is None and s_on is None:
                        continue
                    pid = p["player"]["id"]
                    entry = player_shots[pid]
                    entry["name"] = p["player"]["name"]
                    entry["photo"] = p["player"].get("photo")
                    entry["shots_total"] += s_total or 0
                    entry["shots_on_goal"] += s_on or 0
                    entry["matches"] += 1

    if not per_match:
        return jsonify({
            "error": "No se pudieron obtener estadísticas detalladas para ninguno de los partidos más recientes encontrados."
        }), 404

    AVG_KEYS = [
        "shots_total", "shots_on_goal", "shots_off_goal", "shots_blocked",
        "shots_inside_box", "shots_outside_box",
        "corners", "corners_against",
        "fouls", "offsides", "yellow_cards", "red_cards",
        "possession_pct", "goalkeeper_saves", "expected_goals", "goals_prevented",
    ]
    averages = {
        key: round(totals[key] / counts[key], 2) if counts[key] else None
        for key in AVG_KEYS
    }

    sorted_shooters2 = sorted(
        player_shots.values(), key=lambda p: (p["shots_on_goal"] / max(p["matches"], 1), p["shots_on_goal"]), reverse=True
    )[:5]
    top_shooters = [
        {**p, "shots_on_goal_per_match": round(p["shots_on_goal"] / max(p["matches"], 1), 2)}
        for p in sorted_shooters2
    ]

    result = {
        "team_id": team_id,
        "leagues_searched": league_ids,
        "leagues_used": sorted(leagues_used),
        "matches_analyzed": len(per_match),
        "averages_per_match": averages,
        "per_match_detail": per_match,
        "top_shooters": top_shooters,
    }
    return jsonify(result)


def _compute_apisports_trends_from_fixtures(selected, team_id):
    """
    Misma idea que _compute_fd_trends_from_matches pero para fixtures de
    api-sports.io (estructura distinta a football-data.org: selected es
    una lista de tuplas (league_id, fixture)). Separado para poder
    calcular distintas ventanas (ej: últimos 5 vs últimos 20) sobre la
    misma lista ya traída.
    """
    scored = []
    conceded = []
    over_25_scored = 0
    over_15_scored = 0
    clean_sheets = 0
    failed_to_score = 0
    ht_goals_scored = []
    st_goals_scored = []
    ht_goals_conceded = []
    st_goals_conceded = []
    btts = 0
    wins = draws = losses = 0
    sample = []
    leagues_used = set()

    for lg_id, fx in selected:
        is_home = fx["teams"]["home"]["id"] == team_id
        gf = fx["goals"]["home"] if is_home else fx["goals"]["away"]
        ga = fx["goals"]["away"] if is_home else fx["goals"]["home"]
        if gf is None or ga is None:
            continue

        ht = fx.get("score", {}).get("halftime", {}) or {}
        ht_gf = ht.get("home") if is_home else ht.get("away")
        ht_ga = ht.get("away") if is_home else ht.get("home")

        scored.append(gf)
        conceded.append(ga)

        if gf >= 3:
            over_25_scored += 1
        if gf >= 2:
            over_15_scored += 1
        if ga == 0:
            clean_sheets += 1
        if gf == 0:
            failed_to_score += 1
        if gf > 0 and ga > 0:
            btts += 1

        ht_goals_scored.append(ht_gf if ht_gf is not None else 0)
        st_goals_scored.append((gf - ht_gf) if ht_gf is not None else None)
        ht_goals_conceded.append(ht_ga if ht_ga is not None else 0)
        st_goals_conceded.append((ga - ht_ga) if ht_ga is not None else None)

        if gf > ga:
            wins += 1
        elif gf < ga:
            losses += 1
        else:
            draws += 1

        leagues_used.add(lg_id)
        opponent = fx["teams"]["away"]["name"] if is_home else fx["teams"]["home"]["name"]
        sample.append({
            "date": fx["fixture"]["date"][:10],
            "venue": "local" if is_home else "visitante",
            "opponent": opponent,
            "goalsFor": gf,
            "goalsAgainst": ga,
            "league_id": lg_id,
        })

    n = len(sample)
    if n == 0:
        return None

    valid_st = [g for g in st_goals_scored if g is not None]
    valid_st_conceded = [g for g in st_goals_conceded if g is not None]

    return {
        "leagues_used": sorted(leagues_used),
        "sample_size": n,
        "record": {"won": wins, "draw": draws, "lost": losses},
        "goals_for": {
            "avg_per_match": round(sum(scored) / n, 2),
            "matches_with_2plus_goals": over_15_scored,
            "matches_with_2plus_goals_pct": round(over_15_scored / n * 100, 1),
            "matches_with_3plus_goals": over_25_scored,
            "matches_with_3plus_goals_pct": round(over_25_scored / n * 100, 1),
            "failed_to_score_count": failed_to_score,
            "failed_to_score_pct": round(failed_to_score / n * 100, 1),
        },
        "goals_against": {
            "avg_per_match": round(sum(conceded) / n, 2),
            "clean_sheets": clean_sheets,
            "clean_sheets_pct": round(clean_sheets / n * 100, 1),
        },
        "btts_pct": round(btts / n * 100, 1),
        "goals_by_half": {
            "avg_first_half": round(sum(ht_goals_scored) / n, 2),
            "avg_second_half": round(sum(valid_st) / len(valid_st), 2) if valid_st else None,
            "avg_first_half_conceded": round(sum(ht_goals_conceded) / n, 2),
            "avg_second_half_conceded": round(sum(valid_st_conceded) / len(valid_st_conceded), 2) if valid_st_conceded else None,
            "note": "Promedio de goles propios y del rival, anotados/recibidos en cada mitad.",
        },
        "tendency_over_2_5_team_goals": (
            "Sí, suele superar 2.5 goles propios por partido"
            if (sum(scored) / n) > 2.5 else
            "No, normalmente anota 2.5 goles propios o menos por partido"
        ),
        "recent_matches": sample,
    }


@extra_bp.route("/api/team-trends-multi/<int:team_id>", methods=["GET"])
def team_trends_multi(team_id):
    """
    Igual que /api/team-trends pero para SELECCIONES NACIONALES: junta
    partidos de varias ligas de api-sports.io (Mundial, Eurocopa,
    eliminatorias por confederación, amistosos) en vez de una sola
    competición de football-data.org, porque football-data.org no tiene
    cargadas las eliminatorias ni los amistosos de selecciones — solo el
    torneo final. Calcula las mismas métricas que /api/team-trends
    (goles a favor/contra, BTTS, clean sheets, goles por mitad) pero a
    partir de los datos de api-sports.io.

    Ejemplo: /api/team-trends-multi/2382?league_ids=1,4,32,34,29,30,31,33,10&last_n=10
    """
    last_n = int(request.args.get("last_n", 10))
    if last_n > 20:
        return jsonify({"error": "last_n máximo permitido: 20 (para cuidar la cuota diaria)."}), 400

    league_ids_param = request.args.get("league_ids", "")
    try:
        league_ids = [int(x) for x in league_ids_param.split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "league_ids inválido, esperaba una lista de números separados por coma."}), 400
    if not league_ids:
        return jsonify({"error": "Falta el parámetro ?league_ids=1,32,34,..."}), 400

    competition_filter = request.args.get("league_id_filter")
    if competition_filter:
        try:
            competition_filter = int(competition_filter)
        except ValueError:
            return jsonify({"error": "league_id_filter debe ser un número."}), 400

    selected, err = _fetch_apisports_multi_league_fixtures(team_id, league_ids, last_n)
    if err:
        return jsonify({"error": err}), 404

    if competition_filter:
        selected = [(lg_id, fx) for lg_id, fx in selected if lg_id == competition_filter]
        if not selected:
            return jsonify({
                "error": f"No se encontraron partidos para este equipo en la competición #{competition_filter}."
            }), 404

    team_name_real = selected[0][1]["teams"]["home"]["name"] if selected[0][1]["teams"]["home"]["id"] == team_id else selected[0][1]["teams"]["away"]["name"]

    result = _compute_apisports_trends_from_fixtures(selected, team_id)
    if result is None:
        return jsonify({
            "error": "Ninguno de los partidos encontrados tenía marcador final disponible para calcular tendencias."
        }), 404

    result["team_id"] = team_id
    result["team_matched"] = team_name_real
    result["leagues_searched"] = league_ids
    result["league_id_filter_applied"] = competition_filter

    # selected ya viene ordenado del más reciente al más viejo (heredado
    # de _fetch_apisports_multi_league_fixtures), así que las primeras 5
    # tuplas son los últimos 5 partidos reales en el tiempo.
    if len(selected) >= 5:
        short_window = _compute_apisports_trends_from_fixtures(selected[:5], team_id)
        long_window = _compute_apisports_trends_from_fixtures(selected, team_id)
        result["form_comparison"] = {
            "short_window_size": short_window["sample_size"],
            "long_window_size": long_window["sample_size"],
            "short_window": {
                "goals_for_avg": short_window["goals_for"]["avg_per_match"],
                "goals_against_avg": short_window["goals_against"]["avg_per_match"],
                "record": short_window["record"],
                "btts_pct": short_window["btts_pct"],
                "clean_sheets_pct": short_window["goals_against"]["clean_sheets_pct"],
            },
            "long_window": {
                "goals_for_avg": long_window["goals_for"]["avg_per_match"],
                "goals_against_avg": long_window["goals_against"]["avg_per_match"],
                "record": long_window["record"],
                "btts_pct": long_window["btts_pct"],
                "clean_sheets_pct": long_window["goals_against"]["clean_sheets_pct"],
            },
        }
    else:
        result["form_comparison"] = None

    return jsonify(result)


# Traducción de las razones de baja más comunes que reporta api-sports.io
# (siempre vienen en inglés). No es exhaustiva, pero cubre el vocabulario
# real visto en la temporada — lo que no esté acá se muestra tal cual.
INJURY_REASON_ES = {
    "ankle injury": "Lesión de tobillo",
    "sprained ankle": "Esguince de tobillo",
    "calf injury": "Lesión de gemelo",
    "foot injury": "Lesión de pie",
    "groin injury": "Lesión de ingle",
    "hamstring injury": "Lesión de isquiotibiales",
    "illness": "Enfermedad",
    "inactive": "Inactivo",
    "injured doubtful": "Lesionado, en duda",
    "injury": "Lesión",
    "jumpers knee": "Tendinitis rotuliana",
    "knee injury": "Lesión de rodilla",
    "knock": "Golpe / contusión",
    "lacking match fitness": "Sin ritmo de competencia",
    "leg injury": "Lesión de pierna",
    "muscle injury": "Lesión muscular",
    "shoulder injury": "Lesión de hombro",
    "thigh injury": "Lesión de muslo",
    "thigh problems": "Problemas en el muslo",
    "wound": "Herida / corte",
    "yellow cards": "Suspendido por acumulación de amarillas",
    "red card": "Suspendido por tarjeta roja",
    "suspended": "Suspendido",
    "rest": "Descanso / rotación",
    "concussion": "Conmoción cerebral",
    "back injury": "Lesión de espalda",
    "hip injury": "Lesión de cadera",
    "wrist injury": "Lesión de muñeca",
    "personal reasons": "Motivos personales",
    "covid-19": "COVID-19",
}


def _translate_injury_reason(reason):
    if not reason:
        return reason
    return INJURY_REASON_ES.get(reason.strip().lower(), reason)


@extra_bp.route("/api/team-injuries/<int:team_id>", methods=["GET"])
def team_injuries(team_id):
    """
    Lesionados y suspendidos de un equipo de cara a su PRÓXIMO partido
    programado. Busca el siguiente fixture del equipo y trae las bajas
    asociadas a ese partido específico.

    OJO — limitación real de los datos, no del código: api-sports.io solo
    tiene esta información poblada cuando falta POCO para el partido (días,
    no meses). Si el próximo partido programado está lejos en el
    calendario (por ejemplo, recién termina una temporada y el próximo
    partido oficial es en 1-2 meses), es normal y esperable que la
    respuesta venga vacía aunque el equipo sí tenga jugadores lesionados
    en este momento — esa info todavía no se cargó para ese fixture
    puntual. No es un error, es cómo migaja la cobertura de este dato.

    Como respaldo, si el fixture específico no tiene nada cargado, también
    devolvemos las bajas reportadas más recientemente en la temporada
    (puede no aplicar exactamente al próximo partido, se marca aparte).

    Ejemplo: /api/team-injuries/42
    """
    next_data, err = fetch_apisports("/fixtures", params={"team": team_id, "next": 1}, ttl=300)
    if err:
        return jsonify({"error": err}), 502

    next_fixtures = next_data.get("response", [])
    if not next_fixtures:
        return jsonify({
            "team_id": team_id,
            "next_fixture": None,
            "injuries_for_next_fixture": [],
            "recent_injuries_fallback": [],
            "note": "No se encontró un próximo partido programado para este equipo en api-sports.io.",
        })

    nf = next_fixtures[0]
    next_fixture_info = {
        "fixture_id": nf["fixture"]["id"],
        "date": nf["fixture"]["date"][:10],
        "opponent": (
            nf["teams"]["away"]["name"] if nf["teams"]["home"]["id"] == team_id
            else nf["teams"]["home"]["name"]
        ),
        "venue": "local" if nf["teams"]["home"]["id"] == team_id else "visitante",
        "league_name": nf["league"]["name"],
    }

    def _format_injury(inj):
        return {
            "player_name": inj["player"]["name"],
            "photo": inj["player"].get("photo"),
            "type": inj["player"].get("type"),
            "reason": _translate_injury_reason(inj["player"].get("reason")),
            "reason_raw": inj["player"].get("reason"),
        }

    inj_data, err2 = fetch_apisports(
        "/injuries", params={"fixture": next_fixture_info["fixture_id"]}, ttl=300
    )
    injuries_for_next_fixture = []
    if not err2:
        injuries_for_next_fixture = [
            _format_injury(inj) for inj in inj_data.get("response", [])
            if inj["team"]["id"] == team_id and inj["player"].get("name")
        ]

    recent_injuries_fallback = []
    if not injuries_for_next_fixture:
        # No hay nada cargado todavía para el próximo partido específico.
        # Probamos el año de temporada europea (año actual - 1, válido para
        # clubes con temporada agosto-mayo) y si no hay nada, el año
        # calendario actual (válido para selecciones, que juegan por año
        # natural) — sin saber de antemano si team_id es un club o una
        # selección, probamos ambos y nos quedamos con el que traiga algo.
        current_year = datetime.now(timezone.utc).year
        all_injuries = []
        for season_guess in (current_year - 1, current_year):
            season_data, err3 = fetch_apisports(
                "/injuries", params={"team": team_id, "season": season_guess}, ttl=300
            )
            if not err3 and season_data.get("response"):
                all_injuries = season_data["response"]
                break
        all_injuries.sort(key=lambda i: i["fixture"]["date"], reverse=True)
        seen_players = set()
        for inj in all_injuries:
            if not inj["player"].get("name"):
                continue  # api-sports.io a veces devuelve registros sin nombre, los ignoramos
            pid = inj["player"]["id"]
            if pid in seen_players:
                continue
            seen_players.add(pid)
            recent_injuries_fallback.append(_format_injury(inj))
            if len(recent_injuries_fallback) >= 10:
                break

    if injuries_for_next_fixture:
        note = None
    elif recent_injuries_fallback:
        note = (
            "Todavía no hay bajas confirmadas específicamente para el próximo partido "
            "(normal si falta más de unos días). Se muestran las últimas bajas reportadas "
            "en la temporada como referencia, puede que no coincidan exactamente con "
            "quién está disponible para este partido en particular."
        )
    else:
        note = (
            "No hay datos de lesionados/suspendidos disponibles para este equipo en "
            "api-sports.io. Es más común en selecciones nacionales y equipos de ligas "
            "menores — la cobertura de este dato es más completa en clubes de ligas top."
        )

    return jsonify({
        "team_id": team_id,
        "next_fixture": next_fixture_info,
        "injuries_for_next_fixture": injuries_for_next_fixture,
        "recent_injuries_fallback": recent_injuries_fallback,
        "note": note,
    })


@extra_bp.route("/api/head-to-head-apisports", methods=["GET"])
def head_to_head_apisports():
    """
    Historial de enfrentamientos directos entre dos equipos buscando en
    TODAS las competiciones a la vez, via api-sports.io. Pensado para
    selecciones nacionales (donde el head-to-head de football-data.org
    solo busca dentro de una competición y se queda corto), pero también
    funciona para clubes.

    Requiere los IDs numéricos de api-sports.io de ambos equipos — el
    frontend los resuelve previamente con /api/apisports/search-team.

    Ejemplo: /api/head-to-head-apisports?team1_id=26&team2_id=6&last_n=10
    """
    try:
        team1_id = int(request.args.get("team1_id", ""))
        team2_id = int(request.args.get("team2_id", ""))
    except (ValueError, TypeError):
        return jsonify({"error": "Se requieren ?team1_id=<id> y ?team2_id=<id> numéricos."}), 400

    last_n = int(request.args.get("last_n", 10))
    team1_name = request.args.get("team1_name", f"Equipo #{team1_id}")
    team2_name = request.args.get("team2_name", f"Equipo #{team2_id}")

    data, err = fetch_apisports(
        "/fixtures/headtohead",
        params={"h2h": f"{team1_id}-{team2_id}", "last": last_n, "status": "FT"},
        ttl=600,
    )
    if err:
        return jsonify({"error": err}), 502

    fixtures = data.get("response", [])
    if not fixtures:
        return jsonify({
            "error": (
                f"No se encontraron enfrentamientos directos entre "
                f"'{team1_name}' y '{team2_name}' en api-sports.io."
            )
        }), 404

    team1_wins = team2_wins = draws = 0
    team1_goals = team2_goals = 0
    sample = []

    for fx in fixtures:
        home_id = fx["teams"]["home"]["id"]
        home_name = fx["teams"]["home"]["name"]
        away_name = fx["teams"]["away"]["name"]
        home_goals = fx["goals"]["home"]
        away_goals = fx["goals"]["away"]
        if home_goals is None or away_goals is None:
            continue

        t1_is_home = (home_id == team1_id)
        t1_goals = home_goals if t1_is_home else away_goals
        t2_goals = away_goals if t1_is_home else home_goals

        team1_goals += t1_goals
        team2_goals += t2_goals

        winner = fx["teams"]["home"].get("winner")
        if winner is None:
            draws += 1
        elif (winner and t1_is_home) or (not winner and not t1_is_home):
            team1_wins += 1
        else:
            team2_wins += 1

        sample.append({
            "date": fx["fixture"]["date"][:10],
            "competition": fx["league"]["name"],
            "home_team": home_name,
            "away_team": away_name,
            "home_goals": home_goals,
            "away_goals": away_goals,
            "t1_goals": t1_goals,
            "t2_goals": t2_goals,
            "venue": fx["fixture"].get("venue", {}).get("name") or "—",
        })

    n = len(sample)
    return jsonify({
        "team1_id": team1_id,
        "team2_id": team2_id,
        "team1_name": team1_name,
        "team2_name": team2_name,
        "matches_found": n,
        "summary": {
            "team1_wins": team1_wins,
            "team2_wins": team2_wins,
            "draws": draws,
            "team1_goals_total": team1_goals,
            "team2_goals_total": team2_goals,
            "team1_goals_avg": round(team1_goals / n, 2) if n else 0,
            "team2_goals_avg": round(team2_goals / n, 2) if n else 0,
        },
        "matches": sample,
    })



# ─────────────────────────────────────────────────────────────────────────────
# ÁRBITROS — Base de datos de árbitros con estadísticas históricas
# Fuente: kickoffscore.com / statshub.com / valuestats.com (datos públicos)
# Actualizado: junio 2026
# ─────────────────────────────────────────────────────────────────────────────

REFEREE_DB = {
    # ─── 50 ÁRBITROS CONFIRMADOS DEL MUNDIAL 2026 ───
    # Fuente: footymetrics.com (datos reales de carrera, junio 2026)
    # yellows/reds/cards = promedios por partido en carrera completa

    # ── UEFA (15) ──────────────────────────────────────────────────────────
    "michael oliver": {
        "name": "Michael Oliver", "country": "Inglaterra", "confederation": "UEFA",
        "yellows_per_game": 3.7, "reds_per_game": 0.14, "cards_per_game": 3.8, "fouls_per_game": 22.8, "matches": 251,
        "style": "permisivo", "style_label": "🟢 Permisivo — 3.7 amarillas/partido",
        "cards_market_note": "Límite para 'Más de 3.5 tarjetas'. Arriesgado con él.",
    },
    "anthony taylor": {
        "name": "Anthony Taylor", "country": "Inglaterra", "confederation": "UEFA",
        "yellows_per_game": 3.9, "reds_per_game": 0.16, "cards_per_game": 4.1, "fouls_per_game": 21.4, "matches": 263,
        "style": "activo", "style_label": "🟡 Activo — 3.9 amarillas/partido",
        "cards_market_note": "'Más de 3.5 tarjetas' favorable con Taylor.",
    },
    "szymon marciniak": {
        "name": "Szymon Marciniak", "country": "Polonia", "confederation": "UEFA",
        "yellows_per_game": 4.3, "reds_per_game": 0.13, "cards_per_game": 4.4, "fouls_per_game": 25.2, "matches": 214,
        "style": "activo", "style_label": "🟡 Activo — 4.3 amarillas/partido",
        "cards_market_note": "Árbitro de la final del 2022. 'Más de 3.5 tarjetas' muy probable.",
    },
    "francois letexier": {
        "name": "François Letexier", "country": "Francia", "confederation": "UEFA",
        "yellows_per_game": 4.0, "reds_per_game": 0.22, "cards_per_game": 4.2, "fouls_per_game": 22.5, "matches": 183,
        "style": "activo", "style_label": "🟡 Activo — 4.0 amarillas, muchas rojas (0.22/partido)",
        "cards_market_note": "Alto promedio de rojas. Mercado de tarjetas muy favorable.",
    },
    "danny makkelie": {
        "name": "Danny Makkelie", "country": "Curaçao", "confederation": "UEFA",
        "yellows_per_game": 3.4, "reds_per_game": 0.15, "cards_per_game": 3.4, "fouls_per_game": 22.3, "matches": 255,
        "style": "permisivo", "style_label": "🟢 Permisivo — 3.4 amarillas/partido",
        "cards_market_note": "Makkelie es permisivo. Cuidado con el mercado de tarjetas.",
    },
    "clement turpin": {
        "name": "Clément Turpin", "country": "Francia", "confederation": "UEFA",
        "yellows_per_game": 3.2, "reds_per_game": 0.23, "cards_per_game": 3.4, "fouls_per_game": 23.4, "matches": 205,
        "style": "permisivo", "style_label": "🟢 Permisivo en amarillas (3.2) pero muchas rojas (0.23)",
        "cards_market_note": "Pocas amarillas pero rojas frecuentes. Mercado de tarjetas incierto.",
    },
    "felix zwayer": {
        "name": "Felix Zwayer", "country": "Alemania", "confederation": "UEFA",
        "yellows_per_game": 4.7, "reds_per_game": 0.15, "cards_per_game": 4.7, "fouls_per_game": 22.7, "matches": 199,
        "style": "estricto", "style_label": "🔴 Estricto — 4.7 amarillas/partido",
        "cards_market_note": "'Más de 3.5 tarjetas' muy probable con Zwayer.",
    },
    "istvan kovacs": {
        "name": "Istvan Kovacs", "country": "Rumania", "confederation": "UEFA",
        "yellows_per_game": 5.0, "reds_per_game": 0.26, "cards_per_game": 5.0, "fouls_per_game": 25.3, "matches": 197,
        "style": "muy estricto", "style_label": "🔴🔴 Muy estricto — 5.0 amarillas/partido (más alto UEFA)",
        "cards_market_note": "'Más de 3.5 tarjetas' casi seguro con Kovacs.",
    },
    "maurizio mariani": {
        "name": "Maurizio Mariani", "country": "Italia", "confederation": "UEFA",
        "yellows_per_game": 4.0, "reds_per_game": 0.20, "cards_per_game": 4.0, "fouls_per_game": 25.2, "matches": 183,
        "style": "activo", "style_label": "🟡 Activo — 4.0 amarillas/partido",
        "cards_market_note": "Supera 3.5 tarjetas con regularidad.",
    },
    "alejandro hernandez hernandez": {
        "name": "Alejandro Hernández Hernández", "country": "España", "confederation": "UEFA",
        "yellows_per_game": 5.2, "reds_per_game": 0.23, "cards_per_game": 5.2, "fouls_per_game": 25.4, "matches": 165,
        "style": "muy estricto", "style_label": "🔴🔴 Muy estricto — 5.2 amarillas/partido",
        "cards_market_note": "El árbitro español del Mundial, muy estricto. Casi seguro supera 3.5 tarjetas.",
    },
    "sandro scharer": {
        "name": "Sandro Schärer", "country": "Suiza", "confederation": "UEFA",
        "yellows_per_game": 4.3, "reds_per_game": 0.24, "cards_per_game": 4.3, "fouls_per_game": 24.9, "matches": 153,
        "style": "estricto", "style_label": "🔴 Estricto — 4.3 amarillas/partido",
        "cards_market_note": "Estricto y con rojas frecuentes. Favorable para tarjetas.",
    },
    "espen eskas": {
        "name": "Espen Eskås", "country": "Noruega", "confederation": "UEFA",
        "yellows_per_game": 3.4, "reds_per_game": 0.10, "cards_per_game": 3.4, "fouls_per_game": 20.6, "matches": 147,
        "style": "permisivo", "style_label": "🟢 Muy permisivo — 3.4 amarillas/partido",
        "cards_market_note": "Uno de los más permisivos del torneo. Evitar mercado de tarjetas.",
    },
    "joao pinheiro": {
        "name": "João Pedro Pinheiro", "country": "Portugal", "confederation": "UEFA",
        "yellows_per_game": 4.6, "reds_per_game": 0.21, "cards_per_game": 4.6, "fouls_per_game": 25.4, "matches": 141,
        "style": "estricto", "style_label": "🔴 Estricto — 4.6 amarillas/partido",
        "cards_market_note": "'Más de 3.5 tarjetas' muy probable con Pinheiro.",
    },
    "slavko vincic": {
        "name": "Slavko Vinčić", "country": "Eslovenia", "confederation": "UEFA",
        "yellows_per_game": 3.9, "reds_per_game": 0.12, "cards_per_game": 3.9, "fouls_per_game": 25.4, "matches": 90,
        "style": "moderado", "style_label": "🟡 Moderado — 3.9 amarillas/partido",
        "cards_market_note": "Moderado. 'Más de 3.5 tarjetas' posible.",
    },
    "glenn nyberg": {
        "name": "Glenn Nyberg", "country": "Suecia", "confederation": "UEFA",
        "yellows_per_game": 3.5, "reds_per_game": 0.12, "cards_per_game": 3.5, "fouls_per_game": 24.8, "matches": 166,
        "style": "moderado", "style_label": "🟡 Moderado — 3.5 amarillas (justo en el límite)",
        "cards_market_note": "Límite para 'Más de 3.5 tarjetas'. Riesgo moderado.",
    },

    # ── CONMEBOL (12) ──────────────────────────────────────────────────────
    "dario herrera": {
        "name": "Darío Herrera", "country": "Argentina", "confederation": "CONMEBOL",
        "yellows_per_game": 5.5, "reds_per_game": 0.34, "cards_per_game": 5.5, "fouls_per_game": 25.0, "matches": 249,
        "style": "muy estricto", "style_label": "🔴🔴 Más estricto del torneo — 5.5 amarillas/partido",
        "cards_market_note": "El más estricto del Mundial 2026. Cualquier mercado de tarjetas es excelente con él.",
    },
    "facundo tello": {
        "name": "Facundo Tello", "country": "Argentina", "confederation": "CONMEBOL",
        "yellows_per_game": 4.7, "reds_per_game": 0.25, "cards_per_game": 4.7, "fouls_per_game": 23.9, "matches": 248,
        "style": "estricto", "style_label": "🔴 Estricto — 4.7 amarillas/partido",
        "cards_market_note": "'Más de 3.5 tarjetas' muy probable.",
    },
    "raphael claus": {
        "name": "Raphael Claus", "country": "Brasil", "confederation": "CONMEBOL",
        "yellows_per_game": 4.1, "reds_per_game": 0.19, "cards_per_game": 4.1, "fouls_per_game": 24.0, "matches": 225,
        "style": "activo", "style_label": "🟡 Activo — 4.1 amarillas/partido",
        "cards_market_note": "Supera 3.5 tarjetas con regularidad.",
    },
    "wilton sampaio": {
        "name": "Wilton Sampaio", "country": "Brasil", "confederation": "CONMEBOL",
        "yellows_per_game": 4.8, "reds_per_game": 0.23, "cards_per_game": 4.8, "fouls_per_game": 25.8, "matches": 222,
        "style": "estricto", "style_label": "🔴 Estricto — 4.8 amarillas/partido",
        "cards_market_note": "'Más de 3.5 tarjetas' muy probable.",
    },
    "gustavo tejera": {
        "name": "Gustavo Tejera", "country": "Uruguay", "confederation": "CONMEBOL",
        "yellows_per_game": 4.9, "reds_per_game": 0.28, "cards_per_game": 4.9, "fouls_per_game": 23.1, "matches": 188,
        "style": "estricto", "style_label": "🔴 Estricto — 4.9 amarillas/partido",
        "cards_market_note": "'Más de 3.5 tarjetas' muy probable.",
    },
    "yael falcon perez": {
        "name": "Yael Falcón Pérez", "country": "Argentina", "confederation": "CONMEBOL",
        "yellows_per_game": 5.3, "reds_per_game": 0.26, "cards_per_game": 5.3, "fouls_per_game": 25.8, "matches": 186,
        "style": "muy estricto", "style_label": "🔴🔴 Muy estricto — 5.3 amarillas/partido",
        "cards_market_note": "Extremadamente estricto. Cualquier mercado de tarjetas es favorable.",
    },
    "ramon abatti": {
        "name": "Ramon Abatti", "country": "Brasil", "confederation": "CONMEBOL",
        "yellows_per_game": 4.5, "reds_per_game": 0.25, "cards_per_game": 4.5, "fouls_per_game": 28.6, "matches": 181,
        "style": "estricto", "style_label": "🔴 Estricto — 4.5 amarillas + más faltas del torneo (28.6/partido)",
        "cards_market_note": "Muy favorable para tarjetas y faltas.",
    },
    "jesus valenzuela": {
        "name": "Jesús Valenzuela", "country": "Venezuela", "confederation": "CONMEBOL",
        "yellows_per_game": 5.1, "reds_per_game": 0.24, "cards_per_game": 5.1, "fouls_per_game": 27.3, "matches": 88,
        "style": "muy estricto", "style_label": "🔴🔴 Muy estricto — 5.1 amarillas/partido",
        "cards_market_note": "Muy estricto. 'Más de 3.5 tarjetas' casi seguro.",
    },
    "kevin ortega": {
        "name": "Kevin Ortega", "country": "Perú", "confederation": "CONMEBOL",
        "yellows_per_game": 4.9, "reds_per_game": 0.21, "cards_per_game": 4.9, "fouls_per_game": 25.6, "matches": 63,
        "style": "estricto", "style_label": "🔴 Estricto — 4.9 amarillas/partido",
        "cards_market_note": "'Más de 3.5 tarjetas' muy probable.",
    },
    "cristian garay": {
        "name": "Cristián Garay", "country": "Chile", "confederation": "CONMEBOL",
        "yellows_per_game": 4.0, "reds_per_game": 0.23, "cards_per_game": 4.0, "fouls_per_game": 22.1, "matches": 57,
        "style": "activo", "style_label": "🟡 Activo — 4.0 amarillas/partido",
        "cards_market_note": "Supera 3.5 tarjetas con regularidad.",
    },
    "andres rojas": {
        "name": "Andrés Rojas", "country": "Colombia", "confederation": "CONMEBOL",
        "yellows_per_game": 5.1, "reds_per_game": 0.29, "cards_per_game": 5.1, "fouls_per_game": 24.8, "matches": 56,
        "style": "muy estricto", "style_label": "🔴🔴 Muy estricto — 5.1 amarillas/partido",
        "cards_market_note": "Extremadamente estricto para su poca experiencia internacional.",
    },
    "juan benitez": {
        "name": "Juan Benítez", "country": "Paraguay", "confederation": "CONMEBOL",
        "yellows_per_game": 3.6, "reds_per_game": 0.17, "cards_per_game": 3.6, "fouls_per_game": 23.2, "matches": 36,
        "style": "moderado", "style_label": "🟡 Moderado — 3.6 amarillas/partido",
        "cards_market_note": "Moderado. 'Más de 3.5 tarjetas' posible.",
    },

    # ── CONCACAF (9) ───────────────────────────────────────────────────────
    "ismail elfath": {
        "name": "Ismail Elfath", "country": "EE.UU.", "confederation": "CONCACAF",
        "yellows_per_game": 4.3, "reds_per_game": 0.23, "cards_per_game": 4.5, "fouls_per_game": 24.6, "matches": 94,
        "style": "activo", "style_label": "🟡 Activo — 4.3 amarillas/partido",
        "cards_market_note": "'Más de 3.5 tarjetas' favorable.",
    },
    "cesar ramos": {
        "name": "César Ramos", "country": "México", "confederation": "CONCACAF",
        "yellows_per_game": 4.4, "reds_per_game": 0.41, "cards_per_game": 4.4, "fouls_per_game": 23.5, "matches": 174,
        "style": "estricto", "style_label": "🔴 Estricto — más rojas del torneo (0.41/partido)",
        "cards_market_note": "El árbitro con más rojas del Mundial. Muy favorable para tarjetas.",
    },
    "drew fischer": {
        "name": "Drew Fischer", "country": "Canadá", "confederation": "CONCACAF",
        "yellows_per_game": 3.6, "reds_per_game": 0.15, "cards_per_game": 3.6, "fouls_per_game": 23.3, "matches": 123,
        "style": "moderado", "style_label": "🟡 Moderado — 3.6 amarillas/partido",
        "cards_market_note": "Moderado. 'Más de 3.5 tarjetas' posible.",
    },
    "tori penso": {
        "name": "Tori Penso", "country": "EE.UU.", "confederation": "CONCACAF",
        "yellows_per_game": 3.8, "reds_per_game": 0.09, "cards_per_game": 3.8, "fouls_per_game": 21.3, "matches": 78,
        "style": "moderado", "style_label": "🟡 Moderado — 3.8 amarillas/partido (árbitro femenina)",
        "cards_market_note": "Moderada. 'Más de 3.5 tarjetas' posible pero no seguro.",
    },
    "katia garcia": {
        "name": "Katia García", "country": "México", "confederation": "CONCACAF",
        "yellows_per_game": 4.2, "reds_per_game": 0.06, "cards_per_game": 4.2, "fouls_per_game": 22.4, "matches": 34,
        "style": "activo", "style_label": "🟡 Activa — 4.2 amarillas/partido (árbitro femenina)",
        "cards_market_note": "Activa pero pocas rojas. Favorable para amarillas.",
    },
    "ivan barton": {
        "name": "Ivan Barton", "country": "El Salvador", "confederation": "CONCACAF",
        "yellows_per_game": 3.8, "reds_per_game": 0.36, "cards_per_game": 3.8, "fouls_per_game": 22.9, "matches": 28,
        "style": "moderado", "style_label": "🟡 Moderado en amarillas pero muchas rojas (0.36/partido)",
        "cards_market_note": "Pocas amarillas pero muy alto en rojas. Mercado de tarjetas incierto.",
    },
    "oshane nation": {
        "name": "Oshane Nation", "country": "Jamaica", "confederation": "CONCACAF",
        "yellows_per_game": 3.5, "reds_per_game": 0.14, "cards_per_game": 3.5, "fouls_per_game": 24.2, "matches": 14,
        "style": "moderado", "style_label": "🟡 Moderado — 3.5 amarillas (poca muestra internacional)",
        "cards_market_note": "Poca experiencia internacional. Dato a tomar con cautela.",
    },
    "juan calderon": {
        "name": "Juan Calderón", "country": "Costa Rica", "confederation": "CONCACAF",
        "yellows_per_game": 3.3, "reds_per_game": 0.10, "cards_per_game": 3.3, "fouls_per_game": 23.4, "matches": 10,
        "style": "permisivo", "style_label": "🟢 Permisivo — 3.3 amarillas (muestra muy pequeña)",
        "cards_market_note": "Muy pocos partidos de referencia. Dato no confiable.",
    },
    "hector martinez": {
        "name": "Héctor Martínez", "country": "Honduras", "confederation": "CONCACAF",
        "yellows_per_game": 3.2, "reds_per_game": 0.33, "cards_per_game": 3.2, "fouls_per_game": 21.8, "matches": 6,
        "style": "permisivo", "style_label": "🟢 Permisivo en amarillas — pero pocas rojas también (solo 6 partidos)",
        "cards_market_note": "Muestra de solo 6 partidos. Dato no confiable.",
    },

    # ── AFC (7) ────────────────────────────────────────────────────────────
    "alireza faghani": {
        "name": "Alireza Faghani", "country": "Irán/Australia", "confederation": "AFC",
        "yellows_per_game": 3.7, "reds_per_game": 0.11, "cards_per_game": 3.7, "fouls_per_game": 19.7, "matches": 141,
        "style": "moderado", "style_label": "🟡 Moderado — 3.7 amarillas/partido",
        "cards_market_note": "Moderado. 'Más de 3.5 tarjetas' posible pero no seguro.",
    },
    "adham makhadmeh": {
        "name": "Adham Makhadmeh", "country": "Jordania", "confederation": "AFC",
        "yellows_per_game": 3.4, "reds_per_game": 0.10, "cards_per_game": 3.5, "fouls_per_game": 21.9, "matches": 49,
        "style": "permisivo", "style_label": "🟢 Permisivo — 3.4 amarillas/partido",
        "cards_market_note": "Permisivo. Cuidado con el mercado de tarjetas.",
    },
    "abdulrahman al jassim": {
        "name": "Abdulrahman Al Jassim", "country": "Qatar", "confederation": "AFC",
        "yellows_per_game": 4.3, "reds_per_game": 0.24, "cards_per_game": 4.3, "fouls_per_game": 22.8, "matches": 108,
        "style": "activo", "style_label": "🟡 Activo — 4.3 amarillas/partido",
        "cards_market_note": "'Más de 3.5 tarjetas' favorable.",
    },
    "ning ma": {
        "name": "Ning Ma", "country": "China", "confederation": "AFC",
        "yellows_per_game": 4.2, "reds_per_game": 0.33, "cards_per_game": 4.2, "fouls_per_game": 25.6, "matches": 108,
        "style": "activo", "style_label": "🟡 Activo — 4.2 amarillas + muchas rojas (0.33/partido)",
        "cards_market_note": "Activo con muchas rojas. Muy favorable para mercado de tarjetas.",
    },
    "omar al ali": {
        "name": "Omar Al Ali", "country": "Emiratos Árabes", "confederation": "AFC",
        "yellows_per_game": 3.7, "reds_per_game": 0.27, "cards_per_game": 3.7, "fouls_per_game": 23.7, "matches": 102,
        "style": "moderado", "style_label": "🟡 Moderado en amarillas pero muchas rojas (0.27/partido)",
        "cards_market_note": "Moderado en amarillas, muchas rojas. Mercado de tarjetas posible.",
    },
    "yusuke araki": {
        "name": "Yusuke Araki", "country": "Japón", "confederation": "AFC",
        "yellows_per_game": 2.9, "reds_per_game": 0.14, "cards_per_game": 2.9, "fouls_per_game": 21.2, "matches": 113,
        "style": "permisivo", "style_label": "🟢 Muy permisivo — 2.9 amarillas/partido (el más bajo del torneo)",
        "cards_market_note": "El árbitro más permisivo del Mundial. Evitar cualquier mercado de tarjetas con él.",
    },
    "ilgiz tantashev": {
        "name": "Ilgiz Tantashev", "country": "Uzbekistán", "confederation": "AFC",
        "yellows_per_game": 3.5, "reds_per_game": 0.27, "cards_per_game": 3.5, "fouls_per_game": 21.1, "matches": 48,
        "style": "moderado", "style_label": "🟡 Moderado — 3.5 amarillas, muchas rojas (0.27/partido)",
        "cards_market_note": "Moderado en amarillas pero muchas rojas. Mercado incierto.",
    },

    # ── CAF (6) ────────────────────────────────────────────────────────────
    "amin omar": {
        "name": "Amin Omar", "country": "Egipto", "confederation": "CAF",
        "yellows_per_game": 3.8, "reds_per_game": 0.20, "cards_per_game": 3.8, "fouls_per_game": 17.1, "matches": 117,
        "style": "moderado", "style_label": "🟡 Moderado — 3.8 amarillas/partido, pocas faltas (17.1)",
        "cards_market_note": "Moderado. 'Más de 3.5 tarjetas' posible.",
    },
    "jalal jayed": {
        "name": "Jalal Jayed", "country": "Marruecos", "confederation": "CAF",
        "yellows_per_game": 3.7, "reds_per_game": 0.14, "cards_per_game": 3.7, "fouls_per_game": 15.4, "matches": 85,
        "style": "moderado", "style_label": "🟡 Moderado — 3.7 amarillas, pocas faltas (15.4/partido)",
        "cards_market_note": "Moderado. 'Más de 3.5 tarjetas' posible.",
    },
    "abongile tom": {
        "name": "Abongile Tom", "country": "Sudáfrica", "confederation": "CAF",
        "yellows_per_game": 4.6, "reds_per_game": 0.21, "cards_per_game": 4.6, "fouls_per_game": 21.2, "matches": 33,
        "style": "estricto", "style_label": "🔴 Estricto — 4.6 amarillas/partido",
        "cards_market_note": "'Más de 3.5 tarjetas' muy probable.",
    },
    "dahane beida": {
        "name": "Dahane Beida", "country": "Mauritania", "confederation": "CAF",
        "yellows_per_game": 3.3, "reds_per_game": 0.10, "cards_per_game": 3.3, "fouls_per_game": 27.2, "matches": 31,
        "style": "permisivo", "style_label": "🟢 Permisivo — 3.3 amarillas (pero muchas faltas: 27.2)",
        "cards_market_note": "Permisivo en tarjetas pero pita muchas faltas. Mercado de tarjetas arriesgado.",
    },
    "mustapha ghorbal": {
        "name": "Mustapha Ghorbal", "country": "Argelia", "confederation": "CAF",
        "yellows_per_game": 4.0, "reds_per_game": 0.26, "cards_per_game": 4.0, "fouls_per_game": 29.3, "matches": 31,
        "style": "activo", "style_label": "🟡 Activo — 4.0 amarillas + más faltas del torneo junto con Abatti (29.3)",
        "cards_market_note": "Activo en tarjetas y récord de faltas. Muy favorable para tarjetas.",
    },
    "pierre atcho": {
        "name": "Pierre Atcho", "country": "Gabón", "confederation": "CAF",
        "yellows_per_game": 3.4, "reds_per_game": 0.11, "cards_per_game": 3.4, "fouls_per_game": 22.6, "matches": 37,
        "style": "permisivo", "style_label": "🟢 Permisivo — 3.4 amarillas/partido",
        "cards_market_note": "Permisivo. Cuidado con el mercado de tarjetas.",
    },

    # ── OFC (1) ────────────────────────────────────────────────────────────
    "campbell kawana-waugh": {
        "name": "Campbell Kawana-Waugh", "country": "Nueva Zelanda", "confederation": "OFC",
        "yellows_per_game": 3.0, "reds_per_game": 0.0, "cards_per_game": 3.0, "fouls_per_game": 0.0, "matches": 2,
        "style": "sin datos", "style_label": "⚪ Sin datos suficientes — solo 2 partidos en la base",
        "cards_market_note": "Dato no confiable con solo 2 partidos. No usar para análisis.",
    },
}

def lookup_referee(name: str) -> dict | None:
    """Busca un árbitro por nombre (insensible a mayúsculas y acentos)."""
    if not name:
        return None
    key = name.strip().lower()
    # Búsqueda exacta
    if key in REFEREE_DB:
        return REFEREE_DB[key]
    # Búsqueda parcial
    for db_key, data in REFEREE_DB.items():
        if key in db_key or db_key in key:
            return data
    return None


@extra_bp.route("/api/referee/lookup", methods=["GET"])
def referee_lookup():
    """
    Busca estadísticas históricas de un árbitro por nombre.
    Devuelve perfil de tarjetas, estilo y nota para el mercado.

    Ejemplo: /api/referee/lookup?name=Michael Oliver
    """
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Falta el parámetro ?name=<nombre del árbitro>"}), 400

    ref = lookup_referee(name)
    if ref:
        return jsonify({
            "found": True,
            "referee": ref,
            "cards_threshold_35": {
                "likely": ref["cards_per_game"] > 3.5,
                "avg_vs_threshold": round(ref["cards_per_game"] - 3.5, 2),
                "recommendation": (
                    "✅ Favorable para 'Más de 3.5 tarjetas'" if ref["cards_per_game"] > 3.8
                    else "⚠️ Límite — riesgo moderado para 'Más de 3.5 tarjetas'" if ref["cards_per_game"] > 3.3
                    else "❌ Desfavorable para 'Más de 3.5 tarjetas'"
                )
            }
        })
    else:
        return jsonify({
            "found": False,
            "name_searched": name,
            "message": f"No tenemos estadísticas para '{name}' en nuestra base de datos. Podés buscar manualmente en kickoffscore.com/referees",
            "url": f"https://kickoffscore.com/referees/{name.lower().replace(' ', '-')}",
            "known_referees": list(REFEREE_DB.keys()),
        })


@extra_bp.route("/api/referee/all", methods=["GET"])
def referee_all():
    """Devuelve todos los árbitros en la base de datos."""
    return jsonify({
        "count": len(REFEREE_DB),
        "referees": list(REFEREE_DB.values()),
    })
