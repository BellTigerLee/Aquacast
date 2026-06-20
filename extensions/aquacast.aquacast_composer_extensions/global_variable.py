EXPORT_STAGE_TOPOLOGY_JSON = False
ENABLE_STAGE_STRUCTURE_CACHE = False
STAGE_TOPOLOGY_INCLUDE_TRANSFORMS = False
STAGE_TOPOLOGY_INCLUDE_BOUNDS = False
STAGE_TOPOLOGY_TRANSFORM_PRECISION = 6
STAGE_TOPOLOGY_JSON_PATH = "~/cs-project/Aquacast/extensions/aquacast.aquacast_composer_extensions/stage_topology.json"

ENABLE_AUTO_OPEN_STAGE = True
AUTO_OPEN_STAGE_PATH = "~/cs-project/assets/scene.usd"

ENABLE_FISH_SWIMMING = True
FISH_NAME_PREFIX = "Fish_"
WATER_PRIM_PATH = ""
FISH_WATER_UP_AXIS = "Y"
FISH_USE_STAGE_TOPOLOGY_JSON = False
FISH_INIT_RETRY_SECONDS = 0.7 #1.0

# Runtime fish authoring. Environment variables with the AQUACAST_ prefix
# take precedence, e.g. AQUACAST_DYNAMIC_FISH_COUNT=40.
DYNAMIC_FISH_COUNT_PER_TANK = 0
DYNAMIC_FISH_SCALE = 1.0
DYNAMIC_FISH_SALMON_1_SCALE = 0.5
DYNAMIC_FISH_SALMON_2_SCALE = 1
DYNAMIC_FISH_SALMON_1_RATIO = 0.5
DYNAMIC_FISH_SALMON_1_PATH = "~/cs-project/assets/salmon_1.usd"
DYNAMIC_FISH_SALMON_2_PATH = "~/cs-project/assets/salmon_2.usd"
FISH_RNG_SEED = "random"

ENABLE_FISH_MANAGEMENT_UI = True
MAX_FISH_PER_TANK = 30
FISH_MANAGEMENT_UI_UPDATE_INTERVAL_SECONDS = 0.5
ENABLE_FISH_POPULATION_CSV = True
FISH_POPULATION_CSV_PATH = "~/cs-project/Aquacast/extensions/aquacast.aquacast_composer_extensions/fish_population.csv"
SYNC_WQ_STOCK_WITH_FISH_POPULATION = True
WQ_METRIC_BAND_THRESHOLDS = {
    "temperature_c": {
        "healthy": [{"gte": 11.5, "lte": 12.5}],
        "warn": [{"lt": 11.5}, {"gt": 12.5, "lte": 18.0}],
        "critical": [{"gt": 18.0}],
    },
    "dissolved_oxygen_mg_l": {
        "healthy": [{"gte": 8.0}],
        "warn": [{"gte": 5.0, "lt": 8.0}],
        "critical": [{"lt": 5.0}],
    },
    "tan_mg_l": {
        "healthy": [{"lt": 1.0}],
        "warn": [{"gte": 1.0, "lte": 3.0}],
        "critical": [{"gt": 3.0}],
    },
    "nh3_mg_l": {
        "healthy": [{"lt": 0.0125}],
        "warn": [{"gte": 0.0125, "lte": 0.05}],
        "critical": [{"gt": 0.05}],
    },
    "ph": {
        "healthy": [{"gte": 6.8, "lte": 8.3}],
        "warn": [{"gte": 6.0, "lt": 6.8}, {"gt": 8.3, "lte": 9.0}],
        "critical": [{"lt": 6.0}, {"gt": 9.0}],
    },
    "co2_mg_l": {
        "healthy": [{"gte": 0.0, "lte": 5.0}],
        "warn": [{"gt": 5.0, "lte": 15.0}],
        "critical": [{"gt": 15.0}],
    },
    "alkalinity_mg_l_as_caco3": {
        "healthy": [{"gte": 80.0, "lte": 150.0}],
        "warn": [{"gte": 50.0, "lt": 80.0}, {"gt": 150.0}],
        "critical": [{"lt": 50.0}],
    },
    "salinity_ppt": {
        "healthy": [{"gte": 0.0, "lte": 0.5}],
        "warn": [{"gt": 0.5}],
        "critical": [],
    },
    "turbidity_ntu": {
        "healthy": [{"lt": 5.0}],
        "warn": [{"gte": 5.0, "lte": 20.0}],
        "critical": [{"gt": 20.0}],
    },
    "nitrite_mg_l": {
        "healthy": [{"lt": 0.1}],
        "warn": [{"gte": 0.1, "lte": 1.0}],
        "critical": [{"gt": 1.0}],
    },
    "nitrate_mg_l": {
        "healthy": [{"lt": 50.0}],
        "warn": [{"gte": 50.0, "lte": 200.0}],
        "critical": [{"gt": 200.0}],
    },
}
ENABLE_FISH_SURVIVAL = True
FISH_SURVIVAL_DEATH_TICKS = 24
# Mortality is based on sustained Critical band exposure.
FISH_SURVIVAL_THRESHOLDS = WQ_METRIC_BAND_THRESHOLDS
FISH_SPECIES = [
    {
        "id": "salmon_1",
        "label": "Atlantic",
        "asset": DYNAMIC_FISH_SALMON_1_PATH,
        "scale": DYNAMIC_FISH_SALMON_1_SCALE,
        "weight_kg": 1.0,
    },
    {
        "id": "salmon_2",
        "label": "Chinook",
        "asset": DYNAMIC_FISH_SALMON_2_PATH,
        "scale": DYNAMIC_FISH_SALMON_2_SCALE,
        "weight_kg": 1.0,
    },
]

