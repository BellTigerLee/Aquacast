# Aquacast 현재 구현 기능 기반 디지털 트윈 시나리오 후보

작성일: 2026-06-13

## 1. 범위

이 문서는 **현재 코드에 구현되어 있는 기능만** 사용해서 Aquacast 디지털 트윈 화면에서 보여줄 수 있는 후보 시나리오를 정리한다.

포함 기준은 다음과 같다.

| 구분 | 포함 여부 | 기준 |
| --- | --- | --- |
| JSON 프리셋 | 포함 | `data/wq_scenarios.json`에 존재 |
| UI 버튼 | 포함 | `Aquacast Tank Controls` 또는 `Aquacast` 메뉴에서 실행 가능 |
| 모델 액션 | 포함 | `list_water_quality_actions()` / `WaterQualityModel._apply_action_now()`에 구현 |
| 향후 아이디어 | 제외 | 코드에 없는 실센서, 실액추에이터, 자동제어, 알람 정책 |

## 2. 현재 시연 가능한 기능 목록

### 2.1 수질 프리셋

| 프리셋 | 현재 접근 경로 | 설명 |
| --- | --- | --- |
| `baseline` | Tank Controls, Aquacast menu, API | 정상 기준 상태 |
| `normal` | API / 모델 | baseline과 동일한 정상 상태, 현재 UI 버튼은 없음 |
| `overfeed` | Tank Controls, Aquacast menu, API | feed pool과 turbidity가 높은 과급이 상태 |
| `pump_off` | Tank Controls, Aquacast menu, API | inflow off, flow 0 상태 |
| `biofilter_off` | Tank Controls, API | biofilter off 상태, 현재 Aquacast menu의 scenario 항목에는 없음 |
| `high_temp_spike` | Aquacast menu, API | 고수온, 낮은 DO, 약간 높은 CO2/turbidity 상태 |

### 2.2 Tank Controls 액션

| UI 그룹 | 구현 액션 | 디지털 트윈에서 보여줄 수 있는 변화 |
| --- | --- | --- |
| Thermal | `set_temperature`, `set_heater`, `set_inlet_temperature` | 수온 변화, heater 상태, 온도 particle color |
| Feeding / Stock | `feed`, `set_stock` | feed pool 증가, DO 저하, TAN/NH3/CO2/turbidity 증가 |
| Water Exchange / Inlet | `set_water_exchange`, `set_inflow`, `set_inlet_salinity`, `set_inlet_turbidity`, `set_inlet_do`, `set_inlet_alkalinity` | 유입/유출 상태, 유입수 조건에 따른 수질 변화 |
| Filtration / Emergency | `set_biofilter`, `set_mechanical_filter`, `oxygen_boost`, `co2_pulse`, `dose_salt`, `add_turbidity` | biofilter/mech/heater/inlet/outlet 상태와 긴급 조치 효과 |
| Scenarios | `load_scenario` | baseline, overfeed, pump_off, biofilter_off 전환 |

### 2.3 화면에서 관찰 가능한 값

| 표시 영역 | 현재 가능한 표시 |
| --- | --- |
| Particle color | `temperature`, `dissolved_oxygen`, `tan`, `co2`, `alkalinity`, `salinity`, `turbidity`, `ph`, `nh3` primvar 지원 |
| Aquacast menu view | Temperature, Dissolved O2, TAN, pH, CO2 |
| Water Quality Sensor | Total 및 개별 sensor 값 표시 |
| Sensor 목록 | `inlet_reference`, `feed_zone_tan`, `fish_core_do`, `bottom_co2`, `biofilter_sentinel`, `mixed_tank_outlet` |
| Actuator Overview | Inlet, Outlet, Biofilter, Mech, Heater 상태 dot |
| Fish Management | 탱크별 fish add/delete/clear, species 선택, swimming |

## 3. 바로 시연하기 좋은 핵심 시나리오

### S1. 정상 운전 기준선

