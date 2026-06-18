#!/usr/bin/env python3
import argparse
import gzip
import json
import os
import zipfile

from pathlib import Path


ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def compact_json(value):
  return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def write_member(archive, name, value):
  info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
  info.compress_type = zipfile.ZIP_DEFLATED
  info.create_system = 3
  info.external_attr = 0o644 << 16
  archive.writestr(info, compact_json(value), compresslevel=9)


def build_location_data(input_path, output_path):
  with gzip.open(input_path, "rt", encoding="utf-8") as location_file:
    location_data = json.load(location_file)

  records = location_data["records"]
  metadata = {key: value for key, value in location_data.items() if key not in {"admin1_capitals", "country_capitals", "grid", "records"}}
  capitals = {
    "admin1_capitals": {key: records[index] for key, index in location_data["admin1_capitals"].items()},
    "country_capitals": {key: records[index] for key, index in location_data["country_capitals"].items()},
  }

  output_path = Path(output_path)
  temporary_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
  try:
    with zipfile.ZipFile(temporary_path, "w") as archive:
      write_member(archive, "metadata.json", metadata)
      write_member(archive, "capitals.json", capitals)
      for key in sorted(location_data["grid"]):
        indexes = location_data["grid"][key]
        write_member(archive, f"grid/{key.replace(':', '/')}.json", [records[index] for index in indexes])
    os.replace(temporary_path, output_path)
  finally:
    temporary_path.unlink(missing_ok=True)


def main():
  parser = argparse.ArgumentParser(description="Build FrogPilot's bounded-memory location lookup archive")
  parser.add_argument("input", type=Path, help="Source location_data.json.gz")
  parser.add_argument("output", type=Path, help="Destination location_data.zip")
  args = parser.parse_args()
  build_location_data(args.input, args.output)


if __name__ == "__main__":
  main()
