"""Simple rules-based engine that turns a daily readiness check-in
into a list of recommended activities. No ML here on purpose -
this is the 'basic features first' version described in the project
notes; pose-detection / adaptive AI is future-phase work.
"""

EXERCISES = [
    {"id": "warmup", "name": "Warm-up", "kind": "warmup", "icon": "🚶",
     "duration": 2, "reps": None, "avoid": [],
     "desc": "Gentle full-body movement to get warmed up safely."},
    {"id": "chair_squat", "name": "Chair Squats", "kind": "strength", "icon": "🏋️",
     "duration": 3, "reps": 10, "avoid": ["knee"],
     "desc": "Lower-body strength using a chair for support."},
    {"id": "shoulder_mobility", "name": "Shoulder Mobility", "kind": "mobility", "icon": "🤸",
     "duration": 3, "reps": None, "avoid": ["shoulder"],
     "desc": "Loosens shoulders and upper back."},
    {"id": "indoor_walk", "name": "Indoor Walk", "kind": "cardio", "icon": "🚶",
     "duration": 5, "reps": None, "avoid": ["ankle", "foot"],
     "desc": "Light walking to build endurance."},
    {"id": "ankle_pumps", "name": "Ankle Pumps", "kind": "mobility", "icon": "🦶",
     "duration": 2, "reps": 15, "avoid": [],
     "desc": "Improves ankle circulation and flexibility."},
    {"id": "seated_marches", "name": "Seated Marches", "kind": "strength", "icon": "🪑",
     "duration": 3, "reps": 12, "avoid": [],
     "desc": "Seated hip-flexor strengthening, low impact."},
    {"id": "wall_pushups", "name": "Wall Push-ups", "kind": "strength", "icon": "💪",
     "duration": 3, "reps": 10, "avoid": ["shoulder", "wrist"],
     "desc": "Upper-body strength using a wall, no equipment."},
    {"id": "neck_stretch", "name": "Neck Stretch", "kind": "mobility", "icon": "🙆",
     "duration": 2, "reps": None, "avoid": ["neck"],
     "desc": "Relieves neck and upper back tension."},
    {"id": "balance_stand", "name": "Balance Stand", "kind": "balance", "icon": "🧍",
     "duration": 3, "reps": None, "avoid": ["ankle", "knee"],
     "desc": "Single-leg stand near a counter to build balance."},
    {"id": "cooldown", "name": "Cool-down Stretch", "kind": "mobility", "icon": "🧘",
     "duration": 2, "reps": None, "avoid": [],
     "desc": "Gentle stretches to finish the session safely."},
]

_ENERGY_PRIORITY = {
    "low": ["warmup", "neck_stretch", "shoulder_mobility", "ankle_pumps",
            "seated_marches", "balance_stand", "cooldown"],
    "okay": ["warmup", "shoulder_mobility", "seated_marches", "chair_squat",
             "ankle_pumps", "indoor_walk", "balance_stand", "cooldown"],
    "good": ["warmup", "chair_squat", "indoor_walk", "wall_pushups",
             "shoulder_mobility", "balance_stand", "seated_marches", "cooldown"],
    "great": ["warmup", "chair_squat", "wall_pushups", "indoor_walk",
              "balance_stand", "seated_marches", "shoulder_mobility", "cooldown"],
}

_BY_ID = {e["id"]: e for e in EXERCISES}


def generate_plan(energy, time_available, location, discomfort_areas):
    """Return an ordered list of exercise dicts that fit the given
    time budget while avoiding any flagged discomfort areas."""
    energy = energy if energy in _ENERGY_PRIORITY else "okay"
    budget = int(time_available) if time_available else 10
    discomfort_areas = set(discomfort_areas or [])

    order = _ENERGY_PRIORITY[energy]
    candidates = [_BY_ID[eid] for eid in order]

    # Outdoors naturally favors a walk; office favors seated/no-equipment work.
    if location == "outdoors":
        candidates.sort(key=lambda e: 0 if e["id"] == "indoor_walk" else 1)
    elif location == "office":
        candidates = [e for e in candidates if e["id"] != "indoor_walk"] + \
            [e for e in candidates if e["id"] == "indoor_walk"]

    safe = [e for e in candidates if not (set(e["avoid"]) & discomfort_areas)]
    if not safe:
        safe = [_BY_ID["warmup"], _BY_ID["cooldown"]]

    plan, total = [], 0
    include_cooldown = budget >= 8
    for ex in safe:
        if ex["id"] == "cooldown":
            continue
        if total + ex["duration"] > budget and plan:
            continue
        plan.append(ex)
        total += ex["duration"]
        if total >= budget:
            break

    if include_cooldown and all(e["id"] != "cooldown" for e in plan):
        plan.append(_BY_ID["cooldown"])
        total += _BY_ID["cooldown"]["duration"]

    if not plan:
        plan = [_BY_ID["warmup"]]

    return plan, total
