# Aquacast Tank Controls

이 문서는 `Aquacast Tank Controls` UI에서 각 버튼이 어떤 action을 보내고, 모델에서 어떤 값이 바뀌는지 정리한 것이다.

검증 기준 파일은 `extension.py`, `main.py`, `water_quality_model.py`, `thermal_dynamics.py`, `water_quality_dynamics.py`이다.

## Scope

`Aquacast Tank Controls`의 모든 버튼은 선택된 `Tank`의 `tank_path`를 붙여 `execute_water_quality_action()`으로 보낸다.

backend 사용 시 `WaterQualityBackend`가 `tank_path`별 `WaterQualityModel`을 lazy-create해서 해당 탱크 모델만 바꾼다.

backend 미사용 local mode에서도 `WaterQualityController`가 `tank_path`별 local model을 만들어 해당 탱크 모델만 바꾼다.

선택된 탱크가 없으면 action은 기본 shared model로 들어간다.

## Sensor Total

`Aquacast Water Quality Sensor`의 `Sensor` dropdown에는 `Total` pseudo sensor가 있다.

`Total`은 실제 USD sensor prim이 아니라 선택된 탱크의 bulk snapshot을 보여준다.

`Total` 선택 시 모든 water-quality 표시 row가 보인다: Temp, DO, TAN, CO2, Alk, Salinity, Turbidity, pH, NH3, Nitrite, Nitrate.

개별 sensor 선택 시에는 해당 sensor 담당 항목만 보인다.

Sensor 패널에는 actuator 상태 표시도 있다. 초록 원은 ON, 빨간 원은 OFF, 회색 원은 상태값 없음/unknown이다.

`Window > Aquacast/Actuator Overview` 패널에서도 모든 탱크의 actuator 상태를 한눈에 볼 수 있다. 이 패널은 탱크별 row에 Inlet, Outlet, Biofilter, Mech, Heater 상태를 같은 색상 원형 indicator로 표시한다.

| Actuator | ON 판정 |
| --- | --- |
| Inlet | `inlet_enabled=True`, 즉 `inflow_enabled=True`이고 `q_makeup_lph > 0` |
| Outlet | `outlet_enabled=True`, 즉 `inflow_enabled=True`이고 `flow_lph > 0` |
| Biofilter | `biofilter_on=True` |
| Mech | `mechanical_filter_on=True`, 즉 `turbidity_settle_h > 0` |
| Heater | `heater_on=True`, 즉 `heater_power_w > 0` |

## Thermal Controls

| UI | Action | 바뀌는 값 | 효과 |
| --- | --- | --- | --- |
| Set Temp C / Apply | `set_temperature` | `state.temperature_c` | 현재 물 온도를 즉시 지정한다. 해당 tank particle temperature도 같은 평균으로 즉시 맞춘다. |
| Heater W / Apply | `set_heater` | `params.heater_power_w`, `params.heater_power` | 히터 전력 W를 설정한다. 온도를 즉시 바꾸지 않고 다음 thermal step에서 `q_heater_w`로 `q_net_w`에 더해진다. |
| Inlet Temp C / Apply | `set_inlet_temperature` | `params.inlet_temp_c` | 유입수 온도를 설정한다. inflow가 켜져 있을 때 advective heat term으로 수온 변화에 반영된다. |

`Heater W`는 목표 온도가 아니다. 500을 넣으면 500 W heater가 켜진 상태처럼 계산된다.

## Feeding And Stock

| UI | Action | 바뀌는 값 | 효과 |
| --- | --- | --- | --- |
| Feed kg / Pulse | `feed` | `state.feed_pool_kg += mass_kg` | 사료 pulse를 즉시 추가한다. 이후 `tau_feed_h`로 감소하며 O2 소비, TAN, CO2, turbidity source에 반영된다. |
| Stock / Apply | `set_stock` | `params.fish_count`, `params.fish_weight_kg` | biomass를 바꾼다. fish O2 소비, fish CO2, fish TAN, fish TSS, baseline feed 계산에 반영된다. |

## Water Exchange And Inlet

