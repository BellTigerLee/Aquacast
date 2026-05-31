# Aquacast 연산 최적화 Spec (Extension + Backend)

> 목적: extension(Kit)과 backend의 **캐싱 / 연산 / 갱신** 핫스팟을 증거 기반으로 정리하고,
> 각 항목별 솔루션과 단계별 실행 계획을 정의한다.
> 작성 기준일: 2026-05-31. 모든 수치는 실제 코드 읽기/측정으로 검증함(추정치는 그렇게 표기).

---

## 0. 범위 & 목표

- **대상**
  - Extension: `Aquacast/extensions/aquacast.aquacast_composer_extensions/` (`main.py` 3231줄 + dynamics/model 모듈)
  - Backend: `Aquacast/backend/water_quality_backend.py` (HTTP 서버) + `water_quality_backend_client.py`
- **목표**
  1. 프레임 루프(매 프레임/매 객체)에서 반복되는 불필요 연산 제거 → 안정적 60fps
  2. 메인 스레드 블로킹(HTTP) 제거 → 프레임 스톨 0
  3. 시뮬레이션/입자 갱신을 렌더 프레임레이트와 분리(throttle/배치)
- **비목표**: 시뮬레이션 수식·정확도 변경, 렌더링 품질 변경, 신규 기능. (순수 성능/구조 리팩터링)

## 1. 검증 방법

- `get_global_config` 비용: 2000회 반복 측정 → **641µs/호출** (디스크 read + 모듈 전체 재실행)
- `stage_topology.json` 로딩: `json.load` 측정 → **6.3ms** (2.75MB, 5406노드) → **병목 아님**
- 입자/HTTP 경로: 해당 줄 직접 정독으로 확인(아래 file:line 인용)
- ⚠️ 사전 조사 에이전트의 일부 수치(입자 2000개·800ms 등)는 과장으로 판명 → 본 spec은 정정된 값만 사용

---

## 2. 검증된 핫스팟 (심각도 순)

### H1 — `get_global_config()`가 매 호출마다 설정 파일을 디스크에서 재실행 〔CRITICAL〕

- **위치**: `main.py` 내 `get_global_config` 정의(약 70줄대) + 호출 **125곳**, 그중 매 프레임 루프 내부 다수
- **현상**: 호출할 때마다 `importlib.util.spec_from_file_location` → `exec_module`로
  `global_variable.py` 전체(색상 테이블·임계값 dict 포함)를 **디스크에서 다시 읽고 재실행**
- **측정**: **641µs/호출**
- **빈도(검증)**:
  - 물고기 `_on_update`(`main.py:1140~`): 본문 11회 + `_desired_direction` 9회 + `_wander_vector`/`_boundary_steering` 등 헬퍼 추가 → **물고기 1마리당 ~12회**
  - 수온 `_on_update`(`main.py:2129~`): 9회/프레임
  - 수질 `_on_update`(`main.py:2426~`): 7회/프레임
- **영향**:
  - 물고기 없이도 ~27회/프레임 × 641µs ≈ **17ms/프레임** → config 읽기만으로 60fps 천장 초과
  - 물고기 N마리면 ~12N회 추가 (예: 30마리 → +360회 × 641µs ≈ +230ms/프레임)
- **근본 원인**: 핫리로드(런타임 값 수정) 편의를 위해 캐시를 의도적으로 끔. 하지만 캐시 무효화를 mtime으로 하면 편의 유지 가능.

### H2 — 입자 온도/색을 per-particle 파이썬 루프로 계산 〔HIGH〕

- **위치**: `main.py:2449-2453` (온도/색 루프), 색 함수 `thermal_dynamics.py:272 temperature_to_rgb`
- **현상**:
  ```python
  for weight in self._particle_heat_weights:           # 1000개(TEMP_PARTICLE_COUNT)
      temp = self._T + heat_delta * weight * spread
      temperatures.append(float(temp))                 # list.append
      colors.append(Gf.Vec3f(*thermal_dynamics.temperature_to_rgb(temp, stops)))
  ```
  - `temperature_to_rgb`는 **호출마다 `sorted(stops)` 수행** + 선형 스캔 (순수 파이썬)
  - 입자마다 `Gf.Vec3f` 객체 생성 + list.append
