"""List accessible Modal endpoint IDs once; request at most one short reply."""

import argparse
import json
import logging
import os
import re

logging.disable(logging.CRITICAL)

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI


def safe_print(value):
    text = str(value)
    for name in ("MODEL_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "MODAL_PROXY_TOKEN"):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    print(text, flush=True)


def main(model_override=None):
    if not os.environ.get("MODAL_PROXY_TOKEN", "").strip():
        safe_print("NOT RUN: MODAL_PROXY_TOKEN이 설정되지 않았습니다.")
        return 2
    stage = "엔드포인트 목록 조회"
    try:
        with OpenAI(
            base_url="https://inference.us-west.modal.direct/v1",
            api_key=os.environ["MODAL_PROXY_TOKEN"],
            max_retries=0,
            timeout=60.0,
        ) as client:
            models = client.models.list().data
            model_ids = sorted({model.id for model in models})
            safe_print("Modal /v1/models 엔드포인트 ID 목록:")
            for model_id in model_ids:
                safe_print("  " + model_id)
            if not model_ids:
                safe_print("  (목록이 비어 있습니다.)")
                safe_print("PASS: 목록 조회 성공. Muse Spark Open 엔드포인트 없음.")
                return 0

            if model_override:
                if model_override not in model_ids:
                    safe_print("FAIL: 지정한 엔드포인트가 /v1/models 목록에 없습니다.")
                    return 1
                model_id = model_override
            else:
                candidates = [
                    model.id for model in models
                    if re.search(
                        r"muse[\s_.-]*spark[\s_.-]*open",
                        json.dumps(model.model_dump(), ensure_ascii=False),
                        re.IGNORECASE,
                    )
                ]
                if not candidates:
                    safe_print("Muse Spark Open으로 식별되는 엔드포인트 없음. 추론은 실행하지 않습니다.")
                    safe_print("사용자 지정 호스트명만 반환된 경우 모델 종류는 NOT VERIFIED입니다. 확인한 ID는 --model로 지정할 수 있습니다.")
                    return 0
                model_id = sorted(candidates)[0]

            safe_print("선택한 엔드포인트: " + model_id)
            stage = "짧은 응답"
            response = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": "Reply with only the word OK."}],
                max_completion_tokens=512,
            )
            content = response.choices[0].message.content if response.choices else None
            if not content or not content.strip():
                safe_print("FAIL: API 호출은 성공했으나 텍스트 응답이 없습니다. 자동 재시도하지 않습니다.")
                return 1
            safe_print("응답: " + content.strip())
            safe_print("PASS: Modal 엔드포인트 짧은 응답 확인 완료.")
            return 0
    except APIStatusError as exc:
        safe_print(f"FAIL: {stage}, HTTP {exc.status_code}. 토큰 권한·엔드포인트 상태를 확인하세요. 원문 오류는 출력하지 않습니다.")
    except APITimeoutError:
        safe_print(f"FAIL: {stage}, 요청 시간 초과. 자동 재시도하지 않습니다.")
    except APIConnectionError:
        safe_print(f"FAIL: {stage}, 네트워크/TLS 연결 실패. 자동 재시도하지 않습니다.")
    except Exception:
        safe_print(f"FAIL: {stage}, 응답 처리 오류. 키 보호를 위해 원문 오류는 출력하지 않습니다.")
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="확인된 Muse Spark Open 엔드포인트 ID (반환 목록에 있어야 함)")
    args = parser.parse_args()
    raise SystemExit(main(args.model))
