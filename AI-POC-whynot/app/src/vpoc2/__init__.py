"""vpoc2 — 명령 → 캐릭터 확정 → 15초 영상 → 리뷰. 명세: docs/spec-v1.md

server.py 등록 (한 곳):
    import vpoc2
    app.register_blueprint(vpoc2.create_blueprint(vpoc2.Vpoc2Dependencies(
        require_auth=require_auth, current_username=lambda: g.username)))
"""
from .routes import Vpoc2Dependencies, create_blueprint

__all__ = ["Vpoc2Dependencies", "create_blueprint"]
