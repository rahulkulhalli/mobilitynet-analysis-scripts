import argparse
import logging
import json
import copy
import arrow
import requests
import osmapi
import re
import polyline as pl
import osrm as osrm
import shapely.geometry as geo

sensing_configs = json.load(open("sensing_regimes.all.specs.json"))

def validate_and_fill_datetime(current_spec):
    ret_spec = copy.copy(current_spec)
    timezone = current_spec["region"]["timezone"]
    ret_spec["start_ts"] = arrow.get(current_spec["start_fmt_date"]).replace(tzinfo=timezone).timestamp
    ret_spec["end_ts"] = arrow.get(current_spec["end_fmt_date"]).replace(tzinfo=timezone).timestamp
    return ret_spec

def node_to_geojson_coords(node_id):
    osm = osmapi.OsmApi()
    node_details = osm.NodeGet(node_id)
    return [node_details["lon"], node_details["lat"]]

def get_route_coords(mode, waypoint_coords):
    if mode == "CAR" \
      or mode == "WALKING" \
      or mode == "BICYCLING" \
      or mode == "BUS":
        # Use OSRM
        overview_geometry_params = {"overview": "full",
            "geometries": "polyline", "steps": "false"}
        route_coords = osrm.get_route_points(mode, waypoint_coords, overview_geometry_params)
        return route_coords
    else:
        raise NotImplementedError("OSRM does not support train modes at this time")

def _fill_coords_from_id(loc):
    if loc is None:
        return None
    if "osm_id" in loc["properties"]:
        if loc["geometry"]["type"] == "Point":
            loc["geometry"]["coordinates"] = node_to_geojson_coords(loc["properties"]["osm_id"])
        elif loc["geometry"]["type"] == "Polygon":
            # get coords for way returns a tuple of (nodes, points)
            loc["geometry"]["coordinates"] = [coords_swap(c) for c in get_coords_for_way(loc["properties"]["osm_id"])[1]]
    else:
        assert "coordinates" in loc["geometry"],\
            "Location %s does not have either an osmid or specified set of coordinates"
    return loc

def validate_and_fill_calibration_tests(curr_spec):
    modified_spec = copy.copy(curr_spec)
    calibration_tests = modified_spec["calibration_tests"]
    for t in calibration_tests:
        _fill_coords_from_id(t["start_loc"])
        _fill_coords_from_id(t["end_loc"])
        t["config"] = sensing_configs[t["config"]["id"]]
    return modified_spec

def coords_swap(lon_lat):
    return list(reversed(lon_lat))

def get_route_from_osrm(t, start_coords, end_coords):
    if "route_waypoints" in t:
        waypoints = t["route_waypoints"]
        waypoint_coords = [node_to_geojson_coords(node_id) for node_id in waypoints]
        t["waypoint_coords"] = {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": waypoint_coords
            }
        }
    elif "waypoint_coords" in t:
        if isinstance(t["waypoint_coords"], dict):
            waypoint_coords = t["waypoint_coords"]["geometry"]["coordinates"]
        else:
            waypoint_coords = []
            for wc in waypoint_coords:
                waypoint_coords.append({
                    "valid_start_fmt_date": wc["properties"]["valid_start_fmt_date"],
                    "valid_start_ts": arrow.get(wc["properties"]["valid_start_fmt_date"]).timestamp,
                    "valid_end_fmt_date": wc["properties"]["valid_end_fmt_date"],
                    "valid_end_ts": arrow.get(wc["properties"]["valid_end_fmt_date"]).timestamp,
                    "coordinates": wc["geometry"]["coordinates"]
                })
    else:
        waypoint_coords = []
    logging.debug("waypoint_coords = %s..." % waypoint_coords[0:3])
    route_coords = get_route_coords(t["mode"],
        [start_coords] + waypoint_coords + [end_coords])
    return route_coords

def get_route_from_polyline(t):
    return pl.PolylineCodec().decode(t if isinstance(t, str) else t["polyline"])

# Porting the perl script at
# https://wiki.openstreetmap.org/wiki/Relations/Relations_to_GPX to python

def get_way_list(relation_details):
    wl = []
    for member in relation_details["member"]:
        # print(member["ref"], member["type"])
        assert member["type"] != "relation", "This is a parent relation for child %d, expecting only child relations" % member["ref"]
        if member["type"] == "way" and member["role"] != "platform":
            wl.append(member["ref"])
    return wl

