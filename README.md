# Muse Platform

Local Windows WebUI for Meta Work, fixed-model Baseten Chat, Modal GPU execution, collaborative Goal agents and shared ontology memory.

## 설치 및 실행

Python 3.11 이상과 일반 사용자 PowerShell을 사용합니다. API 키는 채팅·Git·프로젝트 파일에 넣지 않습니다.

```powershell
git clone https://github.com/pixaxe0826/llm-ontology-ochestration.git
cd llm-ontology-ochestration
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\set_keys.ps1
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\set_baseten_key.ps1
```

필요한 서비스의 키만 사용자 환경 변수로 설정하고 터미널을 다시 연 뒤 실행합니다. Modal SDK 인증과 공유 추론 엔드포인트 Proxy Token은 별개입니다. GPU 기능을 쓰려면 `.\.venv\Scripts\modal.exe setup`으로 로그인합니다.

```powershell
.\start_platform.cmd
```

브라우저 주소는 `http://127.0.0.1:8765`입니다. 키·대화·첨부·장부·작업 결과는 이 저장소에 포함하지 않습니다. 실행 시 생기는 `.workbench/`는 Git에서 제외됩니다. 종료 시에는 활성 Goal과 GPU·장시간 명령을 확인하세요.

## 문서와 검증

- [Goal 모드](GOAL_MODE.md), [알림](NOTIFICATIONS.md), [WebUI 안내](WEBUI_README.md)
- Baseten 모델은 `meta-models/muse-spark-open-1026`으로 고정됩니다.
- 완료·superseded 항목은 계획 슬롯을 쓰지 않으며, superseded는 수용 완료와 구분됩니다.
- GPU 결과 회수 근거는 제출한 입력 파일의 현재 해시도 검사합니다.
- 2026-11-03 오프라인 회귀 검사: Python 테스트 실행 788회와 JavaScript 6개 스위트 PASS. 외부 모델/GPU 요청을 차단한 검사입니다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
node tests/test_markdown_export.cjs
node tests/test_goal_decision_view.cjs
node tests/test_chat_attachments.cjs
node tests/test_goal_executions.cjs
node tests/test_ui_performance.cjs
node tests/test_model_routing_ui.cjs
```

## Goal 역할별 공급자와 사람 승인

Goal 생성·설정에서 Planner/Reviewer, Builder, 이미지 분석, 참고 검색의 공급자·모델을 각각 선택합니다. 개별 계획 항목의 **Builder 경로**에서 그 항목만 별도로 지정할 수도 있습니다. 실행 중 변경은 다음 묶음 경계부터 적용됩니다. 기존 Goal의 경로·기록과 제품 프록시의 독립 설정은 유지합니다.

연결 설정에서 OpenAI 호환 공급자의 주소·모델·기능·가격을 등록합니다. 키는 WebUI에 입력하지 않고 다음 보안 입력 스크립트를 사용합니다. OpenAI 템플릿의 공급자 ID는 `openai`입니다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\set_provider_key.ps1 -Provider openai
```

시각 승인 기준에는 `human_approval`과 `approval_paths`를 사용합니다. 해당 파일 해시에 대한 명시적인 사람 승인이 있어야 하며, 파일이 바뀌면 다시 승인받아야 합니다. 이 기준은 새로운 명령이나 GPU 실행을 요구하지 않습니다. 실행 기준과 문서 기준의 근거 규칙은 그대로 유지됩니다.

실제 소액 점검: Meta Planner + Baseten Builder, Baseten Planner + Baseten Builder 두 임시 Goal 및 OpenAI 연결 PASS. 초기 검토 실패와 수정 후 기존 근거 재검토까지 포함한 추정 비용은 $0.1494입니다. Modal LLM 실제 역할 호출은 NOT RUN이며 등록 모델과 기능을 먼저 확인해야 합니다. [설정·보안·API 상세](GOAL_MODE.md)를 참고하세요.

이 커밋은 개발 소스 공개입니다. 기존 운영 PC의 서버나 Goal을 자동으로 배포·재시작하지 않습니다.

## 상세 사용법


## 실행

`<프로젝트 폴더>\start_platform.cmd`를 더블클릭합니다.
브라우저에서 `http://127.0.0.1:8765`가 열립니다. 서버는 이 PC의 loopback 주소에만 바인딩합니다.
이전에 설정한 사용자 환경 변수를 시작 시 읽으므로 키를 다시 입력할 필요가 없습니다.
별도 관리자 권한이나 추가 웹 프레임워크 설치는 필요하지 않습니다. 기존 `openai`, `modal` 패키지를 사용합니다.

터미널에서 실행하려면:

```powershell
Set-Location "<프로젝트 폴더>"
.\.venv\Scripts\python.exe -m workbench.server
```

