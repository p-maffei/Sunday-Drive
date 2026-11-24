# Sunday Drive pipeline, part 1: download, clean, and stage the road network.
#
# Run in Google Colab (one cell). Builds into a staging schema, never the live one.
# Enter the database host, port, user and password when prompted
# (or adapt the four input lines to read Colab Secrets). Never put real
# credentials in this file.
#
# Colab setup line (run first, or keep as the first line of the cell):
# !pip install -q osmnx geopandas pyogrio sqlalchemy geoalchemy2 psycopg2-binary

import getpass
import geopandas as gpd
import osmnx as ox
from shapely.geometry import box
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

# ---------- CONFIG: the only part you change ----------
REGION = box(-119.122009, 33.978670, -118.499222, 34.242459)  # west, south, east, north
SCHEMA = "stage"                      # built here, then swapped in by part 3
DROP_CLASSES = {"busway", "escape"}   # roads cars cannot use
# ------------------------------------------------------

assert SCHEMA != "public", "never build straight into the live schema"

G = ox.graph_from_polygon(REGION, network_type="drive")
nodes, edges = ox.graph_to_gdfs(G)
edges = edges.reset_index()


def flat(v):
    # OSM tags can be lists (e.g. a road with two names); flatten to text
    return ",".join(map(str, v)) if isinstance(v, list) else v


keep = ["u", "v", "osmid", "name", "highway", "oneway", "maxspeed",
        "lanes", "surface", "length", "geometry"]
edges = edges[[c for c in keep if c in edges.columns]].copy()
for c in edges.columns.drop("geometry"):
    edges[c] = edges[c].map(flat)

edges["hw"] = edges["highway"].str.split(",").str[0]
edges = edges[~edges["hw"].isin(DROP_CLASSES)]
edges = edges.rename(columns={"u": "source", "v": "target", "length": "length_m"})
edges.insert(0, "id", range(1, len(edges) + 1))
edges["cost"] = edges["length_m"]
edges["reverse_cost"] = -1    # every edge is already one-directional

used = set(edges["source"]) | set(edges["target"])          # drop orphan nodes
nodes = nodes.reset_index()[["osmid", "x", "y", "geometry"]].rename(columns={"osmid": "id"})
nodes = nodes[nodes["id"].isin(used)]
print(len(edges), "edges |", len(nodes), "nodes")

host = input("DB host: ").strip()
port = input("DB port: ").strip()
user = input("DB user: ").strip()
pw = getpass.getpass("DB password: ")
engine = create_engine(URL.create("postgresql+psycopg2", username=user, password=pw,
                                  host=host, port=int(port), database="postgres"))

with engine.begin() as c:
    c.execute(text(f"create schema if not exists {SCHEMA}"))
edges.to_postgis("ways", engine, schema=SCHEMA, if_exists="replace", index=False, chunksize=5000)
nodes.to_postgis("nodes", engine, schema=SCHEMA, if_exists="replace", index=False, chunksize=5000)

setup = f"""
alter table {SCHEMA}.ways add primary key (id);
alter table {SCHEMA}.nodes add primary key (id);
create index on {SCHEMA}.ways using gist (geometry);
create index on {SCHEMA}.ways (source);
create index on {SCHEMA}.ways (target);
create index on {SCHEMA}.nodes using gist (geometry);
alter table {SCHEMA}.ways add column mid geometry(Point, 4326);
update {SCHEMA}.ways set mid = ST_LineInterpolatePoint(geometry, 0.5);
create index on {SCHEMA}.ways using gist (mid);
alter table {SCHEMA}.ways enable row level security;
alter table {SCHEMA}.nodes enable row level security;
analyze {SCHEMA}.ways;
analyze {SCHEMA}.nodes;
"""
with engine.begin() as c:
    for stmt in [s.strip() for s in setup.split(";") if s.strip()]:
        c.execute(text(stmt))
    print(c.execute(text(f"select count(*) from {SCHEMA}.ways")).scalar(), "ways staged")
