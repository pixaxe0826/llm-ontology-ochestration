# Muse Goal

왼쪽 **Goal → 새 Goal**에서 목표, 작업 폴더, Builder 모델, 정책, 예산과 마감을 지정합니다. 기본 산출물 언어는 `English`, 시간대는 `America/New_York`입니다. Windows 관리자 권한은 필요하지 않습니다.

제목은 1~120자 필수이며 목표 본문과 별도로 저장합니다. 비어 있으면 본문의 첫 Markdown 머리글에서 `#`를 제거한 제목을 제안하며 **제안 적용**으로 채울 수 있습니다. 기존 Goal은 제목을 자동 생성하고 본문·상태·갱신 시각을 보존합니다. 머리글 없는 기존 본문은 첫 비어 있지 않은 줄을 최대 120자로 사용합니다. 제목은 화면·기록 목록에만 사용하고 에이전트 프롬프트에는 넣지 않습니다.

목표 본문은 기본적으로 접힌 **목표 보기**에서 기존 marked + DOMPurify 렌더러로 읽습니다. 설정에서 제목만 수정하면 `POST /api/goals/title`로 제목만 저장하며 본문·계획·메시지·장부·상태는 유지합니다. Markdown 내보내기는 `# 제목` 다음에 목표 원문을 그대로 넣고 진행 보고와 메시지를 이어 붙입니다.

- **Planner / Reviewer**: Muse Spark Open. 기본 Baseten `meta-models/muse-spark-open-1026`, 선택 경로 Modal 공유 엔드포인트. 계획·검토·재계획과 메모리/읽기 전용 근거 조회를 담당합니다. 파일 쓰기·로컬 명령·GPU 실행 도구는 서버에서 거부합니다.
- **Builder**: 선택한 Meta 모델. 기존 Agents의 파일·명령·GPU 도구 루프를 사용합니다. 전체 Goal에서 실행 중인 Builder는 하나이며, 승인 대기 시 실행 잠금을 반환해 다른 항목이 진행될 수 있습니다.
- **Executor**: 기존 Modal 도구와 하드웨어 기록을 그대로 사용합니다. 이번 업데이트는 GPU 작업을 실행하지 않습니다.

계획과 판정은 JSON 스키마를 검증한 뒤 서버 함수가 실제로 적용합니다. 문장만으로 계획·파일·완료 상태를 만들지 않습니다. 초기 계획 JSON은 실패 시 한 번만 재요청합니다. 원문 근거가 없는 성공은 완료로 받지 않고, 테스트 실행 기준에는 성공한 명령/GPU 근거가 필요합니다. Planner는 `read_file`, `search_files`, `memory_search`, `memory_read`, `memory_note`만 호출할 수 있습니다. 네이티브 도구가 미지원이면 JSON 도구 요청을 서버가 같은 허용 목록으로 처리합니다.

## 실행과 중지

계획 → 배정 → 구현 → REPORT → REVIEW를 반복합니다. 한 항목은 최대 4묶음, 재작업은 최대 3회입니다. 새 근거 없는 순환 3회, 비용 상한 또는 UTC 마감 도달 시 일시정지합니다. 막힌 항목이 있어도 가능한 다른 항목은 진행합니다. 서버 재시작 후에는 `interrupted`가 되며 사용자가 재개합니다. 실행 상태·승인·근거·에이전트 메시지·비용은 Goal 화면에서 확인합니다.

막힌 항목은 정지 상태에서 **기존 근거 재검토** 또는 **검토 후 재작업 허용**으로 판단할 수 있습니다. 재검토는 현재 파일과 일치하는 기존 관측 근거가 있을 때만 가능하며 Builder를 추가 실행하지 않습니다. 변경된 파일 뒤의 동일한 테스트 출력은 파일 해시와 함께 별도 근거로 기록합니다.

**Manual**은 명령과 GPU를 개별 승인합니다. **Autonomous**는 선택한 기본 명령 목록, 허용 GPU와 작업당/일일/전체 상한 안에서만 자동 승인합니다. 그 밖은 승인 대기합니다. `git push`, 배포·게시, 명시적 폴더 밖 경로와 키 접근 명령은 거부합니다. 기존과 같이 일반 사용자 프로세스 실행이며 OS 수준 파일 시스템 샌드박스는 아닙니다. 자동 실행은 신뢰하는 작업 폴더와 프로그램에 사용해야 합니다.

## 추론 강도와 출력

모델 옆 선택기는 Meta Work와 Baseten Chat 각각 저장됩니다. Goal은 Planner와 Builder가 별도 값을 사용합니다. 기본값은 모두 **high**이며, 복잡도나 단계가 선택값을 바꾸지 않습니다. 선택지는 `minimal`, `low`, `medium`, `high`, `xhigh`입니다.

| 선택값 | 요청 출력 상한 |
|---|---:|
| minimal / low | 8,000 |
| medium | 12,000 |
| high | 16,000 |
| xhigh | 24,000 |

모델별 설정 상한으로 다시 제한합니다. Baseten은 24,000 요청과 `high` 파라미터의 접수를 실호출로 확인한 보수적 상한입니다. Meta 1.3은 공식 문서의 131,072 상한을 사용합니다. 미등록 모델은 확인 전까지 4,096으로 제한합니다. 공급자가 추론 설정을 지원하지 않으면 UI를 비활성화하고 파라미터를 생략합니다.

`finish_reason=length`이면 최대 3회 이어 생성합니다(최초 요청 포함 최대 4회). 하나의 응답으로 합쳐 저장하고 각 호출의 사용량을 기록합니다. 이후에도 `length`이면 `interrupted`로 남기고 미완성 원문을 공유 검색에서 제외합니다. 연결 종료 신호가 없거나 API 오류인 경우에는 성공으로 바꾸지 않습니다. Goal의 큰 코드·표·사양은 파일 도구로 작성하고 메시지는 요약과 경로만 남기도록 지시합니다.

설정 파일: `workbench/model_limits.json`, `workbench/goal_prices.json`. 추론 토큰은 출력 토큰에 포함되므로 출력 비용에 **중복 가산하지 않습니다**. 장부와 미터에는 별도 수치로 표시하고, 미보고 시에는 미보고로 표시합니다.

## 시간·메모리·근거

Goal 프롬프트는 영어로 작성하며 새 주석·문서·커밋 메시지·메모리 노트·에이전트 메시지·보고의 언어를 `output_language`로 지정합니다. 원문 근거와 기존 사용자 파일은 그대로 보존합니다. 마감과 진행 시각은 선택한 IANA 시간대로 표시하고 UTC도 함께 저장합니다. 마감 판정은 UTC epoch로만 합니다. DST로 존재하지 않거나 중복되는 현지 시각은 거부하고 UTC 오프셋 명시를 요구합니다.

도구 결과 원문은 store에 남깁니다. Builder 입력의 결과는 개별 최대 8,000자, Planner의 근거 입력은 호출당 합계 최대 20,000자로 제한합니다. Planner는 필요하면 범위를 지정한 `read_file(start_line, line_count)`로 더 확인할 수 있습니다. 메모리 노트에는 Goal ID와 역할 개념을 추가하며 기존 비밀값 검사를 적용합니다.

## 비용과 제품 프록시

호출 전 보수적 비용을 예약하고 응답 usage로 정산합니다. 사용량을 확인하지 못한 요청은 예약 추정값을 남깁니다. Baseten 개발 호출은 제품 예비분을 쓰지 못합니다. Goal 상한과 플랫폼 공통 상한을 함께 적용하며 80%에서 경고합니다. GPU 비용은 실제 프로그램 실행 시간 × 설정 시간당 단가의 추정값입니다. 빌드·CPU·메모리·스토리지·네트워크 비용은 포함하지 않습니다. 장부 도입 전의 사용량과 공급자 청구서를 복원하지 않습니다.

승인된 로컬 명령에만 다음 환경 변수가 주입됩니다. 실제 공급자 키와 프록시 토큰은 프로젝트 파일·브라우저·GPU 작업에 전달하지 않습니다. 프록시 토큰은 서버 메모리에만 존재하고 재시작 시 폐기됩니다.

```python
import os
from openai import OpenAI
client = OpenAI(base_url=os.environ['MUSE_PROXY_BASE_URL'],
                api_key=os.environ['MUSE_PROXY_TOKEN'])
reply = client.chat.completions.create(
    model='meta-models/muse-spark-open-1026',
    messages=[{'role':'user','content':'Summarize the measured diagnostic result.'}])
```

`GET /proxy/v1/models`는 Goal 프록시 토큰 인증 후 현재 경로의 실제 허용 모델 ID를 OpenAI 목록 형식으로 반환합니다. Baseten은 `meta-models/muse-spark-open-1026`, Modal은 설정된 엔드포인트 ID입니다. 목록 조회에는 공급자 호출과 비용이 없습니다.

제품 chat/completions의 허용 요청 필드는 `model`, `messages`, `stream`, `max_tokens`, `max_completion_tokens`, `reasoning_effort`, `tools`, `tool_choice`, `parallel_tool_calls`입니다. 나머지는 `지원하지 않는 필드: ...`와 함께 HTTP 400으로 거부합니다. OpenAI function 도구 정의·선택 옵션, assistant의 `tool_calls`·`reasoning_content`, `role: tool`의 `tool_call_id`·결과를 전달하며 이미지 content 배열과 함께 사용할 수 있습니다. 응답의 `tool_calls`, `finish_reason: tool_calls`, 선택적 `reasoning_content`를 보존합니다. 클라이언트는 도구를 직접 실행하고 반환된 assistant 메시지 전체와 tool 결과를 다음 요청에 넣어야 합니다. 프록시 서버가 도구를 실행하지는 않습니다. SSE는 기존과 같이 버퍼링하며 tool delta에는 `index`를 넣습니다. 도구 정의도 비용 예약에 포함하고, 실제 사용량은 기존 제품 예비분·장부 규칙으로 정산합니다.

`POST /proxy/v1/chat/completions`는 텍스트 또는 OpenAI 호환 content 배열(`text`, user 역할의 `image_url`)을 받으며 배열·URL·detail을 그대로 전달합니다. 비어 있는 model 및 `muse-spark-1026`, `muse-spark-open-1026`, `muse-spark-open`, `meta-models/muse-spark-open-1026`은 설정된 Muse Spark Open 경로의 별칭입니다. Modal 경로에서는 실제 엔드포인트 ID도 받습니다. 다른 모델은 허용 목록과 함께 400으로 거부하며 Meta 공급자로 추측해 보내지 않습니다. 응답의 model에는 실제 사용한 ID를 넣습니다. 공급자 오류는 키·URL·원문 없이 `upstream 404: model not found` 등의 고정된 원인 분류로 표시합니다.

