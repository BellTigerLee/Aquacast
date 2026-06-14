# Copyright (c) 2018-2020, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.
#

import asyncio
import builtins
import importlib.util
import inspect
import logging
import os
import platform
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


import carb
import omni.ext
import omni.kit.app
import omni.kit.menu.utils
import omni.kit.stage_templates as stage_templates
import omni.kit.window.property as property_window_ext
import omni.ui as ui
import omni.usd
from omni.kit.menu.utils import MenuLayout, MenuItemDescription
from omni.kit.property.usd import PrimPathWidget
from omni.kit.quicklayout import QuickLayout
from omni.kit.window.title import get_main_window_title

from .local_llm_panel import LocalLLMPanel

DATA_PATH = Path(carb.tokens.get_tokens_interface().resolve(
    "${aquacast.aquacast_composer_extensions}")
)

_RUNTIME_MODULE_NAME = "test_aquacast_runtime_main"
_RUNTIME_REGISTRY_NAME = "_aquacast_runtime_modules"
_WQ_BANDS_MODULE_NAME = "aquacast_water_quality_bands_for_extension"
_wq_bands_module = None


def _stop_runtime_module(module):
    if module is None:
        return
    for stop_name in (
        "stop_water_quality_controller",
        "stop_water_temp_controller",
        "stop_fish_swim_controller",
        "stop_stage_structure_cache",
    ):
        stop_fn = getattr(module, stop_name, None)
        if not callable(stop_fn):
            continue
        try:
            stop_fn()
        except Exception as exc:
            carb.log_warn(f"[test-Aquacast] Failed to run {stop_name} on stale runtime: {exc}")


def _stop_registered_runtime_modules():
    registry = getattr(builtins, _RUNTIME_REGISTRY_NAME, [])
    for module in list(registry):
        _stop_runtime_module(module)
    registry.clear()

    previous = sys.modules.get(_RUNTIME_MODULE_NAME)
    _stop_runtime_module(previous)


def _register_runtime_module(module):
    registry = getattr(builtins, _RUNTIME_REGISTRY_NAME, None)
    if registry is None:
        registry = []
        setattr(builtins, _RUNTIME_REGISTRY_NAME, registry)
    registry.append(module)