콘솔을 닫거나 Ctrl+C로 종료할 수 있습니다. 종료 시 실행 중인 작업 취소를 요청합니다.
설정의 **프로그램 종료**로 서버와 실행 중인 작업을 중지할 수 있습니다. 브라우저 탭만 닫아서는 서버나 GPU 작업이 종료되지 않습니다. 이미 실행 중이면 시작 파일은 기존 서비스를 엽니다.

## 세 서비스와 공통 메모리

왼쪽 서비스 메뉴에서 독립적으로 전환합니다.

- **Meta Work**: 폴더 지정, 모델 선택, 파일 읽기·편집·로컬 테스트, AI GPU 계획과 실행.
- **Baseten Chat**: 폴더나 Modal 없이 대화. API 모델은 `meta-models/muse-spark-open-1026`으로 서버에서 고정하며 UI·설정·변조 요청으로 바꿀 수 없습니다.
- **Modal Compute**: Python / PyTorch / Blender / custom 프로그램을 GPU 직접 선택 또는 자동 선택으로 실행. **AI로 작업 준비**는 Meta Work로 이어집니다.
- **공유 메모리**: 세 서비스의 원문·파일 변경·실제 실행 결과를 한 온톨로지에서 연결합니다.

`기억 참고`는 다른 기록의 인용을 현재 모델에 전달할지, `메모리 공유`는 현재 대화를 이후 검색에 포함할지 정합니다.
공유 메모리 화면에서 개념·별칭·연결·출처를 탐색하고, 기록별 공유 제외와 수동 기억 추가를 할 수 있습니다.
이전 Muse Spark Chat의 `data` 폴더를 지정하면 Markdown 대화와 개념·별칭만 가져옵니다. 기존 파일을 삭제하거나 암호화 키 파일을 읽지 않습니다.

온톨로지는 사용자가 제공한 ZIP의 `knowledge.py`를 재사용합니다. 원문 Markdown이 근거이고 개념의 동시 등장은 탐색 연결입니다.
다른 기록 최대 5개 / 발췌 합계 6000자까지 인용합니다. 실패·중단된 답변과 공유 제외 기록은 검색하지 않습니다.
공통 메모리는 `.workbench/memory/`에 저장되며 서비스별로 따로 복제된 기억이 아닙니다.

## Meta 폴더 작업

1. **폴더 찾기** 또는 절대 경로 입력으로 프로젝트 폴더를 엽니다.
2. 공급자와 모델을 선택합니다. Meta 기본 모델은 `muse-spark-1.3`입니다.
3. 요청을 입력합니다. 예: `이 폴더 코드를 읽고 CSV 처리 오류를 수정한 다음 관련 테스트를 실행해 줘.`
4. **파일 수정 허용**을 끄면 에이전트는 파일을 수정할 수 없습니다.
5. 모델은 파일 목록·읽기·검색·수정 도구를 호출합니다. 기존 파일은 읽은 sha256과 일치할 때만 수정하고 백업을 남깁니다.
6. 로컬 명령은 실행 파일·인수·작업 경로를 확인한 뒤 승인합니다. 결과를 받은 모델이 작업을 계속합니다.
7. **변경** 탭에서 diff를 확인합니다. 이후 파일이 바뀌지 않았다면 수정 직전 백업으로 복구할 수 있습니다.

모델 API에 선택 폴더의 읽은 내용이 전달될 수 있습니다. 폴더 전체를 무조건 전송하지는 않습니다.
파일·로그 내용은 모델에 제공되는 자료이며, 별도의 사용자 승인으로 취급하지 않습니다.

## GPU 직접 실행

**GPU 작업 → 새 작업**에서 다음을 지정합니다.

- 프로그램: Python / PyTorch / Blender / custom 공개 컨테이너.
- GPU: 자동 또는 지정한 GPU. 자동은 Python에 T4, PyTorch·Blender에 L4부터 시작하고 최소 VRAM에 따라 올립니다. 실시간 가격·가용성을 보장하는 추천은 아닙니다.
- 진입 파일: 폴더 기준 `.py` 또는 `.blend` 상대 경로.
- 전송 파일·폴더: 필요한 항목만 한 줄에 하나. 진입 파일을 포함해야 합니다.
- 인수: JSON 배열. 예: `["--epochs", "10"]`.
- 패키지: PyPI 이름과 버전. PyTorch 프로그램은 `torch`를 자동 추가합니다.
- 시간: 기본 600초, 최대 21600초. 자동 재시도는 0회입니다.
- 결과 Volume: 기본 `muse-workbench-results`. GPU뿐 아니라 Volume 저장 요금도 발생합니다. 빈칸이면 영구 보관하지 않고 합계 32 MB까지만 즉시 반환합니다.