이미지는 PNG/JPEG/WebP/GIF, **장당 5 MiB(5,242,880 bytes), 요청 전체 4장**, JSON 본문 30 MiB가 상한입니다. `data:image/png;base64,...` 등의 data URI는 디코딩한 바이트로 검사합니다. 공개 HTTP/HTTPS URL은 표준 포트만 허용하며 인증 없이 최대 5 MiB+1까지만 읽어 검사합니다. DNS의 모든 응답이 공개 IP여야 하고 검사한 IP에 연결을 고정합니다. 리디렉션도 매번 재검사하며 로컬·사설·링크 로컬 주소/파일 경로/URL 내 인증값은 거부합니다. 전체 URL 검사는 20초 예산이며 외부 URL은 검증 후에도 공급자에 원문 그대로 전달하므로, URL의 내용이 나중에 바뀌는 경우까지 동일 바이트임을 보장하지는 않습니다. 안정적인 크기 보장이 필요하면 data URI를 사용합니다. 원본 이미지와 URL은 장부/진단 로그/공유 메모리에 저장하지 않습니다.

이미지 요청의 사전 비용 예약은 base64 문자 수 대신 Muse Spark Open의 전체 입력 문맥 1,048,576토큰을 보수적으로 예약합니다. 실제 비용은 각 응답의 usage로 정산하며 원래 usage(이미지 등 공급자 추가 필드 포함)는 각 제품 ledger의 `provider_usage`에 비밀값 제거 후 보존합니다. 이어 생성한 응답도 각각 기록합니다. 예약 비용이 남은 예산보다 크면 기존 예산 보호 규칙에 따라 거부됩니다.

제품 호출은 `product` 장부에 기록합니다. 스트리밍 요청은 MVP에서 완성 응답을 버퍼링한 SSE 1개와 `[DONE]`으로 반환합니다. Baseten Chat과 제품 프록시는 Goal 역할 제한의 대상이 아닙니다. 일시정지된 Goal의 프록시는 계속 비활성 상태이며, 사용자가 재개하면 새 토큰을 승인된 로컬 명령에만 전달합니다.

제품 프록시의 레거시 `max_tokens`만 최소 1024로 보정하고 더 큰 요청값은 모델 최대값까지 유지합니다. 명시적 `max_completion_tokens`는 보정하지 않으며, 보정 시 `X-Muse-Proxy-Adjusted: max_tokens 32->1024` 헤더와 제품 비용 장부의 `proxy_adjusted`·요청/적용 토큰 수로 표시합니다.

### Goal 설정 변경과 자동 승인

Goal의 **설정 → 정책 Autonomous → 자동 승인 범위 → 설정 저장**으로 변경합니다. paused·waiting_approval·interrupted에서 바로 저장할 수 있고, 사용자 일시정지는 그대로 유지합니다. running/planning 중에는 현재 Builder 묶음 또는 Planner 단계가 끝나면 적용하며 대기 표시를 보여 줍니다. 설정 창 안에 저장 오류를 계속 표시하고, **설정 변경 기록**에 이전/이후 값과 적용 상태를 저장합니다. 이 기록은 계획·에이전트 메시지·비용 장부와 별도이며 목표 본문·작업 폴더를 바꾸지 않습니다. 추론 강도 바로 선택기도 같은 적용 규칙을 사용합니다.

새 정책 적용 시 살아 있는 승인 대기 작업을 다시 판정합니다. 새 정책의 허용 명령이나 GPU·작업당 비용·전체/일일 예산에 맞으면 자동 승인하고, 나머지는 사유를 표시한 채 대기합니다. 질문에는 사람이 답해야 합니다. 비활성 Goal을 재개하거나 취소/중단된 승인을 되살리지 않습니다.

`python <workspace.py>`의 스크립트는 작업 폴더 안의 상대 `.py` 경로여야 하며, 이후 인수는 안전한 상대 경로·일반 문자열·플래그만 자동 승인합니다. pytest/unittest 뒤 인수도 같은 기준입니다. 절대 경로·`..`·셸 특수문자는 자동 승인하지 않고, 기존 폴더 밖/비밀 경로·push/배포/키 접근 금지는 유지합니다. Goal 전용 프록시 환경 변수 주입에는 별도 수동 승인 예외가 없으며 공급자 키는 자식 환경에서 제거합니다.

### Planner 검토 오류와 계획 키

Planner의 검토 JSON/스키마/근거 참조/재계획 검증 오류는 Builder의 rework로 계산하지 않습니다. 오류를 Planner에게 돌려주고 한 번 재요청하며, 두 응답이 모두 잘못되면 항목 상태·Builder 카운터를 유지한 채 `review_error`와 두 응답의 redact된 원문을 저장합니다. 다음 검토는 최소 30초 뒤에 하고 다른 항목은 계속 진행합니다. 실패한 검토 주기 3회(주기당 최대 2응답)에만 System이 플랫폼 오류로 ESCALATE하고 그 항목의 자동 검토를 멈춥니다. 다른 실행 가능 항목이 없으면 Goal은 blocked가 됩니다. 일시정지한 뒤 **검토 다시 시도 허용**으로 재검토 잠금을 풀 수 있습니다. 실제로 유효한 rework 판정과 구체적인 Builder 결함은 기존대로 보완 횟수에 반영합니다.

계획에는 안정된 `key`를 저장하며, 기존 키가 없는 항목은 기존 항목 ID를 키로 사용합니다. Planner 시스템 프롬프트에 현재 키 목록과 갱신 규칙을 제공합니다. `next_items`에서 기존 키는 갱신, 새 키는 추가입니다. 별도 새 항목이 기존 키를 재사용하면 `operation: "create"`로 표시하며 서버가 고유 접미사를 붙입니다. 서로 다른 새 항목끼리의 중복 키도 고유화하고 동일한 중복 행은 합칩니다. 충돌하는 기존 항목 갱신, 모호한 부모, 잘못된 ID·키·순환 관계만 오류로 처리합니다. 전체 변경을 검증한 뒤 적용하며 갱신을 새 항목 수로 중복 계산하지 않습니다.

플랫폼 오류 복구로 재실행하는 항목은 과거 실행 기록·파일·장부를 지우지 않습니다. 복구 기록에 보정한 카운터와 이전 작업 ID를 남기고 todo로 돌립니다. 새 성공 근거가 필요하므로 과거 작업의 근거는 승인용에서 제외하고, 지정된 원본 스크립트의 새 실행을 확인합니다. 사용자 Goal을 자동 재개하지 않습니다.

### 큰 파일 읽기와 다음 묶음 문맥

Goal Builder의 `read_file(path, start_line?, end_line?)`은 1부터 시작하는 양 끝 포함 줄 범위를 받습니다. 결과에 `total_lines`, 실제 `start_line`/`end_line`, `truncated`, `next_start_line`을 넣습니다. 전체 파일의 모델 입력은 8,000자, 범위를 지정한 읽기는 20,000자 이하의 유효한 JSON으로 전달합니다. 문자 수에는 메타데이터와 JSON 이스케이프가 포함되며, 가능한 한 온전한 줄에서 자릅니다. 한 줄 자체가 상한을 넘으면 `partial_line`을 표시하고 YAML/JSON은 구조화 읽기로 좁히도록 안내합니다. 원본 파일은 기존 UTF-8·200 KB·전체 파일 비밀값 검사·경로 제한을 유지합니다.

`read_structured(path, key_path)`는 YAML/JSON의 해당 부분만 반환합니다. 예: `conveyors.CV1_infeed`, `conveyors.0.length`, `items[0].name`. 실제 파일에 해당 경로가 없으면 `found=false`와 `top_level_keys`를 반환합니다. 큰 하위 값도 20,000자 이내로 미리보기·하위 키·truncated 표시를 전달하므로 더 작은 key_path로 좁힙니다. YAML은 이미 설치된 PyYAML 6.0.3의 SafeLoader를 사용하며 사용자 생성자/코드 실행을 허용하지 않고 재귀·별칭 확장·깊이/노드 수를 제한합니다. requirements에도 버전을 명시합니다.

전체 읽기 원문은 로컬 `goal_file_read`에 보존하고, 모델이 실제로 본 범위는 작은 별도 인덱스에 남깁니다. 다음 Builder 묶음과 이후 호출에는 이전 묶음의 진행 요약·관찰된 도구 결과, 이미 읽은 파일/줄 범위/key_path/sha256을 제공합니다. 요약을 위해 추가 LLM 호출은 하지 않습니다. 같은 SHA의 겹치는 범위를 합치고, 다른 버전의 파일은 구분합니다. 오래된 잘린 기록에서 실제 표시 범위를 알 수 없으면 미확인으로 표시하며 파일 전체를 읽었다고 주장하지 않습니다. 긴 목록은 최근 80개/12,000자 이내로 제공하고 생략 개수를 표시합니다. 시스템 규칙은 큰 파일을 먼저 검색하고 줄 범위 또는 구조화 읽기를 쓰며 같은 묶음에서 같은 범위를 반복해서 읽지 않도록 합니다.

Meta Work의 기존 파일 읽기와 Baseten Chat 동작은 유지합니다. Planner는 start_line/end_line과 기존 line_count를 사용할 수 있으며 원문 보관·호출당 근거 20,000자 상한을 유지합니다. 이 기능 적용만으로 Goal을 재개하거나 과거 파일·계획·메시지·비용 기록을 바꾸지 않습니다.

## Muse Spark Open 경로

**Goal → 경로 설정** 또는 **연결 및 모델 설정 → Muse Spark Open**에서 `baseten`(기본) / `modal`을 선택하고 **경로 적용**을 누릅니다. Goal Planner/Reviewer와 제품 프록시의 다음 호출에 함께 적용합니다. Baseten Chat과 Meta Builder는 이 설정을 따르지 않습니다. 한 응답의 이어 생성은 처음 결정한 경로를 끝까지 유지합니다. 오류·예산 초과 시 자동 전환하거나 우회 호출하지 않습니다.

