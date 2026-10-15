/* vpoc1 영상 기능 화면 — PRD 7장(API 계약) · 8장(화면 명세).

   #vpoc-root 안의 DOM 을 전부 직접 만든다. 기존 마크업을 찾지 않고, 이벤트는 자기가 만든
   요소에만 단다. 어떤 요소가 없어도 예외로 멈추지 않는다. CSS 클래스는 vpoc- 접두사.

   금액은 화면에 보이지 않는다(설계자 2026-10-07 — 비용·토큰은 admin 모델 관리에서만 본다).
   서버의 금액 확인(confirmUsd)은 그대로라 서버가 준 cost.totalUsd 를 보이지 않게 실어 보낸다.
   화면에서 계산하지 않는다 — 틀린 화면 추정치가 작업을 멈춰 세운 적이 있다(PRD R5).

   전역: window.vpoc = { mount(projectId), unmount() } */
(function () {
  "use strict";

  var TOKEN_KEY = "toon-change-ai-pj:token";
  var ROOT_ID = "vpoc-root";
  var POLL_TASK_MS = 2000;
  var POLL_VIDEO_MS = 5000;
  var MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

  var STEP_LABELS = ["입력", "확인", "계획 승인", "영상"];
  var TASK_LABELS = {
    analyze: ["AI가 이야기를 분석하고 있어요", "보통 20~60초"],
    prepare: ["캐릭터를 그리고 장면을 나누고 있어요", "보통 30~90초"],
    redraw: ["캐릭터를 다시 그리고 있어요", "보통 10~30초"],
    revise: ["수정안을 만들고 있어요", "보통 10~30초"]
  };
  // PRD 8.7 쉬운 촬영 용어. 계획에 cameraKo 가 비어 있을 때만 쓴다.
  var CAMERA_WORDS = [
    ["close-up", "얼굴을 크게(클로즈업)"],
    ["medium shot", "허리 위까지(미디엄 샷)"],
    ["long shot", "온몸과 배경까지(롱 샷)"],
    ["wide shot", "온몸과 배경까지(롱 샷)"],
    ["two-shot", "두 사람을 한 화면에(투 샷)"],
    ["two shot", "두 사람을 한 화면에(투 샷)"],
    ["over-the-shoulder", "한 사람 어깨 너머로 상대를(오버 더 숄더)"],
    ["push-in", "천천히 다가가기(푸시 인)"],
    ["pull-out", "천천히 멀어지기(풀 아웃)"],
    ["pan", "옆으로 훑기(팬)"],
    ["tracking", "인물을 따라가기(트래킹)"],
    ["static", "카메라를 고정(고정 샷)"]
  ];

  var state = {
    projectId: "",
    planId: "",
    plan: null,
    project: null,
    view: "flow", // flow | project
    timer: null,
    busy: false,
    mounted: false,
    mock: false,
    lastError: null,
    draft: { text: "", characters: [], sceneFiles: [] }
  };

  /* ---------------------------------------------------------------- 도구 */

  function root() {
    return document.getElementById(ROOT_ID);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function token() {
    try {
      return sessionStorage.getItem(TOKEN_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function mmss(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    var minutes = Math.floor(total / 60);
    var rest = total % 60;
    return minutes + ":" + (rest < 10 ? "0" : "") + rest;
  }


  /* ③ 만들 분량 한 줄(금액 없음). 모델 고르기가 켜진 배포에서는 모델 이름을 앞에 붙인다. */
  function costLabel(plan, cost) {
    var text = (cost.shots || 0) + "샷 × " + (cost.durationS || 5) + "초";
    var model = (plan.videoModels || []).filter(function (item) {
      return item.id === cost.model;
    })[0];
    if (plan.modelPicker && model) return model.label + " · " + text;
    return text + " · " + (cost.resolution || "540p");
  }

  function easyCamera(shot) {
    if (shot && shot.cameraKo) return shot.cameraKo;
    var source = String((shot && shot.promptEn) || "").toLowerCase();
    for (var index = 0; index < CAMERA_WORDS.length; index += 1) {
      if (source.indexOf(CAMERA_WORDS[index][0]) >= 0) return CAMERA_WORDS[index][1];
    }
    return "";
  }

  /* ---------------------------------------------------------------- 통신 */

  function api(path, options) {
    var init = options || {};
    init.headers = init.headers || {};
    init.headers["Authorization"] = "Bearer " + token();
    if (init.body && !(init.body instanceof FormData)) {
      init.headers["Content-Type"] = "application/json";
    }
    return fetch(path, init).then(function (response) {
      return response
        .json()
        .catch(function () {
          return {
            ok: false,
            error: {
              where: path,
              type: "NonJsonResponse",
              message: "서버가 JSON 이 아닌 응답을 보냈습니다.",
              requestId: "",
              httpStatus: response.status
            }
          };
        })
        .then(function (payload) {
          if (!payload || payload.ok !== true) {
            throw (payload && payload.error) || {
              where: path,
              type: "UnknownError",
              message: "알 수 없는 오류",
              requestId: "",
              httpStatus: response.status
            };
          }
          return payload;
        });
    });
  }

  /* ------------------------------------------------- 개발용 픽스처 (?vpocMock=1) */

  function mockPlan() {
    return JSON.parse(JSON.stringify(MOCK_PLAN));
  }

  function mockProject() {
    return JSON.parse(JSON.stringify(MOCK_PROJECT));
  }

  /* ---------------------------------------------------------------- 조각 */

  function header() {
    var bar = el("div", "vpoc-topbar");
    var title = el("div", "vpoc-topbar-title", (state.project && state.project.title) || "영상 만들기");
    var right = el("div", "vpoc-topbar-right");

    var toggle = el("button", "vpoc-btn", state.view === "project" ? "영상 계획으로" : "프로젝트 보기");
    toggle.type = "button";
    toggle.addEventListener("click", function () {
      state.view = state.view === "project" ? "flow" : "project";
      refresh();
    });
    right.appendChild(toggle);

    // 누적 비용 표시는 걷었다(설계자 2026-10-07). admin 모델 관리 "영상 비용 · 토큰"에서 본다.
    bar.appendChild(title);
    bar.appendChild(right);
    return bar;
  }

  function stepBar() {
    var plan = state.plan;
    var wrap = el("div", "vpoc-steps");
    for (var number = 1; number <= 4; number += 1) {
      var step = (plan.steps && plan.steps[String(number)]) || {};
      var button = el("button", "vpoc-step", "\u2460\u2461\u2462\u2463".charAt(number - 1) + " " + STEP_LABELS[number - 1]);
      button.type = "button";
      if (number === plan.currentStep) button.classList.add("is-current");
      if (step.status === "stale") button.classList.add("is-stale");
      if (number > plan.maxReachedStep) button.classList.add("is-locked");
      button.disabled = number > plan.maxReachedStep || number === plan.currentStep;
      button.dataset.step = String(number);
      button.addEventListener("click", function (event) {
        goBackTo(Number(event.currentTarget.dataset.step));
      });
      wrap.appendChild(button);
    }
    return wrap;
  }

  function progressBand() {
    var plan = state.plan;
    var task = plan.task || {};
    var wrap = el("div", "vpoc-bands");

    if (task.status === "running") {
      var labels = TASK_LABELS[task.name] || ["작업 중", ""];
      var seconds = task.startedAt ? Math.round((Date.now() - Date.parse(task.startedAt)) / 1000) : 0;
      var band = el("div", "vpoc-progress");
      band.appendChild(el("span", "vpoc-spinner"));
      band.appendChild(el("span", null, labels[0] + " \u00b7 " + mmss(seconds) + " \u00b7 " + labels[1]));
      wrap.appendChild(band);
    }
    if (task.status === "error" && task.error) {
      // revise 는 샷별 의견이 있어야 다시 부를 수 있다. 샷 카드에서 [의견 고쳐서 다시]로 한다.
      var again = task.name === "revise"
        ? null
        : function () {
            runAction(task.name === "prepare" ? "prepare" : "analyze", {});
          };
      wrap.appendChild(errorBox(task.error, again));
    }
    if (state.lastError) {
      wrap.appendChild(errorBox(state.lastError, null));
    }
    return wrap;
  }

  function errorBox(error, retry) {
    var box = el("div", "vpoc-error-box");
    var head = el("div", "vpoc-error-head");
    head.appendChild(el("strong", null, "\u2716 " + (error.where || "") + " \u00b7 " + (error.type || "")));
    var actions = el("div", "vpoc-error-actions");

    var copy = el("button", "vpoc-btn vpoc-btn-small", "복사");
    copy.type = "button";
    copy.addEventListener("click", function () {
      var text = JSON.stringify(error, null, 2);
      if (navigator.clipboard) navigator.clipboard.writeText(text);
      copy.textContent = "복사했습니다";
    });
    actions.appendChild(copy);

    if (retry) {
      var again = el("button", "vpoc-btn vpoc-btn-small", "다시 시도");
      again.type = "button";
      again.addEventListener("click", retry);
      actions.appendChild(again);
    }
    head.appendChild(actions);
    box.appendChild(head);

    var body = el("pre", "vpoc-error-body");
    body.textContent =
      (error.message || "") +
      "\n\nHTTP " + (error.httpStatus || "-") +
      (error.requestId ? "  requestId " + error.requestId : "") +
      (error.at ? "  at " + error.at : "");
    box.appendChild(body);
    return box;
  }

  function confirmModal(title, lines, okLabel, onOk) {
    var scrim = el("div", "vpoc-scrim");
    var card = el("div", "vpoc-modal");
    card.appendChild(el("h3", null, title));
    for (var index = 0; index < lines.length; index += 1) {
      card.appendChild(el("p", null, lines[index]));
    }
    var actions = el("div", "vpoc-modal-actions");
    var cancel = el("button", "vpoc-btn", "취소");
    cancel.type = "button";
    cancel.addEventListener("click", function () {
      if (scrim.parentNode) scrim.parentNode.removeChild(scrim);
    });
    var confirm = el("button", "vpoc-btn vpoc-btn-primary", okLabel);
    confirm.type = "button";
    confirm.addEventListener("click", function () {
      if (scrim.parentNode) scrim.parentNode.removeChild(scrim);
      onOk();
    });
    actions.appendChild(cancel);
    actions.appendChild(confirm);
    card.appendChild(actions);
    scrim.appendChild(card);
    scrim.addEventListener("click", function (event) {
      if (event.target === scrim && scrim.parentNode) scrim.parentNode.removeChild(scrim);
    });
    document.body.appendChild(scrim);
  }

  /* ---------------------------------------------------------------- ① 입력 */

  function renderStep1(host) {
    var plan = state.plan;
    var step = plan.steps["1"];
    var editable = plan.canEdit;
    var draft = state.draft;
    if (!draft.loaded) {
      draft.text = step.text || "";
      draft.characters = (step.characters || []).map(function (card) {
        return {
          name: card.name || "",
          description: card.description || "",
          files: (card.files || []).slice()
        };
      });
      draft.sceneFiles = (step.sceneFiles || []).slice();
      draft.loaded = true;
    }

    var section = el("section", "vpoc-card");
    section.appendChild(el("h2", null, "\u2460 무엇을 만들까요?"));
    section.appendChild(
      el("p", "vpoc-hint", "한 줄만 써도 됩니다. 비어 있는 부분은 AI가 채웁니다.")
    );

    var textarea = el("textarea", "vpoc-textarea");
    textarea.rows = 7;
    textarea.value = draft.text;
    textarea.placeholder = "예) 두 남자는 F1 레이서이고, 둘은 원수지간이다.";
    textarea.disabled = !editable;
    textarea.addEventListener("input", function () {
      draft.text = textarea.value;
      updateAnalyzeButton();
    });
    section.appendChild(textarea);

    /* 캐릭터 카드 */
    var charWrap = el("div", "vpoc-charcards");
    function drawCharCards() {
      clear(charWrap);
      draft.characters.forEach(function (card, index) {
        var box = el("div", "vpoc-charcard");
        var name = el("input", "vpoc-input");
        name.type = "text";
        name.placeholder = "이름 (몰라도 됩니다)";
        name.value = card.name;
        name.disabled = !editable;
        name.addEventListener("input", function () {
          card.name = name.value;
        });
        var desc = el("textarea", "vpoc-input");
        desc.rows = 2;
        desc.placeholder = "외형·성격·관계 등 (선택)";
        desc.value = card.description;
        desc.disabled = !editable;
        desc.addEventListener("input", function () {
          card.description = desc.value;
        });
        box.appendChild(name);
        box.appendChild(desc);
        box.appendChild(filePicker("character", index, card.files, drawCharCards));

        if (editable) {
          var remove = el("button", "vpoc-btn vpoc-btn-small", "카드 삭제");
          remove.type = "button";
          remove.addEventListener("click", function () {
            draft.characters.splice(index, 1);
            drawCharCards();
            updateAnalyzeButton();
          });
          box.appendChild(remove);
        }
        charWrap.appendChild(box);
      });
    }
    drawCharCards();
    section.appendChild(charWrap);

    if (editable) {
      var add = el("button", "vpoc-btn", "캐릭터 추가");
      add.type = "button";
      add.addEventListener("click", function () {
        draft.characters.push({ name: "", description: "", files: [] });
        drawCharCards();
      });
      section.appendChild(add);
    }

    section.appendChild(el("h3", null, "장면 · 웹툰 이미지 (선택)"));
    var sceneBox = el("div", "vpoc-scenefiles");
    function drawSceneFiles() {
      clear(sceneBox);
      sceneBox.appendChild(filePicker("scene", -1, draft.sceneFiles, drawSceneFiles));
    }
    drawSceneFiles();
    section.appendChild(sceneBox);

    var analyze = el("button", "vpoc-btn vpoc-btn-primary vpoc-btn-run", "AI 분석");
    analyze.type = "button";
    analyze.addEventListener("click", function () {
      saveStep(1, {
        text: draft.text,
        characters: draft.characters,
        sceneFiles: draft.sceneFiles
      }).then(function () {
        return runAction("analyze", {});
      });
    });
    if (editable) section.appendChild(analyze);

    function updateAnalyzeButton() {
      var hasFiles =
        draft.sceneFiles.length > 0 ||
        draft.characters.some(function (card) {
          return (card.files || []).length > 0;
        });
      analyze.disabled = state.busy || (!draft.text.trim() && !hasFiles);
    }
    updateAnalyzeButton();

    host.appendChild(section);
  }

  function filePicker(kind, charIndex, files, redraw) {
    var wrap = el("div", "vpoc-files");
    files.forEach(function (item, index) {
      var chip = el("div", "vpoc-filechip");
      if (item.thumbUrl || item.url) {
        var img = el("img");
        img.src = item.thumbUrl || item.url;
        img.alt = item.name || "업로드 이미지";
        chip.appendChild(img);
      }
      chip.appendChild(el("span", null, item.name || item.fileId));
      if (state.plan && state.plan.canEdit) {
        var remove = el("button", "vpoc-btn vpoc-btn-small", "\u2715");
        remove.type = "button";
        remove.addEventListener("click", function () {
          files.splice(index, 1);
          redraw();
        });
        chip.appendChild(remove);
      }
      wrap.appendChild(chip);
    });

    if (!state.plan || state.plan.canEdit) {
      var label = el("label", "vpoc-filebtn", "이미지 추가");
      var input = el("input");
      input.type = "file";
      input.accept = "image/*";
      input.multiple = true;
      input.addEventListener("change", function () {
        var list = Array.prototype.slice.call(input.files || []);
        input.value = "";
        list.forEach(function (file) {
          if (file.size > MAX_UPLOAD_BYTES) {
            state.lastError = {
              where: "upload",
              type: "FileTooLarge",
              message: file.name + " 은(는) 10MB를 넘습니다. 10MB 이하로 줄여 주세요.",
              httpStatus: 413
            };
            refresh();
            return;
          }
          uploadFile(file, kind, charIndex).then(function (payload) {
            files.push({
              fileId: payload.fileId,
              name: payload.name,
              url: payload.url,
              thumbUrl: payload.thumbUrl
            });
            redraw();
          });
        });
      });
      label.appendChild(input);
      wrap.appendChild(label);
    }
    return wrap;
  }

  function uploadFile(file, kind, charIndex) {
    var form = new FormData();
    form.append("file", file);
    form.append("kind", kind);
    form.append("charIndex", String(charIndex));
    return api("/api/vpoc/p/" + encodeURIComponent(state.projectId) + "/uploads", {
      method: "POST",
      body: form
    }).catch(function (error) {
      state.lastError = error;
      refresh();
      throw error;
    });
  }

  /* ---------------------------------------------------------------- ② 확인 */

  function renderStep2(host) {
    var plan = state.plan;
    var step = plan.steps["2"];
    var editable = plan.canEdit;
    var story = step.story || {};
    var edits = { story: {}, characters: {}, answers: {}, extraKo: step.extraKo || "" };

    var section = el("section", "vpoc-card");
    section.appendChild(el("h2", null, "\u2461 이 내용이 맞나요?"));
    if (step.status === "stale") {
      section.appendChild(el("p", "vpoc-stale", "앞 단계를 고쳤습니다. 다시 만들어야 합니다."));
    }

    function field(labelText, key, rows) {
      var block = el("div", "vpoc-field");
      block.appendChild(el("label", null, labelText));
      var input = rows ? el("textarea", "vpoc-input") : el("input", "vpoc-input");
      if (rows) input.rows = rows;
      else input.type = "text";
      input.value = story[key] || "";
      input.disabled = !editable;
      input.addEventListener("input", function () {
        edits.story[key] = input.value;
      });
      block.appendChild(input);
      return block;
    }
    section.appendChild(field("제목", "titleKo"));
    section.appendChild(field("줄거리", "loglineKo", 2));
    section.appendChild(field("배경", "settingKo", 2));

    if ((story.genre || []).length) {
      section.appendChild(el("p", "vpoc-hint", "장르: " + story.genre.join(", ")));
    }

    /* 화풍 */
    var styleBlock = el("div", "vpoc-field");
    styleBlock.appendChild(el("label", null, "화풍"));
    var styleRow = el("div", "vpoc-radios");
    [["live_action", "실사"], ["anime", "애니"], ["webtoon", "웹툰 원작 그대로 (올린 웹툰 속 인물·화풍)"]].forEach(function (pair) {
      var label = el("label", "vpoc-radio");
      var radio = el("input");
      radio.type = "radio";
      radio.name = "vpoc-style";
      radio.value = pair[0];
      radio.checked = (story.style || story.styleSuggestion || "live_action") === pair[0];
      radio.disabled = !editable;
      radio.addEventListener("change", function () {
        if (radio.checked) edits.story.style = pair[0];
      });
      label.appendChild(radio);
      label.appendChild(el("span", null, pair[1]));
      styleRow.appendChild(label);
    });
    styleBlock.appendChild(styleRow);
    section.appendChild(styleBlock);

    /* 영상 모델 (dev 만 — 서버가 modelPicker 를 켜 둔 배포에서만 보인다) */
    if (plan.modelPicker && (plan.videoModels || []).length) {
      var modelBlock = el("div", "vpoc-field");
      modelBlock.appendChild(el("label", null, "영상 모델"));
      var modelRow = el("div", "vpoc-radios");
      var currentModel = step.videoModel || plan.videoModel || "luma-ray2";
      plan.videoModels.forEach(function (item) {
        var label = el("label", "vpoc-radio");
        var radio = el("input");
        radio.type = "radio";
        radio.name = "vpoc-video-model";
        radio.value = item.id;
        radio.checked = currentModel === item.id;
        radio.disabled = !editable || !item.available;
        radio.addEventListener("change", function () {
          if (radio.checked) edits.videoModel = item.id;
        });
        label.appendChild(radio);
        var text = item.label;
        if (!item.available) text += " (" + item.reason + ")";
        label.appendChild(el("span", null, text));
        modelRow.appendChild(label);
      });
      modelBlock.appendChild(modelRow);
      section.appendChild(modelBlock);

      /* 제작 방식(설계자 2026-10-14). 키 장면 = 5초마다 러프 콘티 → 완성 그림 → 그 사이를 Seedance 로 잇는다. */
      var modeBlock = el("div", "vpoc-field");
      modeBlock.appendChild(el("label", null, "제작 방식 (Seedance)"));
      var modeRow = el("div", "vpoc-radios");
      // 설계자 2026-10-14: Seedance 면 키 장면(러프 콘티) 방식이 기본이다. 저장된 값이 있으면 그것.
      var currentMode = step.makeMode || (String(currentModel).indexOf("seedance") === 0 ? "keyframes" : "shots");
      edits.makeMode = edits.makeMode || currentMode;
      [["shots", "샷별로 만들기 (기존)"], ["keyframes", "키 장면(콘티)으로 만들기 — 5초마다 러프 → 완성 그림 → 이어서 영상"]].forEach(function (pair) {
        var label = el("label", "vpoc-radio");
        var radio = el("input");
        radio.type = "radio";
        radio.name = "vpoc-make-mode";
        radio.checked = currentMode === pair[0];
        radio.disabled = !editable;
        radio.addEventListener("change", function () {
          if (radio.checked) edits.makeMode = pair[0];
        });
        label.appendChild(radio);
        label.appendChild(el("span", null, pair[1]));
        modeRow.appendChild(label);
      });
      modeBlock.appendChild(modeRow);
      var totalRow = el("div", "vpoc-radios");
      totalRow.appendChild(el("span", "vpoc-hint", "키 장면 방식 영상 길이"));
      var currentTotal = Number(step.totalSeconds || 30);
      edits.totalSeconds = edits.totalSeconds || currentTotal;
      [20, 30, 40, 60].forEach(function (value) {
        var label = el("label", "vpoc-radio");
        var radio = el("input");
        radio.type = "radio";
        radio.name = "vpoc-total-seconds";
        radio.checked = currentTotal === value;
        radio.disabled = !editable;
        radio.addEventListener("change", function () {
          if (radio.checked) edits.totalSeconds = value;
        });
        label.appendChild(radio);
        label.appendChild(el("span", null, value + "초 (키 장면 " + (value / 5 + 1) + "장)"));
        totalRow.appendChild(label);
      });
      modeBlock.appendChild(totalRow);
      modeBlock.appendChild(
        el("p", "vpoc-hint", "키 장면 방식은 Seedance 모델에서만 쓰입니다. 키 장면 간격은 5초입니다(Seedance 최소 4초).")
      );
      section.appendChild(modeBlock);
    }

    /* 캐릭터 시트 */
    section.appendChild(el("h3", null, "캐릭터"));
    (step.characters || []).forEach(function (character) {
      section.appendChild(characterSheet(character, editable, edits));
    });

    /* 질문 */
    if ((step.questions || []).length) {
      section.appendChild(el("h3", null, "몇 가지만 알려 주세요"));
      (step.questions || []).forEach(function (question) {
        var block = el("div", "vpoc-question");
        block.appendChild(el("p", "vpoc-question-text", question.questionKo));
        if (question.whyKo) block.appendChild(el("p", "vpoc-hint", question.whyKo));
        var answer = (step.answers || {})[question.id] || { index: question.defaultIndex, text: null };
        (question.optionsKo || []).forEach(function (option, index) {
          var label = el("label", "vpoc-radio");
          var radio = el("input");
          radio.type = "radio";
          radio.name = "vpoc-q-" + question.id;
          radio.checked = !answer.text && Number(answer.index) === index;
          radio.disabled = !editable;
          radio.addEventListener("change", function () {
            if (radio.checked) edits.answers[question.id] = { index: index, text: null };
          });
          label.appendChild(radio);
          label.appendChild(el("span", null, option));
          block.appendChild(label);
        });
        var own = el("input", "vpoc-input");
        own.type = "text";
        own.placeholder = "직접 입력 (선택)";
        own.value = answer.text || "";
        own.disabled = !editable;
        own.addEventListener("input", function () {
          edits.answers[question.id] = { index: 0, text: own.value || null };
        });
        block.appendChild(own);
        section.appendChild(block);
      });
    }

    /* 샷 수 (설계자 2026-10-08). 0 = 자동(AI 가 장면에 맞춰 3~6개). 바꾸면 ③ 계획을 다시 만든다. */
    var range = plan.shotCountRange || [3, 6];
    var countBlock = el("div", "vpoc-question");
    countBlock.appendChild(el("p", "vpoc-question-text", "몇 개의 샷으로 나눌까요?"));
    countBlock.appendChild(el("p", "vpoc-hint", "샷 하나가 영상의 한 장면입니다. 자동이면 장면에 맞춰 AI 가 정합니다."));
    var countRow = el("div", "vpoc-radios");
    var currentCount = Number(step.shotCount || 0);
    var countChoices = [[0, "자동"]];
    for (var n = range[0]; n <= range[1]; n += 1) countChoices.push([n, n + "개"]);
    countChoices.forEach(function (pair) {
      var label = el("label", "vpoc-radio");
      var radio = el("input");
      radio.type = "radio";
      radio.name = "vpoc-shot-count";
      radio.checked = currentCount === pair[0];
      radio.disabled = !editable;
      radio.addEventListener("change", function () {
        if (radio.checked) edits.shotCount = pair[0];
      });
      label.appendChild(radio);
      label.appendChild(el("span", null, pair[1]));
      countRow.appendChild(label);
    });
    countBlock.appendChild(countRow);
    section.appendChild(countBlock);

    var extra = el("div", "vpoc-field");
    extra.appendChild(el("label", null, "추가 요청 (선택)"));
    var extraInput = el("textarea", "vpoc-input");
    extraInput.rows = 2;
    extraInput.value = step.extraKo || "";
    extraInput.disabled = !editable;
    extraInput.addEventListener("input", function () {
      edits.extraKo = extraInput.value;
    });
    extra.appendChild(extraInput);
    section.appendChild(extra);

    if (editable) {
      var next = el("button", "vpoc-btn vpoc-btn-primary vpoc-btn-run", "캐릭터 \u00b7 장면 만들기");
      next.type = "button";
      next.addEventListener("click", function () {
        var payload = {
          story: edits.story,
          characters: Object.keys(edits.characters).map(function (id) {
            var row = edits.characters[id];
            row.id = id;
            return row;
          }),
          answers: edits.answers,
          extraKo: edits.extraKo
        };
        if (edits.videoModel) payload.videoModel = edits.videoModel;
        if (edits.shotCount !== undefined) payload.shotCount = edits.shotCount;
        if (edits.makeMode) payload.makeMode = edits.makeMode;
        if (edits.totalSeconds) payload.totalSeconds = edits.totalSeconds;
        var later = plan.steps["3"];
        var dropCount =
          ((later.characters || []).length ? (later.characters || []).length : 0) +
          ((later.shots || []).length ? (later.shots || []).length : 0);
        var proceed = function () {
          saveStep(2, payload).then(function () {
            return runAction("prepare", {});
          });
        };
        if (dropCount > 0) {
          confirmModal(
            "다시 만들까요?",
            [
              "이 단계를 다시 만들면 뒤 단계 결과가 바뀝니다.",
              "버려질 것: 캐릭터 이미지 " +
                (later.characters || []).length +
                "장, 샷 계획 " +
                (later.shots || []).length +
                "개, 샷 영상 " +
                ((plan.steps["4"].shots || []).filter(function (item) {
                  return item.status === "completed";
                }).length) +
                "개.",
              "생성 중인 영상은 취소되지 않고 비용이 발생합니다."
            ],
            "계속",
            proceed
          );
        } else {
          proceed();
        }
      });
      next.disabled = state.busy;
      section.appendChild(next);
    }
    host.appendChild(section);
  }

  function characterSheet(character, editable, edits) {
    var TABS = [
      ["appearanceKo", "외형"],
      ["bodyKo", "체형"],
      ["personalityKo", "성격"],
      ["traitsKo", "특징"],
      ["likesKo", "취향"]
    ];
    var box = el("div", "vpoc-sheet");
    var head = el("div", "vpoc-sheet-head");
    var name = el("input", "vpoc-input vpoc-sheet-name");
    name.type = "text";
    name.value = character.nameKo || "";
    name.disabled = !editable;
    name.addEventListener("input", function () {
      edits.characters[character.id] = edits.characters[character.id] || {};
      edits.characters[character.id].nameKo = name.value;
    });
    head.appendChild(name);
    head.appendChild(
      el(
        "span",
        "vpoc-badge vpoc-badge-" + (character.source || "ai"),
        character.source === "user" ? "사용자" : character.source === "image" ? "이미지" : "AI"
      )
    );
    if (character.blPosition) head.appendChild(el("span", "vpoc-badge", character.blPosition));
    box.appendChild(head);

    if (character.roleKo) box.appendChild(el("p", "vpoc-hint", character.roleKo));

    var tabRow = el("div", "vpoc-tabs");
    var bodyBox = el("div", "vpoc-tabbody");
    var aiFilled = character.aiFilled || [];

    function showTab(key, labelText) {
      clear(bodyBox);
      var wrap = el("div", "vpoc-field");
      var label = el("label", null, labelText);
      if (aiFilled.indexOf(key) >= 0) {
        label.appendChild(el("span", "vpoc-badge vpoc-badge-ai", "AI"));
      }
      wrap.appendChild(label);
      var input = el("textarea", "vpoc-input");
      input.rows = 2;
      input.value = character[key] || "";
      input.disabled = !editable;
      input.addEventListener("input", function () {
        edits.characters[character.id] = edits.characters[character.id] || {};
        edits.characters[character.id][key] = input.value;
        character[key] = input.value;
      });
      wrap.appendChild(input);
      bodyBox.appendChild(wrap);
    }

    TABS.forEach(function (pair, index) {
      var tab = el("button", "vpoc-tab", pair[1]);
      tab.type = "button";
      if (index === 0) tab.classList.add("is-active");
      tab.addEventListener("click", function () {
        Array.prototype.forEach.call(tabRow.children, function (node) {
          node.classList.remove("is-active");
        });
        tab.classList.add("is-active");
        showTab(pair[0], pair[1]);
      });
      tabRow.appendChild(tab);
    });
    box.appendChild(tabRow);
    box.appendChild(bodyBox);
    showTab(TABS[0][0], TABS[0][1]);
    return box;
  }

  /* ---------------------------------------------------------------- ③ 계획 승인 */

  function renderStep3(host) {
    var plan = state.plan;
    var step = plan.steps["3"];
    var step2 = plan.steps["2"];
    var editable = plan.canEdit;
    var sheetById = {};
    (step2.characters || []).forEach(function (item) {
      sheetById[item.id] = item;
    });
    var keep = (step.shots || []).map(function (shot) {
      return shot.id;
    });
    var promptEdits = {};

    var section = el("section", "vpoc-card");
    section.appendChild(el("h2", null, "\u2462 이렇게 만들까요?"));
    if (step.status === "stale") {
      section.appendChild(el("p", "vpoc-stale", "앞 단계를 고쳤습니다. 다시 만들어야 합니다."));
    }

    /* 캐릭터 카드 */
    var cards = el("div", "vpoc-cards");
    (step.characters || []).forEach(function (entry) {
      var sheet = sheetById[entry.id] || {};
      var image = entry.image || {};
      var card = el("div", "vpoc-imagecard");
      if (image.url) {
        var img = el("img");
        img.src = image.url;
        img.alt = (sheet.nameKo || entry.id) + " 캐릭터 이미지";
        card.appendChild(img);
      } else {
        card.appendChild(el("div", "vpoc-imageph", image.status === "error" ? "이미지 없음" : "준비 중"));
      }
      card.appendChild(el("strong", null, sheet.nameKo || entry.id));
      card.appendChild(
        el("span", "vpoc-badge vpoc-badge-" + (image.source || "ai"), image.source === "user" ? "사용자" : "AI")
      );
      if (image.error) card.appendChild(errorBox(image.error, null));
      if (editable) {
        var redraw = el("button", "vpoc-btn vpoc-btn-small", "다시 그리기");
        redraw.type = "button";
        redraw.addEventListener("click", function () {
          runAction("redraw", { charId: entry.id });
        });
        card.appendChild(redraw);
      }
      cards.appendChild(card);
    });
    section.appendChild(cards);

    /* 키 장면 콘티(설계자 2026-10-14) — 5초마다 러프 스케치. 설명을 고쳐 다시 그릴 수 있다. */
    var keyframeMode = step.makeMode === "keyframes";
    if (keyframeMode) {
      section.appendChild(el("h3", null, "키 장면 콘티 " + (step.keyframes || []).length + "장 (5초 간격)"));
      section.appendChild(
        el("p", "vpoc-hint", "러프 스케치입니다. 승인하면 인물 그림과 웹툰 화풍을 입혀 완성 그림으로 그린 뒤, 장면 사이를 영상으로 잇습니다.")
      );
      var board = el("div", "vpoc-cards");
      (step.keyframes || []).forEach(function (kf) {
        var card = el("div", "vpoc-imagecard");
        var shown = (kf.final && kf.final.url) || (kf.rough && kf.rough.url);
        if (shown) {
          var img = el("img");
          img.src = shown;
          img.alt = kf.t + "초 키 장면";
          card.appendChild(img);
        } else {
          card.appendChild(el("div", "vpoc-imageph", (kf.rough || {}).status === "error" ? "그림 없음" : "준비 중"));
        }
        card.appendChild(el("strong", null, kf.t + "초"));
        card.appendChild(el("span", "vpoc-badge", kf.final && kf.final.url ? "완성" : "러프"));
        card.appendChild(el("p", "vpoc-hint", kf.summaryKo || ""));
        if ((kf.rough || {}).error) card.appendChild(errorBox(kf.rough.error, null));
        if (editable) {
          var more = el("details", "vpoc-advanced");
          more.appendChild(el("summary", null, "장면 설명 고치기"));
          var area = el("textarea", "vpoc-input");
          area.rows = 4;
          area.value = kf.sceneEn || "";
          more.appendChild(area);
          card.appendChild(more);
          var again = el("button", "vpoc-btn vpoc-btn-small", "다시 그리기");
          again.type = "button";
          again.addEventListener("click", function () {
            runAction("redraw", { keyframeId: kf.id, sceneEn: area.value });
          });
          card.appendChild(again);
        }
        board.appendChild(card);
      });
      section.appendChild(board);
    }

    /* 영상 설정 — 화질 · 샷 길이 (설계자 2026-10-07). 고를 수 있는 범위는 서버가 모델별로 준다. */
    var options = plan.videoOptions || { resolutions: ["540p"], minDurationS: 5, maxDurationS: 5, durationFixed: true };
    var chosenResolution = step.resolution || options.defaultResolution || options.resolutions[0];
    var settingsBlock = el("div", "vpoc-field");
    settingsBlock.appendChild(el("label", null, "영상 설정"));
    var resRow = el("div", "vpoc-radios");
    resRow.appendChild(el("span", "vpoc-hint", "화질"));
    options.resolutions.forEach(function (value) {
      var label = el("label", "vpoc-radio");
      var radio = el("input");
      radio.type = "radio";
      radio.name = "vpoc-resolution";
      radio.value = value;
      radio.checked = chosenResolution === value;
      radio.disabled = !editable;
      radio.addEventListener("change", function () {
        if (radio.checked) chosenResolution = value;
      });
      label.appendChild(radio);
      label.appendChild(el("span", null, value));
      resRow.appendChild(label);
    });
    settingsBlock.appendChild(resRow);
    settingsBlock.appendChild(
      el(
        "p",
        "vpoc-hint",
        options.durationFixed
          ? "샷 길이: " + options.maxDurationS + "초 고정 (이 모델의 최대)"
          : "샷 길이: 샷마다 " + options.minDurationS + "~" + options.maxDurationS + "초 사이로 정할 수 있습니다."
      )
    );
    if (options.referenceImages) {
      settingsBlock.appendChild(
        el(
          "p",
          "vpoc-hint",
          "캐릭터 유지: 각 샷에 나오는 캐릭터 이미지(올린 이미지가 있으면 그것, 없으면 위에서 승인한 AI 이미지)를 " +
            "참고 이미지로 함께 보내고, 그 캐릭터 그대로 만들도록 프롬프트에 지시합니다."
        )
      );
    }
    /* A안 "한 번에 이어서 만들기"(설계자 2026-10-08, Seedance 만). 샷들을 시간대별 지시 하나로 합쳐
       영상 하나로 이어서 만든다. 샷 길이 합계가 모델 최대를 넘으면 샷별로 만든다. */
    var oneTakeChoice = step.oneTake !== false;
    if (step.oneTakeMaxS && !keyframeMode) {
      var takeLabel = el("label", "vpoc-radio");
      var takeBox = el("input");
      takeBox.type = "checkbox";
      takeBox.checked = oneTakeChoice;
      takeBox.disabled = !editable;
      takeBox.addEventListener("change", function () {
        oneTakeChoice = takeBox.checked;
      });
      takeLabel.appendChild(takeBox);
      takeLabel.appendChild(el("span", null, "한 번에 이어서 만들기 (샷 사이가 끊기지 않게, 권장)"));
      settingsBlock.appendChild(takeLabel);
      var over = step.oneTakeTotalS > step.oneTakeMaxS;
      settingsBlock.appendChild(
        el(
          "p",
          "vpoc-hint",
          "샷 길이 합계 " + step.oneTakeTotalS + "초 / 한 번에 최대 " + step.oneTakeMaxS + "초" +
            (over ? " — 최대를 넘어 샷별로 따로 만듭니다. 샷 길이를 줄이면 한 번에 만들 수 있습니다." : "")
        )
      );
      if (step.oneTakeActive && step.oneTakePromptEn) {
        var takeDetails = el("details", "vpoc-advanced");
        takeDetails.appendChild(
          el("summary", null, "한 번에 보낼 최종 프롬프트 · 참고 이미지 " + (step.oneTakeReferenceCount || 0) + "장")
        );
        var takeArea = el("textarea", "vpoc-input");
        takeArea.rows = 8;
        takeArea.readOnly = true;
        takeArea.value = step.oneTakePromptEn;
        takeDetails.appendChild(takeArea);
        settingsBlock.appendChild(takeDetails);
      }
    }
    // dev 전용(설계자 2026-10-07 "개발까지만"): 모델 고르기가 꺼진 배포(운영)에서는 보이지 않는다.
    if (plan.modelPicker) section.appendChild(settingsBlock);
    var durationEdits = {};

    /* 샷 목록 */
    section.appendChild(el("h3", null, "샷 " + (step.shots || []).length + "개"));
    var list = el("div", "vpoc-shots");
    (step.shots || []).forEach(function (shot, index) {
      var row = el("div", "vpoc-shot");
      if (shot.reviewKo) row.classList.add("is-review");
      row.appendChild(el("span", "vpoc-shot-no", String(index + 1)));
      var body = el("div", "vpoc-shot-body");
      body.appendChild(el("p", "vpoc-shot-summary", shot.summaryKo || ""));
      var meta = el("p", "vpoc-shot-meta");
      meta.appendChild(el("span", null, easyCamera(shot)));
      ((shot.characters || [])).forEach(function (id) {
        meta.appendChild(el("span", "vpoc-chip", (sheetById[id] || {}).nameKo || id));
      });
      if (plan.modelPicker && editable && !options.durationFixed && !keyframeMode) {
        var secWrap = el("label", "vpoc-shot-seconds");
        var secInput = el("input", "vpoc-input");
        secInput.type = "number";
        secInput.min = String(options.minDurationS);
        secInput.max = String(options.maxDurationS);
        secInput.step = "1";
        secInput.value = String(shot.durationS || 5);
        secInput.style.width = "4.5em";
        secInput.addEventListener("input", function () {
          durationEdits[shot.id] = Number(secInput.value);
        });
        secWrap.appendChild(secInput);
        secWrap.appendChild(el("span", null, "초"));
        meta.appendChild(secWrap);
      } else {
        meta.appendChild(el("span", null, (shot.durationS || 5) + "초"));
      }
      body.appendChild(meta);
      if (shot.reviewKo) body.appendChild(el("p", "vpoc-review", shot.reviewKo));

      var advanced = el("details", "vpoc-advanced");
      advanced.appendChild(el("summary", null, "고급: 영어 프롬프트 편집 (" + (shot.promptChars || 0) + "자)"));
      var area = el("textarea", "vpoc-input");
      area.rows = 4;
      area.value = shot.promptEn || "";
      area.disabled = !editable;
      area.addEventListener("input", function () {
        promptEdits[shot.id] = area.value;
      });
      advanced.appendChild(area);
      if (shot.sendPromptEn) {
        // API 를 부르기 전에 실제로 보낼 문장(참고 이미지 지시 포함). 위 장면 문장을 고치면 저장 뒤 다시 만든다.
        advanced.appendChild(
          el("p", "vpoc-hint", "Seedance 로 보낼 최종 프롬프트 · 참고 이미지 " + (shot.referenceCount || 0) + "장")
        );
        var sent = el("textarea", "vpoc-input");
        sent.rows = 5;
        sent.value = shot.sendPromptEn;
        sent.readOnly = true;
        advanced.appendChild(sent);
      }
      body.appendChild(advanced);
      row.appendChild(body);

      if (editable) {
        var remove = el("button", "vpoc-btn vpoc-btn-small", "삭제");
        remove.type = "button";
        remove.addEventListener("click", function () {
          keep = keep.filter(function (id) {
            return id !== shot.id;
          });
          row.classList.add("is-removed");
          remove.disabled = true;
        });
        row.appendChild(remove);
      }
      list.appendChild(row);
    });
    section.appendChild(list);

    /* 만들 분량 — 금액은 보이지 않는다(설계자 2026-10-07, admin 모델 관리에서만 본다) */
    var cost = step.cost || {};
    section.appendChild(el("p", "vpoc-cost", costLabel(plan, cost)));

    if (editable) {
      var blocked = (step.shots || []).some(function (shot) {
        return Boolean(shot.reviewKo);
      });
      var approve = el(
        "button",
        "vpoc-btn vpoc-btn-primary vpoc-btn-run",
        "승인하고 영상 만들기 · " + (cost.shots || 0) + "샷"
      );
      approve.type = "button";
      approve.disabled = state.busy || blocked;
      if (blocked) {
        section.appendChild(
          el("p", "vpoc-hint", "'검토 필요' 샷이 있습니다. 고급 편집으로 고치거나 샷을 지운 뒤 승인할 수 있습니다.")
        );
      }
      approve.addEventListener("click", function () {
        var byId = {};
        Object.keys(promptEdits).forEach(function (id) {
          byId[id] = { id: id, promptEn: promptEdits[id] };
        });
        Object.keys(durationEdits).forEach(function (id) {
          byId[id] = byId[id] || { id: id };
          byId[id].durationS = durationEdits[id];
        });
        var shotEdits = Object.keys(byId).map(function (id) {
          return byId[id];
        });
        var step3Body = { keepShotIds: keep, shots: shotEdits, useKeyframes: Boolean(step.useKeyframes) };
        if (plan.modelPicker) step3Body.resolution = chosenResolution;
        if (plan.modelPicker && step.oneTakeMaxS && !keyframeMode) step3Body.oneTake = oneTakeChoice;
        saveStep(3, step3Body)
          .then(function () {
            var current = state.plan.steps["3"].cost || {};
            confirmModal(
              "영상을 만들까요?",
              [
                costLabel(state.plan, current) + " 영상을 만듭니다.",
                "시작하면 취소할 수 없습니다."
              ],
              "시작",
              function () {
                runAction("generate", { confirmUsd: current.totalUsd });
              }
            );
          });
      });
      section.appendChild(approve);
    }
    host.appendChild(section);
  }

  /* ---------------------------------------------------------------- ④ 영상 */

  function keyframeStrip(plan) {
    var frames = ((plan.steps["3"] || {}).keyframes || []).filter(function (kf) {
      return (kf.final && kf.final.url) || (kf.rough && kf.rough.url);
    });
    if (!frames.length) return null;
    var box = el("div", "vpoc-field");
    box.appendChild(el("label", null, "키 장면 (완성 그림이 준비되면 바뀝니다)"));
    var row = el("div", "vpoc-cards");
    frames.forEach(function (kf) {
      var card = el("div", "vpoc-imagecard");
      var img = el("img");
      img.src = (kf.final && kf.final.url) || kf.rough.url;
      img.alt = kf.t + "초";
      card.appendChild(img);
      card.appendChild(el("strong", null, kf.t + "초"));
      card.appendChild(el("span", "vpoc-badge", kf.final && kf.final.url ? "완성" : "러프"));
      row.appendChild(card);
    });
    box.appendChild(row);
    return box;
  }

  function renderStep4(host) {
    var plan = state.plan;
    var step = plan.steps["4"];
    var strip = keyframeStrip(plan);
    if (strip) host.appendChild(strip);
    var step3 = plan.steps["3"];
    var summaryById = {};
    (step3.shots || []).forEach(function (shot) {
      summaryById[shot.id] = shot.summaryKo || "";
    });

    var section = el("section", "vpoc-card");
    section.appendChild(el("h2", null, "\u2463 영상"));

    var done = (step.shots || []).filter(function (shot) {
      return shot.status === "completed" && shot.videoUrl;
    });

    if ((step.final || {}).url) {
      section.appendChild(el("h3", null, "합본"));
      section.appendChild(videoTag((step.final || {}).url, false, "final"));
      var download = el("a", "vpoc-btn", "내려받기");
      download.href = (step.final || {}).url;
      download.setAttribute("download", "video.mp4");
      section.appendChild(download);
    } else if (done.length) {
      section.appendChild(el("h3", null, "순서대로 재생"));
      section.appendChild(sequencePlayer(done));
      if (plan.canEdit && step.status === "done") {
        var stitchButton = el("button", "vpoc-btn", "하나로 이어 붙이기");
        stitchButton.type = "button";
        stitchButton.addEventListener("click", function () {
          runAction("stitch", {});
        });
        section.appendChild(stitchButton);
      }
    }

    var list = el("div", "vpoc-shots");
    (step.shots || []).forEach(function (shot, index) {
      var row = el("div", "vpoc-shot");
      row.appendChild(el("span", "vpoc-shot-no", String(index + 1)));
      var body = el("div", "vpoc-shot-body");
      var isTake = Boolean(step.oneTake) && shot.id === "take";
      body.appendChild(
        el(
          "p",
          "vpoc-shot-summary",
          isTake
            ? "한 번에 이어서 만든 영상 · 샷 " + (step3.shots || []).length + "개 · " +
              (step3.shots || []).reduce(function (sum, item) {
                return sum + (item.durationS || 5);
              }, 0) + "초"
            : summaryById[shot.id] || shot.id
        )
      );

      var chip = el("span", "vpoc-status vpoc-status-" + shot.status);
      var text = {
        queued: "대기",
        submitted: "생성 중",
        completed: "완료",
        failed: "실패"
      }[shot.status] || shot.status;
      if (shot.status === "submitted") {
        text += " " + mmss(shot.elapsedS) + " \u00b7 보통 2~5분";
        if (shot.elapsedS > 600) text += " \u00b7 평소보다 오래 걸려요";
      } else if (shot.status === "completed") {
        text += " " + mmss(shot.elapsedS);
      }
      chip.textContent = text;
      body.appendChild(chip);
      if (shot.note) body.appendChild(el("p", "vpoc-hint", shot.note));
      if (shot.mode === "keyframe") body.appendChild(el("span", "vpoc-badge", "키프레임"));

      if (shot.status === "completed" && shot.videoUrl) {
        body.appendChild(videoTag(shot.videoUrl, true, "shot"));
      }
      if (shot.status === "failed" && shot.error) {
        body.appendChild(errorBox(shot.error, null));
        if (plan.canEdit) {
          var retry = el("button", "vpoc-btn vpoc-btn-small", "다시 시도");
          retry.type = "button";
          retry.addEventListener("click", function () {
            confirmModal(
              "이 샷을 다시 만들까요?",
              ["이 샷을 다시 만듭니다."],
              "다시 만들기",
              function () {
                runAction("retry", { shotId: shot.id });
              }
            );
          });
          body.appendChild(retry);
        }
      }
      // vpoc-regen: "이 샷만 의견 반영해 다시 만들기". 위 요소는 건드리지 않고 아래에 덧붙인다.
      // 한 번에 만든 영상은 샷 하나만 고칠 수 없다 — ③ 을 고쳐 다시 승인한다.
      if (isTake) {
        body.appendChild(el("p", "vpoc-hint", "고치려면 ③ 계획 승인에서 샷 내용을 바꿔 다시 승인해 주세요."));
      } else {
        vpocRegenAttach(body, shot, plan);
      }
      row.appendChild(body);
      list.appendChild(row);
    });
    section.appendChild(list);

    if ((step.final || {}).status === "stale") {
      var staleBand = el("div", "vpoc-regen-stale");
      staleBand.appendChild(el("span", null, "합본이 이전 버전입니다."));
      if (plan.canEdit) {
        var again = el("button", "vpoc-btn vpoc-btn-small", "다시 이어 붙이기");
        again.type = "button";
        again.addEventListener("click", function () {
          runAction("stitch", {});
        });
        staleBand.appendChild(again);
      }
      section.appendChild(staleBand);
    }

    if (step.status === "done") {
      var note = el("p", "vpoc-done", "프로젝트에 저장했습니다.");
      section.appendChild(note);
      var open = el("button", "vpoc-btn", "프로젝트 보기");
      open.type = "button";
      open.addEventListener("click", function () {
        state.view = "project";
        refresh();
      });
      section.appendChild(open);
    }
    host.appendChild(section);
  }

  /* ------------------------------------------- vpoc-regen: 이 샷만 다시 만들기 */

  /* 계획 문서 2·6장. 기존 샷 카드 요소·이벤트는 건드리지 않고 아래에 덧붙인다.
     함수는 vpocRegen 접두사, CSS 클래스는 vpoc-regen- 접두사. */

  var REGEN_CHIPS = [
    ["표정 바꾸기", "표정을 "],
    ["움직임 바꾸기", "움직임을 "],
    ["카메라 바꾸기", "카메라를 "],
    ["다른 컷과 얼굴 맞추기", "다른 컷과 얼굴을 맞춰 주세요. "],
    ["배경 정리", "배경을 정리해 주세요. "]
  ];
  var REGEN_LIMIT_NOTE =
    "지금 영상 모델은 캐릭터 얼굴을 완전히 고정하지 못해요. 다시 만들어도 다른 컷과 똑같아지지 않을 수 있어요.";

  // 샷별로 펼침 상태와 입력값을 기억한다. 조회로 화면을 다시 그려도 입력이 사라지지 않는다.
  var regenUi = {};

  function regenState(shotId) {
    if (!regenUi[shotId]) {
      regenUi[shotId] = { open: false, feedback: "", useKeyframe: true, promptEdit: null, versionsOpen: false };
    }
    return regenUi[shotId];
  }

  function vpocRegenAvailable(plan) {
    // 서버가 기능을 켰는지. 옛 배포(키가 없음)에서는 버튼을 보이지 않는다.
    return plan && plan.regenEnabled === true && plan.canEdit === true;
  }

  function vpocRegenKeyframePossible(shot, plan) {
    var step3 = (plan.steps && plan.steps["3"]) || {};
    var planShot = (step3.shots || []).filter(function (item) {
      return item.id === shot.id;
    })[0];
    if (!planShot || (planShot.characters || []).length !== 1) return false;
    var entry = (step3.characters || []).filter(function (item) {
      return item.id === planShot.characters[0];
    })[0];
    var image = (entry || {}).image || {};
    return image.source === "ai" && Boolean(image.url);
  }

  function vpocRegenAttach(body, shot, plan) {
    if (!vpocRegenAvailable(plan)) {
      // 보기 전용이어도 버전이 여러 개면 목록은 보여 준다.
      if ((shot.versions || []).length > 1) body.appendChild(vpocRegenVersions(shot, plan, false));
      return;
    }
    var ui = regenState(shot.id);
    var busy = (plan.task || {}).status === "running";
    var regen = shot.regen;
    var running = regen && (regen.status === "queued" || regen.status === "submitted");

    if (running) {
      body.appendChild(vpocRegenProgress(regen));
    } else if (regen && regen.status === "completed") {
      body.appendChild(vpocRegenCompare(shot, regen, plan));
    } else if (regen && regen.status === "failed") {
      var failBox = el("div", "vpoc-regen-panel");
      failBox.appendChild(el("p", "vpoc-regen-title", "새 버전 만들기가 실패했습니다"));
      if (regen.error) failBox.appendChild(errorBox(regen.error, null));
      failBox.appendChild(el("p", "vpoc-hint", "이전 버전을 그대로 쓰고 있습니다."));
      var retryFeedback = el("button", "vpoc-btn vpoc-btn-small", "의견 고쳐서 다시");
      retryFeedback.type = "button";
      retryFeedback.addEventListener("click", function () {
        ui.open = true;
        ui.feedback = regen.feedbackKo || ui.feedback;
        refresh();
      });
      failBox.appendChild(retryFeedback);
      body.appendChild(failBox);
    }

    if ((shot.versions || []).length > 1) body.appendChild(vpocRegenVersions(shot, plan, true));

    var canStart =
      !running && (shot.status === "completed" || shot.status === "failed");
    if (!canStart) return;

    if (shot.revision && !ui.open) {
      body.appendChild(vpocRegenRevision(shot, plan));
      return;
    }

    if (!ui.open) {
      var open = el("button", "vpoc-btn vpoc-btn-small vpoc-regen-open", "이 샷 다시 만들기");
      open.type = "button";
      open.disabled = busy;
      open.addEventListener("click", function () {
        ui.open = true;
        refresh();
      });
      body.appendChild(open);
      return;
    }
    body.appendChild(vpocRegenPanel(shot, plan));
  }

  function vpocRegenProgress(regen) {
    var band = el("div", "vpoc-regen-progress");
    band.appendChild(el("span", "vpoc-spinner"));
    var elapsed = regen.submittedAt
      ? Math.round((Date.now() - Date.parse(regen.submittedAt)) / 1000)
      : 0;
    var text =
      regen.status === "queued"
        ? "새 버전 대기 중 \u00b7 앞 작업이 끝나면 시작해요"
        : "새 버전 만드는 중 \u00b7 " + mmss(elapsed) + " \u00b7 보통 2~5분";
    if (regen.status === "submitted" && elapsed > 600) text += " \u00b7 평소보다 오래 걸려요";
    band.appendChild(el("span", null, text));
    if (regen.note) band.appendChild(el("span", "vpoc-badge", regen.note));
    return band;
  }

  function vpocRegenPanel(shot, plan) {
    var ui = regenState(shot.id);
    var panel = el("div", "vpoc-regen-panel");
    panel.appendChild(el("p", "vpoc-regen-title", "어떻게 바꾸고 싶은지 적어 주세요"));

    var chips = el("div", "vpoc-regen-chips");
    REGEN_CHIPS.forEach(function (pair) {
      var chip = el("button", "vpoc-regen-chip", pair[0]);
      chip.type = "button";
      chip.addEventListener("click", function () {
        var current = area.value;
        area.value = (current ? current.replace(/\s*$/, " ") : "") + pair[1];
        ui.feedback = area.value;
        area.focus();
        count.textContent = ui.feedback.length + " / 500";
        make.disabled = !ui.feedback.trim();
      });
      chips.appendChild(chip);
    });
    panel.appendChild(chips);

    var area = el("textarea", "vpoc-input");
    area.rows = 3;
    area.maxLength = 500;
    area.placeholder = "예) 울기보다 이를 악물고 참는 표정, 얼굴을 조금 더 가까이";
    area.value = ui.feedback;
    panel.appendChild(area);

    var count = el("p", "vpoc-hint", ui.feedback.length + " / 500");
    panel.appendChild(count);

    var keyframeBox = null;
    if (vpocRegenKeyframePossible(shot, plan)) {
      var label = el("label", "vpoc-radio");
      keyframeBox = el("input");
      keyframeBox.type = "checkbox";
      keyframeBox.checked = ui.useKeyframe;
      keyframeBox.addEventListener("change", function () {
        ui.useKeyframe = keyframeBox.checked;
      });
      label.appendChild(keyframeBox);
      label.appendChild(el("span", null, "캐릭터 이미지로 시작하기 (일관성이 조금 나아질 수 있어요)"));
      panel.appendChild(label);
    }

    var actions = el("div", "vpoc-regen-actions");
    var make = el("button", "vpoc-btn vpoc-btn-primary vpoc-btn-small", "수정안 만들기");
    make.type = "button";
    make.disabled = !ui.feedback.trim() || state.busy;
    make.addEventListener("click", function () {
      ui.open = false;
      runAction("revise", {
        shotId: shot.id,
        feedbackKo: ui.feedback.trim(),
        useKeyframe: keyframeBox ? keyframeBox.checked : false
      });
    });
    var close = el("button", "vpoc-btn vpoc-btn-small", "닫기");
    close.type = "button";
    close.addEventListener("click", function () {
      ui.open = false;
      refresh();
    });
    actions.appendChild(make);
    actions.appendChild(close);
    panel.appendChild(actions);

    area.addEventListener("input", function () {
      ui.feedback = area.value;
      count.textContent = ui.feedback.length + " / 500";
      make.disabled = !ui.feedback.trim();
    });

    panel.appendChild(el("p", "vpoc-regen-limit", REGEN_LIMIT_NOTE));
    return panel;
  }

  function vpocRegenRevision(shot, plan) {
    var ui = regenState(shot.id);
    var revision = shot.revision;
    var step3 = (plan.steps && plan.steps["3"]) || {};
    var planShot = (step3.shots || []).filter(function (item) {
      return item.id === shot.id;
    })[0] || {};

    var panel = el("div", "vpoc-regen-panel");
    panel.appendChild(el("p", "vpoc-regen-title", "이렇게 바꿀까요?"));
    panel.appendChild(el("p", "vpoc-hint", "의견: " + (revision.feedbackKo || "")));

    var compare = el("div", "vpoc-regen-compare");
    var before = el("div", "vpoc-regen-col");
    before.appendChild(el("h4", null, "현재"));
    before.appendChild(el("p", null, planShot.summaryKo || ""));
    before.appendChild(el("p", "vpoc-hint", planShot.cameraKo || ""));
    var after = el("div", "vpoc-regen-col vpoc-regen-col-new");
    after.appendChild(el("h4", null, "수정안"));
    after.appendChild(el("p", null, revision.summaryKo || ""));
    after.appendChild(el("p", "vpoc-hint", revision.cameraKo || ""));
    compare.appendChild(before);
    compare.appendChild(after);
    panel.appendChild(compare);

    if (revision.notesKo) {
      panel.appendChild(el("p", "vpoc-regen-note", "AI 메모: " + revision.notesKo));
    }
    if (revision.useKeyframe) {
      panel.appendChild(el("span", "vpoc-badge", "캐릭터 이미지로 시작"));
    }
    if (revision.reviewKo) {
      panel.appendChild(el("p", "vpoc-review", revision.reviewKo));
    }

    var advanced = el("details", "vpoc-advanced");
    advanced.appendChild(
      el("summary", null, "고급: 영어 프롬프트 편집 (" + (revision.promptChars || 0) + "자)")
    );
    var promptArea = el("textarea", "vpoc-input");
    promptArea.rows = 5;
    promptArea.value = ui.promptEdit === null ? revision.promptEn || "" : ui.promptEdit;
    var promptCount = el("p", "vpoc-hint", promptArea.value.length + " / 1200");
    promptArea.addEventListener("input", function () {
      ui.promptEdit = promptArea.value;
      promptCount.textContent = promptArea.value.length + " / 1200";
    });
    advanced.appendChild(promptArea);
    advanced.appendChild(promptCount);
    panel.appendChild(advanced);

    var actions = el("div", "vpoc-regen-actions");
    // 서버 확인용 금액(화면에는 보이지 않는다). 샷마다 길이가 달라 그 샷의 값을 쓴다.
    var planShot = ((plan.steps["3"] || {}).shots || []).filter(function (item) {
      return item.id === shot.id;
    })[0];
    var amount = planShot && planShot.usd != null ? planShot.usd : ((plan.steps["3"] || {}).cost || {}).perShotUsd;
    var go = el(
      "button",
      "vpoc-btn vpoc-btn-primary vpoc-btn-small",
      "이대로 다시 만들기"
    );
    go.type = "button";
    go.disabled = state.busy || Boolean(revision.reviewKo && ui.promptEdit === null);
    go.addEventListener("click", function () {
      confirmModal(
        "이 샷만 다시 만들까요?",
        [
          "이 샷만 다시 만듭니다.",
          "시작하면 취소할 수 없습니다. 기존 영상은 새 버전을 고르기 전까지 그대로 남습니다."
        ],
        "다시 만들기",
        function () {
          var payload = { shotId: shot.id, revisionId: revision.id, confirmUsd: amount };
          if (ui.promptEdit !== null && ui.promptEdit !== revision.promptEn) {
            payload.promptEn = ui.promptEdit;
          }
          ui.promptEdit = null;
          runAction("regenerate", payload);
        }
      );
    });
    var redo = el("button", "vpoc-btn vpoc-btn-small", "의견 고쳐서 다시");
    redo.type = "button";
    redo.addEventListener("click", function () {
      ui.open = true;
      ui.feedback = revision.feedbackKo || "";
      ui.promptEdit = null;
      refresh();
    });
    var cancel = el("button", "vpoc-btn vpoc-btn-small", "취소");
    cancel.type = "button";
    cancel.addEventListener("click", function () {
      ui.promptEdit = null;
      ui.open = false;
      // 수정안은 서버에 남는다. 다음에 다시 열면 그대로 보인다.
      refresh();
    });
    actions.appendChild(go);
    actions.appendChild(redo);
    actions.appendChild(cancel);
    panel.appendChild(actions);
    panel.appendChild(el("p", "vpoc-regen-limit", REGEN_LIMIT_NOTE));
    return panel;
  }

  function vpocRegenCompare(shot, regen, plan) {
    var panel = el("div", "vpoc-regen-panel");
    panel.appendChild(el("p", "vpoc-regen-title", "어느 버전을 쓸까요?"));

    var compare = el("div", "vpoc-regen-compare");
    var oldCol = el("div", "vpoc-regen-col");
    oldCol.appendChild(el("h4", null, "이전 버전 (버전 " + shot.selectedAttempt + ")"));
    if (shot.videoUrl) oldCol.appendChild(videoTag(shot.videoUrl, true, "regen-old"));
    var newCol = el("div", "vpoc-regen-col vpoc-regen-col-new");
    newCol.appendChild(el("h4", null, "새 버전 (버전 " + regen.attempt + ")"));
    if (regen.videoUrl) newCol.appendChild(videoTag(regen.videoUrl, true, "regen-new"));
    newCol.appendChild(el("p", "vpoc-hint", regen.summaryKo || ""));
    compare.appendChild(oldCol);
    compare.appendChild(newCol);
    panel.appendChild(compare);
    if (regen.note) panel.appendChild(el("p", "vpoc-hint", regen.note));

    var actions = el("div", "vpoc-regen-actions");
    var use = el("button", "vpoc-btn vpoc-btn-primary vpoc-btn-small", "새 버전 쓰기");
    use.type = "button";
    use.disabled = state.busy;
    use.addEventListener("click", function () {
      runAction("select", { shotId: shot.id, attempt: regen.attempt });
    });
    var keep = el("button", "vpoc-btn vpoc-btn-small", "이전 버전 유지");
    keep.type = "button";
    keep.disabled = state.busy;
    keep.addEventListener("click", function () {
      runAction("select", { shotId: shot.id, attempt: shot.selectedAttempt });
    });
    actions.appendChild(use);
    actions.appendChild(keep);
    panel.appendChild(actions);
    return panel;
  }

  function vpocRegenVersions(shot, plan, canPick) {
    var ui = regenState(shot.id);
    var wrap = el("details", "vpoc-regen-versions");
    wrap.open = ui.versionsOpen;
    wrap.addEventListener("toggle", function () {
      ui.versionsOpen = wrap.open;
    });
    var versions = shot.versions || [];
    wrap.appendChild(
      el(
        "summary",
        null,
        "버전 " + versions.length + "개 \u00b7 버전 " + shot.selectedAttempt + " 사용 중"
      )
    );
    versions.forEach(function (item) {
      var row = el("div", "vpoc-regen-version");
      var head = el("div", "vpoc-regen-version-head");
      head.appendChild(el("strong", null, "버전 " + item.attempt));
      head.appendChild(
        el("span", "vpoc-badge", item.origin === "regen" ? "다시 만듦" : "처음 계획")
      );
      head.appendChild(el("span", "vpoc-status vpoc-status-" + item.status, item.status));
      if (item.attempt === shot.selectedAttempt) {
        head.appendChild(el("span", "vpoc-badge vpoc-badge-ai", "사용 중"));
      }
      row.appendChild(head);
      if (item.feedbackKo) row.appendChild(el("p", "vpoc-hint", "의견: " + item.feedbackKo));
      if (item.summaryKo) row.appendChild(el("p", "vpoc-hint", item.summaryKo));
      if (item.videoUrl) row.appendChild(videoTag(item.videoUrl, true, "version"));
      if (canPick && item.status === "completed" && item.attempt !== shot.selectedAttempt) {
        var pick = el("button", "vpoc-btn vpoc-btn-small", "이 버전 쓰기");
        pick.type = "button";
        pick.disabled = state.busy;
        pick.addEventListener("click", function () {
          runAction("select", { shotId: shot.id, attempt: item.attempt });
        });
        row.appendChild(pick);
      }
      wrap.appendChild(row);
    });
    return wrap;
  }

  /* 완성된 영상 요소를 다시 쓴다(설계자 2026-10-08).
     ④ 에서 다른 샷을 만드는 동안 몇 초마다 화면 전체를 다시 그리는데, 그때마다 <video> 를 새로
     만들고 서버도 매번 새 서명 주소를 줘서 이미 만들어진 영상이 처음부터 다시 로드됐다.
     파일 경로(서명 쿼리를 뺀 주소)가 같으면 같은 요소를 그대로 붙인다 — 같은 작업 안에서 떼었다
     다시 붙이면 브라우저는 재생을 멈추지 않는다. 서명이 만료돼 재생이 실패하면 최신 주소로 바꾼다. */
  var videoCache = {};

  function videoTag(url, small, slot) {
    // slot: 같은 영상이 한 화면 두 곳(샷 목록 · 다시 만들기 비교)에 나올 수 있어 자리마다 따로 둔다.
    var path = String(url || "").split("?")[0] + "#" + (slot || (small ? "small" : "main"));
    var node = videoCache[path];
    if (node) {
      node.dataset.latestSrc = url;
      return node;
    }
    node = el("video", small ? "vpoc-video vpoc-video-small" : "vpoc-video");
    node.src = url;
    node.dataset.latestSrc = url;
    node.controls = true;
    // 설계자 2026-10-14: 영상에 소리가 들어간다 — 기본 음소거를 뺐다(자동 재생은 하지 않는다).
    node.setAttribute("playsinline", "");
    node.addEventListener("error", function () {
      if (node.dataset.latestSrc && node.src !== node.dataset.latestSrc) node.src = node.dataset.latestSrc;
    });
    videoCache[path] = node;
    return node;
  }

  /* 완성된 샷을 순서대로 이어 보는 플레이어.
     ④ 는 샷 하나만 끝나도 이 플레이어를 보여 주는데, 다른 샷을 만드는 동안 몇 초마다 화면을
     다시 그리면서 플레이어도 새로 만들어 1번 영상이 계속 처음부터 다시 로드됐다(설계자 2026-10-08).
     slot 마다 하나를 두고 다시 쓴다. 새 샷이 끝나면 재생 중인 영상은 건드리지 않고 목록만 늘린다. */
  function sequencePlayer(shots, slot) {
    var key = "seq#" + (slot || "step4");
    var cached = videoCache[key];
    if (cached) {
      cached.update(shots);
      return cached.wrap;
    }
    var wrap = el("div", "vpoc-player");
    var list = shots.slice();
    var current = 0;
    var node = el("video", "vpoc-video");
    node.controls = true;
    node.setAttribute("playsinline", "");
    node.src = list[0].videoUrl;
    var label = el("p", "vpoc-hint", "1 / " + list.length);
    node.addEventListener("ended", function () {
      current += 1;
      if (current >= list.length) {
        current = 0;
        label.textContent = "1 / " + list.length;
        node.src = list[0].videoUrl;
        return;
      }
      label.textContent = current + 1 + " / " + list.length;
      node.src = list[current].videoUrl;
      node.play().catch(function () {});
    });
    wrap.appendChild(node);
    wrap.appendChild(label);
    videoCache[key] = {
      wrap: wrap,
      update: function (next) {
        // 서명 주소만 바뀐 같은 목록이면 아무것도 하지 않는다. 다음 샷으로 넘어갈 때 새 주소를 쓴다.
        list = next.slice();
        if (current >= list.length) current = 0;
        label.textContent = current + 1 + " / " + list.length;
      }
    };
    return wrap;
  }

  /* ---------------------------------------------------------------- 프로젝트 보기 */

  function renderProjectView(host) {
    var view = state.project;
    if (!view) {
      host.appendChild(el("p", "vpoc-hint", "불러오는 중..."));
      return;
    }
    /* 1. 영상 */
    var videos = el("section", "vpoc-card");
    videos.appendChild(el("h2", null, "영상"));
    if (!(view.videos || []).length) {
      videos.appendChild(el("p", "vpoc-hint", "아직 만들어진 영상이 없습니다."));
    } else {
      (view.videos || []).forEach(function (item, itemIndex) {
        videos.appendChild(el("h3", null, "완료 " + (item.completedAt || "")));
        if (item.finalUrl) {
          videos.appendChild(videoTag(item.finalUrl, false, "project"));
        } else {
          var playable = (item.shots || []).filter(function (shot) {
            return shot.videoUrl;
          });
          if (playable.length) {
            videos.appendChild(sequencePlayer(playable, "project-" + (item.planId || itemIndex)));
          }
        }
        var list = el("ol", "vpoc-shotlist");
        (item.shots || []).forEach(function (shot) {
          list.appendChild(el("li", null, shot.summaryKo || shot.id));
        });
        videos.appendChild(list);
      });
    }
    host.appendChild(videos);

    /* 2. 캐릭터 */
    var characters = el("section", "vpoc-card");
    characters.appendChild(
      el("h2", null, "캐릭터" + ((view.characters || {}).status === "in_progress" ? " (진행 중)" : ""))
    );
    var items = (view.characters || {}).items || [];
    if (!items.length) {
      characters.appendChild(el("p", "vpoc-hint", "아직 캐릭터가 없습니다."));
    } else {
      var cards = el("div", "vpoc-cards");
      items.forEach(function (item) {
        var card = el("div", "vpoc-imagecard");
        if ((item.image || {}).url) {
          var img = el("img");
          img.src = item.image.url;
          img.alt = (item.nameKo || item.id) + " 캐릭터 이미지";
          card.appendChild(img);
        } else {
          card.appendChild(el("div", "vpoc-imageph", "이미지 없음"));
        }
        card.appendChild(el("strong", null, item.nameKo || item.id));
        var sheet = item.sheet || {};
        [
          ["roleKo", "역할"],
          ["appearanceKo", "외형"],
          ["bodyKo", "체형"],
          ["personalityKo", "성격"],
          ["traitsKo", "특징"],
          ["likesKo", "취향"]
        ].forEach(function (pair) {
          if (sheet[pair[0]]) {
            card.appendChild(el("p", "vpoc-hint", pair[1] + ": " + sheet[pair[0]]));
          }
        });
        cards.appendChild(card);
      });
      characters.appendChild(cards);
    }
    host.appendChild(characters);

    /* 3. 사용자가 입력한 정보와 파일 */
    var inputs = el("section", "vpoc-card");
    inputs.appendChild(el("h2", null, "사용자가 입력한 정보와 파일"));
    if (!(view.inputs || []).length) {
      inputs.appendChild(el("p", "vpoc-hint", "입력 기록이 없습니다."));
    } else {
      (view.inputs || []).forEach(function (entry) {
        inputs.appendChild(el("p", "vpoc-hint", "입력 시각 " + (entry.savedAt || "")));
        var pre = el("pre", "vpoc-inputtext");
        pre.textContent = entry.text || "(글 없음)";
        inputs.appendChild(pre);
        (entry.characters || []).forEach(function (card) {
          inputs.appendChild(
            el("p", null, (card.name || "(이름 없음)") + " \u2014 " + (card.description || ""))
          );
        });
        var files = el("div", "vpoc-files");
        (entry.files || []).forEach(function (file) {
          var link = el("a", "vpoc-filechip");
          link.href = file.url || "#";
          link.target = "_blank";
          link.rel = "noopener";
          if (file.thumbUrl) {
            var img = el("img");
            img.src = file.thumbUrl;
            img.alt = file.name || "업로드 이미지";
            link.appendChild(img);
          }
          link.appendChild(el("span", null, file.name || file.fileId));
          files.appendChild(link);
        });
        inputs.appendChild(files);
      });
    }
    host.appendChild(inputs);

    /* 접힘: 생성된 영어 프롬프트 */
    if ((view.prompts || []).length) {
      var details = el("details", "vpoc-card");
      details.appendChild(el("summary", null, "생성된 영어 프롬프트"));
      (view.prompts || []).forEach(function (item) {
        var pre = el("pre", "vpoc-inputtext");
        pre.textContent = item.shotId + ": " + item.promptEn;
        details.appendChild(pre);
      });
      host.appendChild(details);
    }

    if (view.canEdit) {
      var resume = el("button", "vpoc-btn vpoc-btn-primary", "영상 계획 이어서 하기");
      resume.type = "button";
      resume.addEventListener("click", function () {
        state.view = "flow";
        if (!state.plan) {
          loadOrCreatePlan();
        } else {
          refresh();
        }
      });
      host.appendChild(resume);
    }
  }

  /* ---------------------------------------------------------------- 동작 */

  function saveStep(number, body) {
    if (state.mock) return Promise.resolve(state.plan);
    state.busy = true;
    state.lastError = null;
    refresh();
    return api(
      "/api/vpoc/p/" + encodeURIComponent(state.projectId) +
        "/plans/" + encodeURIComponent(state.planId) + "/steps/" + number,
      { method: "PUT", body: JSON.stringify(body || {}) }
    )
      .then(function (payload) {
        state.plan = payload;
        state.busy = false;
        refresh();
        return payload;
      })
      .catch(function (error) {
        state.busy = false;
        state.lastError = error;
        refresh();
        throw error;
      });
  }

  function runAction(action, body) {
    if (state.mock) return Promise.resolve();
    // generate 는 새 생성 회차를 시작한다. 이전 회차의 샷별 화면 상태(의견 패널·버전 목록
    // 펼침·프롬프트 편집)를 버려야 옛 회차 값이 새 ④에 섞이지 않는다.
    if (action === "generate") regenUi = {};
    state.busy = true;
    state.lastError = null;
    refresh();
    return api(
      "/api/vpoc/p/" + encodeURIComponent(state.projectId) +
        "/plans/" + encodeURIComponent(state.planId) + "/run/" + action,
      { method: "POST", body: JSON.stringify(body || {}) }
    )
      .then(function () {
        state.busy = false;
        state.draft.loaded = false;
        // ④는 서버 PlanView 로 전부 다시 그린다. 화면에 이전 회차의 영상 URL을 남기지 않는다.
        state.plan = null;
        return loadPlan();
      })
      .catch(function (error) {
        state.busy = false;
        state.lastError = error;
        refresh();
      });
  }

  function goBackTo(number) {
    if (state.mock) {
      state.plan.currentStep = number;
      refresh();
      return;
    }
    state.busy = true;
    refresh();
    api(
      "/api/vpoc/p/" + encodeURIComponent(state.projectId) +
        "/plans/" + encodeURIComponent(state.planId) + "/back",
      { method: "POST", body: JSON.stringify({ toStep: number }) }
    )
      .then(function (payload) {
        state.plan = payload;
        state.busy = false;
        state.draft.loaded = false;
        refresh();
      })
      .catch(function (error) {
        state.busy = false;
        state.lastError = error;
        refresh();
      });
  }

  function loadPlan() {
    if (state.mock) {
      state.plan = mockPlan();
      refresh();
      return Promise.resolve(state.plan);
    }
    return api(
      "/api/vpoc/p/" + encodeURIComponent(state.projectId) +
        "/plans/" + encodeURIComponent(state.planId)
    )
      .then(function (payload) {
        state.plan = payload;
        refresh();
        return payload;
      })
      .catch(function (error) {
        state.lastError = error;
        refresh();
      });
  }

  function loadProject() {
    if (state.mock) {
      state.project = mockProject();
      return Promise.resolve(state.project);
    }
    return api("/api/vpoc/p/" + encodeURIComponent(state.projectId)).then(function (payload) {
      state.project = payload;
      return payload;
    });
  }

  function loadOrCreatePlan() {
    return api("/api/vpoc/p/" + encodeURIComponent(state.projectId) + "/plans", {
      method: "POST",
      body: JSON.stringify({})
    }).then(function (payload) {
      state.planId = payload.planId;
      state.draft = { text: "", characters: [], sceneFiles: [], loaded: false };
      return loadPlan();
    });
  }

  /* ---------------------------------------------------------------- 렌더 · 조회 */

  function refresh() {
    var host = root();
    if (!host || !state.mounted) return;
    clear(host);

    if (!token()) {
      host.appendChild(el("p", "vpoc-boot", "로그인이 필요합니다."));
      return;
    }
    host.appendChild(header());

    if (state.view === "project") {
      renderProjectView(host);
      schedule();
      return;
    }
    if (!state.plan) {
      var boot = el("div");
      boot.appendChild(el("p", "vpoc-boot", "불러오는 중..."));
      if (state.lastError) boot.appendChild(errorBox(state.lastError, function () {
        start(state.projectId);
      }));
      host.appendChild(boot);
      return;
    }

    host.appendChild(stepBar());
    host.appendChild(progressBand());

    var body = el("div", "vpoc-body");
    var step = state.plan.currentStep || 1;
    if (step === 1) renderStep1(body);
    else if (step === 2) renderStep2(body);
    else if (step === 3) renderStep3(body);
    else renderStep4(body);
    host.appendChild(body);
    schedule();
  }

  function vpocRegenBusy(plan) {
    var shots = ((plan.steps || {})["4"] || {}).shots || [];
    for (var index = 0; index < shots.length; index += 1) {
      var regen = shots[index].regen;
      if (regen && (regen.status === "queued" || regen.status === "submitted")) return true;
    }
    return false;
  }

  function schedule() {
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    if (!state.mounted || state.mock || document.hidden) return;
    var plan = state.plan;
    if (!plan) return;
    var delay = 0;
    if ((plan.task || {}).status === "running") delay = POLL_TASK_MS;
    // vpoc-regen: 재생성은 ④ 전체 상태를 바꾸지 않으므로(G2) 따로 봐야 한다.
    else if ((plan.steps["4"] || {}).status === "running" || vpocRegenBusy(plan)) {
      delay = POLL_VIDEO_MS;
    }
    if (!delay) return;
    state.timer = setTimeout(function () {
      state.timer = null;
      loadPlan();
    }, delay);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (state.timer) {
        clearTimeout(state.timer);
        state.timer = null;
      }
      return;
    }
    if (state.mounted && state.plan) loadPlan();
  });

  /* ---------------------------------------------------------------- mount */

  function start(projectId, planId) {
    state.projectId = String(projectId || "");
    // 프로젝트 화면의 계획 카드에서 "이 계획 열기"로 들어오면 그 계획을 바로 연다(설계자 2026-10-08).
    var requestedPlanId = String(planId || "");
    state.lastError = null;
    state.plan = null;
    state.project = null;
    state.planId = "";
    state.draft = { text: "", characters: [], sceneFiles: [], loaded: false };
    videoCache = {}; // 다른 프로젝트로 가면 이전 영상 요소를 버린다.
    refresh();

    if (state.mock) {
      state.project = mockProject();
      state.plan = mockPlan();
      state.planId = state.plan.planId;
      refresh();
      return;
    }
    if (!state.projectId) {
      state.lastError = {
        where: "vpoc.mount",
        type: "NoProject",
        message: "프로젝트를 먼저 선택해 주세요.",
        httpStatus: 400
      };
      refresh();
      return;
    }

    loadProject()
      .then(function (view) {
        if (requestedPlanId) {
          state.view = "flow";
          state.planId = requestedPlanId;
          return loadPlan();
        }
        // PRD 8.1: 완료된 계획이 있으면 프로젝트 보기, 아니면 진행 중 계획의 현재 단계,
        // 계획이 없고 편집 권한이 있으면 새 계획을 만들어 ①을 연다.
        if ((view.videos || []).length) {
          state.view = "project";
          if (view.activePlanId) {
            state.planId = view.activePlanId;
            return loadPlan().then(function (plan) {
              // 설계자 2026-10-13: 가장 최근 계획이 아직 끝나지 않았으면(중간에 멈춤) 결과 목록이 아니라
              // 그 계획의 멈춘 단계를 연다. 끝난 영상은 단계 막대 위 "프로젝트 보기"로 볼 수 있다.
              var step4 = plan && plan.steps ? plan.steps["4"] || {} : {};
              if (plan && view.canEdit && step4.status !== "done") state.view = "flow";
              refresh();
            });
          }
          refresh();
          return null;
        }
        state.view = "flow";
        if (view.activePlanId) {
          state.planId = view.activePlanId;
          return loadPlan();
        }
        if (!view.canEdit) {
          state.view = "project";
          refresh();
          return null;
        }
        return loadOrCreatePlan();
      })
      .catch(function (error) {
        state.lastError = error;
        refresh();
      });
  }

  window.vpoc = {
    mount: function (projectId, planId) {
      var host = root();
      if (!host) return;
      state.mounted = true;
      try {
        state.mock = /(?:\?|&)vpocMock=1(?:&|$)/.test(window.location.search);
      } catch (error) {
        state.mock = false;
      }
      start(projectId, planId);
    },
    unmount: function () {
      state.mounted = false;
      if (state.timer) {
        clearTimeout(state.timer);
        state.timer = null;
      }
      var host = root();
      if (host) clear(host);
    }
  };

  /* --------------------------------------------- 개발용 픽스처 (PRD 7.3 · 7.4) */

  var MOCK_PLAN = {
    ok: true,
    planId: "pl_20260923_a1b2",
    projectId: "prj_9f3c",
    canEdit: true,
    currentStep: 4,
    maxReachedStep: 4,
    budget: { spentUsd: 22.99, capUsd: null },
    regenEnabled: true,
    task: {
      name: null,
      status: "idle",
      startedAt: null,
      finishedAt: null,
      error: null,
      shotId: null
    },
    steps: {
      "1": {
        status: "saved",
        text: "두 남자는 F1 레이서이고, 둘은 원수지간이다.",
        characters: [],
        sceneFiles: []
      },
      "2": {
        status: "ready",
        story: {
          titleKo: "피트레인의 적",
          loglineKo: "원수였던 두 F1 레이서가 충돌 끝에 서로의 마음을 알게 된다.",
          genre: ["스포츠", "BL", "로맨스"],
          settingKo: "야간 레이스 직후, 조명이 켜진 서킷 피트레인. 젖은 노면.",
          settingEn: "A floodlit racing pit lane at night right after a race",
          style: "live_action",
          styleSuggestion: "live_action",
          notesKo: ""
        },
        characters: [
          {
            id: "c1",
            nameKo: "로만 싱클레어",
            nameEn: "Roman Sinclair",
            source: "user",
            roleKo: "정상을 지키려는 베테랑 레이서",
            blPosition: null,
            appearanceKo: "짧은 하얀 머리, 서양인 얼굴, 검정·노랑 레이스 슈트",
            appearanceEn: "A tall man with short white hair and Western features",
            handleEn: "the white-haired racer",
            bodyKo: "키가 크고 어깨가 넓다",
            bodyEn: "tall and broad-shouldered",
            personalityKo: "자존심이 강하고 냉정하다",
            traitsKo: "헬멧을 벗으면 머리를 쓸어 넘긴다",
            likesKo: "블랙커피",
            aiFilled: ["roleKo", "bodyKo", "personalityKo", "traitsKo", "likesKo"]
          }
        ],
        questions: [
          {
            id: "q1",
            questionKo: "이번 영상에서 보여줄 장면은?",
            optionsKo: ["두 사람이 처음 정면으로 마주치는 장면", "갈등이 폭발하는 장면"],
            defaultIndex: 0,
            whyKo: "이야기 전체를 한 영상에 담을 수 없어요"
          }
        ],
        answers: { q1: { index: 0, text: null } }
      },
      "3": {
        status: "ready",
        useKeyframes: false,
        characters: [
          {
            id: "c1",
            image: { status: "ready", source: "ai", imageId: "img_c1_01", url: null, seed: 1837461, error: null }
          }
        ],
        shots: [
          {
            id: "s1",
            durationS: 5,
            summaryKo: "레이스가 끝난 피트레인, 로만이 헬멧을 벗으며 누군가를 노려본다",
            cameraKo: "허리 위까지, 천천히 다가가기(미디엄 샷, 푸시 인)",
            characters: ["c1"],
            promptEn: "Cinematic live-action film footage. Slow push-in, eye-level medium shot.",
            promptChars: 72,
            useKeyframe: false,
            reviewKo: null
          }
        ],
        cost: { shots: 1, durationS: 5, resolution: "540p", perShotUsd: 3.75, totalUsd: 3.75 }
      },
      "4": {
        status: "done",
        shots: [
          {
            id: "s1",
            status: "completed",
            attempt: 1,
            mode: "text",
            submittedAt: "2026-09-23T05:10:03Z",
            completedAt: "2026-09-23T05:13:40Z",
            elapsedS: 218,
            videoUrl: null,
            note: null,
            error: null,
            selectedAttempt: 1,
            versions: [
              {
                attempt: 1,
                origin: "plan",
                feedbackKo: null,
                summaryKo: "레이스가 끝난 피트레인, 로만이 헬멧을 벗으며 누군가를 노려본다",
                cameraKo: "허리 위까지, 천천히 다가가기(미디엄 샷, 푸시 인)",
                promptEn: "Cinematic live-action film footage. Slow push-in.",
                mode: "text",
                status: "completed",
                videoUrl: null,
                createdAt: "2026-09-23T05:10:03Z"
              },
              {
                attempt: 2,
                origin: "regen",
                feedbackKo: "울기보다 이를 악물고 참는 표정, 얼굴을 조금 더 가까이",
                summaryKo: "로만이 이를 악물고 눈물을 참으며 정면을 노려본다",
                cameraKo: "얼굴을 크게, 천천히 다가가기(클로즈업, 푸시 인)",
                promptEn: "Cinematic live-action film footage. Slow push-in, close-up.",
                mode: "text",
                status: "completed",
                videoUrl: null,
                createdAt: "2026-09-23T06:02:11Z"
              }
            ],
            revision: null,
            regen: {
              attempt: 2,
              status: "completed",
              mode: "text",
              feedbackKo: "울기보다 이를 악물고 참는 표정, 얼굴을 조금 더 가까이",
              summaryKo: "로만이 이를 악물고 눈물을 참으며 정면을 노려본다",
              cameraKo: "얼굴을 크게, 천천히 다가가기(클로즈업, 푸시 인)",
              submittedAt: "2026-09-23T06:02:11Z",
              completedAt: "2026-09-23T06:05:35Z",
              videoUrl: null,
              note: null,
              error: null
            }
          }
        ],
        final: { status: "none", url: null, error: null }
      }
    }
  };

  var MOCK_PROJECT = {
    ok: true,
    projectId: "prj_9f3c",
    title: "피트레인의 적",
    visibility: "private",
    canEdit: true,
    activePlanId: "pl_20260923_a1b2",
    videos: [],
    characters: { status: "in_progress", items: [] },
    inputs: [],
    prompts: []
  };
})();