- **영향(정정)**: 1000개 × ~3-5µs ≈ **3-5ms/업데이트**(0.12s마다). 에이전트 주장 800ms는 오류.
- **낭비 포인트**: ① 매 입자 `sorted()` ② numpy 미사용 ③ palette/온도-색 LUT 미캐시

### H3 — 입자 색을 prim 1개씩 개별 USD write 〔HIGH, 조건부〕

- **위치**: `main.py:2462-2464`, `2474-2476`, `3062-3064`
  ```python
  for attr, color in zip(sphere_color_attrs, colors):
      attr.Set(Vt.Vec3fArray([color]))   # 입자 1개당 USD attr.Set 1회
  ```
- **현상**: 입자를 개별 `Sphere` prim으로 author한 경로일 때, 1000개면 **USD write 1000회/업데이트**.
  바로 옆 대체 경로(`self._particle_color_attr.Set(Vt.Vec3fArray(colors))`)는 **단일 배치 write** — 이미 빠른 길이 존재.
- **영향(추정)**: USD attr.Set 1회당 오버헤드(수~수십µs) × 1000 → **수~수십 ms/업데이트**. Sphere 경로가 활성일 때만.
- **조건**: 현재 활성 입자 표현이 개별 Sphere인지 단일 Points/PointInstancer인지에 따라 영향 상이 → 구현 전 확인 필요.

### H4 — 수질 backend `advance()`를 메인 스레드에서 동기 블로킹 HTTP 호출 〔HIGH, 구조적〕

- **위치**: `main.py:2812`(`self._model = client`), `main.py:2893`(`state = self._model.advance(dt)`),
  클라이언트 `water_quality_backend_client.py:_post`(동기 `urlopen(request, timeout=self.timeout_s)`)
- **현상**: 수질 `_on_update`(app update event stream = **메인 스레드**)에서 매 `WQ_UPDATE_INTERVAL_SECONDS`(0.12~0.5s)마다
  **동기 HTTP POST**. 비동기/executor/스레드 없음.
- **영향**:
  - 정상: 왕복 지연만큼 메인 스레드 정지(수~수십 ms)
  - 백엔드 지연/다운: **`timeout_s`(=0.25s)만큼 풀 스톨 = 약 15프레임 끊김**, 매 주기 반복
  - `_ensure_particles_registered` / `_write_particle_primvars` 경로도 추가 HTTP 발생 가능
- **부가**: 클라이언트에 재시도/백오프 없음, connect 타임아웃 별도 미설정

### H5 — 물고기 flock 입력 numpy 배열을 매 프레임 재구성 〔MEDIUM〕

- **위치**: `main.py:1163-1172` (positions/directions를 dict 리스트 컴프리헨션 → `np.asarray`)
- **현상**: 매 프레임 `self._fish`(dict 리스트)에서 위치/방향을 파이썬 리스트로 펼친 뒤 `np.asarray`로 재할당
- **영향(추정)**: N마리 × 소량(수십µs). 작지만 매 프레임 회피 가능. SoA(numpy) 단일 소스로 유지하면 제거 가능.

### H6 — Backend 측 경미 항목 〔LOW〕

- **JSON 재로드**: `water_quality_backend.py:52` `reset()`마다 `load_model`이 `wq_constants/feed_rate/scenarios.json` 3개 재read (`water_quality_model.py:609~`). reset은 드물어 영향 작음. 시작 시 1회 파싱 후 dict 보관 권장.
- **RK4 중복 계산**: `thermal_dynamics.step_temperature_rk4`에서 `tank_geometry()`가 스텝당 4회(상수인데 매번). <1µs, 무시 가능하나 정리 가능.
- **이중 계산**: `water_quality_model.py` `as_dict()`와 `sensor_reading()`이 `nh3_fraction`/`ph_from_carbonate` 중복 호출. on-demand라 영향 미미.

### 비병목(오해 차단)

- **`stage_topology.json`**: 로딩 6.3ms, 매 프레임 미사용, mtime 캐시 이미 적용 → **느림의 원인 아님**.
  단, 내부 `root_layer`(ECO_AQUACULTURE…)와 실제 auto-open(`Fishtank_test.usd`) **불일치 = stale**.
  init 시 topology 매칭 실패 → full `stage.Traverse()` 폴백 유발 가능(정확성/init 비용 이슈, 프레임 병목 아님).

