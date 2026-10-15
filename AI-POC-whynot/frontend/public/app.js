"use strict";

/*
  배포 버전. Dockerfile 의 sed 가 치환하고, 치환되지 않으면 빌드가 실패한다.

  **왜 필요한가.** 이 앱은 단일 페이지 앱이라 배포해도 이미 열려 있는 탭은 새로고침할
  때까지 옛 코드를 계속 실행한다. 그래서 버그를 고쳐 배포한 뒤에도 사용자는 같은 오류를
  본다. 그 상태를 구분할 방법이 없으면 "고쳤는데 재현된다"가 되어, 없는 버그를 계속
  찾게 된다. 실제로 그 일이 있었다.

  오류 문구에 이 값을 붙이면 스크린샷 한 장으로 어느 코드가 돌고 있는지 판별된다.
*/
const APP_VERSION = "__ASSET_VERSION__";
const VIDEO_GEN_ENABLED = window.__WHYNOT_RUNTIME__?.videoGen === true;
const STORY_DEV_ENABLED = window.__WHYNOT_RUNTIME__?.storyDev !== false;
// 로그인 유지(운영만 켠다). 토큰은 원래 탭마다 따로인 sessionStorage 에만 있어 새 창마다
// 다시 로그인해야 했다. 켜져 있으면 localStorage 에도 두고, 새 탭이 열릴 때 sessionStorage 로
// 옮겨 준다 — vpoc*.js 는 sessionStorage 만 읽으므로 그쪽은 고치지 않는다.
const PERSIST_LOGIN = window.__WHYNOT_RUNTIME__?.persistLogin === true;
const PERSISTED_TOKEN_KEY = "toon-change-ai-pj:persistedToken";
// 마지막으로 본 화면(설계자 2026-10-13). 사이트를 다시 열면(주소가 / 뿐이면) 이 화면으로 들어간다.
// 작업 프로젝트는 원래 탭마다 따로인 sessionStorage 에만 있어 새 창의 /video 가 목록으로 튕겼다.
const LAST_ROUTE_KEY = "toon-change-ai-pj:lastRoute";

function rememberRoute() {
  try {
    if (!state.user?.username) return;
    localStorage.setItem(LAST_ROUTE_KEY, JSON.stringify({
      user: state.user.username,
      path: window.location.pathname,
      working: getWorkingProject()
    }));
  } catch (error) { /* 저장소 없음 */ }
}

function lastRoute() {
  try {
    const saved = JSON.parse(localStorage.getItem(LAST_ROUTE_KEY) || "null");
    return saved && saved.user === state.user?.username ? saved : null;
  } catch (error) {
    return null;
  }
}
(function restorePersistedLogin() {
  try {
    if (!PERSIST_LOGIN) {
      localStorage.removeItem(PERSISTED_TOKEN_KEY);
      return;
    }
    const live = sessionStorage.getItem("toon-change-ai-pj:token");
    if (live) {
      // 이 탭은 이미 로그인돼 있다. 저장해 둔 것이 없으면 지금 토큰을 남긴다 — 로그인 유지를 켜기
      // 전부터 열려 있던 탭은 로그인 버튼을 다시 누르지 않아 새 창마다 로그인해야 했다(2026-10-08).
      // 만료 시각은 토큰 앞부분(base64url JSON 의 exp)에서 읽는다. 서버가 최종 판단한다.
      if (!localStorage.getItem(PERSISTED_TOKEN_KEY)) {
        const head = live.split(".")[0].replace(/-/g, "+").replace(/_/g, "/");
        const claims = JSON.parse(atob(head + "===".slice((head.length + 3) % 4)));
        if (claims?.exp && Number(claims.exp) * 1000 > Date.now()) {
          localStorage.setItem(PERSISTED_TOKEN_KEY, JSON.stringify({ token: live, expiresAt: Number(claims.exp) }));
        }
      }
      return;
    }
    const saved = JSON.parse(localStorage.getItem(PERSISTED_TOKEN_KEY) || "null");
    // 만료 판단은 서버가 최종이다. 여기서는 이미 지난 토큰을 들고 들어가지 않을 뿐이다.
    if (saved?.token && Number(saved.expiresAt) * 1000 > Date.now()) {
      sessionStorage.setItem("toon-change-ai-pj:token", saved.token);
    } else {
      localStorage.removeItem(PERSISTED_TOKEN_KEY);
    }
  } catch (error) {
    // 저장소가 막힌 브라우저(사생활 보호 모드 등)는 탭마다 로그인하는 원래 동작으로 남는다.
  }
})();
const PROJECT_TRACKS = {
  story: {
    label: "스토리",
    actionLabel: "스토리 개발로 진행",
    enabled: () => STORY_DEV_ENABLED,
    disabledReason: "이 환경에서는 스토리 개발 기능이 꺼져 있습니다."
  },
  video: {
    label: "영상",
    actionLabel: "영상 개발로 진행",
    enabled: () => VIDEO_GEN_ENABLED,
    disabledReason: "이 환경에서는 영상 개발 기능이 꺼져 있습니다."
  }
};

/*
  최근 행동 기록. 오류가 났을 때 "그 직전에 무엇을 했는가"를 알려 준다.
  화면 전환·API 호출만 남기고 사용자가 입력한 내용은 담지 않는다(개인정보·프롬프트 유출 방지).
*/
const breadcrumbs = [];
const BREADCRUMB_LIMIT = 20;

function trail(kind, detail) {
  breadcrumbs.push({ t: new Date().toISOString(), kind, detail: String(detail).slice(0, 120) });
  if (breadcrumbs.length > BREADCRUMB_LIMIT) breadcrumbs.shift();
}

/*
  오류를 서버로 보낸다. 사용자가 콘솔을 열지 않아도 원인을 볼 수 있게 하기 위한 것이다.

  담지 않는 것: 사용자가 입력한 텍스트, 이미지 base64, 토큰. 담는 것은 어디서 왜 터졌는지에
  필요한 최소한이다. `extra` 에는 값이 아니라 **형태**(키 목록, 길이)만 넣는다.
  "무엇이 undefined 였는가"는 키 목록만 있어도 알 수 있다.
*/
function reportError(where, error, extra = {}) {
  const id = Math.random().toString(36).slice(2, 8);
  const report = {
    id,
    appVersion: APP_VERSION,
    where,
    name: error?.name || "Error",
    message: String(error?.message || error || "").slice(0, 500),
    // 상위 몇 프레임만. 전체를 보내면 커지고, 원인은 대개 첫 줄에 있다.
    stack: String(error?.stack || "").split("\n").slice(0, 6).join("\n").slice(0, 1200),
    route: window.location.pathname,
    view: state?.activeView || "",
    ua: navigator.userAgent.slice(0, 200),
    at: new Date().toISOString(),
    breadcrumbs: breadcrumbs.slice(-BREADCRUMB_LIMIT),
    extra
  };
  try {
    const blob = new Blob([JSON.stringify(report)], { type: "application/json" });
    // sendBeacon 은 화면이 닫히는 중에도 전송된다. 실패해도 예외를 던지지 않는다.
    if (!navigator.sendBeacon || !navigator.sendBeacon("/api/client-errors", blob)) {
      fetch("/api/client-errors", { method: "POST", body: blob, keepalive: true }).catch(() => {});
    }
  } catch (_ignored) {
    // 리포트 전송 실패가 원래 오류 처리를 막아서는 안 된다.
  }
  return report;
}

const STORAGE_KEYS = {
  token: "toon-change-ai-pj:token",
  prompt: "toon-change-ai-pj:prompt",
  duration: "toon-change-ai-pj:duration",
  resolution: "toon-change-ai-pj:resolution",
  aspect: "toon-change-ai-pj:aspect",
  model: "toon-change-ai-pj:model",
  // 화면 배치는 사용자마다 다르게 굳는다. 브라우저에만 남긴다(서버로 보내지 않는다).
  navCollapsed: "toon-change-ai-pj:navCollapsed",
  split: "toon-change-ai-pj:split"
};

const state = {
  token: sessionStorage.getItem(STORAGE_KEYS.token) || "",
  user: null,

  previewUrl: "",
  duration: localStorage.getItem(STORAGE_KEYS.duration) || "5",
  resolution: localStorage.getItem(STORAGE_KEYS.resolution) || "720p",
  aspectRatio: localStorage.getItem(STORAGE_KEYS.aspect) || "16:9",
  selectedModelId: localStorage.getItem(STORAGE_KEYS.model) || "",
  models: [],
  // 모델 목록을 못 불러온 이유. 빈 문자열이면 정상적으로 불러온 것이다.
  // "등록된 모델이 없음" 과 "불러오지 못함" 을 화면에서 구분하려고 따로 둔다.
  modelsError: "",
  // 서버가 카탈로그에서 버린 정의 [{id, source, reason}]. 관리자에게만 내려온다.
  modelsRejected: [],
  videos: [],
  projects: { shared: [], private: [] },
  projectTrackFilter: "all",
  projectTrackTarget: null,
  projectTrackSelection: "",
  projectTrackModalOpener: null,
  projectCreateId: "",
  projectModalOpener: null,
  currentProject: null,
  users: [],
  deleteTarget: null,
  // 한도 조정 모달이 다루는 사용자. 저장 후 표의 값을 응답으로 갱신하는 데 쓴다.
  quotaTarget: null,
  // 프롬프트 두 계층. prompts 는 역할별 현황, promptMapping 은 지시서 8-2 표.
  prompts: [],
  promptMapping: [],
  promptMaxChars: 2000,
  pollTimer: null,
  activeView: "home",
  // AI 산출물: 목록은 items, 펼쳐서 본 본문만 content에 캐시한다.
  documents: { projectId: null, items: [], allowedKinds: [], content: {}, expanded: [] },
  docPollTimer: null,
  // 서버가 알려주는 산출물 메타데이터(생성 옵션 선택지). /api/document-kinds 에서 받는다.
  // 선택지를 화면에 하드코딩하지 않는다. 두 곳에 있으면 서버 검증과 조용히 어긋난다.
  docKindMeta: {},
  // 기획자가 고른 생성 옵션. kind별로 따로 둔다(카드가 여러 개 동시에 열린다).
  docOptions: {},
  // 옵션 패널을 펼친 kind 목록
  docOptionsOpen: [],
  // 그림체 · 콘티 이미지
  styles: [],
  stylePurposes: [],
  images: [],
  imagePreset: "",
  videoPreset: "",
  imagePurpose: "",
  imageAspect: "16:9",
  referenceKey: "",
  lastImage: null,
  // 스튜디오가 서버에 이미 있는 이미지를 첫 프레임으로 쓸 때 사용한다.
  studioAssetKey: "",
  // 첫 프레임 모드: none(텍스트만) / upload / ai
  frameMode: "none",
  frameAiKey: "",
  // 콘티 기획표
  sbCutCounts: [],
  sbDetails: [],
  sbModes: [],
  sbShots: [],
  sbMaxCuts: 24,
  sbMode: "",
  sbCutCount: 0,
  sbDetail: "",
  storyboard: null,
  storyboardId: null,
  storyboardTimer: null,
  // 롱테이크
  longOptions: [],
  longConcurrency: 3,
  longTarget: 0,
  sequenceId: null,
  sequenceTimer: null,
  // 진행 중인 롱테이크 목록. 화면과 무관하게 유지한다.
  sequences: [],
  runningTimer: null,
  /*
    참고 이미지 묶음. **두 화면이 상태를 공유하지 않는다.**
    받는 모델이 다르고(Bedrock / 자체 GPU Wan) 분석 결과도 그 모델에 맞춰 나오므로,
    한 화면에서 올린 이미지와 분석이 다른 화면에 새어 들어가면 잘못된 근거로 생성된다.
    지시서 1-2가 두 경로를 섞지 말라고 정한 것도 같은 이유다.
      studio = 새 영상 만들기 (Bedrock/Luma), wan = NEW_영상 제작 (Wan 2.2)
  */
  refSets: {
    studio: { items: [], primaryKey: "", brief: null, answers: {}, busy: false },
    wan: { items: [], primaryKey: "", brief: null, answers: {}, busy: false }
  },
  // NEW_영상 제작 (자체 GPU / Wan 2.2). 기존 스튜디오 상태와 섞지 않는다.
  wanResolutions: [],
  // 비율 안내 문구. 서버가 내려 준다(중복 정의하면 두 곳이 갈린다).
  wanAspectNote: "",
  wanDurations: [],
  wanPresets: [],
  wanEstimateMinutes: {},
  // 위 예상 시간이 몇 초 길이를 기준으로 한 값인지. 서버가 알려 준다.
  wanEstimateRefSeconds: 5,
  /*
    화풍 변환(2단계 파이프라인의 1단계) 상태.

    wanConvert       : 서버가 내려 준 모드 목록과 강도 범위 (/api/v2/video/options 의 convert)
    wanConvertMode   : 고른 모드 id. 기본은 keep(변환 안 함)이라 기존 동작이 그대로 유지된다
    wanConvertStrength: 구도 유지 강도. 모드를 바꾸면 그 모드의 권장값으로 되돌린다
    wanConverted     : 변환 결과 {key, url, ...}. **이 값이 있으면 이것이 첫 프레임이 된다.**
                       원본 대신 이 키를 생성 요청의 primary 로 보낸다
  */
  wanConvert: null,
  wanConvertMode: "keep",
  wanConvertStrength: 0.6,
  wanConverted: null,
  wanResolution: "",
  wanDuration: 0,
  wanPreset: "",
  wanJobs: [],
  wanJobId: null,
  wanTimer: null,
  wanServer: null
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
let toastTimer = null;

/* ------------------------------ helpers ------------------------------ */
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", "\"": "&quot;"
  })[c]);
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 MB";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const v = bytes / (1024 ** i);
  return `${v >= 10 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

function formatDate(value) {
  if (!value) return "-";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "-";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"
  }).format(d);
}

function initials(name) {
  return String(name || "?").slice(0, 2).toUpperCase();
}

/* 파일 이름으로 쓸 수 없는 글자를 없앤다. Windows 는 \ / : * ? " < > | 를 거부하고,
   끝의 점과 공백도 조용히 잘라낸다. 한글은 그대로 둔다 — 제목이 대부분 한글이다. */
function safeFileName(value, fallback = "결과물") {
  const cleaned = String(value ?? "")
    .replace(/[\\/:*?"<>|]/g, " ")
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .replace(/^[.\s]+|[.\s]+$/g, "")
    .slice(0, 80)
    .trim();
  return cleaned || fallback;
}

/* 텍스트를 txt 파일로 내려준다.

   BOM(\uFEFF)을 앞에 붙인다. 붙이지 않으면 Windows 메모장 구버전이 UTF-8 을
   ANSI 로 읽어 한글이 전부 깨진다 — 대본을 받아 보는 쪽이 대개 메모장이다.
   줄바꿈은 CRLF 로 바꾼다. LF 만 있으면 같은 메모장에서 전부 한 줄로 붙는다. */
function downloadTextFile(filename, text) {
  const body = String(text ?? "").replace(/\r\n/g, "\n").replace(/\n/g, "\r\n");
  const blob = new Blob([`\uFEFF${body}`], { type: "text/plain;charset=utf-8" });
  saveBlob(filename.toLowerCase().endsWith(".txt") ? filename : `${filename}.txt`, blob);
}

/* blob 을 파일로 저장한다. txt 와 PDF 가 같은 경로를 쓴다.

   임시 <a download> 를 만들어 누르는 방식이다. 브라우저가 파일 저장을 시작하게 하는
   방법이 이것뿐이다. */
function saveBlob(filename, blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  // 즉시 해제하면 일부 브라우저가 저장을 시작하기 전에 URL 이 사라져 다운로드가 취소된다.
  window.setTimeout(() => URL.revokeObjectURL(url), 10000);
}

/* 서버가 만든 PDF 를 받는다.

   ## 왜 `<a href>` 하나로 끝내지 못하는가

   이 API 는 Bearer 토큰을 헤더로 받는다. 링크와 window.open 은 헤더를 붙일 수 없어서
   401 이 된다. 토큰을 쿼리 문자열로 옮기면 브라우저 히스토리와 ALB 액세스 로그에 토큰이
   남는다 — 세션을 훔칠 수 있는 값을 로그에 뿌리는 것은 대가가 너무 크다.
   그래서 fetch 로 받아 blob 으로 저장한다.

   파일명은 서버가 Content-Disposition 에 담아 준다(한국어 제목이 들어간다).
   그 값을 우선 쓰고, 없으면 화면이 아는 이름으로 대신한다. */
async function downloadPdf(url, fallbackName, button) {
  if (!url) return;
  const label = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "만드는 중...";
  }
  try {
    const headers = new Headers();
    if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
    const response = await fetch(url, { headers });
    trail("pdf", `GET ${url} -> ${response.status}`);

    if (response.status === 401) {
      signOut("세션이 만료되었습니다. 다시 로그인해 주세요.");
      return;
    }
    if (!response.ok) {
      /* 오류 응답은 JSON 이다. 서버가 적은 사유를 그대로 보여 준다 —
         "실패했습니다" 만 띄우면 아직 안 그려진 것인지 권한이 없는 것인지 알 수 없다. */
      let message = `PDF 를 받지 못했습니다. (${response.status})`;
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) {
        try {
          const failure = await response.json();
          if (failure?.error?.message) message = failure.error.message;
        } catch (_error) {
          // 본문이 깨졌으면 위 문구를 쓴다.
        }
      }
      throw new Error(message);
    }

    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    let name = fallbackName;
    if (encoded) {
      try {
        name = decodeURIComponent(encoded[1]);
      } catch (_error) {
        // 서버가 준 이름을 못 읽으면 화면 이름을 쓴다.
      }
    }
    saveBlob(name, blob);
    showToast("PDF로 저장했습니다", name);
  } catch (error) {
    showToast("PDF 를 받지 못했습니다", error.message, "error");
  } finally {
    if (button) {
      button.disabled = false;
      if (label) button.textContent = label;
    }
  }
}

/* 클립보드 복사. clipboard API 는 보안 컨텍스트(HTTPS/localhost)에서만 동작하므로
   실패하면 예전 execCommand 경로로 내려간다. */
async function copyTextToClipboard(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_error) {
    // 아래 대체 경로로 내려간다.
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch (_error) {
    return false;
  }
}

function showToast(title, message, type = "success", reportId = "") {
  const toast = $("#toast");
  $("#toastTitle").textContent = title;
  /*
    오류에는 버전을 붙인다. 한 곳에서 붙여 모든 오류 문구가 함께 갖게 한다.
    호출부마다 적으면 반드시 빠뜨리는 곳이 생기고, 그 문구로 보고가 들어오면 다시 못 쓴다.

    이 값이 없어서 "v41 코드를 실행 중인 탭"과 "새 코드의 새 버그"를 구분하지 못했다.
  */
  $("#toastMessage").textContent = type === "error"
    ? `${message} (v${APP_VERSION}${reportId ? `, id ${reportId}` : ""})`
    : message;
  toast.classList.toggle("error", type === "error");
  toast.classList.add("show");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("show"), 5000);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers });
  // 경로와 상태 코드만 남긴다. 본문은 남기지 않는다(프롬프트·이미지가 들어 있다).
  trail("api", `${options.method || "GET"} ${path} -> ${response.status}`);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (response.status === 401 && !path.startsWith("/api/auth/login") && !path.startsWith("/api/auth/signup")) {
    signOut("세션이 만료되었습니다. 다시 로그인해 주세요.");
    throw new Error("세션이 만료되었습니다.");
  }
  if (!response.ok) {
    const error = new Error(payload?.error?.message || `요청을 처리하지 못했습니다. (${response.status})`);
    // 오류 응답 전체를 실어 둔다. 대부분의 호출부는 error.message만 쓰지만, 일부
    // 엔드포인트(예: scene_video 생성 게이트)는 message 로는 다 담을 수 없는 구조화된
    // 상세(예: 캐릭터별 부족 항목 목록)를 함께 돌려준다. 필요한 호출부만 error.payload를 읽는다.
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

/* ------------------------------ 확인 모달 ------------------------------ */
/* window.confirm 을 대신한다. 브라우저 기본 대화상자는 앱 디자인과 이질적이고,
   모바일에서는 페이지 URL이 함께 노출되며, 되돌릴 수 없는 작업과 단순 확인이
   똑같이 보인다. 여기서는 대상과 결과를 분리해서 보여준다.

   Promise<boolean> 을 돌려준다. 호출부는 await 로 받는다. */
let confirmResolver = null;

function closeConfirm(result) {
  const scrim = $("#confirmModal");
  if (!scrim || scrim.classList.contains("is-hidden")) return;
  scrim.classList.add("is-hidden");
  document.body.classList.remove("modal-open");
  const resolve = confirmResolver;
  confirmResolver = null;
  if (confirmLastFocus && document.contains(confirmLastFocus)) confirmLastFocus.focus();
  if (resolve) resolve(Boolean(result));
}

let confirmLastFocus = null;

function confirmDialog({
  title,
  lead,
  body,
  target = "",
  okLabel = "삭제",
  // 취소 라벨도 받는다. 예/아니오로 묻는 질문에 "취소"가 붙으면 무엇을 취소하는지 애매해진다.
  cancelLabel = "취소",
  tone = "danger"
}) {
  const scrim = $("#confirmModal");
  if (!scrim) return Promise.resolve(window.confirm(`${title}\n${body}`));

  $("#confirmTitle").textContent = title;
  $("#confirmLead").textContent = lead;
  $("#confirmBody").textContent = body;
  $("#confirmTarget").textContent = target;
  $("#confirmTargetRow").classList.toggle("is-hidden", !target);

  const ok = $("#confirmOk");
  ok.textContent = okLabel;
  ok.className = tone === "danger" ? "danger-button" : "primary-button";
  // 매번 지정한다. 기본값으로 되돌리지 않으면 앞선 대화상자의 라벨이 남는다.
  $("#confirmCancel").textContent = cancelLabel;
  scrim.querySelector(".modal").classList.toggle("danger", tone === "danger");
  // 배너를 감추면 안내 문구까지 같이 사라진다. 색만 바꾼다.
  $("#confirmBanner").classList.toggle("warn", tone !== "danger");

  confirmLastFocus = document.activeElement;
  scrim.classList.remove("is-hidden");
  document.body.classList.add("modal-open");
  // 포커스를 모달 안으로 옮긴다. 그러지 않으면 키보드 사용자는 모달이 열린 줄 모른다.
  window.setTimeout(() => ok.focus(), 0);

  return new Promise((resolve) => { confirmResolver = resolve; });
}

/* ------------------------------ 결과물 읽기 뷰어 ------------------------------ */
/* 대본·콘티·로그라인 본문을 보여 주는 읽기 전용 모달.

   전에는 confirmDialog 를 그대로 재사용했다. 두 가지가 잘못됐다.
     1) 그 모달의 본문은 danger-banner 안이라 대본이 붉은 경고문으로 보였다.
     2) 본문을 <p> 한 개의 textContent 로 넣어 씬을 나누던 줄바꿈이 전부 사라졌다.
   여기서는 블록 단위로 그리고(줄바꿈은 CSS pre-wrap 으로 살린다), 같은 내용을
   txt 로 내려받게 한다.

   화면과 파일이 같은 원본을 쓰도록 openReader 가 blocks(화면)와 text(파일)를
   함께 받는다. 둘을 각자 만들면 한쪽만 고쳐져 내려받은 파일이 화면과 달라진다. */
/* pdfUrl/pdfName: PDF는 화면이 조립하지 않는다. 서버가 만든 파일을 그대로 내려받는다
   (한국어 폰트와 S3 컷 그림을 안정적으로 넣을 수 있는 곳이 서버뿐이다).
   pdfUrl 이 비어 있으면 그 결과물은 PDF로 만들 수 없다는 뜻이고, 버튼을 숨긴다. */
const readerState = { text: "", filename: "결과물.txt", pdfUrl: "", pdfName: "결과물.pdf" };
let readerLastFocus = null;

function readerBlocksHtml(blocks) {
  const items = (blocks || []).filter(Boolean);
  if (!items.length) return '<p class="reader-empty">내용이 비어 있습니다.</p>';

  const html = [];
  let sceneOpen = false;
  const closeScene = () => {
    if (sceneOpen) html.push("</section>");
    sceneOpen = false;
  };

  items.forEach((block) => {
    if (block.type === "scene") {
      closeScene();
      html.push('<section class="reader-scene">');
      sceneOpen = true;
      html.push(`<h3 class="reader-slug">${escapeHtml(block.text)}</h3>`);
      return;
    }
    if (block.type === "action") {
      html.push(`<p class="reader-action">${escapeHtml(block.text)}</p>`);
      return;
    }
    if (block.type === "cue") {
      html.push(`<div class="reader-cue">
        <b class="reader-character">${escapeHtml(block.character || "대사")}</b>
        <p class="reader-line">${escapeHtml(block.text)}</p>
      </div>`);
      return;
    }
    if (block.type === "note") {
      html.push(`<p class="reader-note"><b>${escapeHtml(block.label)}</b><span>${escapeHtml(block.text)}</span></p>`);
      return;
    }
    if (block.type === "hook") {
      closeScene();
      html.push(`<p class="reader-hook"><b>${escapeHtml(block.label || "엔딩 훅")}</b>${escapeHtml(block.text)}</p>`);
      return;
    }
  });

  closeScene();
  return html.join("");
}

/* 콘티 기획표. 컷별 그림과 설명이 짝을 이루는 표다.

   ## 왜 블록 목록이 아니라 표인가

   콘티는 회의에 인쇄해서 들고 가는 문서다. 컷을 위아래로 늘어놓으면 한 장에 두 컷밖에
   들어가지 않고, 컷을 서로 비교할 수 없다. 참고 문서(콘티_러프)도 이 표를 인쇄한 것이다.

   ## 왜 클래스를 새로 만들지 않는가

   `.sb-sheet` 계열은 콘티 기획표 화면(#storyboardPanel)이 쓰는 것이고, 인쇄 규칙까지
   이미 갖고 있다. 같은 모양을 두 번 정의하면 한쪽만 고쳐져 같은 콘티가 화면마다 다르게
   나온다(convert-actions 주석과 같은 이유). 컬럼 구성도 그 표와 같아야 한다. */
function readerSheetHtml(sheet) {
  if (!sheet || !(sheet.rows || []).length) {
    return '<p class="reader-empty">컷이 없습니다.</p>';
  }

  const rows = sheet.rows.map((row) => {
    // 음향 칸은 대사 · 음향 · 메모 세 줄을 겹쳐 쓴다. 기획표 화면과 같은 라벨이다.
    const audio = [
      row.dialogue && `<b>대사</b> ${escapeHtml(row.dialogue)}`,
      row.audio && `<b>음향</b> ${escapeHtml(row.audio)}`,
      row.note && `<b>메모</b> ${escapeHtml(row.note)}`
    ].filter(Boolean);

    const frame = row.imageUrl
      ? `<img src="${escapeHtml(row.imageUrl)}" alt="컷 ${row.number} 그림" loading="lazy">`
      : `<span class="sb-frame-note">${escapeHtml(row.placeholder || "그림 없음")}</span>`;

    return `
      <tr class="sb-row ${escapeHtml(row.status || "")}">
        <td class="sb-col-cut">
          <span class="sb-cut-no">${row.number}</span>
          ${row.shotLabel ? `<small>${escapeHtml(row.shotLabel)}</small>` : ""}
        </td>
        <td class="sb-col-video">
          <div class="sb-frame ${escapeHtml(row.imageUrl ? "done" : row.status || "pending")}">${frame}</div>
        </td>
        <td class="sb-col-context">${escapeHtml(row.context || "")}</td>
        <td class="sb-col-audio">${audio.length ? audio.join("<br>") : '<span class="sb-dim">-</span>'}</td>
        <td class="sb-col-time">${row.seconds ? `${row.seconds}s` : "-"}</td>
      </tr>`;
  });

  return `
    ${sheet.meta ? `<p class="reader-sheet-meta">${escapeHtml(sheet.meta)}</p>` : ""}
    <div class="sb-sheet-head">
      <span>Title : <b>${escapeHtml(sheet.title || "-")}</b></span>
      <span>Cuts : <b>${sheet.cutCount || 0}컷</b></span>
      <span>Total : <b>${sheet.totalSeconds ? `${sheet.totalSeconds}초` : "-"}</b></span>
    </div>
    <div class="sb-sheet-wrap">
      <table class="sb-sheet">
        <thead>
          <tr>
            <th class="sb-col-cut">Cut</th>
            <th class="sb-col-video">Video</th>
            <th class="sb-col-context">Context</th>
            <th class="sb-col-audio">Audio</th>
            <th class="sb-col-time">Time</th>
          </tr>
        </thead>
        <tbody>${rows.join("")}</tbody>
      </table>
    </div>`;
}

function openReader({
  title, meta = "", blocks = [], sheet = null, text = "", filename = "결과물",
  pdfUrl = "", pdfName = ""
}) {
  const scrim = $("#readerModal");
  if (!scrim) return;

  readerState.text = text;
  readerState.filename = `${safeFileName(filename)}.txt`;
  readerState.pdfUrl = pdfUrl;
  readerState.pdfName = pdfName || `${safeFileName(filename)}.pdf`;

  $("#readerTitle").textContent = title || "결과물";
  $("#readerMeta").textContent = meta;
  // "공백 제외"를 적는다. 회차 목록에 서버가 센 글자 수가 따로 떠 있어서,
  // 기준을 밝히지 않으면 두 숫자가 어긋난 것으로 보인다.
  const chars = text.replace(/\s/g, "").length;
  $("#readerCount").textContent = chars ? `공백 제외 ${chars.toLocaleString("ko-KR")}자` : "";

  // 콘티는 표라서 폭이 필요하다(.sb-sheet 최소 720px). 대본은 글이라 좁은 편이 읽기 좋다.
  const isSheet = Boolean(sheet && (sheet.rows || []).length);
  scrim.querySelector(".modal").classList.toggle("is-sheet", isSheet);

  /* PDF 버튼은 서버가 그 결과물을 PDF 로 만들 수 있을 때만 보인다. 누를 수 있는 것처럼
     보여 놓고 400 을 돌려주면, 사용자는 자기가 뭘 잘못했는지 알 수 없다. */
  $("#readerPdf").classList.toggle("is-hidden", !pdfUrl);

  const page = $("#readerBody");
  page.innerHTML = isSheet ? readerSheetHtml(sheet) : readerBlocksHtml(blocks);
  page.classList.toggle("is-sheet", isSheet);
  page.scrollTop = 0;

  // 내용이 없으면 내려받을 것도 없다. 빈 파일을 주면 사용자가 원인을 못 찾는다.
  $("#readerDownload").disabled = !text;
  $("#readerCopy").disabled = !text;

  readerLastFocus = document.activeElement;
  scrim.classList.remove("is-hidden");
  document.body.classList.add("modal-open");
  // 인쇄 규칙이 이 클래스로 앱 화면을 숨기고 모달만 남긴다.
  document.body.classList.add("reader-open");
  // 포커스를 본문에 둔다. 이 모달의 목적은 읽기이고, 바로 위아래 키로 넘길 수 있어야 한다.
  window.setTimeout(() => page.focus(), 0);
}

function closeReader() {
  const scrim = $("#readerModal");
  if (!scrim || scrim.classList.contains("is-hidden")) return;
  scrim.classList.add("is-hidden");
  document.body.classList.remove("reader-open");
  // 다른 모달이 위에 겹쳐 있을 수 있다(재생성 확인 등). 남아 있으면 잠금을 유지한다.
  if (!$(".modal-scrim:not(.is-hidden)")) document.body.classList.remove("modal-open");
  if (readerLastFocus && document.contains(readerLastFocus)) readerLastFocus.focus();
}

/* ------------------------------ auth ------------------------------ */
function switchAuthTab(tab) {
  const isLogin = tab === "login";
  $("#tabLogin").classList.toggle("active", isLogin);
  $("#tabSignup").classList.toggle("active", !isLogin);
  $("#loginPane").classList.toggle("is-hidden", !isLogin);
  $("#signupPane").classList.toggle("is-hidden", isLogin);
}

const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

function syncLoginMotion() {
  const video = $("#loginMotionVideo");
  if (!video) return;
  const loginVisible = !$("#loginScreen").classList.contains("is-hidden");
  const shouldPlay = loginVisible && !document.hidden && !reducedMotionQuery.matches;
  if (!shouldPlay) {
    video.pause();
    return;
  }
  const playAttempt = video.play();
  if (playAttempt) playAttempt.catch(() => {});
}

// 인증 화면의 표시 상태, 탭 가시성, OS 축소 모션 설정에 맞춰 디코딩도 멈춘다.
new MutationObserver(syncLoginMotion).observe($("#loginScreen"), {
  attributes: true,
  attributeFilter: ["class"],
});
document.addEventListener("visibilitychange", syncLoginMotion);
if (typeof reducedMotionQuery.addEventListener === "function") {
  reducedMotionQuery.addEventListener("change", syncLoginMotion);
} else {
  reducedMotionQuery.addListener(syncLoginMotion);
}
syncLoginMotion();

function applyUser(user) {
  state.user = user;
  const isAdmin = Boolean(user?.isAdmin);
  $("#adminNavSection").classList.toggle("is-hidden", !isAdmin);
  // 한도가 걸린 계정에만 '내 사용량'을 보여 준다. 한도가 없으면 보여 줄 것이 없다.
  // 서버가 로그인 응답과 /api/auth/me 에 quotas.limited 를 담아 준다.
  $("#navMyUsage")?.classList.toggle("is-hidden", !user?.quotas?.limited);
  $("#userDisplayName").textContent = user?.displayName || user?.username || "-";
  $("#userRoleLabel").textContent = isAdmin ? "관리자" : "일반 사용자";
  $("#userAvatar").textContent = initials(user?.username);
  $("#topAvatar").textContent = initials(user?.username);
}

function signOut(message = "") {
  window.clearTimeout(state.pollTimer);
  state.token = "";
  state.user = null;
  state.currentProject = null;
  sessionStorage.removeItem(STORAGE_KEYS.token);
  sessionStorage.removeItem(WORKING_KEY);
  try { localStorage.removeItem(PERSISTED_TOKEN_KEY); } catch (error) { /* 저장소 없음 */ }
  window.clearTimeout(state.runningTimer);
  // 로그아웃 후 뒤로가기로 앱 화면에 되돌아가지 않도록 현재 항목을 루트로 바꾼다.
  window.history.replaceState({ view: "home" }, "", "/");
  $("#appShell").classList.add("is-hidden");
  $("#loginScreen").classList.remove("is-hidden");
  $("#loginForm").reset();
  $("#loginError").textContent = message;
  switchAuthTab("login");
}

async function enterWorkspace() {
  $("#loginScreen").classList.add("is-hidden");
  $("#appShell").classList.remove("is-hidden");
  // 메뉴 접기 상태와 열 비율을 되살린다. 셸이 보인 뒤에 해야 폭 계산이 맞는다.
  initLayout();
  $("#promptInput").value = localStorage.getItem(STORAGE_KEYS.prompt) || "";
  updatePromptCount();
  syncVideoOptions();

  // 주소 없이(/) 다시 열었으면 마지막으로 본 화면으로, 작업 프로젝트가 없으면 마지막 것으로 되살린다.
  // refreshAll 이 화면을 바꾸며 기록을 덮기 전에 읽어 둔다.
  const saved = lastRoute();
  await refreshAll();

  if (saved) {
    if (!getWorkingProject() && saved.working?.id) setWorkingProject(saved.working);
    if (window.location.pathname === "/" && saved.path && saved.path !== "/") {
      window.history.replaceState(null, "", saved.path);
    }
  }
  // 로그인 전에 열려 있던 주소(딥링크)를 그대로 복원한다.
  const route = parseLocation();
  window.history.replaceState(route, "", pathForView(route.view, route.projectId));
  await applyRoute(route);
  renderWorkingProject();
}

async function handleLogin(event) {
  event.preventDefault();
  $("#loginError").textContent = "";
  const button = $("#loginButton");
  button.disabled = true;
  try {
    const payload = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("#username").value.trim(),
        password: $("#password").value,
        remember: PERSIST_LOGIN
      })
    });
    state.token = payload.token;
    sessionStorage.setItem(STORAGE_KEYS.token, state.token);
    if (PERSIST_LOGIN && payload.expiresAt) {
      try {
        localStorage.setItem(PERSISTED_TOKEN_KEY, JSON.stringify({ token: payload.token, expiresAt: payload.expiresAt }));
      } catch (error) { /* 저장소 없음 — 탭마다 로그인 */ }
    }
    applyUser(payload.user);
    await enterWorkspace();
    showToast("로그인 완료", `${payload.user.displayName}님, 환영합니다.`);
  } catch (error) {
    $("#loginError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function handleSignup(event) {
  event.preventDefault();
  $("#signupError").textContent = "";
  $("#signupSuccess").textContent = "";
  const button = $("#signupButton");
  button.disabled = true;
  try {
    await api("/api/auth/signup", {
      method: "POST",
      body: JSON.stringify({
        username: $("#signupUsername").value.trim(),
        password: $("#signupPassword").value,
        companyCode: $("#signupCompanyCode").value.trim()
      })
    });
    $("#signupSuccess").textContent = "가입이 완료되었습니다. 로그인해 주세요.";
    $("#signupForm").reset();
    showToast("회원가입 완료", "이제 로그인할 수 있습니다.");
  } catch (error) {
    $("#signupError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function validateSession() {
  if (!state.token) return;
  try {
    const payload = await api("/api/auth/me");
    applyUser(payload.user);
    await renewLogin();
    await enterWorkspace();
  } catch (_error) {
    signOut();
  }
}

/* 로그인 유지: 화면을 열 때마다 토큰을 새 만료 시각으로 바꿔 둔다. 실패해도 지금 토큰으로 계속한다. */
async function renewLogin() {
  if (!PERSIST_LOGIN) return;
  try {
    const payload = await api("/api/auth/renew", { method: "POST", body: JSON.stringify({ remember: true }) });
    if (!payload.token) return;
    state.token = payload.token;
    sessionStorage.setItem(STORAGE_KEYS.token, payload.token);
    localStorage.setItem(PERSISTED_TOKEN_KEY, JSON.stringify({ token: payload.token, expiresAt: payload.expiresAt }));
  } catch (error) { /* 옛 서버 · 저장소 없음 — 그대로 둔다 */ }
}

/* --------------------------- 비밀번호 변경 ---------------------------
   관리자와 일반 사용자가 같은 화면을 쓴다. 대상 아이디를 보내지 않는다 —
   서버가 토큰의 주인만 바꾸므로, 화면에도 대상을 고를 여지를 두지 않는다.

   서버는 현재 비밀번호가 틀리면 403 을 준다. 401 이면 위 api() 가 세션 만료로 보고
   로그아웃시켜서, 한 글자 잘못 적은 사용자가 처음부터 다시 해야 한다. */
let passwordLastFocus = null;

function openPasswordModal() {
  const scrim = $("#passwordModal");
  if (!scrim) return;
  $("#passwordForm").reset();
  $("#passwordError").textContent = "";
  $("#passwordSuccess").textContent = "";
  $("#passwordSubmit").disabled = false;
  $("#passwordModalUser").textContent = state.user?.username || "-";
  passwordLastFocus = document.activeElement;
  scrim.classList.remove("is-hidden");
  document.body.classList.add("modal-open");
  // 포커스를 모달 안으로 옮긴다. 그러지 않으면 키보드 사용자는 열린 줄 모른다.
  window.setTimeout(() => $("#currentPassword").focus(), 0);
}

function closePasswordModal() {
  const scrim = $("#passwordModal");
  if (!scrim || scrim.classList.contains("is-hidden")) return;
  scrim.classList.add("is-hidden");
  document.body.classList.remove("modal-open");
  // 입력한 비밀번호를 DOM 에 남기지 않는다. 닫은 모달은 감춰질 뿐 사라지지 않는다.
  $("#passwordForm").reset();
  if (passwordLastFocus && document.contains(passwordLastFocus)) passwordLastFocus.focus();
}

async function handlePasswordChange(event) {
  event.preventDefault();
  const errorBox = $("#passwordError");
  const successBox = $("#passwordSuccess");
  errorBox.textContent = "";
  successBox.textContent = "";

  const currentPassword = $("#currentPassword").value;
  const newPassword = $("#newPassword").value;
  const repeated = $("#newPasswordConfirm").value;

  // 확인란은 오타를 잡기 위한 화면 장치다. 서버로 보내지 않는다 — 서버가 판단할 값이 아니고,
  // 같은 비밀번호를 한 번 더 실어 보낼 이유도 없다.
  if (newPassword !== repeated) {
    errorBox.textContent = "새 비밀번호와 확인 값이 일치하지 않습니다.";
    $("#newPasswordConfirm").focus();
    return;
  }
  if (newPassword === currentPassword) {
    errorBox.textContent = "현재 비밀번호와 다른 값을 입력해 주세요.";
    $("#newPassword").focus();
    return;
  }

  const button = $("#passwordSubmit");
  button.disabled = true;
  try {
    await api("/api/auth/password", {
      method: "POST",
      body: JSON.stringify({ currentPassword, newPassword })
    });
    // 성공하면 닫는다. reset() 이 입력값을 지우므로 모달에 비밀번호가 남지 않는다.
    closePasswordModal();
    showToast("비밀번호 변경 완료", "다음 로그인부터 새 비밀번호를 사용하세요.");
  } catch (requestError) {
    errorBox.textContent = requestError.message;
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------ data load ------------------------------ */
async function refreshAll() {
  // 사용자 목록도 같은 묶음에서 병렬로 부른다.
  // 전에는 다른 로드가 전부 끝난 뒤에 시작해서, 관리자가 사용자 화면으로 바로 들어오면
  // 앞의 다섯 요청이 끝날 때까지 표가 비어 있었다.
  await Promise.all([
    loadProjects(),
    loadDashboard(),
    loadModels(),
    loadVideos(),
    loadStyles(),
    loadLongTakeOptions(),
    loadRunning(),
    state.user?.isAdmin ? loadUsers() : Promise.resolve()
  ]);
  // 진행 중인 것이 있으면 화면과 무관하게 계속 확인한다.
  scheduleRunningPoll();
}

/*
  대시보드 수치와 모델 목록을 **따로** 부른다.

  예전에는 Promise.all 로 묶고 catch 를 하나만 두었다. 그래서 /api/dashboard 가 실패하면
  /api/models 가 성공했어도 state.models 가 채워지지 않았고, "새 영상 만들기"의 생성 모델
  칸이 빈 채로 남았다. 대시보드는 S3 를 훑어 용량을 계산하므로 둘 중 느리고 잘 실패하는
  쪽이다. 관계없는 두 화면이 한 실패를 공유할 이유가 없다.
*/
async function loadDashboard() {
  try {
    const dashboard = await api("/api/dashboard");
    const count = $("#navVideoCount");
    if (count) count.textContent = dashboard.counts?.videos ?? 0;
  } catch (error) {
    showToast("정보를 불러오지 못했습니다", error.message, "error");
  }
}

async function loadModels() {
  try {
    const models = await api("/api/models");
    state.models = models.items || [];
    // 서버가 카탈로그에서 버린 정의. 관리자에게만 온다. 비어 있는 것이 정상이다.
    // 이것을 표에 띄우지 않으면 "등록했는데 목록에 없다" 로만 보인다. 실제로 그렇게
    // 이미지 모델이 전부 사라진 채 표지 생성이 엉뚱한 모델을 부른 일이 있었다.
    state.modelsRejected = models.rejected || [];
    state.modelsError = "";
  } catch (error) {
    // 목록을 비우고 이유를 남긴다. renderModelPicker 가 "등록된 모델이 없음" 과
    // "불러오지 못함" 을 다르게 보여줘야 하기 때문이다.
    state.models = [];
    state.modelsRejected = [];
    state.modelsError = error.message || "모델 목록을 불러오지 못했습니다.";
  }
  renderModelPicker();
  renderModelTable();
}

/* ---- 스켈레톤 ----
   목록을 불러오는 동안 화면이 비어 있으면 "빈 목록"과 "로딩 중"을 구분할 수 없다.
   실제 카드와 같은 모양을 미리 그려 레이아웃이 튀지 않게 한다.
   렌더 함수들이 첫 줄에서 innerHTML 을 비우므로 성공 시에는 저절로 지워진다.
   실패 시에는 catch 에서 지운다. */
// 스켈레톤이 떠 있는 동안 "아직 없습니다" 문구가 같이 보이면 모순이다. 먼저 감춘다.
const SKELETON_EMPTY = {
  "#sharedGrid": "#sharedEmpty",
  "#privateGrid": "#privateEmpty",
  "#videoGrid": "#emptyLibrary",
  "#imageGrid": "#emptyImages"
};

function showSkeleton(selector, count, kind) {
  const box = $(selector);
  if (!box) return;
  const empty = SKELETON_EMPTY[selector];
  if (empty) $(empty)?.classList.add("is-hidden");
  box.classList.remove("is-hidden");
  box.innerHTML = "";

  // 표 본문에는 div 를 넣을 수 없다. tr/td 로 만들어야 행으로 그려진다.
  const isTableBody = box.tagName === "TBODY";
  const columns = isTableBody
    ? box.closest("table")?.querySelectorAll("thead th").length || 1
    : 0;

  for (let i = 0; i < count; i += 1) {
    if (isTableBody) {
      const tr = document.createElement("tr");
      tr.setAttribute("aria-hidden", "true");
      const td = document.createElement("td");
      td.colSpan = columns;
      td.innerHTML = '<span class="skel skel-row"><span class="skel-shimmer"></span></span>';
      tr.appendChild(td);
      box.appendChild(tr);
      continue;
    }
    const el = document.createElement("div");
    el.className = `skel skel-${kind}`;
    el.setAttribute("aria-hidden", "true");
    el.innerHTML = '<span class="skel-shimmer"></span>';
    box.appendChild(el);
  }
}

function clearSkeleton(...selectors) {
  selectors.forEach((s) => {
    const box = $(s);
    if (box && box.querySelector(".skel")) box.innerHTML = "";
  });
}

async function loadProjects() {
  showSkeleton("#sharedGrid", 4, "poster");
  showSkeleton("#privateGrid", 6, "tile");
  try {
    const payload = await api("/api/projects");
    state.projects = { shared: payload.shared || [], private: payload.private || [] };
    renderProjects();
  } catch (error) {
    clearSkeleton("#sharedGrid", "#privateGrid");
    showToast("프로젝트를 불러오지 못했습니다", error.message, "error");
  }
}

async function loadVideos() {
  showSkeleton("#videoGrid", 3, "video");
  try {
    const payload = await api("/api/videos");
    state.videos = payload.items || [];
    renderVideos();
  } catch (error) {
    clearSkeleton("#videoGrid");
    // GET /api/videos 는 옛 영상 기능과 함께 지워졌다(51ba039, 2026-09-23). 그 뒤로 로그인할 때마다
    // 404 알림이 떴다. 없는 API 는 빈 목록으로 두고, 그 밖의 실패만 알린다.
    if (error.status === 404) {
      state.videos = [];
      renderVideos();
      return;
    }
    showToast("영상 목록을 불러오지 못했습니다", error.message, "error");
  }
}

async function loadUsers() {
  showSkeleton("#userTableBody", 5, "row");
  try {
    const [users, code] = await Promise.all([api("/api/admin/users"), api("/api/admin/company-code")]);
    state.users = users.items || [];
    $("#companyCodeInput").value = code.companyCode || "";
    renderUsers();
  } catch (error) {
    clearSkeleton("#userTableBody");
    showToast("사용자 정보를 불러오지 못했습니다", error.message, "error");
  }
}

/* ------------------------------ 진행 중인 작업 ------------------------------ */
/* 롱테이크는 목표 10분이면 예상 335분이 걸린다. 그런데 진행 상태가 state.sequenceId 에만
   있어서, 다른 화면으로 가거나 새로고침하면 진행 중인지조차 알 수 없었다.
   서버는 계속 만들고 있는데 사용자만 모른다.

   GET /api/videos/long-take 는 사용자의 시퀀스 목록을 이미 돌려준다.
   프론트가 한 번도 부르지 않고 있었을 뿐이다. 백엔드 변경 없이 해결한다. */

const SEQ_ACTIVE = ["planning", "rendering", "stitching"];
const SEQ_LABELS = {
  planning: "컷 계획 중",
  rendering: "컷 생성 중",
  stitching: "이어 붙이는 중",
  ready: "완료",
  failed: "실패"
};

async function loadRunning() {
  try {
    const payload = await api("/api/videos/long-take");
    state.sequences = payload.items || [];
  } catch (_error) {
    // 조용히 넘어간다. 이건 보조 정보라 실패해도 본 작업을 막지 않는다.
    state.sequences = [];
  }
  renderRunning();
}

function renderRunning() {
  const active = (state.sequences || []).filter((s) => SEQ_ACTIVE.includes(s.status));

  // 상단바 칩
  const chip = $("#runningChip");
  if (chip) {
    chip.classList.toggle("is-hidden", active.length === 0);
    $("#runningLabel").textContent = active.length === 1
      ? "영상 생성 중"
      : `영상 생성 중 ${active.length}건`;
  }

  // 라이브러리의 진행 목록
  const panel = $("#runningPanel");
  const list = $("#runningList");
  if (!panel || !list) return;
  panel.classList.toggle("is-hidden", active.length === 0);
  list.innerHTML = "";

  active.forEach((seq) => {
    const shots = seq.shots || [];
    const done = shots.filter((s) => s.status === "done").length;
    const failed = shots.filter((s) => s.status === "failed").length;
    const total = shots.length || seq.shotCount || 0;
    const percent = total ? Math.round((done / total) * 100) : 0;

    const item = document.createElement("article");
    item.className = "running-item";
    item.innerHTML = `
      <div class="running-item-head">
        <strong>${escapeHtml(seq.label || seq.title || "롱테이크")}</strong>
        <span class="doc-chip generating">${escapeHtml(SEQ_LABELS[seq.status] || seq.status)}</span>
      </div>
      <div class="job-bar"><i style="width:${percent}%"></i></div>
      <p class="running-meta">컷 ${done}/${total} 완료${failed ? ` · ${failed}컷 실패` : ""} · ${percent}%</p>
      <div class="shot-chips"></div>`;

    // 컷 하나하나의 상태. 어디서 막혔는지 눈으로 짚을 수 있어야 한다.
    const chips = item.querySelector(".shot-chips");
    shots.slice(0, 40).forEach((shot) => {
      const c = document.createElement("span");
      c.className = `shot-chip ${shot.status || "pending"}`;
      c.textContent = shot.number ?? "";
      c.title = shot.error || shot.summary || `컷 ${shot.number}`;
      chips.appendChild(c);
    });
    if (shots.length > 40) {
      const more = document.createElement("span");
      more.className = "shot-chip";
      more.textContent = `+${shots.length - 40}`;
      chips.appendChild(more);
    }

    list.appendChild(item);
  });
}

/* 진행 중인 작업이 있으면 어느 화면에 있든 주기적으로 확인한다.
   전에는 스튜디오 화면에서만, 그것도 이번 세션에 시작한 것만 추적했다. */
function scheduleRunningPoll() {
  window.clearTimeout(state.runningTimer);
  const active = (state.sequences || []).some((s) => SEQ_ACTIVE.includes(s.status));
  if (!active) return;
  state.runningTimer = window.setTimeout(async () => {
    await loadRunning();
    scheduleRunningPoll();
  }, 20000);
}

/* ------------------------------ 오버플로 메뉴 ------------------------------ */
/* 카드마다 액션 버튼을 4개씩 늘어놓으면 프로젝트 10개짜리 화면에 버튼 40개가 깔린다.
   되돌릴 수 없는 삭제가 "새 창 열기"와 같은 무게로 보이는 것도 문제다.
   주 액션 하나만 카드에 남기고 나머지는 이 메뉴로 접는다.

   items: [{ label, onSelect, danger?, element? }]
     element 를 주면 그 노드를 그대로 넣는다(파일 입력을 감싼 label 처럼
     클릭 핸들러로 대체할 수 없는 경우). */
let openMenuEl = null;

function closeOpenMenu() {
  if (!openMenuEl) return;
  const trigger = openMenuEl.querySelector(".menu-trigger");
  openMenuEl.querySelector(".menu-list").hidden = true;
  if (trigger) trigger.setAttribute("aria-expanded", "false");
  openMenuEl = null;
}

document.addEventListener("click", (event) => {
  if (openMenuEl && !openMenuEl.contains(event.target)) closeOpenMenu();
});

function overflowMenu(items, ariaLabel = "추가 작업") {
  const wrap = document.createElement("div");
  wrap.className = "menu";

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "menu-trigger";
  trigger.setAttribute("aria-haspopup", "true");
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-label", ariaLabel);
  trigger.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>';

  const list = document.createElement("div");
  list.className = "menu-list";
  list.setAttribute("role", "menu");
  list.hidden = true;

  items.filter(Boolean).forEach((item) => {
    if (item.element) {
      item.element.classList.add("menu-item");
      item.element.setAttribute("role", "menuitem");
      list.appendChild(item.element);
      return;
    }
    const b = document.createElement("button");
    b.type = "button";
    b.className = `menu-item${item.danger ? " danger" : ""}`;
    b.setAttribute("role", "menuitem");
    b.textContent = item.label;
    b.addEventListener("click", () => {
      closeOpenMenu();
      item.onSelect();
    });
    list.appendChild(b);
  });

  trigger.addEventListener("click", (event) => {
    event.stopPropagation();
    const willOpen = list.hidden;
    closeOpenMenu();
    if (!willOpen) return;
    list.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    openMenuEl = wrap;
    const first = list.querySelector(".menu-item");
    if (first) window.setTimeout(() => first.focus(), 0);
  });

  wrap.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    closeOpenMenu();
    trigger.focus();
  });

  wrap.appendChild(trigger);
  wrap.appendChild(list);
  return wrap;
}

/* ------------------------------ projects ------------------------------ */
// 진입점별 표시 이름과 다음 단계. 서버의 INPUT_TYPES와 값이 일치해야 한다.
const INPUT_TYPES = {
  webtoon: { step: 1, label: "웹툰 원작", next: "영상화 분석 · 각색 방향 · 트리트먼트" },
  logline: { step: 2, label: "소재 · 로그라인", next: "구체화 · 인물 설정 · 트리트먼트" },
  treatment: { step: 3, label: "트리트먼트", next: "씬 분해 · 대본 초안" },
  script: { step: 4, label: "논의된 대본", next: "톤 분석 · 후속화 확장 · 콘티" }
};

const SOURCE_PLACEHOLDERS = {
  webtoon: "웹툰 회차 내용이나 원작 소설 텍스트를 붙여넣으세요. 회차 구분이 있으면 그대로 두는 편이 좋습니다.",
  logline: "한 줄 로그라인 또는 소재 메모를 붙여넣으세요. 짧아도 괜찮습니다.",
  treatment: "보유한 트리트먼트를 붙여넣으세요. 화별 구성이 있으면 함께 넣어주세요.",
  script: "미리 논의된 대본이 있다면 해당 내용을 넣어주세요. 분량은 1화 정도면 충분하고, 인물명과 지문 형식은 그대로 두시면 됩니다."
};

function inputTypeInfo(project) {
  return INPUT_TYPES[project?.inputType] || INPUT_TYPES.webtoon;
}

function projectTrackInfo(track) {
  return PROJECT_TRACKS[track] || {
    label: "미선택",
    actionLabel: "개발 트랙 미선택",
    enabled: () => false,
    disabledReason: "프로젝트 소유자가 개발 트랙을 선택해야 합니다."
  };
}

function configureTrackChoices(containerSelector, { clear = true, editable = true } = {}) {
  const bothDisabled = !STORY_DEV_ENABLED && !VIDEO_GEN_ENABLED;
  $$(`${containerSelector} [data-track]`).forEach((button) => {
    const info = projectTrackInfo(button.dataset.track);
    const enabled = editable && info.enabled();
    button.disabled = !enabled;
    if (clear) button.classList.remove("active");
    button.querySelector(".track-disabled-reason")?.classList.toggle("is-hidden", info.enabled());
  });
  return bothDisabled;
}

function matchesTrackFilter(project) {
  if (state.projectTrackFilter === "all") return true;
  if (state.projectTrackFilter === "unassigned") return !project.track;
  return project.track === state.projectTrackFilter;
}

function newProjectOperationId() {
  return window.crypto?.randomUUID?.() || `project-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function coverMarkup(project, fallbackLabel) {
  if (project.coverUrl) {
    return `<img src="${escapeHtml(project.coverUrl)}" alt="${escapeHtml(project.title)} 표지" loading="lazy">`;
  }
  return `<div class="cover-placeholder"><span>${escapeHtml(fallbackLabel)}</span><strong>${escapeHtml(project.title)}</strong></div>`;
}

function projectCard(project, kind) {
  const card = document.createElement("article");
  card.className = kind === "shared" ? "poster-card" : "tile-card";
  const badge = project.visibility === "shared" ? "공유" : "개인";
  // 진입점 배지는 그 기능을 쓸 때만 붙인다. 영상 전용에서는 값이 항상 webtoon 이라
  // "진입점 1 · 웹툰 원작"이 모든 카드에 똑같이 찍혀 뜻 없는 잡음이 된다.
  const info = FEATURES.legacyInputType ? inputTypeInfo(project) : null;
  const stageBadge = info
    ? `<span class="stage-badge small">진입점 ${info.step} · ${escapeHtml(info.label)}</span>`
    : "";
  const trackInfo = projectTrackInfo(project.track);
  const trackClass = project.track ? `track-${project.track}` : "track-unassigned";
  const trackBadge = `<span class="stage-badge small ${trackClass}">${escapeHtml(trackInfo.label)} 트랙</span>`;
  const videoSummary = project.track === "video"
    ? `<span class="project-video-summary">완성 영상 ${Number(project.completedVideoCount || 0)}편${project.latestVideoCompletedAt ? ` · 최근 ${formatDate(project.latestVideoCompletedAt)}` : ""}</span>`
    : "";
  const summary = project.story
    || (FEATURES.storyFeatures ? "스토리가 아직 없습니다." : "설명이 없습니다.");
  card.innerHTML = `
    <div class="cover">${coverMarkup(project, badge)}
      <span class="visibility-badge ${escapeHtml(project.visibility)}">${badge}</span>
      <span class="cover-open">열기</span>
    </div>
    <div class="card-body">
      <strong title="${escapeHtml(project.title)}">${escapeHtml(project.title)}</strong>
      ${trackBadge}
      ${stageBadge}
      ${videoSummary}
      <small>${escapeHtml(project.owner || "소유자 없음")} · ${formatDate(project.updatedAt)}</small>
      <p>${escapeHtml(summary.slice(0, 90))}</p>
      <div class="card-actions"></div>
    </div>`;

  // 카드 본문을 누르면 상세로 진입한다. 액션 버튼 클릭은 전파를 막아 구분한다.
  card.classList.add("clickable");
  card.setAttribute("role", "button");
  card.tabIndex = 0;
  card.addEventListener("click", () => openProject(project.id));
  card.addEventListener("keydown", (event) => {
    if (event.target !== card) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openProject(project.id);
    }
  });

  const actions = card.querySelector(".card-actions");
  actions.addEventListener("click", (event) => event.stopPropagation());
  const isOwner = project.owner === state.user?.username || state.user?.isAdmin;

  if (isOwner) {
    // 주 액션 하나만 카드에 남긴다. 표지 만들기가 이 카드에서 가장 자주 하는 일이다.
    const aiButton = document.createElement("button");
    aiButton.type = "button";
    aiButton.className = "primary";
    aiButton.textContent = "AI 표지";
    aiButton.addEventListener("click", () => generateCover(project, aiButton));
    actions.appendChild(aiButton);

    // 파일 입력은 클릭 핸들러로 대체할 수 없다. label 노드를 그대로 메뉴에 넣는다.
    const uploadLabel = document.createElement("label");
    uploadLabel.className = "upload-cover";
    uploadLabel.textContent = "표지 직접 등록";
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp";
    // hidden 을 쓰면 포커스를 받지 못해 키보드로 표지를 등록할 수 없다.
    // 화면에서만 감추고 포커스는 남긴다. 라벨이 :focus-within 으로 링을 그린다.
    input.className = "sr-only-focusable";
    input.addEventListener("change", () => uploadCover(project, input.files[0]));
    uploadLabel.appendChild(input);

    actions.appendChild(overflowMenu([
      { element: uploadLabel },
      {
        label: project.visibility === "shared" ? "개인 프로젝트로 전환" : "공유 프로젝트로 전환",
        onSelect: () => toggleVisibility(project)
      },
      { label: "프로젝트 삭제", danger: true, onSelect: () => deleteProject(project) }
    ], `${project.title} 추가 작업`));
  }
  return card;
}

function renderProjects() {
  const sharedGrid = $("#sharedGrid");
  const privateGrid = $("#privateGrid");
  sharedGrid.innerHTML = "";
  privateGrid.innerHTML = "";

  const shared = state.projects.shared.filter(matchesTrackFilter);
  const privateProjects = state.projects.private.filter(matchesTrackFilter);
  shared.forEach((project) => sharedGrid.appendChild(projectCard(project, "shared")));
  privateProjects.forEach((project) => privateGrid.appendChild(projectCard(project, "private")));

  $("#sharedCount").textContent = shared.length;
  $("#privateCount").textContent = privateProjects.length;
  $("#sharedEmpty").classList.toggle("is-hidden", shared.length > 0);
  $("#privateEmpty").classList.toggle("is-hidden", privateProjects.length > 0);
  const filtered = state.projectTrackFilter !== "all";
  $("#sharedEmpty strong").textContent = filtered ? "조건에 맞는 공유 프로젝트가 없습니다" : "공유 프로젝트가 없습니다";
  $("#privateEmpty strong").textContent = filtered ? "조건에 맞는 개인 프로젝트가 없습니다" : "개인 프로젝트가 없습니다";
}

function syncProjectCreateState() {
  const button = $("#projectSubmit");
  if (!button) return;
  const selected = $("#projectTrackOptions [data-track].active")?.dataset.track || "";
  const info = projectTrackInfo(selected);
  const configured = STORY_DEV_ENABLED || VIDEO_GEN_ENABLED;
  button.disabled = !configured || !selected || !info.enabled() || !$("#projectTitle").value.trim();
}

async function createProject(event) {
  event.preventDefault();
  $("#projectError").textContent = "";
  const button = $("#projectSubmit");
  const visibility = $("#visibilityOptions .active")?.dataset.value || "private";
  const inputType = $("#inputTypeOptions .active")?.dataset.value || "webtoon";
  const track = $("#projectTrackOptions [data-track].active")?.dataset.track || "";
  if (!STORY_DEV_ENABLED && !VIDEO_GEN_ENABLED) {
    $("#projectError").textContent = "스토리 개발과 영상 개발이 모두 꺼져 있습니다. 관리자 설정을 확인해 주세요.";
    syncProjectCreateState();
    return;
  }
  if (!track || !projectTrackInfo(track).enabled()) {
    $("#projectError").textContent = track
      ? projectTrackInfo(track).disabledReason
      : "스토리 개발 또는 영상 개발을 선택해 주세요.";
    syncProjectCreateState();
    return;
  }
  const title = $("#projectTitle").value.trim();
  if (!state.projectCreateId) state.projectCreateId = newProjectOperationId();
  button.disabled = true;
  try {
    const payload = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({
        title,
        workTitle: title,
        story: $("#projectStory").value.trim(),
        visibility,
        inputType,
        track,
        idempotencyKey: state.projectCreateId
      })
    });
    const created = payload.item;
    closeProjectModal();
    state.projectCreateId = "";
    // 생성 직후에는 고른 트랙의 개발 페이지로 바로 간다 (FR-9.3).
    await dispatchProject(created, { entry: "create" });
    loadProjects();
    showToast("프로젝트 생성", `${projectTrackInfo(created.track).label} 트랙으로 시작합니다.`);
  } catch (error) {
    $("#projectError").textContent = error.message;
  } finally {
    syncProjectCreateState();
  }
}

async function generateCover(project, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "생성 중...";
  try {
    await api(`/api/projects/${encodeURIComponent(project.id)}/cover/generate`, { method: "POST" });
    await loadProjects();
    showToast("AI 표지 생성", "스토리를 바탕으로 표지를 만들었습니다.");
  } catch (error) {
    showToast("표지를 만들지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function uploadCover(project, file) {
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    await api(`/api/projects/${encodeURIComponent(project.id)}/cover`, { method: "POST", body });
    await loadProjects();
    showToast("표지 등록", "이미지를 표지로 설정했습니다.");
  } catch (error) {
    showToast("표지를 등록하지 못했습니다", error.message, "error");
  }
}

async function toggleVisibility(project) {
  const next = project.visibility === "shared" ? "private" : "shared";
  try {
    await api(`/api/projects/${encodeURIComponent(project.id)}`, {
      method: "PATCH", body: JSON.stringify({ visibility: next })
    });
    await loadProjects();
    showToast("공개 범위 변경", next === "shared" ? "공유 프로젝트로 전환했습니다." : "개인 프로젝트로 전환했습니다.");
  } catch (error) {
    showToast("변경하지 못했습니다", error.message, "error");
  }
}

async function deleteProject(project) {
  if (!await confirmDialog({
    title: "프로젝트 삭제",
    lead: "이 작업은 되돌릴 수 없습니다.",
    body: "프로젝트와 표지 이미지가 함께 삭제됩니다. 생성한 영상과 콘티는 라이브러리에 남습니다.",
    target: project.title,
    okLabel: "프로젝트 삭제"
  })) return;
  try {
    await api(`/api/projects/${encodeURIComponent(project.id)}`, { method: "DELETE" });
    await loadProjects();
    showToast("프로젝트 삭제", "프로젝트를 삭제했습니다.");
  } catch (error) {
    showToast("삭제하지 못했습니다", error.message, "error");
  }
}

/* ------------------------------ project detail ------------------------------ */
function canEditProject(project) {
  return Boolean(project) && (project.owner === state.user?.username || state.user?.isAdmin);
}

function canSelectProjectTrack(project) {
  return Boolean(project) && project.owner === state.user?.username;
}

function closeProjectTrackModal({ toList = false } = {}) {
  const modal = $("#projectTrackModal");
  if (!modal || modal.classList.contains("is-hidden")) return;
  modal.classList.add("is-hidden");
  document.body.classList.toggle("modal-open", Boolean($(".modal-scrim:not(.is-hidden)")));
  const opener = state.projectTrackModalOpener;
  state.projectTrackModalOpener = null;
  state.projectTrackTarget = null;
  state.projectTrackSelection = "";
  if (toList) {
    navigate("home", { replace: true });
  }
  if (opener && document.contains(opener)) opener.focus();
}

function openProjectTrackModal(project) {
  state.projectTrackModalOpener = document.activeElement;
  state.currentProject = project;
  state.projectTrackTarget = project;
  state.projectTrackSelection = "";
  const editable = canSelectProjectTrack(project);
  $("#projectTrackProjectTitle").textContent = project.title || project.id;
  $("#projectTrackSuggestion").textContent = project.trackSuggestionReason
    || (project.suggestedTrack
      ? `${projectTrackInfo(project.suggestedTrack).label} 개발을 추천합니다. 직접 선택해야 저장됩니다.`
      : "추천할 기존 작업 근거가 없습니다. 직접 선택해 주세요.");
  $("#projectTrackError").textContent = "";
  $("#projectTrackOwnerNote").classList.toggle("is-hidden", editable);
  configureTrackChoices("#projectTrackChoices", { clear: true, editable });
  $("#projectTrackConfirm").disabled = true;
  $("#projectTrackModal").classList.remove("is-hidden");
  document.body.classList.add("modal-open");
  const firstChoice = $("#projectTrackChoices [data-track]:not(:disabled)");
  (firstChoice || $("#projectTrackCancel"))?.focus();
}

async function confirmProjectTrack() {
  const project = state.projectTrackTarget;
  const track = state.projectTrackSelection;
  if (!project || !track || !canSelectProjectTrack(project)) return;
  const info = projectTrackInfo(track);
  if (!info.enabled()) {
    $("#projectTrackError").textContent = info.disabledReason;
    return;
  }
  const button = $("#projectTrackConfirm");
  button.disabled = true;
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(project.id)}/track`, {
      method: "PUT",
      body: JSON.stringify({ track })
    });
    const replace = window.location.pathname.startsWith("/projects/");
    closeProjectTrackModal();
    await dispatchProject(payload.item, { replace });
    loadProjects();
  } catch (error) {
    $("#projectTrackError").textContent = error.message;
    button.disabled = false;
  }
}

/* entry="create" 는 방금 만든 프로젝트다 — prd2 FR-9.3 대로 영상 트랙이면 단계 0 으로 바로 간다.
   entry="open" (기본값) 은 목록 카드·딥링크·트랙 선택 완료다 — prd2 FR-9.3 후단과 FR-10.1 대로
   영상 트랙이면 상세 화면(영상 프로젝트 폴더)을 연다. 스토리 트랙 흐름은 양쪽 모두 그대로다. */
async function dispatchProject(project, { replace = false, entry = "open" } = {}) {
  if (!project?.track) {
    openProjectTrackModal(project);
    return;
  }
  const info = projectTrackInfo(project.track);
  if (!info.enabled()) {
    showToast("이 트랙을 열 수 없습니다", info.disabledReason, "error");
    navigate("home", { replace: true });
    return;
  }
  if (project.track === "video") {
    state.currentProject = project;
    setWorkingProject(project);
    if (entry === "create") {
      navigate("videoGen", { replace });
      return;
    }
    resetDocuments(project.id);
    renderProjectDetail(project);
    navigate("projectDetail", { projectId: project.id, replace });
    return;
  }
  state.currentProject = project;
  resetDocuments(project.id);
  renderProjectDetail(project);
  navigate("projectDetail", { projectId: project.id, replace });
  loadDocuments(project.id);
}

async function openProject(projectId) {
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(projectId)}`);
    await dispatchProject(payload.item);
  } catch (error) {
    showToast("프로젝트를 열지 못했습니다", error.message, "error");
  }
}

/* 프로젝트 진행률.
   원본 자료 -> 기획 방향 -> AI 산출물 -> 제작. 전에는 긴 스크롤을 끝까지 훑어야
   어디까지 왔는지 알 수 있었다. 각 단계의 완료 판정은 실제 데이터로 한다. */
function renderProgress(project) {
  const track = $("#progressTrack");
  if (!track || !project) return;

  const source = project.source || {};
  const direction = project.direction || {};
  const docs = state.documents.items || [];
  const readyDocs = docs.filter((d) => d.status === "ready").length;
  const totalDocs = (state.documents.allowedKinds || []).length;

  const done = {
    source: Boolean(source.charCount),
    // 기획 방향은 하나라도 적었으면 시작한 것으로 본다. 전부 채우도록 강요하지 않는다.
    direction: Boolean(
      direction.targetPlatform || direction.audience || direction.tone
      || direction.freeDirection || (direction.genres || []).length
    ),
    documents: totalDocs > 0 && readyDocs === totalDocs,
    handoff: false
  };
  const partial = {
    documents: readyDocs > 0 && readyDocs < totalDocs
  };

  $("#progressDocCount").textContent = `${readyDocs}/${totalDocs || 5}`;

  track.querySelectorAll(".progress-step").forEach((el) => {
    const step = el.dataset.step;
    el.classList.toggle("done", Boolean(done[step]));
    el.classList.toggle("partial", Boolean(partial[step]));
  });

  // 제작 단계는 "지금 할 수 있는 것"으로 표시한다. 산출물이 하나라도 있으면 넘어갈 수 있다.
  const handoffStep = track.querySelector('[data-step="handoff"]');
  if (handoffStep) handoffStep.classList.toggle("next", readyDocs > 0);
}

function applyProjectTrackView(project) {
  const storyTrack = project?.track === "story";
  const videoTrack = project?.track === "video";
  ["#storyPanel", "#outputsPanel", "#progressTrack"].forEach((selector) => {
    $(selector)?.classList.toggle("is-hidden", !storyTrack || !STORY_DEV_ENABLED);
  });
  // "이전 방식의 작업 구획"은 2026-10-08 설계자 지시로 감춘다(지우면 안의 요소를 읽는 코드가 깨진다).
  $("#legacySection")?.classList.add("is-hidden");
  // 결과물 만들기의 "영상" 카드 — 영상 기능이 켜진 배포에서만.
  $("#makeVideo")?.classList.toggle("is-hidden", !storyTrack || !VIDEO_GEN_ENABLED);
  $("#projectFolderPanel")?.classList.toggle("is-hidden", !videoTrack);
  // 이전 방식 구획은 지우지 않고 접이식으로 남긴다. 감싼 details 도 트랙에 맞춰 감춘다.
  $("#projectFolderLegacySection")?.classList.toggle("is-hidden", !videoTrack);
  // Spec 10 (prd2 FR-10.1): video 트랙 상세 화면이 영상 프로젝트 폴더다. story 트랙에는 없다.
  $("#videoFolder")?.classList.toggle("is-hidden", !videoTrack || !VIDEO_GEN_ENABLED);
  ["#handoffShort", "#handoffStoryboard", "#handoffLong"].forEach((selector) => {
    $(selector)?.classList.toggle("is-hidden", !storyTrack);
  });
  $("#handoffSceneVideo")?.classList.toggle("is-hidden", !videoTrack || !VIDEO_GEN_ENABLED);
}

/* 영상 프로젝트 상세의 "영상" 칸 — 이 프로젝트의 영상 계획 목록(설계자 2026-10-08).
   채우던 video-folder.js 가 옛 영상 기능과 함께 지워져(51ba039) 칸이 비어 있었다.
   GET /api/vpoc/p/:id 의 plans 를 카드로 보이고, 카드에서 그 계획을 바로 연다. */
const VIDEO_MODEL_LABEL = { "luma-ray2": "Luma Ray2", "seedance-2-0": "Seedance 2.0", "seedance-2-5": "Seedance 2.5" };
const VIDEO_SHOT_STATUS = { planned: "계획", queued: "대기", submitted: "생성 중", completed: "완료", failed: "실패" };

async function loadVideoFolderPlans(project) {
  const list = $("#videoFolderList");
  if (!list) return;
  $("#videoFolderTitle").textContent = project.title || "프로젝트";
  list.innerHTML = '<p class="field-hint">영상 계획을 불러오는 중...</p>';
  let view;
  try {
    view = await api(`/api/vpoc/p/${encodeURIComponent(project.id)}`);
  } catch (error) {
    list.innerHTML = "";
    $("#videoFolderError").textContent = `영상 계획을 불러오지 못했습니다: ${error.message}`;
    return;
  }
  if (state.currentProject?.id !== project.id) return; // 그 사이 다른 프로젝트로 옮겼다
  $("#videoFolderError").textContent = "";
  const plans = view.plans || [];
  const videoCount = plans.reduce((sum, plan) => sum + (plan.videos || []).length, 0);
  $("#videoFolderCountBadge").textContent = `계획 ${plans.length} · 영상 ${videoCount}`;
  $("#videoFolderEmpty").classList.toggle("is-hidden", plans.length > 0);
  $("#videoFolderEmpty").textContent = "아직 영상 계획이 없습니다. 위의 \"영상 개발 시작\"으로 시작하세요.";
  list.innerHTML = "";
  plans.forEach((plan) => {
    const card = document.createElement("div");
    card.className = "folder-item video-plan-card";
    card.style.cssText = "display:block;padding:12px;border:1px solid var(--line, #333);border-radius:10px;margin-bottom:10px";
    const meta = [
      plan.stepName,
      `샷 ${(plan.shots || []).length}개`,
      plan.totalSeconds ? `${plan.totalSeconds}초` : "",
      VIDEO_MODEL_LABEL[plan.videoModel] || plan.videoModel,
      plan.oneTake ? "한 번에 이어서" : "",
      formatDate(plan.createdAt)
    ].filter(Boolean).join(" · ");
    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:center">
        <strong>${escapeHtml(plan.title)}</strong>
        <button class="outline-button" type="button" data-open-plan="${escapeHtml(plan.planId)}">이 계획 열기</button>
      </div>
      <p class="field-hint">${escapeHtml(meta)}</p>
      <ol class="video-plan-shots" style="margin:6px 0 0 18px;padding:0">
        ${(plan.shots || []).map((shot) => `<li>${escapeHtml(shot.summaryKo || shot.id)} <small class="field-hint">· ${shot.durationS}초${VIDEO_SHOT_STATUS[shot.status] ? ` · ${VIDEO_SHOT_STATUS[shot.status]}` : ""}</small></li>`).join("")}
      </ol>`;
    (plan.videos || []).forEach((item) => {
      const label = document.createElement("p");
      label.className = "field-hint";
      label.textContent = item.label;
      const tag = document.createElement("video");
      tag.src = item.videoUrl;
      tag.controls = true;
      tag.setAttribute("playsinline", "");
      tag.style.cssText = "width:100%;max-width:520px;border-radius:8px;margin-top:6px";
      card.appendChild(label);
      card.appendChild(tag);
    });
    card.querySelector("[data-open-plan]").addEventListener("click", () => {
      window.whynotOpenVideoGen(project, plan.planId);
    });
    list.appendChild(card);
  });
}

function renderProjectDetail(project) {
  const editable = canEditProject(project);
  const isShared = project.visibility === "shared";

  const badge = $("#detailVisibilityBadge");
  badge.textContent = isShared ? "공유" : "개인";
  badge.className = `visibility-badge ${isShared ? "shared" : "private"}`;

  $("#detailMeta").textContent =
    `소유자 ${project.owner || "없음"} · 생성 ${formatDate(project.createdAt)} · 수정 ${formatDate(project.updatedAt)}`;

  $("#detailTitle").value = project.title || "";
  $("#detailStory").value = project.story || "";
  $("#detailTitle").readOnly = !editable;
  $("#detailStory").readOnly = !editable;

  const frame = $("#detailCoverFrame");
  frame.className = `detail-cover-frame ${isShared ? "poster" : "tile"}`;
  frame.innerHTML = project.coverUrl
    ? `<img src="${escapeHtml(project.coverUrl)}" alt="${escapeHtml(project.title)} 표지">`
    : `<div class="cover-placeholder"><span>${isShared ? "공유" : "개인"}</span><strong>${escapeHtml(project.title)}</strong></div>`;

  $("#detailCoverActions").classList.toggle("is-hidden", !editable);
  $("#detailCoverHint").classList.toggle("is-hidden", !editable);
  $("#detailOwnerButtons").classList.toggle("is-hidden", !editable);
  $("#detailReadonlyNote").classList.toggle("is-hidden", editable);
  // 제작으로 넘기는 것은 읽기 권한만 있어도 가능하다. 프로젝트를 바꾸지 않는다.
  // Overlay 는 이 두 줄을 지웠지만 handoffPanel 을 유지하므로 함께 되살린다(PRD H2).
  $("#handoffPanel")?.classList.remove("is-hidden"); // 패널은 2026-10-08 에 지웠다
  $("#detailVisibilityToggle").textContent = isShared ? "개인 프로젝트로 전환" : "공유 프로젝트로 전환";
  $("#detailSaveHint").textContent = "";

  renderCoverHistory(project, editable);
  applyProjectTrackView(project);
  if (project.track === "video" && VIDEO_GEN_ENABLED) loadVideoFolderPlans(project);
  renderSourcePanel(project, editable);
  renderDirectionPanel(project, editable);
  renderDocumentsPanel();
  renderProgress(project);
  if (project.track === "story") {
    // 기존 스토리 타입 선택과 생성 흐름을 그대로 사용한다.
    renderStoryPanel(project, editable);
  } else if (project.track === "video") {
    renderProjectFolder(project);
    // vpoc1: 영상 프로젝트의 결과 조회는 영상 화면(vpoc.js)의 '프로젝트 보기' 구역이 맡는다.
  }
}

/* ---- 영상 폴더 (캐릭터 정보 + 영상 컷) ----

   와이낫미디어용. 프로젝트를 스토리 작업 공간이 아니라 폴더로 본다.

   읽기 전용이다. 새로 만드는 것은 '새로운 영상 만들기' 화면에서 하고 여기서는
   그 화면에 무엇이 쌓였는지만 보여준다. 그래서 편집 UI 를 넣지 않았다 —
   같은 데이터를 두 곳에서 고칠 수 있게 만들면 어느 쪽이 정본인지 흐려진다.

   두 목록 모두 실패해도 화면을 죽이지 않는다. 와이낫 개발계에는 Neptune 인스턴스가
   아직 없어서 두 요청이 500 으로 떨어질 수 있다. scene.js 와 같은 방식으로
   console.warn 만 남기고 자리에 사유를 적는다. */
const SCENE_STATUS_TEXT = {
  draft: "입력 대기",
  analyzed: "분석 완료",
  planned: "샷 계획 완료",
  generating: "생성 중",
  assembling: "조립 중",
  done: "완료",
  needs_review: "검수 필요",
  failed: "실패"
};

const MODEL_STATUS_TEXT = {
  none: "모델 없음",
  needs_data: "자료 부족",
  generating: "생성 중",
  ready: "준비됨",
  failed: "생성 실패"
};

function folderNote(text) {
  const p = document.createElement("p");
  p.className = "field-hint tight";
  p.textContent = text;
  return p;
}

async function renderProjectFolder(project) {
  const panel = $("#projectFolderPanel");
  // video track detail에서만 이 읽기 전용 폴더를 요청한다.
  if (!panel || panel.classList.contains("is-hidden") || !project) return;
  // 접이식이 닫혀 있으면 요청하지 않는다. 기본 닫힘이라 상세를 열 때마다
  // /api/characters + /api/scenes 두 콜이 낭비되는 것을 막는다. 펼치면 toggle 배선이 다시 부른다.
  if ($("#projectFolderLegacySection")?.open === false) return;

  const characterList = $("#folderCharacterList");
  const sceneList = $("#folderSceneList");
  const badge = $("#folderCountBadge");
  characterList.innerHTML = "";
  sceneList.innerHTML = "";
  characterList.appendChild(folderNote("불러오는 중..."));
  sceneList.appendChild(folderNote("불러오는 중..."));

  const query = `?projectId=${encodeURIComponent(project.id)}`;
  // 둘을 함께 보내고 각각 따로 성공/실패를 판정한다. 하나가 실패해도 다른 쪽은 보여준다.
  const [characters, scenes] = await Promise.all([
    api(`/api/characters${query}`).then((p) => p.items || []).catch((error) => {
      console.warn("캐릭터 목록을 불러오지 못했습니다", error);
      return null;
    }),
    api(`/api/scenes${query}`).then((p) => p.items || []).catch((error) => {
      console.warn("영상 목록을 불러오지 못했습니다", error);
      return null;
    })
  ]);

  // 응답이 늦게 온 사이에 사용자가 다른 프로젝트를 열었으면 버린다.
  if (state.currentProject?.id !== project.id) return;

  characterList.innerHTML = "";
  sceneList.innerHTML = "";

  if (characters === null) {
    characterList.appendChild(folderNote("캐릭터 정보를 불러오지 못했습니다."));
  } else if (!characters.length) {
    characterList.appendChild(folderNote("등록된 캐릭터가 없습니다. 캐릭터가 없어도 영상은 만들 수 있습니다."));
  } else {
    characters.forEach((character) => {
      const row = document.createElement("article");
      row.className = "folder-item";
      const meta = [
        character.role,
        MODEL_STATUS_TEXT[character.modelStatus] || character.modelStatus,
        (character.refImageKeys || []).length ? `참조 이미지 ${character.refImageKeys.length}장` : "",
        formatDate(character.updatedAt)
      ].filter(Boolean).join(" · ");
      row.innerHTML = `
        <strong>${escapeHtml(character.name || "이름 없음")}</strong>
        <small>${escapeHtml(meta)}</small>`;
      characterList.appendChild(row);
    });
  }

  if (scenes === null) {
    sceneList.appendChild(folderNote("영상 목록을 불러오지 못했습니다."));
  } else if (!scenes.length) {
    sceneList.appendChild(folderNote("아직 만든 영상이 없습니다."));
  } else {
    scenes.forEach((scene) => {
      const row = document.createElement("article");
      row.className = `folder-item status-${escapeHtml(scene.status)}`;
      const meta = [
        scene.style === "anime" ? "웹툰 애니메이션" : "실사화",
        // 어떤 컷을 몇 개 만들었는지가 이 목록의 핵심 정보다.
        `컷 ${scene.shotsDone ?? 0}/${scene.shotCount ?? 0}`,
        formatDate(scene.createdAt)
      ].filter(Boolean).join(" · ");
      const chip = scene.status === "done" ? "ready" : scene.status === "failed" ? "failed" : "generating";
      row.innerHTML = `
        <div class="folder-item-head">
          <span class="doc-chip ${chip}">${escapeHtml(SCENE_STATUS_TEXT[scene.status] || scene.status)}</span>
          <strong>${escapeHtml(scene.title || "제목 없음")}</strong>
        </div>
        <small>${escapeHtml(meta)}</small>`;
      sceneList.appendChild(row);
    });
  }

  const cutTotal = (scenes || []).reduce((sum, scene) => sum + (scene.shotsDone || 0), 0);
  badge.textContent =
    `캐릭터 ${characters === null ? "?" : characters.length} · 영상 ${scenes === null ? "?" : scenes.length}`
    + (cutTotal ? ` · 컷 ${cutTotal}` : "");
}

/* ---- 원본 자료 ---- */
function renderSourcePanel(project, editable) {
  const info = inputTypeInfo(project);
  const source = project.source || {};
  const textarea = $("#sourceText");

  $("#detailInputTypeBadge").textContent = `진입점 ${info.step} · ${info.label}`;
  $("#sourceHint").textContent = `AI가 읽을 원본입니다. 다음 단계: ${info.next}`;
  textarea.placeholder = SOURCE_PLACEHOLDERS[project.inputType] || SOURCE_PLACEHOLDERS.webtoon;
  textarea.readOnly = !editable;

  $$("#detailInputTypeOptions button").forEach((button) => {
    button.classList.toggle("active", button.dataset.value === (project.inputType || "webtoon"));
    button.disabled = !editable;
  });
  $("#sourceButtons").classList.toggle("is-hidden", !editable);

  if (source.charCount > 0) {
    // textContent에 넣으므로 이스케이프하지 않는다. 이스케이프하면 엔티티가 그대로 보인다.
    $("#sourceStatus").textContent =
      `저장됨 · ${source.name || "원본 자료"} · ${source.charCount.toLocaleString()}자`;
  } else {
    $("#sourceStatus").textContent = "저장된 자료가 없습니다.";
  }

  // 본문은 목록 응답에 없다. 상세를 열 때 한 번만 따로 받아온다.
  textarea.value = "";
  if (source.key) {
    textarea.placeholder = "원본 자료를 불러오는 중...";
    api(`/api/projects/${encodeURIComponent(project.id)}/source`)
      .then((payload) => {
        if (state.currentProject?.id !== project.id) return;
        textarea.value = payload.text || "";
        textarea.placeholder = SOURCE_PLACEHOLDERS[project.inputType] || SOURCE_PLACEHOLDERS.webtoon;
      })
      .catch((error) => {
        textarea.placeholder = SOURCE_PLACEHOLDERS[project.inputType] || SOURCE_PLACEHOLDERS.webtoon;
        showToast("원본 자료를 불러오지 못했습니다", error.message, "error");
      });
  }
}

async function saveSource() {
  const project = state.currentProject;
  if (!project) return;
  const text = $("#sourceText").value.trim();
  if (text.length < 20) {
    showToast("자료가 너무 짧습니다", "원본 자료는 20자 이상이어야 합니다.", "error");
    return;
  }
  const button = $("#sourceSave");
  button.disabled = true;
  button.textContent = "저장 중...";
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(project.id)}/source`, {
      method: "POST",
      body: JSON.stringify({ text, name: project.source?.name || "붙여넣은 자료" })
    });
    state.currentProject = payload.item;
    renderSourcePanel(payload.item, true);
    // 자료가 생기면 AI 작업 버튼이 열린다.
    renderDocumentsPanel();
    showToast("자료 저장", `${payload.charCount.toLocaleString()}자를 저장했습니다.`);
  } catch (error) {
    showToast("저장하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "자료 저장";
  }
}

function loadSourceFile(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    $("#sourceText").value = String(reader.result || "").trim();
    $("#sourceStatus").textContent = `${file.name} 불러옴 · 저장 버튼을 눌러 반영하세요.`;
  };
  reader.onerror = () => showToast("파일을 읽지 못했습니다", "텍스트 파일인지 확인해 주세요.", "error");
  reader.readAsText(file, "utf-8");
}

async function changeInputType(inputType) {
  const project = state.currentProject;
  if (!project || project.inputType === inputType) return;
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(project.id)}`, {
      method: "PATCH",
      body: JSON.stringify({ inputType })
    });
    state.currentProject = payload.item;
    renderProjectDetail(payload.item);
    // 진입점이 바뀌면 만들 수 있는 산출물 목록도 달라진다.
    await loadDocuments(payload.item.id);
    await loadProjects();
    showToast("진입점 변경", `${inputTypeInfo(payload.item).label} 기준으로 바꿨습니다.`);
  } catch (error) {
    showToast("변경하지 못했습니다", error.message, "error");
  }
}

/* ---- AI 산출물 ---- */
// 서버 documents.KIND_SPECS와 같은 순서/라벨을 쓴다.
const DOC_KINDS = {
  analysis: { label: "영상화 가능성 분석", goal: "원작이 드라마로 갈 수 있는지, 어떻게 가야 하는지" },
  adaptation: { label: "각색 방향 제안", goal: "서로 다른 각색 선택지와 추천안" },
  logline: { label: "로그라인 구체화", goal: "짧은 소재를 기획서 수준으로" },
  characters: { label: "인물 설정", goal: "성격, 말투, 배경, 관계" },
  treatment: { label: "트리트먼트", goal: "시즌 구조와 화별 시놉시스" }
};

// 진행 순서. 트리트먼트는 앞 단계 결과를 참고하므로 마지막에 둔다.
const DOC_ORDER = ["analysis", "adaptation", "logline", "characters", "treatment"];

const DOC_STATUS_LABELS = {
  generating: "생성 중",
  ready: "완료",
  failed: "실패"
};

function resetDocuments(projectId) {
  window.clearTimeout(state.docPollTimer);
  state.docPollTimer = null;
  // 다른 프로젝트로 옮기면 앞 프로젝트의 생성 배너를 이어받지 않는다.
  // 그러지 않으면 앞 프로젝트의 산출물 이름으로 "완성했습니다"가 뜬다.
  if (runState.projectDetail) runState.projectDetail.kinds = [];
  // loaded는 목록을 받기 전 "산출물 없음" 문구가 잠깐 보이는 것을 막기 위한 것이다.
  state.documents = { projectId, items: [], allowedKinds: [], content: {}, expanded: [], loaded: false };
}

/** 산출물 메타데이터(생성 옵션 선택지). 한 번만 받아 두면 되므로 캐시한다. */
async function loadDocumentKinds() {
  if (Object.keys(state.docKindMeta).length) return;
  try {
    const payload = await api("/api/document-kinds");
    const meta = {};
    (payload.items || []).forEach((item) => { meta[item.kind] = item; });
    state.docKindMeta = meta;
    // 서버 기본값으로 초기 선택을 채운다. 사용자가 이미 고른 값은 건드리지 않는다.
    Object.entries(meta).forEach(([kind, item]) => {
      const options = item.options;
      if (!options) return;
      const current = state.docOptions[kind] || {};
      state.docOptions[kind] = {
        pages: current.pages || options.defaultPages || 4,
        detail: current.detail || options.defaultDetail || "normal",
        details: current.details || ""
      };
    });
  } catch (error) {
    // 옵션을 못 받아도 생성은 가능하다(서버가 기본값을 쓴다). 화면을 막지 않는다.
    console.warn("산출물 옵션을 불러오지 못했습니다", error);
  }
}

async function loadDocuments(projectId) {
  try {
    await loadDocumentKinds();
    const payload = await api(`/api/projects/${encodeURIComponent(projectId)}/documents`);
    if (state.currentProject?.id !== projectId) return;
    state.documents.projectId = projectId;
    state.documents.items = payload.items || [];
    state.documents.allowedKinds = payload.allowedKinds || [];
    state.documents.loaded = true;
    renderDocumentsPanel();
    scheduleDocumentPoll();
  } catch (error) {
    showToast("AI 작업 목록을 불러오지 못했습니다", error.message, "error");
  }
}

/* 산출물 생성은 여러 종류가 동시에 돌 수 있다. 상단 배너는 하나뿐이라
   지금 돌고 있는 것들을 묶어서 보여주고, 전부 끝나면 결과를 알린다.
   이 화면은 입력이 별도 열이 아니라 본문 안에 섞여 있어 접지 않는다(RUN_VIEWS.collapse=false). */
function syncDocumentRun() {
  const items = state.documents.items || [];
  const generating = items.filter((item) => item.status === "generating");
  const names = (list) => list.map((kind) => DOC_KINDS[kind]?.label || kind).join(", ");

  if (generating.length) {
    const kinds = generating.map((item) => item.kind);
    const note = `${names(kinds)}을 만들고 있습니다. 화면을 벗어나도 계속 진행됩니다.`;
    if (!runState.projectDetail?.active) {
      startRun("projectDetail", note);
      runState.projectDetail.kinds = kinds;
    } else {
      kinds.forEach((kind) => {
        if (!runState.projectDetail.kinds.includes(kind)) runState.projectDetail.kinds.push(kind);
      });
      // 서버가 진행률을 주지 않는다. 경과 시간에 기대어 천천히 올린다(92%에서 멈춘다).
      const seconds = (Date.now() - runState.projectDetail.startedAt) / 1000;
      updateRun("projectDetail", Math.min(92, 8 + seconds * 0.7), note);
    }
    return;
  }

  const watched = runState.projectDetail?.kinds || [];
  if (!watched.length) return;
  runState.projectDetail.kinds = [];
  const failed = watched.filter((kind) => documentByKind(kind)?.status === "failed");
  if (failed.length) endRun("projectDetail", false, `${names(failed)}을 만들지 못했습니다.`);
  else endRun("projectDetail", true, `${names(watched)}을 완성했습니다.`);
}

function scheduleDocumentPoll() {
  window.clearTimeout(state.docPollTimer);
  state.docPollTimer = null;
  syncDocumentRun();
  const generating = state.documents.items.some((item) => item.status === "generating");
  if (!generating) return;
  // 생성은 1~2분 걸린다. 완료되면 폴링이 스스로 멈춘다.
  state.docPollTimer = window.setTimeout(() => {
    const projectId = state.documents.projectId;
    // 다른 프로젝트나 다른 화면으로 옮겼으면 폴링을 멈춘다.
    if (!projectId || state.currentProject?.id !== projectId) return;
    if (state.activeView !== "projectDetail") return;
    loadDocuments(projectId);
  }, 4000);
}

function documentByKind(kind) {
  return state.documents.items.find((item) => item.kind === kind) || null;
}

async function generateDocument(kind) {
  const project = state.currentProject;
  if (!project) return;
  // 옵션을 받는 산출물만 본문을 실어 보낸다. 나머지는 예전처럼 빈 POST다.
  const supportsOptions = Boolean(state.docKindMeta[kind]?.options);
  const options = supportsOptions ? (state.docOptions[kind] || {}) : null;
  try {
    const payload = await api(
      `/api/projects/${encodeURIComponent(project.id)}/documents/${encodeURIComponent(kind)}/generate`,
      options
        ? { method: "POST", body: JSON.stringify({ options }) }
        : { method: "POST" }
    );
    const others = state.documents.items.filter((item) => item.kind !== kind);
    state.documents.items = [payload.item, ...others];
    delete state.documents.content[kind];
    renderDocumentsPanel();
    scheduleDocumentPoll();
    // 트리트먼트는 분량에 따라 모델을 여러 번 부른다. 1~2분이라고 안내하면 사용자가
    // 멈춘 줄 알고 다시 누른다(그러면 409가 뜬다).
    const label = DOC_KINDS[kind]?.label || kind;
    const wait = supportsOptions
      ? `${docGenerateMinutes(kind)}분쯤 걸립니다. 화면을 벗어나도 계속 진행됩니다.`
      : "1~2분 걸립니다.";
    showToast("생성 시작", `${label}을 만들고 있습니다. ${wait}`);
  } catch (error) {
    showToast("생성을 시작하지 못했습니다", error.message, "error");
  }
}

async function toggleDocument(kind) {
  const expanded = state.documents.expanded;
  const index = expanded.indexOf(kind);
  if (index >= 0) {
    expanded.splice(index, 1);
    renderDocumentsPanel();
    return;
  }
  expanded.push(kind);
  if (!state.documents.content[kind]) {
    const project = state.currentProject;
    try {
      const payload = await api(
        `/api/projects/${encodeURIComponent(project.id)}/documents/${encodeURIComponent(kind)}`
      );
      state.documents.content[kind] = payload.content || {};
    } catch (error) {
      showToast("산출물을 불러오지 못했습니다", error.message, "error");
      const at = state.documents.expanded.indexOf(kind);
      if (at >= 0) state.documents.expanded.splice(at, 1);
    }
  }
  renderDocumentsPanel();
}

/* 산출물 간 의존. app/src/documents.py 의 contextKinds 와 같아야 한다.
   확인 결과 앞 단계를 참고하는 것은 트리트먼트 하나뿐이고 나머지 넷은 서로 독립이다.
   앞 단계가 없어도 생성은 되지만 품질이 떨어진다(서버는 있으면 맥락으로 넣고 없으면 건너뛴다). */
const DOC_CONTEXT = {
  treatment: ["analysis", "adaptation", "logline", "characters"]
};

function documentContextState(kind) {
  const needs = DOC_CONTEXT[kind];
  if (!needs) return null;
  const ready = needs.filter((k) => documentByKind(k)?.status === "ready");
  return { needs, ready, missing: needs.filter((k) => !ready.includes(k)) };
}

/* ---- 생성 옵션 (지금은 트리트먼트만) ---- */

/** 예상 소요 시간(분). 설계 1회 + 본문 3회 + 화별 시놉시스 N회를 순차로 부른다. */
function docGenerateMinutes(kind) {
  const options = state.docKindMeta[kind]?.options;
  if (!options) return 2;
  const pages = state.docOptions[kind]?.pages || options.defaultPages || 4;
  // 본문은 분량에 비례하고 나머지 단계는 거의 고정이다. 페이지당 40초 + 기본 90초.
  return Math.max(2, Math.round((90 + pages * 40) / 60));
}

/** 옵션 패널. 콘티 옵션(renderStoryboardOptions)과 같은 방식으로 세그먼트를 JS가 채운다. */
function documentOptionsBlock(kind) {
  const meta = state.docKindMeta[kind]?.options;
  if (!meta) return null;
  const picked = state.docOptions[kind] || {};
  const isOpen = state.docOptionsOpen.includes(kind);

  const wrap = window.document.createElement("div");
  wrap.className = "doc-options";

  const toggle = window.document.createElement("button");
  toggle.type = "button";
  toggle.className = "text-action doc-options-toggle";
  const detailLabel = (meta.details || []).find((d) => d.id === picked.detail)?.label || "";
  toggle.textContent = isOpen
    ? "옵션 접기"
    : `옵션 · A4 ${picked.pages || meta.defaultPages}장${detailLabel ? ` · ${detailLabel}` : ""}`;
  toggle.addEventListener("click", () => {
    const at = state.docOptionsOpen.indexOf(kind);
    if (at >= 0) state.docOptionsOpen.splice(at, 1);
    else state.docOptionsOpen.push(kind);
    renderDocumentsPanel();
  });
  wrap.appendChild(toggle);

  if (!isOpen) return wrap;

  const body = window.document.createElement("div");
  body.className = "doc-options-body";
  body.innerHTML = `
    <div class="options-row">
      <fieldset>
        <legend>${escapeHtml(meta.labels?.pages?.label || "A4 분량")}</legend>
        <div class="segmented" data-role="pages"></div>
      </fieldset>
      <fieldset>
        <legend>${escapeHtml(meta.labels?.detail?.label || "상세도")}</legend>
        <div class="segmented" data-role="detail"></div>
      </fieldset>
    </div>
    <p class="field-hint tight" data-role="hint"></p>
    <label class="field-label" for="docDetails-${escapeHtml(kind)}">
      ${escapeHtml(meta.labels?.details?.label || "세부사항")}
      <small class="optional">선택</small>
    </label>
    <textarea class="panel-textarea" id="docDetails-${escapeHtml(kind)}" rows="4"
      maxlength="${Number(meta.maxDetailsChars) || 2000}"
      placeholder="꼭 반영할 내용을 적어주세요. 예: 주인공의 직업을 기자로, 배경은 1990년대 부산으로. 결말은 열린 결말로."></textarea>
    <p class="field-hint tight" data-role="estimate"></p>`;

  const pagesBox = body.querySelector('[data-role="pages"]');
  (meta.pageChoices || []).forEach((choice) => {
    const button = window.document.createElement("button");
    button.type = "button";
    button.dataset.value = String(choice.value);
    button.className = Number(picked.pages) === Number(choice.value) ? "active" : "";
    button.textContent = choice.label;
    button.title = `본문 약 ${Number(choice.estimateChars).toLocaleString("ko-KR")}자`;
    button.addEventListener("click", () => {
      state.docOptions[kind] = { ...state.docOptions[kind], pages: choice.value };
      renderDocumentsPanel();
    });
    pagesBox.appendChild(button);
  });

  const detailBox = body.querySelector('[data-role="detail"]');
  (meta.details || []).forEach((option) => {
    const button = window.document.createElement("button");
    button.type = "button";
    button.dataset.value = option.id;
    button.className = picked.detail === option.id ? "active" : "";
    button.textContent = option.label;
    button.title = option.hint || "";
    button.addEventListener("click", () => {
      state.docOptions[kind] = { ...state.docOptions[kind], detail: option.id };
      renderDocumentsPanel();
    });
    detailBox.appendChild(button);
  });

  const hint = (meta.details || []).find((option) => option.id === picked.detail)?.hint || "";
  body.querySelector('[data-role="hint"]').textContent = hint;

  const textarea = body.querySelector("textarea");
  textarea.value = picked.details || "";
  // 입력마다 다시 그리면 커서가 튄다. state만 갱신하고 렌더는 하지 않는다.
  textarea.addEventListener("input", () => {
    state.docOptions[kind] = { ...state.docOptions[kind], details: textarea.value };
  });

  const sections = (meta.sections || []).map((s) => s.label).join(" · ");
  const chars = (Number(picked.pages) || 0) * (Number(meta.charsPerPage) || 1100);
  body.querySelector('[data-role="estimate"]').textContent =
    `${sections} 순서로 본문 약 ${chars.toLocaleString("ko-KR")}자를 씁니다. `
    + `약 ${docGenerateMinutes(kind)}분 걸립니다.`;

  wrap.appendChild(body);
  return wrap;
}

function renderDocumentsPanel() {
  const project = state.currentProject;
  const container = $("#documentSteps");
  if (!project || !container) return;
  // 산출물이 늘어나면 진행률도 같이 움직여야 한다.
  window.setTimeout(() => renderProgress(project), 0);

  const editable = canEditProject(project);
  const allowed = DOC_ORDER.filter((kind) => state.documents.allowedKinds.includes(kind));
  $("#documentsEmpty").classList.toggle("is-hidden", allowed.length > 0 || !state.documents.loaded);
  const readyCount = allowed.filter((k) => documentByKind(k)?.status === "ready").length;
  $("#documentsHint").textContent = !(project.source || {}).charCount
    ? "먼저 위에서 원본 자료를 저장하면 생성할 수 있습니다."
    : readyCount === 0
      ? "어느 것부터 만들어도 됩니다. 트리트먼트만 앞 단계 결과를 참고하므로 마지막에 만드는 편이 좋습니다."
      : `${readyCount}개 완료. 앞 단계가 많을수록 트리트먼트 품질이 올라갑니다.`;

  container.innerHTML = "";
  allowed.forEach((kind, index) => {
    container.appendChild(documentStepCard(kind, index + 1, editable, project));
  });
}

function documentStepCard(kind, order, editable, project) {
  const meta = DOC_KINDS[kind] || { label: kind, goal: "" };
  const document_ = documentByKind(kind);
  const status = document_?.status || "none";
  const isOpen = state.documents.expanded.includes(kind);
  const hasSource = Boolean((project.source || {}).charCount);

  const card = window.document.createElement("article");
  card.className = `doc-step status-${status}`;

  const statusChip = status === "none"
    ? '<span class="doc-chip">아직 없음</span>'
    : `<span class="doc-chip ${escapeHtml(status)}">${escapeHtml(DOC_STATUS_LABELS[status] || status)}</span>`;

  const metaLine = [];
  if (document_?.title && status === "ready") metaLine.push(escapeHtml(document_.title));
  if (document_?.updatedAt && status !== "none") metaLine.push(formatDate(document_.updatedAt));
  if (document_?.version > 1) metaLine.push(`${document_.version}차 생성`);

  // 트리트먼트만 앞 단계 결과를 맥락으로 받는다(documents.py 의 contextKinds).
  // 없어도 생성은 되지만 품질이 떨어지므로, 무엇이 빠졌는지 이름으로 알려준다.
  const ctx = documentContextState(kind);
  const contextNote = ctx
    ? `<p class="doc-step-context${ctx.missing.length ? "" : " ok"}">`
      + (ctx.missing.length
          ? `앞 단계 ${ctx.ready.length}/${ctx.needs.length} 완료 · `
            + `${ctx.missing.map((k) => escapeHtml(DOC_KINDS[k]?.label || k)).join(", ")}을(를) 먼저 만들면 품질이 올라갑니다`
          : `앞 단계 ${ctx.needs.length}개가 모두 준비되었습니다`)
      + "</p>"
    : "";


  card.innerHTML = `
    <div class="doc-step-head">
      <span class="doc-order">${order}</span>
      <div class="doc-step-title">
        <strong>${escapeHtml(meta.label)}</strong>
        <small>${escapeHtml(meta.goal)}</small>
      </div>
      ${statusChip}
    </div>
    ${metaLine.length ? `<p class="doc-step-meta">${metaLine.join(" · ")}</p>` : ""}
    ${contextNote}
    ${status === "failed" ? `<p class="doc-step-error">${escapeHtml(document_.error || "생성에 실패했습니다.")}</p>` : ""}
    <div class="doc-step-actions"></div>
    <div class="doc-body"></div>`;

  // 생성 옵션은 버튼 위에 둔다. 누르기 전에 무엇으로 만들지 정하는 순서가 자연스럽다.
  if (editable && status !== "generating") {
    const optionsBlock = documentOptionsBlock(kind);
    if (optionsBlock) card.querySelector(".doc-step-actions").before(optionsBlock);
  }

  const actions = card.querySelector(".doc-step-actions");
  if (status === "ready") {
    const toggle = window.document.createElement("button");
    toggle.type = "button";
    toggle.className = "text-action";
    toggle.textContent = isOpen ? "접기" : "결과 보기";
    toggle.addEventListener("click", () => toggleDocument(kind));
    actions.appendChild(toggle);
  }
  if (editable && status !== "generating") {
    const generate = window.document.createElement("button");
    generate.type = "button";
    generate.className = status === "ready" ? "text-action" : "primary-button";
    generate.textContent = status === "none" ? "생성하기" : "다시 생성";
    generate.disabled = !hasSource;
    if (!hasSource) generate.title = "원본 자료를 먼저 저장해 주세요.";
    generate.addEventListener("click", () => generateDocument(kind));
    actions.appendChild(generate);
  }
  if (status === "generating") {
    const spinner = window.document.createElement("span");
    spinner.className = "doc-progress";
    spinner.textContent = "AI가 작성하는 중입니다. 화면을 벗어나도 계속 진행됩니다.";
    actions.appendChild(spinner);
  }

  if (isOpen && state.documents.content[kind]) {
    card.querySelector(".doc-body").innerHTML = renderDocumentContent(kind, state.documents.content[kind]);
  }
  return card;
}

/* ---- 산출물 본문 렌더러 ---- */
function docList(items) {
  const values = (items || []).filter((item) => String(item ?? "").trim());
  if (!values.length) return "";
  return `<ul class="doc-list">${values.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function docPairs(items, titleKey = "title", detailKey = "detail", extraKey = null, extraLabel = "") {
  const rows = (items || []).filter((item) => item && (item[titleKey] || item[detailKey]));
  if (!rows.length) return "";
  return `<div class="doc-pairs">${rows.map((row) => `
    <div class="doc-pair">
      <strong>${escapeHtml(row[titleKey] || "")}</strong>
      <p>${escapeHtml(row[detailKey] || "")}</p>
      ${extraKey && row[extraKey] ? `<p class="doc-pair-extra"><b>${escapeHtml(extraLabel)}</b> ${escapeHtml(row[extraKey])}</p>` : ""}
    </div>`).join("")}</div>`;
}

function docSection(title, inner) {
  if (!inner) return "";
  return `<section class="doc-section"><h3>${escapeHtml(title)}</h3>${inner}</section>`;
}

function docText(value) {
  const text = String(value ?? "").trim();
  return text ? `<p class="doc-text">${escapeHtml(text)}</p>` : "";
}

function docRaw(content) {
  const json = JSON.stringify(content, (key, value) => (key === "_meta" ? undefined : value), 2);
  return `<details class="doc-raw"><summary>원본 JSON 보기</summary><pre>${escapeHtml(json)}</pre></details>`;
}

function renderDocumentContent(kind, content) {
  const renderers = {
    analysis: renderAnalysisDoc,
    adaptation: renderAdaptationDoc,
    logline: renderLoglineDoc,
    characters: renderCharactersDoc,
    treatment: renderTreatmentDoc
  };
  const renderer = renderers[kind];
  const body = renderer ? renderer(content) : "";
  return `<div class="doc-content">${body}${docRaw(content)}</div>`;
}

function renderAnalysisDoc(c) {
  const score = Number.isFinite(Number(c.score)) ? Number(c.score) : null;
  const format = c.formatRecommendation || {};
  return [
    `<div class="doc-verdict">
       <span class="doc-verdict-badge">${escapeHtml(c.verdict || "판단")}</span>
       ${score !== null ? `<span class="doc-score"><b>${score}</b><small>/100</small></span>` : ""}
     </div>`,
    docText(c.summary),
    docSection("강점", docPairs(c.strengths)),
    docSection("위험 요소와 해결 방향", docPairs(c.risks, "title", "detail", "mitigation", "해결")),
    docSection("추천 형식", [
      format.format ? `<p class="doc-text"><b>${escapeHtml(format.format)}</b>${format.runtime ? ` · ${escapeHtml(format.runtime)}` : ""}</p>` : "",
      docText(format.platform),
      docText(format.reason)
    ].join("")),
    docSection("제작 고려사항", docPairs(c.productionNotes)),
    docSection("비교 작품", docPairs(c.comparableWorks, "title", "why"))
  ].join("");
}

function renderAdaptationDoc(c) {
  const options = (c.options || []).filter((option) => option && option.name);
  const recommended = String(c.recommended || "").trim();
  const cards = options.map((option) => {
    const isPick = recommended && option.name === recommended;
    return `<div class="doc-option ${isPick ? "recommended" : ""}">
      <div class="doc-option-head">
        <strong>${escapeHtml(option.name)}</strong>
        ${isPick ? '<span class="doc-chip ready">추천</span>' : ""}
      </div>
      ${option.logline ? `<p class="doc-option-logline">${escapeHtml(option.logline)}</p>` : ""}
      ${docText(option.approach)}
      <div class="doc-option-grid">
        ${option.keeps?.length ? `<div><h4>살릴 것</h4>${docList(option.keeps)}</div>` : ""}
        ${option.changes?.length ? `<div><h4>바꿀 것</h4>${docList(option.changes)}</div>` : ""}
        ${option.pros?.length ? `<div><h4>장점</h4>${docList(option.pros)}</div>` : ""}
        ${option.cons?.length ? `<div><h4>감수할 것</h4>${docList(option.cons)}</div>` : ""}
      </div>
      ${option.fit ? `<p class="doc-pair-extra"><b>적합성</b> ${escapeHtml(option.fit)}</p>` : ""}
    </div>`;
  }).join("");

  return [
    docText(c.summary),
    docSection("각색 방향 선택지", cards),
    docSection("추천 이유", docText(c.recommendReason)),
    docSection("톤앤매너", docText(c.toneAndManner)),
    docSection("반드시 살릴 장면", docPairs(c.keyScenes, "title", "why"))
  ].join("");
}

function renderLoglineDoc(c) {
  const world = c.worldSetting || {};
  const conflict = c.conflict || {};
  return [
    c.logline ? `<blockquote class="doc-logline">${escapeHtml(c.logline)}</blockquote>` : "",
    docSection("프리미스", docText(c.premise)),
    docSection("주제", docText(c.theme)),
    c.genreMix?.length ? docSection("장르", `<div class="doc-chips">${c.genreMix.map((genre) => `<span class="doc-chip">${escapeHtml(genre)}</span>`).join("")}</div>`) : "",
    docSection("세계관", [
      world.time ? `<p class="doc-text"><b>시간</b> ${escapeHtml(world.time)}</p>` : "",
      world.place ? `<p class="doc-text"><b>공간</b> ${escapeHtml(world.place)}</p>` : "",
      docList(world.rules)
    ].join("")),
    docSection("갈등", [
      conflict.external ? `<p class="doc-text"><b>외적</b> ${escapeHtml(conflict.external)}</p>` : "",
      conflict.internal ? `<p class="doc-text"><b>내적</b> ${escapeHtml(conflict.internal)}</p>` : "",
      conflict.antagonistForce ? `<p class="doc-text"><b>대립 축</b> ${escapeHtml(conflict.antagonistForce)}</p>` : ""
    ].join("")),
    docSection("훅", docPairs(c.hooks)),
    docSection("시즌 흐름", docText(c.seasonArc)),
    docSection("결정이 필요한 것", docList(c.openQuestions))
  ].join("");
}

function renderTreatmentDoc(c) {
  const format = c.format || {};
  const structure = c.structure || {};
  const formatChips = [
    format.episodeCount ? `${format.episodeCount}부작` : "",
    format.runtime || "",
    format.platform || ""
  ].filter(Boolean);

  const episodes = (c.episodes || []).filter((ep) => ep && (ep.synopsis || ep.title));
  const episodeCards = episodes.map((ep) => `
    <div class="doc-episode">
      <div class="doc-episode-head">
        <span class="doc-episode-no">${escapeHtml(String(ep.number ?? "?"))}화</span>
        <strong>${escapeHtml(ep.title || "")}</strong>
        ${ep.characterFocus?.length ? `<small>${ep.characterFocus.map(escapeHtml).join(" · ")}</small>` : ""}
      </div>
      ${docText(ep.synopsis)}
      ${ep.keyScenes?.length ? `<div class="doc-episode-scenes"><h4>핵심 장면</h4>${docList(ep.keyScenes)}</div>` : ""}
      ${ep.hook ? `<p class="doc-episode-hook"><b>다음 화로</b> ${escapeHtml(ep.hook)}</p>` : ""}
    </div>`).join("");

  const characters = (c.mainCharacters || []).filter((person) => person && person.name).map((person) => `
    <div class="doc-pair">
      <strong>${escapeHtml(person.name)}${person.role ? ` · ${escapeHtml(person.role)}` : ""}</strong>
      <p>${escapeHtml(person.summary || "")}</p>
      ${person.arc ? `<p class="doc-pair-extra"><b>변화</b> ${escapeHtml(person.arc)}</p>` : ""}
    </div>`).join("");

  // 트리트먼트 본문. 섹션 순서와 라벨은 서버가 narrativeSections 로 내려준다.
  // 프론트에 순서를 하드코딩하면 서버에서 섹션을 늘렸을 때 조용히 빠진다.
  const narrative = c.narrative || {};
  const beats = c.narrativeBeats || {};
  const sectionList = Array.isArray(c.narrativeSections) && c.narrativeSections.length
    ? c.narrativeSections
    : Object.keys(narrative).map((id) => ({ id, label: id }));
  const bodyBlocks = sectionList
    .filter((section) => String(narrative[section.id] || "").trim())
    .map((section) => {
      // 모델이 문단을 빈 줄로 나눠 준다. 그대로 <p>로 쪼개야 읽을 수 있다.
      const paragraphs = String(narrative[section.id])
        .split(/\n{2,}/)
        .map((chunk) => chunk.trim())
        .filter(Boolean);
      const list = Array.isArray(beats[section.id]) ? beats[section.id] : [];
      return `<section class="doc-narrative">
        <h3>${escapeHtml(section.label)}</h3>
        ${paragraphs.map((p) => `<p class="doc-text">${escapeHtml(p)}</p>`).join("")}
        ${list.length ? `<div class="doc-episode-scenes"><h4>핵심 장면</h4>${docList(list)}</div>` : ""}
      </section>`;
    })
    .join("");

  const estimate = c.pageEstimate || {};
  const pageNote = estimate.estimatedPages
    ? `<p class="field-hint tight">본문 ${Number(estimate.bodyChars || 0).toLocaleString("ko-KR")}자 · `
      + `A4 약 ${estimate.estimatedPages}장`
      + (estimate.requestedPages ? ` (요청 ${estimate.requestedPages}장)` : "")
      + "</p>"
    : "";

  return [
    c.incompleteNote ? `<p class="doc-step-error">${escapeHtml(c.incompleteNote)}</p>` : "",
    c.logline ? `<blockquote class="doc-logline">${escapeHtml(c.logline)}</blockquote>` : "",
    formatChips.length ? `<div class="doc-chips">${formatChips.map((chip) => `<span class="doc-chip">${escapeHtml(chip)}</span>`).join("")}</div>` : "",
    pageNote,
    bodyBlocks ? `<div class="doc-narrative-body">${bodyBlocks}</div>` : "",
    docSection("기획 의도", docText(c.overview)),
    docSection("톤앤매너", docText(c.toneAndManner)),
    c.themes?.length ? docSection("주제", docList(c.themes)) : "",
    docSection("시즌 구조", [
      structure.act1 ? `<p class="doc-text"><b>1막</b> ${escapeHtml(structure.act1)}</p>` : "",
      structure.act2 ? `<p class="doc-text"><b>2막</b> ${escapeHtml(structure.act2)}</p>` : "",
      structure.act3 ? `<p class="doc-text"><b>3막</b> ${escapeHtml(structure.act3)}</p>` : ""
    ].join("")),
    docSection("주요 인물", characters),
    docSection(`화별 시놉시스 (${episodes.length}화)`, episodeCards),
    docSection("시즌 엔딩", docText(c.seasonEnding)),
    docSection("제작 참고", docList(c.productionNotes))
  ].join("");
}

function renderCharactersDoc(c) {
  const cards = (c.characters || []).filter((person) => person && person.name).map((person) => `
    <div class="doc-character">
      <div class="doc-character-head">
        <strong>${escapeHtml(person.name)}</strong>
        ${person.role ? `<span class="doc-chip">${escapeHtml(person.role)}</span>` : ""}
        <small>${[person.age, person.occupation].filter(Boolean).map(escapeHtml).join(" · ")}</small>
      </div>
      ${person.personality?.length ? docList(person.personality) : ""}
      <div class="doc-character-grid">
        ${person.desire ? `<p><b>원하는 것</b> ${escapeHtml(person.desire)}</p>` : ""}
        ${person.need ? `<p><b>필요한 것</b> ${escapeHtml(person.need)}</p>` : ""}
        ${person.wound ? `<p><b>상처</b> ${escapeHtml(person.wound)}</p>` : ""}
        ${person.fear ? `<p><b>두려움</b> ${escapeHtml(person.fear)}</p>` : ""}
      </div>
      ${person.speechStyle ? `<p class="doc-speech"><b>말투</b> ${escapeHtml(person.speechStyle)}</p>` : ""}
      ${person.signaturePhrases?.length ? `<div class="doc-quotes">${person.signaturePhrases.map((line) => `<span>"${escapeHtml(line)}"</span>`).join("")}</div>` : ""}
      ${person.background ? `<p class="doc-text"><b>배경</b> ${escapeHtml(person.background)}</p>` : ""}
      ${person.arc ? `<p class="doc-text"><b>변화</b> ${escapeHtml(person.arc)}</p>` : ""}
      ${person.relationships?.length ? `<div class="doc-relations">${person.relationships.map((rel) => `<p><b>${escapeHtml(rel.with || "")}</b> ${escapeHtml(rel.type || "")} — ${escapeHtml(rel.detail || "")}</p>`).join("")}</div>` : ""}
      ${person.castingDirection ? `<p class="doc-pair-extra"><b>캐스팅</b> ${escapeHtml(person.castingDirection)}</p>` : ""}
    </div>`).join("");
  return [docText(c.summary), docSection("인물", cards)].join("");
}

/* ---- 기획 방향 ---- */
function renderDirectionPanel(project, editable) {
  const direction = project.direction || {};
  $("#directionPlatform").value = direction.targetPlatform || "";
  $("#directionEpisodes").value = direction.episodeCount || 4;
  $("#directionGenres").value = (direction.genres || []).join(", ");
  $("#directionAudience").value = direction.audience || "";
  $("#directionTone").value = direction.tone || "";
  $("#directionFree").value = direction.freeDirection || "";

  ["#directionPlatform", "#directionEpisodes", "#directionGenres", "#directionAudience", "#directionTone", "#directionFree"]
    .forEach((selector) => { $(selector).readOnly = !editable; });
  $("#directionButtons").classList.toggle("is-hidden", !editable);
  $("#directionHint").textContent = "";
}

async function saveDirection() {
  const project = state.currentProject;
  if (!project) return;
  const button = $("#directionSave");
  button.disabled = true;
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(project.id)}/direction`, {
      method: "PUT",
      body: JSON.stringify({
        targetPlatform: $("#directionPlatform").value.trim(),
        episodeCount: Number($("#directionEpisodes").value) || 4,
        genres: $("#directionGenres").value,
        audience: $("#directionAudience").value.trim(),
        tone: $("#directionTone").value.trim(),
        freeDirection: $("#directionFree").value.trim()
      })
    });
    state.currentProject = payload.item;
    renderDirectionPanel(payload.item, true);
    $("#directionHint").textContent = `저장됨 · ${formatDate(payload.item.updatedAt)}`;
    showToast("방향 저장", "기획 방향을 반영했습니다.");
  } catch (error) {
    showToast("저장하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function renderCoverHistory(project, editable) {
  const history = project.coverHistory || [];
  const block = $("#coverHistoryBlock");
  const grid = $("#coverHistoryGrid");
  const limit = project.coverHistoryLimit || 3;

  $("#coverHistoryLimit").textContent = limit;
  $("#coverHistoryCount").textContent = `${history.length} / ${limit}`;
  block.classList.toggle("is-hidden", history.length === 0 || !editable);
  grid.innerHTML = "";

  history.forEach((entry) => {
    const item = document.createElement("figure");
    item.className = "cover-history-item";
    const label = entry.source === "ai" ? "AI" : entry.source === "upload" ? "업로드" : "이전";
    item.innerHTML = `
      <img src="${escapeHtml(entry.url)}" alt="이전 표지" loading="lazy">
      <figcaption>
        <span>${escapeHtml(label)}</span>
        <small>${formatDate(entry.replacedAt)}</small>
      </figcaption>`;
    const restore = document.createElement("button");
    restore.type = "button";
    restore.textContent = "되돌리기";
    restore.addEventListener("click", () => restoreCover(project, entry.key, restore));
    item.appendChild(restore);
    grid.appendChild(item);
  });
}

async function restoreCover(project, coverKey, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "적용 중...";
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(project.id)}/cover/restore`, {
      method: "POST",
      body: JSON.stringify({ coverKey })
    });
    state.currentProject = payload.item;
    renderProjectDetail(payload.item);
    await loadProjects();
    showToast("표지 되돌리기", "이전 표지를 현재 표지로 지정했습니다.");
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    showToast("되돌리지 못했습니다", error.message, "error");
  }
}

async function saveProjectDetail() {
  const project = state.currentProject;
  if (!project) return;
  const button = $("#detailSave");
  button.disabled = true;
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(project.id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        title: $("#detailTitle").value.trim(),
        story: $("#detailStory").value.trim()
      })
    });
    state.currentProject = payload.item;
    renderProjectDetail(payload.item);
    $("#detailSaveHint").textContent = `저장됨 · ${formatDate(payload.item.updatedAt)}`;
    await loadProjects();
    showToast("저장 완료", "프로젝트 내용을 업데이트했습니다.");
  } catch (error) {
    showToast("저장하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function toggleDetailVisibility() {
  const project = state.currentProject;
  if (!project) return;
  await toggleVisibility(project);
  await openProject(project.id);
}

async function deleteDetailProject() {
  const project = state.currentProject;
  if (!project) return;
  if (!await confirmDialog({
    title: "프로젝트 삭제",
    lead: "이 작업은 되돌릴 수 없습니다.",
    body: "프로젝트와 표지 이미지가 함께 삭제됩니다. 생성한 영상과 콘티는 라이브러리에 남습니다.",
    target: project.title,
    okLabel: "프로젝트 삭제"
  })) return;
  try {
    await api(`/api/projects/${encodeURIComponent(project.id)}`, { method: "DELETE" });
    state.currentProject = null;
    await loadProjects();
    navigate("home", { replace: true });
    showToast("프로젝트 삭제", "프로젝트를 삭제했습니다.");
  } catch (error) {
    showToast("삭제하지 못했습니다", error.message, "error");
  }
}

/**
 * 프로젝트 내용을 가지고 제작 화면으로 넘긴다.
 * kind: short(숏폼 한 컷) / storyboard(콘티 이미지) / long(롱테이크)
 */
/* ------------------------------ 프로젝트 맥락 ------------------------------ */
/* sendProjectTo() 는 프로젝트의 텍스트를 스튜디오 폼에 복사할 뿐, 도착한 화면에는
   어느 작품에서 왔는지가 남지 않는다. 프로젝트 다섯 개를 오가면 지금 무엇을 만들고
   있는지 알 수 없다.

   주의: 여기서 유지하는 것은 "화면 맥락"이지 저장 위치가 아니다.
   생성한 이미지·영상은 여전히 사용자 단위로 저장된다(S3 키에 프로젝트가 없다).
   그래서 배너 문구에서 "이 프로젝트에 저장된다"고 말하지 않는다. */
const WORKING_KEY = "toon-change-ai-pj:working";

function setWorkingProject(project) {
  if (project) {
    sessionStorage.setItem(WORKING_KEY, JSON.stringify({ id: project.id, title: project.title }));
  } else {
    sessionStorage.removeItem(WORKING_KEY);
  }
  renderWorkingProject();
}

function getWorkingProject() {
  try {
    return JSON.parse(sessionStorage.getItem(WORKING_KEY) || "null");
  } catch (_error) {
    return null;
  }
}

function renderWorkingProject() {
  const bar = $("#fromProject");
  if (!bar) return;
  const working = getWorkingProject();
  // 제작 화면에서만 의미가 있다. 프로젝트 목록·상세에서는 감춘다.
  const showOn = ["studio", "imageStudio", "library", "videoGen"];
  const show = Boolean(working) && showOn.includes(state.activeView);
  bar.classList.toggle("is-hidden", !show);
  if (show) $("#fromProjectTitle").textContent = working.title || working.id;
}

function sendProjectTo(kind) {
  const project = state.currentProject;
  if (!project) return;
  setWorkingProject(project);
  const story = (project.story || "").trim();

  if (kind === "storyboard") {
    if (story) $("#imageScene").value = story.slice(0, 4000);
    const direction = project.direction || {};
    // 프로젝트에 적어둔 톤앤매너를 그림체 입력의 출발점으로 준다.
    if (direction.tone) $("#imageStyle").value = direction.tone;
    state.imagePurpose = "storyboard";
    renderPurposeOptions();
    navigate("imageStudio");
    showToast(
      "콘티 화면으로 이동",
      story ? "이야기를 넣어두었습니다. 컷 수와 설명 상세도를 고르고 시작하세요." : "만들 이야기를 적어주세요."
    );
    return;
  }

  if (kind === "videoGen") {
    navigate("videoGen");
    showToast("영상 만들기", "글을 쓰거나 이미지를 올리면 AI가 나머지를 채웁니다.");
    return;
  }

  if (story) {
    const input = $("#promptInput");
    input.value = story.slice(0, 600);
    localStorage.setItem(STORAGE_KEYS.prompt, input.value);
    updatePromptCount();
  }
  const direction = project.direction || {};
  if (direction.tone && !$("#videoStyle").value.trim()) $("#videoStyle").value = direction.tone;

  if (kind === "short") {
    // 숏폼은 세로 화면에 짧은 길이가 기본이다.
    state.aspectRatio = "9:16";
    state.duration = "9";
    localStorage.setItem(STORAGE_KEYS.aspect, state.aspectRatio);
    localStorage.setItem(STORAGE_KEYS.duration, state.duration);
    syncVideoOptions();
    navigate("studio");
    showToast("숏폼 만들기", "세로 9:16, 9초로 맞춰 두었습니다.");
    return;
  }

  // 롱테이크
  state.aspectRatio = "16:9";
  localStorage.setItem(STORAGE_KEYS.aspect, state.aspectRatio);
  syncVideoOptions();
  navigate("studio");
  window.setTimeout(() => {
    $("#longTakeButton").scrollIntoView({ behavior: "smooth", block: "center" });
  }, 200);
  showToast("롱테이크 만들기", "아래 롱테이크 항목에서 목표 길이를 고르고 시작하세요.");
}

function openProjectModal() {
  state.projectModalOpener = document.activeElement;
  $("#projectForm").reset();
  state.projectCreateId = newProjectOperationId();
  $("#projectError").textContent = "";
  $$("#visibilityOptions button").forEach((button, index) => button.classList.toggle("active", index === 0));
  $$("#inputTypeOptions .input-type").forEach((button, index) => button.classList.toggle("active", index === 0));
  const bothDisabled = configureTrackChoices("#projectTrackOptions", { clear: true, editable: true });
  if (bothDisabled) {
    $("#projectError").textContent = "스토리 개발과 영상 개발이 모두 꺼져 있습니다. 관리자 설정을 확인해 주세요.";
  }
  syncProjectCreateState();
  $("#projectModal").classList.remove("is-hidden");
  document.body.classList.add("modal-open");
  const firstChoice = $("#projectTrackOptions [data-track]:not(:disabled)");
  (firstChoice || $("#projectTitle"))?.focus();
}

function closeProjectModal() {
  const modal = $("#projectModal");
  if (!modal || modal.classList.contains("is-hidden")) return;
  modal.classList.add("is-hidden");
  document.body.classList.toggle("modal-open", Boolean($(".modal-scrim:not(.is-hidden)")));
  const opener = state.projectModalOpener;
  state.projectModalOpener = null;
  state.projectCreateId = "";
  if (opener && document.contains(opener)) opener.focus();
}

/* ------------------------------ users (admin) ------------------------------ */
function renderUsers() {
  const body = $("#userTableBody");
  body.innerHTML = "";
  state.users.forEach((user) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><strong>${escapeHtml(user.displayName || user.username)}</strong><small>${escapeHtml(user.username)}</small></td>
      <td>${user.role === "admin" ? '<span class="state-default">관리자</span>' : '<span class="state-off">일반</span>'}</td>
      <td>${quotaSummaryHtml(user.quotas)}</td>
      <td>${user.projectCount}</td>
      <td>${formatDate(user.createdAt)}</td>
      <td>${formatDate(user.lastLoginAt)}</td>
      <td class="row-actions"></td>`;
    const actions = row.querySelector(".row-actions");
    if (user.role !== "admin") {
      // 한도 조정은 삭제와 나란히 두지만 위험도가 전혀 다르다. 삭제만 danger 로 둔다.
      const quotaButton = document.createElement("button");
      quotaButton.type = "button";
      quotaButton.textContent = "한도";
      quotaButton.addEventListener("click", () => openQuotaModal(user));
      actions.appendChild(quotaButton);

      const button = document.createElement("button");
      button.type = "button";
      button.className = "danger";
      button.textContent = "삭제";
      button.addEventListener("click", () => openDeleteUserModal(user));
      actions.appendChild(button);
    } else {
      actions.textContent = "기본 계정";
    }
    body.appendChild(row);
  });
}

/* ------------------------------ 생성 한도 (admin) ------------------------------
  한도는 **결과물 건수**다. 모델 호출 수가 아니다. 콘티 한 장은 내부에서 모델을 열 번 넘게
  부르지만 1건으로 센다. 그래서 사용량 화면(호출 수)과 이 숫자는 일부러 다르다.
*/
const QUOTA_FIELDS = [
  { id: "video", label: "영상", hint: "영상 만들기 · 롱테이크 · NEW_영상 제작" },
  { id: "asset", label: "이미지·문서", hint: "이미지 · 콘티 · 트리트먼트 · 표지 · 화풍 변환" }
];

function quotaSummaryHtml(quotas) {
  // 서버는 한도가 없는 계정에 limited:false 를 준다. "무제한"과 "0회 남음"을 반드시
  // 구분해야 한다. 예전 사고 방식대로 빈 값을 0으로 읽으면 멀쩡한 계정이 잠긴 것처럼 보인다.
  if (!quotas || !quotas.limited) return '<span class="state-off">무제한</span>';
  const parts = QUOTA_FIELDS.map((field) => {
    const entry = quotas.categories?.[field.id];
    if (!entry || entry.limit === null || entry.limit === undefined) {
      return `${field.label} 무제한`;
    }
    return `${field.label} ${entry.used}/${entry.limit}`;
  });
  return `<small>${escapeHtml(parts.join(" · "))}</small>`;
}

function openQuotaModal(user) {
  state.quotaTarget = user;
  const modal = $("#quotaModal");
  if (!modal) return;
  $("#quotaUserName").textContent = user.username;
  $("#quotaError").textContent = "";
  $("#quotaReset").checked = false;

  QUOTA_FIELDS.forEach((field) => {
    const input = $(`#quota_${field.id}`);
    if (!input) return;
    const entry = user.quotas?.categories?.[field.id];
    // 빈 칸 = 무제한. 0 은 "한 번도 못 씀"이라 다른 값이다.
    input.value = entry && entry.limit !== null && entry.limit !== undefined ? String(entry.limit) : "";
  });
  renderQuotaUsed(user.quotas);
  modal.classList.remove("is-hidden");
  document.body.classList.add("modal-open");
}

function renderQuotaUsed(quotas) {
  const box = $("#quotaUsed");
  if (!box) return;
  if (!quotas || !quotas.limited) {
    box.textContent = "지금은 한도가 없습니다. 숫자를 넣으면 그때부터 제한됩니다.";
    return;
  }
  box.innerHTML = QUOTA_FIELDS.map((field) => {
    const entry = quotas.categories?.[field.id];
    if (!entry || entry.limit === null || entry.limit === undefined) {
      return `<span>${escapeHtml(field.label)}: 무제한</span>`;
    }
    return `<span>${escapeHtml(field.label)}: ${entry.used}건 사용 / ${entry.remaining}건 남음</span>`;
  }).join("");
}

function closeQuotaModal() {
  $("#quotaModal")?.classList.add("is-hidden");
  document.body.classList.remove("modal-open");
  state.quotaTarget = null;
}

async function saveQuota() {
  const user = state.quotaTarget;
  if (!user) return;
  const button = $("#quotaSave");
  const body = {};

  for (const field of QUOTA_FIELDS) {
    const raw = ($(`#quota_${field.id}`)?.value || "").trim();
    if (raw === "") {
      // 빈 칸은 "무제한으로 만들어라"는 뜻이다. 보내지 않으면 서버가 기존 값을 유지하므로
      // 한도를 푸는 방법이 없어진다. null 을 명시해서 지운다.
      body[field.id] = null;
      continue;
    }
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 0) {
      $("#quotaError").textContent = `${field.label} 한도는 0 이상의 정수여야 합니다.`;
      return;
    }
    body[field.id] = value;
  }
  if ($("#quotaReset")?.checked) body.reset = true;

  button.disabled = true;
  try {
    const payload = await api(`/api/admin/users/${encodeURIComponent(user.username)}/quota`, {
      method: "PATCH",
      body: JSON.stringify(body)
    });
    // 목록의 값을 응답으로 갱신한다. 다시 불러오지 않으면 표가 옛 숫자를 보여 준다.
    const target = state.users.find((item) => item.username === user.username);
    if (target) target.quotas = payload.quotas;
    renderUsers();
    closeQuotaModal();
    const reset = payload.resetCount ? ` 사용 기록 ${payload.resetCount}건을 초기화했습니다.` : "";
    showToast("한도를 저장했습니다", `${user.username}에 적용했습니다.${reset}`);
  } catch (error) {
    $("#quotaError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function saveCompanyCode(event) {
  event.preventDefault();
  try {
    const payload = await api("/api/admin/company-code", {
      method: "PUT",
      body: JSON.stringify({ companyCode: $("#companyCodeInput").value.trim() })
    });
    showToast("회사 코드 저장", `이제 가입 시 ${payload.companyCode} 코드가 필요합니다.`);
  } catch (error) {
    showToast("코드를 저장하지 못했습니다", error.message, "error");
  }
}

function openDeleteUserModal(user) {
  state.deleteTarget = user;
  $("#deleteUserName").textContent = user.username;
  $("#deleteUserProjects").textContent = user.projectCount > 0 ? `(프로젝트 ${user.projectCount}개 보유)` : "(보유 프로젝트 없음)";
  $("#deleteUserError").textContent = "";
  $("#deleteConfirmCheck").checked = false;
  $("#deleteUserConfirm").disabled = true;
  $$("#deleteOptions input").forEach((input, index) => { input.checked = index === 0; });
  $("#deleteUserModal").classList.remove("is-hidden");
}

function closeDeleteUserModal() {
  $("#deleteUserModal").classList.add("is-hidden");
  state.deleteTarget = null;
}

async function confirmDeleteUser() {
  const user = state.deleteTarget;
  if (!user) return;
  const scope = $$("#deleteOptions input").find((input) => input.checked)?.value || "user";
  const deleteProjects = scope === "all";
  const button = $("#deleteUserConfirm");
  button.disabled = true;
  try {
    const payload = await api(
      `/api/admin/users/${encodeURIComponent(user.username)}?deleteProjects=${deleteProjects}`,
      { method: "DELETE" }
    );
    closeDeleteUserModal();
    await Promise.all([loadUsers(), loadProjects()]);
    const removed = payload.result?.deletedProjects || 0;
    const kept = payload.result?.keptProjects || 0;
    showToast(
      "사용자 삭제 완료",
      deleteProjects ? `계정과 프로젝트 ${removed}개를 삭제했습니다.` : `계정을 삭제했습니다. 프로젝트 ${kept}개는 보관됩니다.`
    );
  } catch (error) {
    $("#deleteUserError").textContent = error.message;
    button.disabled = false;
  }
}

/* ------------------------------ models ------------------------------ */
// 스튜디오에서는 영상 모델만 고를 수 있어야 한다. 콘티용 이미지 모델이 섞이면 생성이 실패한다.
function videoModels() {
  return state.models.filter((m) => m.enabled && (m.kind || "video") === "video");
}

function activeModel() {
  const enabled = videoModels();
  return enabled.find((m) => m.id === state.selectedModelId)
    || enabled.find((m) => m.isDefault)
    || enabled[0]
    || null;
}

function renderModelPicker() {
  const picker = $("#modelPicker");
  // 이 화면에 없는 경우에도 불린다(로그인 직후 등). 예전에는 null 에 innerHTML 을 써서
  // 여기서 던진 예외가 뒤따르는 렌더까지 함께 멈췄다.
  if (!picker) return;
  const enabled = videoModels();
  picker.innerHTML = "";
  if (enabled.length === 0) {
    // "못 불러왔다" 와 "등록된 것이 없다" 는 원인도 조치도 다르다. 예전에는 둘 다
    // "관리자가 모델을 등록해야 합니다" 로 보여서, 통신이 실패했을 때 엉뚱하게
    // 모델 카탈로그를 의심하게 만들었다. 실패면 이유와 재시도 버튼을 준다.
    if (state.modelsError) {
      picker.innerHTML = `
        <p class="model-empty">
          모델 목록을 불러오지 못했습니다.<br>${escapeHtml(state.modelsError)}<br>
          <button class="text-action" type="button" id="modelRetry">다시 시도</button>
        </p>`;
      const retry = $("#modelRetry");
      if (retry) retry.addEventListener("click", () => loadModels());
      return;
    }
    picker.innerHTML = '<p class="model-empty">사용 가능한 모델이 없습니다. 관리자가 모델을 등록해야 합니다.</p>';
    return;
  }
  const current = activeModel();
  enabled.forEach((model) => {
    const option = document.createElement("button");
    option.type = "button";
    option.className = `model-option${model.id === current?.id ? " active" : ""}`;
    const max = model.capabilities?.maxDurationSeconds;
    option.innerHTML = `
      <span class="model-radio"></span>
      <span class="model-option-body">
        <strong>${escapeHtml(model.label)}</strong>
        <small>${model.isDefault ? "기본 모델" : "선택 가능"}${max ? ` · 최대 ${escapeHtml(max)}초` : ""}</small>
      </span>`;
    option.addEventListener("click", () => {
      state.selectedModelId = model.id;
      localStorage.setItem(STORAGE_KEYS.model, model.id);
      renderModelPicker();
    });
    picker.appendChild(option);
  });
}

function renderModelTable() {
  if (!state.user?.isAdmin) return;
  const body = $("#modelTableBody");
  body.innerHTML = "";

  /*
    거부된 카탈로그 정의를 표 맨 위에 먼저 알린다.

    이 경고가 없으면 어떤 일이 벌어지는지 실제로 겪었다. ConfigMap 항목에 provider 가
    빠져 있어 서버가 이미지 모델 전부를 버렸다. 이 표에는 영상 모델 하나만 떠 있었고,
    "원래 그런가" 로 보였다. 사용자에게는 표지 생성이 권한 오류로 실패했고, 그 모델은
    이 표에 나온 적조차 없는 모델이었다.
  */
  (state.modelsRejected || []).forEach((item) => {
    const row = document.createElement("tr");
    row.className = "model-rejected";
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.innerHTML =
      `<strong>카탈로그 정의가 거부되었습니다 (${escapeHtml(item.source || "?")} / ` +
      `${escapeHtml(item.id || "?")})</strong>` +
      `<small>${escapeHtml(item.reason || "")} — 이 모델을 쓰는 기능은 지금 동작하지 않습니다.</small>`;
    row.appendChild(cell);
    body.appendChild(row);
  });

  state.models.forEach((model) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><strong>${escapeHtml(model.label)}</strong><small>${escapeHtml(model.id)}</small></td>
      <td>${escapeHtml({ video: "영상", image: "콘티·이미지", text: "텍스트" }[model.kind] || model.kind || "영상")}</td>
      <td><code>${escapeHtml(model.modelId || "-")}</code></td>
      <td>${escapeHtml(model.region || "-")}</td>
      <td><span class="source-tag">${escapeHtml(model.source || "-")}</span></td>
      <td>${model.enabled ? '<span class="state-on">활성</span>' : '<span class="state-off">비활성</span>'}${model.isDefault ? '<span class="state-default">기본</span>' : ""}</td>
      <td class="row-actions"></td>`;
    const actions = row.querySelector(".row-actions");

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.textContent = model.enabled ? "비활성화" : "활성화";
    toggle.addEventListener("click", () => patchModel(model.id, { enabled: !model.enabled }));
    actions.appendChild(toggle);

    if (!model.isDefault && model.enabled) {
      const makeDefault = document.createElement("button");
      makeDefault.type = "button";
      makeDefault.className = "primary";
      makeDefault.textContent = "기본 지정";
      makeDefault.addEventListener("click", () => patchModel(model.id, { isDefault: true }));
      actions.appendChild(makeDefault);
    }
    body.appendChild(row);
  });
}

async function patchModel(modelId, changes) {
  try {
    await api(`/api/admin/models/${encodeURIComponent(modelId)}`, {
      method: "PATCH", body: JSON.stringify(changes)
    });
    // 카탈로그만 바뀌었다. 대시보드 수치는 관계없으므로 모델만 다시 읽는다.
    await loadModels();
    showToast("모델 설정 변경", `${modelId} 설정을 업데이트했습니다.`);
  } catch (error) {
    showToast("변경하지 못했습니다", error.message, "error");
  }
}

/* ------------------------------ studio ------------------------------ */
function updatePromptCount() {
  $("#promptCount").textContent = $("#promptInput").value.length;
}

function syncDuration() {
  $$("#durationOptions button").forEach((b) => b.classList.toggle("active", b.dataset.value === state.duration));
  $("#selectedDuration").textContent = state.duration;
}

function syncVideoOptions() {
  syncDuration();
  $$("#resolutionOptions button").forEach((b) => b.classList.toggle("active", b.dataset.value === state.resolution));
  $$("#aspectOptions button").forEach((b) => b.classList.toggle("active", b.dataset.value === state.aspectRatio));
  $("#selectedResolution").textContent = state.resolution;
  $("#selectedAspect").textContent = state.aspectRatio;
}

/* ------------------------------ 그림체 · 콘티 이미지 ------------------------------ */
async function loadStyles() {
  try {
    const payload = await api("/api/styles");
    state.styles = payload.presets || [];
    state.stylePurposes = payload.purposes || [];
    renderStyleChips();
    renderPurposeOptions();
  } catch (error) {
    showToast("그림체 목록을 불러오지 못했습니다", error.message, "error");
  }
}

// 프리셋을 직접 고르면 AI 판단을 건너뛴다. 다시 누르면 해제되어 자동 판단으로 돌아간다.
function styleChipRow(containerId, selectedKey) {
  const container = $(containerId);
  if (!container) return;
  container.innerHTML = "";
  state.styles.forEach((preset) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `style-chip${state[selectedKey] === preset.id ? " active" : ""}`;
    chip.title = preset.hint;
    chip.textContent = preset.label;
    chip.addEventListener("click", () => {
      state[selectedKey] = state[selectedKey] === preset.id ? "" : preset.id;
      renderStyleChips();
    });
    container.appendChild(chip);
  });
}

function renderStyleChips() {
  styleChipRow("#stylePresetChips", "imagePreset");
  styleChipRow("#videoStyleChips", "videoPreset");
}

function renderPurposeOptions() {
  const container = $("#imagePurposeOptions");
  if (!container) return;
  container.innerHTML = "";
  state.stylePurposes.forEach((purpose, index) => {
    const button = document.createElement("button");
    button.type = "button";
    if (!state.imagePurpose && index === 1) state.imagePurpose = purpose.id;
    button.className = state.imagePurpose === purpose.id ? "active" : "";
    button.dataset.value = purpose.id;
    button.textContent = purpose.label;
    button.addEventListener("click", () => {
      state.imagePurpose = purpose.id;
      renderPurposeOptions();
    });
    container.appendChild(button);
  });
  syncStoryboardMode();
}

/* ------------------------------ 콘티 기획표 ------------------------------ */
/**
 * 콘티는 그림 한 장이 아니라 컷별 그림 + 설명이 짝을 이루는 표다.
 * 용도에서 '콘티'를 고르면 컷 수와 설명 상세도를 정하는 옵션이 열리고,
 * 생성 버튼도 단일 이미지가 아니라 기획표를 만드는 쪽으로 바뀐다.
 */
function isStoryboardMode() {
  return state.imagePurpose === "storyboard";
}

function syncStoryboardMode() {
  const on = isStoryboardMode();
  $("#storyboardOptions")?.classList.toggle("is-hidden", !on);
  $("#imageGenerateButton")?.classList.toggle("is-hidden", on);
  $("#storyboardGenerateButton")?.classList.toggle("is-hidden", !on);
}

async function loadStoryboardOptions() {
  try {
    const payload = await api("/api/storyboards/options");
    state.sbCutCounts = payload.cutCounts || [];
    state.sbDetails = payload.details || [];
    state.sbModes = payload.modes || [];
    state.sbShots = payload.shots || [];
    state.sbMaxCuts = payload.maxCuts || 24;
    if (!state.sbCutCount) state.sbCutCount = payload.defaultCutCount || 6;
    if (!state.sbDetail) state.sbDetail = payload.defaultDetail || "normal";
    if (!state.sbMode) state.sbMode = "auto";
    renderStoryboardOptions();
  } catch (error) {
    console.warn("콘티 옵션을 불러오지 못했습니다", error);
  }
}

/** 직접 입력한 컷 줄 수. 번호를 붙여 적어도 세어진다. */
function manualCutLines() {
  return ($("#sbManualCuts")?.value || "")
    .split("\n")
    .map((line) => line.replace(/^\s*\d+\s*[.)\]]\s*/, "").trim())
    .filter((line) => line.length >= 2);
}

function renderStoryboardOptions() {
  const cuts = $("#sbCutOptions");
  const details = $("#sbDetailOptions");
  if (!cuts || !details) return;

  const modes = $("#sbModeOptions");
  modes.innerHTML = "";
  state.sbModes.forEach((mode) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.value = mode.id;
    button.className = state.sbMode === mode.id ? "active" : "";
    button.textContent = mode.label;
    button.title = mode.hint || "";
    button.addEventListener("click", () => {
      state.sbMode = mode.id;
      renderStoryboardOptions();
    });
    modes.appendChild(button);
  });
  const manual = state.sbMode === "manual";
  $("#sbAutoBlock").classList.toggle("is-hidden", manual);
  $("#sbManualBlock").classList.toggle("is-hidden", !manual);

  cuts.innerHTML = "";
  state.sbCutCounts.forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.value = String(option.value);
    button.className = state.sbCutCount === option.value ? "active" : "";
    button.textContent = option.label;
    button.addEventListener("click", () => {
      state.sbCutCount = option.value;
      renderStoryboardOptions();
    });
    cuts.appendChild(button);
  });

  details.innerHTML = "";
  state.sbDetails.forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.value = option.id;
    button.className = state.sbDetail === option.id ? "active" : "";
    button.textContent = option.label;
    button.addEventListener("click", () => {
      state.sbDetail = option.id;
      renderStoryboardOptions();
    });
    details.appendChild(button);
  });

  const picked = state.sbDetails.find((option) => option.id === state.sbDetail);
  $("#sbDetailHint").textContent = picked ? `장면 설명: ${picked.hint}` : "";

  // 직접 입력이면 적은 줄 수가 곧 컷 수다.
  const count = manual ? manualCutLines().length : state.sbCutCount;
  const perCut = state.sbCutCounts[0]
    ? (state.sbCutCounts[0].estimateSeconds - 20) / state.sbCutCounts[0].value
    : 9;
  const mins = Math.max(1, Math.round((20 + count * perCut) / 60));

  if (manual) {
    $("#sbManualCount").textContent = count
      ? `컷 ${count}개를 만듭니다 (최대 ${state.sbMaxCuts}개). AI가 컷을 합치거나 늘리지 않습니다.`
      : `한 줄에 컷 하나씩 적어주세요 (최대 ${state.sbMaxCuts}개).`;
  }
  $("#sbEstimate").textContent = count
    ? `컷 ${count}개를 각각 그립니다. 약 ${mins}분 걸리고 컷 수만큼 생성 비용이 발생합니다. `
      + "화면을 벗어나도 계속 진행됩니다."
    : "";
  $("#sbButtonNote").textContent = count ? `컷 ${count}개 · 약 ${mins}분` : "컷을 먼저 적어주세요";
}

async function startStoryboard() {
  const story = $("#imageScene").value.trim();
  const manual = state.sbMode === "manual";
  const lines = manualCutLines();

  if (manual) {
    if (!lines.length) {
      $("#imageError").textContent = "컷을 한 줄 이상 적어주세요.";
      $("#sbManualCuts").focus();
      return;
    }
    if (lines.length > state.sbMaxCuts) {
      $("#imageError").textContent = `컷은 최대 ${state.sbMaxCuts}개까지입니다. 지금 ${lines.length}개입니다.`;
      return;
    }
  } else if (story.length < 10) {
    $("#imageError").textContent = "콘티로 만들 이야기를 10자 이상 적어주세요.";
    $("#imageScene").focus();
    return;
  }

  const count = manual ? lines.length : state.sbCutCount;
  const cutOption = state.sbCutCounts.find((option) => option.value === state.sbCutCount);
  const perCut = state.sbCutCounts[0]
    ? (state.sbCutCounts[0].estimateSeconds - 20) / state.sbCutCounts[0].value
    : 9;
  const mins = manual
    ? Math.max(1, Math.round((20 + count * perCut) / 60))
    : (cutOption ? Math.max(1, Math.round(cutOption.estimateSeconds / 60)) : 2);

  if (!await confirmDialog({
    title: "콘티 기획표 생성",
    lead: manual
      ? `적으신 컷 ${count}개를 그대로 그립니다.`
      : `컷 ${count}개를 순서대로 그립니다.`,
    body: `약 ${mins}분이 걸리고 컷마다 이미지 생성 비용이 발생합니다. `
      + "중간에 멈춰도 이미 만든 컷의 비용은 청구됩니다.",
    target: `${count}컷`,
    okLabel: "콘티 만들기",
    tone: "warn"
  })) return;

  const button = $("#storyboardGenerateButton");
  button.disabled = true;
  $("#imageError").textContent = "";
  startRun("imageStudio", `컷 ${count}개를 나누고 순서대로 그립니다. 약 ${mins}분 걸립니다.`);
  try {
    const working = getWorkingProject();
    const payload = await api("/api/storyboards", {
      method: "POST",
      body: JSON.stringify({
        mode: state.sbMode,
        prompt: story,
        // 직접 입력이면 적은 줄을 그대로 보낸다. 컷 수는 서버가 줄 수로 정한다.
        cuts: manual ? lines : undefined,
        cutCount: manual ? undefined : state.sbCutCount,
        detail: state.sbDetail,
        stylePrompt: $("#imageStyle").value.trim(),
        presetId: state.imagePreset || undefined,
        aspectRatio: state.imageAspect,
        referenceKey: state.referenceKey || undefined,
        // 프로젝트에서 넘어왔다면 인물 설정과 트리트먼트를 기획에 함께 쓴다.
        projectId: working?.id || undefined
      })
    });
    state.storyboardId = payload.item.id;
    renderStoryboard(payload.item);
    pollStoryboard();
    showToast("콘티 생성 시작", "컷을 나누고 순서대로 그립니다.");
  } catch (error) {
    $("#imageError").textContent = error.message;
    endRun("imageStudio", false, error.message);
    showToast("콘티를 시작하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

const SB_STATUS_LABELS = {
  planning: "컷 나누는 중",
  drawing: "컷 그리는 중",
  ready: "완료",
  failed: "실패"
};

const SB_SHOT_LABELS = {
  wide: "와이드",
  medium: "미디엄",
  "close-up": "클로즈업",
  "close up": "클로즈업",
  closeup: "클로즈업",
  "over-the-shoulder": "오버더숄더",
  "over the shoulder": "오버더숄더"
};

function renderStoryboard(board) {
  const panel = $("#storyboardPanel");
  if (!panel) return;
  panel.classList.remove("is-hidden");
  state.storyboard = board;

  $("#sbTitle").textContent = board.title || "콘티 기획표";
  $("#sbSheetTitle").textContent = board.title || "-";
  $("#sbSheetCuts").textContent = `${board.cutCount || 0}컷`;
  $("#sbSheetSeconds").textContent = board.totalSeconds ? `${board.totalSeconds}초` : "-";

  const detail = state.sbDetails.find((option) => option.id === board.detail);
  $("#sbMeta").textContent = [
    board.presetLabel && `그림체 ${board.presetLabel}`,
    board.modelLabel && `모델 ${board.modelLabel}`,
    detail && `설명 ${detail.label}`
  ].filter(Boolean).join(" · ");

  const status = $("#sbStatus");
  status.textContent = SB_STATUS_LABELS[board.status] || board.status;
  status.className = `doc-chip ${board.status === "ready" ? "ready" : board.status === "failed" ? "failed" : "generating"}`;

  const total = board.cutCount || 1;
  const done = board.doneCount || 0;
  const percent = Math.min(100, Math.round((done / total) * 100));
  $("#sbBar").style.width = `${percent}%`;
  const progressText = board.status === "planning"
    ? "이야기를 컷으로 나누고 있습니다..."
    : `컷 ${done}/${total} 완성` + (board.failedCount ? ` · 실패 ${board.failedCount}개` : "");
  $("#sbProgress").textContent = progressText;

  // 상단 배너: 진행 중이면 갱신, 끝났으면 마무리한다.
  // 이 함수는 화면에 들어올 때 예전 콘티를 다시 그리는 데도 쓰인다(loadLatestStoryboard).
  // 그때 배너를 건드리면 방금 만든 이미지의 완료 문구가 엉뚱한 콘티 문구로 덮인다.
  // 그래서 지금 추적 중인 콘티일 때만 반영한다.
  if (state.storyboardId === board.id) {
    if (board.status === "ready") {
      endRun("imageStudio", true, `${progressText} · 아래 기획표에서 확인하세요.`);
    } else if (board.status === "failed") {
      endRun("imageStudio", false, board.error || "콘티를 만들지 못했습니다.");
    } else {
      updateRun("imageStudio", Math.max(8, percent), progressText);
    }
  }

  const errorBlock = $("#sbError");
  errorBlock.classList.toggle("is-hidden", !board.error);
  errorBlock.textContent = board.error || "";

  const body = $("#sbBody");
  body.innerHTML = "";
  (board.cuts || []).forEach((cut) => {
    const row = document.createElement("tr");
    row.className = `sb-row ${cut.status}`;

    const shotLabel = SB_SHOT_LABELS[(cut.shot || "").toLowerCase()] || cut.shot || "";
    const audioParts = [
      cut.dialogue && `<b>대사</b> ${escapeHtml(cut.dialogue)}`,
      cut.audio && `<b>음향</b> ${escapeHtml(cut.audio)}`,
      cut.note && `<b>메모</b> ${escapeHtml(cut.note)}`
    ].filter(Boolean);

    row.innerHTML = `
      <td class="sb-col-cut"><span class="sb-cut-no">${cut.number}</span>${shotLabel ? `<small>${escapeHtml(shotLabel)}</small>` : ""}</td>
      <td class="sb-col-video">
        <div class="sb-frame ${cut.status}">
          ${cut.imageUrl
            ? `<img src="${escapeHtml(cut.imageUrl)}" alt="컷 ${cut.number} 그림" loading="lazy">`
            : `<span class="sb-frame-note">${cut.status === "failed" ? "그림 실패" : "그리는 중"}</span>`}
        </div>
      </td>
      <td class="sb-col-context">${escapeHtml(cut.context || "")}</td>
      <td class="sb-col-audio">${audioParts.length ? audioParts.join("<br>") : '<span class="sb-dim">-</span>'}</td>
      <td class="sb-col-time">${cut.seconds ? `${cut.seconds}s` : "-"}</td>`;

    // 컷 단위 작업. 생성이 끝난 뒤에만 편집할 수 있다(서버도 같은 조건으로 막는다).
    const editable = ["ready", "failed"].includes(board.status);
    const actions = document.createElement("div");
    actions.className = "sb-frame-actions";

    if (cut.imageUrl && cut.imageKey) {
      // 콘티 다음 단계가 이 컷으로 영상을 만드는 것이다.
      const toVideo = document.createElement("button");
      toVideo.type = "button";
      toVideo.className = "text-action";
      toVideo.textContent = "영상으로";
      toVideo.addEventListener("click", () => sendImageToStudio({
        key: cut.imageKey,
        url: cut.imageUrl,
        filename: `cut${cut.number}.png`,
        size: 0
      }));
      actions.appendChild(toVideo);
    }
    if (editable) {
      const redraw = document.createElement("button");
      redraw.type = "button";
      redraw.className = "text-action";
      redraw.textContent = cut.imageUrl ? "다시 그리기" : "그리기";
      redraw.addEventListener("click", () => redrawCut(cut.number, redraw));
      actions.appendChild(redraw);

      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "text-action";
      edit.textContent = "설명 수정";
      edit.addEventListener("click", () => editCut(cut));
      actions.appendChild(edit);

      if ((board.cuts || []).length > 1) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "text-action danger";
        remove.textContent = "삭제";
        remove.addEventListener("click", () => deleteCut(cut));
        actions.appendChild(remove);
      }
    }
    if (actions.childElementCount) row.querySelector(".sb-col-video").appendChild(actions);
    body.appendChild(row);
  });

  if (!(board.cuts || []).length) {
    body.innerHTML = '<tr><td colspan="5" class="usage-empty">컷을 나누는 중입니다...</td></tr>';
  }
}

/* ---- 컷 단위 편집 ---- */
/** 컷 한 개만 다시 그린다. 그림체와 인물은 처음 판단을 그대로 쓴다(서버가 보관한다). */
async function redrawCut(number, button) {
  const board = state.storyboard;
  if (!board) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "그리는 중...";
  try {
    const payload = await api(
      `/api/storyboards/${encodeURIComponent(board.id)}/cuts/${number}`,
      { method: "PATCH", body: JSON.stringify({ redraw: true }) }
    );
    renderStoryboard(payload.item);
    showToast("컷 다시 그림", `${number}번 컷을 새로 그렸습니다.`);
    loadImages();
  } catch (error) {
    button.disabled = false;
    button.textContent = label;
    showToast("다시 그리지 못했습니다", error.message, "error");
  }
}

/** 설명을 고친다. 설명이 바뀌면 그림도 함께 다시 그린다(내용과 그림이 어긋나면 표가 틀린다). */
async function editCut(cut) {
  const board = state.storyboard;
  if (!board) return;
  const context = window.prompt(`${cut.number}번 컷 설명`, cut.context || "");
  if (context === null) return;
  if (context.trim().length < 2) {
    showToast("설명이 필요합니다", "컷 설명을 두 글자 이상 적어주세요.", "error");
    return;
  }
  const dialogue = window.prompt(`${cut.number}번 컷 대사 (없으면 비워두세요)`, cut.dialogue || "");
  if (dialogue === null) return;
  const audio = window.prompt(`${cut.number}번 컷 음향 (없으면 비워두세요)`, cut.audio || "");
  if (audio === null) return;

  const changed = context.trim() !== (cut.context || "").trim();
  try {
    const payload = await api(
      `/api/storyboards/${encodeURIComponent(board.id)}/cuts/${cut.number}`,
      {
        method: "PATCH",
        body: JSON.stringify({ context: context.trim(), dialogue, audio, redraw: changed })
      }
    );
    renderStoryboard(payload.item);
    showToast("컷 수정", changed ? "설명을 고치고 그림을 다시 그렸습니다." : "설명을 고쳤습니다.");
    if (changed) loadImages();
  } catch (error) {
    showToast("수정하지 못했습니다", error.message, "error");
  }
}

async function deleteCut(cut) {
  const board = state.storyboard;
  if (!board) return;
  if (!await confirmDialog({
    title: "컷 삭제",
    lead: "이 작업은 되돌릴 수 없습니다.",
    body: "컷과 그림이 지워지고 뒤 컷의 번호가 하나씩 당겨집니다.",
    target: `${cut.number}번 컷`,
    okLabel: "컷 삭제"
  })) return;
  try {
    const payload = await api(
      `/api/storyboards/${encodeURIComponent(board.id)}/cuts/${cut.number}`,
      { method: "DELETE" }
    );
    renderStoryboard(payload.item);
    showToast("컷 삭제", `${cut.number}번 컷을 삭제했습니다.`);
    loadImages();
  } catch (error) {
    showToast("삭제하지 못했습니다", error.message, "error");
  }
}

async function addCut() {
  const board = state.storyboard;
  if (!board) return;
  const context = window.prompt("추가할 컷 설명");
  if (context === null || context.trim().length < 2) return;
  const afterRaw = window.prompt(
    `몇 번 컷 뒤에 넣을까요? (0은 맨 앞, 비우면 맨 끝)`,
    String((board.cuts || []).length)
  );
  if (afterRaw === null) return;

  const button = $("#sbAddCut");
  button.disabled = true;
  try {
    const payload = await api(`/api/storyboards/${encodeURIComponent(board.id)}/cuts`, {
      method: "POST",
      body: JSON.stringify({
        context: context.trim(),
        after: afterRaw.trim() === "" ? undefined : Number(afterRaw)
      })
    });
    renderStoryboard(payload.item);
    showToast("컷 추가", `${payload.cutNumber}번 컷을 추가했습니다.`);
    loadImages();
  } catch (error) {
    showToast("추가하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function pollStoryboard() {
  window.clearTimeout(state.storyboardTimer);
  if (!state.storyboardId) return;
  state.storyboardTimer = window.setTimeout(async () => {
    try {
      const payload = await api(`/api/storyboards/${encodeURIComponent(state.storyboardId)}`);
      renderStoryboard(payload.item);
      if (["ready", "failed"].includes(payload.item.status)) {
        if (payload.item.status === "ready") {
          showToast("콘티 완료", `컷 ${payload.item.doneCount}개를 그렸습니다.`);
          loadImages();
        } else {
          showToast("콘티 실패", payload.item.error || "다시 시도해 주세요.", "error");
        }
        state.storyboardId = null;
        return;
      }
      pollStoryboard();
    } catch (error) {
      showToast("콘티 진행 상황을 확인하지 못했습니다", error.message, "error");
    }
  }, 6000);
}

/** 화면에 들어올 때 마지막 콘티를 되살린다. 폴링 중이던 작업도 이어서 추적한다. */
async function loadLatestStoryboard() {
  try {
    const payload = await api("/api/storyboards");
    const latest = (payload.items || [])[0];
    if (!latest) return;
    const active = ["planning", "drawing"].includes(latest.status);
    if (active) {
      // 추적 id 와 배너를 렌더보다 먼저 세운다. renderStoryboard 안의 updateRun 이
      // 둘 다 요구한다. 순서가 뒤바뀌면 첫 렌더에서 진행률이 반영되지 않는다.
      state.storyboardId = latest.id;
      startRun("imageStudio", "진행 중인 콘티를 이어서 봅니다...");
    }
    renderStoryboard(latest);
    if (active) pollStoryboard();
  } catch (error) {
    console.warn("콘티를 불러오지 못했습니다", error);
  }
}

async function deleteStoryboard() {
  const board = state.storyboard;
  if (!board) return;
  if (!await confirmDialog({
    title: "콘티 삭제",
    lead: "이 작업은 되돌릴 수 없습니다.",
    body: "기획표와 컷 그림이 모두 지워집니다.",
    target: board.title || board.id,
    okLabel: "콘티 삭제"
  })) return;
  try {
    await api(`/api/storyboards/${encodeURIComponent(board.id)}`, { method: "DELETE" });
    state.storyboard = null;
    state.storyboardId = null;
    window.clearTimeout(state.storyboardTimer);
    $("#storyboardPanel").classList.add("is-hidden");
    showToast("콘티 삭제", "기획표를 삭제했습니다.");
    loadImages();
  } catch (error) {
    showToast("삭제하지 못했습니다", error.message, "error");
  }
}

async function uploadStyleReference(file) {
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    const uploaded = await api("/api/assets", { method: "POST", body });
    setStyleReference(uploaded.item.key, uploaded.item.url || "", file.name);
    showToast("참조 이미지 등록", "이 그림체를 따라 그립니다.");
  } catch (error) {
    showToast("참조 이미지를 올리지 못했습니다", error.message, "error");
  }
}

function setStyleReference(key, url, name) {
  state.referenceKey = key || "";
  const block = $("#referencePreview");
  if (!key) {
    block.classList.add("is-hidden");
    return;
  }
  if (url) $("#referenceImage").src = url;
  $("#referenceName").textContent = name || key.split("/").pop() || "참조 이미지";
  block.classList.remove("is-hidden");
}

async function generateImage() {
  const scene = $("#imageScene").value.trim();
  if (scene.length < 5) {
    $("#imageError").textContent = "장면 설명을 5자 이상 적어주세요.";
    $("#imageScene").focus();
    return;
  }
  const button = $("#imageGenerateButton");
  button.disabled = true;
  $("#imageError").textContent = "";
  setImageStatus("working", "생성 중");
  startRun("imageStudio", "그림체를 판단하고 모델을 고르는 중입니다...");
  // 이 경로는 진행률을 서버에서 받지 않는다(요청 하나로 끝난다). 대기 표시만 올린다.
  updateRun("imageStudio", 45, "모델이 그림을 그리고 있습니다...");

  try {
    const payload = await api("/api/images/generate", {
      method: "POST",
      body: JSON.stringify({
        prompt: scene,
        stylePrompt: $("#imageStyle").value.trim(),
        presetId: state.imagePreset || undefined,
        purpose: state.imagePurpose || "scene",
        aspectRatio: state.imageAspect,
        referenceKey: state.referenceKey || undefined
      })
    });

    const image = $("#resultImage");
    image.src = payload.item.url;
    image.classList.remove("is-hidden");
    $("#imageEmpty").classList.add("is-hidden");
    state.lastImage = payload.item;

    renderStyleResult(payload);
    setImageStatus("success", "완료");
    endRun("imageStudio", true, `${payload.style.presetLabel} · ${payload.model.label}로 그렸습니다.`);
    showToast("이미지 생성 완료", `${payload.style.presetLabel} · ${payload.model.label}`);
    await loadImages();
  } catch (error) {
    setImageStatus("idle", "실패");
    $("#imageError").textContent = error.message;
    endRun("imageStudio", false, error.message);
    showToast("이미지를 생성하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function setImageStatus(kind, label) {
  const badge = $("#imageStatus");
  badge.className = `status-badge ${kind}`;
  badge.innerHTML = `<i></i> ${escapeHtml(label)}`;
}

const STYLE_SOURCE_LABELS = {
  ai: "AI 판단",
  keyword: "키워드 판단",
  manual: "직접 선택",
  default: "기본값"
};

function renderStyleResult(payload) {
  const style = payload.style || {};
  $("#styleResultPreset").textContent = style.presetLabel || "-";
  $("#styleResultTier").textContent = { draft: "러프", standard: "표준", premium: "고품질" }[style.tier] || style.tier || "-";
  $("#styleResultSource").textContent = STYLE_SOURCE_LABELS[style.source] || style.source || "-";
  $("#styleResultReason").textContent = style.reason || "";
  $("#styleResultModel").textContent =
    `모델 ${payload.model?.label || "-"}` + (style.usedStyleReference ? " · 원작 그림체 참조 적용" : "");
  $("#styleResultPrompt").textContent = payload.prompt || "";
  $("#styleResult").classList.remove("is-hidden");
}

async function loadImages() {
  showSkeleton("#imageGrid", 4, "image");
  try {
    const payload = await api("/api/images");
    state.images = payload.items || [];
    renderImages();
  } catch (error) {
    clearSkeleton("#imageGrid");
    showToast("이미지 목록을 불러오지 못했습니다", error.message, "error");
  }
}

function renderImages() {
  const grid = $("#imageGrid");
  if (!grid) return;
  grid.innerHTML = "";
  $("#emptyImages").classList.toggle("is-hidden", state.images.length > 0);
  grid.classList.toggle("is-hidden", state.images.length === 0);

  state.images.forEach((image) => {
    const card = document.createElement("article");
    card.className = "image-card";
    card.innerHTML = `
      <div class="image-thumb"><img src="${escapeHtml(image.url)}" alt="${escapeHtml(image.filename)}" loading="lazy"></div>
      <div class="image-card-info">
        <strong title="${escapeHtml(image.filename)}">${escapeHtml(image.filename)}</strong>
        <p>${formatDate(image.lastModified)} · ${formatBytes(image.size)}</p>
        <div class="image-actions"></div>
      </div>`;

    const actions = card.querySelector(".image-actions");

    // 콘티 이미지에서 가장 자주 하는 일은 이걸로 영상을 만드는 것이다. 그것만 남긴다.
    const toVideo = document.createElement("button");
    toVideo.type = "button";
    toVideo.className = "primary";
    toVideo.textContent = "영상으로";
    toVideo.addEventListener("click", () => sendImageToStudio(image));
    actions.appendChild(toVideo);

    const openLink = document.createElement("a");
    openLink.href = image.url;
    openLink.target = "_blank";
    openLink.rel = "noopener";
    openLink.textContent = "새 창에서 열기";

    actions.appendChild(overflowMenu([
      { element: openLink },
      {
        label: "그림체 참조로 쓰기",
        onSelect: () => {
          setStyleReference(image.key, image.url, image.filename);
          showToast("참조 등록", "이 이미지의 그림체를 따라 그립니다.");
        }
      },
      { label: "이미지 삭제", danger: true, onSelect: () => deleteImage(image, toVideo) }
    ], `${image.filename} 추가 작업`));

    grid.appendChild(card);
  });
}

async function deleteImage(image, button) {
  if (!await confirmDialog({
    title: "이미지 삭제",
    lead: "이 작업은 되돌릴 수 없습니다.",
    body: "저장소에서 파일이 지워집니다. 이 이미지를 첫 프레임으로 쓴 영상은 그대로 남습니다.",
    target: image.filename,
    okLabel: "이미지 삭제"
  })) return;
  button.disabled = true;
  button.textContent = "삭제 중...";
  try {
    await api(`/api/images?key=${encodeURIComponent(image.key)}`, { method: "DELETE" });
    state.images = state.images.filter((item) => item.key !== image.key);
    renderImages();
    showToast("이미지 삭제", "이미지를 삭제했습니다.");
  } catch (error) {
    button.disabled = false;
    button.textContent = "삭제";
    showToast("삭제하지 못했습니다", error.message, "error");
  }
}

/** 생성한 이미지를 영상 스튜디오의 첫 프레임으로 넘긴다. */
function sendImageToStudio(image) {
  // 콘티에서 넘어온 이미지는 이미 S3에 있다. 업로드 묶음에 그대로 넣어 갤러리에 보이게 한다.
  // 예전에는 별도 미리보기 영역에 그렸는데, 그 영역이 갤러리로 바뀌면서 사라졌다.
  resetRefSet("studio");
  state.studioAssetKey = "";
  const set = refState("studio");
  set.items.push({
    key: image.key,
    name: image.filename || "콘티 이미지",
    size: image.size || 0,
    url: image.url,
    localUrl: null
  });
  set.primaryKey = image.key;
  setFrameMode("upload");
  renderRefGallery("studio");
  navigate("studio");
  showToast("스튜디오로 이동", "이 이미지를 첫 프레임으로 영상을 만듭니다.");
}

/* ------------------------------ 내 사용량 (본인만) ------------------------------
  관리자 사용량 화면을 권한만 풀어서 쓰지 않는다. 그 응답에는 byUser(전체 사용자 목록)와
  모델 구성이 들어 있어서, 한도가 걸린 계정이 남의 사용량까지 보게 된다.
  /api/usage/me 는 사용자 이름을 요청에서 받지 않고 토큰 주체로 고정한다.
*/
async function loadMyUsage() {
  try {
    const payload = await api("/api/usage/me?days=90");
    state.myUsage = payload;
    renderMyUsage();
  } catch (error) {
    showToast("사용량을 불러오지 못했습니다", error.message, "error");
  }
}

function renderMyUsage() {
  const payload = state.myUsage;
  if (!payload) return;

  // 남은 횟수. 한도가 없는 계정에는 "무제한"으로 적는다. 0 과 무제한을 섞으면
  // 멀쩡한 계정이 잠긴 것처럼 보인다.
  const cards = $("#myQuotaCards");
  if (cards) {
    const quotas = payload.quotas;
    if (!quotas || !quotas.limited) {
      cards.innerHTML = usageCard("생성 한도", 0, "제한 없음");
    } else {
      cards.innerHTML = QUOTA_FIELDS.map((field) => {
        const entry = quotas.categories?.[field.id];
        if (!entry || entry.limit === null || entry.limit === undefined) {
          return usageCard(`${field.label} 남은 횟수`, 0, "제한 없음");
        }
        return usageCard(
          `${field.label} 남은 횟수`,
          entry.remaining ?? 0,
          `전체 ${entry.limit}건 중 ${entry.used}건 사용`
        );
      }).join("");
    }
  }

  const body = $("#myCreditBody");
  if (body) {
    const actions = payload.actions || [];
    body.innerHTML = "";
    if (!actions.length) {
      const row = document.createElement("tr");
      row.innerHTML = '<td colspan="3">아직 만든 결과물이 없습니다.</td>';
      body.appendChild(row);
    }
    actions.forEach((item) => {
      const row = document.createElement("tr");
      const label = QUOTA_FIELDS.find((field) => field.id === item.category)?.label || item.category;
      row.innerHTML = `
        <td>${escapeHtml(label)}</td>
        <td><small>${escapeHtml(item.action || "-")}</small></td>
        <td>${formatDate(item.at)}</td>`;
      body.appendChild(row);
    });
  }

  const totals = payload.totals || {};
  const totalsBox = $("#myUsageTotals");
  if (totalsBox) {
    totalsBox.innerHTML = [
      usageCard("모델 호출", totals.calls || 0, "건"),
      usageCard("입력 토큰", totals.inputTokens || 0, "토큰"),
      usageCard("출력 토큰", totals.outputTokens || 0, "토큰"),
      usageCard("실패", totals.failed || 0, "건")
    ].join("");
  }
  const note = $("#myUsageNote");
  if (note) {
    note.textContent = `최근 ${payload.days || 90}일 기준 · 기록 ${payload.rowCount || 0}건`;
  }
}

/* ------------------------------ 프롬프트 관리 (admin) ------------------------------
  지시서 5~8. 두 계층을 한 화면에서 보여준다.

  백엔드 프롬프트에는 **편집 요소를 만들지 않는다.** textarea 를 disabled 로 두는 방식은
  쓰지 않았다. 나중에 누가 속성만 떼면 저장 경로가 열리고, 화면상으로도 "바꿀 수 있는데
  지금 막혀 있다"로 읽힌다. 실제로는 배포로만 바뀌는 값이다.
*/
const PROMPT_KIND_LABELS = { text: "텍스트", image: "이미지", video: "영상" };

async function loadPrompts() {
  if (!state.user?.isAdmin) return;
  const list = $("#promptList");
  if (list) list.innerHTML = '<p class="field-hint">불러오고 있습니다...</p>';
  try {
    const payload = await api("/api/admin/prompts");
    state.prompts = payload.items || [];
    state.promptMaxChars = payload.maxFrontendChars || 2000;
    renderPrompts(payload.missingBackend || []);
  } catch (error) {
    if (list) list.innerHTML = "";
    showToast("프롬프트를 불러오지 못했습니다", error.message, "error");
  }
}

function renderPrompts(missing) {
  const list = $("#promptList");
  if (!list) return;

  // 지시서 8-2 는 백엔드 프롬프트가 누락된 모델이 0개여야 한다고 요구한다.
  // 0개가 아니면 화면 맨 위에서 알린다. 표 안에 섞어 두면 눈에 띄지 않는다.
  const alertBox = $("#promptMissingAlert");
  if (alertBox) {
    alertBox.classList.toggle("is-hidden", !missing.length);
    if (missing.length) {
      alertBox.textContent =
        `백엔드 프롬프트가 없는 용도가 ${missing.length}개 있습니다: ${missing.join(", ")}`;
    }
  }

  list.innerHTML = "";
  // 용도가 스무 개가 넘어 종류로 묶는다. 묶지 않으면 찾는 항목까지 스크롤이 길다.
  const groups = ["text", "image", "video"];
  groups.forEach((kind) => {
    const rows = state.prompts.filter((row) => row.kind === kind);
    if (!rows.length) return;
    const heading = document.createElement("h3");
    heading.className = "prompt-group";
    heading.textContent = `${PROMPT_KIND_LABELS[kind] || kind} (${rows.length})`;
    list.appendChild(heading);
    rows.forEach((row) => list.appendChild(promptCard(row)));
  });
}

function promptCard(row) {
  const card = document.createElement("article");
  card.className = "prompt-card";

  const editable = row.frontendEditable !== false;
  const backendBlocks = (row.backend || [])
    .map(
      (block) => `
        <details class="prompt-backend">
          <summary>${escapeHtml(block.source)}<small>${escapeHtml(block.note || "")}</small></summary>
          <pre>${escapeHtml(block.text || "")}</pre>
        </details>`
    )
    .join("");

  card.innerHTML = `
    <div class="prompt-card-head">
      <div>
        <strong>${escapeHtml(row.label)}</strong>
        <small>${escapeHtml(row.usage)}</small>
      </div>
      <div class="prompt-meta">
        <code>${escapeHtml(row.role)}</code>
        <span class="source-tag">${escapeHtml(row.modelId)}</span>
      </div>
    </div>
    <p class="prompt-section-label">백엔드 프롬프트 · 시스템 고정 (배포로만 변경)</p>
    ${backendBlocks || '<p class="field-hint">참조가 없습니다.</p>'}
    <div class="prompt-frontend"></div>`;

  const box = card.querySelector(".prompt-frontend");
  if (!editable) {
    // 지시서 6-2. 이 용도는 백엔드 고정만 있고 화면에서 바꿀 것이 없다.
    box.innerHTML = `
      <p class="prompt-section-label">프론트엔드 프롬프트</p>
      <p class="field-hint">${escapeHtml(row.frontendHint || "이 용도는 배포로만 변경됩니다.")}</p>`;
    return card;
  }

  box.innerHTML = `
    <p class="prompt-section-label">프론트엔드 프롬프트 · 여기서 수정</p>
    <p class="field-hint">${escapeHtml(row.frontendHint || "")}</p>
    <textarea rows="3" maxlength="${state.promptMaxChars || 2000}" placeholder="비워 두면 적용하지 않습니다."></textarea>
    <div class="prompt-actions">
      <small class="prompt-updated"></small>
      <span class="prompt-spacer"></span>
      <button type="button" class="text-action prompt-history-btn">이력</button>
      <button type="button" class="primary-button prompt-save">저장</button>
    </div>`;

  const textarea = box.querySelector("textarea");
  textarea.value = row.frontend || "";
  const updated = box.querySelector(".prompt-updated");
  updated.textContent = row.updatedAt
    ? `${formatDate(row.updatedAt)} · ${row.updatedBy || "-"}`
    : "아직 설정하지 않았습니다.";

  box.querySelector(".prompt-save").addEventListener("click", (event) =>
    savePrompt(row.role, textarea.value, event.currentTarget, updated)
  );
  box.querySelector(".prompt-history-btn").addEventListener("click", () =>
    openPromptHistory(row.role, row.label)
  );
  return card;
}

async function savePrompt(role, text, button, updatedLabel) {
  button.disabled = true;
  try {
    const payload = await api(`/api/admin/prompts/${encodeURIComponent(role)}`, {
      method: "PUT",
      body: JSON.stringify({ text })
    });
    // 목록 상태도 갱신한다. 다시 불러오지 않으면 새로고침 전까지 옛 값이 남는다.
    const target = state.prompts.find((row) => row.role === role);
    if (target) {
      target.frontend = payload.frontend || "";
      target.updatedAt = payload.updatedAt || "";
      target.updatedBy = payload.updatedBy || "";
    }
    if (updatedLabel) {
      updatedLabel.textContent = payload.updatedAt
        ? `${formatDate(payload.updatedAt)} · ${payload.updatedBy || "-"}`
        : "아직 설정하지 않았습니다.";
    }
    showToast(
      "프롬프트를 저장했습니다",
      payload.frontend ? `${role} 에 적용했습니다.` : `${role} 의 설정을 지웠습니다.`
    );
  } catch (error) {
    showToast("저장하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function openPromptHistory(role, label) {
  const modal = $("#promptHistoryModal");
  const body = $("#promptHistoryBody");
  if (!modal || !body) return;
  $("#promptHistoryRole").textContent = label || role;
  body.innerHTML = '<p class="field-hint">불러오고 있습니다...</p>';
  modal.classList.remove("is-hidden");
  document.body.classList.add("modal-open");
  try {
    const payload = await api(`/api/admin/prompts/${encodeURIComponent(role)}`);
    const rows = payload.history || [];
    if (!rows.length) {
      body.innerHTML = '<p class="field-hint">수정 이력이 없습니다.</p>';
      return;
    }
    body.innerHTML = rows
      .map(
        (item) => `
        <div class="prompt-history-item">
          <div class="prompt-history-head">
            <strong>${escapeHtml(formatDate(item.savedAt || item.updatedAt))}</strong>
            <span>${escapeHtml(item.updatedBy || "-")}</span>
          </div>
          <pre>${escapeHtml(item.text || "(비어 있음)")}</pre>
        </div>`
      )
      .join("");
  } catch (error) {
    body.innerHTML = `<p class="field-hint">${escapeHtml(error.message)}</p>`;
  }
}

async function openPromptMapping() {
  const modal = $("#promptMappingModal");
  const body = $("#promptMappingBody");
  if (!modal || !body) return;
  body.innerHTML = "";
  modal.classList.remove("is-hidden");
  document.body.classList.add("modal-open");
  try {
    const payload = await api("/api/admin/prompts/mapping");
    state.promptMapping = payload.items || [];
    $("#promptMappingSummary").textContent = payload.complete
      ? `용도 ${state.promptMapping.length}개 모두 백엔드 프롬프트가 있습니다. 누락 0건.`
      : `백엔드 프롬프트 누락 ${(payload.missingBackend || []).length}건: ${(payload.missingBackend || []).join(", ")}`;
    body.innerHTML = state.promptMapping
      .map(
        (row) => `
        <tr>
          <td><code>${escapeHtml(row.modelId)}</code><small>${escapeHtml(row.modelEnv)}</small></td>
          <td>${escapeHtml(row.usage)}</td>
          <td>${row.hasBackendPrompt
            ? `<span class="state-on">있음</span><small>${escapeHtml((row.backendSources || []).join(", "))}</small>`
            : '<span class="state-off">없음</span>'}</td>
          <td>${row.frontendSet ? '<span class="state-default">설정됨</span>' : '<small>(빈 값)</small>'}</td>
          <td>${row.frontendEditable ? "가능" : "배포로만"}</td>
        </tr>`
      )
      .join("");
  } catch (error) {
    body.innerHTML = `<tr><td colspan="5">${escapeHtml(error.message)}</td></tr>`;
  }
}

function closePromptModals() {
  $("#promptMappingModal")?.classList.add("is-hidden");
  $("#promptHistoryModal")?.classList.add("is-hidden");
  document.body.classList.remove("modal-open");
}

function copyPromptMapping() {
  // 지시서 8-2 는 표를 보고서에 넣으라고 요구한다. 화면에서 긁어 옮기면 줄이 깨진다.
  const rows = state.promptMapping || [];
  const lines = [
    "| 모델 | 용도 | 백엔드 프롬프트 | 프론트엔드 기본값 | 수정 가능 |",
    "| --- | --- | --- | --- | --- |",
    ...rows.map((row) =>
      `| ${row.modelId} | ${row.usage} | ${row.hasBackendPrompt ? (row.backendSources || []).join(", ") : "없음"} `
      + `| ${row.frontendSet ? "설정됨" : "(빈 값)"} | ${row.frontendEditable ? "가능" : "배포로만"} |`
    )
  ];
  const text = lines.join("\n");
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(
      () => showToast("표를 복사했습니다", `${rows.length}행을 클립보드에 담았습니다.`),
      () => showToast("복사하지 못했습니다", "브라우저가 클립보드를 막았습니다.", "error")
    );
    return;
  }
  showToast("복사하지 못했습니다", "이 브라우저는 클립보드를 지원하지 않습니다.", "error");
}

/* ------------------------------ 사용량 (admin) ------------------------------ */
async function loadUsage() {
  if (!state.user?.isAdmin) return;
  const days = state.usageDays || 30;
  $$("#usageRangeOptions button").forEach((b) => b.classList.toggle("active", Number(b.dataset.value) === days));
  try {
    const payload = await api(`/api/admin/usage?days=${days}`);
    renderUsage(payload);
  } catch (error) {
    showToast("사용량을 불러오지 못했습니다", error.message, "error");
  }
}

/* 영상 비용·토큰 (설계자 2026-10-07). 영상 만들기 화면에는 금액을 보이지 않고 여기서만 본다.
   GET /api/admin/video-costs 는 vpoc1 · vpoc2 원장을 합친 누적값이다. */
const VIDEO_COST_KIND = { video: "영상", image: "이미지", text: "텍스트", preflight: "사전 점검" };

async function loadVideoCosts() {
  if (!state.user?.isAdmin || !$("#videoCostTotals")) return;
  try {
    renderVideoCosts(await api("/api/admin/video-costs"));
  } catch (error) {
    showToast("영상 비용을 불러오지 못했습니다", error.message, "error");
  }
}

function renderVideoCosts(payload) {
  const totals = payload.totals || {};
  const money = (value) => Number(value || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const kindUsd = (kind) => ((payload.byKind || []).find((row) => row.kind === kind) || {}).usd || 0;
  $("#videoCostTotals").innerHTML = [
    ["총 비용", money(totals.usd), "USD"],
    ["영상", money(kindUsd("video")), "USD"],
    ["이미지", money(kindUsd("image")), "USD"],
    ["텍스트", money(kindUsd("text")), "USD"],
    ["입력 토큰", (totals.inputTokens || 0).toLocaleString(), "tokens"],
    ["출력 토큰", (totals.outputTokens || 0).toLocaleString(), "tokens"]
  ].map(([label, value, unit]) =>
    `<div class="usage-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(unit)}</small></div>`
  ).join("");

  const body = $("#videoCostModelBody");
  body.innerHTML = "";
  (payload.byModel || []).forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHtml(row.model || "-")}</strong></td>
      <td>${escapeHtml(VIDEO_COST_KIND[row.kind] || row.kind)}</td>
      <td>${(row.calls || 0).toLocaleString()}</td>
      <td>${(row.inputTokens || 0).toLocaleString()}</td>
      <td>${(row.outputTokens || 0).toLocaleString()}</td>
      <td>${money(row.usd)}</td>`;
    body.appendChild(tr);
  });
  if (!(payload.byModel || []).length) {
    body.innerHTML = '<tr><td colspan="6" class="usage-empty">기록이 없습니다.</td></tr>';
  }
  $("#videoCostNote").textContent =
    `토큰은 ${payload.tokensSince || "2026-10-07"} 이후 호출부터 기록됩니다. 그 전 항목은 금액만 있습니다. ` +
    "Seedance 금액은 콘솔 견적에서 계산한 추정 단가입니다.";
}

function usageCard(label, value, unit) {
  return `<div class="usage-card"><span>${escapeHtml(label)}</span><strong>${value.toLocaleString()}</strong><small>${escapeHtml(unit)}</small></div>`;
}

function renderUsage(payload) {
  const totals = payload.totals || {};
  $("#usageTotals").innerHTML = [
    usageCard("총 호출", totals.calls || 0, "건"),
    usageCard("입력 토큰", totals.inputTokens || 0, "tokens"),
    usageCard("출력 토큰", totals.outputTokens || 0, "tokens"),
    usageCard("영상 생성", Math.round(totals.videoSeconds || 0), "초"),
    usageCard("이미지 생성", Math.round(totals.images || 0), "장"),
    usageCard("실패", totals.failed || 0, "건")
  ].join("");

  const userBody = $("#usageUserBody");
  userBody.innerHTML = "";
  (payload.byUser || []).forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${escapeHtml(row.username)}</strong></td>
      <td>${(row.calls || 0).toLocaleString()}</td>
      <td>${(row.inputTokens || 0).toLocaleString()}</td>
      <td>${(row.outputTokens || 0).toLocaleString()}</td>
      <td>${Math.round(row.videoSeconds || 0).toLocaleString()}</td>
      <td>${Math.round(row.images || 0).toLocaleString()}</td>
      <td>${(row.failed || 0).toLocaleString()}</td>
      <td>${formatDate(row.lastUsedAt)}</td>`;
    userBody.appendChild(tr);
  });
  if (!(payload.byUser || []).length) {
    userBody.innerHTML = '<tr><td colspan="8" class="usage-empty">기간 내 사용 기록이 없습니다.</td></tr>';
  }

  const modelBody = $("#usageModelBody");
  modelBody.innerHTML = "";
  (payload.byModel || []).forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${escapeHtml(row.modelId)}</code></td>
      <td>${escapeHtml(row.kind || "-")}</td>
      <td>${(row.calls || 0).toLocaleString()}</td>
      <td>${(row.inputTokens || 0).toLocaleString()}</td>
      <td>${(row.outputTokens || 0).toLocaleString()}</td>
      <td>${Math.round(row.videoSeconds || 0).toLocaleString()}</td>
      <td>${Math.round(row.images || 0).toLocaleString()}</td>
      <td>${(row.failed || 0).toLocaleString()}</td>`;
    modelBody.appendChild(tr);
  });
  if (!(payload.byModel || []).length) {
    modelBody.innerHTML = '<tr><td colspan="8" class="usage-empty">기간 내 사용 기록이 없습니다.</td></tr>';
  }

  $("#usageNote").textContent =
    `최근 ${payload.days}일 · 기록 ${(payload.rowCount || 0).toLocaleString()}건 기준. ` +
    "영상은 요청한 길이(초), 이미지는 생성된 장수로 집계합니다.";
}

function setStatus(kind, label) {
  const badge = $("#previewStatus");
  badge.className = `status-badge ${kind}`;
  badge.innerHTML = `<i></i> ${escapeHtml(label)}`;
}

function setProgress(percent, label) {
  // 상단 배너도 같은 자리에서 갱신한다(setWanProgress 와 같은 이유).
  updateRun("studio", percent, label);
  const wrap = $("#jobProgress");
  wrap.classList.remove("is-hidden");
  $("#jobBar").style.width = `${Math.max(0, Math.min(100, percent))}%`;
  $("#jobLabel").textContent = label;
}

function hideProgress() {
  $("#jobProgress").classList.add("is-hidden");
}

async function startGeneration() {
  const model = activeModel();
  const prompt = $("#promptInput").value.trim();

  if (!model) {
    showToast("모델이 없습니다", "관리자가 생성 모델을 등록해야 합니다.", "error");
    return;
  }
  if (!prompt) {
    showToast("디렉션을 입력해 주세요", "원하는 움직임을 한 문장 이상 작성해 주세요.", "error");
    $("#promptInput").focus();
    return;
  }

  const button = $("#generateButton");
  button.disabled = true;
  setStatus("working", "준비 중");
  startRun("studio", "이미지를 저장하고 있습니다...");
  setProgress(10, "이미지를 저장하고 있습니다...");

  try {
    // 참고 이미지는 올릴 때 이미 S3에 저장했다. 콘티 화면에서 넘어온 경우에는
    // 서버에 있는 이미지 키를 그대로 쓴다(state.studioAssetKey).
    const refs = state.frameMode === "upload" ? refPayload("studio") : {};
    const handoffKey = state.frameMode === "upload" ? null : state.studioAssetKey || null;
    const hasImage = Boolean(refs.assetKeys?.length || handoffKey);

    setProgress(25, hasImage ? "AI가 이미지를 보고 프롬프트를 쓰는 중입니다..." : "모델을 호출하고 있습니다...");
    const payload = await api("/api/videos/generate", {
      method: "POST",
      body: JSON.stringify({
        ...refs,
        // 참고 이미지가 없으면 키를 아예 보내지 않는다. null을 보내면 서버가 문자열로 받는다.
        assetKey: handoffKey || undefined,
        prompt,
        durationSeconds: Number(state.duration),
        resolution: state.resolution,
        aspectRatio: state.aspectRatio,
        stylePrompt: $("#videoStyle").value.trim(),
        presetId: state.videoPreset || undefined,
        modelId: model.id
      })
    });

    showSentPrompt(payload.job);
    showVideoStyle(payload.job.style);
    applyJobSettings(payload.job);
    setStatus("working", "생성 중");
    setProgress(40, `${model.label} 생성 중입니다. 1~3분 정도 걸립니다.`);
    pollJob(payload.job.invocationArn, 40);
  } catch (error) {
    setStatus("idle", "실패");
    hideProgress();
    endRun("studio", false, error.message);
    showToast("영상 생성을 시작하지 못했습니다", error.message, "error");
    button.disabled = false;
  }
}

function pollJob(invocationArn, progress) {
  window.clearTimeout(state.pollTimer);
  state.pollTimer = window.setTimeout(async () => {
    try {
      const payload = await api(`/api/videos/jobs?arn=${encodeURIComponent(invocationArn)}`);
      const job = payload.job;

      if (job.status === "InProgress") {
        const next = Math.min(90, progress + 5);
        setProgress(next, "모델이 영상을 만들고 있습니다...");
        pollJob(invocationArn, next);
        return;
      }

      $("#generateButton").disabled = false;

      if (job.status === "Completed") {
        setProgress(100, "완료");
        setStatus("success", "완료");
        if (job.videoUrl) {
          const video = $("#resultVideo");
          video.src = job.videoUrl;
          video.classList.remove("is-hidden");
          $("#emptyOutput").classList.add("is-hidden");
          endRun("studio", true, "영상이 완성되었습니다. 라이브러리에도 저장했습니다.");
          showToast("영상 생성 완료", "라이브러리에 저장했습니다.");
          loadVideos();
        } else {
          const note = job.collectError || "영상 파일을 아직 찾지 못했습니다. 잠시 후 라이브러리를 확인해 주세요.";
          endRun("studio", false, note);
          showToast("생성 완료", note, "error");
        }
        window.setTimeout(hideProgress, 1500);
        return;
      }

      setStatus("idle", "실패");
      hideProgress();
      /*
        서버가 원인을 분류한 한국어 문구(failureDetail)를 우선 쓴다.

        예전에는 Bedrock 원문을 그대로 띄웠다. 사용자가 본 것은 "image moderation failed"
        한 줄이었고, 무엇이 문제인지도 다음에 무엇을 할지도 알 수 없었다.
        원문은 진단에 필요하므로 배너에 함께 남긴다(같은 값이면 한 번만).
      */
      const detail = job.failureDetail || job.failureMessage || `상태: ${job.status}`;
      const raw = job.failureMessage && job.failureMessage !== detail
        ? `${detail} (원문: ${job.failureMessage})`
        : detail;
      endRun("studio", false, raw);
      showToast("영상 생성 실패", detail, "error");
    } catch (error) {
      $("#generateButton").disabled = false;
      setStatus("idle", "실패");
      hideProgress();
      endRun("studio", false, error.message);
      showToast("작업 상태를 확인하지 못했습니다", error.message, "error");
    }
  }, 6000);
}

function showSentPrompt(job) {
  const block = $("#sentPromptBlock");
  if (!job?.prompt) {
    block.classList.add("is-hidden");
    return;
  }
  $("#sentPromptText").textContent = job.prompt;
  // 어떤 전략으로 프롬프트를 썼는지 알려준다. 결과가 기대와 다를 때 첫 단서가 된다.
  const note = job.usedImage
    ? "참고 이미지를 첫 프레임으로 사용했습니다. 이 경우 프롬프트는 인물 묘사를 다시 하지 않고 무엇이 움직이는지만 적습니다. 다시 묘사하면 이미지와 충돌해 인물이 뒤틀립니다."
    : "이미지 없이 텍스트만으로 생성했습니다. 숏 크기, 인물, 동작, 장소, 카메라, 조명을 프롬프트가 모두 담습니다.";
  $("#sentPromptNote").textContent = job.cameraMove
    ? `${note} 카메라: ${job.cameraMove}.`
    : note;
  block.classList.remove("is-hidden");
}

/* ---- 첫 프레임 모드: 텍스트만 / 업로드 / AI 이미지 ---- */
function setFrameMode(mode) {
  state.frameMode = mode;
  $$("#frameModeOptions button").forEach((b) => b.classList.toggle("active", b.dataset.value === mode));
  $("#frameUploadBlock").classList.toggle("is-hidden", mode !== "upload");
  $("#frameAiBlock").classList.toggle("is-hidden", mode !== "ai");
  // 모드를 바꾸면 이전 모드에서 고른 이미지는 쓰지 않는다. 의도와 다른 이미지가 들어가는 것을 막는다.
  if (mode !== "upload") {
    resetRefSet("studio");
  }
  if (mode !== "ai") {
    state.frameAiKey = "";
    $("#frameAiResult").classList.add("is-hidden");
  }
  if (mode === "none") state.studioAssetKey = "";
}

/** 스튜디오에서 쓸 첫 프레임 이미지를 AI로 만든다. */
async function generateFrameImage() {
  const scene = $("#frameAiPrompt").value.trim();
  if (scene.length < 5) {
    showToast("설명이 필요합니다", "만들 이미지를 5자 이상 설명해 주세요.", "error");
    $("#frameAiPrompt").focus();
    return;
  }
  const button = $("#frameAiGenerate");
  button.disabled = true;
  $("#frameAiStatus").textContent = "이미지를 만들고 있습니다...";
  try {
    const payload = await api("/api/images/generate", {
      method: "POST",
      body: JSON.stringify({
        prompt: scene,
        stylePrompt: $("#videoStyle").value.trim(),
        presetId: state.videoPreset || undefined,
        purpose: "videoFrame",
        aspectRatio: state.aspectRatio
      })
    });
    state.frameAiKey = payload.item.key;
    state.studioAssetKey = payload.item.key;
    $("#frameAiImage").src = payload.item.url;
    $("#frameAiLabel").textContent = payload.style.presetLabel || "생성 완료";
    $("#frameAiMeta").textContent = `${payload.model.label}${payload.style.reason ? " · " + payload.style.reason : ""}`;
    $("#frameAiResult").classList.remove("is-hidden");
    $("#frameAiStatus").textContent = "이 이미지를 첫 프레임으로 사용합니다.";
    showToast("이미지 생성 완료", "영상의 첫 프레임으로 사용합니다.");
  } catch (error) {
    $("#frameAiStatus").textContent = "";
    showToast("이미지를 만들지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

/* ---- 롱테이크 ---- */
async function loadLongTakeOptions() {
  try {
    const payload = await api("/api/videos/long-take/options");
    state.longOptions = payload.options || [];
    state.longConcurrency = payload.concurrency || 1;
    state.longQuotaNote = payload.quotaNote || "";
    if (!state.longTarget && state.longOptions.length) {
      state.longTarget = state.longOptions[1]?.targetSeconds || state.longOptions[0].targetSeconds;
    }
    renderLongTakeOptions();
  } catch (error) {
    // 옵션을 못 받아도 단일 컷 생성은 되어야 한다.
    console.warn("롱테이크 옵션을 불러오지 못했습니다", error);
  }
}

function renderLongTakeOptions() {
  const container = $("#longTargetOptions");
  if (!container) return;
  container.innerHTML = "";
  state.longOptions.forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.value = String(option.targetSeconds);
    button.className = state.longTarget === option.targetSeconds ? "active" : "";
    button.textContent = option.label;
    button.addEventListener("click", () => {
      state.longTarget = option.targetSeconds;
      renderLongTakeOptions();
    });
    container.appendChild(button);
  });
  const picked = state.longOptions.find((option) => option.targetSeconds === state.longTarget);
  if (picked) {
    const hours = Math.floor(picked.estimateMinutes / 60);
    const mins = picked.estimateMinutes % 60;
    const spent = hours > 0 ? `${hours}시간 ${mins}분` : `${mins}분`;
    $("#longEstimate").textContent =
      `${picked.label} 영상은 ${picked.shotSeconds}초 컷 ${picked.shotCount}개로 만듭니다. ` +
      `예상 ${spent} 걸립니다. ${state.longQuotaNote || ""} 화면을 벗어나도 계속 진행됩니다.`;
  }
}

async function startLongTake() {
  const prompt = $("#promptInput").value.trim();
  if (prompt.length < 10) {
    showToast("장면 설명이 필요합니다", "롱테이크는 장면 설명을 10자 이상 적어주세요.", "error");
    $("#promptInput").focus();
    return;
  }
  const picked = state.longOptions.find((option) => option.targetSeconds === state.longTarget);
  const estimate = picked?.estimateMinutes || 0;
  const spent = estimate >= 60 ? `${Math.floor(estimate / 60)}시간 ${estimate % 60}분` : `${estimate}분`;
  // 롱테이크는 컷마다 모델을 호출한다. 되돌릴 수 없는 삭제는 아니지만
  // 실제 비용이 발생하므로 같은 확인 절차를 거친다.
  if (!await confirmDialog({
    title: "롱테이크 생성",
    lead: `컷 ${picked?.shotCount || "?"}개를 순서대로 만듭니다.`,
    body: `예상 ${spent}이 걸리고, 컷마다 생성 비용이 발생합니다. `
      + "시작하면 중간에 멈춰도 이미 만든 컷의 비용은 청구됩니다. 화면을 벗어나도 계속 진행됩니다.",
    target: picked?.label || "",
    okLabel: "생성 시작",
    tone: "warn"
  })) return;

  const button = $("#longTakeButton");
  button.disabled = true;
  startRun("studio", "컷을 나누고 순서대로 만듭니다. 화면을 벗어나도 계속 진행됩니다.");
  try {
    // 롱테이크는 첫 컷에만 참고 이미지를 쓴다. 여러 장을 올렸으면 첫 프레임으로 고른 것만 보낸다.
    const refs = state.frameMode === "upload" ? refPayload("studio") : {};
    const assetKey = refs.primaryKey || refs.assetKeys?.[0] || state.studioAssetKey || null;
    const payload = await api("/api/videos/long-take", {
      method: "POST",
      body: JSON.stringify({
        prompt,
        targetSeconds: state.longTarget,
        stylePrompt: $("#videoStyle").value.trim(),
        assetKey: assetKey || undefined
      })
    });
    state.sequenceId = payload.item.id;
    renderSequence(payload.item);
    pollSequence();
    showToast("롱테이크 시작", "컷을 나누고 순서대로 생성합니다.");
  } catch (error) {
    endRun("studio", false, error.message);
    showToast("롱테이크를 시작하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

const SEQ_STATUS_LABELS = {
  planning: "컷 나누는 중",
  rendering: "컷 생성 중",
  stitching: "이어 붙이는 중",
  ready: "완료",
  failed: "실패"
};

function renderSequence(sequence) {
  const panel = $("#sequencePanel");
  panel.classList.remove("is-hidden");
  $("#sequenceTitle").textContent = sequence.title || "롱테이크";
  $("#sequenceStatus").textContent = SEQ_STATUS_LABELS[sequence.status] || sequence.status;
  $("#sequenceStatus").className = `doc-chip ${sequence.status === "ready" ? "ready" : sequence.status === "failed" ? "failed" : "generating"}`;

  const total = sequence.shotCount || 1;
  const done = sequence.doneCount || 0;
  const percent = Math.min(100, Math.round((done / total) * 100));
  $("#sequenceBar").style.width = `${percent}%`;
  const progressText = `컷 ${done}/${total} 완료` + (sequence.failedCount ? ` · 실패 ${sequence.failedCount}개` : "");
  $("#sequenceProgress").textContent = progressText;
  if (!["ready", "failed"].includes(sequence.status)) {
    updateRun("studio", percent, `${SEQ_STATUS_LABELS[sequence.status] || sequence.status} · ${progressText}`);
  }

  const shots = $("#sequenceShots");
  shots.innerHTML = "";
  (sequence.shots || []).forEach((shot) => {
    const chip = document.createElement("span");
    chip.className = `shot-chip ${shot.status}`;
    chip.title = shot.summary || "";
    chip.textContent = shot.number;
    shots.appendChild(chip);
  });

  const errorBlock = $("#sequenceError");
  errorBlock.classList.toggle("is-hidden", !sequence.error);
  errorBlock.textContent = sequence.error || "";

  if (sequence.status === "ready" && sequence.videoUrl) {
    const video = $("#resultVideo");
    video.src = sequence.videoUrl;
    video.classList.remove("is-hidden");
    $("#emptyOutput").classList.add("is-hidden");
    setStatus("success", "완료");
    endRun("studio", true, `컷 ${sequence.doneCount || 0}개를 이어 붙였습니다.`);
  }
  if (sequence.status === "failed") {
    endRun("studio", false, sequence.error || "롱테이크를 만들지 못했습니다.");
  }
}

function pollSequence() {
  window.clearTimeout(state.sequenceTimer);
  if (!state.sequenceId) return;
  state.sequenceTimer = window.setTimeout(async () => {
    try {
      const payload = await api(`/api/videos/long-take/${encodeURIComponent(state.sequenceId)}`);
      renderSequence(payload.item);
      if (["ready", "failed"].includes(payload.item.status)) {
        if (payload.item.status === "ready") {
          showToast("롱테이크 완료", "영상을 라이브러리에 저장했습니다.");
          loadVideos();
        } else {
          showToast("롱테이크 실패", payload.item.error || "다시 시도해 주세요.", "error");
        }
        state.sequenceId = null;
        return;
      }
      pollSequence();
    } catch (error) {
      showToast("진행 상황을 확인하지 못했습니다", error.message, "error");
    }
  }, 15000);
}

function showVideoStyle(style) {
  const note = $("#sentPromptNote");
  if (style?.presetLabel) {
    note.textContent += ` 그림체: ${style.presetLabel} (${STYLE_SOURCE_LABELS[style.source] || style.source}).`;
  }
}

function applyJobSettings(job) {
  // 이미지를 넣으면 비율이 이미지에 맞춰 조정된다. 실제 적용값을 화면에 반영한다.
  if (job.aspectRatio) $("#selectedAspect").textContent = job.aspectRatio;
  if (job.resolution) $("#selectedResolution").textContent = job.resolution;
  if (job.duration) $("#selectedDuration").textContent = String(job.duration).replace("s", "");
}

function renderVideos() {
  const grid = $("#videoGrid");
  grid.innerHTML = "";
  $("#emptyLibrary").classList.toggle("is-hidden", state.videos.length > 0);
  grid.classList.toggle("is-hidden", state.videos.length === 0);
  state.videos.forEach((video) => {
    const card = document.createElement("article");
    card.className = "video-card";
    const url = escapeHtml(video.url);
    card.innerHTML = `
      <div class="video-thumb"><video controls preload="metadata" src="${url}"></video></div>
      <div class="video-card-info">
        <strong title="${escapeHtml(video.filename)}">${escapeHtml(video.filename)}</strong>
        <p>${formatDate(video.lastModified)} · ${formatBytes(video.size)}</p>
        <div class="video-actions">
          <a class="primary" href="${url}" download>다운로드</a>
        </div>
      </div>`;

    const actions = card.querySelector(".video-actions");

    const openLink = document.createElement("a");
    openLink.href = video.url;
    openLink.target = "_blank";
    openLink.rel = "noopener";
    openLink.textContent = "새 창에서 열기";

    actions.appendChild(overflowMenu([
      { element: openLink },
      { label: "영상 삭제", danger: true, onSelect: () => deleteVideo(video, actions.querySelector("a")) }
    ], `${video.filename} 추가 작업`));

    grid.appendChild(card);
  });
}

async function deleteVideo(video, button) {
  if (!await confirmDialog({
    title: "영상 삭제",
    lead: "이 작업은 되돌릴 수 없습니다.",
    body: "저장소에서 파일이 지워집니다. 다시 만들려면 생성 비용이 다시 듭니다.",
    target: video.filename,
    okLabel: "영상 삭제"
  })) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "삭제 중...";
  try {
    await api(`/api/videos?key=${encodeURIComponent(video.key)}`, { method: "DELETE" });
    state.videos = state.videos.filter((item) => item.key !== video.key);
    renderVideos();
    $("#navVideoCount").textContent = state.videos.length;
    showToast("영상 삭제", "영상을 삭제했습니다.");
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    showToast("삭제하지 못했습니다", error.message, "error");
  }
}

/* ------------------- 참고 이미지 여러 장 + 분석 + 확인 질문 ------------------- */
/*
  두 영상 화면이 같은 기능을 쓰지만 **공간은 완전히 분리한다.**
    새 영상 만들기 = Bedrock(Luma Ray2), NEW_영상 제작 = 자체 GPU의 Wan 2.2
  분리하는 것은 상태·DOM·API 엔드포인트다. 아래 함수들은 그 세 가지를 config로 받아
  각자의 영역만 건드린다. 화면을 그리는 절차는 같으므로 코드를 두 벌로 두지 않는다.
  두 벌이면 한쪽만 고쳐져 조용히 갈라진다.
*/

/*
  한 번에 쓸 수 있는 참고 이미지 수.
  서버의 server.MAX_REFERENCE_IMAGES 와 image_brief.MAX_IMAGES 와 같아야 한다.
  20장인 이유는 Bedrock Converse 가 요청당 이미지 20장까지만 받기 때문이다.
  .build/check-reference-images.py 가 세 값이 어긋나지 않는지 확인한다.
*/
const MAX_REF_IMAGES = 20;

const REF_CONFIGS = {
  studio: {
    // 새 영상 만들기: Bedrock. 이미지가 없어도 텍스트만으로 생성할 수 있다.
    name: "studio",
    analyzePath: "/api/videos/analyze",
    gallery: "#studioGallery",
    actions: "#studioRefActions",
    analyzeButton: "#studioAnalyze",
    status: "#studioAnalyzeStatus",
    brief: "#studioBrief",
    summary: "#studioBriefSummary",
    primaryNote: "#studioBriefPrimary",
    questions: "#studioQuestions",
    questionNote: "#studioQuestionNote",
    clear: "#studioBriefClear",
    textSource: "#promptInput",
    imageRequired: false
  },
  wan: {
    // NEW_영상 제작: Wan 2.2 I2V. 첫 프레임 이미지가 반드시 필요하다.
    name: "wan",
    analyzePath: "/api/v2/video/analyze",
    gallery: "#wanGallery",
    actions: "#wanRefActions",
    analyzeButton: "#wanAnalyze",
    status: "#wanAnalyzeStatus",
    brief: "#wanBrief",
    summary: "#wanBriefSummary",
    primaryNote: "#wanBriefPrimary",
    questions: "#wanQuestions",
    questionNote: "#wanQuestionNote",
    clear: "#wanBriefClear",
    textSource: "#wanText",
    imageRequired: true
  }
};

const REF_ROLE_LABELS = {
  scene: "원본 장면",
  characterSheet: "캐릭터 시트",
  concept: "컨셉 아트",
  prop: "소품 · 배경",
  unknown: "판단 어려움"
};

function refState(name) {
  return state.refSets[name];
}

/** 올린 이미지를 즉시 서버에 저장한다. 분석과 생성이 모두 S3 키를 쓰기 때문이다. */
async function addRefImages(name, fileList) {
  const config = REF_CONFIGS[name];
  const set = refState(name);
  const files = Array.from(fileList || []).filter((file) => {
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      showToast("지원하지 않는 형식", `${file.name}은 PNG, JPG, WEBP가 아닙니다.`, "error");
      return false;
    }
    if (file.size > 20 * 1024 * 1024) {
      showToast("파일이 너무 큽니다", `${file.name}은 20MB를 넘습니다.`, "error");
      return false;
    }
    return true;
  });
  if (!files.length) return;
  // 서버가 한 번에 읽는 이미지 수와 같은 상한이다(server.MAX_REFERENCE_IMAGES).
  // 20장인 이유는 Bedrock Converse 가 요청당 이미지 20장까지만 받기 때문이다.
  if (set.items.length + files.length > MAX_REF_IMAGES) {
    showToast(
      "이미지가 너무 많습니다",
      `한 번에 최대 ${MAX_REF_IMAGES}장까지 사용합니다.`,
      "error"
    );
    return;
  }

  set.busy = true;
  renderRefGallery(name);
  try {
    for (const file of files) {
      const body = new FormData();
      body.append("file", file);
      const uploaded = await api("/api/assets", { method: "POST", body });
      set.items.push({
        key: uploaded.item.key,
        // 서버가 S3 메타데이터에 보존한 원본 이름. S3 키에서는 한글이 사라진다.
        name: uploaded.item.originalName || file.name,
        size: uploaded.item.size,
        url: null,
        // 미리보기는 업로드한 파일로 바로 만든다. presign을 기다리지 않는다.
        localUrl: URL.createObjectURL(file)
      });
    }
    if (!set.primaryKey && set.items.length) set.primaryKey = set.items[0].key;
    // 이미지가 바뀌면 이전 분석은 더 이상 맞지 않는다.
    set.brief = null;
    set.answers = {};
  } catch (error) {
    showToast("이미지를 올리지 못했습니다", error.message, "error");
  } finally {
    set.busy = false;
    renderRefGallery(name);
    renderRefBrief(name);
  }
}

function removeRefImage(name, key) {
  const set = refState(name);
  set.items = set.items.filter((item) => item.key !== key);
  if (set.primaryKey === key) set.primaryKey = set.items[0]?.key || "";
  set.brief = null;
  set.answers = {};
  renderRefGallery(name);
  renderRefBrief(name);
}

function renderRefGallery(name) {
  const config = REF_CONFIGS[name];
  const set = refState(name);
  const gallery = $(config.gallery);
  const actions = $(config.actions);
  if (!gallery || !actions) return;

  gallery.classList.toggle("is-hidden", set.items.length === 0 && !set.busy);
  actions.classList.toggle("is-hidden", set.items.length === 0);
  gallery.innerHTML = "";

  set.items.forEach((item) => {
    const analyzed = (set.brief?.images || []).find((entry) => entry.key === item.key);
    const isPrimary = set.primaryKey === item.key;
    const card = document.createElement("figure");
    card.className = `ref-card${isPrimary ? " is-primary" : ""}`;
    card.innerHTML = `
      <div class="ref-thumb">
        <img src="${escapeHtml(item.localUrl || item.url || "")}" alt="${escapeHtml(item.name)}">
        ${isPrimary ? '<span class="ref-badge primary">첫 프레임</span>' : ""}
        ${analyzed ? `<span class="ref-badge role">${escapeHtml(analyzed.roleLabel || REF_ROLE_LABELS[analyzed.role] || "")}</span>` : ""}
      </div>
      <figcaption>
        <strong title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</strong>
        <small>${formatBytes(item.size)}</small>
        ${analyzed?.description ? `<p class="ref-desc">${escapeHtml(analyzed.description)}</p>` : ""}
        ${analyzed && analyzed.nameMatchesImage === false ? '<p class="ref-warn">파일 이름과 그림이 다르게 보입니다</p>' : ""}
      </figcaption>
      <div class="ref-card-actions"></div>`;

    const cardActions = card.querySelector(".ref-card-actions");
    if (!isPrimary) {
      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "text-action";
      pick.textContent = "첫 프레임으로";
      pick.addEventListener("click", () => {
        set.primaryKey = item.key;
        renderRefGallery(name);
        renderRefBrief(name);
      });
      cardActions.appendChild(pick);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "text-action danger";
    remove.textContent = "제거";
    remove.addEventListener("click", () => removeRefImage(name, item.key));
    cardActions.appendChild(remove);

    gallery.appendChild(card);
  });

  if (set.busy) {
    const pending = document.createElement("div");
    pending.className = "ref-card pending";
    pending.textContent = "올리는 중...";
    gallery.appendChild(pending);
  }

  // NEW_영상 제작의 변환 버튼은 이미지가 하나라도 있어야 눌릴 수 있다.
  // 갤러리가 바뀔 때 함께 갱신하지 않으면 이미지를 올린 뒤에도 버튼이 잠겨 있다.
  if (name === "wan") renderWanConvert();
}

/** 이미지들을 분석해 역할과 확인 질문을 받아온다. */
async function analyzeRefImages(name) {
  const config = REF_CONFIGS[name];
  const set = refState(name);
  if (!set.items.length) return;

  const button = $(config.analyzeButton);
  const status = $(config.status);
  button.disabled = true;
  status.textContent = "이미지를 읽고 있습니다...";
  try {
    const payload = await api(config.analyzePath, {
      method: "POST",
      body: JSON.stringify({
        assetKeys: set.items.map((item) => item.key),
        text: ($(config.textSource)?.value || "").trim()
      })
    });
    set.brief = payload.brief;
    set.answers = {};
    // 기획자가 이미 첫 프레임을 골랐다면 그 선택을 유지한다. AI 추천이 덮지 않는다.
    if (!set.primaryKey) set.primaryKey = payload.primaryKey;
    status.textContent = payload.brief.degraded
      ? "분석에 실패해 기본값으로 진행합니다."
      : `${payload.brief.questions.length}개 확인 사항이 있습니다.`;
    renderRefGallery(name);
    renderRefBrief(name);
  } catch (error) {
    status.textContent = "";
    showToast("이미지 분석에 실패했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function renderRefBrief(name) {
  const config = REF_CONFIGS[name];
  const set = refState(name);
  const block = $(config.brief);
  if (!block) return;

  if (!set.brief) {
    block.classList.add("is-hidden");
    return;
  }
  block.classList.remove("is-hidden");
  $(config.summary).textContent = set.brief.summary || "";

  const primary = set.items.find((item) => item.key === set.primaryKey);
  $(config.primaryNote).textContent = primary
    ? `첫 프레임: ${primary.name}${set.brief.primaryReason ? ` · ${set.brief.primaryReason}` : ""}`
    : "";

  const box = $(config.questions);
  box.innerHTML = "";
  (set.brief.questions || []).forEach((question) => {
    const row = document.createElement("div");
    row.className = "ref-question";
    row.innerHTML = `
      <div class="ref-question-head">
        <span class="doc-chip">${escapeHtml(question.topicLabel || "")}</span>
        <strong>${escapeHtml(question.question)}</strong>
      </div>
      ${question.why ? `<p class="ref-question-why">${escapeHtml(question.why)}</p>` : ""}
      <div class="ref-question-options"></div>
      <div class="ref-question-input"></div>`;

    const optionBox = row.querySelector(".ref-question-options");
    const inputBox = row.querySelector(".ref-question-input");

    const input = document.createElement("input");
    input.type = "text";
    input.className = "panel-input";
    input.maxLength = 500;
    input.placeholder = "답을 적거나, 아직 정해지지 않았다면 비워 두세요";
    input.value = set.answers[question.id] || "";
    input.addEventListener("input", () => {
      set.answers[question.id] = input.value;
      syncRefQuestionState(name, question.id, row);
    });
    inputBox.appendChild(input);

    (question.options || []).forEach((option) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "ref-option";
      chip.textContent = option;
      chip.addEventListener("click", () => {
        // 같은 선택지를 다시 누르면 해제한다. 잘못 고른 것을 되돌릴 수 있어야 한다.
        const next = set.answers[question.id] === option ? "" : option;
        set.answers[question.id] = next;
        input.value = next;
        renderRefBrief(name);
      });
      optionBox.appendChild(chip);
    });

    const skip = document.createElement("button");
    skip.type = "button";
    skip.className = "text-action ref-skip";
    skip.textContent = "건너뛰기";
    skip.addEventListener("click", () => {
      set.answers[question.id] = "";
      input.value = "";
      renderRefBrief(name);
    });
    row.querySelector(".ref-question-head").appendChild(skip);

    box.appendChild(row);
    syncRefQuestionState(name, question.id, row);
  });

  const total = (set.brief.questions || []).length;
  const answered = (set.brief.questions || []).filter(
    (question) => (set.answers[question.id] || "").trim()
  ).length;
  $(config.questionNote).textContent = total
    ? `${answered}/${total} 답변. 비워 둔 질문은 AI가 알아서 판단합니다. 지금 정하지 않아도 됩니다.`
    : "추가로 확인할 것이 없습니다.";
}

/** 답변한 질문의 선택지와 테두리를 표시한다. */
function syncRefQuestionState(name, questionId, row) {
  const set = refState(name);
  const value = (set.answers[questionId] || "").trim();
  row.classList.toggle("is-answered", Boolean(value));
  row.querySelectorAll(".ref-option").forEach((chip) => {
    chip.classList.toggle("active", chip.textContent === value);
  });
}

/** 생성 요청에 실을 참고 이미지 정보. 두 화면이 각자의 묶음만 보낸다. */
function refPayload(name) {
  const set = refState(name);
  if (!set.items.length) return {};
  return {
    assetKeys: set.items.map((item) => item.key),
    primaryKey: set.primaryKey || undefined,
    brief: set.brief || undefined,
    answers: (set.brief?.questions || [])
      .map((question) => ({
        id: question.id,
        question: question.question,
        answer: (set.answers[question.id] || "").trim()
      }))
      // 건너뛴 질문은 보내지 않는다. 서버도 빈 답을 버리지만 요청을 가볍게 둔다.
      .filter((item) => item.answer)
  };
}

function clearRefBrief(name) {
  const set = refState(name);
  set.brief = null;
  set.answers = {};
  $(REF_CONFIGS[name].status).textContent = "";
  renderRefGallery(name);
  renderRefBrief(name);
}

function resetRefSet(name) {
  const set = refState(name);
  set.items.forEach((item) => {
    if (item.localUrl) URL.revokeObjectURL(item.localUrl);
  });
  state.refSets[name] = { items: [], primaryKey: "", brief: null, answers: {}, busy: false };
  $(REF_CONFIGS[name].status).textContent = "";
  renderRefGallery(name);
  renderRefBrief(name);
}

/** 드롭존과 파일 입력을 배선한다. 두 화면이 각자의 요소로 한 번씩 부른다. */
function wireRefDropzone(name, dropzoneSelector, inputSelector) {
  const dropzone = $(dropzoneSelector);
  const input = $(inputSelector);
  if (!dropzone || !input) return;
  dropzone.addEventListener("click", () => input.click());
  dropzone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragging"));
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragging");
    addRefImages(name, event.dataTransfer?.files);
  });
  input.addEventListener("change", () => {
    addRefImages(name, input.files);
    input.value = "";
  });
  $(REF_CONFIGS[name].analyzeButton)?.addEventListener("click", () => analyzeRefImages(name));
  $(REF_CONFIGS[name].clear)?.addEventListener("click", () => clearRefBrief(name));
}

/* ---------------------- NEW_영상 제작 (자체 GPU / Wan 2.2) ---------------------- */
/*
  기존 스튜디오(Bedrock/Luma)와 함수를 공유하지 않는다. 지시서 1-2가 코드가 섞이지 않게
  하라고 정했고, 실제로 동작도 다르다.
    기존 = Bedrock이 job을 관리하고 6초마다 arn으로 상태를 묻는다
    NEW  = 앱이 job을 그래프에 기록하고 우리 서버를 폴링한다. 한 건에 15분 이상 걸린다.
*/

const WAN_STATUS_LABELS = {
  queued: "대기 중",
  running: "생성 중",
  uploading: "저장 중",
  ready: "완료",
  failed: "실패"
};

// 15분짜리 작업이라 폴링을 자주 할 이유가 없다. 서버 부하와 요금을 줄인다.
const WAN_POLL_MS = 15000;

async function loadWanOptions() {
  try {
    const payload = await api("/api/v2/video/options");
    state.wanResolutions = payload.resolutions || [];
    state.wanAspectNote = payload.aspectNote || "";
    state.wanDurations = payload.durations || [];
    state.wanPresets = payload.presets || [];
    state.wanEstimateMinutes = payload.estimateMinutes || {};
    // 서버가 주는 예상 시간은 특정 길이 기준값이다. 길이 선택지가 2~10초로 넓어져서
    // 기준을 모르면 10초를 5초 시간으로 안내한다. 아래 wanEstimateFor 가 환산한다.
    state.wanEstimateRefSeconds = Number(payload.estimateReferenceSeconds) || 5;
    // 변환 모드 목록도 서버가 준다. 화면에 박아 두면 모드를 추가할 때 두 곳을 고쳐야 하고
    // 권장 강도가 갈린다.
    state.wanConvert = payload.convert || null;
    if (state.wanConvert && !state.wanConvertMode) {
      state.wanConvertMode = state.wanConvert.defaultMode || "keep";
    }
    state.wanServer = payload.server || null;
    if (!state.wanResolution) state.wanResolution = payload.defaultResolution || "480p";
    if (!state.wanDuration) state.wanDuration = payload.defaultDuration || 3;
    renderWanOptions();
    renderWanServer();
  } catch (error) {
    /*
      실패해도 **반드시 상태를 그린다.**

      예전에는 여기서 토스트만 띄웠다. state.wanServer 가 null 로 남아 칩이 "확인 중"에
      영구히 멈추고, 토스트는 몇 초 뒤 사라져 화면만 보면 이유를 알 수 없었다.
      사용자가 본 것은 "확인 중 / GPU 추론 서버 상태를 확인하고 있습니다." 한 줄이었다.

      "확인하지 못했다"와 "확인 중"은 다른 상태다. 구분해서 보여 주고 다시 시도할 수단을 준다.
    */
    state.wanServer = {
      reachable: false,
      ready: false,
      detail: error.message || "옵션을 불러오지 못했습니다.",
      loadFailed: true
    };
    renderWanServer();
    /*
      이 지점이 `Cannot read properties of undefined (reading 'length')` 가 보고된 곳이다.
      토스트에는 메시지만 담기고 스택이 남지 않아, 어느 함수에서 터졌는지 알 수 없었다.

      extra 에는 값이 아니라 **형태**만 넣는다. refSets 의 키 목록이 있으면 옛 스키마
      (`images`)가 남아 있는지, 세트가 아예 없는지 바로 구분된다.
    */
    const report = reportError("loadWanOptions", error, {
      refSetKeys: Object.keys(state.refSets || {}),
      wanSetKeys: Object.keys(state.refSets?.wan || {}),
      wanConvertKeys: Object.keys(state.wanConvert || {}),
      resolutionCount: (state.wanResolutions || []).length,
      durationCount: (state.wanDurations || []).length
    });
    showToast("옵션을 불러오지 못했습니다", error.message, "error", report.id);
  }
}

function renderWanServer() {
  const chip = $("#wanServerChip");
  const note = $("#wanServerNote");
  if (!chip || !note) return;
  const server = state.wanServer;
  if (!server) {
    chip.textContent = "확인 중";
    chip.className = "doc-chip";
    note.textContent = "GPU 추론 서버 상태를 확인하고 있습니다.";
    return;
  }
  if (server.loadFailed) {
    // 상태를 **확인하지 못한** 것이지 서버가 꺼진 것이 아니다. 둘을 섞으면
    // 앱이나 네트워크 문제인데 GPU 노드를 의심하게 된다.
    chip.textContent = "확인 실패";
    chip.className = "doc-chip failed";
    note.textContent = `${server.detail} 우측 상단 [새로고침]으로 다시 시도해 주세요.`;
    return;
  }
  if (!server.reachable) {
    chip.textContent = "서버 꺼짐";
    chip.className = "doc-chip failed";
    // 스케줄로 GPU 노드가 0으로 줄어든다. 그것이 정상 동작이라는 것을 알려 준다.
    note.textContent = server.detail
      || "GPU 노드가 꺼져 있습니다. 평일 10시에 자동으로 켜지며, 그전에 필요하면 관리자에게 요청해 주세요.";
    return;
  }
  if (!server.ready) {
    chip.textContent = "준비 중";
    chip.className = "doc-chip generating";
    note.textContent = server.detail || "모델 가중치를 확인하고 있습니다.";
    return;
  }
  chip.textContent = "사용 가능";
  chip.className = "doc-chip ready";
  const busy = (server.running || 0) + (server.queued || 0);
  note.textContent = busy
    ? `현재 ${busy}건이 처리 중입니다. GPU가 한 장이라 순서대로 진행됩니다.`
    : "GPU가 비어 있습니다. 바로 시작할 수 있습니다.";
}

/*
  고른 해상도·길이에 대한 예상 소요 시간(분).

  서버는 해상도별 값 하나만 주고, 그것이 몇 초 기준인지(estimateReferenceSeconds)를
  함께 알려 준다. 길이는 화면에서 환산한다. 길이 선택지가 2~10초로 넓어진 뒤로는
  환산을 빼먹으면 10초 작업을 5초 시간으로 안내하게 된다. 실제보다 짧게 약속하면
  사용자는 진행이 멈춘 줄 알고 새로고침하거나 다시 요청한다.
*/
function wanEstimateFor(seconds) {
  const base = state.wanEstimateMinutes[state.wanResolution];
  if (!base) return 15;
  const ref = state.wanEstimateRefSeconds || 5;
  const chosen = Number(seconds) || ref;
  return Math.max(1, Math.round(base * (chosen / ref)));
}

/* ------------------------- 화풍 변환 (2단계 중 1단계) -------------------------
  Wan I2V 는 첫 프레임을 유지하며 움직임만 만든다. 그래서 영상 품질의 상한이 첫 프레임으로
  정해진다. 웹툰 컷을 그대로 넣으면 결과는 웹툰이 움직이는 영상이다.

  여기서 구도를 유지한 채 화풍만 바꿔 스틸을 만들고, **기획자가 그것을 보고 승인한 뒤**
  영상을 만든다. 승인 단계가 없으면 나쁜 첫 프레임으로 GPU 를 수십 분 돌린 뒤에야 알게 된다.
*/
function currentConvertMode() {
  const modes = state.wanConvert?.modes || [];
  return modes.find((mode) => mode.id === state.wanConvertMode) || modes[0] || null;
}

function renderWanConvert() {
  const box = $("#wanConvertModes");
  if (!box) return;
  const modes = state.wanConvert?.modes || [];

  box.innerHTML = "";
  modes.forEach((mode) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.value = mode.id;
    button.textContent = mode.label;
    button.title = mode.hint || "";
    button.className = mode.id === state.wanConvertMode ? "active" : "";
    button.addEventListener("click", () => {
      if (state.wanConvertMode === mode.id) return;
      state.wanConvertMode = mode.id;
      // 모드를 바꾸면 그 모드의 권장 강도로 되돌린다. 실사와 웹툰의 권장값이 다르다
      // (웹툰은 비율이 과장돼 있어 실사 변환에서 구조를 덜 붙잡는 편이 자연스럽다).
      if (mode.strength) state.wanConvertStrength = mode.strength;
      // 모드가 바뀌면 앞서 만든 스틸은 근거를 잃는다. 남겨 두면 다른 모드의 결과가
      // 첫 프레임으로 쓰인다.
      discardConverted();
      renderWanConvert();
    });
    box.appendChild(button);
  });

  const mode = currentConvertMode();
  const converts = Boolean(mode?.converts);
  const strengthField = $("#wanStrengthField");
  const actions = $("#wanConvertActions");
  if (strengthField) strengthField.classList.toggle("is-hidden", !converts);
  if (actions) actions.classList.toggle("is-hidden", !converts);

  /*
    서버가 변환 모델을 쓸 수 있는지 함께 알려 준다(convert.available).

    모드 목록은 정적 표라서 목록이 내려온다고 쓸 수 있다는 뜻이 아니다. 실제로 모델이
    비활성이던 동안 화면은 네 모드를 모두 정상으로 보여 주었고, 사용자는 미리보기를 누른
    뒤에야 실패를 봤다. 누르기 전에 알려 주고 버튼을 막는다.
  */
  const unavailable = state.wanConvert && state.wanConvert.available === false;
  const notice = $("#wanConvertUnavailable");
  if (notice) {
    notice.classList.toggle("is-hidden", !unavailable);
    if (unavailable) {
      notice.textContent = state.wanConvert.unavailableReason
        || "화풍 변환 모델을 지금 사용할 수 없습니다. [원본 그대로]로 계속할 수 있습니다.";
    }
  }

  const slider = $("#wanStrength");
  const range = state.wanConvert?.strengthRange || { min: 0.2, max: 0.9 };
  if (slider) {
    slider.min = String(range.min);
    slider.max = String(range.max);
    slider.value = String(state.wanConvertStrength);
  }
  const valueLabel = $("#wanStrengthValue");
  if (valueLabel) valueLabel.textContent = Number(state.wanConvertStrength).toFixed(2);

  const hint = $("#wanConvertHint");
  if (hint) {
    if (!converts) {
      hint.textContent = mode?.hint || "올린 컷을 그대로 첫 프레임으로 씁니다.";
    } else {
      hint.textContent =
        `${mode.hint} 구도 유지 강도를 올리면 원본 구도를 더 지키고, 내리면 화풍이 더 자유로워집니다.`;
    }
  }

  // 참고 이미지 세트의 목록 속성은 items 다(refSets.wan.items).
  // images 로 잘못 쓰면 undefined.length 로 화면 전체가 죽는다.
  const imageCount = refState("wan").items.length;
  const runButton = $("#wanConvertRun");
  if (runButton) {
    // 모델을 쓸 수 없으면 이미지가 있어도 막는다. 눌러도 실패할 요청을 보내지 않는다.
    runButton.disabled = imageCount === 0 || unavailable;
    runButton.title = unavailable
      ? "화풍 변환 모델을 지금 사용할 수 없습니다."
      : (imageCount === 0 ? "먼저 웹툰 컷을 올려 주세요." : "");
  }
  renderConvertResult();
}

function renderConvertResult() {
  const box = $("#wanConvertResult");
  if (!box) return;
  const result = state.wanConverted;
  box.classList.toggle("is-hidden", !result);
  if (!result) return;

  $("#wanConvertImage").src = result.url;
  $("#wanConvertLabel").textContent =
    `${result.modeLabel || "변환"} · ${result.style?.presetLabel || ""}`.replace(/ · $/, "");
  $("#wanConvertMeta").textContent =
    `구도 유지 강도 ${Number(result.style?.controlStrength ?? 0).toFixed(2)} · `
    + "이 이미지가 영상의 첫 프레임이 됩니다.";
  $("#wanConvertPrompt").textContent = result.prompt || "";
}

function discardConverted() {
  state.wanConverted = null;
  // 갤러리는 다시 그리지 않는다. 갤러리의 "첫 프레임" 배지는 set.primaryKey 를 보고
  // 변환본은 거기에 들어가지 않으므로 바뀔 것이 없다. 게다가 renderRefGallery 가
  // renderWanConvert 를 부르므로 서로 부르는 모양이 된다.
  renderConvertResult();
}

async function runWanConvert() {
  const mode = currentConvertMode();
  if (!mode?.converts) return;

  const refs = refPayload("wan");
  if (!refs.assetKeys.length) {
    showToast("이미지가 필요합니다", "웹툰 컷을 먼저 올려 주세요.", "error");
    return;
  }

  const button = $("#wanConvertRun");
  const status = $("#wanConvertStatus");
  button.disabled = true;
  if (status) status.textContent = "변환하고 있습니다...";

  try {
    const payload = await api("/api/v2/video/convert", {
      method: "POST",
      body: JSON.stringify({
        ...refs,
        mode: state.wanConvertMode,
        controlStrength: state.wanConvertStrength,
        text: $("#wanText").value.trim(),
        stylePrompt: $("#wanStyle").value.trim(),
        presetId: state.wanPreset || undefined
      })
    });
    if (!payload.converted) {
      // keep 모드가 잘못 눌린 경우. 원본을 쓰므로 결과를 남기지 않는다.
      discardConverted();
      if (status) status.textContent = payload.note || "원본을 그대로 사용합니다.";
      return;
    }
    state.wanConverted = payload;
    renderConvertResult();
    if (status) status.textContent = "변환했습니다. 마음에 들면 영상을 만들어 주세요.";
    showToast("화풍 변환 완료", "이 이미지가 영상의 첫 프레임이 됩니다.");
  } catch (error) {
    if (status) status.textContent = "";
    showToast("변환하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = refState("wan").items.length === 0;
  }
}

function renderWanOptions() {
  const resolutions = $("#wanResolutionOptions");
  const durations = $("#wanDurationOptions");
  if (!resolutions || !durations) return;

  resolutions.innerHTML = "";
  state.wanResolutions.forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.value = option.id;
    button.className = state.wanResolution === option.id ? "active" : "";
    button.textContent = option.label;
    button.addEventListener("click", () => {
      state.wanResolution = option.id;
      renderWanOptions();
    });
    resolutions.appendChild(button);
  });

  // 비율 안내. **서버 문장을 그대로 쓴다.**
  //
  // 예전 선택지는 "480p 가로 (832x480)" / "480p 세로 (480x832)" 였는데 지키지 못하는
  // 약속이었다. Wan 은 --size 를 면적으로만 쓰고 출력 비율은 올린 이미지에서 계산한다.
  // 그래서 세로 컷에 "가로"를 골라도 세로 영상이 나왔고, 비율을 맞추려고 이미지에
  // 여백을 넣으면 그 여백이 영상에 그대로 들어갔다. 선택지를 화질 단계로 바꾸고
  // 비율은 이미지가 정한다고 적는다.
  const aspectNote = $("#wanAspectNote");
  if (aspectNote) {
    aspectNote.textContent = state.wanAspectNote || "";
    aspectNote.hidden = !state.wanAspectNote;
  }

  // 해상도마다 만들 수 있는 최대 길이가 다르다. 한계는 GPU 구성에 따라 바뀐다
  // (GPU 1장이면 720p 2초, 여러 장에 시퀀스를 쪼개면 720p 5초). 그래서 이 값을
  // 화면에 적어 두지 않고 **서버가 내려 주는 maxSeconds 만** 쓴다.
  // 예전에는 여기에 "720p 는 2초" 를 적어 두었는데 GPU 를 늘린 뒤에도 화면이 계속
  // 2초라고 말했다. 반대로 그 전에는 화면이 720p 5초를 열어 주어, 10분을 기다린 끝에
  // "generate.py 종료 코드 1" 만 떴다. 양쪽 모두 화면이 하드웨어를 추측한 탓이다.
  const picked = state.wanResolutions.find((option) => option.id === state.wanResolution);
  const maxSeconds = Number(picked?.maxSeconds) || 0;

  durations.innerHTML = "";
  state.wanDurations.forEach((option) => {
    const button = document.createElement("button");
    const tooLong = maxSeconds > 0 && option.value > maxSeconds;
    button.type = "button";
    button.dataset.value = String(option.value);
    button.className = state.wanDuration === option.value ? "active" : "";
    button.textContent = option.label;
    button.disabled = tooLong;
    button.title = tooLong
      ? `${picked?.label || state.wanResolution}에서는 만들 수 없습니다 (최대 ${maxSeconds}초)`
      : `${option.frames} 프레임`;
    button.addEventListener("click", () => {
      if (tooLong) return;
      state.wanDuration = option.value;
      renderWanOptions();
    });
    durations.appendChild(button);
  });

  // 고른 길이가 이 해상도에서 불가능해졌다면(해상도를 720p로 바꾼 경우) 가능한 최대로 내린다.
  // 그러지 않으면 막힌 버튼이 active 로 남아 생성 버튼이 400을 받는다.
  if (maxSeconds > 0 && state.wanDuration > maxSeconds) {
    const fallback = state.wanDurations
      .map((option) => option.value)
      .filter((value) => value <= maxSeconds);
    if (fallback.length) {
      state.wanDuration = Math.max(...fallback);
      renderWanOptions();
      return;
    }
  }

  // 변환 모드는 서버가 준 목록으로 그린다. 옵션이 갱신될 때 함께 다시 그려야
  // 모드가 추가되거나 권장 강도가 바뀐 것이 화면에 반영된다.
  renderWanConvert();

  const chips = $("#wanStyleChips");
  if (chips && !chips.childElementCount) {
    // 그림체 프리셋을 직접 고를 수도 있게 한다. 고르지 않으면 적은 문장으로 AI가 판단한다.
    state.wanPresets.slice(0, 8).forEach((preset) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.dataset.value = preset.id;
      chip.textContent = preset.label;
      chip.title = preset.hint || "";
      chip.addEventListener("click", () => {
        state.wanPreset = state.wanPreset === preset.id ? "" : preset.id;
        $$("#wanStyleChips button").forEach((b) => {
          b.classList.toggle("active", b.dataset.value === state.wanPreset);
        });
      });
      chips.appendChild(chip);
    });
  }

  const minutes = wanEstimateFor(state.wanDuration);
  const parts = [
    `약 ${minutes}분 걸립니다.`,
    "GPU가 한 장이라 한 번에 한 건만 만듭니다. 화면을 벗어나도 계속 진행됩니다."
  ];
  // 왜 긴 길이가 막혀 있는지 적는다. 이유 없이 비활성된 버튼은 고장으로 읽힌다.
  if (maxSeconds > 0 && maxSeconds < Math.max(...state.wanDurations.map((o) => o.value))) {
    parts.unshift(`이 해상도는 GPU 메모리 한계로 최대 ${maxSeconds}초까지 만들 수 있습니다.`);
  }
  $("#wanEstimate").textContent = parts.join(" ");
  $("#wanButtonNote").textContent = `${state.wanResolution} · ${state.wanDuration}초 · 약 ${minutes}분`;
}

function setWanStatus(kind, label) {
  const badge = $("#wanStatus");
  if (!badge) return;
  badge.className = `status-badge ${kind}`;
  badge.innerHTML = `<i></i> ${escapeHtml(label)}`;
}

function setWanProgress(percent, label) {
  // 결과 패널의 진행 바와 상단 배너를 한 자리에서 함께 갱신한다. 두 곳을 따로
  // 부르게 하면 한쪽을 빠뜨린 경로가 생긴다.
  updateRun("videoNew", percent, label);
  $("#wanProgress").classList.remove("is-hidden");
  $("#wanBar").style.width = `${Math.max(0, Math.min(100, percent))}%`;
  $("#wanProgressLabel").textContent = label;
}

async function startWanGeneration() {
  const text = $("#wanText").value.trim();
  let refs = refPayload("wan");
  if (!refs.assetKeys?.length) {
    $("#wanError").textContent = "원본 이미지를 먼저 올려 주세요. 이 모델은 이미지가 반드시 필요합니다.";
    return;
  }

  // 이미지 분석을 아직 하지 않았다면 한 번 묻는다.
  //
  // 분석은 수동 버튼으로 하도록 되어 있어서, 누르지 않고 바로 생성하면 파일 이름과 그림에서
  // 읽을 수 있는 정보(각 이미지의 역할, 캐릭터 시트와 장면의 관계)가 프롬프트에 반영되지
  // 않는다. 그 손실을 사용자가 모르는 채로 넘어가지 않게 한다.
  // '예'를 고르면 여기서 자체적으로 분석한 뒤 이어서 생성한다.
  const refSet = refState("wan");
  if (!refSet.brief) {
    const proceed = await confirmDialog({
      title: "이미지 분석 없이 진행",
      lead: "잠깐, 이미지 분석을 하지 않고 바로 진행해도 괜찮습니까?",
      body:
        "분석을 건너뛰면 파일 이름과 그림에서 읽을 수 있는 정보가 프롬프트에 반영되지 않습니다. "
        + "'예'를 누르면 지금 바로 분석한 뒤 이어서 영상을 만듭니다. "
        + "'아니오'를 누르면 멈추고, 직접 [이미지 분석하기]를 눌러 확인 질문에 답할 수 있습니다.",
      target: `이미지 ${refSet.items.length}장`,
      okLabel: "예",
      cancelLabel: "아니오",
      tone: "warn"
    });
    if (!proceed) {
      // 멈춘 이유와 다음 행동을 알려 준다. 그냥 닫히면 버튼이 먹지 않은 것으로 읽힌다.
      $("#wanAnalyze").focus();
      $("#wanAnalyze").scrollIntoView({ behavior: "smooth", block: "center" });
      showToast("생성을 멈췄습니다", "[이미지 분석하기]를 누른 뒤 다시 시도해 주세요.");
      return;
    }

    // 자체 분석. 실패해도 생성을 막지 않는다. 분석은 품질을 올리는 보조 단계이고
    // analyzeRefImages 가 실패를 자체적으로 알린다(이 코드베이스의 보조 단계 공통 태도).
    setWanStatus("working", "이미지 분석 중");
    await analyzeRefImages("wan");
    // 분석 결과(brief, 추천 첫 프레임)를 요청에 실으려면 다시 만들어야 한다.
    refs = refPayload("wan");
  }

  const minutes = wanEstimateFor(state.wanDuration);
  if (!await confirmDialog({
    title: "GPU 영상 생성",
    lead: `${state.wanResolution} · ${state.wanDuration}초 클립을 만듭니다.`,
    body: `약 ${minutes}분 동안 GPU를 점유합니다. 그동안 다른 영상은 만들 수 없습니다.`,
    target: `${state.wanResolution} · ${state.wanDuration}초`,
    okLabel: "영상 만들기",
    tone: "warn"
  })) {
    // 자체 분석을 하고 온 경우 배지가 "이미지 분석 중"에 멈춘다. 대기 상태로 되돌린다.
    setWanStatus("idle", "대기 중");
    return;
  }

  const button = $("#wanGenerateButton");
  button.disabled = true;
  $("#wanError").textContent = "";
  setWanStatus("working", "준비 중");
  // 맨 위에 진행 상태를 띄우고 입력창을 접는다. 결과 대기만 남긴다.
  startRun("videoNew", "AI가 대본을 읽고 영상 프롬프트를 쓰는 중입니다...");
  setWanProgress(18, "AI가 대본을 읽고 영상 프롬프트를 쓰는 중입니다...");

  try {
    /*
      화풍을 변환했다면 **그 스틸이 첫 프레임이 된다.**

      변환 결과는 users/{...}/images/ 에 저장되어 있고 그 경로는 이미 본인 이미지로
      인정되므로(OWNED_IMAGE_CATEGORIES), 키를 목록 맨 앞에 넣고 primaryKey 로 지정하면
      기존 생성 경로가 그대로 받는다. 서버에 새 분기를 만들지 않아도 된다.

      원본 키도 함께 남긴다. 분석 결과(brief)가 원본을 가리키고 있고, 어떤 자료로 만들었는지
      작업 기록에 남아야 한다.
    */
    const converted = state.wanConverted?.item?.key;
    const submitRefs = converted
      ? {
        ...refs,
        assetKeys: [converted, ...refs.assetKeys.filter((key) => key !== converted)],
        primaryKey: converted
      }
      : refs;

    // 이미지는 올릴 때 이미 S3에 저장했다. 여기서는 키만 보낸다.
    const payload = await api("/api/v2/video/generate", {
      method: "POST",
      body: JSON.stringify({
        ...submitRefs,
        text,
        stylePrompt: $("#wanStyle").value.trim(),
        presetId: state.wanPreset || undefined,
        resolution: state.wanResolution,
        seconds: state.wanDuration,
        projectId: getWorkingProject()?.id || undefined
      })
    });
    state.wanJobId = payload.item.id;
    renderWanJob(payload.item);
    showWanPrompt(payload.item, payload.promptDegraded);
    pollWanJob();
    loadWanJobs();
    showToast("생성 시작", `약 ${payload.estimateMinutes}분 걸립니다. 화면을 벗어나도 계속 진행됩니다.`);
  } catch (error) {
    setWanStatus("idle", "실패");
    $("#wanProgress").classList.add("is-hidden");
    $("#wanError").textContent = error.message;
    // 실패하면 배너가 입력창을 다시 펼친다. 오류 문구가 접힌 패널 안에 있기 때문이다.
    endRun("videoNew", false, error.message);
    showToast("영상 생성을 시작하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function showWanPrompt(job, degraded) {
  const block = $("#wanPromptBlock");
  if (!job?.prompt) {
    block.classList.add("is-hidden");
    return;
  }
  $("#wanPromptText").textContent = job.prompt;
  const parts = [];
  if (job.cameraMove) parts.push(`카메라: ${job.cameraMove}`);
  if (job.presetLabel) parts.push(`화풍: ${job.presetLabel}`);
  parts.push(degraded
    ? "프롬프트 변환이 실패해 기본 프롬프트로 진행합니다."
    : "대본에서 한 장면을 골라 영어 프롬프트로 옮겼습니다. 원작 화풍 유지 문구가 함께 들어갑니다.");
  $("#wanPromptNote").textContent = parts.join(" · ");
  block.classList.remove("is-hidden");

  // 보강 전후. 사용자가 적은 문장이 어떻게 늘어났는지 보여준다.
  const boostBlock = $("#wanBoostBlock");
  const boost = job.boost || {};
  const rows = Object.entries(boost);
  if (!rows.length) {
    boostBlock.classList.add("is-hidden");
    return;
  }
  const names = { direction: "장면 설명", style: "화풍 지시" };
  $("#wanBoostBody").innerHTML = rows.map(([name, item]) => `
    <div class="doc-pair">
      <strong>${escapeHtml(names[name] || name)}</strong>
      <p class="boost-before">${escapeHtml(item.original || "")}</p>
      <p class="boost-after">${escapeHtml(item.text || "")}</p>
    </div>`).join("");
  boostBlock.classList.remove("is-hidden");
}

function renderWanJob(job) {
  const percent = { queued: 20, running: 55, uploading: 90, ready: 100, failed: 100 }[job.status] || 10;
  const labels = {
    queued: job.queuePosition > 1 ? `대기 중 (${job.queuePosition}번째)` : "GPU 할당을 기다리고 있습니다...",
    running: "모델이 프레임을 만들고 있습니다...",
    uploading: "결과 영상을 저장하고 있습니다...",
    ready: "완료",
    failed: "실패"
  };
  if (job.status === "ready") {
    setWanStatus("success", "완료");
    setWanProgress(100, "완료");
    if (job.videoUrl) {
      const video = $("#wanVideo");
      video.src = job.videoUrl;
      video.classList.remove("is-hidden");
      $("#wanEmpty").classList.add("is-hidden");
    }
    endRun("videoNew", true, "영상이 완성되었습니다. 결과에서 바로 재생할 수 있습니다.");
    window.setTimeout(() => $("#wanProgress").classList.add("is-hidden"), 1500);
    return;
  }
  if (job.status === "failed") {
    setWanStatus("idle", "실패");
    $("#wanProgress").classList.add("is-hidden");
    $("#wanError").textContent = job.error || "생성에 실패했습니다.";
    endRun("videoNew", false, job.error || "생성에 실패했습니다.");
    return;
  }
  setWanStatus("working", WAN_STATUS_LABELS[job.status] || job.status);
  setWanProgress(percent, labels[job.status] || "진행 중");
}

function pollWanJob() {
  window.clearTimeout(state.wanTimer);
  if (!state.wanJobId) return;
  state.wanTimer = window.setTimeout(async () => {
    const jobId = state.wanJobId;
    if (!jobId) return;
    try {
      const payload = await api(`/api/v2/video/jobs/${encodeURIComponent(jobId)}`);
      const job = payload.item;
      renderWanJob(job);
      if (job.status === "ready") {
        showToast("영상 생성 완료", "라이브러리에 저장했습니다.");
        loadWanJobs();
        loadVideos();
        return;
      }
      if (job.status === "failed") {
        showToast("영상 생성 실패", job.error || "다시 시도해 주세요.", "error");
        loadWanJobs();
        return;
      }
      pollWanJob();
    } catch (error) {
      // 네트워크가 한 번 끊겼다고 폴링을 포기하지 않는다. 15분짜리 작업이다.
      console.warn("Wan 작업 상태 조회 실패", error);
      pollWanJob();
    }
  }, WAN_POLL_MS);
}

async function loadWanJobs() {
  try {
    const payload = await api("/api/v2/video/jobs");
    state.wanJobs = payload.items || [];
    renderWanJobList();
    // 화면을 새로 열었을 때 진행 중인 작업이 있으면 그것을 이어서 본다.
    const active = state.wanJobs.find((job) =>
      ["queued", "running", "uploading"].includes(job.status));
    if (active && state.wanJobId !== active.id) {
      state.wanJobId = active.id;
      // 새로고침으로 돌아온 경우 상단 배너도 함께 되살린다. 그러지 않으면 진행 상태가
      // 결과 패널 안에만 보이는데, 그 패널이 바로 화면 밖으로 밀려 잘리던 자리다.
      // startRun 이 renderWanJob 보다 먼저 와야 한다(updateRun 이 배너를 요구한다).
      startRun("videoNew", "진행 중인 작업을 이어서 봅니다...");
      renderWanJob(active);
      showWanPrompt(active, false);
      pollWanJob();
    }
  } catch (error) {
    console.warn("Wan 작업 목록을 불러오지 못했습니다", error);
  }
}

function renderWanJobList() {
  const list = $("#wanJobList");
  if (!list) return;
  list.innerHTML = "";
  $("#wanJobsEmpty").classList.toggle("is-hidden", state.wanJobs.length > 0);
  state.wanJobs.forEach((job) => {
    const row = document.createElement("article");
    row.className = `wan-job status-${job.status}`;
    const meta = [
      job.resolution,
      `${job.seconds}초`,
      job.elapsedSeconds ? `${Math.round(job.elapsedSeconds / 60)}분 소요` : "",
      formatDate(job.createdAt)
    ].filter(Boolean).join(" · ");
    row.innerHTML = `
      <div class="wan-job-head">
        <span class="doc-chip ${job.status === "ready" ? "ready" : job.status === "failed" ? "failed" : "generating"}">
          ${escapeHtml(WAN_STATUS_LABELS[job.status] || job.status)}
        </span>
        <strong>${escapeHtml(job.title || "제목 없음")}</strong>
      </div>
      <p class="wan-job-meta">${escapeHtml(meta)}</p>
      ${job.error ? `<p class="doc-step-error">${escapeHtml(job.error)}</p>` : ""}
      <div class="wan-job-actions"></div>`;

    const actions = row.querySelector(".wan-job-actions");
    if (job.videoUrl) {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "text-action";
      open.textContent = "이 영상 보기";
      open.addEventListener("click", () => {
        state.wanJobId = job.id;
        renderWanJob(job);
        showWanPrompt(job, false);
      });
      actions.appendChild(open);

      const download = document.createElement("a");
      download.className = "text-action";
      download.href = job.videoUrl;
      download.download = `${job.id}.mp4`;
      download.textContent = "다운로드";
      actions.appendChild(download);
    }
    if (!["queued", "running", "uploading"].includes(job.status)) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "text-action danger";
      remove.textContent = "삭제";
      remove.addEventListener("click", () => deleteWanJob(job));
      actions.appendChild(remove);
    }
    list.appendChild(row);
  });
}

async function deleteWanJob(job) {
  if (!await confirmDialog({
    title: "작업 삭제",
    lead: "이 작업과 생성된 영상을 지웁니다.",
    body: "되돌릴 수 없습니다.",
    target: job.title || job.id,
    okLabel: "삭제",
    tone: "danger"
  })) return;
  try {
    await api(`/api/v2/video/jobs/${encodeURIComponent(job.id)}`, { method: "DELETE" });
    if (state.wanJobId === job.id) {
      state.wanJobId = null;
      $("#wanVideo").classList.add("is-hidden");
      $("#wanEmpty").classList.remove("is-hidden");
      setWanStatus("idle", "대기 중");
    }
    loadWanJobs();
    showToast("삭제했습니다", "작업과 영상을 지웠습니다.");
  } catch (error) {
    showToast("삭제하지 못했습니다", error.message, "error");
  }
}

/* ------------------------------ routing ------------------------------ */
/*
  브라우저 뒤로가기가 사이트를 벗어나지 않도록 화면 전환을 history에 기록한다.
  경로는 nginx의 SPA fallback(try_files ... /index.html) 덕분에 새로고침과 딥링크도 동작한다.
*/
const ROUTES = [
  { view: "home", path: "/projects" },
  { view: "studio", path: "/studio" },
  { view: "imageStudio", path: "/images" },

  // vpoc1 영상 기능 진입 경로. 화면은 vpoc.js 가 #vpoc-root 안에 직접 만든다.
  { view: "videoGen", path: "/video" },
  { view: "library", path: "/library" },
  // 내 사용량. 관리자 경로(/admin/*)와 구분해 둔다. 일반 사용자가 접근하는 화면이다.
  { view: "myUsage", path: "/my-usage" },
  { view: "users", path: "/admin/users" },
  { view: "models", path: "/admin/models" }
];

function pathForView(viewName, projectId) {
  if (viewName === "projectDetail" && projectId) {
    return `/projects/${encodeURIComponent(projectId)}`;
  }
  return ROUTES.find((route) => route.view === viewName)?.path || "/projects";
}

function parseLocation() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts[0] === "projects" && parts[1]) {
    return { view: "projectDetail", projectId: decodeURIComponent(parts[1]) };
  }
  const matched = ROUTES.find((route) => route.path === `/${parts.join("/")}`);
  return { view: matched?.view || "home", projectId: null };
}

/** 화면을 바꾸고 history에 새 항목을 남긴다. 사용자 조작에서 호출한다. */
/* 옛 "새 영상 만들기"(studio) 화면은 서버 API(/api/videos/generate · long-take · v2/video)가
   옛 영상 기능과 함께 지워져(51ba039, 2026-09-23) 무엇을 해도 "요청한 API를 찾을 수 없습니다"였다
   (운영 2026-10-08 실측). 숏폼·롱테이크 카드, 콘티 이미지 보내기, /studio 주소가 아직 이 화면으로
   왔다. 영상 만들기는 새 영상 흐름(vpoc) 하나뿐이므로 거기로 보낸다. */
function redirectStudio() {
  showToast("영상 만들기로 이동", "영상은 프로젝트의 영상 만들기(①~④)에서 만듭니다. 이미지는 ① 입력에서 올려 주세요.");
  const project = state.currentProject?.id ? state.currentProject : getWorkingProject();
  return project && project.id ? "videoGen" : "home";
}

function navigate(viewName, options = {}) {
  let { projectId = null, replace = false } = options;
  if (viewName === "studio") viewName = redirectStudio();
  // runtime flag가 꺼진 환경에서는 숨김 버튼·오래 열린 탭·콘솔 호출도 신규 화면을 열지 못한다.
  if (viewName === "videoGen" && !VIDEO_GEN_ENABLED) {
    showToast("영상 개발을 열 수 없습니다", PROJECT_TRACKS.video.disabledReason, "error");
    viewName = "home";
    projectId = null;
  }
  const path = pathForView(viewName, projectId);
  const entry = { view: viewName, projectId };
  if (replace || window.location.pathname === path) {
    window.history.replaceState(entry, "", path);
  } else {
    window.history.pushState(entry, "", path);
  }
  switchView(viewName);
}

/** history 이동(뒤로/앞으로)으로 들어온 경우. 새 항목을 남기지 않는다. */
async function applyRoute(route) {
  if (!state.token) return;
  if (route.view === "studio") {
    // 즐겨찾기·옛 링크로 /studio 에 들어온 경우. 주소도 바꿔 남긴다.
    const target = redirectStudio();
    route = { view: target, projectId: null };
    window.history.replaceState(route, "", pathForView(target, null));
  }
  if (route.view === "projectDetail" && route.projectId) {
    try {
      const payload = await api(`/api/projects/${encodeURIComponent(route.projectId)}`);
      await dispatchProject(payload.item, { replace: true });
      return;
    } catch (_error) {
      // 접근할 수 없거나 삭제된 프로젝트면 목록으로 되돌린다.
      window.history.replaceState({ view: "home" }, "", "/projects");
      switchView("home");
      showToast("프로젝트를 열 수 없습니다", "목록으로 이동했습니다.", "error");
      return;
    }
  }
  if (route.view === "videoGen" && !VIDEO_GEN_ENABLED) {
    window.history.replaceState({ view: "home", projectId: null }, "", "/projects");
    switchView("home");
    showToast("영상 개발을 열 수 없습니다", PROJECT_TRACKS.video.disabledReason, "error");
    return;
  }
  // `/video` 는 경로에 프로젝트 id 가 없다. 새로고침이나 주소 직접 입력으로 들어오면
  // `state.currentProject` 가 비어 있어 6단계 화면이 아무것도 못 그린다. 작업 프로젝트를
  // sessionStorage 에서 되살린다 — 없으면 목록으로 보낸다.
  if (route.view === "videoGen" && !state.currentProject) {
    const working = getWorkingProject();
    if (working && working.id) {
      try {
        const payload = await api(`/api/projects/${encodeURIComponent(working.id)}`);
        state.currentProject = payload.item;
        setWorkingProject(payload.item);
      } catch (_error) {
        window.history.replaceState({ view: "home", projectId: null }, "", "/projects");
        switchView("home");
        showToast("영상 개발을 열 수 없습니다", "프로젝트를 먼저 선택해 주세요.", "error");
        return;
      }
    } else {
      window.history.replaceState({ view: "home", projectId: null }, "", "/projects");
      switchView("home");
      showToast("영상 개발을 열 수 없습니다", "프로젝트를 먼저 선택해 주세요.", "error");
      return;
    }
  }
  if (route.view === "users" && !state.user?.isAdmin) {
    switchView("home");
    return;
  }
  switchView(route.view);
}

window.whynotOpenVideoGen = (project, planId = "") => {
  if (!project || !project.id) return;
  state.currentProject = project;
  setWorkingProject(project);
  // 프로젝트 화면의 계획 카드에서 들어오면 그 계획을 연다. 비우면 vpoc 가 알아서 고른다.
  state.vpocPlanId = planId || "";
  navigate("videoGen");
};

window.addEventListener("popstate", (event) => {
  const route = event.state && event.state.view ? event.state : parseLocation();
  applyRoute(route);
});

/* ------------------------------ navigation ------------------------------ */
/*
  잡히지 않은 오류도 서버로 보낸다.

  try/catch 로 감싼 곳만 리포트하면, 렌더 함수 안에서 바로 터지는 오류(가장 진단이 어려운
  종류)가 아무 기록도 남기지 않는다. 사용자는 화면이 비어 보이는 것만 겪는다.
*/
window.addEventListener("error", (event) => {
  reportError("window.onerror", event.error || new Error(event.message), {
    source: String(event.filename || "").slice(-120),
    line: event.lineno,
    column: event.colno
  });
});

window.addEventListener("unhandledrejection", (event) => {
  reportError("unhandledrejection", event.reason instanceof Error
    ? event.reason
    : new Error(String(event.reason)));
});

function switchView(viewName) {
  if (viewName === "videoGen" && !VIDEO_GEN_ENABLED) viewName = "home";
  trail("view", viewName);
  state.activeView = viewName;
  rememberRoute();
  $$(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === viewName));
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.viewTarget === viewName));
  const labels = {
    home: "프로젝트",
    projectDetail: state.currentProject?.title || "프로젝트 상세",
    studio: "새 영상 만들기",
    imageStudio: "콘티 · 이미지",
    videoGen: "영상 만들기",
    library: "영상 라이브러리",
    myUsage: "내 사용량",
    users: "사용자 관리",
    models: "모델 관리"
  };
  $("#pageTitle").textContent = labels[viewName] || "AI Studio";
  renderWorkingProject();
  closeSidebar();
  // 숨어 있던 뷰는 폭이 0이라 분할 비율의 픽셀 하한을 계산할 수 없었다. 보이는 지금 맞춘다.
  reclampSplits();
  // 다른 화면은 모두 진입할 때 필요한 데이터를 다시 불러오는데 studio 만 빠져 있었다.
  // 모델 목록은 워크스페이스 진입 때 한 번만 채워지므로, 그때 실패하면 생성 모델 칸이
  // 새로고침 전까지 영구히 비어 있었다. 여기서 되살린다.
  if (viewName === "studio" && !videoModels().length) loadModels();
  if (viewName === "library") loadVideos();
  if (viewName === "imageStudio") {
    loadImages();
    // 콘티 옵션과 마지막 기획표를 함께 되살린다. 화면을 떠났다 와도 진행 상황이 이어진다.
    if (!state.sbCutCounts.length) loadStoryboardOptions();
    if (!state.storyboard) loadLatestStoryboard();
  }
  // vpoc1 영상 기능은 진입점이 하나다. 화면 전체를 vpoc.js 가 #vpoc-root 안에 만든다.
  // vpoc.js 가 없어도 여기서 조용히 넘어가야 app.js 가 깨지지 않는다.
  if (viewName === "videoGen") {
    window.vpoc?.mount(state.currentProject?.id, state.vpocPlanId || "");
    state.vpocPlanId = "";
  }
  else window.vpoc?.unmount?.();
  if (viewName === "home") loadProjects();
  if (viewName === "users" && state.user?.isAdmin) loadUsers();
  // 사용량과 프롬프트 관리는 모델 관리 화면 안에 있다(지시서 5-1).
  if (viewName === "models" && state.user?.isAdmin) {
    loadUsage();
    loadVideoCosts();
    loadPrompts();
  }
  // 내 사용량은 진입할 때마다 다시 읽는다. 잔여 횟수는 생성할 때마다 바뀌므로
  // 캐시된 값을 보여 주면 "남았다고 했는데 거절된다"가 된다.
  if (viewName === "myUsage") loadMyUsage();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function openSidebar() {
  $("#sidebar").classList.add("open");
  $("#sidebarScrim").classList.add("show");
}

function closeSidebar() {
  $("#sidebar").classList.remove("open");
  $("#sidebarScrim").classList.remove("show");
}

/* ------------------------------ 화면 배치 ------------------------------ */
/*
  세 가지를 여기서 담당한다. 전부 브라우저 로컬 상태이고 서버로 보내지 않는다.

  1) 왼쪽 메뉴 접기      좁은 화면은 예전부터 서랍식이었지만 넓은 화면에서는 접을 수 없었다.
                        2단 화면에서 그 242px 이 결과 패널의 잘림으로 이어졌다.
  2) 좌우 분할 바        입력 열과 결과 열의 비율을 사용자가 정한다. 예전 grid 하한이
                        고정 픽셀(520px + 340px)이라 컨테이너를 넘쳐서 오른쪽이 잘렸다.
  3) 생성 중 간략히 보기  결과를 만들기 시작하면 입력창은 더 볼 일이 없다. 맨 위에 진행
                        상태를 띄우고 입력 패널을 접어 결과만 남긴다.
*/

function isNarrowLayout() {
  return window.matchMedia("(max-width: 820px)").matches;
}

/* ---- 1) 왼쪽 메뉴 접기 ---- */
function applyNavCollapsed(collapsed) {
  const shell = $("#appShell");
  if (!shell) return;
  shell.classList.toggle("nav-collapsed", collapsed);
  const label = collapsed ? "왼쪽 메뉴 펼치기" : "왼쪽 메뉴 접기";
  [$("#menuButton"), $("#navCollapse")].forEach((button) => {
    if (!button) return;
    button.setAttribute("aria-expanded", String(!collapsed));
    button.setAttribute("aria-label", label);
    button.title = label;
  });
  // 폭이 바뀌면 분할 비율의 픽셀 하한도 다시 계산해야 한다.
  window.setTimeout(reclampSplits, 240);
}

function toggleNav() {
  // 좁은 화면은 서랍식이다. 접기 상태와 섞으면 서랍이 열리지 않는다.
  if (isNarrowLayout()) {
    if ($("#sidebar").classList.contains("open")) closeSidebar();
    else openSidebar();
    return;
  }
  const collapsed = !$("#appShell").classList.contains("nav-collapsed");
  localStorage.setItem(STORAGE_KEYS.navCollapsed, collapsed ? "1" : "0");
  applyNavCollapsed(collapsed);
  // 접으면 사이드바가 visibility:hidden 이 된다. 방금 누른 버튼이 그 안에 있었다면
  // 포커스가 사라진다(#navCollapse 로 접은 경우). 상단바 토글로 옮겨 준다.
  // 펼칠 때는 누른 버튼에 포커스를 그대로 둔다(disclosure 기본 동작).
  if (collapsed && $("#sidebar").contains(document.activeElement)) $("#menuButton").focus();
}

/* ---- 2) 좌우 분할 바 ---- */
// 기본 비율(%). 왼쪽 열이 차지하는 비율이다.
const SPLIT_DEFAULTS = { studio: 62, videoNew: 62, imageStudio: 60, projectDetail: 25 };
// 어느 쪽도 이보다 좁아지면 읽을 수 없다. 비율 하한보다 이 픽셀 하한이 우선한다.
const SPLIT_MIN_PX = 260;

function splitStorageKey(name) {
  return `${STORAGE_KEYS.split}:${name}`;
}

function splitBounds(container) {
  const bar = container.querySelector(":scope > .split-bar");
  const min = Number(bar?.getAttribute("aria-valuemin"));
  const max = Number(bar?.getAttribute("aria-valuemax"));
  return {
    bar,
    min: Number.isFinite(min) ? min : 20,
    max: Number.isFinite(max) ? max : 80
  };
}

function clampSplit(container, percent) {
  const { bar, min, max } = splitBounds(container);
  const gap = bar ? bar.getBoundingClientRect().width : 0;
  const usable = container.getBoundingClientRect().width - gap;
  // 화면에 없는 뷰는 폭이 0이다. 그때는 값을 그대로 두고 보일 때 다시 맞춘다.
  if (usable <= 0) return percent;
  const floor = Math.max(min, (SPLIT_MIN_PX / usable) * 100);
  const ceil = Math.min(max, 100 - (SPLIT_MIN_PX / usable) * 100);
  if (floor > ceil) return (min + max) / 2;
  return Math.min(ceil, Math.max(floor, percent));
}

function setSplit(container, percent, persist) {
  if (!Number.isFinite(percent)) return;
  const value = Math.round(clampSplit(container, percent) * 10) / 10;
  container.style.setProperty("--split", `${value}%`);
  const { bar } = splitBounds(container);
  if (bar) bar.setAttribute("aria-valuenow", String(Math.round(value)));
  if (persist && container.dataset.split) {
    localStorage.setItem(splitStorageKey(container.dataset.split), String(value));
  }
}

function currentSplit(container) {
  const raw = parseFloat(container.style.getPropertyValue("--split"));
  if (Number.isFinite(raw)) return raw;
  return SPLIT_DEFAULTS[container.dataset.split] ?? 60;
}

function defaultSplit(container) {
  return SPLIT_DEFAULTS[container.dataset.split] ?? 60;
}

function restoreSplit(container) {
  const saved = parseFloat(localStorage.getItem(splitStorageKey(container.dataset.split)));
  setSplit(container, Number.isFinite(saved) && saved > 0 ? saved : defaultSplit(container), false);
}

function reclampSplits() {
  $$(".split-layout").forEach((container) => {
    // 보이지 않는 뷰는 폭이 0이라 계산이 무의미하다.
    if (!container.getBoundingClientRect().width) return;
    setSplit(container, currentSplit(container), false);
  });
}

function wireSplitBar(container) {
  const bar = container.querySelector(":scope > .split-bar");
  if (!bar || bar.dataset.wired === "1") return;
  bar.dataset.wired = "1";
  let dragging = false;

  const stop = (pointerId) => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("dragging");
    document.body.classList.remove("split-dragging");
    if (pointerId != null && bar.hasPointerCapture(pointerId)) bar.releasePointerCapture(pointerId);
    // 저장은 놓을 때 한 번만 한다. 이동 중 매 프레임 쓰면 localStorage 쓰기가 폭주한다.
    setSplit(container, currentSplit(container), true);
  };

  bar.addEventListener("pointerdown", (event) => {
    if (isNarrowLayout() || event.button !== 0) return;
    dragging = true;
    bar.classList.add("dragging");
    document.body.classList.add("split-dragging");
    bar.setPointerCapture(event.pointerId);
    event.preventDefault();
  });

  bar.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const rect = container.getBoundingClientRect();
    if (rect.width <= 0) return;
    setSplit(container, ((event.clientX - rect.left) / rect.width) * 100, false);
  });

  bar.addEventListener("pointerup", (event) => stop(event.pointerId));
  bar.addEventListener("pointercancel", (event) => stop(event.pointerId));
  bar.addEventListener("lostpointercapture", () => stop(null));

  // 마우스를 쓸 수 없는 사용자도 조절할 수 있어야 한다(ARIA separator 패턴).
  bar.addEventListener("keydown", (event) => {
    const { min, max } = splitBounds(container);
    const step = event.shiftKey ? 6 : 2;
    const now = currentSplit(container);
    let next = null;
    if (event.key === "ArrowLeft") next = now - step;
    else if (event.key === "ArrowRight") next = now + step;
    else if (event.key === "Home") next = min;
    else if (event.key === "End") next = max;
    else if (event.key === "Enter" || event.key === " ") next = defaultSplit(container);
    if (next === null) return;
    event.preventDefault();
    setSplit(container, next, true);
  });

  bar.addEventListener("dblclick", () => setSplit(container, defaultSplit(container), true));
}

function initLayout() {
  applyNavCollapsed(localStorage.getItem(STORAGE_KEYS.navCollapsed) === "1");
  $$(".split-layout").forEach((container) => {
    restoreSplit(container);
    wireSplitBar(container);
  });
}

let splitResizeTimer = null;
window.addEventListener("resize", () => {
  window.clearTimeout(splitResizeTimer);
  splitResizeTimer = window.setTimeout(reclampSplits, 120);
});

/* ---- 3) 생성 중: 상단 진행 배너 + 입력창 접기 ---- */
/*
  collapse:false 인 화면은 입력이 별도 열이 아니라 본문 안에 섞여 있어 접을 대상이 없다.
  그런 화면은 배너만 띄운다.
*/
const RUN_VIEWS = {
  studio: { title: "영상을 만들고 있습니다", collapse: true },
  videoNew: { title: "GPU에서 영상을 만들고 있습니다", collapse: true },
  imageStudio: { title: "이미지를 만들고 있습니다", collapse: true },
  projectDetail: { title: "산출물을 만들고 있습니다", collapse: false }
};

const runState = {};

function runHost(view) {
  return $(`.view[data-view="${view}"]`);
}

function runBanner(view, create = false) {
  const host = runHost(view);
  if (!host) return null;
  let banner = host.querySelector(":scope > .run-banner");
  if (banner || !create) return banner;

  banner = document.createElement("div");
  banner.className = "run-banner is-hidden";
  banner.setAttribute("role", "status");
  banner.setAttribute("aria-live", "polite");
  banner.innerHTML = `
    <div class="run-spinner" aria-hidden="true"></div>
    <div class="run-body">
      <div class="run-title">
        <strong data-run="title">만들고 있습니다</strong>
        <span class="run-elapsed" data-run="elapsed">0:00</span>
      </div>
      <p class="run-note" data-run="note">준비 중입니다.</p>
      <div class="run-track"><i data-run="bar"></i></div>
    </div>
    <div class="run-actions">
      <button class="outline-action" type="button" data-run="toggle">입력창 펼치기</button>
    </div>`;
  host.prepend(banner);
  banner.querySelector("[data-run=toggle]").addEventListener("click", () => toggleRunCompact(view));
  return banner;
}

function syncRunToggle(view) {
  const host = runHost(view);
  const banner = runBanner(view);
  if (!host || !banner) return;
  const compact = host.classList.contains("run-compact");
  const button = banner.querySelector("[data-run=toggle]");
  button.textContent = compact ? "입력창 펼치기" : "입력창 접기";
  button.setAttribute("aria-expanded", String(!compact));
  // 접을 열이 없는 화면에서는 버튼이 아무 일도 하지 않는다. 숨긴다.
  button.classList.toggle("is-hidden", !RUN_VIEWS[view]?.collapse);
}

function toggleRunCompact(view) {
  const host = runHost(view);
  if (!host) return;
  const compact = host.classList.toggle("run-compact");
  syncRunToggle(view);
  reclampSplits();
  if (!compact) {
    // 끝난 작업인데 입력창을 되돌렸다면 배너는 역할이 끝났다.
    if (!runState[view]?.active) runBanner(view)?.classList.add("is-hidden");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
}

function startRun(view, note) {
  const meta = RUN_VIEWS[view];
  const host = runHost(view);
  const banner = runBanner(view, true);
  if (!meta || !host || !banner) return;

  banner.classList.remove("is-hidden", "done", "failed");
  banner.querySelector("[data-run=title]").textContent = meta.title;
  banner.querySelector("[data-run=note]").textContent = note || "준비 중입니다.";
  banner.querySelector("[data-run=bar]").style.width = "6%";
  if (meta.collapse) host.classList.add("run-compact");
  syncRunToggle(view);
  reclampSplits();

  window.clearInterval(runState[view]?.timer);
  runState[view] = { startedAt: Date.now(), active: true, timer: null };
  const tick = () => {
    const seconds = Math.floor((Date.now() - runState[view].startedAt) / 1000);
    const element = banner.querySelector("[data-run=elapsed]");
    if (element) element.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  };
  tick();
  runState[view].timer = window.setInterval(tick, 1000);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function updateRun(view, percent, note) {
  if (!runState[view]?.active) return;
  const banner = runBanner(view);
  if (!banner || banner.classList.contains("is-hidden")) return;
  if (Number.isFinite(percent)) {
    banner.querySelector("[data-run=bar]").style.width = `${Math.max(0, Math.min(100, percent))}%`;
  }
  if (note) banner.querySelector("[data-run=note]").textContent = note;
}

function endRun(view, ok, note) {
  const host = runHost(view);
  const banner = runBanner(view);
  if (!host || !banner) return;
  window.clearInterval(runState[view]?.timer);
  if (runState[view]) runState[view].active = false;

  banner.classList.remove("is-hidden");
  banner.classList.toggle("done", Boolean(ok));
  banner.classList.toggle("failed", !ok);
  banner.querySelector("[data-run=title]").textContent = ok ? "완료했습니다" : "만들지 못했습니다";
  banner.querySelector("[data-run=note]").textContent = note
    || (ok ? "결과를 확인해 보세요." : "입력을 고쳐 다시 시도해 주세요.");
  banner.querySelector("[data-run=bar]").style.width = "100%";
  // 실패했으면 입력창을 되돌린다. 고쳐야 할 입력이 접힌 채로 남으면 손댈 수가 없다.
  if (!ok) host.classList.remove("run-compact");
  syncRunToggle(view);
  reclampSplits();
}

/* ------------------------------ wiring ------------------------------ */
$("#tabLogin").addEventListener("click", () => switchAuthTab("login"));
$("#tabSignup").addEventListener("click", () => switchAuthTab("signup"));
$("#loginForm").addEventListener("submit", handleLogin);
$("#signupForm").addEventListener("submit", handleSignup);

$("#passwordToggle").addEventListener("click", () => {
  const input = $("#password");
  input.type = input.type === "password" ? "text" : "password";
});
$("#passwordChangeButton").addEventListener("click", openPasswordModal);
$("#passwordModalClose").addEventListener("click", closePasswordModal);
$("#passwordCancel").addEventListener("click", closePasswordModal);
$("#passwordForm").addEventListener("submit", handlePasswordChange);
$("#passwordModal").addEventListener("click", (event) => {
  // 어두운 배경을 누르면 닫는다. 모달 안을 누른 경우는 무시한다.
  if (event.target === event.currentTarget) closePasswordModal();
});
$("#logoutButton").addEventListener("click", () => signOut());
$("#toastClose").addEventListener("click", () => $("#toast").classList.remove("show"));
// 좁은 화면에서는 서랍 열기/닫기, 넓은 화면에서는 메뉴 접기/펼치기.
$("#menuButton").addEventListener("click", toggleNav);
$("#navCollapse").addEventListener("click", toggleNav);
$("#sidebarClose").addEventListener("click", closeSidebar);
$("#sidebarScrim").addEventListener("click", closeSidebar);
$$("[data-view-target]").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.preventDefault();
    navigate(button.dataset.viewTarget);
  });
});

/* NEW_영상 제작 (드롭존은 wireRefDropzone 이 아래에서 배선한다) */
$("#wanText").addEventListener("input", (event) => {
  $("#wanTextCount").textContent = `${event.target.value.length.toLocaleString("ko-KR")} / 20,000자`;
});
$("#wanGenerateButton").addEventListener("click", startWanGeneration);
$("#wanConvertRun").addEventListener("click", runWanConvert);
$("#wanConvertRetry").addEventListener("click", runWanConvert);
$("#wanConvertDiscard").addEventListener("click", () => {
  discardConverted();
  const status = $("#wanConvertStatus");
  if (status) status.textContent = "원본을 첫 프레임으로 사용합니다.";
});
$("#wanStrength").addEventListener("input", (event) => {
  state.wanConvertStrength = Number(event.target.value);
  $("#wanStrengthValue").textContent = state.wanConvertStrength.toFixed(2);
});
$("#wanRefresh").addEventListener("click", () => {
  loadWanOptions();
  loadWanJobs();
});

$("#newProjectButton").addEventListener("click", openProjectModal);
$("#projectBack").addEventListener("click", () => navigate("home"));
$("#detailSave").addEventListener("click", saveProjectDetail);
$("#detailVisibilityToggle").addEventListener("click", toggleDetailVisibility);
$("#detailDelete").addEventListener("click", deleteDetailProject);
/*
  숏폼·롱테이크 진입점은 지시서 0절에 따라 화면에서 뺐다(코드는 남긴다).
  markup 이 없으므로 `$()` 가 null 을 준다. 옵셔널 체이닝이 없으면 이 줄에서
  TypeError 가 나고 **그 뒤에 등록되는 모든 리스너가 붙지 않는다** — 화면 전체가 죽는다.
  기능을 되살릴 때는 index.html 의 handoff 카드를 되돌리면 이 배선이 다시 살아난다.
  sendProjectTo() 자체는 그대로 두었다.
*/
$("#handoffShort")?.addEventListener("click", () => sendProjectTo("short"));
$("#handoffStoryboard")?.addEventListener("click", () => sendProjectTo("storyboard"));
$("#handoffLong")?.addEventListener("click", () => sendProjectTo("long"));
$("#handoffSceneVideo")?.addEventListener("click", () => sendProjectTo("videoGen"));
/* 영상 프로젝트 상세의 "영상 개발 시작". 처리하던 video-folder.js 가 옛 영상 기능과 함께 지워져
   (51ba039, 2026-09-23) 눌러도 아무 일이 없었다. 영상 화면(vpoc)으로 연결한다 — vpoc 는 그
   프로젝트의 진행 중 계획(현재 단계) 또는 완성된 영상을 스스로 찾아 연다. */
$("#videoFolderEnter")?.addEventListener("click", () => {
  if (!state.currentProject?.id) {
    showToast("프로젝트를 찾을 수 없습니다", "목록에서 프로젝트를 다시 열어 주세요.", "error");
    return;
  }
  window.whynotOpenVideoGen(state.currentProject);
});
// "영상 접기" — 이것도 video-folder.js 와 함께 처리기가 사라졌었다.
$("#videoFolderToggle")?.addEventListener("click", () => {
  const box = $("#videoFolderVideos");
  const toggle = $("#videoFolderToggle");
  const collapsed = box.classList.toggle("is-hidden");
  toggle.setAttribute("aria-expanded", String(!collapsed));
  toggle.textContent = collapsed ? "영상 펼치기 ▲" : "영상 접기 ▼";
});
$("#detailCoverAi").addEventListener("click", async () => {
  const project = state.currentProject;
  if (!project) return;
  await generateCover(project, $("#detailCoverAi"));
  await openProject(project.id);
});
$("#detailCoverFile").addEventListener("change", async (event) => {
  const project = state.currentProject;
  if (!project) return;
  await uploadCover(project, event.target.files[0]);
  event.target.value = "";
  await openProject(project.id);
});
$("#projectModalClose").addEventListener("click", closeProjectModal);
$("#projectCancel").addEventListener("click", closeProjectModal);
$("#projectModal").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closeProjectModal();
});
$("#projectForm").addEventListener("submit", createProject);
$("#projectTitle").addEventListener("input", syncProjectCreateState);
$$("#projectTrackOptions [data-track]").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.disabled) return;
    $$("#projectTrackOptions [data-track]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    $("#projectError").textContent = "";
    syncProjectCreateState();
  });
});
$$("#projectTrackChoices [data-track]").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.disabled) return;
    $$("#projectTrackChoices [data-track]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.projectTrackSelection = button.dataset.track;
    $("#projectTrackError").textContent = "";
    $("#projectTrackConfirm").disabled = false;
  });
});
$("#projectTrackModalClose")?.addEventListener("click", () => closeProjectTrackModal({ toList: true }));
$("#projectTrackCancel")?.addEventListener("click", () => closeProjectTrackModal({ toList: true }));
$("#projectTrackModal")?.addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closeProjectTrackModal({ toList: true });
});
$("#projectTrackConfirm")?.addEventListener("click", confirmProjectTrack);
$$("#projectTrackFilter [data-track-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    state.projectTrackFilter = button.dataset.trackFilter || "all";
    $$("#projectTrackFilter [data-track-filter]").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    renderProjects();
  });
});
// 진입점 그리드는 index.html 에서 지웠다. $$() 는 빈 NodeList 라 아래는 무동작이지만,
// 플래그를 되돌려 그리드를 되살릴 때 배선이 필요하므로 남겨 둔다.
$$("#inputTypeOptions .input-type").forEach((button) => {
  button.addEventListener("click", () => {
    $$("#inputTypeOptions .input-type").forEach((b) => b.classList.remove("active"));
    button.classList.add("active");
  });
});
// 영상 폴더 구획의 두 버튼. 둘 다 '새로운 영상 만들기' 화면으로 보낸다 —
// 캐릭터 등록과 장면 생성이 모두 그 화면에 있다. sendProjectTo 가 working project 를
// 넘겨 주므로 scene.js 가 projectId 를 실어 보내고, 만든 것이 이 폴더에 쌓인다.
$("#folderCharacterAdd")?.addEventListener("click", () => sendProjectTo("videoGen"));
$("#folderSceneAdd")?.addEventListener("click", () => sendProjectTo("videoGen"));
// 접이식을 펼칠 때 목록을 처음 요청한다. 닫힌 상태에서는 renderProjectFolder 가 즉시 반환한다.
$("#projectFolderLegacySection")?.addEventListener("toggle", (event) => {
  if (event.target.open) renderProjectFolder(state.currentProject);
});
$("#sourceSave").addEventListener("click", saveSource);
$("#sourceFile").addEventListener("change", (event) => {
  loadSourceFile(event.target.files[0]);
  event.target.value = "";
});
$("#directionSave").addEventListener("click", saveDirection);
$$("#detailInputTypeOptions button").forEach((button) => {
  button.addEventListener("click", () => changeInputType(button.dataset.value));
});
$$("#visibilityOptions button").forEach((button) => {
  button.addEventListener("click", () => {
    $$("#visibilityOptions button").forEach((b) => b.classList.remove("active"));
    button.classList.add("active");
  });
});

$("#companyCodeForm").addEventListener("submit", saveCompanyCode);
$("#refreshUsers").addEventListener("click", loadUsers);
$("#deleteUserClose").addEventListener("click", closeDeleteUserModal);
$("#deleteUserCancel").addEventListener("click", closeDeleteUserModal);
$("#deleteConfirmCheck").addEventListener("change", (event) => {
  $("#deleteUserConfirm").disabled = !event.target.checked;
});
$("#deleteUserConfirm").addEventListener("click", confirmDeleteUser);

/*
  참고 이미지 묶음을 배선한다. 두 화면이 각자의 요소와 각자의 상태를 쓴다.
    studio = 새 영상 만들기 (Bedrock/Luma)  ->  POST /api/videos/analyze
    wan    = NEW_영상 제작 (자체 GPU Wan)   ->  POST /api/v2/video/analyze
  셀렉터가 겹치지 않으므로 한 화면의 이미지가 다른 화면에 나타나지 않는다.
*/
wireRefDropzone("studio", "#dropzone", "#fileInput");
wireRefDropzone("wan", "#wanDropzone", "#wanFile");

$("#promptInput").addEventListener("input", () => {
  updatePromptCount();
  localStorage.setItem(STORAGE_KEYS.prompt, $("#promptInput").value);
});
$$(".suggestions button").forEach((button) => button.addEventListener("click", () => {
  const input = $("#promptInput");
  input.value = `${input.value.trim()}${input.value.trim() ? " " : ""}${button.dataset.prompt}`.slice(0, 600);
  localStorage.setItem(STORAGE_KEYS.prompt, input.value);
  updatePromptCount();
  input.focus();
}));
$$("#durationOptions button").forEach((button) => button.addEventListener("click", () => {
  state.duration = button.dataset.value;
  localStorage.setItem(STORAGE_KEYS.duration, state.duration);
  syncVideoOptions();
}));
$("#generateButton").addEventListener("click", startGeneration);
$("#refreshLibrary").addEventListener("click", async () => {
  await loadVideos();
  showToast("목록 새로고침", "라이브러리를 최신 상태로 불러왔습니다.");
});
$("#refreshModels").addEventListener("click", loadModels);
/*
  두 영상 기능 사이를 오가는 링크. 사이드바를 다시 찾지 않아도 되게 한다.

  두 기능은 **공존한다.** 지시서 9-1 은 Bedrock 영상 경로를 끄라고 하지만, 그렇게 하면
  "새 영상 만들기"와 롱테이크가 함께 죽는다(카탈로그의 kind=video 가 Luma 하나뿐이고
  Wan 은 카탈로그에 없다). 발주자가 두 기능을 각각 다른 선택지로 남기라고 확정했다.
*/
$$("[data-view-jump]").forEach((button) =>
  button.addEventListener("click", () => navigate(button.dataset.viewJump))
);
// 한도 조정 모달. 요소가 없는 화면에서도 스크립트가 죽지 않게 옵셔널 체이닝을 쓴다.
$("#quotaSave")?.addEventListener("click", saveQuota);
$("#quotaCancel")?.addEventListener("click", closeQuotaModal);
$("#quotaClose")?.addEventListener("click", closeQuotaModal);
$$("#resolutionOptions button").forEach((button) => button.addEventListener("click", () => {
  state.resolution = button.dataset.value;
  localStorage.setItem(STORAGE_KEYS.resolution, state.resolution);
  syncVideoOptions();
}));
$$("#aspectOptions button").forEach((button) => button.addEventListener("click", () => {
  state.aspectRatio = button.dataset.value;
  localStorage.setItem(STORAGE_KEYS.aspect, state.aspectRatio);
  syncVideoOptions();
}));
$$("#usageRangeOptions button").forEach((button) => button.addEventListener("click", () => {
  state.usageDays = Number(button.dataset.value) || 30;
  loadUsage();
}));
$("#refreshUsage").addEventListener("click", () => loadUsage());
$("#refreshVideoCosts")?.addEventListener("click", () => loadVideoCosts());
$("#refreshMyUsage")?.addEventListener("click", () => loadMyUsage());
// 프롬프트 관리
$("#refreshPrompts")?.addEventListener("click", () => loadPrompts());
$("#promptMappingButton")?.addEventListener("click", openPromptMapping);
$("#promptMappingCopy")?.addEventListener("click", copyPromptMapping);
$("#promptMappingOk")?.addEventListener("click", closePromptModals);
$("#promptMappingClose")?.addEventListener("click", closePromptModals);
$("#promptHistoryOk")?.addEventListener("click", closePromptModals);
$("#promptHistoryClose")?.addEventListener("click", closePromptModals);
$("#imageGenerateButton").addEventListener("click", generateImage);
$("#storyboardGenerateButton").addEventListener("click", startStoryboard);
$("#sbPrint").addEventListener("click", () => window.print());
$("#sbDelete").addEventListener("click", deleteStoryboard);
$("#sbAddCut").addEventListener("click", addCut);
// 적은 줄 수가 곧 컷 수라 입력하는 동안 개수와 예상 시간을 갱신한다.
$("#sbManualCuts").addEventListener("input", () => {
  if (state.sbMode === "manual") renderStoryboardOptions();
});
$("#refreshImages").addEventListener("click", loadImages);
$("#referenceFile").addEventListener("change", (event) => {
  uploadStyleReference(event.target.files[0]);
  event.target.value = "";
});
$("#referenceClear").addEventListener("click", () => {
  setStyleReference("", "", "");
  showToast("참조 해제", "그림체 참조를 사용하지 않습니다.");
});
$$("#imageAspectOptions button").forEach((button) => button.addEventListener("click", () => {
  state.imageAspect = button.dataset.value;
  $$("#imageAspectOptions button").forEach((b) => b.classList.toggle("active", b === button));
}));
$("#useAsVideoFrame").addEventListener("click", () => {
  if (state.lastImage) sendImageToStudio(state.lastImage);
});
$("#useAsReference").addEventListener("click", () => {
  if (!state.lastImage) return;
  setStyleReference(state.lastImage.key, state.lastImage.url, state.lastImage.filename);
  showToast("참조 등록", "이 이미지의 그림체를 따라 그립니다.");
});
$$("#frameModeOptions button").forEach((button) =>
  button.addEventListener("click", () => setFrameMode(button.dataset.value))
);
$("#frameAiGenerate").addEventListener("click", generateFrameImage);
$("#frameAiRetry").addEventListener("click", generateFrameImage);
$("#longTakeButton").addEventListener("click", startLongTake);

/* 프로젝트 맥락 배너 */
$("#fromProjectBack").addEventListener("click", () => {
  const working = getWorkingProject();
  if (working?.id) openProject(working.id);
});
$("#fromProjectClear").addEventListener("click", () => setWorkingProject(null));

/* 진행 중 작업 */
$("#runningChip").addEventListener("click", () => {
  navigate("library");
  // 진행 목록이 화면 위쪽에 있으므로 맨 위로 올린다.
  window.scrollTo({ top: 0, behavior: "smooth" });
});
$("#refreshRunning").addEventListener("click", () => loadRunning());

/* 확인 모달 이벤트. 기존 모달들과 같은 방식으로 붙인다. */
$("#confirmClose").addEventListener("click", () => closeConfirm(false));
$("#confirmCancel").addEventListener("click", () => closeConfirm(false));
$("#confirmOk").addEventListener("click", () => closeConfirm(true));
$("#confirmModal").addEventListener("click", (event) => {
  // 어두운 배경을 누르면 취소. 모달 안을 누른 경우는 무시한다.
  if (event.target === event.currentTarget) closeConfirm(false);
});

/* 결과물 읽기 뷰어 이벤트. */
$("#readerClose")?.addEventListener("click", closeReader);
$("#readerDismiss")?.addEventListener("click", closeReader);
$("#readerModal")?.addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closeReader();
});
$("#readerDownload")?.addEventListener("click", () => {
  if (!readerState.text) {
    showToast("내려받을 내용이 없습니다", "본문이 비어 있습니다.", "error");
    return;
  }
  downloadTextFile(readerState.filename, readerState.text);
  showToast("txt로 저장했습니다", readerState.filename);
});
$("#readerCopy")?.addEventListener("click", async () => {
  if (!readerState.text) return;
  const ok = await copyTextToClipboard(readerState.text);
  if (ok) showToast("복사했습니다", "본문 전체를 클립보드에 담았습니다.");
  else showToast("복사하지 못했습니다", "txt 다운로드를 이용해 주세요.", "error");
});
$("#readerPdf")?.addEventListener("click", (event) => {
  /* 화면에 그려진 표를 종이로 옮기는 것이 아니라, 서버에 같은 결과물의 PDF 를 만들어
     달라고 요청한다. 그래서 컷 그림이 화면에 다 받아졌는지 기다릴 필요가 없다 —
     서버가 S3 에서 직접 읽는다. */
  downloadPdf(readerState.pdfUrl, readerState.pdfName, event.currentTarget);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeConfirm(false);
    closeReader();
    closeProjectModal();
    closeProjectTrackModal({ toList: true });
    closeDeleteUserModal();
    closePasswordModal();
  }
});

window.addEventListener("DOMContentLoaded", validateSession, { once: true });


/* =========================================================================
   스토리 개편 (로그라인 / 콘티 / 대본)

   지시서 3절의 화면. 기존 스튜디오·콘티 화면과 코드를 공유하지 않는다 —
   저 화면들은 프로젝트와 무관하게 단독으로 동작하고, 이쪽은 프로젝트 상세 안에 있다.

   상태는 state.story 하나에 모은다. 전역에 흩뿌리면 프로젝트를 옮겨 다닐 때
   앞 프로젝트의 값이 남는다(기존 refSets 가 studio/wan 을 분리한 것과 같은 이유).
   ========================================================================= */

const STORY_TYPE_LABELS = { script: "대본", treatment: "트리트먼트", logline: "로그라인" };
const STORY_KIND_LABELS = { logline: "로그라인", storyboard: "콘티", script: "대본" };
const STORY_IMAGE_KINDS = {
  webtoon_cut: "웹툰 컷",
  character_sheet: "캐릭터 시트",
  concept_art: "컨셉 아트",
  photo: "사진",
  document: "문서",
  other: "기타"
};
const STORY_USABLE_LABELS = {
  character: "인물", setting: "배경", mood: "분위기", plot: "사건", style: "화풍"
};
const STORY_FLAG_LABELS = {
  minor: "미성년 의심", nudity: "노출", violence: "폭력", real_person: "실존 인물"
};
/* 스토리 계보. 결과물이 "지금 스토리로 만든 것"인지 알려 준다.

   프로젝트는 고유한 스토리 하나를 갖고, 결과물은 그 스토리로 만든 것만 보관한다.
   그런데 스토리 수정을 막을 수는 없다 — 막으면 한 번 생성한 프로젝트가 영구히 잠긴다.
   그래서 막지 않고 **어긋났다는 사실을 보인다.**

   판정은 서버가 한다(story_context.stamp_drift). 화면이 다시 계산하지 않는 이유:
   판정 근거가 생성 시점에 박은 지문이고, 그것은 그래프에만 있다. */
const STORY_DRIFT_LABELS = {
  story_text: "생성한 뒤 실제 스토리가 수정되었습니다.",
  story_type: "생성한 뒤 스토리 타입이 변경되었습니다."
};

function storyDriftChip(stamp) {
  return (stamp || {}).stale ? '<span class="doc-chip drift">스토리 변경됨</span>' : "";
}

function storyDriftNote(stamp) {
  const info = stamp || {};
  if (!info.drift) return "";
  const label = STORY_DRIFT_LABELS[info.drift] || "생성한 뒤 스토리가 변경되었습니다.";
  const then = Number(info.storyCharCount) > 0
    ? `생성 당시 원문 ${Number(info.storyCharCount).toLocaleString("ko-KR")}자`
    : "";
  const snapshot = String(info.storySnapshot || "").trim();
  return `<div class="story-drift" role="note">
    <p><b>${escapeHtml(label)}</b>${then ? ` ${escapeHtml(then)}` : ""}</p>
    ${snapshot ? `<p class="story-drift-snapshot">생성 당시 요약: ${escapeHtml(snapshot)}</p>` : ""}
    <p class="story-drift-hint">지금 스토리에 맞추려면 다시 만들어 주세요. 기존 결과물은 그대로 남습니다.</p>
  </div>`;
}

const STORY_MAX_IMAGES = 10;
const STORY_SUMMARY_MAX = 300;
const STORY_CUT_CHOICES = [4, 6, 8, 12];
// 대본 잡 폴링 간격. 회차당 40~90초라 자주 물을 이유가 없다.
const SCRIPT_POLL_MS = 10000;

state.story = {
  projectId: "",
  assets: [],
  outputs: [],
  cutCount: 6,
  // 콘티 옵션이 펼쳐져 있는지. [콘티]를 고른 상태만 참이다.
  cutOpen: false,
  scriptJob: null,
  scriptTimer: null,
  outlineJobId: "",
  scriptOptions: null,
  busy: false
};

function storyReset(projectId) {
  if (state.story.scriptTimer) {
    window.clearInterval(state.story.scriptTimer);
    state.story.scriptTimer = null;
  }
  state.story = {
    projectId,
    assets: [],
    outputs: [],
    cutCount: 6,
    cutOpen: false,
    scriptJob: null,
    scriptTimer: null,
    outlineJobId: "",
    scriptOptions: null,
    busy: false
  };
}

/* ------------------------------ 렌더 ------------------------------ */
function renderStoryPanel(project, editable) {
  if (state.story.projectId !== project.id) storyReset(project.id);

  const summary = $("#storySummary");
  const text = $("#storyText");
  if (!summary || !text) return;

  summary.value = project.summaryStory || "";
  summary.readOnly = !editable;
  text.readOnly = !editable;
  updateStoryCounts();

  // 실제 스토리는 S3에 있어 프로젝트 응답에 본문이 없다. 별도로 읽어 온다.
  // charCount 로 있는지 알 수 있어, 없으면 요청을 아끼고 옛 story 필드로 내려간다.
  if ((project.storyText || {}).charCount > 0) {
    loadStoryText(project.id);
  } else {
    text.value = project.story || "";
    updateStoryCounts();
  }

  const type = project.storyType || "";
  $$("#storyTypeOptions button").forEach((button) => {
    button.classList.toggle("active", button.dataset.value === type);
    button.disabled = !editable;
  });
  const badge = $("#storyTypeBadge");
  badge.textContent = type ? STORY_TYPE_LABELS[type] : "타입 미선택";
  badge.classList.toggle("is-hidden", false);

  $("#storyButtons").classList.toggle("is-hidden", !editable);
  $("#storySummaryAuto").classList.toggle("is-hidden", !editable);
  $("#storyDropzone").classList.toggle("is-hidden", !editable);

  state.story.assets = project.storyAssets || [];
  renderStoryAssets();
  renderStoryCutOptions();
  // 프로젝트를 새로 열면 콘티 옵션은 접힌 상태에서 시작한다.
  setStoryCutPanel(false);
  syncStoryActions(project);
  loadStoryOutputs(project.id);
  loadScriptJobs(project.id);
}

async function loadStoryText(projectId) {
  // 프로젝트 응답에 본문이 없으므로 결과물 조회와 같은 방식으로 읽는다.
  // 실패해도 화면을 막지 않는다 — 사용자가 다시 입력할 수 있다.
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(projectId)}/story-text`);
    if (state.story.projectId !== projectId) return;
    $("#storyText").value = payload.text || "";
    updateStoryCounts();
  } catch (_error) {
    // 라우트가 없으면(구 배포) 조용히 넘어간다. 저장은 정상 동작한다.
  }
}

function updateStoryCounts() {
  const summary = $("#storySummary");
  const text = $("#storyText");
  if (!summary || !text) return;
  const used = summary.value.length;
  const count = $("#storySummaryCount");
  count.textContent = `${used.toLocaleString("ko-KR")} / ${STORY_SUMMARY_MAX}자`;
  // 상한에 닿으면 알려준다. maxlength 로 막히지만 왜 안 써지는지 모르면 혼란스럽다.
  count.style.color = used >= STORY_SUMMARY_MAX ? "var(--danger)" : "";
  $("#storyTextCount").textContent = `${text.value.length.toLocaleString("ko-KR")}자`;
}

function currentStoryType() {
  return $("#storyTypeOptions .active")?.dataset.value || "";
}

function syncStoryActions(project) {
  // 결과물을 만들려면 타입과 실제 스토리가 있어야 한다. 서버도 같은 것을 검사하지만
  // 눌러 보고 나서 거절되는 것보다 미리 막고 이유를 적는 편이 낫다.
  const hasType = Boolean(currentStoryType());
  const hasText = Boolean(($("#storyText")?.value || "").trim());
  const editable = canEditProject(project || state.currentProject || {});
  const ready = hasType && hasText && editable;

  // #storyCutRun 은 콘티 옵션 안의 실행 버튼이라 카드와 같은 조건으로 막는다.
  ["#makeLogline", "#makeStoryboard", "#makeScript", "#makeVideo", "#storyCutRun"].forEach((selector) => {
    const button = $(selector);
    if (button) button.disabled = !ready || state.story.busy;
  });

  const hint = $("#outputsHint");
  if (!editable) hint.textContent = "다른 사용자의 공유 프로젝트입니다. 결과물을 볼 수만 있습니다.";
  else if (!hasText) hint.textContent = "실제 스토리를 입력하고 저장한 뒤 만들 수 있습니다.";
  else if (!hasType) hint.textContent = "스토리 타입(대본 / 트리트먼트 / 로그라인)을 선택해 주세요.";
  else hint.textContent = "첨부 이미지 분석과 작품 바이블을 자동으로 만든 뒤 결과물을 생성합니다.";
}

/* ---- 분석 카드 ---- */
function renderStoryAssets() {
  const host = $("#storyAssetCards");
  if (!host) return;
  const assets = state.story.assets || [];
  $("#storyAssetsEmpty").classList.toggle("is-hidden", assets.length > 0);
  host.innerHTML = "";

  assets.forEach((asset) => {
    const analysis = asset.analysis || {};
    const card = document.createElement("article");
    card.className = `story-card${asset.edited_by_user ? " edited" : ""}`;
    const kind = STORY_IMAGE_KINDS[analysis.image_kind] || "기타";
    const usable = (analysis.usable_as || []).map((v) => STORY_USABLE_LABELS[v] || v).join(" · ");
    const flags = (analysis.safety_flags || [])
      .map((v) => `<span class="story-flag">${escapeHtml(STORY_FLAG_LABELS[v] || v)}</span>`)
      .join("");
    const characters = (analysis.characters || [])
      .map((c) => `${c.name_if_visible ? `${escapeHtml(c.name_if_visible)}: ` : ""}${escapeHtml(c.descriptor || "")}`)
      .join("\n");

    card.innerHTML = `
      <div class="story-card-thumb">
        ${asset.url ? `<img src="${escapeHtml(asset.url)}" alt="${escapeHtml(asset.name || "첨부 이미지")}">` : ""}
        <span class="doc-chip story-card-kind">${escapeHtml(kind)}</span>
      </div>
      <div class="story-card-body">
        <strong title="${escapeHtml(asset.name || "")}">${escapeHtml(asset.name || "(이름 없음)")}</strong>
        <div class="story-field">
          <label>인물</label>
          <textarea data-field="characters" rows="2" placeholder="인물이 보이지 않으면 비워 둡니다">${escapeHtml(characters)}</textarea>
        </div>
        <div class="story-field">
          <label>배경 · 시간대</label>
          <textarea data-field="setting" rows="2">${escapeHtml(analysis.setting || "")}</textarea>
        </div>
        <div class="story-field">
          <label>분위기 · 조명</label>
          <textarea data-field="mood_lighting" rows="2">${escapeHtml(analysis.mood_lighting || "")}</textarea>
        </div>
        <div class="story-field">
          <label>화풍</label>
          <textarea data-field="art_style" rows="2">${escapeHtml(analysis.art_style || "")}</textarea>
        </div>
        <div class="story-field tall">
          <label>이미지 속 텍스트 (말풍선 · 자막)</label>
          <textarea data-field="text_in_image" rows="3">${escapeHtml(analysis.text_in_image || "")}</textarea>
        </div>
        <p class="field-hint tight">용도: ${escapeHtml(usable || "판단 없음")}</p>
      </div>
      <div class="story-card-foot">
        <div class="story-card-flags">${flags}</div>
        <button class="text-action" type="button" data-save>수정 저장</button>
      </div>`;

    card.querySelector("[data-save]").addEventListener("click", () => saveAssetAnalysis(asset, card));
    host.appendChild(card);
  });
}

async function saveAssetAnalysis(asset, card) {
  const body = {};
  card.querySelectorAll("[data-field]").forEach((input) => {
    const field = input.dataset.field;
    if (field === "characters") {
      // 한 줄에 인물 하나. "이름: 설명" 형태를 받는다.
      body.characters = input.value
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          const at = line.indexOf(":");
          return at > 0
            ? { name_if_visible: line.slice(0, at).trim(), descriptor: line.slice(at + 1).trim() }
            : { name_if_visible: "", descriptor: line };
        });
    } else {
      body[field] = input.value;
    }
  });

  try {
    const payload = await api(
      `/api/projects/${encodeURIComponent(state.story.projectId)}/assets/${encodeURIComponent(asset.s3_key)}`,
      { method: "PATCH", body: JSON.stringify(body) }
    );
    state.story.assets = (payload.project?.storyAssets || []).map((item) => ({
      ...item,
      url: state.story.assets.find((a) => a.s3_key === item.s3_key)?.url
    }));
    renderStoryAssets();
    showToast("분석 수정", "수정한 내용이 작품 바이블에 반영됩니다.");
  } catch (error) {
    showToast("저장하지 못했습니다", error.message, "error");
  }
}

/* ---- 이미지 첨부 ---- */
async function addStoryImages(fileList) {
  const files = Array.from(fileList || []).filter((file) => {
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      showToast("지원하지 않는 형식", `${file.name}은 PNG, JPG, WEBP가 아닙니다.`, "error");
      return false;
    }
    if (file.size > 20 * 1024 * 1024) {
      showToast("파일이 너무 큽니다", `${file.name}은 20MB를 넘습니다.`, "error");
      return false;
    }
    return true;
  });
  if (!files.length) return;

  const existing = (state.story.assets || []).length;
  if (existing + files.length > STORY_MAX_IMAGES) {
    showToast("이미지가 너무 많습니다", `최대 ${STORY_MAX_IMAGES}장까지 첨부합니다.`, "error");
    return;
  }

  const status = $("#storyStatus");
  status.textContent = "이미지를 올리고 분석하는 중입니다...";
  state.story.busy = true;
  syncStoryActions();

  try {
    const keys = (state.story.assets || []).map((a) => a.s3_key);
    for (const file of files) {
      const body = new FormData();
      body.append("file", file);
      const uploaded = await api("/api/assets", { method: "POST", body });
      keys.push(uploaded.item.key);
    }

    const payload = await api(
      `/api/projects/${encodeURIComponent(state.story.projectId)}/assets/analyze`,
      { method: "POST", body: JSON.stringify({ assetKeys: keys }) }
    );
    state.story.assets = (payload.items || []).map((item) => ({
      s3_key: item.s3Key,
      name: item.name,
      url: item.url,
      edited_by_user: item.editedByUser,
      analysis: item
    }));
    renderStoryAssets();

    const totals = payload.totals || {};
    status.textContent =
      `이미지 ${state.story.assets.length}장 · 분석 ${totals.calls || 0}건` +
      (totals.cacheHits ? ` · 캐시 재사용 ${totals.cacheHits}건` : "");

    if ((payload.safetyBlocked || []).length) {
      showToast(
        "안전 검토가 필요한 이미지가 있습니다",
        `${payload.safetyBlocked.join(", ")} — 이 이미지가 있으면 생성이 중단됩니다.`,
        "error"
      );
    } else {
      showToast("분석 완료", "카드에서 내용을 확인하고 필요하면 수정해 주세요.");
    }
  } catch (error) {
    status.textContent = "";
    showToast("분석하지 못했습니다", error.message, "error");
  } finally {
    state.story.busy = false;
    syncStoryActions();
  }
}

/* ---- 저장 / 자동 요약 ---- */
async function saveStory() {
  const type = currentStoryType();
  if (!type) {
    showToast("스토리 타입을 선택해 주세요", "대본 / 트리트먼트 / 로그라인 중 하나입니다.", "error");
    return;
  }
  const button = $("#storySave");
  button.disabled = true;
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(state.story.projectId)}/story`, {
      method: "POST",
      body: JSON.stringify({
        summaryStory: $("#storySummary").value.trim(),
        storyText: $("#storyText").value,
        storyType: type
      })
    });
    state.currentProject = { ...(state.currentProject || {}), ...payload.item };
    $("#storyStatus").textContent = `저장했습니다 · ${$("#storyText").value.length.toLocaleString("ko-KR")}자`;
    $("#storyTypeBadge").textContent = STORY_TYPE_LABELS[type] || type;
    syncStoryActions();
    showToast("스토리 저장", "이제 로그라인 · 콘티 · 대본을 만들 수 있습니다.");
  } catch (error) {
    showToast("저장하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function autoSummarize() {
  const text = $("#storyText").value.trim();
  if (!text) {
    showToast("실제 스토리가 필요합니다", "요약할 원문을 먼저 입력해 주세요.", "error");
    return;
  }
  const button = $("#storySummaryAuto");
  button.disabled = true;
  button.textContent = "요약 중...";
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(state.story.projectId)}/summary`, {
      method: "POST"
    });
    $("#storySummary").value = payload.summary || "";
    updateStoryCounts();
    showToast("자동 요약", "내용을 확인하고 [스토리 저장]을 눌러 주세요.");
  } catch (error) {
    showToast("요약하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "실제 스토리에서 자동 요약";
  }
}


/* ---- 콘티 옵션 ---- */
function renderStoryCutOptions() {
  const host = $("#storyCutCounts");
  if (!host) return;
  host.innerHTML = "";
  STORY_CUT_CHOICES.forEach((count) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.value = String(count);
    button.textContent = `${count}컷`;
    button.className = count === state.story.cutCount ? "active" : "";
    button.addEventListener("click", () => {
      state.story.cutCount = count;
      renderStoryCutOptions();
    });
    host.appendChild(button);
  });
  // 컷당 그림 1장이라 시간과 비용이 컷 수에 그대로 비례한다. 그것을 알려준다.
  $("#storyCutHint").textContent =
    `컷 ${state.story.cutCount}개 · 컷마다 그림 1장을 만듭니다 (약 ${20 + state.story.cutCount * 9}초). 콘티 한 장이 결과물 1건입니다.`;
}

/* 콘티 옵션을 펼치고 접는다.
   전에는 항상 펼쳐 두었다. 컷 수를 바꿀 수 있다는 것을 알리려던 것인데, [로그라인]이나
   [대본]을 눌러도 컷 수 칸이 그대로 남아 어느 결과물의 설정인지 알 수 없었다.
   그래서 [콘티]를 고른 동안에만 보여 주고, 카드에 눌린 표시를 남긴다. */
function setStoryCutPanel(open) {
  state.story.cutOpen = Boolean(open);
  const box = $("#storyCutOptions");
  const card = $("#makeStoryboard");
  if (box) box.classList.toggle("is-hidden", !state.story.cutOpen);
  if (card) {
    card.classList.toggle("active", state.story.cutOpen);
    card.setAttribute("aria-expanded", state.story.cutOpen ? "true" : "false");
  }
}

/* ---- 결과물 생성 ---- */
async function makeStoryOutput(kind) {
  if (state.story.busy) return;
  const projectId = state.story.projectId;
  const body = kind === "storyboard" ? { cutCount: state.story.cutCount } : {};

  state.story.busy = true;
  syncStoryActions();
  startRun("projectDetail", `${STORY_KIND_LABELS[kind]}을 만들고 있습니다...`);
  updateRun("projectDetail", 20, "첨부 이미지 분석과 작품 바이블을 확인하는 중입니다...");

  try {
    const payload = await api(
      `/api/projects/${encodeURIComponent(projectId)}/outputs/${kind}`,
      { method: "POST", body: JSON.stringify(body) }
    );
    endRun("projectDetail", true, `${STORY_KIND_LABELS[kind]}을 만들었습니다.`);

    if (kind === "storyboard") {
      // 시작했으면 옵션 칸은 접는다. 남겨 두면 다시 눌러야 하는 것처럼 보인다.
      setStoryCutPanel(false);
      showToast("콘티 생성 시작", "컷 그림을 그리는 중입니다. 콘티 화면에서 진행을 볼 수 있습니다.");
    } else {
      const status = payload.item?.status;
      showToast(
        `${STORY_KIND_LABELS[kind]} 완료`,
        status === "needs_review"
          ? "검수에서 확인이 필요한 항목이 있습니다. 카드의 메모를 확인해 주세요."
          : "결과를 확인해 주세요."
      );
    }
    await loadStoryOutputs(projectId);
  } catch (error) {
    endRun("projectDetail", false, error.message);
    showToast(`${STORY_KIND_LABELS[kind]}을 만들지 못했습니다`, error.message, "error");
  } finally {
    state.story.busy = false;
    syncStoryActions();
  }
}

async function loadStoryOutputs(projectId) {
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(projectId)}/outputs`);
    if (state.story.projectId !== projectId) return;
    state.story.outputs = payload.items || [];
    renderStoryOutputs();
  } catch (error) {
    console.warn("결과물 목록을 불러오지 못했습니다", error);
  }
}

function renderStoryOutputs() {
  const host = $("#storyOutputList");
  if (!host) return;
  const items = state.story.outputs || [];
  $("#storyOutputsEmpty").classList.toggle("is-hidden", items.length > 0);
  host.innerHTML = "";

  items.forEach((item) => {
    const row = document.createElement("article");
    const needsReview = item.status === "needs_review";
    row.className = `story-output${needsReview ? " needs-review" : ""}${item.status === "failed" ? " failed" : ""}`;

    const meta = [
      STORY_KIND_LABELS[item.kind] || item.kind,
      item.model,
      item.tokensOut ? `출력 ${item.tokensOut.toLocaleString("ko-KR")}토큰` : "",
      formatDate(item.createdAt)
    ].filter(Boolean).join(" · ");

    // 지시서 0절: 모든 생성물에 AI 생성 표기.
    const badge = item.aiGenerated ? '<span class="ai-badge">AI 생성</span>' : "";
    const statusChip = needsReview
      ? '<span class="doc-chip failed">검수 확인 필요</span>'
      : item.status === "failed"
        ? '<span class="doc-chip failed">실패</span>'
        : '<span class="doc-chip ready">완료</span>';

    const notes = (item.validatorNotes || []).length
      ? `<div class="review-notes">${item.validatorNotes.map((note) => `
          <p class="review-note"><b>${escapeHtml(note.label || note.kind || "확인")}</b> ${escapeHtml(note.detail || "")}
          ${note.fix ? `<span class="fix">→ ${escapeHtml(note.fix)}</span>` : ""}</p>`).join("")}</div>`
      : "";

    row.innerHTML = `
      <div class="story-output-head">
        ${statusChip}
        <strong title="${escapeHtml(item.title || "")}">${escapeHtml(item.title || STORY_KIND_LABELS[item.kind] || "결과물")}</strong>
        ${storyDriftChip(item.story)}
        ${badge}
      </div>
      <p class="story-output-meta">${escapeHtml(meta)}</p>
      ${item.error ? `<p class="doc-step-error">${escapeHtml(item.error)}</p>` : ""}
      ${storyDriftNote(item.story)}
      ${notes}
      <div class="story-output-actions"></div>`;

    const actions = row.querySelector(".story-output-actions");
    if (item.s3Key) {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "text-action";
      open.textContent = "내용 보기";
      open.addEventListener("click", () => openStoryOutput(item));
      actions.appendChild(open);
    }
    if (needsReview) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "text-action";
      retry.textContent = "재생성";
      retry.addEventListener("click", () => makeStoryOutput(item.kind));
      actions.appendChild(retry);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "text-action danger";
    remove.textContent = "삭제";
    remove.addEventListener("click", () => deleteStoryOutput(item));
    actions.appendChild(remove);

    host.appendChild(row);
  });
}

/* 컷 그림이 없을 때 그 자리에 적을 말. 상태를 그대로 옮긴다.

   콘티는 컷마다 모델을 부르는 비동기 작업이다. "아직 그리는 중"과 "실패"와
   "그림 없음"은 사용자가 해야 할 일이 다르다(기다리기 / 다시 그리기 / 콘티 화면 확인). */
function shotPlaceholder(shot) {
  if (!shot) return "그림 없음";
  if (shot.error) return `그림 실패: ${shot.error}`;
  return { pending: "그리는 중", drawing: "그리는 중", failed: "그림 실패" }[shot.status] || "그림 없음";
}

async function openStoryOutput(item) {
  try {
    const payload = await api(`/api/outputs/${encodeURIComponent(item.id)}`);
    const body = payload.payload?.payload || payload.payload || {};
    const label = STORY_KIND_LABELS[item.kind] || "결과물";
    // 상세 응답의 story 가 최신 판정이다. 목록에서 넘어온 item 은 그 사이에 스토리가
    // 바뀌었을 수 있어 신뢰하지 않는다.
    const stamp = payload.item?.story || item.story;
    // 콘티 컷 그림. 결과물 본문(S3)에는 없고 storyboard 정점에 있어 서버가 함께 내려준다.
    const board = payload.storyboard;
    const shots = board?.cuts || [];
    const drawn = shots.filter((shot) => shot.imageUrl).length;
    showStoryPayload(item.kind, body, {
      title: item.title || body.title || label,
      meta: [
        label,
        item.model,
        formatDate(item.createdAt),
        // 몇 컷이 그려졌는지 보여 준다. 그림이 비어 있을 때 원인을 여기서 알 수 있다.
        shots.length ? `그림 ${drawn}/${shots.length}컷` : "",
        board && board.status === "drawing" ? "그리는 중" : "",
        stamp?.stale ? "생성 후 스토리 변경됨" : ""
      ].filter(Boolean).join(" · "),
      filename: `${projectFilePrefix()}_${label}`,
      shots,
      // 기획표 머리글(그림체·모델)과 제목이 콘티 정점에 있다.
      board,
      /* 콘티와 로그라인만 서버가 PDF 로 만든다. 대본 결과물은 회차 단위라 잡 라우트로
         받는다(openEpisode / 잡 패널의 전체 PDF). 목록을 여기 적어 두는 이유는 서버가
         지원하지 않는 종류에 버튼을 띄우지 않기 위해서다 — 서버는 400 을 준다. */
      pdfUrl: ["storyboard", "logline"].includes(item.kind)
        ? `/api/outputs/${encodeURIComponent(item.id)}/pdf`
        : "",
      pdfName: `${projectFilePrefix()}_${label}.pdf`
    });
  } catch (error) {
    showToast("내용을 불러오지 못했습니다", error.message, "error");
  }
}

/* 내려받은 파일이 어느 프로젝트 것인지 파일명만 보고 알 수 있게 접두사를 만든다.
   같은 이름의 파일이 다운로드 폴더에 쌓이면 (1),(2) 가 붙어 구분이 안 된다. */
function projectFilePrefix() {
  const title = state.currentProject?.title || "프로젝트";
  const now = new Date();
  const stamp = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0")
  ].join("");
  return `${safeFileName(title, "프로젝트")}_${stamp}`;
}

/* 결과물 payload 를 "읽을 수 있는 글"로 바꾼다.

   화면용 blocks 와 파일용 text 를 한 번에 만든다. 둘을 따로 만들면 한쪽만 고쳐져
   내려받은 txt 가 화면과 달라진다. 지문 안의 줄바꿈은 손대지 않고 그대로 넘긴다 —
   화면은 CSS pre-wrap 으로, 파일은 그 자체로 줄바꿈을 살린다. */
function buildStoryDocument(kind, body, heading = "", shots = [], board = null) {
  const blocks = [];
  const lines = [];
  // 콘티는 블록 목록이 아니라 기획표(표)로 그린다. 그 행을 여기서 함께 모은다 —
  // 표와 txt 를 따로 순회하면 한쪽만 고쳐져 인쇄물과 파일이 달라진다.
  const sheetRows = [];
  const clean = (value) => String(value ?? "").replace(/\r\n/g, "\n").trim();

  if (heading) lines.push(heading, "");

  if (kind === "logline") {
    (body.options || []).forEach((option, index) => {
      const recommended = Number(body.recommended || 0) === Number(option.no || index + 1);
      const slug = `${option.no || index + 1}안`
        + (option.angle ? ` · ${option.angle}` : "")
        + (recommended ? " (추천)" : "");
      blocks.push({ type: "scene", text: slug });
      lines.push(slug);

      const logline = clean(option.logline);
      if (logline) {
        blocks.push({ type: "action", text: logline });
        lines.push(logline);
      }
      [
        ["장르", option.genre],
        ["톤", option.tone],
        ["고른 이유", option.why]
      ].forEach(([label, value]) => {
        const text = clean(value);
        if (!text) return;
        blocks.push({ type: "note", label, text });
        lines.push(`${label}: ${text}`);
      });
      lines.push("");
    });
  } else if (kind === "storyboard") {
    [["등장 인물", body.characters], ["배경", body.setting]].forEach(([label, value]) => {
      const text = clean(value);
      if (!text) return;
      blocks.push({ type: "note", label, text });
      lines.push(`${label}: ${text}`);
    });
    if (lines.length && lines[lines.length - 1] !== "") lines.push("");

    // 컷 번호로 그림을 찾는다. 배열 순서가 아니라 번호로 맞춘다 —
    // 컷을 추가·삭제하면 순서가 어긋난다.
    const shotByNumber = new Map(
      (shots || []).map((shot, index) => [Number(shot.number || index + 1), shot])
    );

    (body.cuts || []).forEach((cut, index) => {
      const number = Number(cut.number || index + 1);
      const slug = `컷 ${number}`
        + (cut.shot ? ` · ${cut.shot}` : "")
        + (cut.seconds ? ` · ${cut.seconds}초` : "");
      blocks.push({ type: "scene", text: slug });
      lines.push(slug);

      // txt 로 내려받을 때는 그림을 넣을 수 없다. 있었다는 사실만 남긴다.
      // 화면은 아래 sheetRows 가 그린다(콘티는 블록 목록이 아니라 표다).
      const shot = shotByNumber.get(number);
      if (shot) {
        lines.push(shot.imageUrl ? "[컷 그림 있음]" : `[컷 그림 ${shotPlaceholder(shot)}]`);
      }

      const context = clean(cut.context);
      if (context) {
        blocks.push({ type: "action", text: context });
        lines.push(context);
      }
      const dialogue = clean(cut.dialogue);
      if (dialogue) {
        blocks.push({ type: "cue", character: "대사", text: dialogue });
        lines.push(`대사: ${dialogue}`);
      }
      const audio = clean(cut.audio);
      if (audio) lines.push(`음향: ${audio}`);
      const note = clean(cut.note);
      if (note) lines.push(`메모: ${note}`);
      const scene = clean(cut.scene);
      if (scene) {
        blocks.push({ type: "note", label: "이미지 프롬프트", text: scene });
        lines.push(`이미지 프롬프트: ${scene}`);
      }
      lines.push("");

      // 기획표 행. 컬럼 구성은 콘티 기획표(#storyboardPanel)와 같아야 한다 —
      // 그 표가 인쇄물의 기준 형식이고, 두 화면이 갈라지면 같은 콘티가 다르게 나온다.
      sheetRows.push({
        number,
        shotLabel: SB_SHOT_LABELS[String(cut.shot || "").toLowerCase()] || cut.shot || "",
        status: shot?.status || (shot?.imageUrl ? "done" : "pending"),
        imageUrl: shot?.imageUrl || "",
        placeholder: shot ? shotPlaceholder(shot) : "그림 없음",
        context,
        dialogue,
        audio,
        note,
        seconds: Number(cut.seconds) || 0
      });
    });
  } else {
    (body.scenes || []).forEach((scene, index) => {
      const slug = clean(scene.slug) || `S#${index + 1}`;
      blocks.push({ type: "scene", text: slug });
      lines.push(slug);

      const action = clean(scene.action);
      if (action) {
        blocks.push({ type: "action", text: action });
        // 지문은 원문 줄바꿈을 그대로 둔다. 한 줄로 합치면 동작 단위가 사라진다.
        lines.push(action);
      }
      (scene.dialogue || []).forEach((line) => {
        const text = clean(line.line);
        if (!text) return;
        const character = clean(line.character) || "대사";
        blocks.push({ type: "cue", character, text });
        // 파일에서도 인물명과 대사를 두 줄로 나눠 대본 형태를 유지한다.
        lines.push("", `    ${character}`, `        ${text.replace(/\n/g, "\n        ")}`);
      });
      lines.push("");
    });

    const hook = clean(body.ending_hook);
    if (hook) {
      blocks.push({ type: "hook", label: "엔딩 훅", text: hook });
      lines.push("[엔딩 훅]", hook, "");
    }
  }

  // 빈 줄이 셋 이상 붙는 것만 둘로 줄인다. 그 이상은 씬 구분이 아니라 여백일 뿐이다.
  const text = lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();

  /* 콘티 기획표. 컷이 하나라도 있을 때만 만든다 — 빈 표를 그리면 "만들어졌는데
     비어 있다" 와 "아직 없다" 를 구별할 수 없다.

     Total 은 컷 초의 합으로 낸다. board.totalSeconds 를 쓰지 않는 이유: 결과물 본문의
     컷과 콘티 정점의 컷이 어긋날 수 있고(컷 추가·삭제), 표에 적힌 초의 합과 머리글의
     합이 다르면 표가 틀린 것으로 보인다. */
  const sheet = kind === "storyboard" && sheetRows.length
    ? {
        title: clean(board?.title) || clean(body.title) || heading || "콘티 기획표",
        meta: [
          board?.presetLabel && `그림체 ${board.presetLabel}`,
          board?.modelLabel && `모델 ${board.modelLabel}`,
          clean(body.characters) && `인물 ${clean(body.characters)}`,
          clean(body.setting) && `배경 ${clean(body.setting)}`
        ].filter(Boolean).join(" · "),
        rows: sheetRows,
        cutCount: sheetRows.length,
        totalSeconds: sheetRows.reduce((sum, row) => sum + (row.seconds || 0), 0)
      }
    : null;

  return { blocks, text, sheet };
}

function showStoryPayload(kind, body, options = {}) {
  const label = STORY_KIND_LABELS[kind] || "결과물";
  const title = options.title || body.title || label;
  const heading = options.heading ?? title;
  // shots: 콘티 컷 그림 [{number, imageUrl, status, error}]. 결과물 본문과 별도 정점에서 온다.
  const { blocks, text, sheet } = buildStoryDocument(
    kind, body, heading, options.shots || [], options.board || null
  );

  openReader({
    title,
    meta: options.meta || label,
    blocks,
    // 콘티는 기획표로 그린다. sheet 가 있으면 리더가 블록 대신 표를 쓴다.
    sheet,
    text,
    filename: options.filename || `${projectFilePrefix()}_${label}`,
    // PDF 를 만들 수 있는 결과물만 pdfUrl 을 넘긴다. 호출부가 판단한다 —
    // 그쪽만 결과물 id(또는 잡 id + 회차)를 안다.
    pdfUrl: options.pdfUrl || "",
    pdfName: options.pdfName || ""
  });
}

async function deleteStoryOutput(item) {
  if (!await confirmDialog({
    title: "결과물 삭제",
    lead: "이 결과물을 지웁니다.",
    body: "되돌릴 수 없습니다.",
    target: item.title || STORY_KIND_LABELS[item.kind],
    okLabel: "삭제",
    tone: "danger"
  })) return;
  try {
    await api(`/api/outputs/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    await loadStoryOutputs(state.story.projectId);
    showToast("삭제했습니다", "결과물을 지웠습니다.");
  } catch (error) {
    showToast("삭제하지 못했습니다", error.message, "error");
  }
}

/* ---- 대본: 모달 → 아웃라인 → 승인 → 회차 ---- */
async function openScriptModal() {
  $("#scriptModal").classList.remove("is-hidden");
  await refreshScriptEstimate();
}

function closeScriptModal() {
  $("#scriptModal").classList.add("is-hidden");
}

function scriptModalValues() {
  return {
    from: Math.max(1, parseInt($("#scriptFrom").value, 10) || 1),
    to: Math.max(1, parseInt($("#scriptTo").value, 10) || 1),
    length: $("#scriptLengthOptions .active")?.dataset.value || "normal",
    format: $("#scriptFormatOptions .active")?.dataset.value || "drama",
    quality: $("#scriptQualityOptions .active")?.dataset.value || "standard"
  };
}

async function refreshScriptEstimate() {
  const values = scriptModalValues();
  const count = values.to - values.from + 1;
  const submit = $("#scriptSubmit");

  if (count < 1) {
    $("#scriptEstimate").textContent = "끝 화가 시작 화보다 커야 합니다.";
    submit.disabled = true;
    return;
  }
  try {
    const query = `from=${values.from}&to=${values.to}&length=${values.length}&quality=${values.quality}`;
    const payload = await api(
      `/api/projects/${encodeURIComponent(state.story.projectId)}/outputs/script/options?${query}`
    );
    state.story.scriptOptions = payload;
    const max = payload.maxEpisodesPerJob || 12;
    $("#scriptRangeHint").textContent = `한 번에 최대 ${max}화까지 만들 수 있습니다.`;
    submit.disabled = count > max;

    const est = payload.estimate || {};
    if (count > max) {
      $("#scriptEstimate").textContent = `${count}화를 요청했습니다. 최대 ${max}화까지 나눠 만들어 주세요.`;
    } else {
      // 예상 시간은 범위로 보여준다. 단일 값으로 보여 주면 그 값을 약속으로 읽고,
      // 정상적으로 그보다 오래 걸릴 때 고장으로 판단한다.
      const span = est.estimateMinutesRange;
      const minutes = span ? `${span[0]}~${span[1]}분` : `약 ${est.estimateMinutes}분`;
      $("#scriptEstimate").textContent =
        `${est.episodes}화 · 모델 호출 약 ${est.calls?.total}회 ` +
        `(아웃라인 1 + 회차 ${est.calls?.episodes} + 검수 ${est.calls?.validator}) · ` +
        `출력 약 ${(est.tokens?.outputEstimate || 0).toLocaleString("ko-KR")}토큰 · ` +
        `예상 ${minutes}. ${est.note || ""}`;
    }
    const quality = (payload.qualities || []).find((q) => q.id === values.quality);
    $("#scriptQualityHint").textContent = quality ? `모델: ${quality.model}` : "";
  } catch (error) {
    $("#scriptEstimate").textContent = `예상값을 불러오지 못했습니다: ${error.message}`;
  }
}

async function submitScriptJob() {
  const values = scriptModalValues();
  const button = $("#scriptSubmit");
  button.disabled = true;
  button.textContent = "시작하는 중...";
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(state.story.projectId)}/outputs/script`, {
      method: "POST",
      body: JSON.stringify({
        episodeFrom: values.from,
        episodeTo: values.to,
        length: values.length,
        format: values.format,
        quality: values.quality
      })
    });
    closeScriptModal();
    state.story.outlineJobId = payload.item.id;
    /*
      아웃라인은 이제 이 응답에 오지 않는다. 서버가 202로 즉시 답하고 백그라운드에서
      만든다. 예전에는 이미지 분석 + 바이블 + 아웃라인을 이 요청 안에서 처리해서
      1~5분이 걸렸고, ALB idle timeout 60초에 걸려 브라우저가 먼저 끊겼다.
      사용자는 실패로 봤지만 서버는 계속 돌고 있었다.

      완성은 폴링으로 확인한다. awaiting 이 되면 startScriptPolling 이 아웃라인 모달을
      자동으로 띄운다.
    */
    showToast("아웃라인 생성 시작", "아웃라인을 만들고 있습니다. 완성되면 자동으로 열립니다.");
    startScriptPolling(payload.item.id);
  } catch (error) {
    showToast("대본 생성을 시작하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "아웃라인 만들기";
  }
}

function showOutlineModal(outline, job) {
  $("#outlineArc").textContent = outline.arc || "";
  $("#outlineBody").innerHTML = (outline.episodes || []).map((ep) => `
    <div class="outline-item">
      <h3>${ep.no}화 · ${escapeHtml(ep.title || "")}</h3>
      <p>${escapeHtml(ep.summary || "")}</p>
      ${(ep.beats || []).length ? `<ul class="outline-beats">${ep.beats.map((b) => `<li>${escapeHtml(b)}</li>`).join("")}</ul>` : ""}
      ${ep.ending_hook ? `<p class="outline-hook">엔딩 훅: ${escapeHtml(ep.ending_hook)}</p>` : ""}
    </div>`).join("");
  $("#outlineModal").classList.remove("is-hidden");
  state.story.outlineJobId = job.id;
}

async function approveOutline() {
  const jobId = state.story.outlineJobId;
  if (!jobId) return;
  const button = $("#outlineApprove");
  button.disabled = true;
  try {
    await api(`/api/script-jobs/${encodeURIComponent(jobId)}/approve`, { method: "POST" });
    $("#outlineModal").classList.add("is-hidden");
    showToast("승인했습니다", "회차별 대본 생성을 시작했습니다. 진행 상황이 아래에 표시됩니다.");
    startScriptPolling(jobId);
  } catch (error) {
    showToast("승인하지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function startScriptPolling(jobId, { openOutlineWhenReady = true } = {}) {
  if (state.story.scriptTimer) window.clearInterval(state.story.scriptTimer);
  const poll = async () => {
    try {
      const payload = await api(`/api/script-jobs/${encodeURIComponent(jobId)}`);
      state.story.scriptJob = payload.item;
      renderScriptJob(payload.item, payload.progress);
      if (!["planning", "writing"].includes(payload.item.status)) {
        window.clearInterval(state.story.scriptTimer);
        state.story.scriptTimer = null;
        if (payload.item.status === "ready") {
          showToast("대본 완료", `${payload.progress.done}화를 만들었습니다.`);
        }
        if (payload.item.status === "failed") {
          showToast("대본 생성 실패", payload.item.error || "다시 시도해 주세요.", "error");
        }
        // 아웃라인이 완성되면 바로 보여준다. 승인 버튼을 찾아 누르게 만들면
        // 사용자는 "왜 회차가 안 만들어지지" 로 한참을 기다린다.
        if (payload.item.status === "awaiting" && openOutlineWhenReady) {
          showOutlineModal(outlineFromJob(payload.item), payload.item);
        }
      }
    } catch (error) {
      console.warn("대본 진행을 읽지 못했습니다", error);
    }
  };
  poll();
  state.story.scriptTimer = window.setInterval(poll, SCRIPT_POLL_MS);
}

/* 잡 응답에서 아웃라인 모달이 쓰는 형태를 만든다. 아웃라인 원문은 S3 에 있고
   잡에는 회차 요약만 실려 오므로, 그것으로 대신 그린다(beats 는 없다). */
function outlineFromJob(job) {
  return {
    arc: job.arc,
    episodes: (job.episodes || []).map((ep) => ({
      no: ep.no, title: ep.title, summary: ep.summary, beats: [], ending_hook: ep.endingHook
    }))
  };
}

async function loadScriptJobs(projectId) {
  try {
    const payload = await api(`/api/projects/${encodeURIComponent(projectId)}/script-jobs`);
    if (state.story.projectId !== projectId) return;
    const job = (payload.items || [])[0];
    if (!job) return;
    state.story.scriptJob = job;
    const total = (job.episodes || []).length || 1;
    renderScriptJob(job, {
      done: job.doneCount || 0, failed: job.failedCount || 0, total,
      percent: Math.round((job.doneCount || 0) / total * 100)
    });
    if (["planning", "writing"].includes(job.status)) startScriptPolling(job.id);
    else if (job.status === "awaiting") {
      // 승인 대기 상태로 남아 있으면 알려준다. 그러지 않으면 사용자는 왜 회차가
      // 만들어지지 않는지 알 수 없다.
      $("#storyStatus").textContent = "대본 아웃라인이 승인 대기 중입니다.";
    }
  } catch (_error) {
    // 라우트가 없는 구 배포에서는 조용히 넘어간다.
  }
}

function remainingEpisodes(job) {
  return (job.episodes || []).filter((ep) => ep.status !== "done").length;
}

/* 만들어진 회차 수. 대본 전체 PDF 버튼을 보일지 정한다.

   아직 진행 중이어도 이미 만든 회차는 받을 수 있게 한다 — 12화 중 5화까지 나왔으면
   그 5화가 문서다. 서버도 같은 규칙으로 done 회차만 묶는다. */
function madeEpisodes(job) {
  return (job.episodes || []).filter((ep) => ep.status === "done").length;
}

/* 이어서 만들 수 있는 조건: 실패했고, 승인한 아웃라인이 남아 있고, 남은 회차가 있다.
   서버의 /resume 가 같은 조건을 다시 검사한다 — 화면 판단만 믿지 않는다. */
function canResumeScript(job) {
  return job.status === "failed" && !!job.outlineKey && remainingEpisodes(job) > 0;
}

function renderScriptJob(job, progress) {
  let host = $("#scriptJobPanel");
  if (!host) {
    host = document.createElement("div");
    host.id = "scriptJobPanel";
    $("#storyOutputList").insertAdjacentElement("beforebegin", host);
  }

  const statusLabel = {
    planning: "아웃라인 생성 중", awaiting: "승인 대기", writing: "회차 생성 중",
    ready: "완료", failed: "실패"
  }[job.status] || job.status;

  host.innerHTML = `
    <div class="story-output">
      <div class="story-output-head">
        <span class="doc-chip ${job.status === "ready" ? "ready" : job.status === "failed" ? "failed" : "generating"}">${escapeHtml(statusLabel)}</span>
        <strong>대본 ${job.episodeFrom}~${job.episodeTo}화</strong>
        ${storyDriftChip(job.story)}
        <span class="ai-badge">AI 생성</span>
      </div>
      <p class="story-output-meta">
        ${progress.done}/${progress.total}화 완료${progress.failed ? ` · ${progress.failed}화 실패` : ""}
        · 분량 ${escapeHtml(job.length)} · 품질 ${escapeHtml(job.quality)}
      </p>
      ${["writing", "planning"].includes(job.status) ? `<div class="job-bar"><i style="width:${progress.percent}%"></i></div>` : ""}
      ${job.error ? `<p class="doc-step-error">${escapeHtml(job.error)}</p>` : ""}
      ${storyDriftNote(job.story)}
      <div class="episode-list"></div>
      <div class="story-output-actions">
        ${job.status === "awaiting" ? '<button class="text-action" type="button" data-review>아웃라인 확인하고 승인</button>' : ""}
        ${canResumeScript(job) ? `<button class="text-action" type="button" data-resume>남은 ${remainingEpisodes(job)}화 이어서 만들기</button>` : ""}
        ${madeEpisodes(job) ? `<button class="text-action" type="button" data-pdf>대본 전체 PDF (${madeEpisodes(job)}화)</button>` : ""}
      </div>
    </div>`;

  const list = host.querySelector(".episode-list");
  (job.episodes || []).forEach((ep) => {
    const row = document.createElement("div");
    row.className = `episode-row ${ep.status === "done" ? "done" : ep.status === "failed" ? "failed" : ""}`;
    const review = ep.reviewStatus === "needs_review"
      ? '<span class="doc-chip failed">검수 확인</span>' : "";
    row.innerHTML = `
      <span class="episode-no">${ep.no}화</span>
      <strong>${escapeHtml(ep.title || "(제목 없음)")}</strong>
      ${review}
      <span class="field-hint">${ep.charCount ? `${ep.charCount.toLocaleString("ko-KR")}자` : ep.status === "pending" ? "대기" : ""}</span>`;

    if (ep.status === "done") {
      const view = document.createElement("button");
      view.type = "button";
      view.className = "text-action";
      view.textContent = "보기";
      view.addEventListener("click", () => openEpisode(job.id, ep.no));
      row.appendChild(view);
    }
    if (["done", "failed"].includes(ep.status) && job.status === "ready") {
      const again = document.createElement("button");
      again.type = "button";
      again.className = "text-action";
      again.textContent = "재생성";
      again.addEventListener("click", () => regenerateEpisode(job.id, ep.no));
      row.appendChild(again);
    }
    list.appendChild(row);
  });

  host.querySelector("[data-review]")?.addEventListener("click", async () => {
    try {
      const payload = await api(`/api/script-jobs/${encodeURIComponent(job.id)}`);
      showOutlineModal(outlineFromJob(payload.item), payload.item);
    } catch (error) {
      showToast("아웃라인을 불러오지 못했습니다", error.message, "error");
    }
  });

  host.querySelector("[data-resume]")?.addEventListener("click", (event) => resumeScript(job.id, event.target));

  /* 만들어진 회차를 한 PDF 로 묶어 받는다. 회차별 PDF 는 리더 안에 따로 있다. */
  host.querySelector("[data-pdf]")?.addEventListener("click", (event) => downloadPdf(
    `/api/script-jobs/${encodeURIComponent(job.id)}/pdf`,
    `${projectFilePrefix()}_대본.pdf`,
    event.currentTarget
  ));
}

/* 실패한 잡을 남은 회차부터 이어서 만든다.

   이 버튼이 없던 동안 사용자가 8화까지 만들어 놓고 실패하면 할 수 있는 일은 회차별
   재생성을 하나씩 누르는 것뿐이었다. 서버는 이미 done 회차를 건너뛸 수 있었는데
   그 경로를 부를 방법이 없었다. */
async function resumeScript(jobId, button) {
  if (button) button.disabled = true;
  try {
    const payload = await api(`/api/script-jobs/${encodeURIComponent(jobId)}/resume`, { method: "POST" });
    showToast("이어서 만들기", `남은 ${payload.remaining}화를 이어서 만듭니다.`);
    startScriptPolling(jobId, { openOutlineWhenReady: false });
  } catch (error) {
    showToast("이어서 만들지 못했습니다", error.message, "error");
  } finally {
    if (button) button.disabled = false;
  }
}

async function openEpisode(jobId, episodeNo) {
  try {
    const payload = await api(`/api/script-jobs/${encodeURIComponent(jobId)}/episodes/${episodeNo}`);
    const body = payload.payload?.payload || {};
    const episode = (state.story.scriptJob?.episodes || []).find((ep) => ep.no === episodeNo);
    const heading = `${episodeNo}화${body.title || episode?.title ? ` ${body.title || episode.title}` : ""}`;
    const meta = [
      "대본",
      state.currentProject?.title,
      episode?.charCount ? `${episode.charCount.toLocaleString("ko-KR")}자` : "",
      state.story.scriptJob?.story?.stale ? "생성 후 스토리 변경됨" : ""
    ].filter(Boolean).join(" · ");

    showStoryPayload("script", body, {
      title: heading,
      heading,
      meta,
      filename: `${projectFilePrefix()}_대본_${String(episodeNo).padStart(2, "0")}화`,
      // 대본은 txt 와 PDF 를 함께 준다. txt 는 다른 도구에 붙여 고치는 용도로 계속 쓰인다.
      pdfUrl: `/api/script-jobs/${encodeURIComponent(jobId)}/episodes/${episodeNo}/pdf`,
      pdfName: `${projectFilePrefix()}_대본_${String(episodeNo).padStart(2, "0")}화.pdf`
    });
  } catch (error) {
    showToast("회차를 불러오지 못했습니다", error.message, "error");
  }
}

async function regenerateEpisode(jobId, episodeNo) {
  if (!await confirmDialog({
    title: `${episodeNo}화 재생성`,
    lead: "이 회차만 다시 만듭니다.",
    body: "기존 회차는 대체됩니다. 다른 회차는 그대로 유지됩니다.",
    target: `${episodeNo}화`,
    okLabel: "재생성",
    tone: "warn"
  })) return;
  try {
    await api(`/api/script-jobs/${encodeURIComponent(jobId)}/episodes/${episodeNo}/regenerate`, {
      method: "POST"
    });
    // 202 다. 회차 하나가 최대 16,000 토큰이라 응답을 기다리면 브라우저가 먼저 끊긴다.
    // 완료 여부는 폴링으로 확인한다.
    showToast("재생성 시작", `${episodeNo}화를 다시 만들고 있습니다.`);
    startScriptPolling(jobId, { openOutlineWhenReady: false });
  } catch (error) {
    showToast("재생성하지 못했습니다", error.message, "error");
  }
}

/* ------------------------------ 배선 ------------------------------ */
/* 요소가 없는 화면에서도 스크립트가 죽지 않게 옵셔널 체이닝을 쓴다.
   이 파일 앞쪽에서 같은 실수(비옵셔널 $())가 화면 전체를 죽인 적이 있다. */
$("#storySummary")?.addEventListener("input", updateStoryCounts);
$("#storyText")?.addEventListener("input", () => {
  updateStoryCounts();
  syncStoryActions();
});
$("#storySave")?.addEventListener("click", saveStory);
$("#storySummaryAuto")?.addEventListener("click", autoSummarize);
$$("#storyTypeOptions button").forEach((button) => {
  button.addEventListener("click", () => {
    $$("#storyTypeOptions button").forEach((b) => b.classList.remove("active"));
    button.classList.add("active");
    $("#storyTypeBadge").textContent = STORY_TYPE_LABELS[button.dataset.value] || "타입 미선택";
    syncStoryActions();
  });
});

$("#storyDropzone")?.addEventListener("click", () => $("#storyFile")?.click());
$("#storyFile")?.addEventListener("change", (event) => {
  addStoryImages(event.target.files);
  event.target.value = "";
});
["dragenter", "dragover"].forEach((type) =>
  $("#storyDropzone")?.addEventListener(type, (event) => {
    event.preventDefault();
    $("#storyDropzone").classList.add("dragging");
  })
);
["dragleave", "drop"].forEach((type) =>
  $("#storyDropzone")?.addEventListener(type, (event) => {
    event.preventDefault();
    $("#storyDropzone").classList.remove("dragging");
    if (type === "drop") addStoryImages(event.dataTransfer?.files);
  })
);

// [로그라인]·[대본]은 곧바로 진행한다. 콘티 옵션이 남아 있으면 접는다.
$("#makeLogline")?.addEventListener("click", () => {
  setStoryCutPanel(false);
  makeStoryOutput("logline");
});
// [콘티]는 컷 수를 먼저 정해야 하므로 옵션을 펼치는 것으로 바꿨다. 실제 생성은 아래 [콘티 만들기].
$("#makeStoryboard")?.addEventListener("click", () => {
  setStoryCutPanel(!state.story.cutOpen);
  if (state.story.cutOpen) {
    $("#storyCutOptions")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
});
$("#makeScript")?.addEventListener("click", () => {
  setStoryCutPanel(false);
  openScriptModal();
});
/* 결과물 만들기 > 영상 (설계자 2026-10-08). 서버가 이 프로젝트의 스토리 · 첨부 이미지 · 바이블 캐릭터로
   영상 계획을 만들고(① 입력이 채워짐) 그 계획을 바로 연다. 첨부 이미지는 복사만 되고 원본은 그대로다. */
$("#makeVideo")?.addEventListener("click", async () => {
  const project = state.currentProject;
  if (!project?.id) return;
  setStoryCutPanel(false);
  const button = $("#makeVideo");
  button.disabled = true;
  try {
    const url = `/api/projects/${encodeURIComponent(project.id)}/video-plan-from-story`;
    let payload = await api(url, { method: "POST", body: JSON.stringify({}) });
    // 중간에 멈춘 계획이 있으면 서버가 그 계획을 돌려준다(설계자 2026-10-13). 이어서 할지 묻는다.
    if (payload.resumed) {
      const stepNames = { 1: "① 입력", 2: "② 확인", 3: "③ 계획 승인", 4: "④ 영상" };
      const resume = window.confirm(
        `진행 중인 영상 계획이 있습니다(${stepNames[payload.currentStep] || "① 입력"} 단계).
` +
        "[확인] 이어서 진행  ·  [취소] 처음부터 새로 만들기"
      );
      if (resume) {
        showToast("이어서 진행합니다", `${stepNames[payload.currentStep] || "① 입력"} 단계부터 계속합니다.`);
        window.whynotOpenVideoGen(project, payload.planId);
        return;
      }
      payload = await api(url, { method: "POST", body: JSON.stringify({ fresh: true }) });
    }
    showToast(
      "영상 계획을 만들었습니다",
      `캐릭터 ${payload.characters}명 · 이미지 ${payload.images}장을 ① 입력에 넣었습니다. 확인하고 분석을 시작하세요.`
    );
    window.whynotOpenVideoGen(project, payload.planId);
  } catch (error) {
    showToast("영상 계획을 만들지 못했습니다", error.message, "error");
  } finally {
    button.disabled = false;
  }
});
$("#storyCutClose")?.addEventListener("click", () => setStoryCutPanel(false));
$("#storyCutRun")?.addEventListener("click", () => makeStoryOutput("storyboard"));
$("#outputsRefresh")?.addEventListener("click", () => {
  loadStoryOutputs(state.story.projectId);
  loadScriptJobs(state.story.projectId);
});

$("#scriptModalClose")?.addEventListener("click", closeScriptModal);
$("#scriptCancel")?.addEventListener("click", closeScriptModal);
$("#scriptSubmit")?.addEventListener("click", submitScriptJob);
["#scriptFrom", "#scriptTo"].forEach((selector) =>
  $(selector)?.addEventListener("input", refreshScriptEstimate)
);
["#scriptLengthOptions", "#scriptFormatOptions", "#scriptQualityOptions"].forEach((group) => {
  $$(`${group} button`).forEach((button) => {
    button.addEventListener("click", () => {
      $$(`${group} button`).forEach((b) => b.classList.remove("active"));
      button.classList.add("active");
      refreshScriptEstimate();
    });
  });
});

$("#outlineClose")?.addEventListener("click", () => $("#outlineModal").classList.add("is-hidden"));
$("#outlineReject")?.addEventListener("click", () => $("#outlineModal").classList.add("is-hidden"));
$("#outlineApprove")?.addEventListener("click", approveOutline);

// 콘티 옵션은 [콘티]를 고른 동안에만 보인다. setStoryCutPanel() 이 관리한다.
setStoryCutPanel(false);

/* =========================================================================
   기능 플래그

   지시서 0절: "롱폼·숏폼·영상 진입점은 feature flag로 숨김(코드 삭제 아님)".
   마크업을 지우지 않는 이유는 이 파일 앞쪽 배선이 비옵셔널 $() 를 쓰기 때문이다 —
   요소 하나가 없어지면 그 뒤의 addEventListener 가 전부 등록되지 않아 화면이 통째로
   죽는다. 그래서 배선이 모두 끝난 이 지점에서 클래스만 붙인다.

   되살리는 방법: 아래 FEATURES 값을 true 로 바꾸면 된다. 다른 곳은 손대지 않는다.
   ========================================================================= */

/* 이 병합본의 기본값은 Overlay 와 반대로 **전부 켠다.**
   Overlay 는 세 기능을 감췄지만 그것은 기능 제거이고, 이 저장소의 병합 규칙은 추가·수정만
   반영한다(PRD H2·H3). Base 에 세 기능의 코드·라우트·화면이 모두 살아 있으므로 끄면
   있는 기능을 잃는다. 감추고 싶으면 값만 false 로 바꾼다 — markup·API·핸들러는 그대로다.

   주의: 빌드 타임 상수라 값을 바꾸면 프론트엔드 이미지를 다시 빌드해야 한다. */
const FEATURES = {
  // 영상 생성. 이름을 gpuVideo 에서 videoGen 으로 바꿨다(2026-09-09, D-S01) —
  // 자체 GPU(Wan 2.2) 경로 개발을 중단하고 Seedance 2.0(SaaS) 로 전환했기 때문이다.
  // Base 의 /api/v2/video/* 와 wan_client 는 GPU 트랙 코드로 보존만 하고 쓰지 않는다.
  // 배포 환경별 runtime-config.js가 개발 preview를 열고 운영에서는 닫는다.
  videoGen: VIDEO_GEN_ENABLED,
  // 롱테이크(롱폼). Base 의 stitcher.py 경로가 살아 있다.
  longForm: true,
  // 옛 진입점 선택(웹툰/소재/트리트먼트/논의된 대본).
  // 와이낫미디어는 영상 기능만 쓰므로 끈다. 새 프로젝트 모달의 그리드는 index.html 에서
  // 지웠고, 상세 화면의 배지·전환 버튼은 마크업에 남겨 두고 이 플래그로 감춘다 —
  // renderSourcePanel 이 #detailInputTypeBadge 를 null-guard 없이 읽기 때문이다.
  legacyInputType: false,
  // 스토리 트랙은 runtime config로 영상 트랙과 독립적으로 켜고 끈다.
  // 코드·라우트는 그대로 두고 track dispatcher와 서버 guard가 접근을 일치시킨다.
  storyFeatures: STORY_DEV_ENABLED
};

function applyFeatureFlags() {
  const hide = (selector) => $$(selector).forEach((node) => node.classList.add("is-hidden"));
  const noTrackAvailable = !STORY_DEV_ENABLED && !VIDEO_GEN_ENABLED;
  if ($("#newProjectButton")) {
    $("#newProjectButton").disabled = noTrackAvailable;
    $("#newProjectButton").title = noTrackAvailable
      ? "스토리 개발과 영상 개발이 모두 꺼져 있습니다."
      : "";
  }
  $("#projectTrackConfigError")?.classList.toggle("is-hidden", !noTrackAvailable);

  if (!FEATURES.videoGen) {
    hide("#videoGenView");
    hide("#handoffSceneVideo");
    hide("#folderCharacterAdd");
    hide("#folderSceneAdd");
  }
  if (!FEATURES.longForm) {
    hide("#longTakeBlock");
  }
  if (!FEATURES.legacyInputType) {
    // 지시서 3절 "웹툰/트리트먼트/이미지 선택 구간 삭제".
    // inputType 자체는 데이터 모델에 남아 기존 AI 작업 단계가 계속 동작한다.
    hide("#detailInputTypeOptions");
    hide("#detailInputTypeBadge");
  }

  // 새 흐름(스토리 → 결과물)을 옛 패널보다 위로 올린다. 두 곳 모두 원문을 붙여넣는
  // 칸이 있어서, 순서가 뒤바뀌면 사용자가 옛 '원본 자료'에 스토리를 적는다.
  const anchor = $("#sourcePanel");
  const story = $("#storyPanel");
  const outputs = $("#outputsPanel");
  if (anchor && story && outputs && anchor.parentNode) {
    const parent = anchor.parentNode;
    parent.insertBefore(story, anchor);
    parent.insertBefore(outputs, anchor);

    // 옛 구획(원본 자료 · 기획 방향 · AI 작업)은 지우지 않는다. /api/projects/<id>/source,
    // /direction, /documents 라우트가 그대로 살아 있고 화면 배선도 붙어 있어 지금도 쓸 수 있다.
    // 다만 새 흐름을 가리지 않게 접이식으로 감싸고 기본은 닫아 둔다. 요소가 DOM 에 남아 있어야
    // 위쪽 비옵셔널 $() 배선이 깨지지 않는다.
    if (!document.getElementById("legacySection")) {
      const box = document.createElement("details");
      box.id = "legacySection";
      box.className = "legacy-section";

      const head = document.createElement("summary");
      head.className = "legacy-summary";
      const title = document.createElement("b");
      title.textContent = "이전 방식의 작업 구획";
      const hint = document.createElement("small");
      hint.textContent =
        "원본 자료 · 기획 방향 · AI 작업 — 로그라인 · 콘티 · 대본은 위의 스토리 구획에서 만듭니다.";
      head.appendChild(title);
      head.appendChild(hint);
      box.appendChild(head);

      const body = document.createElement("div");
      body.className = "legacy-body";
      parent.insertBefore(box, anchor);
      [anchor, $("#directionPanel"), $("#documentsPanel")].forEach((panel) => {
        if (panel) body.appendChild(panel);
      });
      box.appendChild(body);
    }
  }

  /* 영상 전용 모드. 스토리 작업 구획을 전부 감추고 프로젝트를 폴더로 만든다.

     위의 재배치·접이식 묶기를 먼저 끝낸 뒤에 감춘다. 순서가 중요하다 —
     legacySection 이 만들어진 다음에 그것을 감춰야 원본 자료·기획 방향·AI 산출물이
     한 번에 사라진다. 요소는 DOM 에 그대로 남아 있어 배선은 살아 있다. */
  if (!FEATURES.storyFeatures) {
    // 스토리 흐름 구획.
    hide("#storyPanel");
    hide("#outputsPanel");
    hide("#legacySection");
    // 진행률은 원본 자료 → 기획 방향 → AI 산출물 → 제작 네 단계라 영상 전용에서는
    // 전부 빈 칸으로 남는다. renderProgress 는 계속 돌지만 보이지 않는다
    // (#progressDocCount 가 DOM 에 남아 있어야 그 함수가 죽지 않는다).
    hide("#progressTrack");
    // 핸드오프 카드 넷 중 스토리를 재료로 쓰는 셋을 감춘다.
    // '새로운 영상 만들기'(#handoffSceneVideo) 만 남긴다.
    hide("#handoffShort");
    hide("#handoffStoryboard");
    hide("#handoffLong");
    // 폴더 구획을 드러낸다.
    $("#projectFolderPanel")?.classList.remove("is-hidden");
  }
}

applyFeatureFlags();
