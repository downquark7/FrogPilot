#!/usr/bin/env python3
import numpy as np

from openpilot.common.constants import ACCELERATION_DUE_TO_GRAVITY
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib.drive_helpers import MAX_LATERAL_ACCEL_NO_ROLL

from openpilot.frogpilot.common.frogpilot_variables import CRUISING_SPEED, CURVE_SPEED_PROFILES, DEFAULT_LATERAL_ACCELERATION, MINIMUM_LATERAL_ACCELERATION, PLANNER_TIME

# Driver cornering calibration ("Auto" profile)
CALIBRATION_PROGRESS_THRESHOLD = int(10 / DT_MDL)
CALIBRATION_TARGET = CALIBRATION_PROGRESS_THRESHOLD * 50
PERCENTILE = 90
ROUNDING_PRECISION = 3

# Learned maximum lateral acceleration ("Sport" profile)
MAX_ANGLE_GROWTH_RATE = 0.02
MAX_BACKOFF_RATE = 0.1
MAX_GROWTH_RATE = 0.08
MAX_TORQUE_HEADROOM = 0.9
MIN_SATURATION_SPEED = 10.0

# Curve speed target
GENTLE_LATERAL_ACCELERATION = 1.5
TARGET_LEAD_TIME = 2.0
TARGET_RISE_RATE = 1.2
TARGET_TRACKING_MARGIN = 1.0