**목적:** Aquacast 디지털 트윈의 기본 화면, 센서, 액추에이터, 수질 view 전환을 소개한다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | `load_scenario("baseline")` |
| 주요 화면 | Water Quality Sensor `Total`, Actuator Overview, particle color |
| 권장 view | Temperature → Dissolved O2 → TAN → pH → CO2 순서 |
| 관찰 포인트 | 수온 14 C, DO 9 mg/L, TAN 0.3 mg/L, CO2 5 mg/L, pH 정상권 |
| 구현 상태 | 바로 가능 |

**데모 흐름**

1. Tank Controls에서 `Baseline` 클릭
2. Sensor를 `Total`로 두고 전체 수질값 확인
3. Aquacast menu에서 Water Quality View를 순서대로 전환
4. Actuator Overview에서 Inlet/Outlet/Biofilter/Mech/Heater 상태 확인
5. Fish Management가 켜져 있으면 물고기 움직임과 수질 디지털 트윈이 같은 장면에 있음을 보여줌

### S2. 과급이로 인한 TAN/NH3/탁도 상승

**목적:** 사료 과다 투입이 수질 악화로 이어지는 과정을 보여준다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | `load_scenario("overfeed")` 또는 `Feed kg` pulse 반복 |
| 주요 화면 | Feed TAN sensor, Total sensor, particle color |
| 권장 view | TAN, pH, CO2, Dissolved O2 |
| 관찰 포인트 | feed pool 2.5 kg, TAN 상승, NH3 위험, turbidity 상승, DO 저하 가능성 |
| 구현 상태 | 바로 가능 |

**데모 흐름**

1. `Baseline`에서 정상 상태를 먼저 보여줌
2. `Overfeed` 클릭
3. Water Quality View를 `TAN`으로 전환
4. Sensor를 `feed_zone_tan` 또는 `Total`로 바꿔 TAN/NH3 확인
5. 필요하면 `O2 +1`, `Flow L/h` 증가, `Mech ON`으로 대응 조치를 보여줌

**좋은 설명 포인트**

과급이는 단일 값 하나만 나빠지는 이벤트가 아니라 DO, TAN, CO2, pH, 탁도가 같이 움직이는 복합 사고로 보여줄 수 있다.

### S3. 펌프 정지 / 유입수 차단

**목적:** 순환/환수 장애가 발생했을 때 inlet/outlet 상태와 수질 변화를 보여준다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | `load_scenario("pump_off")` 또는 `Inflow OFF` + `Flow L/h=0` |
| 주요 화면 | Actuator Overview, Total sensor, particle color |
| 권장 view | Dissolved O2, CO2, Temperature |
| 관찰 포인트 | Inlet/Outlet off, flow 0, DO 낮아짐, CO2 높아짐 |
| 구현 상태 | 바로 가능 |

**데모 흐름**

1. `Baseline`에서 Inlet/Outlet on 상태 확인
2. `Pump Off` 클릭
3. Actuator Overview에서 Inlet/Outlet dot이 off로 바뀌는지 확인
4. Water Quality View를 `Dissolved O2`와 `CO2`로 전환
5. `Inflow ON`, `Flow L/h=2000`, `O2 +1`로 복구 흐름을 보여줌

### S4. 바이오필터 정지 / 질산화 실패

**목적:** biofilter가 꺼졌을 때 TAN/NH3 제거가 멈추는 상황을 보여준다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | `load_scenario("biofilter_off")` 또는 Biofilter `OFF` |
| 주요 화면 | Actuator Overview, biofilter_sentinel sensor, TAN view |
| 권장 view | TAN, pH |
| 관찰 포인트 | Biofilter off, TAN/NH3 축적, nitrification 중단 |
| 구현 상태 | 바로 가능 |

**데모 흐름**

1. `Baseline`에서 Biofilter on 상태 확인
2. `Biofilter Off` 클릭 또는 Biofilter `OFF`
3. Actuator Overview에서 Biofilter dot off 확인
4. Water Quality View를 `TAN`으로 전환
5. `Biofilter ON`으로 복구한 뒤 TAN 제거 방향을 설명

