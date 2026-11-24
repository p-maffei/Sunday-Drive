# Sunday Drive pipeline, part 2: score the staged network and run checks.
#
# Run in the same Colab session right after part 1 (it reuses `engine`).
# Safe to rerun: every step is idempotent.

import time
from sqlalchemy import text

SCHEMA = "stage"   # must match part 1
THROUGH = "('trunk','primary','secondary','tertiary','unclassified')"

# Known-good routes. The build fails if these drift. Edit when the region changes.
# name, start (lon, lat), end (lon, lat), minutes range, km range
CHECK_ROUTES = [
    ("Pier to Topanga", (-118.6775, 34.0360), (-118.6016, 34.0933), (19, 21), (17.0, 17.4)),
]


def run(name, sql, **params):
    t = time.time()
    with engine.begin() as c:
        c.execute(text("set local statement_timeout = 0"))
        c.execute(text("set local search_path = public, extensions"))
        c.execute(text(sql.replace("__S__", SCHEMA)), params)
    print(f"ok  {name} ({time.time() - t:.0f}s)")


def query(sql, **params):
    with engine.begin() as c:
        c.execute(text("set local statement_timeout = 0"))
        c.execute(text("set local search_path = public, extensions"))
        return c.execute(text(sql.replace("__S__", SCHEMA)), params).fetchall()


# ---- 1. Scales and lookups (shared config; safe to rerun) ----
run("score_scale table", """create table if not exists public.score_scale (
  metric text not null, x double precision not null, y double precision not null,
  primary key (metric, x))""")
run("score_scale rls", "alter table public.score_scale enable row level security")
run("score_scale values", """insert into public.score_scale (metric, x, y) values
  ('curvy',0,0.00),('curvy',37,0.10),('curvy',93,0.25),('curvy',234,0.50),
  ('curvy',470,0.75),('curvy',828,0.90),('curvy',1224,0.97),('curvy',2000,1.00)
  on conflict do nothing""")
run("road_class_score table", """create table if not exists public.road_class_score (
  hw text primary key, backroads double precision, urban double precision,
  default_kmh double precision)""")
run("road_class_score rls", "alter table public.road_class_score enable row level security")
run("road_class_score values", """insert into public.road_class_score (hw, backroads, urban, default_kmh) values
  ('living_street',0.3,0.4,15),('primary',0.2,0.9,65),('primary_link',0.2,0.4,40),
  ('residential',0.5,0.8,35),('secondary',0.4,0.9,55),('secondary_link',0.4,0.4,40),
  ('tertiary',0.8,0.6,45),('tertiary_link',0.8,0.4,40),('trunk',0.1,0.4,70),
  ('trunk_link',0.1,0.4,40),('unclassified',0.9,0.4,40),
  ('motorway',0.0,0.4,105),('motorway_link',0.0,0.4,65)
  on conflict do nothing""")
run("interp_score function", """create or replace function public.interp_score(p_metric text, p_x double precision)
returns double precision language sql stable as $$
  with lo as (select x, y from public.score_scale
              where metric = p_metric and x <= p_x order by x desc limit 1),
       hi as (select x, y from public.score_scale
              where metric = p_metric and x >  p_x order by x asc  limit 1)
  select coalesce(lo.y + (hi.y - lo.y) * (p_x - lo.x) / (hi.x - lo.x), lo.y, hi.y)
  from lo full join hi on true
$$""")
run("lock down function", """revoke execute on function public.interp_score(text, double precision)
  from public, anon, authenticated""")

# ---- 2. Fail loudly on road classes we have no scores for ----
missing = query("select distinct hw from __S__.ways where hw not in (select hw from public.road_class_score)")
assert not missing, f"Add these road classes to road_class_score, then rerun: {[m[0] for m in missing]}"

# ---- 3. Score the staged network ----
run("add columns", """alter table __S__.ways
  add column if not exists curv_raw double precision,
  add column if not exists curvy double precision,
  add column if not exists backroads double precision,
  add column if not exists urban double precision,
  add column if not exists speed_kmh double precision,
  add column if not exists time_s double precision""")

run("raw curvature", """with pts as (
  select w.id, d.path[1] as n, d.geom
  from __S__.ways w, lateral ST_DumpPoints(ST_Transform(w.geometry, 32611)) d),
seg as (
  select id, n, degrees(ST_Azimuth(geom, lead(geom) over (partition by id order by n))) as az
  from pts),
turn as (
  select id, az, lag(az) over (partition by id order by n) as prev
  from seg where az is not null),
agg as (
  select id, sum(abs(mod((az - prev + 540)::numeric, 360) - 180)) as turn_deg
  from turn where prev is not null group by id)
update __S__.ways w
set curv_raw = coalesce(a.turn_deg / nullif(w.length_m / 1000.0, 0), 0)
from agg a where a.id = w.id""")
run("curvature for 2-point edges", "update __S__.ways set curv_raw = 0 where curv_raw is null")

run("curvy score", f"""update __S__.ways set curvy = case
  when hw in {THROUGH} then public.interp_score('curvy', curv_raw)
  else least(0.3, public.interp_score('curvy', curv_raw)) end""")

run("backroads and urban", """update __S__.ways w
  set backroads = c.backroads, urban = c.urban
  from public.road_class_score c where c.hw = w.hw""")

run("speed", r"""update __S__.ways w set speed_kmh = coalesce(
  case when w.maxspeed ~ '^\s*\d+'
       then substring(w.maxspeed from '^\s*(\d+)')::float
            * case when w.maxspeed ~* '^\s*\d+\s*mph' then 1.609344 else 1 end end,
  (select c.default_kmh from public.road_class_score c where c.hw = w.hw), 40)""")

run("travel time", "update __S__.ways set time_s = (length_m / (speed_kmh / 3.6)) * 1.3")
run("analyze", "analyze __S__.ways")

# ---- 4. Checks ----
problems = []

nulls = query("""select count(*) filter (where curvy is null or backroads is null or urban is null
                 or speed_kmh is null or time_s is null), count(*) from __S__.ways""")[0]
print(f"edges: {nulls[1]}, rows with nulls: {nulls[0]}")
if nulls[0]:
    problems.append("null scores")

for name, a, b, mins, kms in CHECK_ROUTES:
    m, km = query("""select round((sum(w.time_s) / 60)::numeric), round((sum(w.length_m) / 1000)::numeric, 1)
      from pgr_dijkstra('select id, source, target, time_s as cost, reverse_cost from __S__.ways',
        (select id from __S__.nodes order by geometry <-> ST_SetSRID(ST_MakePoint(:alon, :alat), 4326) limit 1),
        (select id from __S__.nodes order by geometry <-> ST_SetSRID(ST_MakePoint(:blon, :blat), 4326) limit 1),
        true) d
      join __S__.ways w on w.id = d.edge where d.edge <> -1""",
      alon=a[0], alat=a[1], blon=b[0], blat=b[1])[0]
    ok = m is not None and mins[0] <= float(m) <= mins[1] and kms[0] <= float(km) <= kms[1]
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {m} min, {km} km (expected {mins} min, {kms} km)")
    if not ok:
        problems.append(name)

stats = query(f"""select round(avg(curvy)::numeric, 2),
  round((count(*) filter (where hw in {THROUGH} and curvy > 0.5)::numeric
         / nullif(count(*) filter (where hw in {THROUGH}), 0)), 2)
  from __S__.ways""")[0]
print(f"avg curvy {stats[0]}, share of through roads above 0.5: {stats[1]}")

print("\nALL CHECKS PASSED. Safe to swap." if not problems else f"\nSTOP. Do not swap: {problems}")