| UI | Action | 바뀌는 값 | 효과 |
| --- | --- | --- | --- |
| Flow L/h / Apply | `set_water_exchange` | `params.flow_lph`, `params.q_makeup_lph` | 유량을 설정한다. inflow가 켜져 있을 때 DO, TAN, CO2, Alk, Salinity, Turbidity dilution/input과 thermal advective heat에 반영된다. |
| Inflow ON | `set_inflow(enabled=True)` | `params.inflow_enabled=True` | 유입/교환을 켠다. |
| Inflow OFF | `set_inflow(enabled=False)` | `params.inflow_enabled=False` | 유입/교환을 끈다. dynamics에서 `q_lph=0`이 되어 inlet 값들이 들어오지 않는다. |
| Inlet Sal ppt / Apply | `set_inlet_salinity` | `params.salinity_in_ppt` | inflow가 켜져 있을 때 salinity가 이 값 쪽으로 이동한다. salinity는 DO saturation에도 영향을 준다. |
| Inlet Turb NTU / Apply | `set_inlet_turbidity` | `params.turbidity_in_ntu` | inflow turbidity 기준값을 바꾼다. turbidity dilution/input과 mechanical settling 기준값에 반영된다. |
| Inlet DO / Apply | `set_inlet_do` | `params.do_in` | inflow DO 농도를 설정한다. inflow가 켜져 있을 때 DO balance에 반영된다. |
| Inlet Alk / Apply | `set_inlet_alkalinity` | `params.alk_in` | inflow alkalinity를 설정한다. inflow가 켜져 있을 때 alkalinity balance에 반영된다. |

Tank-scoped `set_inflow`는 water-quality model의 inflow만 바꾼다. 전역 temperature controller toggle은 건드리지 않는다.

## Filtration And Emergency

| UI | Action | 바뀌는 값 | 효과 |
| --- | --- | --- | --- |
| Biofilter ON | `set_biofilter(enabled=True)` | `params.biofilter_on=True` | nitrification을 켠다. TAN removal, DO consumption, alkalinity consumption이 발생한다. |
| Biofilter OFF | `set_biofilter(enabled=False)` | `params.biofilter_on=False` | nitrification을 끈다. TAN removal이 멈춘다. |
| Mech ON | `set_mechanical_filter(enabled=True, settle_h=0.35)` | `params.turbidity_settle_h=0.35` | turbidity settling/removal을 켠다. |
| Mech OFF | `set_mechanical_filter(enabled=False)` | `params.turbidity_settle_h=0.0` | turbidity settling/removal을 끈다. |
| O2 +1 | `oxygen_boost` | `state.dissolved_oxygen_mg_l += 1.0` | DO를 즉시 1 mg/L 올린다. |
| CO2 +2 | `co2_pulse` | `state.co2_mg_l += 2.0` | CO2를 즉시 2 mg/L 올린다. pH는 다음 snapshot에서 carbonate calculation으로 낮아질 수 있다. |
| Salt +0.3 | `dose_salt` | `state.salinity_ppt += 0.3` | salinity를 즉시 0.3 ppt 올린다. |
| Turb +5 | `add_turbidity` | `state.turbidity_ntu += 5.0` | turbidity를 즉시 5 NTU 올린다. |

## Scenarios

| UI | Action | 효과 |
| --- | --- | --- |
| Baseline | `load_scenario(name="baseline")` | 선택 탱크 모델을 baseline 초기 상태/params로 로드한다. |
| Overfeed | `load_scenario(name="overfeed")` | feed pool과 water quality를 overfeed preset으로 바꾼다. |
| Pump Off | `load_scenario(name="pump_off")` | `inflow_enabled=false`, `flow_lph=0.0` preset을 로드한다. |
| Biofilter Off | `load_scenario(name="biofilter_off")` | `biofilter_on=false` preset을 로드한다. |

Scenario load는 선택된 tank model에만 적용된다.

## Important Notes

`Set Temp C`, `O2 +1`, `CO2 +2`, `Salt +0.3`, `Turb +5`, `Feed kg`은 state를 즉시 바꾸는 action이다.

`Heater W`, `Inlet Temp C`, `Flow L/h`, `Inflow`, inlet chemistry, `Biofilter`, `Mech`, `Stock`은 params를 바꾸고 다음 simulation advance에서 점진적으로 영향을 준다.

모든 action은 성공 후 water-quality visuals refresh를 요청한다. Omniverse/Kit이 이미 실행 중이면 extension reload/restart 후 UI 변경과 문서화된 behavior가 반영된다.
