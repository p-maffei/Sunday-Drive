import os
import json
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="Sunday Drive API")

# Lets your UI (on a different URL) call this API. Tighten later.
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# Only these names are ever allowed into SQL (column names can't be parameterized)
PREF_COLUMNS = ["curvy", "backroads", "urban"]
DEFAULT_PREFS = ["curvy", "backroads"]   # used when wandering with nothing selected

# Area you actually loaded: west, south, east, north
BBOX = (-119.122009, 33.978670, -118.499222, 34.242459)

# --- /wander tuning knobs ---
WINDOW_LOW = 0.2        # waypoint's fastest A->W->B trip must be >= 20% of budget...
WINDOW_HIGH = 0.8       # ...and <= 80% (fun routes run longer than the fastest trip)
MAX_CANDIDATES = 20     # how many waypoints to fully route (more = slower)
REUSE_CAP_M = 1000      # reject routes that retrace more than 1 km
MAX_OVERLAP = 0.8       # drop an option if 80%+ of it matches a better option
MAX_OPTIONS = 3

# Row layout used by the helpers below:
# (length_m, curvy, backroads, urban, geojson_text, time_s)
SCORE_IDX = {"curvy": 1, "backroads": 2, "urban": 3}


class RouteRequest(BaseModel):
    start: list[float] = Field(min_length=2, max_length=2)   # [lon, lat]
    end: list[float] = Field(min_length=2, max_length=2)     # [lon, lat]
    preferences: list[str] = []
    strength: float = Field(default=7, ge=0, le=30)


class FastestRequest(BaseModel):
    start: list[float] = Field(min_length=2, max_length=2)
    end: list[float] = Field(min_length=2, max_length=2)


class WanderRequest(BaseModel):
    start: list[float] = Field(min_length=2, max_length=2)   # [lon, lat]
    end: list[float] = Field(min_length=2, max_length=2)     # [lon, lat]
    extra_min: int = Field(default=0, ge=0, le=300)          # minutes added to the fastest trip
    preferences: list[str] = []
    strength: float = Field(default=7, ge=0, le=30)


def get_conn():
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        dbname="postgres",
        connect_timeout=10,
    )


def nearest_node(cur, lon, lat):
    cur.execute(
        """SELECT n.id FROM nodes n
           WHERE EXISTS (SELECT 1 FROM ways w
                         WHERE (w.source = n.id OR w.target = n.id)
                           AND w.hw NOT IN ('motorway', 'motorway_link'))
           ORDER BY n.geometry <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)
           LIMIT 1""",
        (lon, lat),
    )
    return cur.fetchone()[0]


def check_bbox(*points):
    for lon, lat in points:
        if not (BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]):
            raise HTTPException(
                400, "Point is outside the loaded area (Malibu / San Fernando Valley)."
            )


def clean_prefs(prefs):
    return [p for p in dict.fromkeys(prefs) if p in PREF_COLUMNS]


def fastest_rows(cur, a, b):
    cur.execute(
        """SELECT w.length_m, w.curvy, w.backroads, w.urban,
                  ST_AsGeoJSON(w.geometry), w.time_s
           FROM pgr_dijkstra(
                  'SELECT id, source, target, time_s AS cost, reverse_cost FROM ways',
                  %s::bigint, %s::bigint, directed := true) d
           JOIN ways w ON w.id = d.edge
           WHERE d.edge <> -1
           ORDER BY d.seq""",
        (a, b),
    )
    return cur.fetchall()


def geojson_of(rows):
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": json.loads(r[4]), "properties": {}}
            for r in rows
        ],
    }


def pct_scores(rows):
    total = sum(r[0] for r in rows)
    return {
        p: (round(sum(r[0] * r[i] for r in rows) / total * 100) if total else 0)
        for p, i in SCORE_IDX.items()
    }


def fun_meters(rows, prefs):
    if not prefs:
        return 0.5 * sum(r[0] for r in rows)
    return sum(r[0] * sum(r[SCORE_IDX[p]] for p in prefs) / len(prefs) for r in rows)


def summarize(rows, prefs):
    total = sum(r[0] for r in rows)
    scores = pct_scores(rows)
    breakdown = {p: scores[p] for p in prefs}
    overall = round(sum(breakdown.values()) / len(breakdown)) if breakdown else None
    return {
        "distance_km": round(total / 1000, 1),
        "duration_min": round(sum(r[5] for r in rows) / 60),
        "match_pct": overall,
        "breakdown": breakdown,
        "geojson": geojson_of(rows),
    }


