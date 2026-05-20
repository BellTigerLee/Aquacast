EXPORT_STAGE_TOPOLOGY_JSON = False
STAGE_TOPOLOGY_JSON_PATH = "/home/netai-sys/cs-project/Aquacast/extensions/aquacast.aquacast_composer/stage_topology.json"

ENABLE_FISH_SWIMMING = True
FISH_NAME_PREFIX = "Fish_"
WATER_PRIM_PATH = "/Root/FishTanks/Contents/Tank/InWater/MetalTank/Water"
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