# Fish movement is scaled by the detected Water cylinder radius.
FISH_SWIM_SPEED_RADIUS_PER_SECOND = 0.12 #0.12
FISH_DIRECTION_LERP_RATE = 4.0 #4.0
FISH_MAX_TURN_RADIANS_PER_SECOND = 1.8 #1.8
FISH_BOUNDARY_START_RATIO = 0.85 # 0.68
FISH_BOUNDARY_MARGIN_RATIO = 0.12 #0.12
FISH_SEPARATION_RADIUS_RATIO = 0.18 #0.18
FISH_COHESION_WEIGHT = 0.18 #0.18
FISH_ALIGNMENT_WEIGHT = 0.25 #0.25
FISH_SEPARATION_WEIGHT = 0.42 #0.42
FISH_WANDER_WEIGHT = 0.20 #0.20
FISH_BOUNDARY_WEIGHT = 1.0 #1.35 - strong boundary repulsion is important to prevent fish from escaping the tank, which looks bad and can cause performance issues if they get too far away. The strong boundary weight also helps keep fish near the center of the tank where they are more visible, since the fish can sometimes get "stuck" swimming along the walls if the boundary weight is too low.
FISH_VERTICAL_WANDER_WEIGHT = 0.06 #0.12

# Realism dynamics: per-fish speed, preferred depth, decorrelated bob, banking.
ENABLE_REALISM_DYNAMICS = True

FISH_RNG_BASE_SEED = 1
FISH_MIN_SPEED_FRACTION = 0.4

FISH_CRUISE_SPEED_SCALE_RANGE = (0.85, 1.15)
FISH_SPEED_NOISE_AMPLITUDE_RANGE = (0.15, 0.35)
FISH_SPEED_NOISE_FREQ_HZ_RANGE = (0.05, 0.12)

FISH_DEPTH_BAND_CENTER_NORM_RANGE = (0.15, 0.85)
FISH_DEPTH_BAND_HALF_WIDTH_NORM_RANGE = (0.08, 0.18)
FISH_DEPTH_BAND_WEIGHT = 0.30

FISH_VERTICAL_WANDER_FREQ_HZ_RANGE = (0.07, 0.18)

FISH_BANK_GAIN_RANGE = (0.6, 1.0)
FISH_BANK_GAIN_GLOBAL = 0.35
FISH_MAX_BANK_RADIANS = 0.6
FISH_BANK_LERP_RATE = 3.0