# way details is an array of n-1 node entries followed by a way entry
# the way entry has an "nd" field which is an array of node ids in the correct
# order the n-1 node entries are not necessarily in the correct order but
# provide the id -> lat,lng mapping
# Note also that the way can sometimes have the nodes in the reversed order
# e.g. way 367132251 in relation 9605483 is reversed compared to ways 
# 368345083 and 27422567 before it
# this function automatically detects that and reverses the node array
def get_coords_for_way(wid, prev_last_node=-1):
    osm = osmapi.OsmApi()
    lat = {}
    lon = {}
    coords_list = []
    way_details = osm.WayFull(wid)
    # print("Processing way %d with %d nodes" % (wid, len(way_details) - 1))
    for e in way_details:
        if e["type"] == "node":
            lat[e["data"]["id"]] = e["data"]["lat"]
            lon[e["data"]["id"]] = e["data"]["lon"]
        if e["type"] == "way":
            assert e["data"]["id"] == wid, "Way id mismatch! %d != %d" % (e["data"]["id"], wl[0])
            ordered_node_array = e["data"]["nd"]
            if prev_last_node != -1 and ordered_node_array[-1] == prev_last_node:
                print("LAST entry %d matches prev_last_node %d, REVERSING order for %d" %
                      (ordered_node_array[-1], prev_last_node, wid))
                ordered_node_array = list(reversed(ordered_node_array))
            for on in ordered_node_array:
                # Returning lat,lon instead of lon,lat to be consistent with
                # the returned values from OSRM. Since we manually swap the
                # values later
                coords_list.append([lat[on], lon[on]])
    return ordered_node_array, coords_list

def get_coords_for_relation(rid, start_node, end_node):
    osm = osmapi.OsmApi()
    relation_details = osm.RelationGet(rid)
    wl = get_way_list(relation_details)
    print("Relation %d mapped to %d ways" % (rid, len(wl)))
    coords_list = []
    on_list = []
    prev_last_node = -1
    for wid in wl:
        w_on_list, w_coords_list = get_coords_for_way(wid, prev_last_node)
        on_list.extend(w_on_list)
        coords_list.extend(w_coords_list)
        prev_last_node = w_on_list[-1]
        print("After adding %d entries from wid %d, curr count = %d" % (len(w_on_list), wid, len(coords_list)))
    start_index = on_list.index(start_node)
    end_index = on_list.index(end_node)
    assert start_index <= end_index, "Start index %d is before end %d" % (start_index, end_index)
    return coords_list[start_index:end_index+1]

def get_route_from_relation(r):
    # get_coords_for_relation assumes that start and end are both nodes
    return get_coords_for_relation(r["relation_id"], r["start_node"], r["end_node"])

def _add_temporal_ground_truth(orig_loc, default_start_fmt_date, default_end_fmt_date):
    # fill in timespan for which ground truth is valid (see issue #11)
    # first, if loc is a dict, we need to convert loc to a list of dicts.
    # otherwise, expect a list of dicts for each.

    loc = copy.copy(orig_loc)

    if isinstance(loc, dict):
        loc = [loc]

    # next, add dates if they do not exist
    for l in loc:
        if l["properties"].get("valid_start_fmt_date") is None:
            l["properties"]["valid_start_fmt_date"] = default_start_fmt_date
            l["properties"]["valid_start_ts"] = arrow.get(default_start_fmt_date).timestamp
        else:
            l["properties"]["valid_start_ts"] = arrow.get(l["properties"]["valid_start_fmt_date"]).timestamp

        if l["properties"].get("valid_end_fmt_date") is None:
            l["properties"]["valid_end_fmt_date"] = default_end_fmt_date
            l["properties"]["valid_end_ts"] = arrow.get(default_end_fmt_date).timestamp
        else:
            l["properties"]["valid_end_ts"] = arrow.get(l["properties"]["valid_end_fmt_date"]).timestamp

    return loc

def validate_and_fill_leg(orig_leg, default_start_fmt_date, default_end_fmt_date):
    t = copy.copy(orig_leg)
    t["type"] = "TRAVEL"
    t["start_loc"] = _add_temporal_ground_truth(t["start_loc"], default_start_fmt_date, default_end_fmt_date)
    t["end_loc"] = _add_temporal_ground_truth(t["end_loc"], default_start_fmt_date, default_end_fmt_date)

    route_coords = []

    if "polylines" in t:
        for p in t["polylines"]:
            route_coords.append({
                "type": "Feature",
                "properties": {
                    "valid_start_fmt_date": p["valid_start_fmt_date"],
                    "valid_start_ts": arrow.get(p["valid_start_fmt_date"]).timestamp,
                    "valid_end_fmt_date": p["valid_end_fmt_date"],
                    "valid_end_ts": arrow.get(p["valid_end_fmt_date"]).timestamp
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [coords_swap(c) for c in get_route_from_polyline(p["polyline"])]
                }
            })
    elif "polyline" in t:
        route_coords.append({
            "type": "Feature",
            "properties": {
                "valid_start_fmt_date": default_start_fmt_date,
                "valid_start_ts": arrow.get(default_start_fmt_date).timestamp,
                "valid_end_fmt_date": default_end_fmt_date,
                "valid_end_ts": arrow.get(default_end_fmt_date).timestamp
            },
            "geometry": {
                "type": "LineString",
                "coordinates": [coords_swap(c) for c in get_route_from_polyline(t)]
            }
        })
    else:
        raise KeyError("'polyline' or 'polylines' structure not found in leg!")


    t["route_coords"] = route_coords

    return t

