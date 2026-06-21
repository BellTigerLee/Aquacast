# Aquacast Demo Scenario

## 목적

이 문서는 Aquacast 시연에서 어떤 순서로 기능을 보여줄지 정리한 운영 시나리오입니다. 핵심은 복잡한 모델 설명보다, 사용자가 탱크를 선택하고 원클릭 시나리오를 적용한 뒤 수질 변화, actuator 상태, AI proposal 흐름을 바로 이해하게 만드는 것입니다.

시연은 `beginner` 운영자 경험을 기준으로 합니다. 화면에서는 `Aquacast Tank Controls`, `Aquacast Sensor Overview`, `Aquacast Metrics Dashboard`, `Aquacast Water Quality View`, `Aquacast Local LLM Panel`을 중심으로 보여줍니다.

## 사전 준비

1. Aquacast Composer를 실행합니다.

```bash
cd /home/user/cs-project/Aquacast
./start_aquacast.sh --composer
```

2. 필요한 창을 엽니다.

- `Aquacast Tank Controls`
- `Aquacast Sensor Overview`
- `Aquacast Metrics Dashboard`
- `Aquacast Water Quality View`
- `Aquacast Local LLM Panel`

3. `Tank Controls`에서 시연할 탱크를 하나 선택합니다.

4. `Water Quality View`에서 현재 색상 모드를 확인합니다. 색상 막대는 현재 mode의 min-to-max color scale이며, current value와 current color bin을 함께 보여줍니다.

## 시연 흐름 요약

1. `Normal State`로 안전한 기준 상태를 보여줍니다.
2. `Water Too Hot`으로 온도 위험 상태와 색상 변화, threshold 경고를 보여줍니다.
3. `Too Much Feed`로 사료 증가, 탁도, TAN/NH3 위험 흐름을 보여줍니다.
4. `Pump Off`로 water exchange가 멈췄을 때 actuator와 수질 drift를 보여줍니다.
5. `Filter Failure`로 biofilter OFF와 ammonia 상승 위험을 보여줍니다.
6. warn/critical 상태에서 Local LLM proposal이 생성되는지 확인합니다.
7. `Auto Execute OFF` 상태에서는 proposal이 pending으로 남고, 운영자가 `Confirm` 또는 `Reject`를 선택합니다.
8. 적용된 proposal은 `Clear Completed` 또는 개별 `Delete`로 proposal history에서 정리합니다.

## Scenario 1: Normal State

UI 버튼: `Normal State`

내부 scenario: `baseline`

목표는 안전한 baseline 상태를 먼저 보여주는 것입니다.

초기 상태:

| Metric | Value |
| --- | ---: |
| Temperature | 10.5 C |
| Dissolved Oxygen | 9.0 mg/L |
| TAN | 0.3 mg/L |
| CO2 | 5.0 mg/L |
| Alkalinity | 120.0 mg/L as CaCO3 |
| Salinity | 0.2 ppt |
| Turbidity | 2.0 NTU |
| Feed Pool | 0.0 kg |

보여줄 포인트:

- Sensor Overview에서 주요 수질 값이 안정적인지 확인합니다.
- Metrics Dashboard에서 healthy band 중심으로 표시되는지 확인합니다.
- Water Quality View color scale에서 current value가 어느 색상 bin에 있는지 보여줍니다.
- Actuator Overview에서 inlet, outlet, biofilter, mechanical filter 상태를 확인합니다.

## Scenario 2: Water Too Hot

UI 버튼: `Water Too Hot`

내부 scenario: `high_temp_spike`

목표는 온도 위험 상태가 가장 직관적으로 보이도록 만드는 것입니다. 시연에서 가장 먼저 강조하기 좋은 이상 상태입니다.

초기 상태:

| Metric | Value |
| --- | ---: |
| Temperature | 22.0 C |
| Dissolved Oxygen | 7.2 mg/L |
| TAN | 0.35 mg/L |
| CO2 | 6.5 mg/L |
| Alkalinity | 116.0 mg/L as CaCO3 |
| Salinity | 0.2 ppt |
| Turbidity | 3.5 NTU |
| Feed Pool | 0.2 kg |