# Water temperature visualization.
ENABLE_WATER_TEMP_VIS = True

# Legacy ParticleSystem/Isosurface color driving is disabled while
# TemperatureParticlesInsideWater is used for physical heat visualization.
ENABLE_PARTICLE_SYSTEM_TEMP_COLOR = False

ISOSURFACE_PRIM_PATH = ""
TEMP_VIS_USE_STAGE_TOPOLOGY_JSON = False
TEMP_VIS_INIT_RETRY_SECONDS = 0.2

INITIAL_WATER_TEMP_C = 10.5
INLET_WATER_TEMP_C = 10.5
ROOM_TEMP_C = 22.0
THERMAL_K_ROOM = 0.012
THERMAL_K_INFLOW = 0.022
INFLOW_ENABLED_DEFAULT = True

TEMP_COLOR_STOPS = [
    (10.0, (0.05, 0.25, 1.00)),
    (12.0, (0.05, 0.65, 1.00)),
    (15.0, (0.00, 0.75, 0.75)),
    (16.0, (0.85, 0.72, 0.10)),
    (18.0, (1.00, 0.32, 0.05)),
    (20.0, (1.00, 0.00, 0.00)),
]

TEMP_VIS_LOG_INTERVAL_SECONDS = 5.0

# Runtime water temperature particles authored into the session layer at startup.
# The particle prim is authored under the resolved Water prim parent, as a sibling of Water.
ENABLE_WATER_TEMP_PARTICLES = True
TEMP_PARTICLE_PRIM_PATH = "TemperatureParticlesInsideWater"
# Runtime particles are forced through UsdGeom.Points; sphere geometry is disabled for performance.
TEMP_PARTICLE_AUTHORING_MODE = "points"
# Increase or decrease this to control how many runtime particles are authored.
TEMP_PARTICLE_COUNT = 300
TEMP_PARTICLE_RANDOM_SEED = 42
TEMP_PARTICLE_RADIUS_RATIO = 0.94
TEMP_PARTICLE_HEIGHT_RATIO = 0.94
TEMP_PARTICLE_UP_AXIS = "Y"
# Visual radius of each temperature particle. Set to 0.0 to auto-size from the Water radius.
TEMP_PARTICLE_RADIUS = 0.0
# Backward-compatible alias. TEMP_PARTICLE_RADIUS takes precedence when set above 0.0.
TEMP_PARTICLE_WIDTH = 0.0
TEMP_PARTICLE_WIDTH_RATIO = 0.01
TEMP_PARTICLE_MIN_WIDTH = 5.0
TEMP_PARTICLE_DEBUG_COLOR = (1.0, 0.05, 0.0)
TEMP_PARTICLE_COLOR_BINS = 64
TEMP_PARTICLE_HEATING_MODE = "side"
TEMP_PARTICLE_HEAT_DELTA_C = 2.0
TEMP_PARTICLE_SPREAD_RATE = 0.05
TEMP_PARTICLE_UPDATE_INTERVAL_SECONDS = 0.12

# Test UI that samples particle temperatures near the inlet reference sensor prim.
ENABLE_WATER_TEMP_SENSOR_UI = True
TEMP_SENSOR_PRIM_PATH = ""
TEMP_SENSOR_PRIM_NAME = "inlet_reference"
TEMP_SENSOR_SAMPLE_RADIUS = 8.0
TEMP_SENSOR_FALLBACK_NEAREST_COUNT = 16
TEMP_SENSOR_UPDATE_INTERVAL_SECONDS = 0.5

