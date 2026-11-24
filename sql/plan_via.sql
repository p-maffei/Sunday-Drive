-- Route A -> W -> B with a penalty for reusing roads from the outbound leg.
-- Requires ways.mid (edge midpoint, geometry(Point, 4326)) with a GiST index.
-- The indexed ST_DWithin on `mid` (about 30 m in degrees) narrows candidates before
-- the exact meters check; without it, this was ~25 s per call on 46k edges (now ~2 s).
CREATE OR REPLACE FUNCTION public.plan_via(a bigint, w bigint, b bigint, prefs text[] DEFAULT ARRAY['curvy'::text, 'backroads'::text], k double precision DEFAULT 7, reuse_penalty double precision DEFAULT 5, reuse_dist_m double precision DEFAULT 20)
 RETURNS TABLE(leg integer, seq integer, edge bigint, length_m double precision, time_s double precision, match double precision, reused boolean)
 LANGUAGE plpgsql
AS $function$
#variable_conflict use_column
declare
  allowed    text[] := array['curvy','backroads','urban'];
  match_expr text;
  base_cost  text;
  leg1_sql   text;
  leg2_sql   text;
  used_ids   bigint[];
begin
  if prefs is null or cardinality(prefs) = 0 then
    match_expr := '0.5';
  else
    if exists (select 1 from unnest(prefs) p where p <> all(allowed)) then
      raise exception 'unsupported preference';
    end if;
    match_expr := '(' || array_to_string(prefs, ' + ') || ') / ' || cardinality(prefs)::text || '.0';
  end if;

  base_cost := format('(length_m * (1 + (1 - (%s)) * %s))', match_expr, k);
  leg1_sql  := format('select id, source, target, %s as cost, reverse_cost from ways', base_cost);

  select array_agg(distinct w3.id)
  into used_ids
  from pgr_dijkstra(leg1_sql, a, w, directed := true) d
  join ways w2 on w2.id = d.edge
  join ways w3 on st_dwithin(w3.mid, w2.geometry, 0.0003)
              and st_dwithin(w3.mid::geography, w2.geometry::geography, reuse_dist_m)
  where d.edge <> -1;

  leg2_sql := format(
    'select id, source, target,
            %s * (case when id = any(%L::bigint[]) then %s else 1 end) as cost,
            reverse_cost
     from ways', base_cost, used_ids, reuse_penalty);

  return query execute format($q$
    select 1, d.seq::int, d.edge::bigint, w2.length_m::double precision, w2.time_s::double precision,
           ((%1$s))::double precision, false
    from pgr_dijkstra(%2$L, %3$s::bigint, %4$s::bigint, directed := true) d
    join ways w2 on w2.id = d.edge
    where d.edge <> -1
    union all
    select 2, d.seq::int, d.edge::bigint, w2.length_m::double precision, w2.time_s::double precision,
           ((%1$s))::double precision,
           coalesce(w2.id = any(%5$L::bigint[]), false)
    from pgr_dijkstra(%6$L, %4$s::bigint, %7$s::bigint, directed := true) d
    join ways w2 on w2.id = d.edge
    where d.edge <> -1
    order by 1, 2
  $q$, match_expr, leg1_sql, a, w, used_ids, leg2_sql, b);
end;
$function$;

revoke execute on function public.plan_via(bigint, bigint, bigint, text[], double precision, double precision, double precision)
  from public, anon, authenticated;