---

## 3. 솔루션 설계

### S1 — 설정 모듈 mtime 캐싱 (H1 해결, 최우선)

`global_variable.py`를 한 번만 로드하고 파일 변경 시에만 재로드. 호출부 125곳은 무수정.

```python
_config_cache = {"mtime_ns": None, "module": None}

def get_global_config(name, default=None):
    config_path = Path(__file__).with_name("global_variable.py")
    try:
        mtime_ns = config_path.stat().st_mtime_ns
    except OSError:
        return default
    if _config_cache["mtime_ns"] != mtime_ns:
        spec = importlib.util.spec_from_file_location("aquacast_global_variable", config_path)
        if spec is None or spec.loader is None:
            return default
        module = importlib.util.module_from_spec(spec)
        old = sys.dont_write_bytecode; sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = old
        _config_cache.update({"mtime_ns": mtime_ns, "module": module})
    return getattr(_config_cache["module"], name, default)
```

- 효과: 641µs → 캐시 히트 시 `stat()` 1회(~µs↓) + dict 조회. 핫리로드 편의 유지.
- **추가(권장)**: 프레임당 `stat()` 수십 회도 아까우면, `_on_update` 진입 시 1회만 검사하도록 컨트롤러별 "프레임 config 스냅샷"을 만들고 헬퍼에 dict로 전달(아래 S2와 결합).

### S2 — 핫루프 config 스냅샷 패턴 (H1 보강)

각 `_on_update` 시작에서 필요한 파라미터를 **한 번만** 로컬 dict로 읽고, `_desired_direction`/`_wander_vector`/`_boundary_steering`에 인자로 전달. 함수 시그니처에 `cfg` 추가.
- 효과: 물고기 마리당 ~12회 → 프레임당 1세트. S1만으로도 충분히 빠르지만, 이 패턴은 `stat()` 호출 빈도까지 제거.

### S3 — 입자 온도/색 벡터화 + LUT 캐시 (H2 해결)

```python
weights = self._heat_weights_np            # init에서 1회 np.asarray로 캐시
temps = self._T + heat_delta * weights * spread          # 벡터 연산
# 색: 정렬·LUT를 stops 변경 시에만 1회 구성 후 np.interp로 일괄 매핑
idx = np.interp(temps, self._lut_temps, np.arange(len(self._lut)))   # 또는 직접 채널별 interp
colors_np = self._lut_rgb[np.clip(idx.astype(int), 0, len(self._lut)-1)]
```
- `temperature_to_rgb`의 per-call `sorted` 제거: 정렬된 stops와 LUT를 stops identity 기준 캐시.
- `temperatures`/`colors`를 numpy로 만들고 USD엔 `Vt.FloatArray`/`Vt.Vec3fArray`로 **한 번에** 전달.
- 효과: 3-5ms → 수백 µs 수준.

### S4 — 입자 색 단일 배치 write (H3 해결)

- 개별 `Sphere` prim 경로를 지양하고 **단일 `UsdGeom.Points` 또는 `PointInstancer` + primvar 배열** 경로로 통일.
- 부득이 Sphere 유지 시에도, 변경분만 write(이전 색과 동일하면 skip)하거나 `Sdf.ChangeBlock`으로 묶기.
- 효과: USD write 1000회 → 1회.

### S5 — Backend 호출 비동기/스레드 분리 (H4 해결, 구조적)

- **원칙**: 시뮬레이션 갱신 cadence를 렌더 프레임과 분리. 메인 스레드는 **마지막 스냅샷만 읽음**.
- 구현 옵션(택1):
  - (a) **백그라운드 워커 스레드**: `advance` 호출을 전용 스레드에서 주기 실행, 결과를 `self._latest_state`(lock 보호)에 저장. `_on_update`는 논블로킹으로 그 값만 읽어 USD 반영.
  - (b) **asyncio executor**: `await loop.run_in_executor(None, client.advance, dt)` 로 메인 루프 비블로킹.
- 타임아웃/실패: 짧은 connect timeout + 실패 시 직전 스냅샷 유지 + 백오프 재시도. 절대 메인 스레드 정지 금지.
- 입자 register/values POST도 동일 워커로 이동.
- 효과: 프레임 스톨 0. 백엔드 다운에도 렌더 영향 없음.

