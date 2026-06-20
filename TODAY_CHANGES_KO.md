# 오늘 작업 요약: Aquacast 운영자 경험 고도화

## 핵심 방향

오늘 작업의 중심은 Aquacast를 단순한 시뮬레이터에서 운영자 친화형 디지털 트윈으로 끌어올리는 것이었습니다. 특히 `초보자 모드`와 `전문가 모드`를 명확히 분리해, 같은 수질 데이터라도 사용자 숙련도에 따라 다르게 보여주고 다르게 설명하도록 구조를 정리했습니다.

초보자는 “무엇이 위험한가”와 “지금 무엇을 눌러 보면 되는가”를 빠르게 이해해야 합니다. 전문가는 “어떤 수치가 어떤 threshold를 넘어섰고, 어떤 근거와 추세로 판단했는가”를 확인해야 합니다. 이번 변경은 이 두 사용 흐름을 하나의 시스템 안에서 자연스럽게 공존시키는 방향으로 진행했습니다.

## 1. 초보자/전문가 운영 모드 도입

`AQUACAST_OPERATOR_LEVEL` 설정을 추가해 운영자 프로파일을 `beginner` 또는 `expert`로 전환할 수 있게 했습니다. 기본값은 `beginner`입니다.

초보자 모드에서는 설명을 쉽게 만들고 핵심 지표를 우선합니다. Local LLM Panel은 어려운 용어보다 운영 의미를 먼저 설명하고, Metrics Dashboard도 모든 기술 지표를 한꺼번에 보여주기보다 수온, 산소, 암모니아, pH, CO2, 탁도처럼 초보자가 바로 이해해야 하는 항목 중심으로 정리합니다.

전문가 모드에서는 threshold band, trend delta, actuator state, RAG/SQLite 근거를 더 적극적으로 사용합니다. 즉, 초보자에게는 “현재 상태와 다음 행동”을, 전문가에게는 “판단 근거와 제어 영향”을 제공하도록 역할을 나누었습니다.

## 2. 경고/위험 상태 자동 감지와 AI 제안 흐름

수질 snapshot과 healthy/warn/critical band를 비교해 warn 또는 critical 상태가 발생하면 자동으로 alert를 만들고, 이를 AI proposal 생성 요청으로 연결했습니다.

이때 단순히 “위험함”이라고 보내지 않고, 어떤 지표가 어떤 값이었고 어떤 threshold 조건 때문에 warn 또는 critical인지 prompt와 evidence에 포함했습니다. 예를 들어 수온이 특정 범위를 넘었을 때, 측정값과 `>12.5 and <=18` 같은 조건이 함께 전달됩니다.

생성된 proposal은 Local LLM Panel에 표시되고, 운영자가 Confirm 또는 Reject를 선택해야 실제 actuator action이 반영됩니다. 자동화는 제안까지만 수행하고, 최종 적용은 사람이 승인하도록 안전 장치를 유지했습니다.

## 3. 같은 이벤트 중복 제안 억제

자동 alert가 너무 자주 반복되면 초보자에게는 불안감을 주고 전문가에게도 노이즈가 됩니다. 이를 막기 위해 같은 이벤트 상태는 60초 동안 중복 proposal이 생성되지 않도록 했습니다.

중복 판단 기준은 tank, metric, band state입니다. 즉, 같은 탱크의 같은 지표가 계속 warn 상태라면 60초 동안 반복 생성하지 않습니다. 반대로 `warn -> critical`처럼 상태가 바뀌면 다른 이벤트로 보아 즉시 다시 proposal을 만들 수 있습니다.

이 구조 덕분에 알림 피로도는 줄이고, 실제 위험도가 상승하는 순간은 놓치지 않도록 만들었습니다.

## 4. 초보자용 원클릭 시나리오 버튼 추가

기존 Tank Controls에는 `baseline`, `overfeed`, `pump_off`, `biofilter_off`처럼 개발자나 전문가에게 익숙한 기술 이름이 노출되어 있었습니다. 오늘 변경에서는 이를 초보자가 이해하기 쉬운 영어 버튼으로 바꾸었습니다.

새 버튼은 다음과 같습니다.

- `Normal State`
- `Too Much Feed`
- `Pump Off`
- `Filter Failure`
- `Water Too Hot`

사용자는 탱크를 선택하고 버튼을 한 번 클릭하면 해당 tank에 scenario가 바로 적용됩니다. 내부적으로는 기존 `load_scenario` action을 그대로 사용하므로 기능 안정성은 유지하면서, 외부 경험만 훨씬 직관적으로 개선했습니다.

## 5. 첫 사용자를 위한 Tutorial Panel 추가

새 사용자가 Aquacast에 들어왔을 때 어디서 시작해야 하는지 알 수 있도록 `Aquacast First Steps` 튜토리얼 패널을 추가했습니다.

튜토리얼은 다음 순서로 시스템 적응을 유도합니다.

- 탱크 선택하기
- 원클릭 시나리오 시작하기
- Sensor Overview와 Metrics Dashboard에서 수질 변화 보기
- Local LLM Panel에서 AI proposal을 확인하고 Confirm/Reject 하기

패널에는 `Open Tank Controls`, `Open Sensors`, `Open Metrics`, `Open Local LLM` 버튼도 넣어 사용자가 주요 창을 바로 열 수 있게 했습니다. Omniverse UI에서 한글 깨짐 가능성이 있어 패널 텍스트는 영어/ASCII로 구현했습니다.

## 6. 기본 수온 10.5C 적용

초기 tank temperature를 기존 `14.0C` 중심에서 salmon/RAS 기준에 맞춘 `10.5C`로 변경했습니다. 시나리오 초기값, 모델 기본값, thermal fallback, backend particle fallback, UI control default를 함께 맞춰 초기 상태가 일관되도록 정리했습니다.

수온 healthy band도 `11.5-12.5C` 기준으로 정리해, 낮은 시작 온도에서의 운영 흐름과 경고 판단이 더 명확해졌습니다.

## 7. 구조적 개선 포인트

이번 변경은 단순히 버튼과 문구를 바꾼 것이 아니라, 운영 경험을 계층화한 작업입니다.

- 초보자에게는 쉬운 버튼, 쉬운 설명, 핵심 지표, 튜토리얼을 제공합니다.
- 전문가에게는 전체 지표, threshold 근거, trend, actuator 상태, RAG/SQLite evidence를 제공합니다.
- 자동화는 proposal 생성까지 수행하지만, 실제 제어는 Confirm을 통해 사람이 승인합니다.
- 중복 alert는 억제하지만 severity 상승은 놓치지 않습니다.
- UI는 영어/ASCII로 유지해 Omniverse 렌더링 안정성을 확보했습니다.

## 검증 결과

변경 후 plain Python으로 검증 가능한 Aquacast 테스트를 실행했습니다.

```bash
python3 -m py_compile extensions/aquacast.aquacast_composer_extensions/aquacast/aquacast_composer_extensions/extension.py extensions/aquacast.aquacast_composer_extensions/global_variable.py
python3 -m pytest extensions/aquacast.aquacast_composer_extensions/tests -q
```

결과는 `63 passed`로 통과했습니다.

## 한 줄 요약

오늘의 Aquacast는 전문가만 이해하는 시뮬레이션 도구에서, 초보자도 버튼 한 번으로 상태를 체험하고 AI 도움을 받아 판단할 수 있는 운영자 중심 디지털 트윈으로 한 단계 발전했습니다.