def get_hidden_access_transfer_walk_segments(prev_l, l, default_start_fmt_date, default_end_fmt_date):
    # print("prev_l = %s, l = %s" % (prev_l, l))
    if prev_l is None and l["mode"] != "WALKING":
        # This is the first leg and is a vehicular trip,
        # need to add an access leg to represent the walk to where the
        # vehicle will be parked. This is unknown at spec creation time,
        # so we don't have any ground truth for it
        return [{
            "id": "walk_start",
            "type": "ACCESS",
            "mode": "WALKING",
            "name": "Walk from the building to your vehicle",
            "loc": _add_temporal_ground_truth(l["start_loc"], default_start_fmt_date, default_end_fmt_date),
        }]

    if l is None and prev_l["mode"] != "WALKING":
        # This is the first leg and is a vehicular trip,
        # need to add an access leg to represent the walk to where the
        # vehicle will be parked. This is unknown at spec creation time,
        # so we don't have any ground truth for it
        return [{
            "id": "walk_end",
            "type": "ACCESS",
            "mode": "WALKING",
            "name": "Walk from your vehicle to the building",
            "loc": _add_temporal_ground_truth(prev_l["end_loc"], default_start_fmt_date, default_end_fmt_date)
        }]

    # The order of the checks is important because we want the STOPPED to come
    # after the WALKING
    ret_list = []

    if prev_l is not None and l is not None and\
        prev_l["mode"] != "WALKING" and l["mode"] != "WALKING":
        # transferring between vehicles, add a transit transfer
        # without a ground truthed trajectory
        # NOTE: unlike the first two cases, we are NOT returning here
        # we will run the next check as well, because for most
        # transit transfers, there will be both a transfer and a stop
        if isinstance(prev_l["end_loc"], list):
            loc = _add_temporal_ground_truth(l["end_loc"][0], default_start_fmt_date, default_end_fmt_date)
            if len(l["end_loc"]) > 1:
                for sl in l["end_loc"][1:]:
                    loc.append(_add_temporal_ground_truth(el, default_start_fmt_date, default_end_fmt_date)[0])

            ret_list.append({
                "id": "wait_for_%s_%s_%s" % (prev_l["id"], l["id"]),
                "type": "WAITING",
                "mode": "STOPPED",
                "name": "Transfer between %s and %s at %s" %\
                    (prev_l["mode"], l["mode"], " & ".join([el["properties"]["name"] for el in l["end_loc"]])),
                "loc": loc
            })
        else:
            ret_list.append({
                "id": "tt_%s_%s" % (prev_l["id"], l["id"]),
                "type": "TRANSFER",
                "mode": "WALKING",
                "name": "Transfer between %s and %s at %s" %\
                    (prev_l["mode"], l["mode"], prev_l["end_loc"]["properties"]["name"]),
                "loc": _add_temporal_ground_truth(l["start_loc"], default_start_fmt_date, default_end_fmt_date)
            })

    if l is not None and "multiple_occupancy" in l and l["multiple_occupancy"] == True:
        if isinstance(l["start_loc"], list):
            loc = _add_temporal_ground_truth(l["start_loc"][0], default_start_fmt_date, default_end_fmt_date)
            if len(l["start_loc"]) > 1:
                for sl in l["start_loc"][1:]:
                    loc.append(_add_temporal_ground_truth(sl, default_start_fmt_date, default_end_fmt_date)[0])

            ret_list.append({
                "id": "wait_for_%s" % l["id"],
                "type": "WAITING",
                "mode": "STOPPED",
                "name": "Wait for %s at %s" % (l["mode"], " & ".join([sl["properties"]["name"] for sl in l["start_loc"]])),
                "loc": loc
            })
        else:
            ret_list.append({
                "id": "wait_for_%s" % l["id"],
                "type": "WAITING",
                "mode": "STOPPED",
                "name": "Wait for %s at %s" %\
                    (l["mode"], l["start_loc"]["properties"]["name"]),
                "loc": _add_temporal_ground_truth(l["start_loc"], default_start_fmt_date, default_end_fmt_date)
            })

    # return from the last two checks
    return ret_list