class CurveSpeedController:
  def __init__(self, FrogPilotVCruise):
    self.frogpilot_planner = FrogPilotVCruise.frogpilot_planner

    self.enable_training = False
    self.target_set = False

    self.training_timer = 0

    self.budget = DEFAULT_LATERAL_ACCELERATION

    self.curvature_data = self.frogpilot_planner.params.get("CurvatureData")
    self.max_limit = self.frogpilot_planner.params.get("MaxLateralAcceleration")

    self.normalize_curvature_data()
    self.update_lateral_acceleration()

  def log_data(self, long_control_active, v_ego, sm):
    self.enable_training = v_ego > CRUISING_SPEED
    self.enable_training &= not self.frogpilot_planner.lead_one.status
    self.enable_training &= not long_control_active
    self.enable_training &= not (sm["carState"].leftBlinker or sm["carState"].rightBlinker)

    if self.enable_training:
      self.training_timer += DT_MDL

      if self.training_timer >= PLANNER_TIME and self.frogpilot_planner.driving_in_curve:
        lateral_acceleration = abs(self.frogpilot_planner.lateral_acceleration)
        road_curvature = abs(round(sm["controlsState"].curvature, ROUNDING_PRECISION))

        key = str(road_curvature)
        if key in self.curvature_data:
          data = self.curvature_data[key]

          average = data["average"]
          count = data["count"]

          self.curvature_data[key] = {
            "average": ((average * count) + lateral_acceleration) / (count + 1),
            "count": min(count + 1, CALIBRATION_PROGRESS_THRESHOLD)
          }
        else:
          self.curvature_data[key] = {
            "average": lateral_acceleration,
            "count": 1
          }
      else:
        self.enable_training = False

    elif self.training_timer >= PLANNER_TIME:
      self.frogpilot_planner.params.put_nonblocking("CurvatureData", self.curvature_data)
      self.update_lateral_acceleration()

      self.training_timer = 0

    else:
      self.training_timer = 0

  def normalize_curvature_data(self):
    normalized_data = {}
    for key, data in self.curvature_data.items():
      normalized_key = str(abs(round(float(key), ROUNDING_PRECISION)))
      count = min(int(data["count"]), CALIBRATION_PROGRESS_THRESHOLD)
      if count <= 0:
        continue

      if data["average"] < MINIMUM_LATERAL_ACCELERATION or data["average"] <= CRUISING_SPEED**2 * (float(normalized_key) - 0.5 * 10**-ROUNDING_PRECISION):
        normalized_data = {}
        break

      if normalized_key in normalized_data:
        merged_data = normalized_data[normalized_key]
        total_count = merged_data["count"] + count

        normalized_data[normalized_key] = {
          "average": ((merged_data["average"] * merged_data["count"]) + (data["average"] * count)) / total_count,
          "count": min(total_count, CALIBRATION_PROGRESS_THRESHOLD)
        }
      else:
        normalized_data[normalized_key] = {"average": data["average"], "count": count}

    self.curvature_data = normalized_data

  def update_lateral_acceleration(self):
    if self.curvature_data:
      all_samples = [data["average"] for data in self.curvature_data.values()]
      all_counts = [data["count"] for data in self.curvature_data.values()]
      self.lateral_acceleration = float(np.percentile(np.repeat(all_samples, all_counts), PERCENTILE))
    else:
      self.lateral_acceleration = DEFAULT_LATERAL_ACCELERATION

    collected_samples = sum(data["count"] for data in self.curvature_data.values())

    self.frogpilot_planner.params.put_nonblocking("CalibratedLateralAcceleration", self.lateral_acceleration)
    self.frogpilot_planner.params.put_nonblocking("CalibrationProgress", float(min(collected_samples / CALIBRATION_TARGET, 1.0) * 100))

  def update_max_limit(self, v_ego, sm):
    if sm["controlsState"].lateralControlState.which() in ("pidState", "torqueState"):
      controller_state = getattr(sm["controlsState"].lateralControlState, sm["controlsState"].lateralControlState.which())
      growth_rate = MAX_GROWTH_RATE * max(0, (MAX_TORQUE_HEADROOM - abs(controller_state.output)) / MAX_TORQUE_HEADROOM)
      minimum_speed = MIN_SATURATION_SPEED
      saturation_has_no_headroom = abs(controller_state.output) >= MAX_TORQUE_HEADROOM
    elif sm["controlsState"].lateralControlState.which() == "angleState":
      controller_state = sm["controlsState"].lateralControlState.angleState
      growth_rate = MAX_ANGLE_GROWTH_RATE
      minimum_speed = CRUISING_SPEED
      saturation_has_no_headroom = True
    else:
      return

    if self.max_limit <= 0:
      self.max_limit = DEFAULT_LATERAL_ACCELERATION

    lateral_acceleration = abs(self.frogpilot_planner.lateral_acceleration - sm["liveParameters"].roll * ACCELERATION_DUE_TO_GRAVITY)

    if controller_state.active and not sm["carState"].steeringPressed and v_ego > minimum_speed and lateral_acceleration >= MINIMUM_LATERAL_ACCELERATION:
      if controller_state.saturated:
        if saturation_has_no_headroom:
          self.max_limit *= 1 - MAX_BACKOFF_RATE * DT_MDL
      elif lateral_acceleration >= self.max_limit * MAX_TORQUE_HEADROOM and growth_rate > 0:
        self.max_limit *= 1 + growth_rate * DT_MDL
    self.max_limit = float(np.clip(self.max_limit, MINIMUM_LATERAL_ACCELERATION, MAX_LATERAL_ACCEL_NO_ROLL))

  def update_budget(self, frogpilot_toggles):
    lateral_acceleration = self.lateral_acceleration

    if frogpilot_toggles.curve_speed_profile == CURVE_SPEED_PROFILES["GENTLE"]:
      lateral_acceleration = GENTLE_LATERAL_ACCELERATION
    elif frogpilot_toggles.curve_speed_profile == CURVE_SPEED_PROFILES["STANDARD"]:
      lateral_acceleration = DEFAULT_LATERAL_ACCELERATION
    elif frogpilot_toggles.curve_speed_profile == CURVE_SPEED_PROFILES["SPORT"] and self.max_limit > 0:
      lateral_acceleration = self.max_limit

    if self.max_limit > 0:
      lateral_acceleration = min(lateral_acceleration, self.max_limit)

    if self.frogpilot_planner.frogpilot_weather.weather_id != 0:
      lateral_acceleration -= lateral_acceleration * self.frogpilot_planner.frogpilot_weather.reduce_lateral_acceleration

    self.budget = max(lateral_acceleration, 0)

  def update_target(self, v_ego):
    csc_speed = max((self.budget / abs(self.frogpilot_planner.road_curvature))**0.5, CRUISING_SPEED)

    if not self.target_set:
      self.target_set = True
      self.target = max(v_ego, csc_speed)

    if csc_speed < self.target:
      decel_rate = max(v_ego - csc_speed, 0) / max(self.frogpilot_planner.time_to_curve - TARGET_LEAD_TIME, 1)

      self.target = max(min(self.target, v_ego) - decel_rate * DT_MDL, csc_speed)
    elif v_ego <= self.target + TARGET_TRACKING_MARGIN and abs(self.frogpilot_planner.lateral_acceleration) < self.budget:
      self.target = min(self.target + TARGET_RISE_RATE * DT_MDL, csc_speed)