전송 전 파일 목록·해시·총 크기를 확인합니다. 확인 이후 파일이 바뀌면 재승인이 필요합니다.
전송 상한은 합계 100 MB / 2000파일입니다. 큰 데이터셋은 프로그램에서 승인된 저장소를 읽도록 별도로 준비해야 합니다.
로컬 키는 원격 프로그램에 주입하지 않습니다. 등록한 `meta-api` Secret도 일반 GPU 코드에 자동으로 주입하지 않습니다.

클라우드 프로그램 결과는 **반드시 `os.environ["MUSE_OUTPUT_DIR"]` 아래**에 저장하세요.
하드웨어 기록·로그와 작은 결과를 즉시 반환합니다. Volume 사용 시 큰 체크포인트도 보관하며 WebUI 다운로드는 스트리밍합니다.
필요 없는 결과는 Modal 대시보드에서 삭제해야 저장 요금이 계속 쌓이지 않습니다.

Blender 프리셋은 공식 Blender 4.5.3 Linux 바이너리와 Cycles CUDA로 `.blend`의 지정 프레임을 PNG 렌더링합니다.
다른 렌더러는 공개 컨테이너 이름과 실행 명령 배열을 custom 모드에 지정합니다. 라이선스 및 해당 컨테이너의 Linux GPU 지원은 별도 확인 대상입니다.

## 자연어 → 로컬 수정 → GPU → 결과 분석

예시 요청:

> 이 폴더의 train.py와 설정을 읽고 실행 오류를 고쳐 줘. 24GB 이상 GPU를 선택하고 requirements의 필요한 패키지만 사용해 10 epoch 학습을 준비해 줘. 입력 전송 목록과 실행 내용을 보여 준 다음 승인 후 실행하고, 결과와 하드웨어 기록을 분석해 줘.

모델이 로컬 파일을 준비한 뒤 GPU 작업을 제안합니다. 전송·GPU·시간·Volume을 승인하면 실행하고, 완료 결과를 받은 모델이 후속 분석을 이어갑니다.
큰 학습·렌더링은 사용자가 실제 입력·비용·라이선스를 확인한 뒤 실행해야 합니다.

## Baseten

Baseten은 제공된 ZIP의 정확한 API ID `meta-models/muse-spark-open-1026`만 사용합니다. UI에는 Muse Spark 1026 고정으로 표시됩니다.
키는 다음 스크립트에서만 직접 입력합니다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "<프로젝트 폴더>\scripts\set_baseten_key.ps1"
```

WebUI의 **연결 및 모델 설정 → 키 상태 새로 고침** 후 Baseten을 선택합니다.
주소는 `https://inference.baseten.co/v1`으로 고정됩니다. 다른 모델·주소로의 변경 요청은 서버에서 거부합니다. Meta의 작업 모델은 상단 모델 선택 메뉴에서 선택합니다.
키 값은 브라우저에 전달되지 않습니다. 실제 계정의 모델 접근 권한과 tool calling 지원은 연결 후 확인됩니다.

## 로컬 실행 권한과 한계

- 파일 도구는 선택 폴더 내부 상대 경로만 허용합니다. symlink/junction, `.env`, 키 파일, `.git`, `.venv` 등은 제외합니다.
- **로컬 명령은 OS 샌드박스가 아닙니다.** 승인한 명령은 현재 Windows 사용자 권한으로 실행되며 코드 자체가 폴더 밖 파일·네트워크에 접근할 수 있습니다. 신뢰할 수 있는 프로젝트·명령만 승인하세요. 공급자 키는 자식 명령의 환경 변수에서 제거합니다.
- 브라우저 요청은 loopback Host·Origin·세션 토큰을 검사합니다. LAN 바인딩, 원격 공개, CORS 개방을 하지 않습니다.
- 작업 기록·백업·GPU 결과는 `.workbench/`에 저장되고 Git에서 제외합니다. 현재 알려진 키 문자열은 저장·표시 전에 제거합니다. 사용자가 새 비밀값을 프롬프트에 입력하지 않아야 합니다.
- 모델 호출은 한 단계당 최대 4096 출력 토큰, 작업은 기본 16단계입니다. 실패를 자동 재시도하지 않습니다.
- 중지 시 원격 FunctionCall 취소와 컨테이너 종료를 요청합니다. 서버 재시작으로 중단된 GPU 작업은 작업 목록에서 상태를 확인하고 중지하세요.

## 하드웨어 기록

GPU 작업은 `hardware-telemetry.csv`, `hardware-summary.json`, `hardware-report.md`, `run.log`를 남깁니다.
NVIDIA 사용률·VRAM·온도·전력·클록, 가능한 procfs host 수치를 기본 2초 간격으로 기록합니다.
같은 Python 프로세스에서 초기화된 PyTorch CUDA allocator의 allocated/reserved/peak/free를 별도로 수집합니다.
외부 프로세스 trainer의 PyTorch allocator와 Linux에서의 Windows WDDM 지표는 NA입니다.

