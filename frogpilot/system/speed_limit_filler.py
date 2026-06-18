#!/usr/bin/env python3
import os
import subprocess
import time

from cereal import custom, messaging

from openpilot.common.gps import get_gps_location_service
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.frogpilot.common.frogpilot_utilities import is_gps_location_valid, is_mapd_data_valid

COORDINATE_KEYS = {"latitude", "longitude", "bearing"}
ENTRY_KEYS = {"segment_id", "speed_limit", "coordinates", "last_checked"}

NAVIGATION_PATH = os.path.join(os.path.dirname(__file__), "..", "navigation")

MAPD_MATCH_TIME = 2
MAPD_STARTUP_TIME = 10

REVERIFICATION_INTERVAL = 7 * 24 * 60 * 60

class SpeedLimitFiller:
  def __init__(self):
    self.params = Params()

    self.gps_service = get_gps_location_service(self.params)

    self.started_previously = False

    self.speed_limits = {limit.pop("segment_id"): limit for limit in (self.params.get("SpeedLimits") or []) if self.valid_entry(limit)}

    self.sm = messaging.SubMaster(["deviceState", "frogpilotCarState", "frogpilotPlan", "mapdOut", self.gps_service], poll="deviceState")

  def valid_entry(self, limit):
    if not isinstance(limit, dict) or set(limit) != ENTRY_KEYS:
      return False

    return isinstance(limit["coordinates"], dict) and set(limit["coordinates"]) == COORDINATE_KEYS

  def log_speed_limit(self):
    gps_location = self.sm[self.gps_service]
    gps_valid = is_gps_location_valid(gps_location, self.gps_service, self.sm)

    if not is_mapd_data_valid(self.sm["mapdOut"], gps_valid, self.sm):
      return

    if self.sm["mapdOut"].waySelectionType != custom.WaySelectionType.current:
      return

    dash_speed_limit = self.sm["frogpilotCarState"].dashboardSpeedLimit
    map_speed_limit = self.sm["mapdOut"].speedLimit
    mapbox_speed_limit = self.sm["frogpilotPlan"].slcMapboxSpeedLimit

    way_id = self.sm["mapdOut"].wayId

    if way_id not in self.speed_limits:
      new_limit = 0
      if dash_speed_limit >= 1 and abs(dash_speed_limit - map_speed_limit) > 1:
        new_limit = dash_speed_limit
      elif dash_speed_limit < 1 and self.sm["frogpilotPlan"].slcMapboxWayId == way_id and mapbox_speed_limit >= 1 and abs(mapbox_speed_limit - map_speed_limit) > 1:
        new_limit = mapbox_speed_limit

      if new_limit:
        coordinates = {"latitude": gps_location.latitude, "longitude": gps_location.longitude, "bearing": gps_location.bearingDeg}
        self.speed_limits[way_id] = {"speed_limit": new_limit, "coordinates": coordinates, "last_checked": time.time()}
    elif map_speed_limit > 0 and abs(self.speed_limits[way_id]["speed_limit"] - map_speed_limit) <= 1:
      del self.speed_limits[way_id]

  def aborted(self, mapd):
    self.sm.update(0)
    return self.sm["deviceState"].started or mapd.poll() is not None

  def gps_message(self, coordinates):
    message = messaging.new_message(self.gps_service, valid=True)
    location = getattr(message, self.gps_service)
    location.hasFix = True
    location.latitude = coordinates["latitude"]
    location.longitude = coordinates["longitude"]
    location.bearingDeg = coordinates["bearing"]
    return message

  def verify_speed_limits(self):
    due = [(way_id, limit) for way_id, limit in self.speed_limits.items() if time.time() - limit["last_checked"] >= REVERIFICATION_INTERVAL]
    if not due:
      return False

    changed = False

    with OpenpilotPrefix():
      mapd = subprocess.Popen(["./mapd"], cwd=NAVIGATION_PATH, env={**os.environ, "USE_MSGQ_PREFIX": "true"})

      try:
        sm = messaging.SubMaster(["mapdOut"])
        pm = messaging.PubMaster([self.gps_service])

        startup = time.monotonic()

        while not sm.alive["mapdOut"]:
          if self.aborted(mapd) or time.monotonic() - startup > MAPD_STARTUP_TIME:
            return False

          pm.send(self.gps_service, self.gps_message(due[0][1]["coordinates"]))

          sm.update(100)

        for way_id, limit in due:
          if self.aborted(mapd):
            break

          settle = time.monotonic()

          while time.monotonic() - settle < MAPD_MATCH_TIME:
            pm.send(self.gps_service, self.gps_message(limit["coordinates"]))

            time.sleep(0.1)

          sm.update(100)

          if not sm.updated["mapdOut"] or sm["mapdOut"].wayId != way_id:
            continue

          limit["last_checked"] = time.time()

          changed = True

          if sm["mapdOut"].speedLimit > 0 and abs(limit["speed_limit"] - sm["mapdOut"].speedLimit) <= 1:
            del self.speed_limits[way_id]
      finally:
        mapd.terminate()
        try:
          mapd.wait(timeout=MAPD_MATCH_TIME)
        except subprocess.TimeoutExpired:
          mapd.kill()
          mapd.wait()

    return changed

  def write_speed_limits(self):
    self.params.put("SpeedLimits", [{"segment_id": way_id, **limit} for way_id, limit in self.speed_limits.items()])

  def update(self):
    self.sm.update(1000)

    started = self.sm["deviceState"].started

    if started:
      self.log_speed_limit()
    elif self.started_previously:
      self.write_speed_limits()
      if self.verify_speed_limits():
        self.write_speed_limits()

    self.started_previously = started

def main():
  speed_limit_filler = SpeedLimitFiller()

  while True:
    speed_limit_filler.update()


if __name__ == "__main__":
  main()