def _load_aquacast_main_module():
    main_path = Path(__file__).resolve().parents[2] / "main.py"
    spec = importlib.util.spec_from_file_location(_RUNTIME_MODULE_NAME, main_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Aquacast runtime module: {main_path}")

    module = importlib.util.module_from_spec(spec)
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
    _register_runtime_module(module)
    return module


def _get_runtime_config(name, default=None):
    config_path = Path(__file__).resolve().parents[2] / "global_variable.py"
    spec = importlib.util.spec_from_file_location("aquacast_global_variable_for_extension", config_path)
    if spec is None or spec.loader is None:
        return default
    module = importlib.util.module_from_spec(spec)
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        carb.log_warn(f"[test-Aquacast] Failed to read runtime config {config_path}: {exc}")
        return default
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
    return getattr(module, name, default)


def _get_wq_bands_module():
    global _wq_bands_module
    if _wq_bands_module is not None:
        return _wq_bands_module
    bands_path = Path(__file__).resolve().parents[2] / "water_quality_bands.py"
    spec = importlib.util.spec_from_file_location(_WQ_BANDS_MODULE_NAME, bands_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load water-quality bands module: {bands_path}")
    module = importlib.util.module_from_spec(spec)
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
    _wq_bands_module = module
    return module


async def _load_layout(layout_file: str, keep_windows_open=False):
    """Loads a provided layout file and ensures the viewport is set to FILL."""
    try:
        # few frames delay to avoid the conflict with the
        # layout of omni.kit.mainwindow
        for _ in range(3):
            await omni.kit.app.get_app().next_update_async()
        QuickLayout.load_file(layout_file, keep_windows_open)
    except:
        QuickLayout.load_file(layout_file)


class CreateSetupExtension(omni.ext.IExt):
    """Create Final Configuration"""
    _WQ_SENSOR_ROWS = (
        ("temperature_c", "Temp"),
        ("dissolved_oxygen_mg_l", "DO"),
        ("tan_mg_l", "TAN"),
        ("co2_mg_l", "CO2"),
        ("alkalinity_mg_l_as_caco3", "Alk"),
        ("salinity_ppt", "Salinity"),
        ("turbidity_ntu", "Turbidity"),
        ("ph", "pH"),
        ("nh3_mg_l", "NH3"),
        ("nitrite_mg_l", "Nitrite"),
        ("nitrate_mg_l", "Nitrate"),
        ("fish_count", "Fish"),
        ("fish_weight_kg", "Mean kg"),
        ("biomass_kg", "Biomass"),
        ("fish_o2_mg_h", "Fish O2"),
        ("fish_tan_kg_h", "Fish TAN"),
        ("total_tan_kg_h", "Total TAN"),
    )
    _WQ_SENSOR_FIELD_KEYS = {
        "total": tuple(key for key, _label in _WQ_SENSOR_ROWS),
        "inlet_reference": ("alkalinity_mg_l_as_caco3", "salinity_ppt", "turbidity_ntu"),
        "feed_zone_tan": ("tan_mg_l", "nh3_mg_l"),
        "fish_core_do": ("temperature_c", "ph"),
        "bottom_co2": ("co2_mg_l",),
        "biofilter_sentinel": ("nitrite_mg_l", "nitrate_mg_l"),
        "mixed_tank_outlet": ("dissolved_oxygen_mg_l",),
    }
    _WQ_ACTUATOR_ROWS = (
        ("inlet_enabled", "Inlet"),
        ("outlet_enabled", "Outlet"),
        ("biofilter_on", "Biofilter"),
        ("mechanical_filter_on", "Mech"),
        ("heater_on", "Heater"),
    )
    _WQ_STATUS_DOT_STYLES = {
        "on": {"background_color": 0xFF00C853, "border_radius": 6},
        "off": {"background_color": 0xFFE53935, "border_radius": 6},
        "unknown": {"background_color": 0xFF808080, "border_radius": 6},
    }
    _METRIC_DASHBOARD_SPECS = (
        {
            "key": "temperature_c",
            "label": "Water Temperature",
            "short": "Temp",
            "unit": "C",
            "default_threshold": 18.0,
            "mode": "max",
            "range": (6.0, 22.0),
            "min_span": 0.5,
            "color": 0xFFFF7043,
        },
        {
            "key": "dissolved_oxygen_mg_l",
            "label": "Dissolved Oxygen",
            "short": "DO",
            "unit": "mg/L",
            "default_threshold": 8.0,
            "mode": "min",
            "range": (0.0, 12.0),
            "min_span": 0.5,
            "color": 0xFF4EA7FF,
        },
        {
            "key": "tan_mg_l",
            "label": "Total Ammonia Nitrogen",
            "short": "TAN",
            "unit": "mg/L",
            "default_threshold": 2.0,
            "mode": "max",
            "range": (0.0, 3.0),
            "min_span": 0.05,
            "color": 0xFFFFB74D,
        },
        {
            "key": "nh3_mg_l",
            "label": "Unionized Ammonia",
            "short": "NH3",
            "unit": "mg/L",
            "default_threshold": 0.0125,
            "mode": "max",
            "range": (0.0, 0.05),
            "min_span": 0.002,
            "color": 0xFF00E676,
        },
        {
            "key": "ph",
            "label": "pH",
            "short": "pH",
            "unit": "",
            "default_threshold": 8.5,
            "mode": "max",
            "range": (5.0, 10.0),
            "min_span": 0.2,
            "color": 0xFFBA68C8,
        },
        {
            "key": "co2_mg_l",
            "label": "Carbon Dioxide",
            "short": "CO2",
            "unit": "mg/L",
            "default_threshold": 15.0,
            "mode": "max",
            "range": (0.0, 25.0),
            "min_span": 0.5,
            "color": 0xFF81C784,
        },
        {
            "key": "alkalinity_mg_l_as_caco3",
            "label": "Alkalinity",
            "short": "Alk",
            "unit": "mg/L CaCO3",
            "default_threshold": 70.0,
            "mode": "min",
            "range": (0.0, 180.0),
            "min_span": 5.0,
            "color": 0xFF66BB6A,
        },
        {
            "key": "salinity_ppt",
            "label": "Salinity",
            "short": "Salinity",
            "unit": "ppt",
            "default_threshold": 0.5,
            "mode": "max",
            "range": (0.0, 2.0),
            "min_span": 0.05,
            "color": 0xFF26C6DA,
        },
        {
            "key": "turbidity_ntu",
            "label": "Turbidity",
            "short": "Turb",
            "unit": "NTU",
            "default_threshold": 5.0,
            "mode": "max",
            "range": (0.0, 60.0),
            "min_span": 1.0,
            "color": 0xFFD4A056,
        },
        {
            "key": "nitrite_mg_l",
            "label": "Nitrite",
            "short": "NO2",
            "unit": "mg/L",
            "default_threshold": 0.1,
            "mode": "max",
            "range": (0.0, 1.0),
            "min_span": 0.02,
            "color": 0xFFEF5350,
        },
        {
            "key": "nitrate_mg_l",
            "label": "Nitrate",
            "short": "NO3",
            "unit": "mg/L",
            "default_threshold": 100.0,
            "mode": "max",
            "range": (0.0, 150.0),
            "min_span": 5.0,
            "color": 0xFF8D6E63,
        },
    )
    _METRIC_DASHBOARD_THRESHOLD_COLOR = 0xFFE53935
    _METRIC_DASHBOARD_BACKGROUND_COLOR = 0xFF111820
    _METRIC_DASHBOARD_GRID_COLOR = 0xFF263241
    _METRIC_DASHBOARD_STATE_COLORS = {
        "healthy": 0xFF00C853,
        "warn": 0xFFFFB300,
        "critical": 0xFFE53935,
        "unknown": 0xFF808080,
    }
    _METRIC_DASHBOARD_BAND_COLORS = {
        "healthy": 0xFF10251C,
        "warn": 0xFF302812,
        "critical": 0xFF301419,
    }

    def on_startup(self, _ext_id):
        """
        setup the window layout, menu, final configuration
        of the extensions etc
        """
        self._settings = carb.settings.get_settings()
        self._dev_reload_ext_id = _ext_id
        self._dev_reload_sub = None
        self._dev_reload_snapshot = {}
        self._dev_reload_last_check = 0.0
        self._dev_reload_requested = False
        self._stage_structure_cache = None
        self._dynamic_fish_spawner = None
        self._fish_swim_controller = None
        self._water_temp_controller = None
        self._water_quality_controller = None
        self._aquacast_main = None
        self._sensor_window = None
        self._sensor_update_sub = None
        self._sensor_last_update = 0.0
        self._sensor_status_label = None
        self._sensor_avg_label = None
        self._sensor_range_label = None
        self._sensor_samples_label = None
        self._sensor_path_label = None
        self._sensor_value_labels = {}
        self._sensor_value_rows = {}
        self._sensor_actuator_dots = {}
        self._sensor_tanks = []
        self._sensor_tank_labels = []
        self._sensor_tank_combo = None
        self._sensor_tank_index = 0
        self._sensor_names = []
        self._sensor_name_labels = []
        self._sensor_name_combo = None
        self._sensor_name_index = 0
        self._sensor_combo_change_subs = []
        self._sensor_menu_items = []
        self._wq_view_window = None
        self._wq_view_label = None
        self._wq_view_menu_items = []
        self._control_window = None
        self._control_menu_items = []
        self._control_tanks = []
        self._control_tank_labels = []
        self._control_tank_combo = None
        self._control_tank_index = 0
        self._control_fields = {}
        self._control_status_label = None
        self._control_rebuild_requested = False
        self._actuator_window = None
        self._actuator_update_sub = None
        self._actuator_menu_items = []
        self._actuator_last_update = 0.0
        self._actuator_status_label = None
        self._actuator_tanks = []
        self._actuator_tank_labels = []
        self._actuator_tank_dot_sets = {}
        self._metrics_window = None
        self._metrics_update_sub = None
        self._metrics_menu_items = []
        self._metrics_last_update = 0.0
        self._metrics_tanks = []
        self._metrics_tank_labels = []
        self._metrics_tank_combo = None
        self._metrics_tank_index = 0
        self._metrics_status_label = None
        self._metrics_chart_frames = {}
        self._metrics_current_labels = {}
        self._metrics_range_labels = {}
        self._metrics_state_frames = {}
        self._metrics_threshold_fields = {}
        self._metrics_history = {}
        self._metrics_thresholds = self._default_metric_thresholds()
        self._local_llm_panel = None
        self._local_llm_menu_items = []
        self._fish_window = None
        self._fish_update_sub = None
        self._fish_last_update = 0.0
        self._fish_menu_items = []
        self._fish_tanks = []
        self._fish_species = []
        self._fish_tank_labels = []
        self._fish_species_labels = []
        self._fish_tank_combo = None
        self._fish_species_combo = None
        self._fish_qty_field = None
        self._fish_count_label = None
        self._fish_status_label = None
        self._fish_add_button = None
        self._fish_delete_button = None
        self._fish_clear_button = None
        self._aquacast_menu_items = []
        self._start_dev_autoreload()
        test_mode = self._settings.get("/app/testMode")
        should_open_stage = (
            not test_mode
            and not self._settings.get("/app/content/emptyStageOnStart")
        )
        if should_open_stage:
            asyncio.ensure_future(self.__open_configured_stage_or_new(wait_frames=1))
        if self._settings and self._settings.get("/app/warmupMode"):
            # if warmup mode is enabled, we don't want to load the stage or
            # layout, just return
            return


        try:
            _stop_registered_runtime_modules()
            aquacast_main = _load_aquacast_main_module()
            self._aquacast_main = aquacast_main
            self._stage_structure_cache = aquacast_main.start_stage_structure_cache()
            self._dynamic_fish_spawner = aquacast_main.start_dynamic_fish_spawner()
            self._fish_swim_controller = aquacast_main.start_fish_swim_controller()
            self._water_temp_controller = aquacast_main.start_water_temp_controller()
            self._water_quality_controller = aquacast_main.start_water_quality_controller()
        except Exception as exc:
            carb.log_error(f"[test-Aquacast] Failed to start Aquacast runtime: {exc}")

        self._menu_layout = []

        telemetry_logger = logging.getLogger("idl.telemetry.opentelemetry")
        telemetry_logger.setLevel(logging.ERROR)

        # this is a work around as some Extensions don't properly setup their
        # default setting in time
        self._set_defaults()

        # adjust couple of viewport settings
        self._settings.set("/app/viewport/boundingBoxes/enabled", True)

        # These two settings do not co-operate well on ADA cards, so for
        # now simulate a toggle of the present thread on startup to work around
        if self._settings.get("/exts/omni.kit.renderer.core/present/enabled") \
            and self._settings.get(
            "/exts/omni.kit.widget.viewport/autoAttach/mode"
        ):
            async def _toggle_present(settings, n_waits: int = 1):
                async def _toggle_setting(app, enabled: bool, n_waits: int):
                    for _ in range(n_waits):
                        await app.next_update_async()
                    settings.set(
                        "/exts/omni.kit.renderer.core/present/enabled",
                        enabled
                    )

                app = omni.kit.app.get_app()
                await _toggle_setting(app, False, n_waits)
                await _toggle_setting(app, True, n_waits)

            asyncio.ensure_future(_toggle_present(self._settings))

        # Setting and Saving FSD as a global change in preferences
        # Requires to listen for changes at the local path to update
        # Composer's persistent path.
        fabric_app_setting = self._settings.get("/app/useFabricSceneDelegate")
        fabric_persistent_setting = self._settings.get(
            "/persistent/app/useFabricSceneDelegate"
        )
        fabric_enabled: bool = fabric_app_setting if \
            fabric_persistent_setting is None else fabric_persistent_setting

        self._settings.set("/app/useFabricSceneDelegate", fabric_enabled)

        self._sub_fabric_delegate_changed = \
            omni.kit.app.SettingChangeSubscription(
                "/app/useFabricSceneDelegate",
                self._on_fabric_delegate_changed
            )

        # Adjust the Window Title to show the Create Version
        window_title = get_main_window_title()

        app_version = self._settings.get("/app/version")
        if not app_version:
            with open(
                carb.tokens.get_tokens_interface().resolve("${app}/../VERSION"),
                encoding="utf-8"
            ) as f:
                app_version = f.read()

        if app_version:
            if "+" in app_version:
                app_version, _ = app_version.split("+")

            # for RC version we remove some details
            if self._settings.get("/privacy/externalBuild"):
                if "-" in app_version:
                    app_version, _ = app_version.split("-")
                window_title.set_app_version(app_version)
            else:
                window_title.set_app_version(app_version)

        imgui_style_applied = False
        try:
            # using imgui directly to adjust some color and Variable
            import omni.kit.imgui as _imgui
            imgui = _imgui.acquire_imgui()
            if imgui.is_valid():
                imgui.push_style_color(_imgui.StyleColor.ScrollbarGrab, carb.Float4(0.4, 0.4, 0.4, 1))
                imgui.push_style_color(_imgui.StyleColor.ScrollbarGrabHovered, carb.Float4(0.6, 0.6, 0.6, 1))
                imgui.push_style_color(_imgui.StyleColor.ScrollbarGrabActive, carb.Float4(0.8, 0.8, 0.8, 1))
                imgui.push_style_var_float(_imgui.StyleVar.DockSplitterSize, 2)
                imgui_style_applied = True
        except ImportError:
            pass

        if not imgui_style_applied:
            carb.log_error("Style may not be as expected (carb.imgui was not valid)")

        layout_file = f"{DATA_PATH}/layouts/default.json"

        if not test_mode:
            asyncio.ensure_future(_load_layout(layout_file, True))

        asyncio.ensure_future(self.__property_window())

        self.__menu_update()
        self._register_aquacast_menu()
        self._create_sensor_window()
        self._create_wq_view_window()
        self._create_control_window()
        self._create_actuator_window()
        self._create_metrics_dashboard_window()
        self._create_local_llm_panel()
        self._create_fish_window()

        startup_time = \
            omni.kit.app.get_app_interface().get_time_since_start_s()
        self._settings.set(
            "/crashreporter/data/startup_time", f"{startup_time}"
        )

        def show_documentation(*args):
            webbrowser.open(
                "https://docs.omniverse.nvidia.com/composer/latest/index.html"
            )
        self._help_menu_items = [
            MenuItemDescription(
                name="Documentation",
                onclick_fn=show_documentation,
                appear_after=[omni.kit.menu.utils.MenuItemOrder.FIRST]
            )
        ]
        omni.kit.menu.utils.add_menu_items(self._help_menu_items, name="Help")

    def _set_defaults(self):
        """
        This is trying to setup some defaults for extensions to avoid warnings.
        """
        self._settings.set_default("/persistent/app/omniverse/bookmarks", {})
        self._settings.set_default(
            "/persistent/app/stage/timeCodeRange", [0, 100]
        )

        self._settings.set_default(
            "/persistent/audio/context/closeAudioPlayerOnStop",
            False
        )

        self._settings.set_default(
            "/persistent/app/primCreation/PrimCreationWithDefaultXformOps",
            True
        )
        self._settings.set_default(
            "/persistent/app/primCreation/DefaultXformOpType",
            "Scale, Rotate, Translate"
        )
        self._settings.set_default(
            "/persistent/app/primCreation/DefaultRotationOrder",
            "ZYX"
        )
        self._settings.set_default(
            "/persistent/app/primCreation/DefaultXformOpPrecision",
            "Double"
        )

        # omni.kit.property.tagging
        self._settings.set_default(
            "/persistent/exts/omni.kit.property.tagging/showAdvancedTagView",
            False
        )
        self._settings.set_default(
            "/persistent/exts/omni.kit.property.tagging/showHiddenTags",
            False
        )
        self._settings.set_default(
            "/persistent/exts/omni.kit.property.tagging/modifyHiddenTags",
            False
        )

        self._settings.set_default(
            "/rtx/sceneDb/ambientLightIntensity", 0.0
        )  # set default ambientLight intensity to Zero

    def _on_fabric_delegate_changed(
            self, _v: str, event_type: carb.settings.ChangeEventType):
        if event_type == carb.settings.ChangeEventType.CHANGED:
            enabled: bool = self._settings.get_as_bool(
                "/app/useFabricSceneDelegate"
            )
            self._settings.set(
                "/persistent/app/useFabricSceneDelegate", enabled
            )

    async def __open_configured_stage_or_new(self, wait_frames=1):
        """Open the configured Aquacast scene, falling back to a new stage."""
        for _ in range(max(0, int(wait_frames))):
            await omni.kit.app.get_app().next_update_async()

        usd_context = omni.usd.get_context()
        if not usd_context.can_open_stage() or usd_context.get_stage_url():
            return

        stage_path = str(_get_runtime_config("AUTO_OPEN_STAGE_PATH", "") or "").strip()
        if bool(_get_runtime_config("ENABLE_AUTO_OPEN_STAGE", False)) and stage_path:
            path = Path(stage_path).expanduser()
            if path.exists():
                carb.log_info(f"[test-Aquacast] Opening configured stage: {path}")
                result = usd_context.open_stage(str(path))
                if inspect.isawaitable(result):
                    await result
                return
            carb.log_warn(f"[test-Aquacast] AUTO_OPEN_STAGE_PATH does not exist: {path}")

        stage_templates.new_stage(template=None)

    def _launch_app(self, app_id, console=True, custom_args=None):
        """launch another Kit app with the same settings"""
        app_path = carb.tokens.get_tokens_interface().resolve("${app}")
        kit_file_path = os.path.join(app_path, app_id)

        # https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html
        # Validate input from command line (detected in static analysis)
        kit_exe = sys.argv[0]
        if not os.path.exists(kit_exe):
            print(f"cannot find executable{kit_exe}")
            return

        launch_args = [kit_exe]
        launch_args += [kit_file_path]
        if custom_args:
            launch_args.extend(custom_args)

        # Pass all exts folders
        exts_folders = self._settings.get("/app/exts/folders")
        if exts_folders:
            for folder in exts_folders:
                launch_args.extend(["--ext-folder", folder])

        kwargs = {"close_fds": False}
        if platform.system().lower() == "windows":
            if console:
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE | \
                    subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        subprocess.Popen(launch_args, **kwargs)

    def _show_ui_docs(self):
        """show the omniverse ui documentation as an external Application"""
        self._launch_app("omni.app.uidoc.kit")

    def _show_launcher(self):
        """show the omniverse ui documentation as an external Application"""
        self._launch_app(
            "omni.create.launcher.kit",
            console=False,
            custom_args={"--/app/auto_launch=false"}
        )

    def _create_sensor_window(self):
        if not bool(_get_runtime_config("ENABLE_WATER_TEMP_SENSOR_UI", True)):
            return

        quality_enabled = bool(_get_runtime_config("ENABLE_WATER_QUALITY", _get_runtime_config("ENABLE_WATER_QUALITY_SIM", False)))
        title = "Aquacast Water Quality Sensor" if quality_enabled else "Aquacast Temperature Sensor"
        self._sensor_window = ui.Window(title, width=500, height=450 if quality_enabled else 310, visible=True)
        self._sensor_tanks, self._sensor_tank_labels = self._sensor_tank_window_data()
        self._sensor_names = self._sensor_name_window_data() if quality_enabled else []
        self._sensor_name_labels = [self._sensor_label_for_name(name) for name in self._sensor_names]
        self._build_sensor_window_contents(quality_enabled)

        self._sensor_update_sub = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_sensor_ui_update,
            name="aquacast_temperature_sensor_ui",
        )
        self._register_sensor_window_menu()
        self._on_sensor_ui_update(None)

    def _build_sensor_window_contents(self, quality_enabled):
        if self._sensor_window is None:
            return
        self._sensor_combo_change_subs = []
        with self._sensor_window.frame:
            with ui.ScrollingFrame():
                with ui.VStack(spacing=6):
                    ui.Label("Water Quality Sensor" if quality_enabled else "Temperature Sensor", height=22)
                    if quality_enabled:
                        with ui.HStack(height=26, spacing=6):
                            ui.Label("Tank:", width=70)
                            self._sensor_tank_index = self._clamp_index(self._sensor_tank_index, self._sensor_tank_labels)
                            self._sensor_tank_combo = ui.ComboBox(self._sensor_tank_index, *(self._sensor_tank_labels or ["(no tanks)"]))
                            self._bind_sensor_combo(self._sensor_tank_combo, "_sensor_tank_index")
                        with ui.HStack(height=26, spacing=6):
                            ui.Label("Sensor:", width=70)
                            self._sensor_name_index = self._clamp_index(self._sensor_name_index, self._sensor_names)
                            self._sensor_name_combo = ui.ComboBox(self._sensor_name_index, *(self._sensor_name_labels or ["mixed_tank_outlet"]))
                            self._bind_sensor_combo(self._sensor_name_combo, "_sensor_name_index")
                    self._sensor_status_label = ui.Label("Waiting for particles", height=20)
                    self._sensor_value_labels = {}
                    self._sensor_value_rows = {}
                    rows = list(self._WQ_SENSOR_ROWS) if quality_enabled else [
                        ("average_c", "Average"),
                        ("range_c", "Range"),
                        ("sample_count", "Samples"),
                    ]
                    for key, label in rows:
                        row = ui.HStack(height=20)
                        with row:
                            ui.Label(label, width=115)
                            value_label = ui.Label("--")
                            self._sensor_value_labels[key] = value_label
                        self._sensor_value_rows[key] = row
                    if quality_enabled:
                        self._apply_sensor_row_visibility(self._selected_sensor_name())
                        self._build_sensor_actuator_rows()
                    self._sensor_path_label = ui.Label("Sensor: --", height=42, word_wrap=True)

    def _build_sensor_actuator_rows(self):
        self._sensor_actuator_dots = {}
        ui.Label("Actuators", height=18)
        with ui.HStack(height=22, spacing=8):
            for key, label in self._WQ_ACTUATOR_ROWS:
                with ui.HStack(width=88, spacing=2):
                    ui.Label(label, width=68)
                    self._sensor_actuator_dots[key] = self._build_status_indicator()

    def _build_status_indicator(self, size=12):
        dots = {}
        for state in ("on", "off", "unknown"):
            dot = ui.Rectangle(width=size, height=size, style=self._WQ_STATUS_DOT_STYLES[state])
            dot.visible = state == "unknown"
            dots[state] = dot
        return dots

    def _set_status_indicator(self, dots, value):
        if value is None:
            state = "unknown"
        else:
            state = "on" if bool(value) else "off"
        for dot_state, dot in (dots or {}).items():
            try:
                dot.visible = dot_state == state
            except Exception:
                pass

    def _register_sensor_window_menu(self):
        if self._sensor_menu_items:
            return
        quality_enabled = bool(_get_runtime_config("ENABLE_WATER_QUALITY", _get_runtime_config("ENABLE_WATER_QUALITY_SIM", False)))
        self._sensor_menu_items = [
            MenuItemDescription(
                name="Aquacast/Water Quality Sensor" if quality_enabled else "Aquacast/Temperature Sensor",
                onclick_fn=self._show_sensor_window,
            )
        ]
        omni.kit.menu.utils.add_menu_items(self._sensor_menu_items, name="Window")

    def _show_sensor_window(self):
        if self._sensor_window is None:
            self._create_sensor_window()
            return
        self._sensor_window.visible = True

    def _sensor_tank_window_data(self):
        tanks = []
        if self._aquacast_main is not None:
            try:
                tanks = list(self._aquacast_main.list_fish_tanks())
            except Exception as exc:
                carb.log_warn(f"[test-Aquacast] Sensor UI tank refresh failed: {exc}")
        labels = self._unique_sensor_tank_labels(tanks)
        return tanks, labels

    def _sensor_name_window_data(self):
        names = []
        if self._aquacast_main is not None and hasattr(self._aquacast_main, "list_water_quality_sensor_names"):
            try:
                names = list(self._aquacast_main.list_water_quality_sensor_names())
            except Exception as exc:
                carb.log_warn(f"[test-Aquacast] Sensor UI name refresh failed: {exc}")
        if not names:
            names = list(_get_runtime_config("WQ_SENSOR_PRIM_NAMES", []) or [])
        sensor_names = [str(name) for name in names]
        return ["total"] + [name for name in sensor_names if name != "total"] or ["total", "mixed_tank_outlet"]

    def _sensor_tank_label(self, path):
        path = str(path or "")
        parts = [part for part in path.strip("/").split("/") if part]
        if not parts:
            return path
        candidates = parts[:-1] if parts[-1] == "Water" else parts
        generic = {"Root", "scene", "Meshes", "Model", "Components", "Component", "Water"}
        for part in reversed(candidates):
            if part in generic:
                continue
            if part.startswith("Group") and part[5:].isdigit():
                continue
            return part
        return candidates[-1] if candidates else parts[-1]

    def _unique_sensor_tank_labels(self, tanks):
        raw_labels = [self._sensor_tank_label(path) for path in tanks]
        counts = {label: raw_labels.count(label) for label in raw_labels}
        seen = {}
        labels = []
        for path, label in zip(tanks, raw_labels):
            seen[label] = seen.get(label, 0) + 1
            if counts[label] <= 1:
                labels.append(label)
                continue
            suffix = str(path).rstrip("/").rsplit("/", 3)[0].rsplit("/", 1)[-1]
            labels.append(f"{label} #{seen[label]} ({suffix})")
        return labels

    def _sensor_label_for_name(self, name):
        return {
            "total": "Total",
            "inlet_reference": "Inlet reference",
            "feed_zone_tan": "Feed TAN",
            "fish_core_do": "Fish core DO",
            "bottom_co2": "Bottom CO2",
            "biofilter_sentinel": "Biofilter",
            "mixed_tank_outlet": "Mixed outlet",
        }.get(str(name), str(name))

    def _wq_sensor_fields_for_name(self, name):
        sensor_name = str(name or "")
        fields = self._WQ_SENSOR_FIELD_KEYS.get(sensor_name)
        if fields is not None:
            return tuple(fields)
        return tuple(key for key, _label in self._WQ_SENSOR_ROWS)

    def _apply_sensor_row_visibility(self, sensor_name):
        visible_keys = set(self._wq_sensor_fields_for_name(sensor_name))
        for key, row in self._sensor_value_rows.items():
            try:
                row.visible = key in visible_keys
            except Exception:
                pass
            if key not in visible_keys:
                self._set_sensor_label(key, "--")

    def _format_wq_sensor_value(self, key, reading):
        value = float(reading.get(key, 0.0))
        if key == "temperature_c":
            return f"{value:.2f} C"
        if key == "dissolved_oxygen_mg_l":
            return f"{value:.2f} mg/L"
        if key == "tan_mg_l":
            return f"{value:.3f} mg/L"
        if key == "co2_mg_l":
            return f"{value:.2f} mg/L"
        if key == "alkalinity_mg_l_as_caco3":
            return f"{value:.1f} mg/L CaCO3"
        if key == "salinity_ppt":
            return f"{value:.2f} ppt"
        if key == "turbidity_ntu":
            return f"{value:.1f} NTU"
        if key == "ph":
            return f"{value:.2f}"
        if key in {"nh3_mg_l", "nitrite_mg_l", "nitrate_mg_l"}:
            return f"{value:.4f} mg/L"
        if key == "fish_count":
            return f"{value:.0f}"
        if key == "fish_weight_kg":
            return f"{value:.2f} kg"
        if key == "biomass_kg":
            return f"{value:.1f} kg"
        if key == "fish_o2_mg_h":
            return f"{value:.1f} mg/h"
        if key in {"fish_tan_kg_h", "total_tan_kg_h"}:
            return f"{value:.6f} kg/h"
        return str(reading.get(key, "--"))

    def _update_sensor_actuator_statuses(self, reading):
        if not self._sensor_actuator_dots:
            return
        for key, _label in self._WQ_ACTUATOR_ROWS:
            self._set_status_indicator(
                self._sensor_actuator_dots.get(key, {}),
                reading.get(key) if key in reading else None,
            )

    def _selected_sensor_tank(self):
        if not self._sensor_tanks:
            return None
        index = self._clamp_index(self._sensor_combo_index(self._sensor_tank_combo, self._sensor_tank_index), self._sensor_tanks)
        self._sensor_tank_index = index
        return self._sensor_tanks[index]

    def _selected_sensor_name(self):
        if not self._sensor_names:
            return "mixed_tank_outlet"
        index = self._clamp_index(self._sensor_combo_index(self._sensor_name_combo, self._sensor_name_index), self._sensor_names)
        self._sensor_name_index = index
        return self._sensor_names[index]

    def _clamp_index(self, index, items):
        if not items:
            return 0
        try:
            value = int(index)
        except Exception:
            value = 0
        return max(0, min(value, len(items) - 1))

    def _sensor_combo_index(self, combo, fallback=0):
        if combo is None:
            return self._clamp_index(fallback, [None])
        try:
            return int(combo.model.get_item_value_model().as_int)
        except Exception:
            pass
        try:
            return int(combo.model.get_item_value_model().get_value_as_int())
        except Exception:
            return int(fallback or 0)

    def _bind_sensor_combo(self, combo, index_attr):
        try:
            value_model = combo.model.get_item_value_model()
            sub = value_model.add_value_changed_fn(
                lambda *args, attr=index_attr, value_model=value_model: self._on_sensor_combo_changed(
                    attr,
                    args[0] if args else value_model,
                )
            )
            self._sensor_combo_change_subs.append(sub)
        except Exception:
            pass

    def _on_sensor_combo_changed(self, index_attr, model):
        try:
            index = int(model.as_int)
        except Exception:
            try:
                index = int(model.get_value_as_int())
            except Exception:
                index = 0
        setattr(self, index_attr, index)
        self._sensor_last_update = 0.0
        try:
            self._on_sensor_ui_update(None)
        except Exception as exc:
            carb.log_warn(f"[test-Aquacast] Sensor UI selection update failed: {exc}")

    def _refresh_sensor_selector_data(self):
        quality_enabled = bool(_get_runtime_config("ENABLE_WATER_QUALITY", _get_runtime_config("ENABLE_WATER_QUALITY_SIM", False)))
        if not quality_enabled:
            return
        selected_tank = self._selected_sensor_tank()
        selected_name = self._selected_sensor_name()
        tanks, labels = self._sensor_tank_window_data()
        names = self._sensor_name_window_data()
        name_labels = [self._sensor_label_for_name(name) for name in names]
        if tanks != self._sensor_tanks or labels != self._sensor_tank_labels or names != self._sensor_names:
            self._sensor_tank_index = tanks.index(selected_tank) if selected_tank in tanks else self._clamp_index(self._sensor_tank_index, tanks)
            self._sensor_name_index = names.index(selected_name) if selected_name in names else self._clamp_index(self._sensor_name_index, names)
            self._sensor_tanks = tanks
            self._sensor_tank_labels = labels
            self._sensor_names = names
            self._sensor_name_labels = name_labels
            # ComboBox item lists are fixed after construction in this UI version.
            # Rebuild the existing frame only when the underlying stage sensor set changes.
            self._build_sensor_window_contents(quality_enabled)

    def _on_sensor_ui_update(self, _event):
        if self._sensor_window is None or self._aquacast_main is None:
            return
        now = time.monotonic()
        interval = float(_get_runtime_config("TEMP_SENSOR_UPDATE_INTERVAL_SECONDS", 0.5) or 0.5)
        if now - self._sensor_last_update < max(0.05, interval):
            return
        self._sensor_last_update = now

        try:
            quality_enabled = bool(_get_runtime_config("ENABLE_WATER_QUALITY", _get_runtime_config("ENABLE_WATER_QUALITY_SIM", False)))
            self._refresh_sensor_selector_data()
            if quality_enabled and hasattr(self._aquacast_main, "sample_water_quality_sensor"):
                reading = self._aquacast_main.sample_water_quality_sensor(
                    self._selected_sensor_name(),
                    tank_path=self._selected_sensor_tank(),
                )
            else:
                reading = self._aquacast_main.sample_water_temp_sensor()
        except Exception as exc:
            reading = {"status": f"sensor read failed: {exc}"}

        status = reading.get("status", "unknown")
        if status != "ok":
            if self._sensor_status_label:
                self._sensor_status_label.text = status
            for label in self._sensor_value_labels.values():
                label.text = "--"
            self._update_sensor_actuator_statuses({})
            if self._sensor_path_label:
                self._sensor_path_label.text = f"Sensor: {reading.get('sensor_path', '--')}"
            return

        if "dissolved_oxygen_mg_l" in reading:
            sensor_name = str(reading.get("sensor_name") or self._selected_sensor_name())
            visible_keys = self._wq_sensor_fields_for_name(sensor_name)
            self._apply_sensor_row_visibility(sensor_name)
            if self._sensor_status_label:
                self._sensor_status_label.text = self._sensor_label_for_name(sensor_name)
            for key in visible_keys:
                self._set_sensor_label(key, self._format_wq_sensor_value(key, reading))
            self._update_sensor_actuator_statuses(reading)
            if self._sensor_path_label:
                tank = reading.get("tank_name") or reading.get("tank_path") or "--"
                self._sensor_path_label.text = f"Tank: {tank}\nSensor: {reading.get('sensor_path', '--')}"
            return

        fallback = " nearest" if reading.get("used_fallback") else ""
        if self._sensor_status_label:
            self._sensor_status_label.text = f"Radius {reading['radius']:.2f}{fallback}"
        self._set_sensor_label("average_c", f"{reading['average_c']:.2f} C")
        self._set_sensor_label("range_c", f"{reading['min_c']:.2f} - {reading['max_c']:.2f} C")
        self._set_sensor_label("sample_count", str(reading["sample_count"]))
        if self._sensor_path_label:
            self._sensor_path_label.text = f"Sensor: {reading['sensor_path']}"

    def _set_sensor_label(self, key, value):
        label = self._sensor_value_labels.get(key)
        if label:
            label.text = value

    def _create_wq_view_window(self):
        if not bool(_get_runtime_config("ENABLE_WATER_QUALITY", _get_runtime_config("ENABLE_WATER_QUALITY_SIM", False))):
            return
        if self._wq_view_window is not None:
            return

        self._wq_view_window = ui.Window("Aquacast Water Quality View", width=300, height=220, visible=True)
        with self._wq_view_window.frame:
            with ui.ScrollingFrame():
                with ui.VStack(spacing=6):
                    self._wq_view_label = ui.Label("Current: --", height=22)
                    with ui.HStack(height=28, spacing=6):
                        ui.Button("Temp", clicked_fn=lambda: self._select_wq_view_variable("temperature"))
                        ui.Button("DO", clicked_fn=lambda: self._select_wq_view_variable("dissolved_oxygen"))
                        ui.Button("TAN", clicked_fn=lambda: self._select_wq_view_variable("tan"))
                    with ui.HStack(height=28, spacing=6):
                        ui.Button("CO2", clicked_fn=lambda: self._select_wq_view_variable("co2"))
                        ui.Button("pH", clicked_fn=lambda: self._select_wq_view_variable("ph"))
                        ui.Button("Alk", clicked_fn=lambda: self._select_wq_view_variable("alkalinity"))
                    with ui.HStack(height=28, spacing=6):
                        ui.Button("NH3", clicked_fn=lambda: self._select_wq_view_variable("nh3"))
                        ui.Button("Sal", clicked_fn=lambda: self._select_wq_view_variable("salinity"))
                        ui.Button("Turb", clicked_fn=lambda: self._select_wq_view_variable("turbidity"))

        self._register_wq_view_window_menu()
        self._refresh_wq_view_label()

    def _register_wq_view_window_menu(self):
        if self._wq_view_menu_items:
            return
        self._wq_view_menu_items = [
            MenuItemDescription(
                name="Aquacast/Water Quality View Controls",
                onclick_fn=self._show_wq_view_window,
            )
        ]
        omni.kit.menu.utils.add_menu_items(self._wq_view_menu_items, name="Window")

    def _show_wq_view_window(self):
        if self._wq_view_window is None:
            self._create_wq_view_window()
            return
        self._wq_view_window.visible = True
        self._refresh_wq_view_label()

    def _select_wq_view_variable(self, variable):
        if self._aquacast_main and hasattr(self._aquacast_main, "set_quality_view_variable"):
            self._aquacast_main.set_quality_view_variable(variable)
        self._refresh_wq_view_label(variable)

    def _refresh_wq_view_label(self, fallback=None):
        if self._wq_view_label is None:
            return
        variable = fallback
        if self._aquacast_main and hasattr(self._aquacast_main, "get_quality_snapshot"):
            try:
                snapshot = self._aquacast_main.get_quality_snapshot()
                if snapshot.get("status") == "ok":
                    variable = snapshot.get("view_variable", variable)
            except Exception:
                pass
        label = {
            "temperature": "Temperature",
            "dissolved_oxygen": "Dissolved O2",
            "tan": "TAN",
            "co2": "CO2",
            "ph": "pH",
            "alkalinity": "Alkalinity",
            "salinity": "Salinity",
            "turbidity": "Turbidity",
            "nh3": "NH3",
        }.get(str(variable or ""), str(variable or "--"))
        self._wq_view_label.text = f"Current: {label}"


    def _create_control_window(self):
        if not bool(_get_runtime_config("ENABLE_WATER_QUALITY", _get_runtime_config("ENABLE_WATER_QUALITY_SIM", False))):
            return
        if self._aquacast_main is None:
            return
        if self._control_window is not None:
            return

        self._control_window = ui.Window("Aquacast Tank Controls", width=500, height=560, visible=True)
        self._refresh_control_tank_data()
        self._build_control_window_contents()
        self._register_control_window_menu()

    def _register_control_window_menu(self):
        if self._control_menu_items:
            return
        self._control_menu_items = [
            MenuItemDescription(
                name="Aquacast/Tank Controls",
                onclick_fn=self._show_control_window,
            )
        ]
        omni.kit.menu.utils.add_menu_items(self._control_menu_items, name="Window")

    def _show_control_window(self):
        if self._control_window is None:
            self._create_control_window()
            return
        self._control_window.visible = True
        self._refresh_control_tank_data(rebuild=True)

    def _refresh_control_tank_data(self, rebuild=False):
        previous = self._selected_control_tank()
        self._control_tanks, self._control_tank_labels = self._sensor_tank_window_data()
        if previous in self._control_tanks:
            self._control_tank_index = self._control_tanks.index(previous)
        else:
            self._control_tank_index = self._clamp_index(self._control_tank_index, self._control_tanks)
        if rebuild and self._control_window is not None:
            self._schedule_control_rebuild()

    def _schedule_control_rebuild(self):
        if self._control_rebuild_requested:
            return
        self._control_rebuild_requested = True
        asyncio.ensure_future(self._rebuild_control_window_next_update())

    async def _rebuild_control_window_next_update(self):
        try:
            await omni.kit.app.get_app().next_update_async()
            if self._control_window is not None:
                previous_rebuild = self._control_rebuild_requested
                self._control_rebuild_requested = False
                self._refresh_control_tank_data(rebuild=False)
                self._build_control_window_contents()
                self._control_rebuild_requested = False if previous_rebuild else self._control_rebuild_requested
        finally:
            self._control_rebuild_requested = False

    def _build_control_window_contents(self):
        if self._control_window is None:
            return
        self._control_fields = {}
        with self._control_window.frame:
            with ui.ScrollingFrame():
                with ui.VStack(spacing=7):
                    ui.Label("Tank Controls", height=22)
                    with ui.HStack(height=26, spacing=6):
                        ui.Label("Tank:", width=85)
                        self._control_tank_index = self._clamp_index(self._control_tank_index, self._control_tank_labels)
                        self._control_tank_combo = ui.ComboBox(self._control_tank_index, *(self._control_tank_labels or ["(no tanks)"]))
                        ui.Button("Refresh", width=80, clicked_fn=lambda: self._refresh_control_tank_data(rebuild=True))

                    self._control_status_label = ui.Label("Ready", height=38, word_wrap=True)

                    ui.Label("Thermal", height=20)
                    self._control_float_row("temperature_c", "Set Temp C", 14.0, "Apply", lambda: self._control_action("set_temperature", temperature_c=self._control_float("temperature_c", 14.0)))
                    self._control_float_row("heater_w", "Heater W", 0.0, "Apply", lambda: self._control_action("set_heater", power_w=self._control_float("heater_w", 0.0)))
                    self._control_float_row("inlet_temp_c", "Inlet Temp C", 12.0, "Apply", lambda: self._control_action("set_inlet_temperature", temperature_c=self._control_float("inlet_temp_c", 12.0)))

                    ui.Label("Feeding / Stock", height=20)
                    self._control_float_row("feed_kg", "Feed kg", 1.0, "Pulse", lambda: self._control_action("feed", mass_kg=self._control_float("feed_kg", 1.0)))
                    with ui.HStack(height=26, spacing=6):
                        ui.Label("Stock", width=85)
                        self._control_fields["fish_count"] = ui.FloatField(width=95)
                        self._set_control_float("fish_count", 200.0)
                        ui.Label("fish", width=32)
                        self._control_fields["fish_weight_kg"] = ui.FloatField(width=95)
                        self._set_control_float("fish_weight_kg", 1.0)
                        ui.Label("kg", width=22)
                        ui.Button("Apply", width=70, clicked_fn=lambda: self._control_action(
                            "set_stock",
                            fish_count=self._control_float("fish_count", 200.0),
                            fish_weight_kg=self._control_float("fish_weight_kg", 1.0),
                        ))

                    ui.Label("Water Exchange / Inlet", height=20)
                    self._control_float_row("flow_lph", "Flow L/h", 2000.0, "Apply", lambda: self._control_action("set_water_exchange", q_lph=self._control_float("flow_lph", 2000.0)))
                    with ui.HStack(height=28, spacing=6):
                        ui.Label("Inflow", width=85)
                        ui.Button("ON", clicked_fn=lambda: self._control_action("set_inflow", enabled=True))
                        ui.Button("OFF", clicked_fn=lambda: self._control_action("set_inflow", enabled=False))
                    self._control_float_row("salinity_ppt", "Inlet Sal ppt", 0.2, "Apply", lambda: self._control_action("set_inlet_salinity", salinity_ppt=self._control_float("salinity_ppt", 0.2)))
                    self._control_float_row("turbidity_ntu", "Inlet Turb NTU", 1.0, "Apply", lambda: self._control_action("set_inlet_turbidity", turbidity_ntu=self._control_float("turbidity_ntu", 1.0)))
                    self._control_float_row("inlet_do", "Inlet DO", 9.0, "Apply", lambda: self._control_action("set_inlet_do", dissolved_oxygen_mg_l=self._control_float("inlet_do", 9.0)))
                    self._control_float_row("inlet_alk", "Inlet Alk", 120.0, "Apply", lambda: self._control_action("set_inlet_alkalinity", alkalinity_mg_l_as_caco3=self._control_float("inlet_alk", 120.0)))

                    ui.Label("Filtration / Emergency", height=20)
                    with ui.HStack(height=28, spacing=6):
                        ui.Label("Biofilter", width=85)
                        ui.Button("ON", clicked_fn=lambda: self._control_action("set_biofilter", enabled=True))
                        ui.Button("OFF", clicked_fn=lambda: self._control_action("set_biofilter", enabled=False))
                        ui.Label("Mech", width=40)
                        ui.Button("ON", clicked_fn=lambda: self._control_action("set_mechanical_filter", enabled=True, settle_h=0.35))
                        ui.Button("OFF", clicked_fn=lambda: self._control_action("set_mechanical_filter", enabled=False))
                    with ui.HStack(height=28, spacing=6):
                        ui.Button("O2 +1", clicked_fn=lambda: self._control_action("oxygen_boost", mg_l=1.0))
                        ui.Button("CO2 +2", clicked_fn=lambda: self._control_action("co2_pulse", mg_l=2.0))
                        ui.Button("Salt +0.3", clicked_fn=lambda: self._control_action("dose_salt", ppt=0.3))
                        ui.Button("Turb +5", clicked_fn=lambda: self._control_action("add_turbidity", ntu=5.0))

                    ui.Label("Scenarios", height=20)
                    with ui.HStack(height=28, spacing=6):
                        ui.Button("Baseline", clicked_fn=lambda: self._control_action("load_scenario", name="baseline"))
                        ui.Button("Overfeed", clicked_fn=lambda: self._control_action("load_scenario", name="overfeed"))
                        ui.Button("Pump Off", clicked_fn=lambda: self._control_action("load_scenario", name="pump_off"))
                        ui.Button("Biofilter Off", clicked_fn=lambda: self._control_action("load_scenario", name="biofilter_off"))
                    self._sync_control_fields_from_current_snapshot()

    def _control_float_row(self, key, label, default, button_label, clicked_fn):
        with ui.HStack(height=26, spacing=6):
            ui.Label(label, width=120)
            self._control_fields[key] = ui.FloatField(width=120)
            self._set_control_float(key, default)
            ui.Button(button_label, width=80, clicked_fn=clicked_fn)

    def _set_control_float(self, key, value):
        field = self._control_fields.get(key)
        if field is None:
            return
        try:
            field.model.set_value(float(value))
        except Exception:
            pass

    def _control_float(self, key, default=0.0):
        field = self._control_fields.get(key)
        if field is None:
            return float(default)
        try:
            return float(field.model.as_float)
        except Exception:
            pass
        try:
            return float(field.model.get_value_as_float())
        except Exception:
            return float(default)

    def _selected_control_tank(self):
        if not self._control_tanks:
            return None
        index = self._clamp_index(self._sensor_combo_index(self._control_tank_combo, self._control_tank_index), self._control_tanks)
        self._control_tank_index = index
        return self._control_tanks[index]

    def _control_action(self, action, **params):
        if self._aquacast_main is None or not hasattr(self._aquacast_main, "execute_water_quality_action"):
            self._set_control_status("action API unavailable")
            return
        tank = self._selected_control_tank()
        try:
            result = self._aquacast_main.execute_water_quality_action(action, tank_path=tank, **params)
        except Exception as exc:
            self._set_control_status(f"{action} failed: {exc}")
            return
        status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
        if status == "ok":
            self._sync_control_fields_from_result(action, result)
            detail = f"{action} applied"
            if tank:
                detail += f" -> {self._sensor_tank_label(tank)}"
            if isinstance(result, dict):
                parts = []
                if "temperature_c" in result:
                    parts.append(f"T={float(result.get('temperature_c', 0.0)):.2f}C")
                if "dissolved_oxygen_mg_l" in result:
                    parts.append(f"DO={float(result.get('dissolved_oxygen_mg_l', 0.0)):.2f}")
                if "ph" in result:
                    parts.append(f"pH={float(result.get('ph', 0.0)):.2f}")
                if parts:
                    detail += " | " + ", ".join(parts)
            self._set_control_status(detail)
            try:
                self._on_sensor_ui_update(None)
            except Exception:
                pass
            try:
                self._on_actuator_ui_update(None, force=True)
            except Exception:
                pass
        else:
            error = result.get("error", status) if isinstance(result, dict) else status
            self._set_control_status(f"{action} failed: {error}")

    def _sync_control_fields_from_current_snapshot(self):
        if self._aquacast_main is None or not hasattr(self._aquacast_main, "get_quality_snapshot"):
            return
        tank = self._selected_control_tank()
        try:
            snapshot = self._aquacast_main.get_quality_snapshot(tank_path=tank)
        except Exception as exc:
            carb.log_warn(f"[test-Aquacast] Control snapshot sync failed: {exc}")
            return
        if isinstance(snapshot, dict) and snapshot.get("status") == "ok":
            self._sync_control_fields_from_snapshot(snapshot)

    def _sync_control_fields_from_result(self, action, result):
        if not isinstance(result, dict):
            return
        values = result.get("control_values")
        if isinstance(values, dict) and values:
            self._sync_control_fields_from_snapshot(values)
            return
        self._sync_control_fields_from_snapshot(result)
        if action == "set_inlet_temperature" and "temperature_c" in result:
            self._set_control_float("inlet_temp_c", result.get("temperature_c"))

    def _sync_control_fields_from_snapshot(self, snapshot):
        if not isinstance(snapshot, dict):
            return
        mapping = {
            "temperature_c": "temperature_c",
            "heater_w": "heater_power_w",
            "inlet_temp_c": "inlet_temp_c",
            "fish_count": "fish_count",
            "fish_weight_kg": "fish_weight_kg",
            "flow_lph": "flow_lph",
            "salinity_ppt": "salinity_in_ppt",
            "turbidity_ntu": "turbidity_in_ntu",
            "inlet_do": "do_in",
            "inlet_alk": "alk_in",
        }
        for field_key, snapshot_key in mapping.items():
            if snapshot_key in snapshot:
                self._set_control_float(field_key, snapshot.get(snapshot_key))

    def _set_control_status(self, text):
        if self._control_status_label is not None:
            self._control_status_label.text = str(text)


    def _create_actuator_window(self):
        if not bool(_get_runtime_config("ENABLE_WATER_QUALITY", _get_runtime_config("ENABLE_WATER_QUALITY_SIM", False))):
            return
        if self._aquacast_main is None:
            return
        if self._actuator_window is not None:
            return

        self._actuator_window = ui.Window("Aquacast Actuator Overview", width=560, height=360, visible=True)
        self._refresh_actuator_tank_data()
        self._build_actuator_window_contents()
        self._register_actuator_window_menu()
        self._actuator_update_sub = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_actuator_ui_update,
            name="aquacast_actuator_overview_ui",
        )
        self._on_actuator_ui_update(None, force=True)

    def _register_actuator_window_menu(self):
        if self._actuator_menu_items:
            return
        self._actuator_menu_items = [
            MenuItemDescription(
                name="Aquacast/Actuator Overview",
                onclick_fn=self._show_actuator_window,
            )
        ]
        omni.kit.menu.utils.add_menu_items(self._actuator_menu_items, name="Window")

    def _show_actuator_window(self):
        if self._actuator_window is None:
            self._create_actuator_window()
            return
        self._actuator_window.visible = True
        self._on_actuator_ui_update(None, force=True)

    def _refresh_actuator_tank_data(self):
        self._actuator_tanks, self._actuator_tank_labels = self._sensor_tank_window_data()

    def _build_actuator_window_contents(self):
        if self._actuator_window is None:
            return
        self._actuator_tank_dot_sets = {}
        with self._actuator_window.frame:
            with ui.ScrollingFrame():
                with ui.VStack(spacing=8):
                    ui.Label("Actuator Overview", height=22)
                    self._actuator_status_label = ui.Label("Waiting for tank state", height=22)
                    if not self._actuator_tanks:
                        ui.Label("No tanks found", height=24)
                        return
                    for tank_path, tank_label in zip(self._actuator_tanks, self._actuator_tank_labels):
                        with ui.VStack(height=72, spacing=4):
                            ui.Label(str(tank_label), height=18)
                            with ui.HStack(height=42, spacing=10):
                                tank_dots = {}
                                for key, label in self._WQ_ACTUATOR_ROWS:
                                    with ui.VStack(width=88, spacing=2):
                                        with ui.HStack(height=16):
                                            ui.Label("", width=34)
                                            dots = self._build_status_indicator(size=14)
                                        ui.Label(label, height=18, alignment=ui.Alignment.CENTER)
                                    tank_dots[key] = dots
                                self._actuator_tank_dot_sets[tank_path] = tank_dots

    def _on_actuator_ui_update(self, _event, force=False):
        if self._actuator_window is None or self._aquacast_main is None:
            return
        now = time.monotonic()
        interval = float(_get_runtime_config("TEMP_SENSOR_UPDATE_INTERVAL_SECONDS", 0.5) or 0.5)
        if not force and now - self._actuator_last_update < max(0.05, interval):
            return
        self._actuator_last_update = now

        previous_tanks = list(self._actuator_tanks)
        self._refresh_actuator_tank_data()
        if previous_tanks != self._actuator_tanks or not self._actuator_tank_dot_sets:
            self._build_actuator_window_contents()

        failures = 0
        for tank_path in self._actuator_tanks:
            try:
                snapshot = self._aquacast_main.get_quality_snapshot(tank_path=tank_path)
            except Exception:
                snapshot = {}
            if snapshot.get("status") != "ok":
                failures += 1
                snapshot = {}
            for key, _label in self._WQ_ACTUATOR_ROWS:
                self._set_status_indicator(
                    self._actuator_tank_dot_sets.get(tank_path, {}).get(key, {}),
                    snapshot.get(key) if key in snapshot else None,
                )
        if self._actuator_status_label is not None:
            if not self._actuator_tanks:
                self._actuator_status_label.text = "No tanks found"
            elif failures:
                self._actuator_status_label.text = f"{failures} tank state read failed"
            else:
                self._actuator_status_label.text = f"{len(self._actuator_tanks)} tank(s)"


    def _default_metric_thresholds(self):
        bands = _get_wq_bands_module()
        configured = _get_runtime_config("WQ_METRIC_DASHBOARD_THRESHOLDS", {}) or {}
        return bands.normalize_bands(configured)

    def _normalize_metric_thresholds(self, thresholds):
        bands = _get_wq_bands_module()
        values = self._default_metric_thresholds()
        raw = thresholds.get("thresholds") if isinstance(thresholds, dict) and "thresholds" in thresholds else thresholds
        raw = raw if isinstance(raw, dict) else {}
        for key, value in raw.items():
            if key in values:
                values[key] = value
        return bands.normalize_bands(values)

    def _dashboard_metric_specs(self):
        configured = _get_runtime_config(
            "WQ_METRICS_DASHBOARD_METRICS",
            [spec["key"] for spec in self._METRIC_DASHBOARD_SPECS],
        )
        configured_keys = {str(key) for key in configured} if isinstance(configured, (list, tuple, set)) else set()
        specs = [spec for spec in self._METRIC_DASHBOARD_SPECS if not configured_keys or spec["key"] in configured_keys]
        return specs or list(self._METRIC_DASHBOARD_SPECS)

    def _load_metric_thresholds(self):
        if self._aquacast_main and hasattr(self._aquacast_main, "get_water_quality_metric_thresholds"):
            try:
                result = self._aquacast_main.get_water_quality_metric_thresholds()
                if isinstance(result, dict) and result.get("status") == "ok":
                    return self._normalize_metric_thresholds(result.get("thresholds", {}))
            except Exception as exc:
                carb.log_warn(f"[test-Aquacast] Metric threshold load failed: {exc}")
        return self._default_metric_thresholds()

    def _create_metrics_dashboard_window(self):
        quality_enabled = bool(_get_runtime_config("ENABLE_WATER_QUALITY", _get_runtime_config("ENABLE_WATER_QUALITY_SIM", False)))
        if not quality_enabled or not bool(_get_runtime_config("ENABLE_WQ_METRICS_DASHBOARD", True)):
            return
        if self._aquacast_main is None:
            return
        if self._metrics_window is not None:
            return

        self._metrics_window = ui.Window("Aquacast Metrics Dashboard", width=760, height=780, visible=True)
        self._metrics_thresholds = self._load_metric_thresholds()
        self._refresh_metrics_tank_data()
        self._build_metrics_dashboard_contents()
        self._register_metrics_dashboard_menu()
        self._metrics_update_sub = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_metrics_dashboard_update,
            name="aquacast_metrics_dashboard_ui",
        )
        self._on_metrics_dashboard_update(None, force=True)

    def _register_metrics_dashboard_menu(self):
        if self._metrics_menu_items:
            return
        self._metrics_menu_items = [
            MenuItemDescription(
                name="Aquacast/Metrics Dashboard",
                onclick_fn=self._show_metrics_dashboard_window,
            )
        ]
        omni.kit.menu.utils.add_menu_items(self._metrics_menu_items, name="Window")

    def _show_metrics_dashboard_window(self):
        if self._metrics_window is None:
            self._create_metrics_dashboard_window()
            return
        self._metrics_window.visible = True
        self._on_metrics_dashboard_update(None, force=True)

    def _refresh_metrics_tank_data(self):
        self._metrics_tanks, self._metrics_tank_labels = self._sensor_tank_window_data()
        self._metrics_tank_index = self._clamp_index(self._metrics_tank_index, self._metrics_tanks)

    def _selected_metrics_tank(self):
        if not self._metrics_tanks:
            return None
        index = self._clamp_index(self._sensor_combo_index(self._metrics_tank_combo, self._metrics_tank_index), self._metrics_tanks)
        self._metrics_tank_index = index
        return self._metrics_tanks[index]

    def _build_metrics_dashboard_contents(self):
        if self._metrics_window is None:
            return
        self._metrics_chart_frames = {}
        self._metrics_current_labels = {}
        self._metrics_range_labels = {}
        self._metrics_state_frames = {}
        self._metrics_threshold_fields = {}
        with self._metrics_window.frame:
            with ui.ScrollingFrame():
                with ui.VStack(spacing=8):
                    ui.Label("Metrics Dashboard", height=24)
                    with ui.HStack(height=28, spacing=6):
                        ui.Label("Tank:", width=58)
                        self._metrics_tank_index = self._clamp_index(self._metrics_tank_index, self._metrics_tank_labels)
                        self._metrics_tank_combo = ui.ComboBox(self._metrics_tank_index, *(self._metrics_tank_labels or ["(no tanks)"]))
                        ui.Button("Refresh", width=80, clicked_fn=lambda: self._refresh_metrics_dashboard(rebuild=True))
                        ui.Button("Save Bands", width=110, clicked_fn=self._save_metric_thresholds)
                    self._metrics_status_label = ui.Label("Waiting for water-quality data", height=24, word_wrap=True)
                    for spec in self._dashboard_metric_specs():
                        self._build_metric_dashboard_panel(spec)

    def _build_metric_dashboard_panel(self, spec):
        key = spec["key"]
        threshold = self._metrics_thresholds.get(key, {})
        with ui.ZStack(height=178):
            ui.Rectangle(style={"background_color": 0xFF0B111A, "border_color": self._METRIC_DASHBOARD_GRID_COLOR, "border_width": 1})
            with ui.VStack(spacing=4):
                with ui.HStack(height=24, spacing=6):
                    ui.Label(str(spec["label"]), width=210)
                    self._metrics_current_labels[key] = ui.Label("--", width=140)
                    self._metrics_range_labels[key] = ui.Label("range --", width=150)
                    state_frame = ui.Frame(width=70, height=20)
                    self._metrics_state_frames[key] = state_frame
                ui.Label(self._metric_band_legend(threshold), height=34, word_wrap=True)
                chart_frame = ui.Frame(height=104)
                self._metrics_chart_frames[key] = chart_frame

    def _refresh_metrics_dashboard(self, rebuild=False):
        self._refresh_metrics_tank_data()
        if rebuild:
            self._metrics_thresholds = self._load_metric_thresholds()
            self._build_metrics_dashboard_contents()
        self._on_metrics_dashboard_update(None, force=True)

    def _save_metric_thresholds(self):
        values = self._normalize_metric_thresholds(self._metrics_thresholds)
        if self._aquacast_main and hasattr(self._aquacast_main, "set_water_quality_metric_thresholds"):
            try:
                result = self._aquacast_main.set_water_quality_metric_thresholds(values)
            except Exception as exc:
                result = {"status": "error", "error": str(exc)}
        else:
            result = {"status": "error", "error": "threshold API unavailable"}
        if isinstance(result, dict) and result.get("status") == "ok":
            self._metrics_thresholds = self._normalize_metric_thresholds(result.get("thresholds", values))
            self._set_metrics_status("Bands saved")
            self._on_metrics_dashboard_update(None, force=True)
        else:
            error = result.get("error", result.get("status", "unknown")) if isinstance(result, dict) else "unknown"
            self._set_metrics_status(f"Threshold save failed: {error}")

    def _on_metrics_dashboard_update(self, _event, force=False):
        if self._metrics_window is None or self._aquacast_main is None:
            return
        now = time.monotonic()
        interval = float(_get_runtime_config("WQ_METRICS_DASHBOARD_UPDATE_INTERVAL_SECONDS", 0.5) or 0.5)
        if not force and now - self._metrics_last_update < max(0.05, interval):
            return
        self._metrics_last_update = now

        previous_tanks = list(self._metrics_tanks)
        previous_labels = list(self._metrics_tank_labels)
        self._refresh_metrics_tank_data()
        if previous_tanks != self._metrics_tanks or previous_labels != self._metrics_tank_labels:
            self._build_metrics_dashboard_contents()

        tank_path = self._selected_metrics_tank()
        if not tank_path:
            self._set_metrics_status("No tanks found")
            return
        try:
            snapshot = self._aquacast_main.get_quality_snapshot(tank_path=tank_path)
        except Exception as exc:
            self._set_metrics_status(f"snapshot failed: {exc}")
            return
        if not isinstance(snapshot, dict) or snapshot.get("status") != "ok":
            status = snapshot.get("status", "unknown") if isinstance(snapshot, dict) else "unknown"
            self._set_metrics_status(f"snapshot unavailable: {status}")
            return

        for spec in self._dashboard_metric_specs():
            key = spec["key"]
            try:
                value = float(snapshot.get(key, 0.0))
            except (TypeError, ValueError):
                value = 0.0
            history = self._append_metric_history(tank_path, key, value, interval)
            self._update_metric_panel(spec, value, history)
        label = self._sensor_tank_label(tank_path)
        self._set_metrics_status(f"Live trend: {label} | {len(self._dashboard_metric_specs())} metrics")

    def _append_metric_history(self, tank_path, key, value, interval):
        history_key = (str(tank_path or ""), str(key))
        history = self._metrics_history.setdefault(history_key, [])
        history.append(float(value))
        history_seconds = float(_get_runtime_config("WQ_METRICS_DASHBOARD_HISTORY_SECONDS", 180.0) or 180.0)
        limit = max(8, int(history_seconds / max(0.05, float(interval))))
        if len(history) > limit:
            del history[:len(history) - limit]
        return history

    def _update_metric_panel(self, spec, value, history):
        key = spec["key"]
        threshold = self._metrics_thresholds.get(key, {})
        label = self._metrics_current_labels.get(key)
        if label is not None:
            label.text = f"{spec['short']} {self._format_metric_value(spec, value)}"
        state, color = self._metric_state(spec, value, threshold)
        self._update_metric_state_frame(key, state, color)
        y_min, y_max = self._metric_chart_range(spec, history, threshold)
        range_label = self._metrics_range_labels.get(key)
        if range_label is not None:
            range_label.text = f"range {y_min:.2f} - {y_max:.2f}"
        self._draw_metric_chart(spec, history, threshold, y_min, y_max, color)

    def _metric_band_legend(self, threshold):
        bands = _get_wq_bands_module()
        parts = []
        labels = (("healthy", "Healthy"), ("warn", "Warn"), ("critical", "Critical"))
        for state, label in labels:
            conditions = threshold.get(state, []) if isinstance(threshold, dict) else []
            if not conditions:
                continue
            text = " or ".join(bands.condition_label(condition) for condition in conditions)
            parts.append(f"{label}: {text}")
        return " | ".join(parts) if parts else "Bands unavailable"

    def _metric_threshold_value(self, spec):
        key = spec["key"]
        field = self._metrics_threshold_fields.get(key)
        if field is not None:
            for attr in ("as_float", "get_value_as_float"):
                try:
                    model = field.model
                    value = getattr(model, attr)
                    return float(value() if callable(value) else value)
                except Exception:
                    pass
        try:
            return float(self._metrics_thresholds.get(key, {}).get("value", spec["default_threshold"]))
        except (TypeError, ValueError):
            return float(spec["default_threshold"])

    def _metric_state(self, spec, value, threshold):
        bands = _get_wq_bands_module()
        result = bands.metric_state(spec["key"], value, {spec["key"]: threshold})
        state = str(result.get("state") or "unknown")
        label = "OK" if state == "healthy" else state.upper()
        return label, self._METRIC_DASHBOARD_STATE_COLORS.get(state, self._METRIC_DASHBOARD_STATE_COLORS["unknown"])

    def _format_metric_value(self, spec, value):
        unit = str(spec.get("unit") or "")
        if spec["key"] == "ph":
            return f"{value:.2f}"
        return f"{value:.2f} {unit}".strip()

    def _metric_chart_range(self, spec, history, threshold):
        values = []
        for value in history or []:
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                pass
        bands = _get_wq_bands_module()
        values.extend(bands.condition_values(threshold))
        if not values:
            values = [0.0]
        y_min = min(values)
        y_max = max(values)
        min_span = max(1e-6, float(spec.get("min_span", 0.1)))
        if y_max - y_min < min_span:
            center = (y_min + y_max) * 0.5
            y_min = center - min_span * 0.5
            y_max = center + min_span * 0.5
        span = max(min_span, y_max - y_min)
        pad = max(min_span * 0.25, span * 0.18)
        return y_min - pad, y_max + pad

    def _update_metric_state_frame(self, key, state, color):
        frame = self._metrics_state_frames.get(key)
        if frame is None:
            return
        with frame:
            with ui.ZStack(height=20):
                ui.Rectangle(style={"background_color": color, "border_radius": 4})
                ui.Label(str(state), alignment=ui.Alignment.CENTER)

    def _draw_metric_chart(self, spec, history, threshold, y_min, y_max, state_color):
        frame = self._metrics_chart_frames.get(spec["key"])
        if frame is None:
            return
        values = list(history or [])
        if len(values) < 2:
            values = values + values[:1]
        if len(values) < 2:
            values = [0.0, 0.0]
        with frame:
            with ui.ZStack(height=100):
                ui.Rectangle(style={"background_color": self._METRIC_DASHBOARD_BACKGROUND_COLOR, "border_color": self._METRIC_DASHBOARD_GRID_COLOR, "border_width": 1})
                self._draw_metric_band_background(threshold, y_min, y_max, 98)
                self._plot_metric_line(values, y_min, y_max, spec.get("color", state_color), 98)

    def _draw_metric_band_background(self, threshold, y_min, y_max, height):
        if not isinstance(threshold, dict):
            return
        for state in ("healthy", "warn", "critical"):
            color = self._METRIC_DASHBOARD_BAND_COLORS.get(state)
            if color is None:
                continue
            for condition in threshold.get(state, []) or []:
                interval = self._condition_interval(condition, y_min, y_max)
                if interval is not None:
                    self._draw_metric_band_region(interval[0], interval[1], y_min, y_max, height, color)

    def _condition_interval(self, condition, y_min, y_max):
        if not isinstance(condition, dict):
            return None
        low = float(y_min)
        high = float(y_max)
        if "gt" in condition:
            low = max(low, float(condition["gt"]))
        if "gte" in condition:
            low = max(low, float(condition["gte"]))
        if "lt" in condition:
            high = min(high, float(condition["lt"]))
        if "lte" in condition:
            high = min(high, float(condition["lte"]))
        if high <= low:
            return None
        return low, high

    def _draw_metric_band_region(self, low, high, y_min, y_max, height, color):
        span = max(1e-9, float(y_max) - float(y_min))
        low_norm = max(0.0, min(1.0, (float(low) - float(y_min)) / span))
        high_norm = max(0.0, min(1.0, (float(high) - float(y_min)) / span))
        if high_norm <= low_norm:
            return
        top_height = max(0, int(round((1.0 - high_norm) * max(1, int(height)))))
        band_height = max(1, int(round((high_norm - low_norm) * max(1, int(height)))))
        bottom_height = max(0, int(height) - top_height - band_height)
        with ui.VStack(height=height):
            if top_height > 0:
                ui.Spacer(height=top_height)
            ui.Rectangle(height=band_height, style={"background_color": color})
            if bottom_height > 0:
                ui.Spacer(height=bottom_height)

    def _draw_metric_threshold_line(self, threshold, y_min, y_max, height):
        span = max(1e-9, float(y_max) - float(y_min))
        normalized = (float(threshold) - float(y_min)) / span
        normalized = max(0.0, min(1.0, normalized))
        top_height = max(0, int(round((1.0 - normalized) * max(1, int(height) - 2))))
        bottom_height = max(0, int(height) - top_height - 2)
        with ui.VStack(height=height):
            if top_height > 0:
                ui.Spacer(height=top_height)
            ui.Rectangle(height=2, style={"background_color": self._METRIC_DASHBOARD_THRESHOLD_COLOR})
            if bottom_height > 0:
                ui.Spacer(height=bottom_height)

    def _plot_metric_line(self, values, y_min, y_max, color, height):
        plot_cls = getattr(ui, "Plot", None)
        type_container = getattr(ui, "Type", None)
        plot_type = getattr(type_container, "LINE", None) if type_container is not None else None
        if plot_type is None:
            plot_type = getattr(getattr(ui, "PlotType", None), "LINE", None)
        if plot_cls is None or plot_type is None:
            ui.Label("Plot widget unavailable", height=height)
            return
        safe_values = [float(value) for value in values]
        style = {"color": color, "line_width": 2}
        arg_sets = (
            (plot_type, float(y_min), float(y_max), *safe_values),
            (plot_type, float(y_min), float(y_max), safe_values),
            (plot_type, float(y_min), float(y_max), len(safe_values), safe_values),
            (plot_type, *safe_values),
        )
        kwarg_sets = (
            {"height": height, "style": style},
            {"height": height},
            {},
        )
        for kwargs in kwarg_sets:
            for args in arg_sets:
                try:
                    plot_cls(*args, **kwargs)
                    return
                except Exception:
                    pass
        ui.Label("Plot failed", height=height)

    def _set_metrics_status(self, text):
        if self._metrics_status_label is not None:
            self._metrics_status_label.text = str(text)

    def _teardown_metrics_dashboard_window(self):
        self._metrics_update_sub = None
        self._metrics_window = None
        self._metrics_tank_combo = None
        self._metrics_status_label = None
        self._metrics_chart_frames = {}
        self._metrics_current_labels = {}
        self._metrics_range_labels = {}
        self._metrics_state_frames = {}
        self._metrics_threshold_fields = {}
        if self._metrics_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._metrics_menu_items, "Window")
            self._metrics_menu_items = []


    def _create_local_llm_panel(self):
        if not bool(_get_runtime_config("ENABLE_LOCAL_LLM_PANEL", True)):
            return
        if self._local_llm_panel is None:
            self._local_llm_panel = LocalLLMPanel(
                aquacast_main=self._aquacast_main,
                config_getter=_get_runtime_config,
            )
        self._register_local_llm_panel_menu()
        if bool(_get_runtime_config("LOCAL_LLM_PANEL_OPEN_ON_STARTUP", False)):
            self._local_llm_panel.show()

    def _register_local_llm_panel_menu(self):
        if self._local_llm_menu_items:
            return
        self._local_llm_menu_items = [
            MenuItemDescription(
                name="Aquacast/Local LLM Panel",
                onclick_fn=self._show_local_llm_panel,
            )
        ]
        omni.kit.menu.utils.add_menu_items(self._local_llm_menu_items, name="Window")

    def _show_local_llm_panel(self):
        if self._local_llm_panel is None:
            self._create_local_llm_panel()
        if self._local_llm_panel is not None:
            self._local_llm_panel.show()

    def _teardown_local_llm_panel(self):
        if self._local_llm_panel is not None:
            self._local_llm_panel.shutdown()
            self._local_llm_panel = None
        if self._local_llm_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._local_llm_menu_items, "Window")
            self._local_llm_menu_items = []


    def _create_fish_window(self):
        if not bool(_get_runtime_config("ENABLE_FISH_MANAGEMENT_UI", True)):
            return
        if self._aquacast_main is None:
            return
        if self._fish_window is not None:
            return

        self._fish_window = ui.Window("Aquacast Fish Management", width=430, height=260, visible=True)
        self._fish_tanks = []
        self._fish_species = []
        self._fish_tank_labels = []
        self._fish_species_labels = []
        self._build_fish_window_contents()
        self._register_fish_window_menu()
        self._fish_update_sub = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_fish_ui_update,
            name="aquacast_fish_management_ui",
        )
        self._on_fish_ui_update(None, force=True)

    def _register_fish_window_menu(self):
        if self._fish_menu_items:
            return
        self._fish_menu_items = [
            MenuItemDescription(
                name="Aquacast/Fish Management",
                onclick_fn=self._show_fish_window,
            )
        ]
        omni.kit.menu.utils.add_menu_items(self._fish_menu_items, name="Window")

    def _show_fish_window(self):
        if self._fish_window is None:
            self._create_fish_window()
            return
        self._fish_window.visible = True
        self._on_fish_ui_update(None, force=True)

    def _fish_combo_index(self, combo):
        if combo is None:
            return 0
        try:
            return int(combo.model.get_item_value_model().as_int)
        except Exception:
            pass
        try:
            return int(combo.model.get_item_value_model().get_value_as_int())
        except Exception:
            return 0

    def _fish_qty(self):
        if self._fish_qty_field is None:
            return 0
        try:
            return max(0, int(self._fish_qty_field.model.as_int))
        except Exception:
            pass
        try:
            return max(0, int(self._fish_qty_field.model.get_value_as_int()))
        except Exception:
            return 0

    def _selected_fish_tank(self):
        if not self._fish_tanks:
            return None
        index = max(0, min(self._fish_combo_index(self._fish_tank_combo), len(self._fish_tanks) - 1))
        return self._fish_tanks[index]

    def _selected_fish_species(self):
        if not self._fish_species:
            return None
        index = max(0, min(self._fish_combo_index(self._fish_species_combo), len(self._fish_species) - 1))
        return self._fish_species[index]

    def _fish_window_data(self):
        tanks = []
        species = []
        if self._aquacast_main is not None:
            try:
                tanks = list(self._aquacast_main.list_fish_tanks())
            except Exception as exc:
                carb.log_warn(f"[test-Aquacast] Fish UI tank refresh failed: {exc}")
            try:
                species = list(self._aquacast_main.get_fish_species())
            except Exception as exc:
                carb.log_warn(f"[test-Aquacast] Fish UI species refresh failed: {exc}")
        return tanks, species

    def _build_fish_window_contents(self):
        if self._fish_window is None:
            return
        tanks, species = self._fish_window_data()
        self._fish_tanks = tanks
        self._fish_species = species
        self._fish_tank_labels = [path.rsplit("/", 1)[-2] if path.endswith("/Water") and "/" in path.rstrip("/") else path for path in tanks]
        self._fish_species_labels = [str(item.get("label") or item.get("id") or "species") for item in species]
        if not self._fish_tank_labels:
            self._fish_tank_labels = ["(no tank)"]
        if not self._fish_species_labels:
            self._fish_species_labels = ["(no species)"]

        with self._fish_window.frame:
            with ui.ScrollingFrame():
                with ui.VStack(spacing=6):
                    ui.Label("Fish Management", height=22)
                    with ui.HStack(height=26, spacing=6):
                        ui.Label("Tank:", width=70)
                        self._fish_tank_combo = ui.ComboBox(0, *self._fish_tank_labels)
                    with ui.HStack(height=26, spacing=6):
                        ui.Label("Species:", width=70)
                        self._fish_species_combo = ui.ComboBox(0, *self._fish_species_labels)
                    self._fish_count_label = ui.Label("Total 0/30 · Alive 0 · Dead 0", height=24)
                    with ui.HStack(height=26, spacing=6):
                        ui.Label("Qty:", width=70)
                        self._fish_qty_field = ui.IntField()
                        try:
                            self._fish_qty_field.model.set_value(1)
                        except Exception:
                            pass
                    with ui.HStack(height=30, spacing=6):
                        self._fish_add_button = ui.Button("ADD", clicked_fn=self._on_fish_add_clicked)
                        self._fish_delete_button = ui.Button("DELETE", clicked_fn=self._on_fish_delete_clicked)
                    self._fish_clear_button = ui.Button("Clear All", height=30, clicked_fn=self._on_fish_clear_clicked)
                    self._fish_status_label = ui.Label("스테이지/탱크 대기", height=42, word_wrap=True)

    def _set_fish_button_enabled(self, button, enabled):
        if button is None:
            return
        try:
            button.enabled = bool(enabled)
        except Exception:
            pass

    def _set_fish_status(self, text):
        if self._fish_status_label is not None:
            self._fish_status_label.text = str(text)

    def _fish_stock_status_suffix(self, result):
        if not isinstance(result, dict):
            return ""
        stock = result.get("stock") or {}
        status = str(result.get("stock_sync_status") or "")
        if not stock:
            return f" | WQ {status}" if status and status != "ok" else ""
        try:
            fish_count = float(stock.get("fish_count", 0.0))
            biomass_kg = float(stock.get("biomass_kg", 0.0))
            mean_weight_kg = float(stock.get("fish_weight_kg", 0.0))
        except Exception:
            return f" | WQ {status}" if status else ""
        suffix = f" | WQ {fish_count:.0f} fish, {biomass_kg:.1f} kg, mean {mean_weight_kg:.2f} kg"
        if status and status != "ok":
            suffix += f", {status}"
        return suffix

    def _on_fish_ui_update(self, _event, force=False):
        if self._fish_window is None or self._aquacast_main is None:
            return
        now = time.monotonic()
        interval = float(_get_runtime_config("FISH_MANAGEMENT_UI_UPDATE_INTERVAL_SECONDS", 0.5) or 0.5)
        if not force and now - self._fish_last_update < max(0.05, interval):
            return
        self._fish_last_update = now

        tanks, species = self._fish_window_data()
        tank_labels = [path.rsplit("/", 1)[-2] if path.endswith("/Water") and "/" in path.rstrip("/") else path for path in tanks]
        species_labels = [str(item.get("label") or item.get("id") or "species") for item in species]
        compare_tank_labels = tank_labels or ["(no tank)"]
        compare_species_labels = species_labels or ["(no species)"]
        if tanks != self._fish_tanks or compare_tank_labels != self._fish_tank_labels or compare_species_labels != self._fish_species_labels:
            self._build_fish_window_contents()

        tank_path = self._selected_fish_tank()
        max_total = int(_get_runtime_config("MAX_FISH_PER_TANK", 30) or 30)
        if not tank_path:
            if self._fish_count_label is not None:
                self._fish_count_label.text = f"Total 0/{max_total} · Alive 0 · Dead 0"
            self._set_fish_button_enabled(self._fish_add_button, False)
            self._set_fish_button_enabled(self._fish_delete_button, False)
            self._set_fish_button_enabled(self._fish_clear_button, False)
            self._set_fish_status("스테이지/탱크 없음")
            return

        try:
            counts = self._aquacast_main.count_fish_in_tank(tank_path)
        except Exception as exc:
            counts = {"total": 0, "by_species": {}}
            self._set_fish_status(f"count failed: {exc}")
        total = int(counts.get("total", 0))
        alive = int(counts.get("alive", total))
        dead = int(counts.get("dead", max(0, total - alive)))
        by_species = counts.get("by_species", {}) or {}
        alive_by_species = counts.get("alive_by_species", {}) or by_species
        wq_state_counts = counts.get("wq_state_counts", {}) or {}
        parts = [f"Total {total}/{max_total}", f"Alive {alive}", f"Dead {dead}"]
        if alive > 0:
            healthy = int(wq_state_counts.get("healthy", 0))
            warn = int(wq_state_counts.get("warn", 0))
            critical = int(wq_state_counts.get("critical", 0))
            parts.append(f"WQ H/W/C {healthy}/{warn}/{critical}")
        for item in self._fish_species:
            species_id = item.get("id")
            label = item.get("label") or species_id
            species_total = int(by_species.get(species_id, 0))
            species_alive = int(alive_by_species.get(species_id, species_total))
            value = f"{species_alive}/{species_total}" if dead > 0 else f"{species_total}"
            parts.append(f"{label} {value}")
        if self._fish_count_label is not None:
            self._fish_count_label.text = " · ".join(parts)
        self._set_fish_button_enabled(self._fish_add_button, total < max_total)
        self._set_fish_button_enabled(self._fish_delete_button, total > 0)
        self._set_fish_button_enabled(self._fish_clear_button, total > 0)

    def _on_fish_add_clicked(self):
        tank = self._selected_fish_tank()
        species = self._selected_fish_species()
        qty = self._fish_qty()
        if not tank or not species:
            self._set_fish_status("스테이지/탱크 없음")
            return
        if qty <= 0:
            self._set_fish_status("수량을 1 이상 입력")
            return
        try:
            result = self._aquacast_main.add_fish(tank, species.get("id"), qty)
        except Exception as exc:
            self._set_fish_status(f"ADD 실패: {exc}")
            return
        added = int(result.get("added", 0))
        suffix = " (clamped at cap)" if result.get("clamped") else ""
        self._set_fish_status(f"ADD {qty} -> {added} added{suffix}{self._fish_stock_status_suffix(result)}")
        self._on_fish_ui_update(None, force=True)

    def _on_fish_delete_clicked(self):
        tank = self._selected_fish_tank()
        species = self._selected_fish_species()
        qty = self._fish_qty()
        if not tank or not species:
            self._set_fish_status("스테이지/탱크 없음")
            return
        if qty <= 0:
            self._set_fish_status("수량을 1 이상 입력")
            return
        try:
            result = self._aquacast_main.remove_fish(tank, species.get("id"), qty)
        except Exception as exc:
            self._set_fish_status(f"DELETE 실패: {exc}")
            return
        removed = int(result.get("removed", 0))
        status = "선택한 종 없음" if removed == 0 else f"DELETE {qty} -> {removed} removed{self._fish_stock_status_suffix(result)}"
        self._set_fish_status(status)
        self._on_fish_ui_update(None, force=True)

    def _on_fish_clear_clicked(self):
        tank = self._selected_fish_tank()
        if not tank:
            self._set_fish_status("스테이지/탱크 없음")
            return
        try:
            result = self._aquacast_main.clear_fish(tank)
        except Exception as exc:
            self._set_fish_status(f"Clear 실패: {exc}")
            return
        self._set_fish_status(f"Clear All -> {int(result.get('removed', 0))} removed{self._fish_stock_status_suffix(result)}")
        self._on_fish_ui_update(None, force=True)

    def _teardown_fish_window(self):
        self._fish_update_sub = None
        self._fish_window = None
        self._fish_tank_combo = None
        self._fish_species_combo = None
        self._fish_qty_field = None
        self._fish_count_label = None
        self._fish_status_label = None
        self._fish_add_button = None
        self._fish_delete_button = None
        self._fish_clear_button = None
        if self._fish_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._fish_menu_items, "Window")
            self._fish_menu_items = []

    def _register_aquacast_menu(self):
        if self._aquacast_menu_items:
            return
        if not bool(_get_runtime_config("ENABLE_WATER_QUALITY", _get_runtime_config("ENABLE_WATER_QUALITY_SIM", False))):
            return

        def _view(variable):
            if self._aquacast_main and hasattr(self._aquacast_main, "set_quality_view_variable"):
                self._aquacast_main.set_quality_view_variable(variable)
            self._refresh_wq_view_label(variable)

        def _feed_pulse():
            if self._aquacast_main and hasattr(self._aquacast_main, "apply_feed"):
                self._aquacast_main.apply_feed(1.0)

        def _biofilter(enabled):
            if self._aquacast_main and hasattr(self._aquacast_main, "set_biofilter"):
                self._aquacast_main.set_biofilter(enabled)

        def _scenario(name):
            if self._aquacast_main and hasattr(self._aquacast_main, "load_scenario"):
                self._aquacast_main.load_scenario(name)

        self._aquacast_menu_items = [
            MenuItemDescription(
                name="Water Quality View/Temperature",
                onclick_fn=lambda: _view("temperature"),
            ),
            MenuItemDescription(
                name="Water Quality View/Dissolved O2",
                onclick_fn=lambda: _view("dissolved_oxygen"),
            ),
            MenuItemDescription(
                name="Water Quality View/TAN",
                onclick_fn=lambda: _view("tan"),
            ),
            MenuItemDescription(
                name="Water Quality View/pH",
                onclick_fn=lambda: _view("ph"),
            ),
            MenuItemDescription(
                name="Water Quality View/CO2",
                onclick_fn=lambda: _view("co2"),
            ),
            MenuItemDescription(
                name="Water Quality Actions/Feed pulse 1 kg",
                onclick_fn=_feed_pulse,
            ),
            MenuItemDescription(
                name="Water Quality Actions/Biofilter ON",
                onclick_fn=lambda: _biofilter(True),
            ),
            MenuItemDescription(
                name="Water Quality Actions/Biofilter OFF",
                onclick_fn=lambda: _biofilter(False),
            ),
            MenuItemDescription(
                name="Water Quality Actions/Scenario baseline",
                onclick_fn=lambda: _scenario("baseline"),
            ),
            MenuItemDescription(
                name="Water Quality Actions/Scenario overfeed",
                onclick_fn=lambda: _scenario("overfeed"),
            ),
            MenuItemDescription(
                name="Water Quality Actions/Scenario pump off",
                onclick_fn=lambda: _scenario("pump_off"),
            ),
            MenuItemDescription(
                name="Water Quality Actions/Scenario high temp",
                onclick_fn=lambda: _scenario("high_temp_spike"),
            ),
        ]
        omni.kit.menu.utils.add_menu_items(self._aquacast_menu_items, name="Aquacast")

    async def __property_window(self):
        """Creates a propety window and sets column sizes."""
        await omni.kit.app.get_app().next_update_async()

        property_window = property_window_ext.get_window()
        property_window.set_scheme_delegate_layout(
            "Create Layout",
            ["basis_curves_prim", "path_prim", "material_prim",
             "xformable_prim", "shade_prim", "camera_prim"],
        )

        # expand width of path_items so "Instancable" doesn't get wrapped
        PrimPathWidget.set_path_item_padding(3.5)

    def __menu_update(self):
        """Update the menu"""
        self._menu_layout = [
            MenuLayout.Menu(
                "Window",
                [
                    MenuLayout.SubMenu(
                        "Animation",
                        [
                            MenuLayout.Item("Timeline"),
                            MenuLayout.Item("Sequencer"),
                            MenuLayout.Item("Curve Editor"),
                            MenuLayout.Item("Retargeting"),
                            MenuLayout.Item("Animation Graph"),
                            MenuLayout.Item("Animation Graph Samples"),
                        ],
                    ),
                    MenuLayout.SubMenu(
                        "Layout",
                        [
                            MenuLayout.Item("Quick Save", remove=True),
                            MenuLayout.Item("Quick Load", remove=True),
                        ],
                    ),
                    MenuLayout.SubMenu(
                        "Browsers",
                        [
                            MenuLayout.Item("Content", source="Window/Content"),
                            MenuLayout.Item("Materials"),
                            MenuLayout.Item("Skies"),
                        ],
                    ),
                    MenuLayout.SubMenu(
                        "Rendering",
                        [
                            MenuLayout.Item("Render Settings"),
                            MenuLayout.Item("Movie Capture"),
                            MenuLayout.Item("MDL Material Graph"),
                            MenuLayout.Item("Tablet XR"),
                        ],
                    ),
                    MenuLayout.SubMenu(
                        "Utilities",
                        [
                            MenuLayout.Item("Console"),
                            MenuLayout.Item("Profiler"),
                            MenuLayout.Item("USD Paths"),
                            MenuLayout.Item("Statistics"),
                            MenuLayout.Item("Activity Progress"),
                            MenuLayout.Item("Actions"),
                            MenuLayout.Item("Asset Validator"),
                        ],
                    ),
                    MenuLayout.Sort(
                        exclude_items=["Extensions"], sort_submenus=True
                    ),
                    MenuLayout.Item("New Viewport Window", remove=True),
                ],
            ),
            MenuLayout.Menu(
                "Layout",
                [
                    MenuLayout.Item("Default", source="Reset Layout"),
                    MenuLayout.Seperator(),
                    MenuLayout.Item(
                        "UI Toggle Visibility",
                        source="Window/UI Toggle Visibility"
                    ),
                    MenuLayout.Item(
                        "Fullscreen Mode", source="Window/Fullscreen Mode"
                    ),
                    MenuLayout.Seperator(),
                    MenuLayout.Item(
                        "Save Layout", source="Window/Layout/Save Layout..."
                    ),
                    MenuLayout.Item(
                        "Load Layout", source="Window/Layout/Load Layout..."
                    ),
                    MenuLayout.Seperator(),
                    MenuLayout.Item(
                        "Quick Save", source="Window/Layout/Quick Save"
                    ),
                    MenuLayout.Item(
                        "Quick Load", source="Window/Layout/Quick Load"
                    ),
                ],
            ),
        ]
        omni.kit.menu.utils.add_layout(self._menu_layout)

        self._layout_menu_items = []

        def add_layout_menu_entry(name, parameter, key):
            """Add a layout menu entry."""
            if inspect.isfunction(parameter):
                menu_dict = omni.kit.menu.utils.build_submenu_dict(
                    [
                        MenuItemDescription(name=f"Layout/{name}",
                                            onclick_fn=lambda: asyncio.ensure_future(parameter()),
                                            hotkey=(carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL, key)),
                    ]
                )
            else:
                async def _active_layout(layout):
                    await _load_layout(layout)
                    # load layout file again to make sure layout correct
                    await _load_layout(layout)

                menu_dict = omni.kit.menu.utils.build_submenu_dict(
                    [
                        MenuItemDescription(name=f"Layout/{name}",
                                            onclick_fn=lambda: asyncio.ensure_future(_active_layout(f"{DATA_PATH}/layouts/{parameter}.json")),
                                            hotkey=(carb.input.KEYBOARD_MODIFIER_FLAG_CONTROL, key)),
                    ]
                )

            # add menu
            for group in menu_dict:
                omni.kit.menu.utils.add_menu_items(menu_dict[group], group)

            self._layout_menu_items.append(menu_dict)

        add_layout_menu_entry(
            "Reset Layout", "default", carb.input.KeyboardInput.KEY_1
        )

        # create Quick Load & Quick Save
        async def quick_save():
            QuickLayout.quick_save(None, None)

        async def quick_load():
            QuickLayout.quick_load(None, None)

        add_layout_menu_entry(
            "Quick Save", quick_save, carb.input.KeyboardInput.KEY_7
        )
        add_layout_menu_entry(
            "Quick Load", quick_load, carb.input.KeyboardInput.KEY_8
        )

        # open "Asset Stores" window
        ui.Workspace.show_window("Asset Stores")


    def _iter_dev_reload_files(self):
        root = Path(__file__).resolve().parents[2]
        for path in root.rglob("*.py"):
            if "__pycache__" not in path.parts:
                yield path
        config_path = root / "config" / "extension.toml"
        if config_path.exists():
            yield config_path

    def _snapshot_dev_reload_files(self):
        snapshot = {}
        for path in self._iter_dev_reload_files():
            try:
                snapshot[str(path)] = path.stat().st_mtime_ns
            except OSError:
                continue
        return snapshot

    def _start_dev_autoreload(self):
        self._dev_reload_snapshot = self._snapshot_dev_reload_files()
        stream = omni.kit.app.get_app().get_update_event_stream()
        self._dev_reload_sub = stream.create_subscription_to_pop(
            self._on_dev_autoreload_update,
            name="test-Aquacast extension autoreload",
        )
        carb.log_info("[test-Aquacast] Watching extension files for auto reload")

    def _on_dev_autoreload_update(self, _event):
        if self._dev_reload_requested:
            return

        now = time.monotonic()
        if now - self._dev_reload_last_check < 1.0:
            return
        self._dev_reload_last_check = now

        snapshot = self._snapshot_dev_reload_files()
        if snapshot == self._dev_reload_snapshot:
            return

        self._dev_reload_requested = True
        carb.log_warn("[test-Aquacast] Extension file changed; reloading extension")
        asyncio.ensure_future(self._reload_dev_extension())

    async def _reload_dev_extension(self):
        app = omni.kit.app.get_app()
        manager = app.get_extension_manager()
        ext_id = self._dev_reload_ext_id
        await app.next_update_async()
        manager.set_extension_enabled_immediate(ext_id, False)
        await app.next_update_async()
        manager.set_extension_enabled_immediate(ext_id, True)

    def on_shutdown(self):
        """Clean up the extension"""
        self._dev_reload_sub = None
        if getattr(self, "_aquacast_main", None):
            if self._water_quality_controller:
                self._aquacast_main.stop_water_quality_controller()
                self._water_quality_controller = None
            if self._water_temp_controller:
                self._aquacast_main.stop_water_temp_controller()
                self._water_temp_controller = None
            if self._fish_swim_controller:
                self._aquacast_main.stop_fish_swim_controller()
                self._fish_swim_controller = None
            if self._dynamic_fish_spawner:
                self._aquacast_main.stop_dynamic_fish_spawner()
                self._dynamic_fish_spawner = None
            if self._stage_structure_cache:
                self._aquacast_main.stop_stage_structure_cache()
                self._stage_structure_cache = None
            self._aquacast_main = None
        self._sub_fabric_delegate_changed = None
        self._sensor_update_sub = None
        self._actuator_update_sub = None
        self._metrics_update_sub = None
        self._sensor_window = None
        self._wq_view_window = None
        self._control_window = None
        self._actuator_window = None
        self._teardown_metrics_dashboard_window()
        self._teardown_local_llm_panel()
        self._teardown_fish_window()
        if self._sensor_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._sensor_menu_items, "Window")
            self._sensor_menu_items = []
        if self._wq_view_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._wq_view_menu_items, "Window")
            self._wq_view_menu_items = []
        if self._control_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._control_menu_items, "Window")
            self._control_menu_items = []
        if self._actuator_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._actuator_menu_items, "Window")
            self._actuator_menu_items = []
        if self._metrics_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._metrics_menu_items, "Window")
            self._metrics_menu_items = []
        if self._local_llm_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._local_llm_menu_items, "Window")
            self._local_llm_menu_items = []
        if self._aquacast_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._aquacast_menu_items, "Aquacast")
            self._aquacast_menu_items = []

        omni.kit.menu.utils.remove_layout(self._menu_layout)
        self._menu_layout = None

        for menu_dict in self._layout_menu_items:
            for group in menu_dict:
                omni.kit.menu.utils.remove_menu_items(menu_dict[group], group)

        self._layout_menu_items = None
        self._launcher_menu = None
        self._reset_menu = None