**주의점**

현재 nitrite/nitrate 값은 모델 seam만 있고 기본적으로 0이다. 따라서 biofilter 시나리오는 nitrite/nitrate보다 TAN/NH3 중심으로 보여주는 것이 맞다.

### S5. 고수온 스트레스 / 산소 리스크

**목적:** 수온 상승이 DO 리스크와 연결되는 장면을 보여준다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | Aquacast menu `Scenario high temp` 또는 `load_scenario("high_temp_spike")` |
| 주요 화면 | Temperature view, Dissolved O2 view, fish tank scene |
| 권장 view | Temperature → Dissolved O2 |
| 관찰 포인트 | temperature 22 C, DO 7.2 mg/L, CO2 6.5 mg/L, turbidity 3.5 NTU |
| 구현 상태 | 바로 가능, 단 Tank Controls에는 별도 high-temp 버튼 없음 |

**데모 흐름**

1. `Baseline`에서 Temperature view 확인
2. Aquacast menu에서 `Water Quality Actions/Scenario high temp` 실행
3. Temperature view에서 고수온 상태 확인
4. Dissolved O2 view로 바꿔 산소 여유가 줄어드는 상황 설명
5. `Set Temp C=14`, `Inlet Temp C=12`, `Flow L/h` 증가 등으로 복구 조합을 보여줌

### S6. 환수량 조절에 따른 수질 회복

**목적:** flow 조절이 수질 회복에 어떤 영향을 주는지 보여준다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | `overfeed` 또는 `pump_off` 이후 |
| 주요 액션 | `set_water_exchange`, `set_inflow` |
| 주요 화면 | Actuator Overview, Total sensor |
| 권장 view | Dissolved O2, TAN, CO2 |
| 구현 상태 | 액션 조합으로 가능 |

**데모 흐름**

1. `Overfeed` 또는 `Pump Off`로 수질 악화 상태 생성
2. `Flow L/h`를 낮은 값으로 설정해 회복이 느린 상태 설명
3. `Flow L/h=2000` 또는 더 높은 값으로 변경
4. Inlet/Outlet 상태와 DO/TAN/CO2 변화를 비교

### S7. 기계식 여과와 탁도 대응

**목적:** 탁도 사고와 mechanical filter의 역할을 보여준다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | `Turb +5` 반복 또는 `overfeed` |
| 주요 액션 | `add_turbidity`, `set_mechanical_filter` |
| 주요 화면 | Actuator Overview, Total sensor |
| 권장 view | Turbidity primvar 또는 Total sensor의 turbidity |
| 구현 상태 | 액션 조합으로 가능, Aquacast menu view에는 Turbidity 항목 없음 |

**데모 흐름**

1. `Baseline`에서 turbidity 2 NTU 확인
2. `Turb +5` 클릭
3. Total sensor에서 turbidity 증가 확인
4. `Mech ON`으로 mechanical filter 상태 표시
5. 시간이 지나며 turbidity settling/removal 방향을 설명

### S8. 입식량 증가 / 사육밀도 스트레스

**목적:** fish count와 weight가 수질 부하를 키우는 것을 보여준다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | `Baseline` |
| 주요 액션 | `set_stock` |
| 주요 화면 | Fish Management, Total sensor, Water Quality View |
| 권장 view | Dissolved O2, TAN, CO2 |
| 구현 상태 | 액션 조합으로 가능 |

**데모 흐름**

1. `Baseline`에서 stock 기본값 200 fish, 1 kg 확인
2. Tank Controls의 Stock 값을 더 높게 적용
3. DO 소비, CO2, TAN 부하가 커지는 구조 설명
4. Fish Management에서 시각적 물고기 수와 수질 모델 stock은 별개일 수 있음을 주의해서 설명

**주의점**

현재 화면상의 fish 개체 수와 water-quality 모델의 `fish_count`는 자동 동기화된다고 보기 어렵다. 데모에서는 “수질 모델의 사육밀도 파라미터”로 설명하는 것이 안전하다.

