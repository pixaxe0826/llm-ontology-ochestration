"""One Meta model-list request and at most one short inference; no retries."""

import logging
import os
import re

logging.disable(logging.CRITICAL)

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI


def safe_print(value):
    text = str(value)
    for name in (
        "MODEL_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "MODAL_PROXY_TOKEN"
    ):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    print(text, flush=True)


def select_model(model_ids):
    candidates = [
        model_id for model_id in model_ids
        if re.fullmatch(r"muse-spark-\d+(?:[.-]\d+)*", model_id, re.IGNORECASE)
    ]
    if candidates:
        return max(candidates, key=lambda name: tuple(map(int, re.findall(r"\d+", name))))
    if "muse-spark" in model_ids:
        return "muse-spark"
    return None


def main():
    if not os.environ.get("MODEL_API_KEY", "").strip():
        safe_print("NOT RUN: MODEL_API_KEY가 설정되지 않았습니다.")
        return 2

    stage = "모델 목록 조회"
    try:
        with OpenAI(
            base_url="https://api.meta.ai/v1",
            api_key=os.environ["MODEL_API_KEY"],
            max_retries=0,
            timeout=60.0,
        ) as client:
            # Read only this response's data; do not trigger pagination requests.
            model_ids = sorted({model.id for model in client.models.list().data})
            safe_print("Meta API 모델 ID 목록:")
            for model_id in model_ids:
                safe_print("  " + model_id)
            if not model_ids:
                safe_print("  (목록이 비어 있습니다.)")

            model = select_model(model_ids)
            if model is None:
                safe_print("FAIL: 반환 목록에 버전이 있는 muse-spark 모델이 없습니다.")
                return 1
            safe_print("선택한 최신 Muse Spark: " + model)
            stage = "짧은 응답"
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with only the word OK."}],
                reasoning_effort="minimal",
                max_completion_tokens=512,
            )
            content = response.choices[0].message.content if response.choices else None
            if not content or not content.strip():
                safe_print("FAIL: API 호출은 성공했으나 텍스트 응답이 없습니다. 자동 재시도하지 않습니다.")
                return 1
            safe_print("응답: " + content.strip())
            safe_print("PASS: Meta 모델 목록 및 짧은 응답 확인 완료.")
            return 0
    except APIStatusError as exc:
        hints = {
            400: "요청 파라미터를 확인하세요.",
            401: "키의 유효성을 Meta 대시보드에서 확인하세요.",
            403: "프로젝트 및 모델 접근 권한을 확인하세요.",
            404: "API 경로 또는 모델 접근 가능 여부를 확인하세요.",
            429: "할당량 또는 요금 상태를 확인하세요.",
        }
        safe_print(f"FAIL: {stage}, HTTP {exc.status_code}. " + hints.get(exc.status_code, "서비스 상태를 확인하세요."))
    except APITimeoutError:
        safe_print(f"FAIL: {stage}, 요청 시간 초과. 자동 재시도하지 않습니다.")
    except APIConnectionError:
        safe_print(f"FAIL: {stage}, 네트워크/TLS 연결 실패. 자동 재시도하지 않습니다.")
    except Exception:
        # Do not print exception bodies, headers, tracebacks, or credentials.
        safe_print(f"FAIL: {stage}, 응답 처리 오류. 키 보호를 위해 원문 오류는 출력하지 않습니다.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
