#!/bin/bash
set -euo pipefail

# 기본값 설정
NO_WINDOW=""
KIT_FILE="aquacast.aquacast_streaming.kit"
APP_ROOT="${HOME}/cs-project/kit-app-template"
EXT_FOLDER="${HOME}/cs-project/Aquacast/extensions"
EXT_ID="aquacast.aquacast_composer"

# 인자값(Arguments) 처리
for arg in "$@"
do
    if [ "$arg" == "--streaming" ]; then
        NO_WINDOW="--no-window"
        # streaming 옵션이 명시되면 streaming용 kit 사용 (기본값과 동일)
        KIT_FILE="aquacast.aquacast_streaming.kit"
    fi

    if [ "$arg" == "--composer" ]; then
        # composer 옵션이 들어오면 일반 composer kit 사용
        KIT_FILE="aquacast.aquacast.kit"
    fi
done

# 실행 부분
"${APP_ROOT}/repo.sh" launch "$KIT_FILE" \
         -- --ext-folder "${EXT_FOLDER}" \
         --enable "${EXT_ID}" \
         ${NO_WINDOW:+"${NO_WINDOW}"}