# Water quality simulation.
ENABLE_WATER_QUALITY = True
ENABLE_WATER_QUALITY_SIM = ENABLE_WATER_QUALITY
WQ_ENABLE_NO2 = False
WQ_CONSTANTS_JSON_PATH = "~/cs-project/Aquacast/extensions/aquacast.aquacast_composer_extensions/data/wq_constants.json"
WQ_FEED_RATE_JSON_PATH = "~/cs-project/Aquacast/extensions/aquacast.aquacast_composer_extensions/data/wq_feed_rate.json"
WQ_SCENARIOS_JSON_PATH = "~/cs-project/Aquacast/extensions/aquacast.aquacast_composer_extensions/data/wq_scenarios.json"
WQ_METRIC_THRESHOLDS_JSON_PATH = "~/cs-project/Aquacast/extensions/aquacast.aquacast_composer_extensions/data/wq_metric_thresholds.json"
WQ_SCENARIO_NAME = "baseline"
WQ_BACKEND_ENABLED = True
WQ_BACKEND_URL = "http://127.0.0.1:8765"
WQ_BACKEND_TIMEOUT_SECONDS = 0.25
WQ_BACKEND_RESET_ON_CONNECT = False
WQ_TIME_SCALE = 1.0
WQ_SUBSTEP_H = 0.0167
WQ_INIT_DO = 9.0
WQ_INIT_TAN = 0.3
WQ_INIT_CO2 = 5.0
WQ_INIT_ALK = 120.0
WQ_INIT_SALINITY_PPT = 0.2
WQ_INIT_TURBIDITY_NTU = 2.0
WQ_TANK_VOLUME_L = 10000.0
WQ_FISH_COUNT = 200
WQ_FISH_WEIGHT_KG = 1.0
WQ_FLOW_LPH = 2000.0
WQ_PROTEIN_CONTENT = 0.45
WQ_KLA_O2 = 2.0
WQ_KLA_CO2 = 1.5
WQ_K_NITRIF = 0.8
WQ_VTR_MAX = 5.0
WQ_TAU_FEED_H = 4.0
WQ_DO_MAXFI = 7.0
WQ_DO_ZERO = 3.0
WQ_DO_IN = 9.0
WQ_TAN_IN_MG_L = 0.0
WQ_CO2_EQ = 0.5
WQ_ALK_IN = 120.0
WQ_SALINITY_IN_PPT = 0.2
WQ_TURBIDITY_IN_NTU = 1.0
WQ_SOLIDS_PER_FEED = 0.275
WQ_FISH_TAN_MG_KG_H = 2.5
WQ_FISH_TSS_MG_KG_H = 1.5
WQ_TURBIDITY_NTU_PER_MG_L_TSS = 0.35
WQ_TURBIDITY_SETTLE_H = 0.35
WQ_BIOFILTER_DEFAULT = True
WQ_UPDATE_INTERVAL_SECONDS = 1.0
WQ_LOG_INTERVAL_SECONDS = 5.0
WQ_WRITE_PARTICLE_COLORS = True
WQ_WRITE_PARTICLE_PRIMVARS = False
WQ_PARTICLE_UPDATE_INTERVAL_SECONDS = 1.0
WQ_PARTICLE_FIELD_UPDATE_INTERVAL_SECONDS = 0.5
WQ_VIEW_VARIABLE = "temperature"
ENABLE_WQ_METRICS_DASHBOARD = True
WQ_METRICS_DASHBOARD_UPDATE_INTERVAL_SECONDS = 0.5
WQ_METRICS_DASHBOARD_HISTORY_SECONDS = 180.0
WQ_METRICS_DASHBOARD_METRICS = [
    "temperature_c",
    "dissolved_oxygen_mg_l",
    "tan_mg_l",
    "nh3_mg_l",
    "ph",
    "co2_mg_l",
    "alkalinity_mg_l_as_caco3",
    "salinity_ppt",
    "turbidity_ntu",
    "nitrite_mg_l",
    "nitrate_mg_l",
]
WQ_METRICS_DASHBOARD_BEGINNER_METRICS = [
    "temperature_c",
    "dissolved_oxygen_mg_l",
    "tan_mg_l",
    "nh3_mg_l",
    "ph",
    "co2_mg_l",
    "turbidity_ntu",
]
WQ_METRIC_DASHBOARD_THRESHOLDS = WQ_METRIC_BAND_THRESHOLDS

# Operator-facing profile. Use "beginner" for guided service wording or
# "expert" for full technical evidence and all monitored metrics.
AQUACAST_OPERATOR_LEVEL = "beginner"