보여줄 포인트:

- Water Quality View를 `Temp` mode로 둡니다.
- color scale에서 높은 온도 값이 high-end 색상에 가까워지는지 확인합니다.
- Metrics Dashboard에서 temperature가 warn/critical band로 표시되는지 확인합니다.
- Local LLM Panel에서 temperature 관련 auto proposal이 생성되는지 확인합니다.
- `Auto Execute OFF`이면 proposal이 pending으로 남고 `Confirm`/`Reject`가 표시됩니다.

운영 메시지:

- 현재 탱크 온도가 안전 범위를 벗어났고, fish stress와 oxygen risk가 함께 커질 수 있음을 보여줍니다.
- AI는 바로 제어하지 않고 proposal을 만들며, 운영자가 확인 후 적용한다는 흐름을 강조합니다.

## Scenario 3: Too Much Feed

UI 버튼: `Too Much Feed`

내부 scenario: `overfeed`

목표는 사료 과다 투입이 수질 부하로 연결되는 흐름을 보여주는 것입니다.

초기 상태:

| Metric | Value |
| --- | ---: |
| Temperature | 10.5 C |
| Dissolved Oxygen | 8.6 mg/L |
| TAN | 0.45 mg/L |
| CO2 | 7.0 mg/L |
| Alkalinity | 115.0 mg/L as CaCO3 |
| Salinity | 0.2 ppt |
| Turbidity | 8.0 NTU |
| Feed Pool | 2.5 kg |

보여줄 포인트:

- Sensor Overview에서 feed pool, turbidity, TAN/NH3 변화를 봅니다.
- Water Quality View를 `Turb`, `TAN`, `NH3` mode로 바꿔 색상 scale이 mode별로 바뀌는 것을 보여줍니다.
- Metrics Dashboard에서 ammonia-related metric이 정상 상태보다 악화되는지 확인합니다.
- 필요하면 Local LLM proposal에서 사료 중단, water exchange, filtration 관련 제안이 나오는지 확인합니다.

운영 메시지:

- 과다 급이는 즉시 눈에 보이는 이벤트이지만, 실제 위험은 시간이 지나며 ammonia/turbidity/oxygen 쪽으로 나타납니다.
- 초보자에게는 `Too Much Feed` 버튼 하나로 이 연결 관계를 체험하게 하는 것이 핵심입니다.

## Scenario 4: Pump Off

UI 버튼: `Pump Off`

내부 scenario: `pump_off`

목표는 water exchange가 멈추는 actuator failure 상황을 보여주는 것입니다.

초기 상태:

| Metric | Value |
| --- | ---: |
| Temperature | 10.5 C |
| Dissolved Oxygen | 8.4 mg/L |
| TAN | 0.45 mg/L |
| CO2 | 8.0 mg/L |
| Alkalinity | 112.0 mg/L as CaCO3 |
| Salinity | 0.2 ppt |
| Turbidity | 5.0 NTU |
| Feed Pool | 0.2 kg |
| Inflow Enabled | false |
| Flow | 0.0 L/h |

보여줄 포인트:

- Actuator Overview에서 inlet/off 또는 flow 관련 상태가 바뀌는지 확인합니다.
- Tank Controls에서 flow/water exchange 관련 control을 확인합니다.
- Sensor Overview에서 DO, CO2, TAN이 정상 상태 대비 나빠지는 방향을 봅니다.
- Local LLM proposal에서 pump/flow recovery 제안이 나오는지 확인합니다.

운영 메시지:

- pump off는 단순 수치 이상이 아니라 actuator 상태 이상입니다.
- 운영자는 sensor 값과 actuator dot을 함께 봐야 합니다.

## Scenario 5: Filter Failure

UI 버튼: `Filter Failure`

내부 scenario: `biofilter_off`

목표는 biofilter가 꺼졌을 때 ammonia risk가 커지는 상황을 보여주는 것입니다.

초기 상태:

| Metric | Value |
| --- | ---: |
| Temperature | 10.5 C |
| Dissolved Oxygen | 8.8 mg/L |
| TAN | 0.6 mg/L |
| CO2 | 6.5 mg/L |
| Alkalinity | 118.0 mg/L as CaCO3 |
| Salinity | 0.2 ppt |
| Turbidity | 3.0 NTU |
| Feed Pool | 0.2 kg |
| Biofilter | false |

보여줄 포인트:

- Actuator Overview에서 biofilter가 OFF로 바뀌는지 확인합니다.
- Water Quality View를 `TAN` 또는 `NH3` mode로 둡니다.
- Metrics Dashboard에서 ammonia-related state를 확인합니다.
- Local LLM proposal에서 biofilter recovery 또는 water exchange 제안이 나오는지 확인합니다.

운영 메시지:

- filter failure는 즉시 치명적인 값으로 보이지 않아도, 시간이 지나며 TAN/NH3 위험으로 연결됩니다.
- 초보자에게는 `Filter Failure` 버튼과 biofilter status dot을 같이 보여주는 것이 중요합니다.

## AI Proposal 시연 흐름

Local LLM Panel에서 다음 상태를 확인합니다.

- `LLM: ON` 또는 `Check LLM`로 연결 상태 확인
- `Auto Proposal ON`
- `Auto Execute OFF`

권장 흐름:

1. `Water Too Hot`, `Pump Off`, `Filter Failure` 중 하나를 적용합니다.
2. warn/critical 상태가 감지되면 AI proposal이 생성됩니다.
3. `Auto Execute OFF` 상태에서는 proposal이 pending으로 남습니다.
4. 운영자가 proposal summary, evidence, action list를 확인합니다.
5. 적용할 경우 `Confirm`, 적용하지 않을 경우 `Reject`를 누릅니다.
6. 적용 완료된 proposal은 `Delete` 또는 `Clear Completed`로 정리합니다.

강조할 메시지:

- 자동화는 위험 상태를 감지하고 제안까지 수행합니다.
- 실제 actuator action은 운영자 승인 후 적용됩니다.
- 같은 이벤트는 cooldown 동안 중복 생성되지 않지만, warn에서 critical로 올라가면 새 이벤트로 처리됩니다.

## Water Quality View 색상 설명

`Aquacast Water Quality View`는 현재 선택된 mode에 따라 particle color scale을 보여줍니다.

- `Temp`: temperature color scale
- `DO`: dissolved oxygen color scale
- `TAN`: total ammonia nitrogen color scale
- `CO2`: carbon dioxide color scale
- `pH`: pH color scale
- `Alk`: alkalinity color scale
- `NH3`: unionized ammonia color scale
- `Sal`: salinity color scale
- `Turb`: turbidity color scale

색상 막대는 현재 mode의 numeric min-to-max scale입니다. 중간이 항상 안전하다는 뜻은 아닙니다. 특히 pH처럼 너무 낮아도 위험하고 너무 높아도 위험한 metric은 색상과 threshold band를 함께 봐야 합니다.

시연에서는 color scale만 보지 말고 Metrics Dashboard의 healthy/warn/critical band와 같이 설명합니다.

## 권장 시연 순서

짧은 기능 중심 시연:

1. `Normal State`
2. `Water Too Hot`
3. Water Quality View `Temp` color scale 확인
4. Metrics Dashboard temperature alert 확인
5. Local LLM proposal 확인
6. `Confirm` 또는 `Reject`

수질-제어 흐름 전체 시연:

1. `Normal State`
2. `Too Much Feed`
3. `Turb`, `TAN`, `NH3` mode 확인
4. `Pump Off`
5. Actuator Overview에서 inlet/flow 상태 확인
6. `Filter Failure`
7. Biofilter OFF와 ammonia risk 확인
8. Local LLM proposal 확인 및 `Confirm`/`Reject`
9. `Clear Completed`로 proposal history 정리

## 주의사항

- pH보다 temperature, feed, flow, filter 변화를 먼저 보여주는 것이 시각적으로 이해하기 쉽습니다.
- scenario는 선택된 tank에 적용됩니다. 시연 전에 반드시 tank selection을 확인합니다.
- Omniverse UI 텍스트는 영어/ASCII 버튼명을 사용합니다.