학습 코드에서 다음 훅을 사용할 수 있습니다.

```python
from muse_telemetry import phase
phase(representation="my-input", objective="classification", seed=7,
      fold=0, epoch=1, step=100, group_id="train", batch_size=32)
```

fold 전환 시 관찰값을 남기고 allocator peak를 초기화합니다. 데이터가 부족한 누적·압력·성능 판정은 NOT VERIFIED로 남깁니다.
모니터 ON/OFF 오버헤드는 자동으로 추가 벤치마크하지 않으며 실제 성능 비교 전 별도 검증해야 합니다.
기존 진행 중인 실험을 모니터링 때문에 변경·재시작하지 않습니다.

## 오프라인 검증

```powershell
Set-Location "<프로젝트 폴더>"
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_workbench.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_shared_memory.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_history.py -v
```

기존 `test_meta.py`, `test_modal.py` 연결 시험은 이 명령에서 실행하지 않습니다.

공식 참고: [Meta tool calling](https://dev.meta.ai/docs/tool-calling), [Modal images](https://modal.com/docs/guide/images), [Modal Volumes](https://modal.com/docs/guide/volumes), [Baseten Chat Completions](https://docs.baseten.co/reference/inference-api/chat-completions).

## 재사용한 코드

사용자가 제공한 `muse-spark-chat.zip`의 온톨로지 모듈을 통합하고, 로컬 Markdown 표시용 marked / DOMPurify / highlight.js와 해당 라이선스를 `workbench/static/vendor/`에 보존했습니다. 원본 앱의 웹 키 입력·DPAPI 저장 흐름은 사용하지 않습니다. 키 설정은 사용자가 직접 실행하는 PowerShell 보안 입력 스크립트로만 합니다.

## 새 폴더에서 소스 ZIP 설치

압축을 풀고 그 폴더의 일반 PowerShell에서 실행합니다. 현재 PC의 설치 폴더에는 이미 환경이 준비되어 있습니다.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start_platform.cmd
```

API 키는 기존 사용자 환경 변수 또는 보안 입력 스크립트를 사용합니다. ZIP에는 키·대화·실행 기록이 포함되지 않습니다. 현재 PC의 공통 기억은 기존 설치 폴더 `.workbench/`에 유지됩니다.

## 기록 관리

왼쪽 **전체 대화 · 작업**에는 서비스와 관계없이 Meta·Baseten 대화와 Modal 실행이 함께 표시됩니다. 기록을 선택하면 연결된 서비스와 원문 또는 실행 결과가 열립니다. 작은 화면에서는 상단 **기록**을 사용합니다. 각 기록의 **⋯**에서 아카이브·복원·삭제할 수 있습니다. 아카이브는 공유 메모리를 유지합니다. 삭제는 확인 후 해당 기록과 연결된 공유 메모리를 제거하며 복구할 수 없습니다. 작업 파일·GPU 결과 파일과 클라우드 자료는 유지됩니다. 실행 중에는 기록을 변경할 수 없습니다. 오른쪽 패널은 상단 버튼 또는 **×**로 열고 닫으며 화면 크기별 상태를 기억합니다.

대화 상단 **↓ MD**는 사용자 메시지·전체 응답·도구 실행·파일 변경을 Markdown으로 내려받습니다. 응답 아래 **↓ Markdown**은 해당 응답만, GPU 상세의 **↓ Markdown**은 실행 로그와 결과를 저장합니다. **다크 모드 / 라이트 모드**는 왼쪽 아래에서 전환하며 선택을 기억합니다. 최초 테마는 Windows/브라우저 설정을 따릅니다.

## Goal 모드와 추론 선택

Goal → 새 Goal에서 시작합니다. Planner는 고정 Baseten 모델, Builder는 Meta입니다. 기본 산출물 언어는 English, 시간대는 America/New_York입니다. 모델 옆 추론 선택은 각 서비스와 Goal 역할별로 따로 저장하며 기본 high입니다. 큰 응답은 최대 3회 자동 이어 생성합니다. 자세한 정책·예산·프록시·API·검사는 GOAL_MODE.md를 참고하세요.


Muse Spark Open 경로는 Goal 화면의 **경로 설정**에서 Baseten(기본) 또는 Modal 공유 엔드포인트를 직접 적용합니다. Planner와 제품 프록시에 함께 반영하며 Baseten Chat은 유지합니다. Modal LLM 상한 기본 30 USD. 세부 사용법과 제한은 [GOAL_MODE.md](GOAL_MODE.md)를 참고하세요.
