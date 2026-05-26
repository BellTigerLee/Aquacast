EXPORT_STAGE_TOPOLOGY_JSON = True
STAGE_TOPOLOGY_JSON_PATH = "/home/netai-sys/cs-project/Aquacast/extensions/aquacast.aquacast_composer_extensions/stage_topology.json"

ENABLE_AUTO_OPEN_STAGE = True
AUTO_OPEN_STAGE_PATH = "/home/netai-sys/cs-project/assets/Fishtank_test.usd"

ENABLE_FISH_SWIMMING = True
FISH_NAME_PREFIX = "Fish_"
WATER_PRIM_PATH = "/Root/Group/Water"
FISH_USE_STAGE_TOPOLOGY_JSON = True
FISH_INIT_RETRY_SECONDS = 1.0 #1.0

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

ISOSURFACE_PRIM_PATH = "/Root/Group/ParticleSystem/Isosurface"
TEMP_VIS_USE_STAGE_TOPOLOGY_JSON = True
TEMP_VIS_INIT_RETRY_SECONDS = 1.0

INITIAL_WATER_TEMP_C = 14.0
INLET_WATER_TEMP_C = 14.0
ROOM_TEMP_C = 22.0
THERMAL_K_ROOM = 0.012
THERMAL_K_INFLOW = 0.022
INFLOW_ENABLED_DEFAULT = True

TEMP_COLOR_STOPS = [
    (10.0, (0.05, 0.25, 1.00)),
    (14.0, (0.00, 0.75, 0.75)),
    (18.0, (0.90, 0.55, 0.20)),
    (25.0, (1.00, 0.12, 0.12)),
]

TEMP_VIS_LOG_INTERVAL_SECONDS = 5.0

# Runtime water temperature particles authored into the session layer at startup.
ENABLE_WATER_TEMP_PARTICLES = True
TEMP_PARTICLE_PRIM_PATH = "/Root/Group/TemperatureParticlesInsideWater"
# Increase or decrease this to control how many runtime point particles are authored.
TEMP_PARTICLE_COUNT = 8001
TEMP_PARTICLE_RANDOM_SEED = 42
TEMP_PARTICLE_RADIUS_RATIO = 0.94
TEMP_PARTICLE_HEIGHT_RATIO = 0.94
TEMP_PARTICLE_UP_AXIS = "Y"
TEMP_PARTICLE_WIDTH = 0.8
TEMP_PARTICLE_HEATING_MODE = "side"
TEMP_PARTICLE_HEAT_DELTA_C = 0.0
TEMP_PARTICLE_SPREAD_RATE = 0.05
TEMP_PARTICLE_UPDATE_INTERVAL_SECONDS = 0.12

# Test UI that samples particle temperatures near a Sensor prim.
ENABLE_WATER_TEMP_SENSOR_UI = True
TEMP_SENSOR_PRIM_PATH = "/Root/Group/Aquarium/AquariumComponents/FishTank/InWater/Components/Sensor"
TEMP_SENSOR_SAMPLE_RADIUS = 8.0
TEMP_SENSOR_FALLBACK_NEAREST_COUNT = 16
TEMP_SENSOR_UPDATE_INTERVAL_SECONDS = 0.5
