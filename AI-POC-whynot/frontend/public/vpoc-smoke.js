/* vpoc1 Gate 1 테스트 페이지 스크립트 (dev 전용).

   앱 화면과 독립이다. app.js 의 전역 헬퍼를 쓰지 않는다 — 이 페이지가 깨져도 앱은 멀쩡하고,
   앱이 깨져도 이 페이지로 영상 경로를 확인할 수 있어야 한다.

   인증 토큰은 앱과 같은 sessionStorage 키를 읽는다. 앱에 로그인한 탭에서 열어야 한다. */
(function () {
  "use strict";

  var TOKEN_KEY = "toon-change-ai-pj:token";
  var POLL_MS = 5000;

  var runButton = document.getElementById("smokeRun");
  var failButton = document.getElementById("smokeFail");
  var statusLine = document.getElementById("smokeStatus");
  var errorBox = document.getElementById("smokeError");
  var videoTag = document.getElementById("smokeVideo");
  var promptBox = document.getElementById("smokePrompt");
  var authLine = document.getElementById("smokeAuth");

  var timer = null;
  var startedAt = 0;

  function token() {
    try {
      return sessionStorage.getItem(TOKEN_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function showAuth() {
    if (token()) {
      authLine.textContent = "로그인 토큰을 찾았습니다.";
      return true;
    }
    authLine.textContent =
      "로그인 토큰이 없습니다. 같은 브라우저에서 앱에 먼저 로그인한 뒤 이 페이지를 새로고침해 주세요.";
    return false;
  }

  function mmss(seconds) {
    var minutes = Math.floor(seconds / 60);
    var rest = seconds % 60;
    return minutes + ":" + (rest < 10 ? "0" : "") + rest;
  }

  function setBusy(busy) {
    runButton.disabled = busy;
    failButton.disabled = busy;
  }

  function showError(payload) {
    errorBox.hidden = false;
    errorBox.textContent = JSON.stringify(payload, null, 2);
  }

  function clearOutput() {
    errorBox.hidden = true;
    errorBox.textContent = "";
    videoTag.hidden = true;
    videoTag.removeAttribute("src");
    promptBox.textContent = "";
  }

  function call(path, options) {
    var init = options || {};
    init.headers = init.headers || {};
    init.headers["Authorization"] = "Bearer " + token();
    if (init.body) init.headers["Content-Type"] = "application/json";
    return fetch(path, init).then(function (response) {
      return response
        .json()
        .catch(function () {
          return { ok: false, error: { where: path, type: "NonJsonResponse", message: "HTTP " + response.status, httpStatus: response.status } };
        })
        .then(function (payload) {
          if (!payload || payload.ok !== true) {
            throw payload && payload.error ? payload.error : { where: path, type: "UnknownError", message: "HTTP " + response.status };
          }
          return payload;
        });
    });
  }

  function poll(id) {
    call("/api/vpoc/smoke/" + encodeURIComponent(id))
      .then(function (payload) {
        var elapsed = Math.max(payload.elapsedS || 0, Math.round((Date.now() - startedAt) / 1000));
        if (payload.status === "completed" && payload.videoUrl) {
          statusLine.textContent = "완료 · " + mmss(elapsed);
          videoTag.src = payload.videoUrl;
          videoTag.hidden = false;
          setBusy(false);
          return;
        }
        if (payload.status === "failed") {
          statusLine.textContent = "실패 · " + mmss(elapsed);
          showError(payload.error || { message: "원문 없음" });
          setBusy(false);
          return;
        }
        statusLine.textContent =
          "영상 생성 중 · " + mmss(elapsed) + " · 보통 2~5분" + (elapsed > 600 ? " · 평소보다 오래 걸려요" : "");
        timer = setTimeout(function () {
          poll(id);
        }, POLL_MS);
      })
      .catch(function (error) {
        statusLine.textContent = "조회 실패";
        showError(error);
        setBusy(false);
      });
  }

  function start(body) {
    if (!showAuth()) return;
    clearOutput();
    setBusy(true);
    startedAt = Date.now();
    statusLine.textContent = "제출 중...";
    if (timer) clearTimeout(timer);
    call("/api/vpoc/smoke", { method: "POST", body: JSON.stringify(body) })
      .then(function (payload) {
        if (payload.promptChars !== undefined) {
          promptBox.textContent = "프롬프트 " + payload.promptChars + "자";
        }
        if (payload.status === "failed") {
          statusLine.textContent = "제출이 거절됐습니다 (과금 없음)";
          showError(payload.error || { message: "원문 없음" });
          setBusy(false);
          return;
        }
        statusLine.textContent = "영상 생성 중 · 0:00 · 보통 2~5분";
        poll(payload.id);
      })
      .catch(function (error) {
        statusLine.textContent = "제출 실패";
        showError(error);
        setBusy(false);
      });
  }

  runButton.addEventListener("click", function () {
    start({});
  });
  failButton.addEventListener("click", function () {
    // 일부러 잘못된 멱등 키를 보내 AWS 원문 오류를 확인한다. API 검증 계층에서 거절되므로
    // 작업이 만들어지지 않고 과금도 없다. 빈 프롬프트는 쓰지 않는다 — 그것은 거절되지 않고
    // 작업으로 들어가서 1초 뒤 Failed 가 된다(과금 위험).
    start({ errorTest: true });
  });

  showAuth();
})();
