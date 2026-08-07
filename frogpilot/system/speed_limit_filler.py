#!/usr/bin/env python3
import json
import time

from cereal import messaging

from openpilot.frogpilot.common.frogpilot_variables import params, params_memory

COORDINATE_KEYS = {"latitude", "longitude", "bearing"}
ENTRY_KEYS = {"segment_id", "speed_limit", "coordinates", "last_checked"}

MAPD_RESPONSE_TIME = 2

REVERIFICATION_INTERVAL = 7 * 24 * 60 * 60

class SpeedLimitFiller:
  def __init__(self):
    self.started_previously = False

    self.speed_limits = {limit.pop("segment_id"): limit for limit in json.loads(params.get("SpeedLimits") or "[]") if self.valid_entry(limit)}

    self.sm = messaging.SubMaster(["deviceState", "frogpilotCarState", "frogpilotNavigation", "frogpilotPlan"], poll="deviceState")

  def valid_entry(self, limit):
    if not isinstance(limit, dict) or set(limit) != ENTRY_KEYS:
      return False

    return isinstance(limit["coordinates"], dict) and set(limit["coordinates"]) == COORDINATE_KEYS

  def log_speed_limit(self):
    map_match = json.loads(params_memory.get("MapMatchedWay") or "{}")

    way_id = map_match.get("way_id", 0)
    if not way_id or not map_match.get("valid"):
      return

    dash_speed_limit = self.sm["frogpilotCarState"].dashboardSpeedLimit
    map_speed_limit = params_memory.get_float("MapSpeedLimit")
    mapbox_speed_limit = self.sm["frogpilotPlan"].slcMapboxSpeedLimit
    nav_speed_limit = self.sm["frogpilotNavigation"].navigationSpeedLimit

    if way_id not in self.speed_limits:
      new_limit = 0
      if dash_speed_limit >= 1 and abs(dash_speed_limit - map_speed_limit) > 1:
        new_limit = dash_speed_limit
      elif self.sm["frogpilotPlan"].slcMapboxWayId == way_id and mapbox_speed_limit >= 1 and abs(mapbox_speed_limit - map_speed_limit) > 1:
        new_limit = mapbox_speed_limit
      elif nav_speed_limit >= 1 and abs(nav_speed_limit - map_speed_limit) > 1:
        new_limit = nav_speed_limit

      coordinates = {key: map_match[key] for key in ("latitude", "longitude", "bearing") if key in map_match}
      if new_limit and len(coordinates) == 3:
        self.speed_limits[way_id] = {"speed_limit": new_limit, "coordinates": coordinates, "last_checked": time.time()}
    elif map_speed_limit > 0 and abs(self.speed_limits[way_id]["speed_limit"] - map_speed_limit) <= 1:
      del self.speed_limits[way_id]

  def verify_speed_limits(self):
    for way_id, limit in list(self.speed_limits.items()):
      self.sm.update(0)

      if self.sm["deviceState"].started:
        break

      if time.time() - limit.get("last_checked", 0) < REVERIFICATION_INTERVAL:
        continue

      token = time.monotonic_ns()

      params_memory.put("LastGPSPosition", json.dumps(dict(limit["coordinates"], location_mono_time=token)))

      time.sleep(MAPD_RESPONSE_TIME)

      map_match = json.loads(params_memory.get("MapMatchedWay") or "{}")

      if map_match.get("location_mono_time") != token:
        continue

      limit["last_checked"] = time.time()

      if not map_match.get("valid"):
        continue

      if map_match.get("way_id") != way_id:
        del self.speed_limits[way_id]
        continue

      if map_match.get("speed_limit", 0) > 0 and abs(limit["speed_limit"] - map_match["speed_limit"]) <= 1:
        del self.speed_limits[way_id]

    params_memory.remove("LastGPSPosition")
    params_memory.remove("MapMatchedWay")
    params_memory.remove("MapSpeedLimit")

  def write_speed_limits(self):
    params.put("SpeedLimits", json.dumps([{"segment_id": way_id, **limit} for way_id, limit in self.speed_limits.items()]))

  def update(self):
    self.sm.update(1000)

    started = self.sm["deviceState"].started

    if started:
      self.log_speed_limit()
    elif self.started_previously:
      self.write_speed_limits()
      self.verify_speed_limits()
      self.write_speed_limits()

    self.started_previously = started

def main():
  speed_limit_filler = SpeedLimitFiller()

  while True:
    speed_limit_filler.update()


if __name__ == "__main__":
  main()
