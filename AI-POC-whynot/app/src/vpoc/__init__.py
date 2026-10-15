"""vpoc1 — 와이낫 영상 기능 1단계 (Luma Ray2).

정본: `AI-POC-whynot/docs/vpoc1/PRD.md`

구성:
  settings.py  환경변수 (PRD 6.8)
  errors.py    오류 봉투 (PRD 6.7)
  store.py     S3 상태·조건부 쓰기·키 규칙 (PRD 6.3 · 6.4)
  compose.py   영어 프롬프트 조립과 코드 검사 (PRD 6.5) — 모델 호출 없음
  ledger.py    비용 원장과 상한 (PRD 6.6)
  video.py     provider 경계 + Luma Ray2 (PRD 6.9)
  images.py    업로드 정규화 · 타일 · SD3.5 캐릭터 이미지
  llm.py       Claude analyze · plan (부록 D)
  routes.py    라우트 8개 (PRD 7장)

server.py 는 등록 한 곳만 바꾼다. 플랫폼 파일(gateway·graph_store·prompt_layers·
model_registry·stitcher)은 수정하지 않는다.
"""

from .routes import VpocDependencies, create_blueprint

__all__ = ["VpocDependencies", "create_blueprint"]
