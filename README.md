# Sunday Drive

A driving-route generator that optimizes for how a road *feels*, not how fast it is.
Enter a start and an end, choose how much extra time you want to spend, and get
routes through the curvy, quiet roads in between instead of the highway.

Current coverage: Malibu, the Santa Monica Mountains, Calabasas / Thousand Oaks,
and the edges of the San Fernando Valley and Westside (about 46,000 directed road
segments).

## How it works

1. **Baseline.** The fastest route is computed first, by travel time.
2. **Extra time.** The user picks how many minutes they are willing to add on top of the
   fastest trip (0 to 300). At 0 they get the fastest route.
3. **Style.** Optional road preferences: curvy, backroads, urban. If the user adds
   time and picks nothing, the default is curvy + backroads.
4. **Wander search.** Preferences matter most in *where the route goes*, so the
   search picks candidate waypoints whose fastest A to W to B trip fits the time budget,
   routes through the best-matching ones, rejects routes that retrace the same road,
   removes near-duplicates, and returns up to three options.
5. **Report.** Each option shows its time, extra time versus fastest, distance,
   and how its style scores changed relative to the fastest route.

## Architecture

| Layer | Tech |
|---|---|
| Data | OpenStreetMap, downloaded with OSMnx |
| Database | Supabase Postgres + PostGIS + pgRouting |
| Pipeline | Python in Colab (download, stage), SQL (score, check) |
| API | FastAPI + psycopg3, deployed on Replit |
| App | Expo / React Native, MapLibre, OpenFreeMap basemap, Geoapify geocoding |

## Repository layout

    pipeline/   three Colab-ready scripts: download and stage, score and check, swap and rollback
    sql/        the routing functions (plan_via, wander_candidates)
    api/        the FastAPI service (main.py)

## Scoring

Every road segment (edge) has scores from 0 to 1:

- **curvy**: total heading change per kilometer. Geometry is projected to UTM 11N,
  each segment's azimuth is taken with `ST_Azimuth`, and the turning between
  consecutive segments is summed and divided by length. The raw value is mapped to 0..1
  by a **fixed piecewise-linear scale** (`score_scale` table, anchored to the original
  Malibu region), so scores do not shift when the region grows. Roads that are not
  through roads (residential, service) are capped at 0.3.
- **backroads** and **urban**: lookups by OSM road class (`road_class_score` table).
- **time_s**: length divided by speed (parsed `maxspeed`, else a class default),
  multiplied by 1.3 to account for real driving. Calibrated against Google Maps
  (direct Malibu route 20 min vs 15 raw; Calabasas trip on a Sunday morning
  25 min vs Google's 24 to 30).

Routing cost for a styled route:

    cost = length x (1 + (1 - match) x strength)

`match` is the average of the selected scores (0.5 if none selected). `strength`
(default 7) controls how far a route may detour to find better roads.

## Pipeline

The network is rebuilt by three repeatable parts, run in order:

1. **Download and stage.** Downloads the region (a bounding box or any polygon),
   drops roads cars cannot use, removes orphan nodes, and loads into a `stage` schema
   with keys and spatial indexes.
2. **Score and check.** Computes curvature and all scores, fails loudly on road classes with no
   score, then runs checks: no nulls, and known routes must come out within expected
   time and distance (Pier to Topanga: 19 to 21 min, 17.0 to 17.4 km).
3. **Swap.** One transaction moves the live tables into `archive` and the staged
   tables into `public`. Rollback is one more transaction.

The live app is never touched until step 3.

## API

- `POST /fastest` : fastest route, its time, distance and style scores.
- `POST /wander` : `{start, end, extra_min, preferences, strength}` returns the
  fastest-route summary plus up to three styled options.
- `POST /route` : single styled route without a time budget.
- Optional `"geometry": false` omits route geometry from responses.

## Security notes

- Preference names are whitelisted before they reach SQL. Values are parameterized.
- Database credentials live in secrets, not in code.
- Row level security is enabled on all tables; internal functions are not callable
  by the public API roles.
- Inputs are bounded (strength, extra minutes, region check).
- Known gaps: CORS is open and there is no rate limiting yet.

## Known limits

- Curvature rewards total turning, not sharpness, so some famous twisty roads
  (for example Piuma Road) score below long, gently winding ones.
- The urban score barely separates road types.
- Travel times use a flat multiplier and do not model traffic lights or live traffic.
- Wander quality depends on the road network: in some places there is not enough
  connected good road to use a large time budget, and the API says so.
- The region check is a rectangle.

## Roadmap

- Suggested extra time ("+45 min gets you the best curvy roads").
- GPX export and a minimal-waypoint Google Maps hand-off.
- Coastal, points-of-interest and elevation scores.
- Round trips.
- Rate limiting and tighter CORS.