### S9. 유입수 품질 변화

**목적:** 유입수의 DO, alkalinity, salinity, turbidity 조건이 수조에 미치는 영향을 보여준다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | `Baseline`, Inflow ON |
| 주요 액션 | `set_inlet_do`, `set_inlet_alkalinity`, `set_inlet_salinity`, `set_inlet_turbidity`, `set_inlet_temperature` |
| 주요 화면 | inlet_reference sensor, Total sensor |
| 권장 view | Temperature, Dissolved O2, pH, CO2 |
| 구현 상태 | Tank Controls로 가능 |

**데모 흐름**

1. Sensor를 `inlet_reference`로 선택
2. Inlet DO 또는 Inlet Alk 값을 변경
3. `Total` sensor로 돌아와 bulk 수질 변화를 비교
4. Inlet Temp C를 낮추거나 높여 thermal 영향도 같이 설명

### S10. CO2/pH 스트레스와 응급 대응

**목적:** CO2 증가가 pH 저하와 연결되는 것을 보여준다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | `Baseline` |
| 주요 액션 | `co2_pulse`, `set_co2_stripping`, `dose_alkalinity` |
| 주요 화면 | Total sensor, CO2 view, pH view |
| 권장 view | CO2 → pH |
| 구현 상태 | 일부 UI 가능, `set_co2_stripping`과 `dose_alkalinity`는 action schema/model에는 있으나 Tank Controls 버튼은 없음 |

**데모 흐름**

1. `CO2 +2` 클릭
2. CO2 view와 Total sensor에서 CO2 증가 확인
3. pH view로 전환해 carbonate 계산에 따른 pH 변화를 설명
4. API 또는 별도 action 호출이 가능하면 alkalinity dosing 또는 CO2 stripping 조치를 시연

### S11. 염도/염분 투입 시나리오

**목적:** salinity 값을 조정하거나 salt dose를 넣었을 때 상태값이 바뀌는 것을 보여준다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | `Baseline` |
| 주요 액션 | `dose_salt`, `set_inlet_salinity` |
| 주요 화면 | Total sensor |
| 권장 view | Salinity primvar 또는 Total sensor의 salinity |
| 구현 상태 | Tank Controls로 가능, Aquacast menu view에는 Salinity 항목 없음 |

**데모 흐름**

1. Total sensor에서 salinity 0.2 ppt 확인
2. `Salt +0.3` 클릭
3. Total sensor에서 salinity 증가 확인
4. Inlet Sal ppt 값을 변경해 유입수 기준값 변경도 보여줌

### S12. 사고 후 복구 프로토콜 데모

**목적:** 하나의 사고를 만들고 여러 조치를 순서대로 적용해 회복 플로우를 보여준다.

| 항목 | 내용 |
| --- | --- |
| 시작 조건 | `overfeed`, `pump_off`, `biofilter_off` 중 하나 |
| 주요 액션 | `set_inflow`, `set_water_exchange`, `oxygen_boost`, `set_biofilter`, `set_mechanical_filter` |
| 주요 화면 | Actuator Overview, Total sensor, Water Quality View |
| 권장 view | 사고별로 TAN/DO/CO2/pH 전환 |
| 구현 상태 | 액션 조합으로 가능 |

**예시 흐름: overfeed 복구**

1. `Overfeed` 실행
2. TAN view에서 위험 상태 확인
3. `Flow L/h` 증가
4. `O2 +1`로 산소 응급 대응
5. `Mech ON`으로 탁도 대응
6. Sensor Total에서 DO, TAN, turbidity, pH 변화를 확인

**예시 흐름: pump off 복구**

1. `Pump Off` 실행
2. Actuator Overview에서 Inlet/Outlet off 확인
3. `Inflow ON` 실행
4. `Flow L/h=2000` 적용
5. Dissolved O2와 CO2 view로 회복 방향 확인

## 4. 시연 우선순위