@app.get("/")
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/route")
def route(req: RouteRequest):
    # 1. Reject points outside the area you loaded
    check_bbox(req.start, req.end)

    # 2. Whitelist the preferences, then build the match expression
    prefs = clean_prefs(req.preferences)
    if prefs:
        match_sql = "(" + " + ".join(prefs) + f") / {len(prefs)}.0"
    else:
        match_sql = "0.5"

    edges_sql = f"""
        SELECT id, source, target,
               length_m * (1 + (1 - {match_sql}) * {float(req.strength)}) AS cost,
               reverse_cost
        FROM ways"""

    # 3. Snap points to the network and route
    with get_conn() as conn, conn.cursor() as cur:
        s = nearest_node(cur, *req.start)
        e = nearest_node(cur, *req.end)
        cur.execute(
            """SELECT w.length_m, w.curvy, w.backroads, w.urban,
                      ST_AsGeoJSON(w.geometry), w.time_s
               FROM pgr_dijkstra(%s, %s, %s, directed := true) d
               JOIN ways w ON w.id = d.edge
               WHERE d.edge <> -1
               ORDER BY d.seq""",
            (edges_sql, s, e),
        )
        rows = cur.fetchall()

    if not rows:
        raise HTTPException(404, "No route found between those points.")

    out = summarize(rows, prefs)
    return out


@app.post("/fastest")
def fastest(req: FastestRequest):
    check_bbox(req.start, req.end)
    with get_conn() as conn, conn.cursor() as cur:
        a = nearest_node(cur, *req.start)
        b = nearest_node(cur, *req.end)
        rows = fastest_rows(cur, a, b)
    if not rows:
        raise HTTPException(404, "No route found between those points.")
    s = summarize(rows, [])
    return {
        "fastest_min": s["duration_min"],
        "distance_km": s["distance_km"],
        "scores": pct_scores(rows),
        "geojson": s["geojson"],
    }


