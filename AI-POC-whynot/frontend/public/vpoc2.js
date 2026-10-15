/* vpoc2 — 명령 → 해석 → 캐릭터 확정 → 15초 영상 → 리뷰. 모든 DOM 은 #vpoc2-root 안에서 만든다. */
(function () {
  "use strict";

  var TOKEN_KEY = "toon-change-ai-pj:token";
  var API = "/api/vpoc2";
  var STEPS = ["명령", "해석", "캐릭터", "15초 영상", "리뷰"];
  var ELEMENT_KINDS = { who: "인물", object: "소품", place: "장소", time: "시간·날씨", action: "동작", change: "변화",
    relation: "관계", mood: "분위기", camera: "카메라", style: "화풍", other: "기타" };

  var root = document.getElementById("vpoc2-root");
  var state = { meta: null, runs: [], run: null, draft: null, reviewDraft: null, timer: null, busy: false,
    page: "home", reviews: null, error: null };

  // --- 유틸 ------------------------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function token() { try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; } }
  function api(method, path, body) {
    var headers = { "Content-Type": "application/json" };
    var t = token();
    if (t) headers.Authorization = "Bearer " + t;
    return fetch(API + path, { method: method, headers: headers, body: body ? JSON.stringify(body) : undefined })
      .then(function (r) {
        return r.json().catch(function () { return { ok: false, error: { type: "HTTP" + r.status, message: r.statusText } }; })
          .then(function (j) {
            if (!r.ok || j.ok === false) {
              var e = new Error((j.error && j.error.message) || r.statusText);
              e.envelope = j.error || { type: "HTTP" + r.status, message: r.statusText };
              e.status = r.status;
              throw e;
            }
            return j;
          });
      });
  }
  function errBox(env) {
    if (!env) return "";
    return '<div class="vpoc2-err">✖ ' + esc(env.where || "") + " · " + esc(env.type || "") + "\n" + esc(env.message || "") +
      (env.requestId ? "\nrequestId " + esc(env.requestId) : "") + "</div>";
  }
  function fail(e) {
    state.busy = false;
    state.error = e.envelope || { type: "Error", message: String(e.message || e) };
    if (e.status === 401) state.error.message = "로그인이 필요합니다. 와이낫 메인 화면에서 로그인한 뒤 이 페이지를 다시 여세요.";
    render();
  }
  function mmss(s) { s = Math.max(0, s | 0); return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2); }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }

  // --- 라우팅 ----------------------------------------------------------------
  function parseHash() {
    var h = location.hash.replace(/^#/, "");
    if (h.indexOf("run=") === 0) return { page: "run", id: h.slice(4) };
    if (h === "reviews") return { page: "reviews" };
    return { page: "home" };
  }
  function go(hash) { location.hash = hash; }
  window.addEventListener("hashchange", load);

  function load() {
    var r = parseHash();
    state.page = r.page;
    state.error = null;
    clearTimeout(state.timer);
    var metaP = api("GET", "/meta").then(function (j) { state.meta = j; });
    if (r.page === "run") {
      Promise.all([metaP, api("GET", "/runs/" + encodeURIComponent(r.id))]).then(function (x) {
        setRun(x[1].run, true);
      }).catch(fail);
    } else if (r.page === "reviews") {
      Promise.all([metaP, api("GET", "/reviews")]).then(function (x) { state.reviews = x[1]; render(); }).catch(fail);
    } else {
      Promise.all([metaP, api("GET", "/runs")]).then(function (x) { state.runs = x[1].runs; render(); }).catch(fail);
    }
  }

  function setRun(run, resetDraft) {
    var prev = state.run;
    state.run = run;
    if (resetDraft || !state.draft || !prev || prev.interpretation.data !== run.interpretation.data &&
        JSON.stringify(prev.interpretation.data) !== JSON.stringify(run.interpretation.data)) {
      state.draft = run.interpretation.data ? clone(run.interpretation.data) : null;
      state.draftDirty = false;
    }
    if (!state.reviewDraft || resetDraft) {
      state.reviewDraft = run.review ? clone(run.review) : { score: 0, elements: {}, goodKo: "", badKo: "", author: "" };
    }
    state.busy = false;
    render();
    schedule();
  }

  function schedule() {
    clearTimeout(state.timer);
    var run = state.run;
    if (!run || state.page !== "run") return;
    var delay = 0;
    if (run.task.status === "running") delay = 1500;
    else if (run.video.status === "running" || run.video.status === "partial" &&
             run.video.shots.some(function (s) { return s.status === "queued" || s.status === "submitted"; })) delay = 4000;
    else if (run.video.status === "done" && run.video.final.status !== "ready" && run.task.status !== "error") delay = 2000;
    if (!delay) return;
    state.timer = setTimeout(function () {
      if (document.hidden) { schedule(); return; }
      api("GET", "/runs/" + run.id).then(function (j) { setRun(j.run, false); }).catch(fail);
    }, delay);
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) schedule(); });

  function act(method, path, body) {
    state.busy = true;
    state.error = null;
    render();
    return api(method, "/runs/" + state.run.id + path, body).then(function (j) { setRun(j.run, false); return j; }).catch(fail);
  }

  // --- 화면: 공통 ---------------------------------------------------------------
  function topBar() {
    var m = state.meta || {};
    var hq = m.hq || {};
    return '<div class="vpoc2-top"><div><h1><a class="vpoc2-list" href="#">와이낫 영상 v2</a></h1>' +
      '<div class="vpoc2-meta">15초 = 5초 × 3샷 · 테스트 540p · 720p 이번 달 남은 ' + esc(hq.remaining) + "/" + esc(hq.limit) +
      "회 " + (m.mock ? '<span class="vpoc2-badge mock">목 모드 — 모델 호출 없음</span>' : "") +
      '</div></div><div class="vpoc2-row" style="margin:0"><button class="vpoc2-btn" data-act="reviews">리뷰 모음</button>' +
      '<button class="vpoc2-btn primary" data-act="new">새 영상 만들기</button></div></div>';
  }

  function stepsBar(run) {
    var html = '<div class="vpoc2-steps">';
    var stale = { 2: run.interpretation.status === "stale", 3: run.characters.status === "stale", 4: run.video.status === "stale" };
    STEPS.forEach(function (name, i) {
      var n = i + 1;
      var cls = n === run.step ? "on" : n > run.maxStep ? "off" : "";
      if (stale[n]) cls += " stale";
      html += '<button class="vpoc2-step ' + cls + '" data-act="back" data-step="' + n + '"' + (n > run.maxStep ? " disabled" : "") +
        ">" + ["①", "②", "③", "④", "⑤"][i] + " " + name + "</button>";
    });
    return html + "</div>";
  }

  var TASK_TEXT = { interpret: "AI가 명령을 해석하고 15초 연출안을 만들고 있어요 · 보통 20~60초",
    draw: "캐릭터를 그리고 있어요 · 보통 10~40초", prepare: "확정 이미지를 보고 샷 프롬프트를 만들고 있어요 · 보통 10~30초",
    stitch: "샷 3개를 15초 영상으로 합치고 있어요" };

  function band(run) {
    var t = run.task;
    var html = "";
    if (t.status === "running") {
      var el = Math.floor((Date.now() - Date.parse(t.startedAt)) / 1000);
      html += '<div class="vpoc2-band"><span class="vpoc2-status run">⟳ ' + esc(TASK_TEXT[t.name] || t.name) + " · " + mmss(el) + "</span></div>";
    } else if (t.status === "error") {
      html += '<div class="vpoc2-band">' + errBox(t.error) + "</div>";
    }
    if (state.error) html += '<div class="vpoc2-band">' + errBox(state.error) + "</div>";
    return html;
  }

  // --- 화면: 목록 · 리뷰 모음 ------------------------------------------------------
  function homePage() {
    var rows = state.runs.map(function (r) {
      return "<tr><td><a data-act=\"open\" data-id=\"" + esc(r.id) + "\">" + esc(r.titleKo || "(제목 없음)") + "</a></td><td>" +
        esc(STEPS[(r.step || 1) - 1]) + "</td><td>" + esc(r.videoStatus) + "</td><td>" + (r.score ? "★" + r.score : "") +
        "</td><td>" + esc((r.updatedAt || "").replace("T", " ").slice(0, 16)) + "</td></tr>";
    }).join("");
    return topBar() + '<div class="vpoc2-card vpoc2-list"><h2>내 영상 작업</h2>' +
      (rows ? '<table class="vpoc2-table"><tr><th>제목</th><th>단계</th><th>영상</th><th>점수</th><th>수정</th></tr>' + rows + "</table>"
        : '<p class="vpoc2-muted">아직 작업이 없습니다. [새 영상 만들기]로 시작하세요.</p>') + "</div>" +
      (state.error ? errBox(state.error) : "");
  }

  function reviewsPage() {
    var r = state.reviews || { items: [] };
    var rows = r.items.map(function (x) {
      return "<tr><td>★" + x.score + "</td><td>" + (x.reflectionRate == null ? "" : Math.round(x.reflectionRate * 100) + "%") +
        "</td><td>" + esc(x.command) + "</td><td>" + esc(x.goodKo) + "</td><td>" + esc(x.badKo) + "</td><td>" + esc(x.author) +
        " · " + esc(x.resolution) + "</td></tr>";
    }).join("");
    return topBar() + '<div class="vpoc2-card"><h2>리뷰 모음</h2><p>리뷰 ' + r.count + "건 · 평균 점수 " +
      (r.avgScore == null ? "-" : r.avgScore) + " · 평균 명령 반영률 " +
      (r.avgReflectionRate == null ? "-" : Math.round(r.avgReflectionRate * 100) + "%") +
      '</p><p class="vpoc2-muted">명령 반영률 = 명령 요소 중 "반영됨"으로 고른 비율</p>' +
      (rows ? '<table class="vpoc2-table"><tr><th>점수</th><th>반영률</th><th>명령</th><th>좋은 점</th><th>아쉬운 점</th><th>작성</th></tr>' +
        rows + "</table>" : '<p class="vpoc2-muted">아직 리뷰가 없습니다.</p>') + "</div>" + (state.error ? errBox(state.error) : "");
  }

  // --- ① 명령 ------------------------------------------------------------------
  function step1(run) {
    var chars = run.input.characters.length ? run.input.characters : [];
    var html = '<div class="vpoc2-card"><h2>① 어떤 15초 장면을 만들까요?</h2>' +
      '<textarea class="vpoc2-ta" id="vpoc2-command" placeholder="한 줄만 써도 됩니다. 예) 비 오는 밤 골목에서 우산을 든 여자가 누군가를 기다리다 미소 짓는다">' +
      esc(run.input.command) + "</textarea>" +
      '<h3>캐릭터 (선택 · 최대 2명)</h3><div class="vpoc2-grid" id="vpoc2-chars">';
    for (var i = 0; i < 2; i++) {
      var c = chars[i] || { name: "", description: "" };
      html += '<div><input class="vpoc2-in" data-char="' + i + '" data-f="name" placeholder="이름 (없으면 비워 두기)" value="' +
        esc(c.name) + '"><textarea class="vpoc2-ta small" data-char="' + i + '" data-f="description" placeholder="외형·성격 (선택)">' +
        esc(c.description) + "</textarea></div>";
    }
    html += '</div><div class="vpoc2-row"><button class="vpoc2-btn primary" data-act="interpret"' + (state.busy ? " disabled" : "") +
      ">AI 해석</button>" + (run.interpretation.status !== "empty" ? '<span class="vpoc2-muted">다시 해석하면 캐릭터·영상을 새로 만듭니다.</span>' : "") +
      "</div></div>";
    return html;
  }

  function readInput() {
    var chars = [];
    for (var i = 0; i < 2; i++) {
      var n = root.querySelector('[data-char="' + i + '"][data-f="name"]').value.trim();
      var d = root.querySelector('[data-char="' + i + '"][data-f="description"]').value.trim();
      if (n || d) chars.push({ name: n, description: d });
    }
    return { command: root.querySelector("#vpoc2-command").value, characters: chars };
  }

  // --- ② 해석 ------------------------------------------------------------------
  function field(label, path, value, multiline) {
    return '<label class="vpoc2-l">' + esc(label) + "</label>" + (multiline
      ? '<textarea class="vpoc2-ta small" data-bind="' + path + '">' + esc(value) + "</textarea>"
      : '<input class="vpoc2-in" data-bind="' + path + '" value="' + esc(value) + '">');
  }

  function step2(run) {
    var d = state.draft;
    if (!d) return '<div class="vpoc2-card"><p class="vpoc2-muted">아직 해석이 없습니다. ① 에서 [AI 해석]을 누르세요.</p></div>';
    var interp = run.interpretation;
    var html = '<div class="vpoc2-card"><h2>② AI 해석 — 명령이 영상에 이렇게 들어갑니다</h2>';
    if (interp.status === "stale") html += '<div class="vpoc2-warnbox">명령이 바뀌어 이 해석은 예전 것입니다. ① 에서 다시 해석하세요.</div>';
    if (interp.violations && interp.violations.length) {
      html += '<div class="vpoc2-warnbox"><b>검토 필요</b> — 고친 뒤 [수정 저장]을 누르세요.<br>' +
        interp.violations.map(esc).join("<br>") + "</div>";
    }
    html += "<h3>명령 반영 체크리스트</h3><p class=\"vpoc2-muted\">명령을 요소로 나눴습니다. 영상이 끝나면 ⑤ 에서 요소마다 반영됐는지 고릅니다.</p>";
    d.elements.forEach(function (e, i) {
      html += '<div class="vpoc2-row" style="margin-top:4px"><span class="vpoc2-chip el">' + esc(e.id) + " · " +
        esc(ELEMENT_KINDS[e.kind] || e.kind) + '</span><input class="vpoc2-in" style="flex:1" data-bind="elements.' + i +
        '.textKo" value="' + esc(e.textKo) + '"></div>';
    });
    html += '<div class="vpoc2-grid" style="margin-top:12px"><div>' + field("제목", "titleKo", d.titleKo) +
      field("줄거리", "summaryKo", d.summaryKo, true) + field("배경", "settingKo", d.settingKo, true) +
      '<label class="vpoc2-l">화풍</label><select class="vpoc2-in" data-bind="style"><option value="live_action"' +
      (d.style === "live_action" ? " selected" : "") + '>실사</option><option value="anime"' + (d.style === "anime" ? " selected" : "") +
      ">애니</option></select>" + "</div><div>" + field("작품 룩 (영어 · 모든 샷 공통)", "lookEn", d.lookEn, true) +
      field("배경 (영어)", "settingEn", d.settingEn, true) + "</div></div>";
    html += "<h3>캐릭터</h3>";
    if (!d.characters.length) html += '<p class="vpoc2-muted">등장 캐릭터가 없습니다. 캐릭터 단계를 건너뜁니다.</p>';
    html += '<div class="vpoc2-grid">';
    d.characters.forEach(function (c, i) {
      var must = (c.mustKeepEn || []).map(function (t) { return '<span class="vpoc2-chip el">' + esc(t) + "</span>"; }).join("");
      html += '<div class="vpoc2-shot"><b>' + esc(c.id) + "</b> " + '<span class="vpoc2-badge">' +
        esc({ human: "사람", animal: "동물", creature: "생물" }[c.kind] || c.kind || "") + (c.speciesEn ? " · " + esc(c.speciesEn) : "") +
        "</span>" + (must ? '<div class="vpoc2-muted" style="margin-top:6px">사용자가 쓴 특징 (이미지·영상에 반드시 넣음)</div>' + must : "") +
        field("이름", "characters." + i + ".nameKo", c.nameKo) +
        field("역할", "characters." + i + ".roleKo", c.roleKo) + field("외형", "characters." + i + ".appearanceKo", c.appearanceKo, true) +
        "<details><summary class=\"vpoc2-muted\">영어 (고급)</summary>" +
        field("외형 (영어)", "characters." + i + ".appearanceEn", c.appearanceEn, true) +
        field("지칭 (영어)", "characters." + i + ".handleEn", c.handleEn) + "</details></div>";
    });
    html += "</div><h3>15초 연출안 (5초 × 3샷)</h3><div class=\"vpoc2-grid\">";
    d.shots.forEach(function (s, i) {
      html += '<div class="vpoc2-shot"><b>샷 ' + (i + 1) + " · " + (i * 5) + "~" + (i * 5 + 5) + "초</b>" +
        field("장면", "shots." + i + ".summaryKo", s.summaryKo, true) + '<div class="vpoc2-muted">카메라: ' + esc(s.cameraKo) + "</div>" +
        '<div style="margin-top:4px">' + d.elements.map(function (e) {
          var on = s.elementIds.indexOf(e.id) >= 0;
          return '<button class="vpoc2-btn" style="padding:1px 8px;font-size:12px;margin:2px' + (on ? ";background:#e7eeff" : "") +
            '" data-act="toggle-el" data-shot="' + i + '" data-el="' + esc(e.id) + '">' + (on ? "✓ " : "") + esc(e.id) + "</button>";
        }).join("") + "</div><details><summary class=\"vpoc2-muted\">영어 연출 (고급)</summary>" +
        ["framingEn", "locationEn", "beatsEn", "lightEn", "detailEn", "cameraEn"].map(function (f) {
          return field(f, "shots." + i + "." + f, s[f], true);
        }).join("") + "</details></div>";
    });
    html += "</div>";
    var hasChars = d.characters.length > 0;
    html += '<div class="vpoc2-row"><button class="vpoc2-btn" data-act="save-interp"' + (state.busy ? " disabled" : "") + ">수정 저장</button>" +
      '<button class="vpoc2-btn primary" data-act="' + (hasChars ? "draw-all" : "prepare") + '"' + (state.busy ? " disabled" : "") + ">" +
      (hasChars ? "캐릭터 그리기" : "영상 준비") + "</button></div></div>";
    return html;
  }

  function setPath(obj, path, value) {
    var parts = path.split(".");
    var o = obj;
    for (var i = 0; i < parts.length - 1; i++) o = o[/^\d+$/.test(parts[i]) ? +parts[i] : parts[i]];
    o[parts[parts.length - 1]] = value;
  }

  // --- ③ 캐릭터 -----------------------------------------------------------------
  function step3(run) {
    var data = run.interpretation.data || { characters: [] };
    var byId = {};
    data.characters.forEach(function (c) { byId[c.id] = c; });
    var items = run.characters.items;
    var html = '<div class="vpoc2-card"><h2>③ 캐릭터 확정</h2><p class="vpoc2-muted">마음에 들 때까지 다시 그리고, 모두 확정하면 영상을 만듭니다. 확정한 이미지를 AI가 보고 영상 속 외형을 고정합니다.</p>';
    if (run.characters.status === "stale") html += '<div class="vpoc2-warnbox">해석이 바뀌었습니다. 캐릭터를 다시 확인하세요.</div>';
    html += '<div class="vpoc2-grid">';
    items.forEach(function (it) {
      var c = byId[it.id] || {};
      var cur = it.image;
      html += '<div class="vpoc2-shot vpoc2-char"><b>' + esc(c.nameKo || it.id) + "</b> <span class=\"vpoc2-status " +
        (it.confirmed ? "ok\">확정됨" : "run\">확정 전") + "</span>" +
        (cur ? '<img src="' + esc(cur.url) + '" alt="">' : '<p class="vpoc2-muted">이미지 없음</p>') +
        '<div class="vpoc2-hist">' + (it.history || []).map(function (h) {
          return '<img src="' + esc(h.url) + '" title="버전 ' + h.version + '" class="' + (cur && cur.key === h.key ? "cur" : "") +
            '" data-act="pick" data-char="' + esc(it.id) + '" data-ver="' + h.version + '">';
        }).join("") + "</div>" + '<div class="vpoc2-muted" style="margin-top:6px">' + esc(c.appearanceKo) + "</div>" +
        '<textarea class="vpoc2-ta small" data-fb="' + esc(it.id) + '" placeholder="바꾸고 싶은 점 (예: 머리를 더 길게, 표정을 더 차갑게)">' +
        esc(it.feedbackKo || "") + '</textarea><div class="vpoc2-row"><button class="vpoc2-btn" data-act="redraw" data-char="' + esc(it.id) + '"' +
        (state.busy ? " disabled" : "") + ">다시 그리기</button>" +
        (it.confirmed ? '<button class="vpoc2-btn" data-act="unconfirm" data-char="' + esc(it.id) + '">확정 취소</button>'
          : '<button class="vpoc2-btn primary" data-act="confirm" data-char="' + esc(it.id) + '"' + (cur ? "" : " disabled") + ">확정</button>") +
        "</div>" + (it.appearanceShortEn ? '<div class="vpoc2-prompt" style="margin-top:6px">' + esc(it.appearanceShortEn) + "</div>" : "") + "</div>";
    });
    html += "</div>";
    var all = items.length && items.every(function (it) { return it.confirmed; });
    html += '<div class="vpoc2-row"><button class="vpoc2-btn primary" data-act="prepare"' + (all && !state.busy ? "" : " disabled") +
      ">영상 만들기</button>" + (all ? "" : '<span class="vpoc2-muted">모든 캐릭터를 확정해야 합니다.</span>') + "</div></div>";
    return html;
  }

  // --- ④ 영상 -------------------------------------------------------------------
  var SHOT_STATUS = { queued: ["대기", "run"], submitted: ["생성 중", "run"], completed: ["완료", "ok"], failed: ["실패", "bad"] };

  function step4(run) {
    var v = run.video;
    var data = run.interpretation.data || { shots: [] };
    var html = '<div class="vpoc2-card"><h2>④ 15초 영상</h2>';
    if (v.status === "stale") html += '<div class="vpoc2-warnbox">앞 단계가 바뀌었습니다. ③ 에서 [영상 만들기]를 다시 누르세요.</div>';
    if (v.status === "prepared") {
      var m = state.meta || { hq: {}, videoUsd: {} };
      var sel = state.resolution || "540p";
      html += '<p class="vpoc2-muted">아래 프롬프트로 샷 3개를 만들어 15초로 이어 붙입니다. 동시 제출 한도 1이라 샷이 차례로 만들어집니다(보통 4~15분).</p><div class="vpoc2-grid">';
      v.prompts.forEach(function (p, i) {
        var s = data.shots[i] || {};
        html += '<div class="vpoc2-shot"><b>샷 ' + (i + 1) + "</b> <span class=\"vpoc2-muted\">" + p.promptChars + "자</span><div>" + esc(s.summaryKo) +
          "</div>" + (p.reviewKo && p.reviewKo.length ? '<div class="vpoc2-warnbox">검토 필요<br>' + p.reviewKo.map(esc).join("<br>") + "</div>" : "") +
          '<details><summary class="vpoc2-muted">영어 프롬프트 (고급 편집)</summary><textarea class="vpoc2-ta small" data-prompt="' +
          esc(p.shotId) + '">' + esc(p.promptEn) + '</textarea><button class="vpoc2-btn" data-act="save-prompt" data-shot="' + esc(p.shotId) +
          '">프롬프트 저장</button></details></div>';
      });
      var bad = v.prompts.some(function (p) { return p.reviewKo && p.reviewKo.length; });
      html += '</div><h3>해상도</h3><label><input type="radio" name="vpoc2-res" value="540p"' + (sel === "540p" ? " checked" : "") +
        "> 540p 테스트</label><br><label><input type=\"radio\" name=\"vpoc2-res\" value=\"720p\"" +
        (sel === "720p" ? " checked" : "") + (m.hq.remaining > 0 ? "" : " disabled") + "> 720p 최종 확인 · 이번 달 남은 " + m.hq.remaining + "회</label>" +
        '<div class="vpoc2-row"><button class="vpoc2-btn primary" data-act="generate"' + (bad || state.busy ? " disabled" : "") +
        ">영상 만들기 시작</button></div>";
    } else if (v.shots.length) {
      if (v.final.status === "ready") {
        html += '<h3>15초 완성본</h3><video class="vpoc2-v" controls src="' + esc(v.final.url) + '"></video>' +
          '<div class="vpoc2-row"><button class="vpoc2-btn primary" data-act="back" data-step="5">리뷰 남기기</button></div>';
      } else if (v.status === "done") {
        var stitchErr = run.task.name === "stitch" && run.task.status === "error";
        html += '<p class="vpoc2-status run">' + (stitchErr ? "합본 실패" : "샷 3개 완료 — 15초로 합치는 중") + "</p>" +
          (stitchErr ? '<button class="vpoc2-btn" data-act="stitch">다시 합치기</button>' : "");
      }
      html += '<p class="vpoc2-muted">' + esc(v.resolution) + ' · 생성 중에 되돌아가도 이미 제출한 샷은 취소되지 않습니다.</p><div class="vpoc2-grid">';
      v.shots.forEach(function (sh, i) {
        var st = SHOT_STATUS[sh.status] || [sh.status, ""];
        var slow = sh.status === "submitted" && sh.elapsedS > 600;
        html += '<div class="vpoc2-shot"><b>샷 ' + (i + 1) + '</b> <span class="vpoc2-status ' + st[1] + '">' + st[0] +
          (sh.elapsedS != null ? " · " + mmss(sh.elapsedS) : "") + "</span>" + (sh.attempt > 1 ? ' <span class="vpoc2-badge">시도 ' + sh.attempt + "</span>" : "") +
          "<div class=\"vpoc2-muted\">" + esc((data.shots[i] || {}).summaryKo) + "</div>" +
          (sh.status === "submitted" ? '<div class="vpoc2-muted">' + (slow ? "평소보다 오래 걸려요" : "보통 2~5분") + "</div>" : "") +
          (sh.note ? '<div class="vpoc2-muted">' + esc(sh.note) + "</div>" : "") +
          (sh.videoUrl ? '<video class="vpoc2-v" controls src="' + esc(sh.videoUrl) + '"></video>' : "") + errBox(sh.error) +
          (sh.status === "failed" ? '<button class="vpoc2-btn" data-act="retry" data-shot="' + esc(sh.id) + '">다시 시도</button>' : "") + "</div>";
      });
      html += "</div>";
    } else {
      html += '<p class="vpoc2-muted">③ 에서 캐릭터를 확정하고 [영상 만들기]를 누르세요.</p>';
    }
    return html + "</div>";
  }

  // --- ⑤ 리뷰 -------------------------------------------------------------------
  var MARKS = [["yes", "반영됨"], ["partial", "일부"], ["no", "안 됨"]];

  function step5(run) {
    var v = run.video;
    var rd = state.reviewDraft;
    var elements = (run.interpretation.data || {}).elements || [];
    if (v.final.status !== "ready") return '<div class="vpoc2-card"><p class="vpoc2-muted">15초 영상이 완성되면 리뷰할 수 있습니다.</p></div>';
    var html = '<div class="vpoc2-card"><h2>⑤ 리뷰 — 명령이 얼마나 반영됐나요?</h2><video class="vpoc2-v" controls src="' + esc(v.final.url) + '"></video>' +
      '<p class="vpoc2-muted">명령: ' + esc(run.input.command) + "</p><h3>전체 점수</h3><div class=\"vpoc2-stars\">";
    for (var s = 1; s <= 5; s++) html += '<button class="' + (rd.score >= s ? "on" : "") + '" data-act="score" data-score="' + s + '">★</button>';
    html += '</div><h3>요소별 반영</h3><table class="vpoc2-table">';
    elements.forEach(function (e) {
      html += "<tr><td>" + esc(e.id) + "</td><td>" + esc(e.textKo) + '</td><td class="vpoc2-marks">' + MARKS.map(function (m) {
        return '<button class="vpoc2-btn' + (rd.elements[e.id] === m[0] ? " on" : "") + '" data-act="mark" data-el="' + esc(e.id) +
          '" data-mark="' + m[0] + '">' + m[1] + "</button>";
      }).join("") + "</td></tr>";
    });
    html += '</table><label class="vpoc2-l">좋은 점</label><textarea class="vpoc2-ta small" data-rv="goodKo">' + esc(rd.goodKo) +
      '</textarea><label class="vpoc2-l">아쉬운 점</label><textarea class="vpoc2-ta small" data-rv="badKo">' + esc(rd.badKo) +
      '</textarea><label class="vpoc2-l">작성자</label><input class="vpoc2-in" data-rv="author" value="' + esc(rd.author) + '">' +
      '<div class="vpoc2-row"><button class="vpoc2-btn primary" data-act="save-review"' + (state.busy ? " disabled" : "") + ">리뷰 저장</button>" +
      (run.review ? '<span class="vpoc2-status ok">저장됨 · 반영률 ' + Math.round((run.review.reflectionRate || 0) * 100) + "%</span>" : "") +
      "</div></div>";
    return html;
  }

  // --- 렌더 ----------------------------------------------------------------------
  function render() {
    if (state.page === "home") { root.innerHTML = homePage(); return; }
    if (state.page === "reviews") { root.innerHTML = reviewsPage(); return; }
    var run = state.run;
    if (!run) { root.innerHTML = topBar() + (state.error ? errBox(state.error) : '<p class="vpoc2-muted">불러오는 중…</p>'); return; }
    var body = [step1, step2, step3, step4, step5][run.step - 1](run);
    var focus = document.activeElement && document.activeElement.closest && document.activeElement.closest("#vpoc2-root") ? true : false;
    if (focus && run.task.status !== "running" && state.lastStep === run.step && !state.busy && state.page === "run" &&
        document.activeElement.matches("input,textarea,select")) {
      // 입력 중에는 다시 그리지 않는다 (조회 결과로 입력이 날아가지 않게)
      return;
    }
    state.lastStep = run.step;
    root.innerHTML = topBar() + stepsBar(run) + body + band(run);
  }

  // --- 이벤트 --------------------------------------------------------------------
  root.addEventListener("input", function (ev) {
    var t = ev.target;
    if (t.dataset.bind && state.draft) { setPath(state.draft, t.dataset.bind, t.value); state.draftDirty = true; }
    if (t.dataset.rv) state.reviewDraft[t.dataset.rv] = t.value;
    if (t.name === "vpoc2-res") { state.resolution = t.value; render(); }
  });
  root.addEventListener("change", function (ev) {
    var t = ev.target;
    if (t.dataset.bind && state.draft) { setPath(state.draft, t.dataset.bind, t.value); state.draftDirty = true; }
    if (t.name === "vpoc2-res") { state.resolution = t.value; render(); }
  });

  root.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-act]");
    if (!b || b.disabled) return;
    var a = b.dataset.act;
    var run = state.run;
    if (a === "new") {
      api("POST", "/runs", {}).then(function (j) { go("run=" + j.run.id); }).catch(fail);
    } else if (a === "open") { go("run=" + b.dataset.id);
    } else if (a === "reviews") { go("reviews");
    } else if (a === "back") {
      if (+b.dataset.step === run.step) return;
      act("POST", "/back", { toStep: +b.dataset.step });
    } else if (a === "interpret") {
      var inp = readInput();
      if (!inp.command.trim()) { state.error = { type: "Input", message: "명령을 입력하세요." }; render(); return; }
      state.busy = true; render();
      api("PUT", "/runs/" + run.id + "/input", inp).then(function () { return act("POST", "/run/interpret"); }).catch(fail);
    } else if (a === "toggle-el") {
      var sh = state.draft.shots[+b.dataset.shot];
      var k = sh.elementIds.indexOf(b.dataset.el);
      if (k >= 0) sh.elementIds.splice(k, 1); else sh.elementIds.push(b.dataset.el);
      state.draftDirty = true;
      render();
    } else if (a === "save-interp") {
      act("PUT", "/interpretation", { data: state.draft }).then(function () { state.draftDirty = false; });
    } else if (a === "draw-all" || a === "prepare" && run.step === 2) {
      var then = a === "draw-all" ? function () { return act("POST", "/run/draw", {}); } : function () { return act("POST", "/run/prepare"); };
      if (state.draftDirty) {
        state.busy = true; render();
        api("PUT", "/runs/" + run.id + "/interpretation", { data: state.draft }).then(function () { state.draftDirty = false; return then(); }).catch(fail);
      } else then();
    } else if (a === "prepare") { act("POST", "/run/prepare");
    } else if (a === "redraw") {
      var fb = root.querySelector('[data-fb="' + b.dataset.char + '"]').value;
      act("POST", "/run/draw", { charId: b.dataset.char, feedbackKo: fb });
    } else if (a === "confirm") { act("POST", "/characters/" + b.dataset.char + "/confirm", { confirmed: true });
    } else if (a === "unconfirm") { act("POST", "/characters/" + b.dataset.char + "/confirm", { confirmed: false });
    } else if (a === "pick") { act("POST", "/characters/" + b.dataset.char + "/confirm", { confirmed: false, version: +b.dataset.ver });
    } else if (a === "save-prompt") {
      act("PUT", "/prompts/" + b.dataset.shot, { promptEn: root.querySelector('[data-prompt="' + b.dataset.shot + '"]').value });
    } else if (a === "generate") {
      var res = state.resolution || "540p";
      var cost = state.meta.videoUsd[res];
      var msg = "샷 3개 × 5초 · " + res + " 영상을 만듭니다.\n시작하면 취소할 수 없습니다." + (res === "720p" ? "\n이번 달 720p 최종 확인 1회를 사용합니다." : "");
      if (!window.confirm(msg)) return;
      act("POST", "/run/generate", { resolution: res, confirmUsd: cost }).then(function () {
        return api("GET", "/meta").then(function (j) { state.meta = j; render(); });
      });
    } else if (a === "retry") {
      if (!window.confirm("이 샷을 다시 만들면 비용이 듭니다. 계속할까요?")) return;
      act("POST", "/run/retry", { shotId: b.dataset.shot });
    } else if (a === "stitch") { act("POST", "/run/stitch");
    } else if (a === "score") { state.reviewDraft.score = +b.dataset.score; render();
    } else if (a === "mark") { state.reviewDraft.elements[b.dataset.el] = b.dataset.mark; render();
    } else if (a === "save-review") {
      var rd = state.reviewDraft;
      if (!rd.score) { state.error = { type: "Input", message: "전체 점수를 고르세요." }; render(); return; }
      act("PUT", "/review", rd).then(function () {
        if (state.run && state.run.review) state.reviewDraft = clone(state.run.review);
        render();
      });
    }
  });

  load();
})();
