-- Candidate waypoints for /wander: through-road nodes whose fastest A -> W -> B trip
-- fits [min_s, max_s] seconds. One search from A, and one search from B on the
-- REVERSED graph (many-to-one in pgRouting runs one Dijkstra per start node,
-- which was ~75 s on 46k edges; this version takes ~4 s).
create or replace function public.wander_candidates(
  p_a bigint, p_b bigint, min_s double precision, max_s double precision)
 returns table(node bigint, cell_x integer, cell_y integer, shortest_s double precision,
               curvy double precision, backroads double precision, urban double precision)
 language sql
 stable
as $function$
  with
  out_leg as (
    select end_vid as node, agg_cost as t_out
    from pgr_dijkstraCost('select id, source, target, time_s as cost, reverse_cost from ways',
                          p_a, (select array_agg(id) from nodes), directed := true)),
  back_leg as (
    -- one search from B on the reversed graph = cost from every node TO B
    select end_vid as node, agg_cost as t_back
    from pgr_dijkstraCost('select id, target as source, source as target, time_s as cost, reverse_cost from ways',
                          p_b, (select array_agg(id) from nodes), directed := true)),
  road_nodes as (
    select source as node,
           avg(ways.curvy)::double precision as curvy,
           avg(ways.backroads)::double precision as backroads,
           avg(ways.urban)::double precision as urban
    from ways
    where split_part(highway, ',', 1) in ('secondary', 'tertiary', 'unclassified')
    group by source
    having count(*) >= 2)
  select r.node, floor(n.x / 0.02)::int, floor(n.y / 0.02)::int,
         (o.t_out + k.t_back)::double precision,
         r.curvy, r.backroads, r.urban
  from road_nodes r
  join out_leg o on o.node = r.node
  join back_leg k on k.node = r.node
  join nodes n on n.id = r.node
  where o.t_out + k.t_back between min_s and max_s;
$function$;

revoke execute on function public.wander_candidates(bigint, bigint, double precision, double precision)
  from public, anon, authenticated;
