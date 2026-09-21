import json
import os
from datetime import datetime, timezone


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(PROJECT_DIR, "results")


def result_dir(design):
    path = os.path.join(RESULTS_DIR, design)
    os.makedirs(path, exist_ok=True)
    return path


def result_file(design):
    return os.path.join(result_dir(design), "run.json")


def new_run(design, technology="sky130hd"):
    data = {
        "design": design,
        "technology": technology,
        "created_at": datetime.now(timezone.utc).isoformat(),

        "verification": {
            "status": "NOT_RUN",
            "tests": 0,
            "passed": 0,
            "failed": 0,
            "coverage_percent": None,
            "vcd": None
        },

        "synthesis": {
            "status": "NOT_RUN",
            "area_um2": None,
            "cell_count": None,
            "sequential_cells": None,
            "combinational_cells": None,
            "netlist": None
        },

        "physical": {
            "floorplan": "NOT_RUN",
            "placement": "NOT_RUN",
            "cts": "NOT_RUN",
            "routing": "NOT_RUN",
            "def": None,
            "odb": None,
            "spef": None,
            "gds": None
        },

        "timing": {
            "status": "NOT_RUN",
            "wns_ns": None,
            "tns_ns": None,
            "fmax_mhz": None,
            "clock_skew_ns": None
        },

        "power": {
            "status": "NOT_RUN",
            "internal_mw": None,
            "switching_mw": None,
            "leakage_mw": None,
            "total_mw": None
        },

        "signoff": {
            "drc": "NOT_RUN",
            "lvs": "NOT_RUN",
            "antenna": "NOT_RUN",
            "unrouted": None
        },

        "ai": {
            "status": "NOT_RUN",
            "bottleneck": None,
            "recommendations": []
        }
    }

    save_run(design, data)
    return data


def save_run(design, data):
    path = result_file(design)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return path


def load_run(design):
    path = result_file(design)

    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def update_run(design, section, values):
    data = load_run(design)

    if data is None:
        data = new_run(design)

    if section not in data:
        data[section] = {}

    if isinstance(data[section], dict):
        data[section].update(values)
    else:
        data[section] = values

    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    save_run(design, data)

    return data