### S6 — 물고기 상태 SoA 유지 (H5 해결)

- 위치/방향을 `self._pos`(N×3), `self._dir`(N×3) numpy로 보관, 매 프레임 in-place 갱신.
- dict↔배열 변환 제거. USD 반영 시에만 행 단위 읽기.

### S7 — Backend 상수 시작 시 1회 파싱 (H6)

- `WaterQualityBackend.__init__`에서 3개 JSON을 dict로 1회 로드, `load_model(...)`에 경로 대신 dict 전달.
- `reset()`은 캐시된 dict로 모델만 재생성.

---

## 4. 실행 계획 (단계 + 검증 기준)

> 각 단계는 독립 커밋. "검증" 통과 못 하면 다음 단계 진행 금지.

1. **S1: config mtime 캐싱**
   → 검증: `get_global_config` 미세벤치 641µs → <5µs(캐시 히트). 기존 테스트 통과. 런타임 값 수정 시 반영되는지 수동 확인.
2. **S7: backend JSON 1회 파싱**
   → 검증: `smoke_test.py` 통과. reset 시 디스크 read 미발생(로그/strace 또는 코드 확인).
3. **S5: backend 호출 스레드 분리** (가장 체감 큰 구조 변경)
   → 검증: 백엔드 강제 종료 상태에서 프레임레이트 유지(스톨 없음). 백엔드 복귀 시 값 갱신 재개.
4. **S3 + S4: 입자 벡터화 + 배치 write**
   → 검증: 입자 업데이트 1틱 시간 측정 전/후 비교(≥5×↓). 시각적 색 결과 동일.
5. **S2: 핫루프 config 스냅샷**
   → 검증: 물고기 N=30/60 프레임타임 측정. 시각적 거동 동일.
6. **S6: 물고기 SoA**
   → 검증: flock 거동 회귀 없음(테스트 `tests/test_fish_dynamics.py`), 프레임타임 추가 개선.

각 단계 측정은 동일 씬(`Fishtank_test.usd`)·동일 입자수·동일 물고기수로 before/after 기록.

## 5. 리스크 & 주의

- **S5(스레드)**: USD/Kit API는 스레드 안전 보장이 제한적 → **워커는 순수 계산/HTTP만**, USD write는 메인 스레드 `_on_update`에서 스냅샷 읽어 수행. 공유 상태는 lock.
- **S1**: 캐시가 단일 전역이라 다중 config 경로(테스트)에서 path별 캐시 필요할 수 있음 → key를 `(path, mtime)`로.
- **S3/S4**: 입자 표현(Sphere vs Points/Instancer) 실제 활성 경로 먼저 확인 후 적용.
- **stale topology JSON**: 별도 작업으로 현재 auto-open 스테이지 기준 재생성하거나, init 폴백 경로 점검(프레임 병목 아님, 정확성 이슈).

## 6. 기대 효과 요약

| 항목 | 변경 전 | 변경 후(목표) |
|---|---|---|
| H1 config 읽기 | 641µs × 수십~수백/프레임 | 캐시 히트 ~µs |
| H2 입자 색/온도 | 3-5ms/업데이트(파이썬 루프) | 수백µs(벡터화) |
| H3 입자 USD write | 최대 1000회/업데이트 | 1회(배치) |
| H4 backend 호출 | 메인 스레드 최대 250ms 스톨 | 스톨 0(워커) |
| H5 flock 입력 | 매 프레임 배열 재구성 | in-place SoA |

> 우선순위: **S1 → S5 → S3/S4 → S2 → S6 → S7**. S1과 S5가 체감 효과의 대부분.
</content>
</invoke>


---

## 7. 2026-05-31 적용 및 전후 검증 기록

### 적용 범위

이번 작업에서 실제 코드에 반영한 항목은 다음 2개다.

1. **S1 / H1: `get_global_config()` mtime 캐싱**
   - 변경 파일: `extensions/aquacast.aquacast_composer_extensions/main.py`
   - 기존: 호출마다 `global_variable.py`를 `spec_from_file_location` + `exec_module`로 재실행.
   - 변경: `global_variable.py`의 `stat().st_mtime_ns`가 바뀔 때만 재실행하고, 캐시 히트 시 기존 모듈 객체에서 값을 조회.
   - 핫리로드 요구사항은 유지된다. 파일 mtime이 바뀌면 다음 호출에서 모듈을 다시 로드한다.

