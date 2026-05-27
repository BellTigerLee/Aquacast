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

DATA_PATH = Path(carb.tokens.get_tokens_interface().resolve(
    "${aquacast.aquacast_composer_extensions}")
)

_RUNTIME_MODULE_NAME = "test_aquacast_runtime_main"
_RUNTIME_REGISTRY_NAME = "_aquacast_runtime_modules"


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
        self._sensor_menu_items = []
        self._wq_view_window = None
        self._wq_view_label = None
        self._wq_view_menu_items = []
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
        self._sensor_window = ui.Window(title, width=430, height=310, visible=True)
        with self._sensor_window.frame:
            with ui.VStack(spacing=6):
                ui.Label("Water Quality Sensor" if quality_enabled else "Temperature Sensor", height=22)
                self._sensor_status_label = ui.Label("Waiting for particles", height=20)
                self._sensor_value_labels = {}
                rows = [
                    ("temperature_c", "Temp"),
                    ("dissolved_oxygen_mg_l", "DO"),
                    ("tan_mg_l", "TAN"),
                    ("co2_mg_l", "CO2"),
                    ("alkalinity_mg_l_as_caco3", "Alk"),
                    ("ph", "pH"),
                    ("nh3_mg_l", "NH3"),
                ] if quality_enabled else [
                    ("average_c", "Average"),
                    ("range_c", "Range"),
                    ("sample_count", "Samples"),
                ]
                for key, label in rows:
                    with ui.HStack(height=20):
                        ui.Label(label, width=105)
                        value_label = ui.Label("--")
                        self._sensor_value_labels[key] = value_label
                self._sensor_path_label = ui.Label("Sensor: --", height=42, word_wrap=True)

        self._sensor_update_sub = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_sensor_ui_update,
            name="aquacast_temperature_sensor_ui",
        )
        self._register_sensor_window_menu()
        self._on_sensor_ui_update(None)

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
            if quality_enabled and hasattr(self._aquacast_main, "sample_water_quality_sensor"):
                reading = self._aquacast_main.sample_water_quality_sensor()
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
            if self._sensor_path_label:
                self._sensor_path_label.text = f"Sensor: {reading.get('sensor_path', '--')}"
            return

        if "dissolved_oxygen_mg_l" in reading:
            if self._sensor_status_label:
                self._sensor_status_label.text = str(reading.get("sensor_name", "sensor"))
            self._set_sensor_label("temperature_c", f"{reading.get('temperature_c', 0.0):.2f} C")
            self._set_sensor_label("dissolved_oxygen_mg_l", f"{reading.get('dissolved_oxygen_mg_l', 0.0):.2f} mg/L")
            self._set_sensor_label("tan_mg_l", f"{reading.get('tan_mg_l', 0.0):.3f} mg/L")
            self._set_sensor_label("co2_mg_l", f"{reading.get('co2_mg_l', 0.0):.2f} mg/L")
            self._set_sensor_label("alkalinity_mg_l_as_caco3", f"{reading.get('alkalinity_mg_l_as_caco3', 0.0):.1f} mg/L CaCO3")
            self._set_sensor_label("ph", f"{reading.get('ph', 0.0):.2f}")
            self._set_sensor_label("nh3_mg_l", f"{reading.get('nh3_mg_l', 0.0):.4f} mg/L")
            if self._sensor_path_label:
                self._sensor_path_label.text = f"Sensor: {reading.get('sensor_path', '--')}"
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

        self._wq_view_window = ui.Window("Aquacast Water Quality View", width=300, height=190, visible=True)
        with self._wq_view_window.frame:
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
            "nh3": "NH3",
        }.get(str(variable or ""), str(variable or "--"))
        self._wq_view_label.text = f"Current: {label}"

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
            if self._stage_structure_cache:
                self._aquacast_main.stop_stage_structure_cache()
                self._stage_structure_cache = None
            self._aquacast_main = None
        self._sub_fabric_delegate_changed = None
        self._sensor_update_sub = None
        self._sensor_window = None
        self._wq_view_window = None
        if self._sensor_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._sensor_menu_items, "Window")
            self._sensor_menu_items = []
        if self._wq_view_menu_items:
            omni.kit.menu.utils.remove_menu_items(self._wq_view_menu_items, "Window")
            self._wq_view_menu_items = []
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
