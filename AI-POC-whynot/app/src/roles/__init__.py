"""역할 모듈. 각 역할 = Bedrock 호출 1개 = system 프롬프트 1개.

## 규칙 (지시서 1절)

1. 역할은 `gateway` 만 호출한다. boto3 를 직접 만지지 않는다 — 그러면 사용량 기록을
   우회하는 호출 지점이 생긴다.
2. **역할끼리 서로를 부르지 않는다.** 호출 순서는 `story_router` 만 안다.
3. 역할은 컨텍스트 팩을 받고 자기 결과를 돌려준다. 팩을 직접 고치지 않는다
   (라우터가 합친다). 그래야 어느 역할이 무엇을 넣었는지 추적된다.
4. 대화 히스토리를 공유하지 않는다. 매 호출이 독립이다.

## 호출자 규약

역할은 `caller` 를 인자로 받는다.

    caller(system: str, content: list[dict], max_tokens: int, temperature: float)
        -> tuple[str, bool]   # (본문, 잘렸는지)

`server.py` 가 `gateway.converse` 를 감싸 만들어 넘긴다. 이 모듈들이 모델 ID 나 리전을
알지 못하게 하려는 것이다. 그 값은 `story_models` 표가 단독으로 갖는다.
"""

from . import bible, validator, vision, writer  # noqa: F401

__all__ = ["vision", "bible", "writer", "validator"]