2. **S3 일부 / H2: 온도 입자 갱신 CPU 경로 벡터화 및 중복 계산 제거**
   - 변경 파일: `extensions/aquacast.aquacast_composer_extensions/main.py`
   - 기존: 입자마다 온도 계산, `temperature_to_rgb()` 호출, 색상 stop 정렬 반복, 프로토타입 인덱스 색상 거리 재계산.
   - 변경: 입자 heat weight를 numpy 배열로 캐시하고, 색상 stop 배열을 캐시한 뒤 `np.interp`로 색상을 일괄 보간한다.
   - PointInstancer 프로토타입 인덱스는 색상 거리 검색 대신 온도 ramp 위치에서 직접 계산한다.
   - 기본 설정(`TEMP_PARTICLE_AUTHORING_MODE = "point_instancer"`)에서 적용된다. `sphere_prims` 디버그 모드의 per-sphere USD write는 아직 남아 있다.

### 전후 마이크로벤치마크

측정 조건:
- 날짜: 2026-05-31
- 실행 위치: `/home/netai-sys/cs-project/Aquacast`
- Python: `./.venv/bin/python`
- 입력 설정: 실제 `extensions/aquacast.aquacast_composer_extensions/global_variable.py`
- 입자 수: `TEMP_PARTICLE_COUNT = 1000`
- 입자 벤치마크는 Kit/USD write 시간을 제외하고, 기존 CPU 계산 경로와 새 CPU 계산 경로를 같은 입력으로 비교했다.

결과:

| 항목 | 개선 전 | 개선 후 | 개선 폭 | 동등성 확인 |
|---|---:|---:|---:|---|
| `get_global_config()` 캐시 히트 | 760.185 us/call | 1.491 us/call | 509.8x faster | 동일 config 파일 사용, mtime 캐시 |
| 온도 입자 CPU 갱신 | 2357.610 us/update | 959.800 us/update | 2.5x faster | `max_temp_diff=0`, `max_color_diff=1.11022302463e-16`, `proto_index_match_pct=100.00` |

벤치마크 원 출력:

```text
config_old_us_per_call=760.185
config_new_us_per_call=1.491
config_speedup=509.8x
particles_count=1000
particles_old_us_per_update=2357.610
particles_new_us_per_update=959.800
particles_speedup=2.5x
max_temp_diff=0
max_color_diff=1.11022302463e-16
proto_index_match_pct=100.00
```

해석:
- H1은 문서의 기존 측정값 641 us/call과 같은 급의 병목이 재현됐고, 캐시 적용 후 1.491 us/call로 내려갔다. 프레임당 수십-수백 회 호출되는 경로라 실제 프레임타임 개선 가능성이 높다.
- H2는 최초 추정처럼 5x 이상은 아니었지만, PointInstancer 기본 경로에서 CPU 계산만 2.5x 개선됐다. 온도와 색상 결과는 부동소수 오차 범위에서 동일했고, 프로토타입 인덱스도 100% 일치했다.
- USD `attr.Set()` 자체 시간과 Kit 렌더 프레임타임은 이 벤치마크에 포함되지 않았다. 따라서 런타임 FPS 개선은 Kit 실행 상태에서 별도 확인이 필요하다.

### 회귀 검증

실행한 검증:

```bash
python3 -m py_compile extensions/aquacast.aquacast_composer_extensions/main.py
./.venv/bin/python -m pytest extensions/aquacast.aquacast_composer_extensions/tests -q
```

결과:

```text
python3 -m py_compile ...: passed
45 passed in 0.99s
```

### 남은 항목

아직 구현하지 않은 항목:
- S5: water quality backend 호출의 메인 스레드 분리.
- S2: fish/update 핫루프 config 스냅샷 전달.
- S4: `sphere_prims` 디버그 모드의 per-sphere USD write 제거 또는 change block/skip 최적화.
- S6: fish 상태 SoA 전환.
- S7: backend JSON 시작 시 1회 파싱.

따라서 이번 변경은 전체 spec 완료가 아니라, 검증 가능한 1차 성능 개선 적용이다.
