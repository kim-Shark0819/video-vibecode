FROM nginxinc/nginx-unprivileged:1.27.5-alpine

# 배포 태그를 자산 URL에 심어 브라우저 캐시를 무효화한다.
ARG ASSET_VERSION=dev

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY public/ /usr/share/nginx/html/

USER root
# index.html 뿐 아니라 app.js 도 치환한다.
#
# app.js 에 배포 버전을 심어야 오류 리포트에 버전이 함께 남는다. 그것이 없으면
# "고쳤는데 재현된다"는 상황에서 사용자가 옛 코드를 실행 중인지 새 코드에서 나는 오류인지
# 구분할 방법이 없다. 실제로 그 구분이 안 되어 원인 확정에 며칠이 걸렸다.
#
# 치환 실패를 빌드에서 끊는다. 조용히 넘어가면 화면은 정상으로 보이지만 모든 리포트가
# 리터럴 "__ASSET_VERSION__" 을 달고 나와 진단에 쓸 수 없게 된다.
# vpoc-smoke.html 도 치환 대상이다. Gate 1 테스트 페이지가 자기 스크립트·스타일을
# 버전 쿼리로 부르므로, 치환이 빠지면 배포 후에도 옛 파일이 캐시에서 나온다.
RUN set -eu; \
    for f in /usr/share/nginx/html/index.html /usr/share/nginx/html/app.js \
             /usr/share/nginx/html/vpoc-smoke.html; do \
      sed -i "s/__ASSET_VERSION__/${ASSET_VERSION}/g" "$f"; \
      if grep -q "__ASSET_VERSION__" "$f"; then \
        echo "ASSET_VERSION placeholder not replaced in $f"; exit 1; \
      fi; \
    done; \
    grep -q "^const APP_VERSION" /usr/share/nginx/html/app.js || \
      { echo "app.js 에 APP_VERSION 선언이 없습니다. 오류 리포트에 버전이 빠집니다."; exit 1; }; \
    grep -q "${ASSET_VERSION}" /usr/share/nginx/html/app.js || \
      { echo "app.js 에 버전 문자열이 심기지 않았습니다."; exit 1; }; \
    echo "asset version: ${ASSET_VERSION}"
USER 101

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/health || exit 1