| 우선순위 | 시나리오 | 이유 |
| --- | --- | --- |
| 1 | S1 정상 운전 기준선 | 모든 데모의 출발점 |
| 2 | S2 과급이 | 수질 악화가 가장 직관적이고 TAN/NH3/turbidity 설명 가능 |
| 3 | S3 펌프 정지 | actuator 상태 변화가 눈에 잘 보임 |
| 4 | S4 바이오필터 정지 | biofilter dot과 TAN/NH3를 연결하기 좋음 |
| 5 | S5 고수온 스트레스 | Temperature/DO view 전환으로 설명력이 좋음 |
| 6 | S12 사고 후 복구 | 단순 관찰이 아니라 조치 효과까지 보여줄 수 있음 |
| 7 | S7 탁도 대응 | mechanical filter 기능을 보여주기 좋음 |
| 8 | S8 입식량 증가 | 운영 파라미터 what-if로 좋지만 fish 개체 수와 모델 stock 분리 설명 필요 |

## 5. 데모 구성 추천

### 5.1 5분 짧은 데모

1. `Baseline`으로 정상 수질 확인
2. Water Quality View를 Temperature, DO, TAN으로 전환
3. `Overfeed` 실행
4. TAN/NH3/turbidity 증가 설명
5. `O2 +1`, `Mech ON`, `Flow L/h` 증가로 대응 조치 시연

### 5.2 10분 운영자 데모

1. `Baseline`으로 정상 상태 확인
2. `Pump Off`로 Inlet/Outlet off 확인
3. DO/CO2 view로 영향 확인
4. `Inflow ON`, `Flow L/h=2000`으로 복구
5. `Biofilter Off`로 TAN/NH3 문제 확인
6. `Biofilter ON`으로 복구 방향 설명

### 5.3 15분 풀 데모

1. 정상 운전 기준선
2. 과급이 사고
3. 펌프 정지 사고
4. 바이오필터 정지 사고
5. 고수온 스트레스
6. 응급 조치 조합
7. Kafka/SQLite가 켜져 있으면 센서 데이터가 backend에서 같이 쌓이는 구조 설명

## 6. 현재 구현상 주의사항

| 주의사항 | 데모에서의 대응 |
| --- | --- |
| `high_temp_spike`는 JSON과 Aquacast menu에는 있으나 Tank Controls scenario 버튼에는 없음 | Aquacast menu 또는 API로 실행 |
| `normal` 프리셋은 JSON에는 있으나 별도 UI 버튼이 없음 | baseline과 동일 계열로 취급 |
| Aquacast menu view는 Temperature, DO, TAN, pH, CO2 중심 | salinity/turbidity/NH3는 Sensor Total 또는 내부 view setter/API 중심으로 설명 |
| nitrite/nitrate는 현재 0으로 표시됨 | biofilter 시나리오는 TAN/NH3 중심으로 설명 |
| fish 화면 개체 수와 WQ `fish_count`는 별도일 수 있음 | stock 시나리오에서는 수질 모델 파라미터라고 명확히 설명 |
| backend가 꺼져 있으면 WQ controller가 준비되지 않을 수 있음 | 데모 전 backend 실행 상태 확인 |

## 7. 결론

현재 구현 상태에서 가장 좋은 디지털 트윈 시나리오는 다음 5개다.

1. `baseline`: 정상 운전 기준선
2. `overfeed`: 과급이로 인한 TAN/NH3/탁도 상승
3. `pump_off`: 펌프/환수 장애와 DO/CO2 악화
4. `biofilter_off`: 바이오필터 장애와 TAN/NH3 축적
5. `high_temp_spike`: 고수온과 산소 리스크

이 5개에 Tank Controls의 `Flow L/h`, `Inflow`, `Biofilter`, `Mech`, `O2 +1`, `Feed kg`, `Stock`, `Set Temp C`를 조합하면, 현재 코드만으로도 “정상 → 사고 → 관찰 → 조치 → 회복” 형태의 디지털 트윈 데모를 구성할 수 있다.