@app.post("/wander")
def wander(req: WanderRequest):
    check_bbox(req.start, req.end)
    selected = clean_prefs(req.preferences)
    wandering = req.extra_min > 0
    prefs = (selected or DEFAULT_PREFS) if wandering else selected
    defaulted = wandering and not selected
    pref_idx = {"curvy": 4, "backroads": 5, "urban": 6}   # columns in wander_candidates

    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '60s'")
            a = nearest_node(cur, *req.start)
            b = nearest_node(cur, *req.end)

            # 1. The fastest route is the baseline for everything else
            base_rows = fastest_rows(cur, a, b)
            if not base_rows:
                raise HTTPException(404, "No route found between those points.")
            base = summarize(base_rows, prefs)
            fastest_s = sum(r[5] for r in base_rows)
            fastest_min = base["duration_min"]
            fastest_info = {
                "duration_min": fastest_min,
                "distance_km": base["distance_km"],
                "scores": pct_scores(base_rows),
            }
            base_option = dict(
                base, rank=1, waypoint=None, extra_min=0,
                fun_km=round(fun_meters(base_rows, prefs) / 1000, 1), reused_km=0)

            def respond(options, note):
                return {
                    "fastest": fastest_info,
                    "extra_min": req.extra_min,
                    "budget_min": round(fastest_s / 60 + req.extra_min),
                    "style": prefs,
                    "style_defaulted": defaulted,
                    "note": note,
                    "options": options,
                }

            # 2. No extra time: the fastest route is the answer
            if not wandering:
                note = None
                if selected:
                    note = ("No extra time was added, so this is the fastest route. "
                            "Add time to get roads that match your style.")
                return respond([base_option], note)

            budget_s = fastest_s + req.extra_min * 60.0
            budget_min = budget_s / 60.0

            # 3. Candidate waypoints whose fastest trip A->W->B fits the window
            cur.execute(
                "SELECT * FROM wander_candidates(%s::bigint, %s::bigint, %s, %s)",
                (a, b, budget_s * WINDOW_LOW, budget_s * WINDOW_HIGH),
            )
            cand_rows = cur.fetchall()

            # 4. Rank by how well each waypoint's surroundings match the style,
            #    keeping only the best waypoint in each ~2 km grid cell
            best = {}
            for row in cand_rows:
                score = sum((row[pref_idx[p]] or 0) for p in prefs) / len(prefs)
                key = (row[1], row[2])
                if key not in best or score > best[key][0]:
                    best[key] = (score, row[0])
            ranked = sorted(best.values(), reverse=True)[:MAX_CANDIDATES]
            nodes_to_try = [n for _, n in ranked]
            if b not in nodes_to_try:
                nodes_to_try.append(b)   # the plain A->B styled route, as a baseline

            # 5. Route through every candidate in one query
            cur.execute(
                """SELECT c.node, r.minutes, r.length_m, r.fun_m, r.reused_m, r.edges, r.lens
                   FROM unnest(%s::bigint[]) AS c(node)
                   CROSS JOIN LATERAL (
                     SELECT sum(time_s) / 60.0 AS minutes,
                            sum(length_m) AS length_m,
                            coalesce(sum(match * length_m) FILTER (WHERE NOT reused), 0) AS fun_m,
                            coalesce(sum(length_m) FILTER (WHERE reused), 0) AS reused_m,
                            array_agg(edge ORDER BY leg, seq) AS edges,
                            array_agg(length_m ORDER BY leg, seq) AS lens
                     FROM plan_via(%s::bigint, c.node, %s::bigint, %s::text[], %s::double precision)
                   ) r""",
                (nodes_to_try, a, b, prefs, float(req.strength)),
            )
            routes = []
            for node, minutes, length_m, fun_m, reused_m, edges, lens in cur.fetchall():
                if not edges:
                    continue
                routes.append({"node": node, "minutes": minutes, "length_m": length_m,
                               "fun_m": fun_m, "reused_m": reused_m,
                               "edges": edges, "lens": lens})

            # 6. Keep routes that fit the budget and don't retrace, best first
            ok = [r for r in routes
                  if r["minutes"] <= budget_min and r["reused_m"] <= REUSE_CAP_M]
            if not ok:
                note = (f"Nothing matching your style fits in +{req.extra_min} min, "
                        "so this is the fastest route. Try adding more time.")
                return respond([base_option], note)
            ok.sort(key=lambda r: r["fun_m"], reverse=True)

            # 7. Drop near-duplicates (an option sharing 80%+ of its road with a better one)
            chosen = []
            for r in ok:
                lens_map = dict(zip(r["edges"], r["lens"]))
                mine = sum(lens_map.values())
                duplicate = False
                for c in chosen:
                    shared = sum(l for e, l in lens_map.items() if e in c["lens_map"])
                    if mine and shared / mine > MAX_OVERLAP:
                        duplicate = True
                        break
                if not duplicate:
                    r["lens_map"] = lens_map
                    chosen.append(r)
                if len(chosen) >= MAX_OPTIONS:
                    break

            # 8. Fetch geometry and scores for the chosen routes only
            all_edges = sorted({e for r in chosen for e in r["edges"]})
            cur.execute(
                """SELECT id, length_m, time_s, curvy, backroads, urban, ST_AsGeoJSON(geometry)
                   FROM ways WHERE id = ANY(%s::bigint[])""",
                (all_edges,),
            )
            info = {row[0]: row for row in cur.fetchall()}
            cur.execute(
                "SELECT id, x, y FROM nodes WHERE id = ANY(%s::bigint[])",
                ([r["node"] for r in chosen],),
            )
            coords = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    except psycopg.errors.QueryCanceled:
        raise HTTPException(504, "Route search took too long. Try less extra time.")

    # 9. Build the response
    options = []
    for rank, r in enumerate(chosen, start=1):
        rows = []
        for e in r["edges"]:
            x = info.get(e)
            if x:
                rows.append((x[1], x[3], x[4], x[5], x[6], x[2]))
        opt = summarize(rows, prefs)
        lon_lat = coords.get(r["node"])
        opt.update({
            "rank": rank,
            "waypoint": ({"lon": lon_lat[0], "lat": lon_lat[1]}
                         if lon_lat and r["node"] != b else None),
            "extra_min": max(0, opt["duration_min"] - fastest_min),
            "fun_km": round(r["fun_m"] / 1000, 1),
            "reused_km": round(r["reused_m"] / 1000, 1),
        })
        options.append(opt)

    used = max((o["extra_min"] for o in options), default=0)
    note = None
    if used < req.extra_min * 0.7:
        note = (f"The best drive found adds about {used} min over the fastest route. "
                "This area doesn't have enough connected good road to use more time.")

    return respond(options, note)