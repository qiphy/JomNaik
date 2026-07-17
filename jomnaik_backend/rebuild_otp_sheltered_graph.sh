#!/usr/bin/env zsh
# Build an OTP graph from the derived sheltered-walkway OSM dataset.
set -euo pipefail

cd "${0:A:h}"
python3 prepare_sheltered_osm.py klang_valley.osm.pbf klang_valley.sheltered.osm.pbf \
  --change-file sheltered_walkway_routing.osc

build_dir="$(mktemp -d "${TMPDIR:-/tmp}/jomnaik-otp-build.XXXXXX")"
ln -s "${PWD}/klang_valley.sheltered.osm.pbf" "$build_dir/klang_valley.osm.pbf"
for feed in gtfs-bus.zip gtfs-ktmb.zip gtfs-mrtfeeder.zip gtfs-rail.zip; do
  ln -s "${PWD}/$feed" "$build_dir/$feed"
done

java -Xmx4G -jar otp-2.4.0-shaded.jar --build --save "$build_dir"
cp graph.obj graph.before-sheltered.obj
mv "$build_dir/graph.obj" graph.obj
