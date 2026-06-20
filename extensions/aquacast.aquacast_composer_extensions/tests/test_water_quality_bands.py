from pathlib import Path
import sys


EXT_ROOT = Path(__file__).resolve().parents[1]
if str(EXT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXT_ROOT))

import water_quality_bands


def test_salmon_ras_temperature_band_defaults():
    assert water_quality_bands.metric_state("temperature_c", 12.0)["state"] == "healthy"
    assert water_quality_bands.metric_state("temperature_c", 14.0)["state"] == "warn"
    assert water_quality_bands.metric_state("temperature_c", 18.1)["state"] == "critical"