def has_duplicate_legs(trip):
    leg_id_list = [l["id"] for l in trip["legs"]]
    unique_leg_id_list = set(leg_id_list)
    # If the lengths are different, there are duplicates, so this returns true
    return len(unique_leg_id_list) != len(leg_id_list)

def validate_and_fill_eval_trips(curr_spec):
    modified_spec = copy.copy(curr_spec)

    default_start_fmt_date = curr_spec["start_fmt_date"]
    default_end_fmt_date = curr_spec["end_fmt_date"]

    eval_trips = modified_spec["evaluation_trips"]
    for t in eval_trips:
        if "legs" in t:
            print("Filling multi-modal trip %s" % t["id"])
            assert not has_duplicate_legs(t), \
                "Found duplicate leg ids in trip %s" % t["id"]
            prev_l = None
            ret_leg_list = []
            for i, l in enumerate(t["legs"]):
                print("Filling leg %s" % l["id"])
                # Add in shim legs like the ones to walk to/from your vehicle
                # or to transfer between transit modes
                shim_legs = get_hidden_access_transfer_walk_segments(prev_l, l, default_start_fmt_date, default_end_fmt_date)
                print("Got shim legs %s, extending" % ([sl["id"] for sl in shim_legs]))
                ret_leg_list.extend(shim_legs)
                ret_leg_list.append(validate_and_fill_leg(l, default_start_fmt_date, default_end_fmt_date))
                prev_l = l
            shim_legs = get_hidden_access_transfer_walk_segments(prev_l, None, default_start_fmt_date, default_end_fmt_date)
            assert len(shim_legs) <= 1, "Last leg should not have a transfer shim"
            print("Got shim legs %s, extending" % ([sl["id"] for sl in shim_legs]))
            ret_leg_list.extend(shim_legs)
            t["legs"] = ret_leg_list
            # Let's check again after we have inserted the shim legs
            assert not has_duplicate_legs(t), \
                "Found duplicate leg ids in trip %s" % t["id"]
        
        else:
            print("Filling unimodal trip %s" % t["id"])
            # unimodal trip, let's add shims if necessary
            # the filled spec will always be multimodal
            # since the only true unimodal trip is walking
            # and it is easier to assume that there are always legs
            # specially since we are adding complexity with the type of trips
            # (ACCESS, TRANSFER, TRAVEL)
            unmod_trip = copy.deepcopy(t)
            t.clear()
            t["id"] = unmod_trip["id"]
            t["name"] = unmod_trip["name"]
            t["legs"] = []
            before_shim_leg = get_hidden_access_transfer_walk_segments(None, unmod_trip, default_start_fmt_date, default_end_fmt_date)
            assert len(before_shim_leg) <= 1, "First leg should not have a transfer shim"
            print("Got shim legs %s, extending" % ([sl["id"] for sl in before_shim_leg]))
            t["legs"].extend(before_shim_leg)
            t["legs"].append(validate_and_fill_leg(unmod_trip, default_start_fmt_date, default_end_fmt_date))
            after_shim_leg = get_hidden_access_transfer_walk_segments(unmod_trip, None, default_start_fmt_date, default_end_fmt_date)
            assert len(after_shim_leg) <= 1, "Last leg should not have a transfer shim"
            print("Got shim legs %s, extending" % ([sl["id"] for sl in after_shim_leg]))
            t["legs"].extend(after_shim_leg)
            # Let's check again after we have inserted the shim legs
            assert not has_duplicate_legs(t), \
                "Found duplicate leg ids in trip %s" % t["id"]

    return modified_spec

def validate_and_fill_sensing_settings(curr_spec):
    modified_spec = copy.copy(curr_spec)
    for ss in modified_spec["sensing_settings"]:
        for phoneOS, compare_list in ss.items():
            ss[phoneOS] = {}
            ss[phoneOS]["compare"] = compare_list
            ss[phoneOS]["name"] = " v/s ".join(compare_list)
            ss[phoneOS]["sensing_configs"] = [sensing_configs[cr] for cr in compare_list]
    return modified_spec

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    parser = argparse.ArgumentParser(prog="autofill_eval_spec")

    parser.add_argument("in_spec_file", help="file to autofill")
    parser.add_argument("out_spec_file", help="autofilled version of in_spec_file")

    args = parser.parse_args()

    print("Reading input from %s" % args.in_spec_file) 
    current_spec = json.load(open(args.in_spec_file))

    dt_spec = validate_and_fill_datetime(current_spec)
    calib_spec = validate_and_fill_calibration_tests(dt_spec)
    eval_spec = validate_and_fill_eval_trips(calib_spec)
    settings_spec = validate_and_fill_sensing_settings(eval_spec)
   
    print("Writing output to %s" % args.out_spec_file) 
    json.dump(settings_spec, open(args.out_spec_file, "w"), indent=2)