# Local LLM UI panel integrated into the Aquacast extension.
# Ollama is exposed on 1234 by docker-compose.ollama.yml.
ENABLE_LOCAL_LLM_PANEL = True
LOCAL_LLM_PANEL_OPEN_ON_STARTUP = False
LM_STUDIO_SERVER_URL = "http://127.0.0.1:1234"
LM_STUDIO_MODEL_NAME = "gemma4"
LM_STUDIO_POLL_INTERVAL_SECONDS = 60
LM_STUDIO_TIMEOUT_SECONDS = 120.0
LM_STUDIO_TEMPERATURE = 0.7
LM_STUDIO_MAX_TOKENS = 256
LM_STUDIO_SYSTEM_PROMPT = "You are a concise Aquacast aquaculture assistant. Use provided RAG context when relevant."
LM_STUDIO_DEFAULT_PROMPT = "You are connected to Aquacast. Give a concise status-style response."
LOCAL_LLM_PROVIDER = "ollama"
LOCAL_LLM_KEEP_ALIVE = "1h"
LOCAL_LLM_NUM_CTX = 4096
ENABLE_LOCAL_LLM_RAG = True
LOCAL_LLM_RAG_MANUALS_PATH = "~/cs-project/CSproject_Aqua/rag/manuals/documents.txt"
LOCAL_LLM_RAG_TOP_K = 3
LOCAL_LLM_RAG_MAX_CHARS = 3500
LOCAL_LLM_INCLUDE_WQ_DB_CONTEXT = True
LOCAL_LLM_WQ_CONTEXT_HOURS = 10.0 / 60.0
LOCAL_LLM_WQ_CONTEXT_LIMIT = 7200
LOCAL_LLM_WQ_CONTEXT_ALERT_LIMIT = 200
LOCAL_LLM_WQ_CONTEXT_MAX_CHARS = 5000
LOCAL_LLM_PANEL_DISPLAY_LOG_LIMIT = 100
LOCAL_LLM_RESPONSE_LOG_LIMIT = 0  # 0 keeps the panel response log unbounded for the session.
ENABLE_LOCAL_LLM_AUTO_ALERT_PROPOSALS = True
LOCAL_LLM_AUTO_ALERT_CHECK_INTERVAL_SECONDS = 10.0
LOCAL_LLM_AUTO_ALERT_SAME_EVENT_COOLDOWN_SECONDS = 60.0
LOCAL_LLM_AUTO_ALERT_RETRY_SECONDS = 60.0
LOCAL_LLM_AUTO_ALERT_MAX_PROPOSALS_PER_CHECK = 1
AI_PROPOSAL_BACKEND_URL = "http://127.0.0.1:8000"
AI_PROPOSAL_TIMEOUT_SECONDS = 60.0
AI_PROPOSAL_INBOX_LIMIT = 20

# Beginner onboarding panel. Keep copy ASCII/English for Kit font compatibility.
ENABLE_AQUACAST_TUTORIAL_PANEL = True
AQUACAST_TUTORIAL_OPEN_ON_STARTUP = True

# Practical operating thresholds for salmon/RAS-style water-quality views.
# Units match snapshot/sensor keys: degC, mg/L, pH, mg/L as CaCO3, ppt, NTU.
WQ_THRESHOLDS = {
    "temperature": {
        "operating": (11.5, 12.5),
        "warning_low": 11.5,
        "warning": (12.5, 18.0),
        "critical_high": 20.0,
    },
    "dissolved_oxygen": {
        "operating_saturation_pct": (90.0, 100.0),
        "warning_low_saturation_pct": 80.0,
        "critical_low_saturation_pct": 40.0,
        # Approximate mg/L equivalents near 12-15 C freshwater.
        "warning_low_mg_l": 8.0,
        "critical_low_mg_l": 4.0,
    },
    "ph": {
        "operating": (6.0, 8.5),
        "critical_low": 5.4,
        "critical_high": 9.0,
    },
    "co2": {
        "operating_high": 12.0,
        "warning": (12.0, 15.0),
        "critical_high": 15.0,
    },
    "tan": {
        "operating_high": 2.0,
        "warning_high": 2.0,
        "critical_note": "risk depends strongly on pH and temperature",
    },
    "nh3": {
        "operating_high": 0.0125,
        "warning_high": 0.0125,
        "critical_high": 0.02,
    },
    "alkalinity": {
        "operating_low": 70.0,
        "warning_low": 50.0,
        "critical_low": 10.0,
    },
    "salinity": {
        "freshwater_high": 0.5,
        "brackish_low": 1.0,
        "brackish_high": 35.0,
    },
    "turbidity": {
        "clear_high": 5.0,
        "cloudy_high": 55.0,
        "opaque_high": 515.0,
    },
}