Modal은 **엔드포인트 조회**로 현재 Proxy Token이 접근할 수 있는 `/v1/models` 목록을 가져옵니다. 이 중 실제 **Muse Spark Open**을 제공하는 호스트 이름을 선택합니다. 호스트 이름만 있는 목록으로 모델 가중치를 서버가 판별할 수 없으므로, Modal 대시보드에서 모델을 확인하고 선택하세요. 반환 목록이 비어 있으면 `Muse Spark Open 엔드포인트 없음`이며 Modal 경로를 적용하지 못합니다. 엔드포인트를 새로 만드는 유료 작업은 자동으로 실행하지 않습니다.

- 주소는 `https://inference.us-west.modal.direct/v1` 고정, 인증은 사용자 환경 변수 `MODAL_PROXY_TOKEN`(`wk-<id>.ws-<secret>`)입니다. SDK/CLI의 `ak-/as-`와 다릅니다. 키는 기존 보안 입력 스크립트로만 입력하며 화면·프로젝트 파일로 보내지 않습니다.
- 장부의 `provider=modal`, `route=modal`, `workload=llm`으로 Planner/제품 호출을 기록합니다. GPU는 `workload=gpu`로 구분합니다. Baseten 기록은 별도 유지합니다.
- Modal LLM 기본 상한은 플랫폼 합계 **30 USD**, 각 Goal도 **30 USD**입니다. 플랫폼 상한은 경로 설정, Goal 상한은 Goal 설정에서 변경합니다. LLM + GPU 합계에는 기존 Modal 전체/일일 예산도 적용합니다. 제품 호출도 Modal LLM 상한을 사용하며 Baseten 예비분은 차감하지 않습니다. 기존 Goal 기록의 누락된 LLM 상한은 30으로 읽습니다.
- Modal Muse Spark Open 단가는 [공식 지원 모델 가격표](https://modal.com/docs/guide/supported-models)의 입력 1.25 / 캐시 입력 0.15 / 출력 4.25 USD/1M 토큰을 설정 파일에 반영합니다. 예약 비용과 usage 정산 모두 추정치입니다. 기존처럼 장부 밖 사용과 과거 청구서는 복원하지 않습니다.
- Baseten 플랫폼 예산이 80% 이상이면 전환 검토 알림만 표시합니다. 사용자가 직접 적용할 때까지 경로는 그대로입니다.
- Modal 공유 엔드포인트의 네이티브 도구와 `reasoning_effort`는 현재 **NOT VERIFIED**입니다. 검증된 Baseten의 지원 여부를 재사용하지 않습니다. 기본은 JSON 오케스트레이션 대체 경로, 추론 파라미터 생략, 미등록 엔드포인트 출력 한도 4,096입니다. 검증 후 `model_limits.json`의 `modal_llm_*` 지원 설정과 엔드포인트별 `model_caps`를 변경할 수 있습니다.

설정·카탈로그는 비밀값 없이 로컬 store에만 저장합니다. 실행 중인 호출은 시작할 때 경로를 고정하고 이후 새 호출부터 적용합니다.

## 새 로컬 API

- `GET/POST /api/muse-open`: 공통 경로·플랫폼 Modal LLM 상한 조회/저장
- `POST /api/muse-open/models`: 인증된 Modal 엔드포인트 목록 조회
- `GET/POST /api/goals`: 목록 / 생성
- `GET /api/goals/{id}`: 계획·메시지·비용·승인
- `GET /api/goals/config`: 가격·출력 설정·역할별 기본값
- `GET /api/goals/evidence/{id}`: Planner의 관측 근거
- `POST /api/goals/control`: pause / stop / resume
- `POST /api/goals/settings`: 정지된 Goal 설정·막힌 항목 판단
- `POST /api/goals/reasoning`: Goal별/기본 역할 추론 선택
- `POST /api/goals/approve`: 명령·GPU 승인 또는 질문 답변
- `POST /proxy/v1/chat/completions`: Goal 토큰 인증 제품 호출
- `GET /proxy/v1/models`: Goal 토큰 인증 허용 모델 목록 (공급자 호출 없음)

기존 `/api/*` 변경 요청은 기존 로컬 세션 토큰을 계속 요구합니다. 제품 프록시만 Goal Bearer 토큰을 사용하며 loopback·Host·Origin 제한은 그대로 유지합니다.

로컬 `run_command`의 cwd가 누락/JSON null/빈 문자열/`null`/`None`/`none`/`undefined`/`./`/`.\`이면 승인 판정과 실행 전에 `.`으로 정규화합니다. 결과의 `cwd_normalization`에 변경을 표시하며 실제 stdout과는 분리합니다. 경로 내부·상대 경로·비밀 경로·링크 검사는 그대로 적용하고, 존재하지 않는 cwd에는 영어로 작업 폴더 루트/상대 폴더 사용을 안내합니다. 파일 읽기 경로나 Modal 원격 경로에는 이 정규화를 적용하지 않습니다.

## 오프라인 검사

기존 연결 시험은 유료 호출이 있을 수 있으므로 아래 파일을 명시합니다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_workbench.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_shared_memory.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_history.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_chat_completion.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_goals.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_reasoning_planner.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_muse_routes.py -v
```

Windows IANA 시간대 데이터는 고정된 `tzdata` 패키지를 사용합니다.

## 플랫폼 예외 진단과 재개

Coordinator와 Goal Builder의 처리되지 않은 예외는 `.workbench/diagnostics/exceptions.jsonl`에 UTC 시각, Goal/작업 ID, 예외 종류, 파일·줄·모듈·함수, traceback을 남깁니다. 프레임의 로컬 변수와 공급자 응답 전체는 수집하지 않습니다. 문자열의 비밀값을 제거한 뒤 JSON으로 기록하며 로그 파일은 UI와 공유 메모리로 전달하지 않습니다. UI의 ESCALATE에는 종류·발생 위치·진단 ID만 표시하고 원문 예외 메시지는 숨깁니다.

오류 수정 후에는 기존 Goal을 선택해 **재개**합니다. 기존 계획·메시지·장부·이전 Builder 작업은 보존하며 같은 미완료 항목에서 다음 Builder 묶음을 시작합니다. 시도·묶음·예산 카운터를 초기화하지 않습니다. 플랫폼 수정은 Goal을 자동 재개하지 않습니다.

서버 재시작 전 실행 중인 Goal/단계를 확인합니다. 활성 단계가 끝난 뒤 재시작합니다. 기본적으로 `interrupted` Goal은 사용자 재개를 기다리며, 아래 무인 모드를 켠 Goal만 재시작 직전 running이었을 때 1회 재개합니다. 사용자가 일시정지한 Goal은 자동 재개하지 않습니다.

## Builder 연속 묶음 스케줄링

Builder는 각 묶음을 `report_item(status="progress"|"completed", summary, verification)`으로 끝냅니다. 네이티브 보고 도구를 쓰지 못하면 마지막 JSON의 `report_status`로 같은 값을 전달합니다. 일반 문장만 반환하거나 단계 한도에 도달한 경우는 progress로 취급하며, 문장의 어조로 완료를 추정하지 않습니다. 서버는 실제 파일/명령/GPU 근거를 별도로 수집하며 완료 보고 자체는 수용 판정이 아닙니다.

준비된 progress 항목은 다른 todo/rework와 다른 항목의 검토보다 먼저 이어 실행합니다. 동일 세대에서는 같은 Agent 작업 이력을 재사용하고 `attempts`를 올리지 않으며 `bundles`만 늘립니다. 서버 세대가 바뀌면 미완성 도구 호출 이력을 재전송하지 않고, 보존된 묶음 요약·읽기 목록·근거를 가진 새 작업 이력으로 같은 시도를 이어갑니다. 기존 이력/근거/카운터는 삭제·초기화하지 않습니다.

한 Goal에서 실제 Builder 슬롯을 가진 항목만 in_progress입니다. 승인·사람 판단·의존성·Builder 차례·플랫폼 오류·분할 하위 작업·Planner 보류 상태는 waiting과 이유로 구분합니다. 승인에 답한 이전 작업도 현재 항목의 연속 묶음을 가로채지 않습니다. 전역 Builder 실행 한도 1은 그대로 유지합니다. 기존 비활성 in_progress 기록은 조회에서 waiting으로 표시하고, 실제 재개 시에만 상태를 정리하므로 조회/설치만으로 이력을 바꾸지 않습니다.

진행 중 보고에는 Planner 검토 호출이 없습니다. 완료 보고 또는 4묶음 한도에서 검토합니다. 한도에서도 모든 수용 기준의 실제 근거가 있으면 accept할 수 있으며, 미완료 rework는 `batch_action=split`(새로운 한정된 하위 항목) 또는 `defer`(해당 항목 판단 대기, 다른 작업 진행)를 명시해야 합니다. 부모의 4묶음 카운터를 초기화하거나 5번째 묶음을 실행하지 않습니다. 분할 항목은 부모의 선행 작업/판단 제약을 계승하고, 소진된 부모에 의존할 수 없습니다. 하위 항목 완료 후 원래 수용 기준을 누적 근거로 다시 검토합니다. 기존 3회 rework·진행 없음·예산·마감 제한은 유지됩니다.

승인 레코드와 연결 판단을 모두 게시한 뒤 답변을 처리하도록 같은 잠금을 사용하며, 이미 처리된 승인에 연결된 ESCALATE는 새 판단을 만들지 않습니다. Meta Work/Chat의 완료 방식은 변경하지 않습니다. 새 HTTP API 경로는 없고 Goal 상세에 execution_status/wait_reason이 추가됩니다.

오프라인 검사: `python -m unittest discover -s tests -p test_goal_scheduler.py`.

## 판단 대기와 독립 작업

`decision`은 질문·선택지·Planner 추천/이유·영향 항목·가역성·pending/answered/withdrawn·UTC/현지 시각을 보존합니다. Planner/Builder의 `request_decision`과 사람에게 보내는 ESCALATE/QUESTION, 명령/GPU 승인 요청이 연결됩니다. Goal의 **판단 대기**에서 선택지 또는 직접 답변을 보내며 알림에서도 해당 판단으로 이동합니다. 기존 승인 버튼도 사용할 수 있습니다.

각 항목의 `depends_on`과 `blocked_by_decision`은 해당 Goal의 항목/판단 ID를 참조합니다. Planner 출력에서는 기존 plan key도 항목 참조로 사용할 수 있습니다. 순환·다른 Goal 참조는 거부합니다. 낮은 `priority`부터 준비된 항목을 배정하며, 화면의 **의존성 수정**에서 변경할 수 있습니다. 기존 항목에 필드가 없으면 조회 시 **의존 여부 미확인**으로 표시합니다. 기록을 임의로 마이그레이션하지 않으며 다음 Planner 갱신에서 명시적으로 채울 때까지 그 항목은 배정하지 않습니다.

판단과 무관한 작업은 계속합니다. 더 진행할 항목이 없으면 `waiting_decision`과 막힌 작업 요약을 저장하고 긴급 알림을 보냅니다. 이때 Planner/Builder 호출을 금지하고 스레드는 이벤트를 기다립니다. 답변·의존성 편집·설정 변경은 즉시 깨우며, UTC 마감이나 사용자 중지·일시정지는 계속 적용됩니다. 사용자가 paused/stopped로 둔 Goal은 답변만으로 재개하지 않습니다. 서버 재시작 시 waiting_decision도 interrupted가 되고 명시적 재개를 기다립니다.

**잠정 진행 허용**은 기본 켬입니다. 판단이 가역적이고 추천안이 있으며 항목에 `provisional_scope=local_files_and_tests`가 지정된 경우만 PROVISIONAL로 진행합니다. GPU/유료 도구·임의 프로그램·git 명령은 금지하며 명령은 기존 정책에 맞는 로컬 pytest/unittest만 허용합니다. 제품 프록시 토큰은 전달하지 않습니다. `spec/`, `specs/`, `specification/`, `specifications/` 아래 파일과 YAML/JSON 수정은 잠정 진행에서 보수적으로 거부합니다. 기존 명령 실행은 OS 샌드박스가 아니므로 테스트 코드 자체의 부작용까지 격리하지는 않습니다.

잠정 파일은 `.muse-provisional-<goal-id>.json`과 `goal_provisional_file`에 파일 경로·판단 ID·PROVISIONAL 상태로 표시합니다. 코드/파일 형식에 주석을 강제 삽입하지 않습니다. 추천안과 같은 답은 표시만 해제하고, 다른 답/철회는 관련 잠정 항목을 rework로 되돌리며 Planner의 영향 재계획 전 실행을 막습니다. 판단 변경은 Builder 결함 횟수로 계산하지 않고 기존 파일·근거·장부를 보존합니다.

- `GET /api/goals/{id}`: `decisions`와 항목별 의존/잠정 상태, 대기 요약 포함.
- `POST /api/goals/decision`: `{goal_id,decision_id,option}` 또는 `answer`; `withdraw:true`로 철회. 기존 로컬 세션 토큰 필요.
- `POST /api/goals/dependencies`: `{goal_id,item_id,depends_on,blocked_by_decision,priority,provisional_scope}`. 기존 로컬 세션 토큰 필요.
- `POST /api/goals/settings`: `allow_provisional` 불리언 지원.
- 오프라인 검사: `python -m unittest discover -s tests -p test_goal_decisions.py`.


## Goal 아카이브와 삭제

완료(done)·중지(stopped)·일시정지(paused) Goal만 아카이브합니다. 실행/계획/승인·판단 대기/blocked/interrupted는 먼저 중지하며, 남은 Builder·GPU·미정산 호출·종료 중 Coordinator가 있으면 기다립니다. 아카이브는 원래 상태·계획·메시지를 보존하는 별도 표시입니다. Goal의 **아카이브 보기** 또는 왼쪽 기록의 **아카이브**에서 열고 복원합니다. 복원은 자동 실행하지 않습니다. 아카이브 동안 실행·설정 변경·제품 프록시는 차단되고 관련 알림은 보존한 채 숨깁니다.

Goal의 **⋯ 관리 → 삭제…**는 아카이브 후에만 표시됩니다. 현재 Goal 제목을 정확히 다시 입력해야 **Goal 영구 삭제**가 활성화되며 서버도 다시 검사합니다. Goal·계획·메시지·판단·알림과 내부 승인/근거/검토/설정 변경 기록을 함께 제거합니다. 삭제는 복구할 수 없습니다. 최소한의 삭제 ID 표시는 지연된 알림 재생을 막기 위해 남습니다.

**비용 ledger는 모든 공급자에 대해 삭제하지 않습니다.** Goal이 사라져도 플랫폼 전체·일일·Baseten 제품/개발·Modal LLM 상한에 계속 계산됩니다. 작업 폴더와 Git, 기존 Builder 작업 기록, GPU 작업 기록/로컬 결과/Modal Volume은 유지합니다. Volume의 남은 결과는 Modal 대시보드에서 사용자가 직접 삭제합니다.

공유 메모리의 원문은 유지합니다. 삭제 창에서 **이 Goal의 메모리 기록을 공유 제외로 변경**(기본 꺼짐)을 선택하면 연결된 Builder/GPU 메모리와 Goal ID 개념이 있는 노트를 shared=false로 바꾸며, 다른 Goal의 메모리는 그대로 둡니다. 제외한 원문은 공유 메모리 화면에서 읽고 다시 공유할 수 있습니다. 향후 메모리를 만들 때도 Goal ID 연결을 유지해야 일괄 제외 대상이 됩니다.

- `GET /api/goals/history?goal_id=...`: 관리 가능 여부, 메모리·GPU 결과 수.
- `POST /api/goals/history`: `{goal_id,action:archive|restore|delete}`. 삭제는 `confirmed:true, confirmation_title:<현재 제목>, exclude_memory:false|true` 추가. 기존 로컬 세션 토큰/출처 검사 적용.
- `GET /api/goals`는 호환성을 위해 아카이브 표시를 포함한 전체 기록을 반환하고, UI의 최근/아카이브 목록이 이를 필터링합니다.
- 오프라인 검사: `python -m unittest discover -s tests -p test_goal_history.py`.


## Goal Builder 모델 카드

Goal 생성/설정은 Builder와 Planner를 같은 세로 카드로 표시합니다. Builder는 모델 선택 다음에 추론 강도를 배치하며, 650px 이하에서는 두 카드를 위아래로 표시합니다. 새 Goal의 기본 모델은 muse-spark-1.3이고 기존 Goal은 저장/대기 중 설정의 모델·추론 강도를 그대로 표시합니다. 화면을 열거나 모델 목록을 읽는 것만으로 설정을 저장하지 않습니다.

`POST /api/goals/models`는 Meta 모델 ID 목록을 조회한 뒤 Goal Builder의 검증된 작업용 목록과 교집합만 반환합니다. 현재 지원 목록은 muse-spark-1.3, muse-spark-1.3-contributor, muse-spark-1.2, muse-spark-1.2-contributor, muse-spark-1.1입니다. 이미지·음성·SAM·Muse Spark Open 등은 Goal Builder 목록에 포함하지 않습니다. 목록 조회 실패 시 **목록을 불러오지 못함**을 표시하고 기본값 또는 기존 선택값을 유지합니다. 조회 목록에 없는 저장값도 임의로 바꾸지 않습니다.

모델 미선택/미지원 선택은 생성·설정 저장 버튼과 서버 검증 양쪽에서 거부합니다. 기존 API의 builder_model 인수 누락은 기본 모델을 사용하는 호환 동작이며, 명시적 빈 문자열/null은 거부합니다. `GET /api/goals/config`의 builder_models/builder_default를 UI와 서버 검증의 공통 목록으로 사용합니다. Meta Work·Baseten Chat·Planner 고정 모델·기존 Goal 설정 데이터는 바꾸지 않습니다.

공식 모델/도구 지원 근거: https://dev.meta.ai/docs/models 및 https://dev.meta.ai/docs/cookbook/tool-function-calling . 미래 모델은 지원 확인 후 목록에 추가합니다.

오프라인 검사: `python -m unittest discover -s tests -p test_goal_models.py`.

## 명령 근거의 입력과 출력

로컬 Python 스크립트 및 `python -m pytest/unittest`는 공급자 키가 제거된 기존 자식 환경에서 실행하며, Python audit hook으로 작업 폴더 안의 입력·출력 해시를 별도로 기록합니다. MuJoCo 등 네이티브 파일 API는 안전한 정적 경로 분석으로 보완합니다. 동적 네이티브 IO와 Python 외 명령의 읽기 목록은 완전한 추적을 보장하지 않으며, 추적 방식과 미확인 입력을 catalog에 표시합니다. 비밀 경로·외부 경로·링크는 관찰용 해시 수집에서도 제외합니다.

명령의 입력이 바뀌면 그 실행 근거가 stale이 됩니다. 직접 쓴 출력은 별도 `command_output` 근거와 해시로 보관하므로, 출력 변경은 해당 출력만 stale로 만듭니다. 무관한 문서 변경은 명령 근거를 무효화하지 않습니다. 과거 명령은 기록된 입력 해시만 재사용하며, 없는 실행 당시 출력 해시를 현재 해시로 소급하지 않습니다.

부모 검토는 같은 Goal의 하위 항목 근거를 함께 읽습니다. 분할 항목(`split_from`)은 원래 부모의 근거를 재사용할 수 있고, catalog에 원래 항목 ID와 stale 사유가 남습니다. 검토 패킷은 최신 유효 실행을 우선 포함하여 오래된 실패 기록이 새 성공을 밀어내지 않게 합니다. 동일 명령을 새로 실행하면 새 실행 ID를 유지합니다.

## Goal GPU 결과 회수와 실행 회수 제한

Goal Builder의 `copy_gpu_artifact(job_id, path, destination, expected_sha256?)`는 이미 내려받은 PNG·GLB 등(32 MB 이하)을 같은 Goal·항목의 작업 폴더에 복사합니다. 명령·GPU를 실행하지 않으며 경로·키 검사와 기존 파일의 SHA 확인·백업을 적용합니다. 원래 GPU 로그와 결과는 store에 보존하고, 모델에는 상태·작업 ID·파일 목록·짧은 로그를 우선 전달합니다. `read_gpu_artifact`는 기존대로 200 KB 이하 UTF-8 텍스트 전용입니다. 성공한 작업을 파일 회수 목적으로 다시 제출하지 않습니다.

서버는 같은 사람 승인 실행 주기의 GPU 작업 레코드를 세어 제출 전에 차단합니다. 기본은 항목당 3회(`gpu_jobs_per_cycle`)이며 Goal 설정에서 변경합니다. 항목의 `max_gpu_jobs_per_cycle`이 있으면 그 값이 우선하므로 P5의 1회 제한은 유지합니다. 성공·실패·취소·대기·중단 작업 모두 1회를 소비합니다. 기존 주기의 사용량을 지우지 않으며, 명시적인 사람 판단 답변에서 `rework`로 새 실행 주기를 열었을 때만 새 주기 한도가 생깁니다. 실행 주기가 없는 과거 작업은 첫 주기 사용량으로 보수적으로 계산합니다. Meta Work의 동작은 바꾸지 않습니다.

Goal의 공급자 연결·플랫폼 예외로 끝난 마지막 묶음은 감사 기록을 남기고 한 번만 복구합니다. 실제 작업을 수행한 시도와 기존 보완 횟수·실행 근거·GPU 작업·비용 장부는 보존합니다. 복구 자체는 판단에 답하지 않으며 Goal이나 Builder를 자동 재개하지 않습니다.

## 문서 수용 기준과 인용

Planner는 새 계획·계획 갱신에서 기준마다 종류를 명시합니다. 예:

```json
{"key":"phase-report","title":"Phase report","acceptance":[
  {"text":"The report accurately summarizes existing execution results.","kind":"document",
   "document_paths":["review/PHASE1_REPORT.md"],"required_sections":["Summary","Evidence","Next"],"format":"markdown"}
]}
```

`execution`은 새로운 실행·생성·측정 결과를 요구하며 성공한 명령/GPU 근거가 필요합니다. `document`는 현재 해시의 문서 읽기·쓰기 근거, 서버의 필수 섹션/형식 검사, 문장·표 행별 괄호 인용으로 검토합니다. 기존 결과를 요약하는 문서를 승인받기 위해 명령이나 GPU를 새로 실행할 필요는 없습니다. 과거 실행이 현재 유효하면 그대로 인용합니다.

기존 `acceptance` 문자열과 수용 기준 텍스트 키는 유지하고, 정규화된 `acceptance_rules`에 종류·필수 섹션·경로·형식을 저장합니다. 기존 항목은 서버 시작/읽기로 변경하지 않으며, Planner의 다음 계획 갱신에서 분류합니다. 미분류 항목은 execution으로 간주하면서 종전의 문구 기반 실행 근거 판정을 그대로 유지합니다. 기존 파일 존재 기준에 실행을 새로 강제하는 마이그레이션은 하지 않습니다. 명시적 execution은 실행 근거를 요구합니다.

문서에는 `9 tests passed (task-id:22).`, `Estimated cost USD 0.25 (ledger:record-id).`처럼 같은 문장/표 행에 인용합니다. 같은 Goal의 다른 항목에서 얻은 근거도 사용할 수 있습니다. 존재하지 않거나 stale·실패인 인용은 성공/수치 주장을 뒷받침하지 못합니다. 문서 자신의 파일 근거를 그 문서의 성공/수치 주장의 증명으로 쓰지 않습니다. ledger 인용은 정산된 추정 비용 기록이며 실제 청구서 검증을 뜻하지 않습니다.

서버는 알려진 테스트 수(예: `9 passed`, `Ran 9 tests`), 비용, 초 단위 시간, SHA-256/명시적 축약 해시를 비교합니다. 비용·시간 표시는 표시 소수 자릿수에 맞춘 반올림 허용 오차를 사용하고 테스트 수·전체 해시는 정확히 비교합니다. 그 밖의 숫자는 인용 결과에서 관찰되는 값과 대조하며, 의미상 연결·내용 완전성은 Planner도 검토해야 합니다. 자연어 전체의 사실성을 자동 보장하는 검사기는 아닙니다. 모호하거나 검증하지 못한 값을 새 실행으로 우회하지 말고, 근거와 표현을 보완합니다.

`format`은 markdown/text/json/yaml입니다. Markdown 필수 섹션은 코드 블록 밖 머리글을 확인하고, JSON/YAML은 구문 및 점으로 구분한 키 경로를 검사합니다. 구조화 문서는 주장 필드가 속한 객체에 `evidence_ids` 배열을 둡니다. YAML은 기존 안전 로더와 노드·깊이·별칭 제한을 적용합니다.

문서 Builder 입력에는 같은 Goal의 실제 근거 ID와 정산된 비용을 담은 8,000자 이내 `DOCUMENT SOURCE CATALOG`를 제공합니다. Planner 검토 입력에는 `DOCUMENT VALIDATION`과 관련 문서·인용 근거를 우선 제공합니다. 동일 문서의 반복 읽기보다 그 문서가 인용한 실행 근거를 먼저 유지합니다. 전체 점검은 기존 `goal_review_attempt.document_validation`에 저장합니다. 문서 내용 결함을 무시한 accept는 서버가 구체적인 rework로 전환하며, Planner JSON 형식 오류로 누적하지 않습니다. 잘못된 검토 JSON·계획 키 오류의 기존 처리 방식은 유지합니다. Goal 화면은 분류된 기준에 `[문서]`/`[실행]`을 표시합니다.

오프라인 검사: `python -m unittest discover -s tests -p test_document_acceptance.py`.

## Builder 참고 자료·이미지 도구

Goal Builder에만 `reference_search(query)`, `fetch_reference_image(url)`, `analyze_images(paths, question)`를 제공합니다. Planner·Meta Work·Baseten Chat의 기존 도구/Chat Completions 흐름은 바꾸지 않습니다. 형상 작업은 제조사 사양서·치수 도면을 우선 찾아 `review/VISUAL_REFERENCE.md`에 출처 URL·관찰 비율/부품/배치·우리 사양과의 대응·근거 ID를 기록한 뒤 진행합니다. 절대 치수는 사진만으로 확정하지 않습니다.

검색은 현재 Builder 모델로 별도 Meta Responses 호출(`web_search`, `max_tool_calls=1`, `store=false`)을 실행합니다. 제목·URL·설명·인용을 반환하며 `searched=false`면 검색이 수행됐다고 주장하지 않습니다. Meta는 검색 수행을 모델이 결정하므로 검색이 보장되지는 않습니다. 응답 usage의 알려진 도구 카운터를 우선 확인하고, 없으면 `web_search_call` 개수로 기록합니다. 검색 없이 답한 호출도 상한 1회를 사용하고, 실패·재시작으로 usage가 불명확한 호출도 1회를 보수적으로 계산합니다. 모델 요청의 일시적 통신 오류만 아래 규칙으로 최대 2회 재시도하며, 재시도마다 검색 상한과 비용을 소비합니다. 이미지 다운로드 자체는 자동 재시도하지 않습니다.

`reference_limits: {"goal":20,"item":8}`가 기본이며 Goal 생성·설정에서 변경할 수 있습니다(0은 검색 끔). 사용량은 ledger에 남으므로 서버 재시작·항목 재작업으로 초기화되지 않습니다. Meta 전체·일일·Goal 예산도 함께 적용합니다. `goal_prices.json`의 `web_search.meta_usd_per_query` 기본값은 **추정 $0.0025/검색**이며 토큰 비용은 별도입니다. ledger에는 토큰·캐시·추론 토큰, 검색 횟수/출처, 검색료·토큰료와 합계를 저장합니다. 검색·이미지 입력은 최악 입력 컨텍스트 기준으로 먼저 예약하고 실제 usage로 정산합니다. 예약 부족으로 호출 전에 멈출 수 있습니다.

이미지 다운로드는 제품 프록시와 동일한 공개 HTTP(S) 주소/DNS 검사·검사한 IP 고정·리디렉션 재검사·크기 5 MiB·시간 상한을 재사용합니다. 참고 도구는 PNG/JPEG/WebP만 허용하고 MIME/서명을 확인합니다. 파일은 `references/<sha256>.<확장자>`, 출처·UTC 시각·크기·해시는 `references/INDEX.json`에 누적합니다. `/references/`를 `.gitignore` 마지막에 추가합니다. 기존 파일과 인덱스 원본을 임의로 덮어쓰지 않습니다.

이미지 분석은 작업 폴더 안의 PNG/JPEG/WebP 1–4장(각 5 MiB 이하)만 일회성 이미지 입력으로 보냅니다. 이미지 요청 본문/base64는 task·ledger·메모리·진단 로그에 저장하지 않고, 경로·해시·usage와 분석 텍스트만 저장합니다. 모델 오류도 원문 본문을 노출하지 않습니다. 입력 파일이 바뀌면 분석 근거는 stale입니다. 추가 유료 작업이므로 PROVISIONAL 실행과 판단 대기 중에는 호출하지 않습니다.

참고 이미지는 비율·구조 파악 전용입니다. 공개 저장소·데모·GLB·텍스처·애셋으로 포함하거나 강제로 Git 추가하지 않도록 Builder 프롬프트에 명시합니다. `.gitignore`와 프롬프트는 파생 산출물의 픽셀/텍스처 혼입이나 라이선스 적합성을 자동 증명하지 않습니다. 공개 전 원본·라이선스 및 산출물을 검토해야 합니다. 검색/분석 텍스트는 관찰 근거이며 성공한 명령/GPU 근거로 승격하지 않습니다.

실제 1회 시험에서 검색 결과 URL 12개는 반환됐지만 공급자가 설명과 문장별 `url_citation`을 비워 보냈습니다. 이 경우 `description_available=false`, `citations_available=false`와 NOT VERIFIED 안내를 반환하며 인용/설명을 만들어 채우지 않습니다. 검색 결과 URL의 존재와 요약의 문장별 사실 검증은 별개입니다. 같은 호출을 반복하지 않고 기존 공개 응답 형태를 가짜 공급자 회귀 테스트로 재현합니다.

공식 자료: [Search grounding](https://dev.meta.ai/docs/search-grounding), [Responses API](https://dev.meta.ai/docs/protocols/responses), [Responses schemas](https://dev.meta.ai/docs/api-reference/responses/schemas), [가격표](https://dev.meta.ai/docs/pricing-rate-limits). 확인일: 2026-11-01.

오프라인 검사: `python -m unittest discover -s tests -p test_goal_references.py`.

## 무인 운영 안정화

Goal 설정의 `unattended` 기본값은 false입니다. 켜면 서버 재시작 직전 **running**이던 Goal만 재시작 후 1회 재개합니다. planning/waiting_approval/waiting_decision/paused/stopped/done은 대상이 아닙니다. 정상 종료 때는 `goal_restart`에 의도를 저장하고, 비정상 종료 때는 남은 running 상태로 판별합니다. 실제 서버 시작 후 시도 전에 attempted를 영구 기록하고 성공/실패를 남기므로 같은 재개를 반복하지 않습니다. 실패하면 주의 알림을 남깁니다. 사용자 일시정지·모드 해제·세대 변경은 이전 재개 의도를 취소합니다. 기존 Goal의 설정을 임의로 켜지 않습니다.

Goal의 Meta/Baseten 모델 요청(Planner·Builder·제품 프록시·참고 검색/이미지 분석)은 APIConnectionError·연결 재설정·일시적 연결 오류·HTTP 429/5xx에 **5초, 20초** 대기 후 최대 2회 재시도합니다. 429 이외 4xx, 형식·검증 오류는 재시도하지 않습니다. Modal 공유 LLM 경로와 기존 일반 Meta Work/Baseten Chat에는 이 Goal 전용 재시도를 추가하지 않습니다. SDK의 자체 max_retries=0은 유지해 중첩 재시도를 방지합니다. 출력 길이 이어 생성은 별도 모델 요청이며 각 요청에 같은 상한을 적용합니다.

각 시도는 별도 ledger 예약/정산과 `retry_index`, `model_call_id`를 남깁니다. 실패 응답에 usage가 있으면 이를 반영하고, 없으면 예약한 보수적 비용을 유지합니다(실제 과금 확인을 뜻하지 않음). 다음 시도 전에 최신 예산·마감·세대·사용자 중지·Builder 취소를 검사합니다. 작업 기록에는 `model_retry_count`/model_retry 이벤트, Planner 포함 모든 시도는 `goal_model_attempt`에 저장합니다. 예외 원문·키·이미지 본문을 재시도 기록에 넣지 않습니다. 소진된 공급자 오류는 provider 범주로 판단 대기하며 Builder의 검증 실패로 바꾸지 않습니다.

재시도 범위는 모델 요청입니다. 명령·GPU 제출·파일 쓰기·이미지 다운로드·산출물 복사는 자동 재실행하지 않습니다. 성공한 GPU 결과는 새 제출 전에 `get_gpu_job`/`copy_gpu_artifact`로 확인·회수합니다. 새 실행이 필요해도 기존 주기 상한과 GPU/전체/일일 예산, 승인 정책을 지켜야 합니다. Planner는 `Planner review of X` 같은 검토 전용 일을 Builder 항목으로 만들지 않고 정상 검토 단계에서 검토합니다.

Watchdog은 알림 루프에서 60초마다 검사합니다. running 상태에서 활성 모델·Builder·GPU 단계가 10분 이상 없거나, waiting 상태인데 pending 판단/승인이 없거나, 같은 항목의 같은 오류가 3회 이상 기록되면 주의 알림과 `goal_watchdog` 기록을 남깁니다. 모델을 호출하거나 Goal·항목 상태를 바꾸지 않습니다. 같은 경고는 지속되는 동안 중복 생성하지 않으며 회복하면 해제합니다. 현재 세대/실행 주기의 작업 기록과 검토 오류를 관찰하며, 운영 점검 시각은 별도 `goal_watchdog_state`에 저장합니다.

설정 예: `{"unattended":false,"gpu_jobs_per_cycle":3}`. 오프라인 검사: `python -m unittest discover -s tests -p test_goal_operations.py`.

## 참고 페이지·PDF 읽기

Goal Builder 전용 `fetch_reference_page(url)`은 공개 HTML/PDF의 원문을 읽어 사양·도면에 접근합니다. 순서는 **reference_search → fetch_reference_page(사양서/제품 페이지) → 필요한 사진만 fetch_reference_image → analyze_images → review/VISUAL_REFERENCE.md 작성**입니다. 기존 Meta Work·Baseten Chat·Planner·제품 프록시의 도구는 바꾸지 않습니다.

- HTML: 본문 텍스트 최대 20,000자, img src를 최종 URL/base href 기준 절대 주소로 변환한 목록 최대 30개와 alt를 반환합니다. main/article을 우선하고 script/style/template 등은 제외합니다. 이미지·링크·JavaScript를 자동 실행/다운로드하지 않습니다.
- PDF: 텍스트 최대 20,000자에 `[Page N]`을 붙이고, 처음 최대 4페이지를 PNG로 저장합니다. `rendered_image_paths`를 그대로 analyze_images에 전달할 수 있습니다. 텍스트는 최대 200페이지를 확인하며 상한/생략 여부를 표시합니다. 스캔 PDF는 OCR 없이 첫 페이지 이미지로 확인하며 암호화·손상 PDF는 명확한 오류로 반환합니다.
- 주소/전송: 기존 이미지 도구와 같은 공개 주소/DNS 응답 검사·검사 IP 고정·TLS 호스트 검증·리디렉션 재검사를 사용합니다. HTML 2 MiB, PDF 15 MiB, 다운로드 20초·전체 45초 상한입니다. 한 요청은 한 번만 읽으며 자동 네트워크 재시도하지 않습니다. application/pdf 및 PDF 서명이 맞는 application/octet-stream을 허용합니다.
- 변환: `pypdfium2==5.14.0`, `Pillow==13.0.0` 고정. PDFium은 별도 자식 프로세스에서만 사용하며 공급자 키/프록시 환경을 제거하고 isolated Python 모드로 실행합니다. 남은 시간 안에 끝나지 않으면 자식 프로세스를 종료합니다. JavaScript·폼·첨부파일/외부 자원 실행은 하지 않습니다. 페이지 긴 변은 최대 2,000픽셀, PNG 한 장은 5 MiB 이하로 제한합니다.
- 보관: 원본 `.html`/`.pdf`, 추출 `.txt`, 변환 `.png`는 해시 이름으로 references/에 저장합니다. INDEX.json의 documents/images에 원본/최종 URL·UTC 시각·크기·SHA-256·PDF 페이지 번호와 부모 문서 해시를 기록합니다. `/references/`는 .gitignore에 추가하며 원본과 변환물 모두 공개 저장소·데모·GLB·텍스처·애셋에 포함하지 않습니다. HTML 원본을 UI에서 실행하거나 iframe으로 표시하지 않습니다.
- 상한: `reference_page_limits: {"goal":30,"item":12}` 기본값이며 Goal 생성/설정에서 변경합니다. 0은 비활성화입니다. 실패/중단도 사용량으로 남고 서버 재시작/재작업으로 초기화하지 않습니다. 기존 검색 상한과 별도로 계산합니다. 요청 기록은 goal_reference_page에, 본문·출처·파일 해시 근거는 기존 goal_evidence에 저장합니다. 추가 LLM 호출·GPU 실행 비용은 없습니다.

도구 원문은 store에 보존합니다. 모델에는 최대 20,000자 본문과 경로/URL 메타데이터를 전달하며 전체 도구 JSON은 40,000자 이내로 유지합니다. 매우 긴 URL/메타데이터는 이미지 목록·페이지 부가 정보·본문을 명시적으로 줄이되 PNG 경로는 보존합니다. 웹 자료는 지시나 승인으로 취급하지 않으며, 읽은 사양은 출처 URL·페이지 번호·근거 ID와 함께 인용합니다. 치수·구조의 의미상 정확성과 사양 대응은 에이전트가 검토해야 합니다.

공식 API/의존성: [pypdfium2](https://pypdfium2.readthedocs.io/en/stable/python_api.html), [고정 패키지](https://pypi.org/project/pypdfium2/5.14.0/), [Pillow](https://pypi.org/project/pillow/13.0.0/). openai/modal 버전은 변경하지 않습니다.

오프라인 검사: `python -m unittest discover -s tests -p test_reference_pages.py`.

## 제품 프록시 SAM 3.1 분할

`POST /proxy/v1/sam/segment`는 Goal 전용 Bearer 프록시 토큰으로 인증하며 Meta의 `sam-3.1`만 호출합니다. Baseten/Modal 채팅 경로 선택과 독립적이고 실제 공급자 키는 제품에 전달하지 않습니다. `/proxy/v1/models`는 기존 채팅 모델 목록을 유지합니다. 기존 loopback·Origin 검사, 활성 Goal/마감, Meta 전체·일일·Goal 예산과 키 제거 규칙을 적용합니다. 중지·일시정지·아카이브한 Goal의 토큰은 사용할 수 없습니다.

요청은 `{"image":"renders/scene.png","prompt":"conveyor belt"}` 형태입니다. image에는 **현재 Goal 작업 폴더 안 상대 경로** 또는 PNG/JPEG/WebP/단일 프레임 GIF의 base64 data URI 한 장만 허용합니다. URL·절대 경로·상위 경로·비밀 파일·링크는 거부합니다. 이미지 5 MiB, JSON 요청 8 MiB, 디코딩 이미지 64메가픽셀, 프롬프트 1–512자입니다. 짧은 명사구를 권장합니다. 추가 모델/키/주소 인수는 허용하지 않습니다.

```python
import json, os
from urllib.request import Request, urlopen

request = Request(
    os.environ["MUSE_PROXY_BASE_URL"] + "/sam/segment",
    data=json.dumps({"image": "renders/scene.png", "prompt": "conveyor belt"}).encode("utf-8"),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + os.environ["MUSE_PROXY_TOKEN"]},
)
with urlopen(request, timeout=150) as response:
    result = json.load(response)
# result["segments"]: [{"id": ..., "box": [x1,y1,x2,y2], "score": ..., "mask": {...}}]
```

반환 `box`는 원본 이미지 픽셀의 xyxy(오른쪽/아래 경계 제외)입니다. `mask`는 공식 `meta-sam-parser==0.1.0`으로 해석한 **COCO compressed RLE**(`size: [height,width]`, `counts` 문자열)와 `bounds`입니다. 전체 이미지 크기의 마스크가 아니라 잘린 마스크이므로 bounds 안에 배치/리사이즈합니다. 신뢰도가 없으면 score는 null이며 검출 0개는 정상 빈 segments입니다. 미완료/손상 응답은 부분 마스크 대신 오류를 반환합니다. 응답·디코딩 크기 8 MiB, 최대 마스크 256개/총 마스크 영역 64메가픽셀 안전 상한을 적용합니다.

가격은 `goal_prices.json`의 `sam.usd_per_thousand_images=2.50`, **추정 $0.0025/장**입니다. ledger에 provider=meta, purpose=product, workload=segmentation, 이미지 수·해시·크기·추정 비용·검증 결과를 기록하며 Baseten 제품 예비분은 소비하지 않습니다. 이미지 원문/base64, 프롬프트, 마스크 원문은 로그·메모리·장부에 저장하지 않습니다. 일시 통신 오류의 기존 모델 재시도 규칙은 유지하고 각 시도별 비용을 기록합니다. usage가 없는 실패도 예약 추정액을 보수적으로 유지하므로 실제 청구액과 다를 수 있습니다.

공식 자료: [Meta SAM 이미지 분할](https://dev.meta.ai/docs/sam/segmenting), [출력 해석](https://dev.meta.ai/docs/sam/reading-segmentation), [SAM 3.1 가격](https://dev.meta.ai/models/sam-3-1), [공식 고정 파서](https://pypi.org/project/meta-sam-parser/0.1.0/). 확인일: 2026-11-01.

오프라인 검사: `python -m unittest discover -s tests -p test_sam_proxy.py`.

## Builder 장시간 로컬 명령

Goal Builder 전용 도구:

- `start_long_command(argv, cwd, timeout_s, label)`: 1–1800초의 명령을 별도 스레드/프로세스로 시작하고 즉시 id를 반환합니다. cwd는 일반 명령과 같은 상대 경로/루트 별칭 정규화·안전 검사를 합니다. label은 비밀값 없는 1–120자입니다. Goal당 동시에 1개만 실행할 수 있습니다.
- `check_long_command(id)`: 상태(queued/running/cancelling/succeeded/failed/timed_out/cancelled/interrupted), 경과 초, 출력 끝부분 최대 8,000자, 최종 결과·입출력 SHA-256·근거 ID·프록시 비용을 반환합니다. 확인은 재실행하지 않습니다.
- `cancel_long_command(id)`: 같은 Goal의 명령과 자식 프로세스를 취소합니다. 다른 Goal의 id로 조회/취소할 수 없습니다.

예: `start_long_command({"argv":["python","tools/evaluate.py","--episodes","30"],"cwd":".","timeout_s":1800,"label":"30 evaluation episodes"})`. 명령은 shell=False이며 일반 run_command와 같은 command 승인 창/Autonomous 허용 목록을 사용합니다. 작업 폴더 .py와 안전한 인수, pytest/unittest 등 기존 규칙을 넓히지 않습니다. 키 제거·PYTHONUTF8=1·PYTHONIOENCODING=utf-8 및 Python 실행 파일 매핑을 재사용합니다. 일반 Meta Work/Goal run_command의 120초 제한은 그대로입니다. 이 기능은 OS 샌드박스가 아니며 현재 Windows 사용자 권한으로 실행합니다.

실행 중 Builder는 독립적인 로컬 작업을 할 수 있습니다. 다른 일이 없으면 progress 보고로 묶음을 끝냅니다. 서버는 항목을 `waiting / long_command`로 표시하고 다른 독립 항목을 배정합니다. 같은 항목을 반복 호출하거나 Planner 검토로 넘기지 않고 모델 호출 없이 기다립니다. 완료 후 기존 묶음 한도 안에서 Builder가 결과를 확인하도록 이어 진행하며, 최대 묶음에 도달한 경우 기존 Planner 범위 검토를 사용합니다. 실행 중에는 해당 항목을 accept하거나 Goal을 done으로 끝내지 않습니다.

running 상태에서 저장한 Goal 설정은 장시간 명령도 활성 단계로 보고 완료 뒤 적용합니다. paused 상태에서 명시적으로 저장하는 설정의 기존 동작은 유지합니다.

완료 결과는 `goal_long_command`에 보존하고 원래 task에 일반 run_command 도구/결과 이벤트를 기록합니다. 기존 명령의 Python audit hook·정적 경로 힌트로 입력과 출력 해시를 기록하고, 기존 근거 catalog/stale 검사를 그대로 사용합니다. exit code·시간 초과·취소·출력 제한·경과 시간과 원문(키 제거 후)을 저장합니다. 출력 상한 4,000,000바이트는 기존 명령과 동일하며 초과 시 프로세스를 중단합니다. 모델에는 끝부분/요약만 전달합니다. 네이티브 라이브러리의 파일 IO는 기존과 같이 정적 힌트로 보완하므로 완전한 OS 파일 감사를 뜻하지 않습니다.

명령마다 별도 임시 프록시 토큰을 메모리에서 발급해 MUSE_PROXY_BASE_URL/TOKEN으로만 주입합니다. 실제 제공자 키·다른 Goal 토큰은 전달하지 않습니다. 토큰 원문은 저장하지 않습니다. 해당 토큰의 채팅/SAM 제품 호출은 ledger의 long_command_id/task_id/plan_item_id로 연결하고 check 결과 및 명령 근거의 proxy_cost에 제공자별 합계·장부 ID·미정산 예약액을 표시합니다. 토큰은 명령이 끝나면 폐기합니다. 일반 Goal 제품 토큰의 호출을 시간대만으로 이 명령에 배정하지 않습니다.

프록시가 예산 상한으로 거부하면 proxy_rejections/budget_refused를 결과에 남깁니다. 스크립트가 429를 잡아 exit 0으로 끝내도 이 실행은 성공 수용 근거가 아닙니다. Goal 일시정지는 명령을 계속 두며 새 모델/제품 프록시 호출은 기존 Goal 비활성 규칙으로 거부합니다. 예산 정지 후에도 프로세스는 최대 timeout 안에서 계속될 수 있으므로 스크립트에서도 429를 처리해야 합니다. 중지는 실행 중 명령을 취소합니다.

서버 종료/재시작은 실행 중 프로세스 트리를 종료하고 interrupted로 기록합니다. 자동 재실행하지 않습니다. Windows 기본 Job Object의 KILL_ON_JOB_CLOSE를 사용해 서버 프로세스가 비정상 종료돼도 연결된 프로세스를 종료합니다. 감독 설정 실패 시 실행을 중단하고 명확한 오류를 남깁니다. 다음 시작 시 남은 queued/running/cancelling 기록도 interrupted로 정리하고, 복구할 수 없는 exit code/출력은 성공으로 만들지 않습니다. 재시작 이전 출력의 전체 복구는 NOT VERIFIED입니다. 실행 중 장시간 명령이 있으면 Goal 아카이브/삭제를 거부합니다. watchdog도 이를 활성 단계로 인식합니다.

공식 Windows 동작: [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects), [KILL_ON_JOB_CLOSE](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information). 추가 패키지는 없습니다.

오프라인 검사: `python -m unittest discover -s tests -p test_long_commands.py`.

## 항목 승인 시 자동 로컬 커밋

Goal 설정 `auto_commit`은 기본 **true**이며 생성·설정 화면의 **항목 승인 시 자동 로컬 커밋**에서 변경합니다. 기존 Goal은 필드가 없어도 켬으로 해석하지만 읽기/시작만으로 설정을 마이그레이션하지 않습니다. 설정 변경은 기존 단계 경계 규칙을 따릅니다. manual/autonomous 명령 정책과 별개로, 이 옵션이 허용한 서버의 로컬 체크포인트이며 추가 승인 창이나 모델 호출은 없습니다. 이미 완료된 항목을 소급 커밋하지 않습니다.

Planner 검토가 서버 근거 검증을 통과해 accept된 뒤, 작업 폴더의 **기존 일반 Git 저장소**에 `[goal] <항목 제목> accepted (<항목 키>)` 형식으로 커밋합니다. 저장소를 자동 초기화하거나 부모 저장소/연결 worktree 경계를 넘지 않습니다. 사용자 Git config의 user.name/user.email이 모두 있어야 하며 설정을 생성/변경하거나 OS 사용자명으로 대체하지 않습니다. 푸시·원격 접속은 없으며 Git 훅/외부 콘텐츠 필터/GPG/자동 lazy fetch를 실행하지 않습니다. 서명·훅이 필수인 저장소에서는 끄고 수동 커밋하세요.

범위는 해당 항목의 change/command_output 근거에 경로·해시가 기록된 파일 및 필요한 .gitignore 보완입니다. 읽기만 한 파일, 다른 항목/사용자의 무관한 수정·미추적 파일은 포함하지 않습니다. 근거와 현재 SHA-256이 다르면 커밋하지 않습니다. Gitignore된 경로는 제외합니다. references/, .venv/, venv/, .env*, *.key/*.pem/*.pfx/*.p12, .workbench/, .ssh/.aws/.codex, 자격증명 파일, Python/Node 캐시 등 공개 제외 패턴이 저장소의 .gitignore에서 유효한지 확인하고 누락된 패턴을 끝에 추가합니다. 기존 내용은 보존하며 비공개 경로의 negation 예외도 검사합니다. 추가 패턴/해시/실패 이유는 Goal 기록에 남깁니다. 이미 추적 중인 비공개 경로는 자동으로 삭제/추적 해제하지 않고 커밋을 건너뛰어 경고합니다.

기존 사용자 스테이징을 포함하거나 덮어쓰지 않습니다. 해당 항목 파일 자체가 미리 스테이징돼 있으면 수동 처리하도록 전체 체크포인트를 건너뜁니다. 별도 인덱스와 비밀값 검사한 불변 파일 스냅샷으로 커밋 트리를 만들고, HEAD 비교/교환 및 인덱스 잠금으로 다른 커밋·스테이징과의 충돌을 방지합니다. 커밋 중 뒤늦게 생긴 작업 파일 수정도 덮어쓰지 않습니다. Builder나 같은 작업 폴더의 장시간 명령이 실행 중이면 건너뜁니다.

성공 해시는 plan_item.commit_hash, commit_records와 goal_commit 기록 및 git_commit 관찰 근거에 저장합니다. UI에 짧은 해시/전체 해시 툴팁과 경고를 표시합니다. 커밋 근거는 **체크포인트 사실만** 증명하며 테스트·문서·실행 수용 근거로 승격하지 않습니다. 같은 검토 ID의 재호출은 커밋하지 않고, 변경이 없으면 빈 커밋도 만들지 않습니다.

실패는 경고/NOTE만 남기고 accept·계획·예산·Goal 진행을 취소하지 않습니다. 전체 25초, 개별 Git 명령 최대 5초, 파일 최대 64 MiB/합계 128 MiB/200개 안전 상한입니다. 중단·재시작 중이던 체크포인트는 경고로 남기며 자동 재시도하지 않습니다. HEAD 갱신은 성공했으나 인덱스 교체가 실패하면 실제 커밋 해시와 복구용 .git/index.lock을 보존하고 수동 확인을 요청합니다. 기존 사용자 index.lock은 지우지 않습니다. 이 동작은 Git 히스토리 전체의 비밀값 정리나 공개 적합성/참고자료 라이선스 검증을 대신하지 않습니다.

오프라인 검사: `python -m unittest discover -s tests -p test_goal_commits.py`. 공식 Git 동작: [commit-tree](https://git-scm.com/docs/git-commit-tree), [update-index](https://git-scm.com/docs/git-update-index), [check-ignore](https://git-scm.com/docs/git-check-ignore).

실제 통합 점검에서 모델이 cwd에 JSON 문자열 `"\".\""`를 준 오류를 보완했다. 양쪽 따옴표가 일치하고 내부가 기존 루트 별칭 또는 `.`인 경우만 루트로 정규화한다. 일반 경로의 따옴표는 제거하지 않으며 절대 경로·상위 경로·비밀 경로 검사는 유지한다. 그 밖의 잘못된 Windows 경로 문법은 실행 전에 영어 ValueError로 안내한다.
# Builder productivity: scoped edits, code map, bounded reading

Goal Builder adds `edit_file(path, old_str, new_str)` for existing UTF-8 files. It requires a read_file/read_structured snapshot from the **same chunk**, checks the current full-file hash internally, and replaces one unique exact occurrence. Missing/ambiguous matches return the count and up to 20 line/column locations; overlapping matches count separately. External changes, secret paths/content and PROVISIONAL core-spec edits remain forbidden. Backups, full change diffs, prior/result hashes and change evidence follow write_file. New files/full replacements still use write_file.

`code_map(paths?)` statically parses Python AST signatures, first docstring lines and line numbers, plus JSON/YAML top-level structure. It never imports/executes workspace code. Results are cached by workspace/path/content hash and limited to 20,000 JSON characters; automatic Builder context includes an 8,000-character relevant-folder map. Scan bounds: 200 candidate files, 2 MB total readable source, 2,000 directory entries and depth 12. Omissions/syntax failures are explicit. Protected/link paths and registered secrets are excluded. Cache records are private state, not shared memory or source-package files.

Goal `read_loop_limits` defaults to `{ "warn": 8, "block": 12 }` (validated `1 <= warn <= block <= 100`). The creation/settings form exposes both thresholds. Each chunk counts consecutive read_file/read_structured/search_files/list_files/code_map calls. The 8th default result carries an action notice; 12 reads are allowed and later reads are refused until a real write/command/action occurs. Failed/denied writes and no-op edits do not reset it. Approvals, budget limits, scoped paths and key removal still apply; dummy edits are forbidden by the prompt.

Read-only chunks are stored as `goal_chunk_summary.status=stalled` with task status, execution cycle, read count and observed-action flag; actual task status is preserved. The next chunk sees the prior summary/read ranges, stalled instruction and map. Provider errors, approval waits and active long commands are not treated as read stalls. Two stalled implementation chunks request one logical Planner scope reduction before a human decision: 1–4 bounded children with explicit read/write/argv hints. Invalid scope JSON gets one correction request with the exact error. Parent criteria/counters/evidence stay intact, and parent acceptance is reviewed after children complete. Children inherit the once-only reduction guard, preventing endless split chains; further stalls escalate. A claimed scope request interrupted by restart is not replayed automatically. Legitimate completed read-only/document verification can still enter normal evidence review.

Optional plan hints: files_to_read (maximum 5 relative paths), files_to_write (20), commands_to_run (20 argv arrays). They survive plan updates and reach ASSIGN/Builder context, but never authorize execution. Planner dependencies must describe actual output consumption, rather than arbitrary tier ordering. New private records: goal_chunk_state, goal_stall_replan, code_map_cache; no new HTTP execution endpoint or provider/model change.


Data/configuration files (YAML, JSON, TOML, CSV) are checked for current file evidence, syntax and required keys, without prose value citations. Planner classifies these deliverables as `execution` with check-script evidence; `document` is for prose reports. Human decision updates may change exact-text `acceptance_rules` through the validated plan update path, with an audit record.

Goal execution display joins GPU/Builder/long-command history through a read-only index; inferred ownership never changes execution policy or evidence validity. `/api/execution-index` reports association counts. Local PNG artifacts up to 5 MiB support `GET /api/artifact?...&preview=1`; normal download behavior remains.


Document grounding: `doc_role` is `results`, `reference`, `design` or `static`; missing roles are inferred by filename/format and the inference is recorded in each review validation. Reference/design facts may cite current reference observation IDs or `(spec:spec/irm1.yaml#exact.key)`; exact key/value and current data hash are checked. Result/performance/cost/test claims still require successful execution grounding. Static LICENSE/ATTRIBUTION documents skip factual citations but retain current-hash and required-section checks. Known Apache-2.0, MIT and BSD-3-Clause complete core bodies are recognized offline using official SPDX normalized fingerprints. Planned storyboard timing is authored intent, not measured execution.

`max_plan_items` is Goal-configurable (1–200, default 40, missing legacy values use 40). A blocked split records a capacity warning to Planner and parks only that item without Builder rework or a human decision; other work continues. When only capacity-blocked work remains, the Goal pauses without model polling. Increasing the configured capacity releases eligible item waits; it does not delete historical completed items or automatically resume a user-paused Goal. Results documents should depend on their producing execution/evaluation items.

Apache-2.0 identification also recognizes its official SPDX standard license header, reported separately as `standard_header`; this identifies the declaration and does not claim the full terms were bundled. An SPDX identifier or title alone is not recognized.
### Human-facing decision summaries

`human_display_language` defaults to `Korean` (`English` is also supported) independently of artifact `output_language`. The same Planner `request_decision` call supplies a validated `human_brief`; one correction is allowed, then a deterministic server template is used without a separate summarizer call. Automatic platform decisions and legacy pending decisions receive templates. Cause, resolution limits, grouped defects, choice effects, affected items and attributed item spend/batches appear before collapsed original data. Platform validation errors are marked uncertain and may need a platform fix. Original agent messages, decision options/answers/states and evidence rules stay unchanged. Pending briefs are regenerated from stored facts when the human display language changes. Goal notifications use the selected human language and a redacted brief; Meta Work notification behavior remains unchanged.

### Human inbox and explicit decision submission

Decision options select only; the **Send answer** button submits the chosen option, free text, or both. The server preserves `selected_option` and `answer_text` and supplies the combined `answer` to the Planner. An inline preview shows the exact message and an in-flight guard prevents duplicate submissions. Pending-state validation remains atomic on the server.

The pinned **Human → Planner** composer uses `POST /api/goals/message` with `goal_id`, `kind` (`note|instruction|interrupt`), `text`, optional `plan_item_id`, an idempotent `request_id`, and explicit `cancel_gpu`/`cancel_long` flags (interrupt only). This endpoint uses the existing loopback/session-token boundary. Messages and ACK processing state are returned in Goal detail; directive records never answer or withdraw decisions.

Notes/instructions ride the next scheduled initial plan, dependency plan, scope-reduction plan or review call. Targeted instruction originals also appear in that item's next Builder input. Interrupts stop the Builder at its next tool boundary after an in-flight tool finishes, retain observed progress and skipped-call markers, then make one logical immediate Planner request. Existing provider retry/length-continuation and ledger rules remain in force. GPU/long commands continue unless explicitly cancelled by the human; the checkboxes target this Goal's running work. A paused/interrupted/blocked Goal stores directions until explicit resume. Notes/instructions while waiting for a decision remain pending until a normal planning point; interrupt is the immediate replan option.

Planner `human_actions` is validated and applied through existing plan/item-action validation, with audit records. ACK lists applied items/directions and unmet parts in `human_display_language`. Invalid or missing ACK/update output yields a visible partial result without an extra validation call or a fabricated decision. Processing interrupted by restart is marked partial and never silently replayed. Approval, budgets, workspace/secret protection, evidence and push/publication prohibitions continue to apply. A Planner ACK is not acceptance of execution evidence.

A note arriving after the final scheduled review never blocks completion or triggers a new call: a server ACK records that it was not consulted. If an instruction arrives with no scheduled work remaining, it stays pending and the Goal pauses with an explicit reason; sending an interrupt and resuming requests the immediate planning path.

## Scheduler liveness

Goal GPU artifact recovery accepts jobs submitted by the current item, its direct parent/children or siblings (including root siblings and recorded split relationships) within the same Goal/workspace. Other Goals and unrelated branches are refused. copy_gpu_artifact records source_job_id, submitting_item_id, copying_item_id and source/destination SHA-256. Equal destination bytes produce verified_present proof without a write/backup or expected_sha256; replacing different bytes still requires the current hash. The Reviewer rechecks source/destination bytes and provenance. This is file-recovery evidence, not a new render/test result; existing execution acceptance requirements remain unchanged. Goal read_gpu_artifact uses the same ownership boundary and the existing 200 KB UTF-8 limit.

Wait reasons are labels, not permanent locks. On coordinator ticks and plan updates, waiting leaves must have an unfinished dependency/child, a pending decision, an active GPU job or an active long command. A resolved trigger wakes the item; explicit retry/reset/rework answers open one fresh bounded cycle and preserve counters in history, evidence, task IDs and commits. An older answer never resets a later human-approved cycle. Unknown legacy dependency metadata is never assumed safe. Parent items still use child-evidence review without a Builder.

The no-runnable check follows dispatch and re-reads current state. In-progress/orphan items, queued Builders, completed but not yet collected Builder results, GPU jobs and long commands count as live work. Orphan in-progress leaves are made ready for ordinary dispatch. Dead-end repairs are audited in goal_schedule_issue; they do not start commands, Builders or GPU jobs. Dependency cycles (including implicit parent/child waits) receive one claimed-once Planner repair; only failure creates a human decision. No polling model calls are made for unresolved waits.