DO_COLOR_STOPS = [
    (4.0, (0.00, 0.00, 0.00)),
    (6.0, (0.08, 0.08, 0.08)),
    (8.0, (0.28, 0.28, 0.28)),
    (9.0, (0.20, 0.80, 1.00)),
    (10.0, (0.85, 0.98, 1.00)),
]
TAN_COLOR_STOPS = [
    (0.0, (0.12, 0.70, 0.24)),
    (1.0, (0.40, 0.60, 0.30)),
    (2.0, (0.58, 0.22, 0.72)),
    (3.0, (0.82, 0.08, 1.00)),
]
CO2_COLOR_STOPS = [
    (0.5, (0.35, 0.85, 1.00)),
    (10.0, (0.45, 0.75, 0.85)),
    (12.0, (0.58, 0.58, 0.58)),
    (15.0, (0.42, 0.42, 0.42)),
    (25.0, (0.22, 0.22, 0.22)),
]
PH_COLOR_STOPS = [
    (5.4, (1.00, 0.05, 0.05)),
    (6.0, (1.00, 0.45, 0.05)),
    (7.0, (0.05, 0.75, 0.18)),
    (8.0, (0.05, 0.45, 1.00)),
    (8.5, (0.35, 0.15, 0.90)),
    (9.0, (0.75, 0.05, 0.95)),
]
ALK_COLOR_STOPS = [
    (10.0, (1.00, 0.90, 0.00)),
    (50.0, (0.95, 0.75, 0.08)),
    (70.0, (0.35, 0.75, 0.25)),
    (120.0, (0.12, 0.70, 0.35)),
    (180.0, (0.05, 0.55, 1.00)),
]
NH3_COLOR_STOPS = [
    (0.0, (0.02, 0.18, 0.08)),
    (0.0125, (0.10, 0.70, 0.20)),
    (0.02, (0.00, 1.00, 0.12)),
    (0.05, (0.62, 1.00, 0.00)),
]
SALINITY_COLOR_STOPS = [
    (0.0, (0.05, 0.45, 1.00)),
    (0.5, (0.10, 0.70, 0.95)),
    (1.0, (0.18, 0.85, 0.75)),
    (10.0, (0.65, 0.78, 0.35)),
    (35.0, (0.95, 0.82, 0.25)),
]
TURBIDITY_COLOR_STOPS = [
    (0.0, (0.02, 0.12, 0.18)),
    (5.0, (0.12, 0.65, 0.85)),
    (25.0, (0.78, 0.68, 0.38)),
    (55.0, (0.62, 0.46, 0.28)),
    (120.0, (0.28, 0.18, 0.10)),
]
WQ_VIEW_AMPLITUDE = {
    "temperature": 0.0,
    "dissolved_oxygen": 1.0,
    "tan": 1.0,
    "co2": 1.0,
    "ph": 0.3,
    "alkalinity": 1.0,
    "salinity": 0.2,
    "turbidity": 1.0,
    "nh3": 1.0,
}
FEEDINGS_PRIM_PATH = "/Root/Group/Aquarium/AquariumComponents/FishTank/Feedings"
INLET_PRIM_PATH = "/Root/Group/Aquarium/AquariumComponents/FishTank/inlet/Inlet_Trace_Source"
WQ_DEFAULT_SENSOR_NAME = "mixed_tank_outlet"
WQ_SENSOR_PRIM_NAMES = [
    "inlet_reference",
    "feed_zone_tan",
    "fish_core_do",
    "bottom_co2",
    "biofilter_sentinel",
    "mixed_tank_outlet",
]
