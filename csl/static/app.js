"use strict";

// ------------------------------------------------------------------ 상태
const S = {
  sessions: [],
  boards: [],              // 세션이 아닌 보드 칸(터미널·마크다운) — csl/boards.py
  sugg: new Map(),         // session_id → { key: state_at, text } 터미널 입력칸에 보이는 제안
  midOpen: new Set(),      // 앞선 중간 메시지를 펼친 턴 id
  settings: {},            // 서버에 저장되는 설정(auto_remote)
  prev: null,              // 직전 폴링의 세션별 상태 (알림 비교용)
  activeCol: null,         // 단축키로 다루는 「지금 칸」(session_id)
  expanded: new Set(),     // 펼친 긴 응답(turn id)
  promptOpen: new Set(),   // 펼친 긴 요청(turn id)
  trailOpen: new Set(),    // 작업 과정을 펼친 턴(turn id)
  recapOpen: new Set(),    // recap 전문을 펼친 세션(session_id)
  notesFolded: new Set(load("csl.notesFolded", [])),  // 넥스트 액션을 접어 둔 세션(session_id, 브라우저에 기억)
  drafts: persistMap("csl.drafts"),   // session_id → 쓰던 메시지(새로고침해도 남게 브라우저에 기억)
  compose: new Map(),      // session_id → 첨부한 이미지 [{ name, data(data URL) }]
  renaming: null,          // 이름을 고치는 중인 session_id
  uiVer: 0,                // 펼치기 등 화면 토글이 바뀔 때마다 증가(칸 다시 그리기 판단용)
  visibleCols: new Set(),  // 보드에서 화면에 보이는(또는 곧 보일) 칸의 session_id
  trail: new Map(),        // turn id → 작업 과정 항목(끝난 턴)
  live: new Map(),         // session_id → { since, items, key } 진행 중 턴의 작업 과정
  editingNote: false,
  pollSeq: 0,              // 보낸 상태 요청 번호
  pollShown: 0,            // 화면에 반영한 가장 새 요청 번호
  noteFocus: null,         // 키보드로 고른 넥스트 액션 항목(note id) — 다시 그린 뒤 포커스 복원용
  cmds: new Map(),         // session_id → 슬래시 명령 목록(입력창 자동완성)
  colWidths: load("csl.colWidths", {}),   // session_id → 사용자가 끌어서 정한 칸 너비(px)
  winBlurEl: null,         // 창이 포커스를 잃을 때의 포커스 요소(돌아왔을 때 다시 오는 focusin 구분용)
  stepOpen: new Set(),     // 펼쳐 본 도구 호출(작업 과정)의 키
  noteImgs: new Map(),     // session_id → 넥스트 액션 입력칸에 붙인 이미지 [{ name, data }]
  showEnded: load("csl.showEnded", false),
  notify: load("csl.notify", true),
  sound: load("csl.sound", true),
  boardCache: new Map(),   // session_id → { sig, detail }
  boardOrder: "",
  boardLimit: 12,
  colLimit: new Map(),     // session_id → 그 칸에 불러올 턴 수(「이전 대화 더 보기」로 늘어남)
  anchor: new Map(),       // session_id → 이전 대화를 더 불러올 때 읽던 위치를 지키기 위한 { h, top }
};

const STATE_LABEL = {
  waiting: "확인 필요", working: "작업 중", unread: "새 응답", error: "오류",
  done: "완료", idle: "대기", ended: "종료",
};
const BOARD_TURNS = 40;
const BELL_SVG = '<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M12 22a2.5 2.5 0 0 0 2.45-2h-4.9A2.5 2.5 0 0 0 12 22Zm7-6V11a7 7 0 0 0-5.5-6.84V3.5a1.5 1.5 0 0 0-3 0v.66A7 7 0 0 0 5 11v5l-2 2v1h18v-1l-2-2Z"/></svg>';
const LONG_RESPONSE = 1600;   // 이보다 긴 응답은 접어서 보여 준다(글자 수)

const $ = (sel, root = document) => root.querySelector(sel);

function load(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); }
  catch (e) { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* 저장 불가 환경 */ }
}
// 글자 크기(설정 → 화면): 화면 전체 배율과 대화 배율. 이 브라우저에만 기억하고 바로 적용한다
const FONT_STEPS = [0.85, 0.9, 1, 1.1, 1.2, 1.3, 1.4];
function applyFontScale() {
  const r = document.documentElement.style;
  const ui = load("csl.uiFont", 1), chat = load("csl.chatFont", 1);
  r.setProperty("--ui-fs", FONT_STEPS.includes(ui) ? ui : 1);
  r.setProperty("--chat-fs", FONT_STEPS.includes(chat) ? chat : 1);
}
applyFontScale();
// 브라우저에 기억하는 Map(입력창 초안). 칠 때마다 쓰지 않고 입력이 0.5초 멈추면 한 번, 페이지를 떠날 때 한 번 저장한다
// — 타이핑이 느려지지 않게. 빈 글은 버리고, 최근 30개 세션·한 개 2만 자까지만(이미지 첨부는 남기지 않는다)
function persistMap(key) {
  const m = new Map((load(key, []) || []).filter(x => Array.isArray(x) && typeof x[0] === "string" && typeof x[1] === "string"));
  const set = m.set.bind(m), del = m.delete.bind(m);
  let timer = 0;
  const flush = () => {
    clearTimeout(timer);
    timer = 0;
    save(key, [...m].filter(([, v]) => typeof v === "string" && v.trim()).slice(-30).map(([k, v]) => [k, v.slice(0, 20000)]));
  };
  const later = () => { clearTimeout(timer); timer = setTimeout(flush, 500); };
  m.set = (k, v) => { del(k); set(k, v); later(); return m; };   // 다시 넣어 최근 순으로
  m.delete = k => { const had = del(k); if (had) later(); return had; };
  window.addEventListener("pagehide", () => { if (timer) flush(); });
  return m;
}

async function api(method, path, body, headers) {
  const opt = { method, headers: Object.assign({}, headers) };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  if (!res.ok) {
    let msg = method + " " + path + " → " + res.status, data = {};
    try { data = await res.json() || {}; msg = data.error || msg; } catch (e) { /* 본문 없음 */ }
    const err = new Error(msg);
    err.data = data;   // 서버가 덧붙인 것(예: resume — 새 탭에서 이어 열기를 물어볼 세션)
    throw err;
  }
  return res.json();
}

// 처리하지 않은 요청 실패(보관·고정·설정 저장 등)는 조용히 묻히지 않게 알린다
window.addEventListener("unhandledrejection", e => {
  const msg = e.reason && e.reason.message;
  if (msg && typeof toast === "function") toast(msg);
});

// ------------------------------------------------------------------ 표시 도우미
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function clamp(s, n) {
  s = String(s || "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
function stripMd(s) {
  return s
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/^#+\s*/gm, "");
}
function respSummary(text, n = 240) {
  if (!text) return "";
  const blocks = text.replace(/\r/g, "").split(/\n\s*\n/);
  for (const b of blocks) {
    const t = b.trim();
    if (!t || /^(#|\||```|---|>|<)/.test(t)) continue;
    return clamp(stripMd(t), n);
  }
  return clamp(stripMd(text), n);
}
function promptSummary(text, n = 120) {
  return clamp(text, n);
}
function sessionTitle(s) {
  // 팀원의 첫 요청은 팀장이 보낸 <teammate-message …> 지시문이라 제목으로 쓰지 않고 팀원 이름으로
  if (!s.title && s.team_agent) return "👥 " + s.team_agent;
  return s.title || promptSummary(s.first_prompt, 60) || "(아직 요청 없음)";
}
function displayState(s) {
  if (s.state === "waiting") return "waiting";
  if (s.state === "working") return "working";
  if (s.state === "error") return "error";
  if (s.state === "ended" || s.archived) return "ended";
  if (s.unread) return "unread";
  return s.last_response ? "done" : "idle";
}
function rel(ts) {
  if (!ts) return "";
  const d = Date.now() / 1000 - ts;
  if (d < 45) return "방금";
  if (d < 3600) return Math.round(d / 60) + "분 전";
  if (d < 86400) return Math.round(d / 3600) + "시간 전";
  if (d < 86400 * 2) return "어제";
  return dateLabel(ts);
}
function hm(ts) {
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}
function dateLabel(ts) {
  const d = new Date(ts * 1000);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((today - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
  if (diff === 0) return "오늘";
  if (diff === 1) return "어제";
  return (d.getMonth() + 1) + "/" + d.getDate() + " (" + "일월화수목금토"[d.getDay()] + ")";
}
function dur(sec) {
  if (sec == null || sec < 0) return "";
  if (sec < 60) return Math.round(sec) + "초";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  if (m < 60) return m + "분" + (s ? " " + s + "초" : "");
  return Math.floor(m / 60) + "시간 " + (m % 60) + "분";
}
function base(p) { return p ? p.replace(/\/+$/, "").split("/").pop() : ""; }
// 프로젝트별 색: 처음 본 순서대로 겹치지 않게 배정하고 브라우저에 기억한다
const PROJECT_COLORS = ["#2f6fde", "#2f9e66", "#c96442", "#8a5cd6", "#0e9aa7", "#d6457d", "#b8860b", "#5b7c99", "#6a8f2a", "#d0453a"];
const projColorMap = load("csl.projColors", {});
function projColor(root) {
  if (!root) return "var(--text-3)";
  if (!(root in projColorMap)) {
    const used = new Set(Object.values(projColorMap));
    let i = 0;
    while (used.has(i) && i < PROJECT_COLORS.length) i++;
    projColorMap[root] = i < PROJECT_COLORS.length ? i : [...root].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7) % PROJECT_COLORS.length;
    save("csl.projColors", projColorMap);
  }
  return PROJECT_COLORS[projColorMap[root]];
}
// 워크트리 표시 이름. 폴더명이 프로젝트명과 같으면(예: ~/.codex/worktrees/<이름>/<프로젝트>) 상위 폴더명을 쓴다
function wtName(s) {
  if (!s.worktree) return "";
  const name = base(s.worktree);
  return name === s.project_name ? base(s.worktree.replace(/\/[^/]+\/?$/, "")) : name;
}
// 워크트리·브랜치 칩. 워크트리 이름이 브랜치와 사실상 같으면 칩 하나로 합친다
// 커밋 안 한 변경이 있으면 칸 머리에 변경 보기 버튼(서버가 20초마다 확인해 둔 값) — 누르면 /diff 창
const dirtySig = s => s.dirty ? [s.dirty.files, s.dirty.new, s.dirty.add, s.dirty.del].join(":") : "";
function kNum(n) { return n >= 10000 ? Math.round(n / 1000) + "k" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n); }
function diffChip(s) {
  const x = s.dirty;
  if (!x || !x.files) return "";
  const nw = x.new || 0, changed = x.files - nw;
  const lines = x.add || x.del ? `<span class="dc-a">+${kNum(x.add)}</span> <span class="dc-d">−${kNum(x.del)}</span>` : changed ? `파일 ${changed}` : "";
  const body = [lines, nw ? `N ${nw}` : ""].filter(Boolean).join(" · ");   // N = 새 파일(언어와 상관없는 표기)
  return `<button class="chip diff-chip" data-action="open-diff" data-sid="${esc(s.session_id)}"
    title="커밋 안 한 변경 — 바뀐 파일 ${changed}개(+${x.add} −${x.del})${nw ? ` · 새 파일 ${nw}개` : ""} · 눌러서 보기 ( /diff )">🧾 ${body}</button>`;
}
function placeChips(s, full) {
  const norm = x => String(x || "").toLowerCase().replace(/[^a-z0-9가-힣]/g, "");
  const wt = wtName(s);
  const br = s.branch ? (full ? s.branch : clamp(s.branch, 34)) : "";
  if (wt && br && (norm(wt) === norm(br) || norm(br).endsWith(norm(wt)))) {
    return `<span class="chip wt" title="워크트리 ${esc(s.worktree)}">🌿 ${esc(br)}</span>`;
  }
  return (wt ? `<span class="chip wt" title="워크트리 ${esc(s.worktree)}">🌿 ${esc(wt)}</span>` : "") +
    (br ? `<span class="chip br" title="브랜치">${esc(br)}</span>` : "");
}

// ------------------------------------------------------------------ 마크다운
// 이스케이프된 글 속 로컬 파일 경로(확장자로 끝나는 절대 경로·~/)를 누르면 열리는 링크로.
// 앞 글자가 단어·/·: 이면 URL 의 일부라 건너뛰고, 뒤의 문장부호·조사는 경로에서 뺀다. <폴더>·* 같은 자리표시 경로는 제외
const PATH_RE = /(?<![\w\/:.~-])((?:~|\/(?:Users|private|var|tmp|Volumes|opt|Applications))\/[^\s<>"'`|&*]*?\.[A-Za-z0-9]{1,8})(?=[.,:;!?]*(?:$|[\s<>"'`|&)\]}]|[가-힣]))/g;
function linkifyPaths(html) {
  return html.replace(PATH_RE, p => `<a class="fpath" href="#" data-action="open-file" data-path="${p}" title="열기 — 이미지·음성·영상·문서는 기본 앱, HTML 은 브라우저 새 탭 (⌥ 누르고 클릭: Finder 에서 보기)">${p}</a>` +
    `<button class="fpath-reveal" data-action="reveal-file" data-path="${p}" title="Finder 에서 보기">📂</button>`);
}
// 이스케이프된 글 속 맨 URL(http·https)을 새 탭 링크로. 바로 뒤에 붙은 조사(…:5114로)·문장부호·닫는 괄호는 주소에서 뺀다
const URL_RE = /(?<![\w"'=\/])https?:\/\/[^\s<>"'`\u0000]+/g;
function linkifyUrls(html, hold = x => x) {
  return html.replace(URL_RE, m => {
    let url = m;
    for (let prev = ""; prev !== url;) {
      prev = url;
      url = url.replace(/(?:[.,;:!?'"」』\]}]|[가-힣]|&gt;|&quot;|&#39;)+$/, "");
      // 닫는 괄호는 여는 괄호보다 많을 때만 뺀다(…/Foo_(bar) 는 주소의 일부)
      if (url.endsWith(")") && (url.match(/\(/g) || []).length < (url.match(/\)/g) || []).length) url = url.slice(0, -1);
    }
    if (!/^https?:\/\/[^/\s]+/.test(url)) return m;
    return hold(`<a class="ext" href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`) + m.slice(url.length);
  });
}
// [{indent, ordered, num, html}] → 중첩 목록 HTML. 더 들여 쓴 항목은 앞 항목 안의 하위 목록, 같은 깊이에서 종류가 바뀌면 새 목록
function listHtml(items) {
  let html = "";
  const stack = [];   // 열린 목록들 {indent, tag}
  const open = it => {
    const tag = it.ordered ? "ol" : "ul";
    stack.push({ indent: it.indent, tag });
    return `<${tag}${it.ordered && it.num > 1 ? ` start="${it.num}"` : ""}>`;
  };
  for (const it of items) {
    while (stack.length > 1 && it.indent < stack[stack.length - 1].indent) html += `</li></${stack.pop().tag}>`;
    const top = stack[stack.length - 1];
    if (!top || it.indent > top.indent) html += open(it);
    else if ((it.ordered ? "ol" : "ul") !== top.tag) html += `</li></${stack.pop().tag}>` + open(it);
    else html += "</li>";
    html += "<li>" + it.html;
  }
  while (stack.length) html += `</li></${stack.pop().tag}>`;
  return html;
}
function inline(t) {
  // 코드·링크는 자리표시(\u0000N\u0000)로 빼 두었다가 마지막에 넣는다 — 파일 경로 링크가 링크 주소(href) 안까지 바꿔
  // 링크가 깨지던 것. 자리표시 문자(NUL)는 md() 가 원문에서 미리 지운다
  const keep = [];
  const hold = html => { keep.push(html); return "\u0000" + (keep.length - 1) + "\u0000"; };
  const put = x => x.replace(/\u0000(\d+)\u0000/g, (_, i) => put(keep[+i] ?? ""));
  t = esc(t).replace(/`([^`]+)`/g, (_, c) => hold("<code>" + linkifyUrls(linkifyPaths(c)) + "</code>"));
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (m, label, url) => url.includes("\u0000") ? m
      : hold(`<a class="ext" href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`));
  t = linkifyUrls(t, hold);   // [이름](주소) 를 뺀 나머지 맨 주소
  return put(linkifyPaths(t));
}
// 같은 응답을 2초마다 다시 변환하지 않도록 결과를 기억한다
const MD_CACHE = new Map();
function mdCached(text) {
  let html = MD_CACHE.get(text);
  if (html === undefined) {
    html = md(text);
    MD_CACHE.set(text, html);
    if (MD_CACHE.size > 1500) MD_CACHE.delete(MD_CACHE.keys().next().value);
  }
  return html;
}
// ``` 코드 블록 — 들여 쓴 것도(목록 항목 안의 코드 블록은 항목 글만큼 들여 쓴다). 줄마다 여는 ``` 만큼의 들여쓰기를 걷는다.
// [html, 다음 줄 번호]
const FENCE = /^(\s*)```/;
function fenceBlock(L, i) {
  const ind = L[i].match(FENCE)[1].replace(/\t/g, "    ").length;
  const lang = L[i].trim().slice(3).trim();
  const strip = new RegExp("^ {0," + ind + "}");
  const buf = []; i++;
  while (i < L.length && !/^\s*```\s*$/.test(L[i])) buf.push(L[i++].replace(strip, ""));
  i++;
  // 코드 블록 오른쪽 위 「복사」(가로로 스크롤돼도 버튼은 제자리)
  return [`<div class="code-wrap"><button class="code-copy" data-action="copy-code" title="코드 복사">복사</button>` +
    `<pre${lang ? ` data-lang="${esc(lang)}"` : ""}><code>` + esc(buf.join("\n")) + "</code></pre></div>", i];
}
// opts.tasks: 체크리스트(- [ ] / - [x])를 누를 수 있는 체크박스로(마크다운 칸의 보기). 아니면 눌리지 않는 체크박스로 보인다
function md(src, opts) {
  const tasks = !!(opts && opts.tasks);
  const L = String(src || "").replace(/[\r\u0000]/g, "").split("\n");   // NUL 은 inline 의 자리표시와 겹친다
  const out = [];
  let i = 0;
  // 구분선(--- ***)도 문단을 끊는다 — 글 바로 아래 줄의 --- 가 글자로 남던 것
  const isBlockStart = l => /^\s*```|^#{1,6}\s|^\s*[-*+]\s|^\s*\d+[.)]\s|^>|^\s*\||^\s*(-{3,}|\*{3,})\s*$/.test(l);
  while (i < L.length) {
    const l = L[i];
    if (FENCE.test(l)) {
      const [html, next] = fenceBlock(L, i);
      out.push(html);
      i = next;
      continue;
    }
    if (!l.trim()) { i++; continue; }
    const h = l.match(/^(#{1,6})\s+(.*)$/);
    if (h) { const n = Math.min(h[1].length + 1, 4); out.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(l)) { out.push("<hr>"); i++; continue; }
    if (/^\s*\|/.test(l) && i + 1 < L.length && /^\s*\|?\s*:?-{2,}/.test(L[i + 1])) {
      // 칸 구분은 이스케이프하지 않은 | 만 — 칸 안의 \| (코드 속 파이프 등)는 글자 | 로
      const cells = r => r.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "").split(/(?<!\\)\|/)
        .map(c => inline(c.trim().replace(/\\\|/g, "|")));
      let html = "<table><thead><tr>" + cells(l).map(c => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>";
      i += 2;
      while (i < L.length && /^\s*\|/.test(L[i])) html += "<tr>" + cells(L[i++]).map(c => "<td>" + c + "</td>").join("") + "</tr>";
      out.push(html + "</tbody></table>");
      continue;
    }
    const listRe = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
    if (listRe.test(l)) {
      // 들여쓰기 깊이대로 진짜 중첩 목록으로 — 「1. … / 3칸 들여 쓴 - …」 의 글머리표가 바깥 번호 목록의 항목이 되어
      // 번호가 이어지고(2·3) 바깥 번호가 밀리던(4·5) 것. 수준마다 번호·글머리표를 따로, 3. 으로 시작하면 3부터
      const items = [];
      while (i < L.length) {
        if (items.length && /^\s+```/.test(L[i])) {   // 항목 아래 들여 쓴 코드 블록은 그 항목 안에
          const [html, next] = fenceBlock(L, i);
          items[items.length - 1].html += html;
          i = next;
          continue;
        }
        if (!(listRe.test(L[i]) || (/^\s{2,}\S/.test(L[i]) && !isBlockStart(L[i].trim())))) break;
        const m = L[i].match(listRe);
        const task = m && m[3].match(/^\[([ xX])\]\s+(.*)$/);   // 체크리스트 항목 — data-line 은 원문의 줄 번호(눌러서 바꿀 때)
        if (task) items.push({ indent: m[1].replace(/\t/g, "    ").length, ordered: /\d/.test(m[2]), num: parseInt(m[2], 10),
          html: `<input type="checkbox" class="md-task" data-line="${i}"${task[1] !== " " ? " checked" : ""}${tasks ? "" : " disabled"}> ` + inline(task[2]) });
        else if (m) items.push({ indent: m[1].replace(/\t/g, "    ").length, ordered: /\d/.test(m[2]), num: parseInt(m[2], 10), html: inline(m[3]) });
        else items[items.length - 1].html += "<br>" + inline(L[i].trim());   // 항목 아래 들여 쓴 이어지는 줄
        i++;
      }
      out.push(listHtml(items));
      continue;
    }
    if (/^>/.test(l)) {
      const buf = [];
      while (i < L.length && /^>/.test(L[i])) buf.push(L[i++].replace(/^>\s?/, ""));
      out.push("<blockquote>" + buf.map(inline).join("<br>") + "</blockquote>");
      continue;
    }
    const buf = [];
    while (i < L.length && L[i].trim() && !isBlockStart(L[i])) buf.push(L[i++]);
    if (!buf.length) buf.push(L[i++]);
    out.push("<p>" + buf.map(inline).join("<br>") + "</p>");
  }
  return out.join("");
}

// ------------------------------------------------------------------ 칸으로 이동
// 보드에 아직 없는 칸(「더 보기」 밖)이면 칸 수를 늘려 꺼낸 뒤, 그 칸으로 스크롤하고 잠깐 강조한다
function ensureColVisible(sid) {
  const col = $(`#board .col[data-col="${CSS.escape(sid)}"]`);
  const board = $("#board");
  if (!col || !board || board.hidden) return;
  const c = col.getBoundingClientRect(), b = board.getBoundingClientRect();
  if (c.left >= b.left - 2 && c.right <= b.right + 2) return;
  // 칸이 보드 폭보다 넓으면 왼쪽 끝을 맞추고, 아니면 가장 덜 움직이는 쪽으로
  const delta = c.width > b.width || c.left < b.left ? c.left - b.left - 12 : c.right - b.right + 12;
  board.scrollLeft += delta;
}
// 갈래 가족의 한 세션으로 가려면(검색·활동·빠른 이동에서) 그 갈래를 칸의 탭에서 골라 둔다
function selectFamMember(sid) {
  const byId = new Map(S.sessions.map(s => [s.session_id, s]));
  const s = byId.get(sid);
  if (!s) return;
  const root = famRootOf(s, byId);
  const hasKids = S.sessions.some(m => isBranch(m) && famRootOf(m, byId) === root);
  if ((root !== sid || hasKids) && S.famSel[root] !== sid) {
    S.famSel[root] = sid;
    save("csl.famSel", S.famSel);
    renderBoard();
  }
}
async function focusCol(sid) {
  if (S.view === "board" && S.boardMode === "cards") setBoardMode("cols");   // 칸으로 가려면 칸 보기로
  selectFamMember(sid);
  const bs = brainSession();
  if (bs && bs.session_id === sid) {   // 브레인 세션은 칸이 아니라 브레인 패널에 있다
    setBrainOpen(true);
    showBrainTab("chat");
    await renderBrain();
    return $("#bpChat .col");
  }
  let idx = boardSessions().findIndex(s => s.session_id === sid);
  if (idx < 0 && S.projFilter && allBoardSessions().some(s => s.session_id === sid)) {
    setProjFilter("");   // 다른 프로젝트의 칸이면 필터를 풀고 간다
    toast("프로젝트 필터를 풀었습니다");
    idx = boardSessions().findIndex(s => s.session_id === sid);
  }
  if (idx < 0) { toast("보드에 없는 세션입니다(종료·보관됐거나 검색에 걸러짐)"); return null; }
  if (idx >= S.boardLimit) { S.boardLimit = idx + 1; renderBoard(); }
  const col = $(`.col[data-col="${CSS.escape(sid)}"]`);
  if (!col) return null;
  col.scrollIntoView({ inline: "nearest", block: "nearest", behavior: "smooth" });
  // 부드러운 스크롤은 그 사이 칸이 다시 그려지면(다른 세션 갱신 등) 중간에 끊긴다 — 끝날 즈음 다시 보고 안 보이면 바로 맞춘다
  setTimeout(() => ensureColVisible(sid), 700);
  col.classList.remove("flash");
  void col.offsetWidth;
  col.classList.add("flash");
  setActive(sid);
  return col;
}

// ------------------------------------------------------------------ 폴링
async function poll() {
  let data;
  const seq = ++S.pollSeq;   // 주기·refresh·본 것 처리가 겹쳐 부른다 — 먼저 보낸 요청의 응답이 늦게 와도 되돌아가지 않게
  try {
    data = await api("GET", "/api/state?archived=" + (S.showEnded || S.reveal.size ? 1 : 0));
  } catch (e) {
    document.title = t("연결 끊김 · Argus");
    return;
  }
  if (seq < S.pollShown) return;
  S.pollShown = seq;
  S.sessions = data.sessions;
  S.boards = data.boards || [];   // 세션이 아닌 보드 칸(터미널·마크다운) — 칸 순서에는 세션과 섞인다
  S.settings = data.settings || {};
  if (!S.colsInit && S.settings.board_cols) { S.boardLimit = S.settings.board_cols; S.colsInit = true; }
  S.inboxNew = data.inbox_new || 0;
  S.inboxNewAt = data.inbox_new_at || [];
  renderRemoteBtn();
  renderProjBtn();
  renderUsage(data.usage);
  checkAlerts();
  const groups = stateGroups();
  renderCounters(groups);
  updateBadge(groups);
  // 편집칸이 다른 이유로 사라졌으면(blur 없이) 플래그가 남아 보드가 영영 멈추지 않게
  if (S.editingNote && !document.querySelector('.note-text[contenteditable="true"]')) S.editingNote = false;
  if (S.editingNote) return;
  await refreshBoard();
  await renderBrain();
  refreshViews();
  await refreshLive();
  watchSuggestions();
  syncPrompts();   // 확인 필요 칸의 터미널 선택 창(기다리지 않는다)
  syncAsks();      // 질문 카드가 떠 있는데 터미널엔 질문 창이 없으면 지운다(입력창이 막히지 않게)
  watchNewSession();
}

// ------------------------------------------------------------------ 터미널 입력칸의 제안(Claude Code prompt suggestion)
// 응답이 끝나고 몇 초 뒤 Claude Code 가 입력칸에 다음 요청을 흐리게 제안한다. 보드는 그 화면 줄을 읽어
// 비어 있는 입력창의 안내 문구로 보여 주고 Tab 으로 채운다. 제안과 터미널에 쳐 둔 글은 구분되지 않는다
const SUGGEST_DELAYS = [2500, 7000, 15000];
function watchSuggestions() {
  for (const s of S.sessions) {
    const d = displayState(s);
    if (d === "working" || d === "waiting" || d === "ended" || s.compact_at) continue;
    const cur = S.sugg.get(s.session_id);
    if (cur && cur.key === s.state_at) continue;
    S.sugg.set(s.session_id, { key: s.state_at, text: cur && cur.text });
    // 방금 끝난 응답이면 제안이 생길 때까지 몇 번, 오래된 것이면 한 번만
    const fresh = Date.now() / 1000 - (s.state_at || 0) < 20;
    for (const ms of fresh ? SUGGEST_DELAYS : [0]) setTimeout(() => fetchSuggestion(s.session_id, s.state_at), ms);
  }
}
async function fetchSuggestion(sid, key) {
  const cur = S.sugg.get(sid);
  if (!cur || (key !== undefined && cur.key !== key)) return;
  let r;
  try { r = await api("GET", `/api/sessions/${sid}/suggestion`); } catch (e) { return; }
  const now = S.sugg.get(sid);
  if (!now || now.key !== cur.key) return;
  now.text = r.text || null;
  now.at = Date.now();
  const col = colEl(sid), s = sessionById(sid);
  if (col && s) updateComposer(col.querySelector(".composer"), s);
}
function sigOf(s) {
  return [s.last_activity, s.state, s.state_at, s.open_notes, s.last_seen_at, s.title, s.recap_at,
    s.pinned, pinOf(s), s.archived, s.turn_count, s.remote, s.rc_requested_at, s.model, s.effort,
    s.ctx ? s.ctx.used + "/" + s.ctx.pct : "", s.compact_at, s.ask ? s.ask.length : 0, s.aside_sig].join("|");
}

// ------------------------------------------------------------------ 진행 중 턴 실시간 표시
function runningTurn(turns) {
  for (let i = turns.length - 1; i >= 0; i--) {
    if (turns[i].status === "running" && !turns[i].response_at) return turns[i];
  }
  return null;
}
function shownSessions() {
  const b = S.brainOpen && brainSession();
  return boardSessions().slice(0, S.boardLimit).filter(s => S.visibleCols.has(s.session_id)).concat(b ? [b] : [])
    .map(s => [s, (S.boardCache.get(s.session_id) || {}).detail]);
}
async function refreshLive() {
  let changed = false;
  const active = new Map();   // session_id → 진행 중 턴의 시작 시각
  for (const [s, detail] of shownSessions()) {
    if (!detail || !["working", "waiting"].includes(s.state)) continue;
    const t = runningTurn(detail.turns);
    if (t && t.prompt_at) active.set(s.session_id, t.prompt_at);
  }
  if (active.size) {
    // 작업 중인 칸들의 진행 과정을 요청 한 번으로(칸마다 따로 받지 않게)
    try {
      const r = await api("GET", "/api/activity?items=" + encodeURIComponent([...active].map(([sid, at]) => sid + ":" + at).join(",")));
      for (const [sid, since] of active) {
        const items = r.items[sid];
        if (!items) continue;
        const last = items[items.length - 1];
        const key = items.length + ":" + (last ? (last.text || last.brief || "").length : 0);
        const prev = S.live.get(sid);
        if (!prev || prev.key !== key || prev.since !== since) {
          S.live.set(sid, { since, items, key });
          changed = true;
        }
      }
    } catch (e) { /* 다음 폴링에서 다시 */ }
  }
  for (const sid of [...S.live.keys()]) {
    if (!active.has(sid)) { S.live.delete(sid); changed = true; }
  }
  if (changed) renderBoard();
}

// ------------------------------------------------------------------ 알림
function checkAlerts() {
  const next = {};
  for (const s of S.sessions) {
    next[s.session_id] = { disp: displayState(s), resp: s.last_response && s.last_response.response_at };
  }
  if (S.prev) {
    for (const s of S.sessions) {
      const p = S.prev[s.session_id];
      const n = next[s.session_id];
      if (!p) continue;
      if (n.disp === "waiting" && p.disp !== "waiting") {
        alertUser(s, "확인 필요 · " + sessionTitle(s), s.state_detail || "Claude 가 입력을 기다립니다");
      } else if (n.disp === "error" && p.disp !== "error") {
        alertUser(s, "오류 · " + sessionTitle(s), s.state_detail || "응답이 실패했습니다");
      } else if (s.unread && n.resp && p.resp !== n.resp) {
        alertUser(s, "새 응답 · " + sessionTitle(s), respSummary(s.last_response.response, 140));
      }
    }
  }
  S.prev = next;
}
function alertUser(s, title, body) {
  if (S.sound) chime();
  if (!S.notify || !("Notification" in window) || Notification.permission !== "granted") return;
  const n = new Notification(title, { body: clamp(body, 180), tag: s.session_id });
  n.onclick = () => { window.focus(); focusCol(s.session_id); n.close(); };
}
let audioCtx = null;
function chime() {
  try {
    audioCtx = audioCtx || new AudioContext();
    const t = audioCtx.currentTime;
    [[880, 0], [1320, 0.12]].forEach(([f, d]) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t + d);
      g.gain.exponentialRampToValueAtTime(0.12, t + d + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + d + 0.35);
      o.connect(g).connect(audioCtx.destination);
      o.start(t + d); o.stop(t + d + 0.4);
    });
  } catch (e) { /* 사용자 조작 전에는 소리를 못 낼 수 있다 */ }
}
function updateBadge(groups) {
  const counts = countStates(groups);
  // 세션이 나를 기다리는 것(확인 필요·새 응답·오류)에 브레인이 인박스에 새로 넣은 카드(아직 안 본 것)를 더한다
  const cards = inboxUnseen();
  const attention = counts.waiting + counts.unread + counts.error + cards;
  const title = (attention ? "(" + attention + ") " : "") + "Argus";
  if (document.title !== title) document.title = title;
  const color = counts.waiting ? "#d98a10" : counts.error ? "#d0453a" : counts.unread ? "#e5484d"
    : cards ? "#7c5cff" : counts.working ? "#2f9e66" : "#d97757";
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><circle cx='16' cy='16' r='12' fill='${color}'/>` +
    (attention ? `<text x='16' y='21' font-size='15' font-family='sans-serif' font-weight='700' text-anchor='middle' fill='white'>${attention > 9 ? "9+" : attention}</text>` : "") +
    "</svg>";
  const href = "data:image/svg+xml," + encodeURIComponent(svg);
  if ($("#favicon").getAttribute("href") !== href) $("#favicon").href = href;
}
// ------------------------------------------------------------------ 프로젝트 필터
// 세션들을 프로젝트(저장소 — 워크트리는 원래 저장소로 묶음)별로 세어 보여 주고, 고르면 그 프로젝트 칸만 보드에
S.projFilter = load("csl.projFilter", "");
function projectGroups() {
  const m = new Map();
  for (const s of allBoardSessions()) {
    const k = projectKey(s);
    if (!m.has(k)) m.set(k, { key: k, name: s.project_name || base(k), n: 0, working: 0, waiting: 0, unread: 0 });
    const g = m.get(k), d = displayState(s);
    g.n++;
    if (d in g) g[d]++;
  }
  return [...m.values()].sort((a, b) => b.n - a.n || a.name.localeCompare(b.name));
}
function renderProjBtn() {
  const groups = projectGroups(), total = allBoardSessions().length;
  const cur = S.projFilter && groups.find(g => g.key === S.projFilter);
  const btn = $("#projBtn");
  btn.classList.toggle("on", !!S.projFilter);
  setHtml(btn, cur ? `<span class="proj-dot" style="background:${projColor(cur.key)}"></span>${esc(cur.name)} <span class="n">${cur.n}</span>`
    : S.projFilter ? `${esc(base(S.projFilter))} <span class="n">0</span>`
    : `프로젝트 ${groups.length} <span class="n">· ${total}</span>`);
  if (!$("#projMenu").hidden) drawProjMenu();
}
function drawProjMenu() {
  const groups = projectGroups(), total = allBoardSessions().length;
  const row = (key, dot, name, n, meta) => `<button class="cm-item ${S.projFilter === key ? "on" : ""}" data-proj="${esc(key)}" title="${esc(key)}">
    ${dot}<span class="pm-name">${esc(name)}</span><span class="pm-meta">${meta}</span><span class="pm-n">${n}</span></button>`;
  $("#projMenu").innerHTML = row("", '<span class="proj-dot all"></span>', "전체 프로젝트", total, `${groups.length}개 프로젝트`) +
    '<div class="tm-sep"></div>' +
    groups.map(g => row(g.key, `<span class="proj-dot" style="background:${projColor(g.key)}"></span>`, g.name, g.n,
      [g.waiting ? `<span class="pm-wait">확인 ${g.waiting}</span>` : "", g.working ? `작업 중 ${g.working}` : "",
        g.unread ? `새 응답 ${g.unread}` : ""].filter(Boolean).join(" · "))).join("");
}
function setProjFilter(key) {
  S.projFilter = key;
  save("csl.projFilter", key);
  S.boardLimit = 12;
  renderProjBtn();
  renderBoard();
  refreshBoard();
}
$("#projBtn").addEventListener("click", () => {
  const m = $("#projMenu");
  if (!m.hidden) { m.hidden = true; return; }
  drawProjMenu();
  m.hidden = false;
  placeMenu(m, $("#projBtn"));
});
$("#projMenu").addEventListener("click", e => {
  const b = e.target.closest("[data-proj]");
  if (!b) return;
  $("#projMenu").hidden = true;
  setProjFilter(b.dataset.proj);
});

// ------------------------------------------------------------------ 상단 카운터
// 보드에 보이는 세션만 센다(보관·종료는 「종료·보관 세션도 보기」를 켰을 때만)
function sessionsIn(kind) {
  return allBoardSessions().filter(s => displayState(s) === kind);
}
// 상태별 세션 묶음 — poll 한 번에 카운터·배지가 같이 쓴다(allBoardSessions 를 상태마다 다시 돌리지 않게)
function stateGroups() {
  const g = { waiting: [], unread: [], working: [], error: [] };
  for (const s of allBoardSessions()) { const d = displayState(s); if (g[d]) g[d].push(s); }
  return g;
}
function countStates(groups = stateGroups()) {
  const c = {};
  for (const k in groups) c[k] = groups[k].length;
  return c;
}
function renderCounters(groups = stateGroups()) {
  const c = countStates(groups);
  // 마우스를 올리면 어떤 세션인지, 누르면 그 칸으로(여러 개면 누를 때마다 다음 것)
  const item = (k, label, style = "") => {
    const names = groups[k].map(s => "· " + sessionTitle(s) + (s.state_detail && k === "waiting" ? " — " + clamp(s.state_detail, 60) : ""));
    return `<span class="counter ${c[k] ? k : "zero"}" ${style} ${c[k] ? `data-action="counter" data-kind="${k}" title="${esc(label + "\n" + names.join("\n") + "\n\n누르면 그 칸으로")}"` : ""}>
      <span class="dot ${c[k] ? k : ""}"></span>${label} <span class="n">${c[k]}</span></span>`;
  };
  // 같으면 다시 쓰지 않는다 — 2초마다 갈아 끼우면 올려 둔 툴팁이 사라지고 번역도 다시 돈다
  setHtml($("#counters"), item("waiting", "확인 필요") + item("unread", "새 응답") + item("working", "작업 중") +
    (c.error ? item("error", "오류", 'style="color:var(--error);background:var(--error-soft)"') : ""));
}
// 여러 개면 누를 때마다 다음 칸(보드 순서, 끝나면 처음으로). 10초 넘게 안 누르면 다시 첫 칸부터
const counterTurn = {};
async function gotoCounter(kind) {
  const list = sessionsIn(kind);
  if (!list.length) return;
  const t = counterTurn[kind] && Date.now() - counterTurn[kind].at < 10000 ? counterTurn[kind].i : -1;
  const i = (t + 1) % list.length;
  counterTurn[kind] = { i, at: Date.now() };
  const s = list[i];
  await focusCol(s.session_id);
  toast(`${{ waiting: "확인 필요", unread: "새 응답", working: "작업 중", error: "오류" }[kind]} ${i + 1}/${list.length} · ${sessionTitle(s)}` +
    (kind === "waiting" && s.state_detail ? " — " + clamp(s.state_detail, 80) : ""));
}

// ------------------------------------------------------------------ 종료 포함 필터
function matches(s) {
  return S.showEnded || isImporting(s) || S.reveal.has(s.session_id) || (s.state !== "ended" && !s.archived);
}
// 검색·활동에서 고른 종료·보관 세션 — 「종료·보관 세션도 보기」를 켜 두지 않고 그 칸만 보인다(다른 세션을 고르거나 새로고침하면 사라짐)
S.reveal = new Set();
// 보드 터미널 밖(Orca·tmux·iTerm2·백그라운드 …)에서 도는 세션이면 { where, note }, 아니면 null(종료·브레인·보드 터미널)
function elsewhere(s) {
  if (s.term_web || s.brain || s.state === "ended" || s.archived || isImporting(s)) return null;
  if (s.bg) return { where: "백그라운드", note: "터미널 없이 돌고 있습니다" };
  if (s.running === false) return { where: "꺼진 세션", note: "어느 터미널에서도 돌고 있지 않습니다", btn: "⇄ 이어 열기" };
  const where = s.term_label || "다른 터미널";
  return { where, note: ["orca", "tmux"].includes(s.term_name) ? "보드 터미널 밖에서 도는 세션"
    : "보드에서 입력할 수 없는 터미널 — 그쪽에서 /exit 한 뒤 가져오기" };
}
// 보드 터미널로 가져오는 중: 지금 터미널에서 끝내고 다시 여는 사이 잠깐 「종료」가 되어도 칸을 그대로 두고 표시한다.
// 서버가 알려 주는 것(importing — 다른 탭·기기에서도) + 이 화면에서 누른 것(다시 열려 종료가 풀릴 때까지)
S.importing = new Map();   // 세션 → { at, done }
function isImporting(s) {
  if (s.importing) return true;
  const x = S.importing.get(s.session_id);
  if (!x) return false;
  if (x.done && (s.state !== "ended" || Date.now() - x.at > 90000)) { S.importing.delete(s.session_id); return false; }
  return true;
}

// ------------------------------------------------------------------ 스크롤 기억(대화는 최신이 아래)
// 채팅처럼: 사용자가 맨 아래에 붙어 있으면(_pinned) 내용이 붙거나 영역 크기가 바뀔 때 계속 따라 내려간다.
// 위로 올려 읽는 중이면 그 위치를 지킨다. 붙어 있는지는 스크롤할 때마다 다시 판단한다.
const threadResize = new ResizeObserver(entries => {
  for (const en of entries) {
    // 칸을 지우거나 브레인 패널을 다시 만들면 대화 영역이 문서에서 빠진다(그때 한 번 불린다) — 감시를 풀어 붙잡아 두지 않는다
    if (!en.target.isConnected) { threadResize.unobserve(en.target); continue; }
    if (en.target._pinned) en.target.scrollTop = en.target.scrollHeight;
  }
});
function setupThread(el) {
  el._pinned = true;
  // 위로 많이 올려 읽을 때 맨 아래(최신)로 바로 내려가는 버튼. 대화 영역은 통째로 다시 그려지므로 버튼은 칸에 둔다
  const btn = document.createElement("button");
  btn.className = "to-bottom";
  btn.type = "button";
  btn.hidden = true;
  btn.title = "맨 아래(최신 대화)로";
  btn.textContent = "↓ 맨 아래로";
  btn.addEventListener("click", () => {
    // 멀면 바로(부드럽게 22,000px 를 내려가면 한참 걸리고 그사이 새 글을 따라가지 못한다), 가까우면 부드럽게
    const far = el.scrollHeight - el.scrollTop - el.clientHeight > 2500;
    el.scrollTo({ top: el.scrollHeight, behavior: far ? "auto" : "smooth" });
    el._pinned = true;
    btn.hidden = true;
  });
  el.after(btn);
  el.addEventListener("scroll", () => {
    const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
    el._pinned = gap < 60;
    const show = gap > 400;
    if (show) btn.style.bottom = (el.parentElement.clientHeight - el.offsetTop - el.clientHeight + 12) + "px";   // 대화 영역 아래 끝 바로 위
    if (btn.hidden === show) btn.hidden = !show;
  }, { passive: true });
  threadResize.observe(el);
}

// ------------------------------------------------------------------ 대화 스레드
function stepsHtml(items, skipText) {
  return items.map(it => {
    if (it.kind === "tool") {
      // 누르면 실제 명령(Bash)·입력 전체를 펼쳐 본다
      const k = it.at + "|" + it.name + "|" + it.brief;
      const open = it.detail && S.stepOpen.has(k);
      return `<div class="step tool ${it.detail ? "has-detail" : ""} ${open ? "open" : ""}" ${it.detail ? `data-action="toggle-step" data-key="${esc(k)}" title="눌러서 ${open ? "접기" : "자세히 보기"}"` : ""}>
        <span class="tname">${esc(it.name)}</span><span class="tbrief">${esc(it.brief)}</span></div>` +
        (open ? `<pre class="tdetail">${esc(it.detail)}</pre>` : "");
    }
    if (skipText && it.text.trim() === skipText.trim()) return "";
    return `<div class="step text md">${mdCached(it.text)}</div>`;
  }).join("");
}
function turnHtml(t, s, lastRunId) {
  const at = t.prompt_at || t.response_at;
  const promptLong = t.prompt && (t.prompt.length > 500 || t.prompt.split("\n").length > 8);
  const promptOpen = S.promptOpen.has(t.id);
  const notice = taskNotice(t.prompt);
  const user = t.prompt == null ? "" : notice ? noticeHtml(notice, at ? hm(at) : "") : `
    <div class="msg-meta">${t.status !== "rewound" && t.prompt && s.state !== "ended" ? `<button class="turn-rw" data-action="rewind-menu" data-sid="${esc(s.session_id)}" data-tid="${t.id}" title="이 요청 앞으로 되돌리기(/rewind)">↶</button>` : ""}${at ? dateLabel(at) + " " + hm(at) : ""}</div>
    <div class="msg-user"><div class="bubble md ${promptLong && !promptOpen ? "clamped" : ""}">${promptHtml(t)}</div></div>
    ${promptLong ? `<div class="msg-user"><button class="linkish" data-action="toggle-prompt" data-tid="${t.id}">${promptOpen ? "접기" : "요청 전체 보기"}</button></div>` : ""}`;

  let claude;
  if (t.status === "running" && !t.response_at && t.id !== lastRunId) {
    claude = '<div class="msg-claude muted-line">진행 중인 작업에 덧붙인 요청 — 아래에서 함께 처리 중</div>';
  } else if (t.status === "running" && !t.response_at) {
    const live = S.live.get(s.session_id);
    const items = live && live.since === t.prompt_at ? live.items : [];
    const shown = items.slice(-30);
    const waiting = s.state === "waiting";
    claude = `<div class="msg-claude">
      <div class="who-line"><span class="who-name">Claude</span></div>
      ${items.length > shown.length ? `<div class="muted step">앞선 단계 ${items.length - shown.length}개 생략</div>` : ""}
      ${shown.length ? `<div class="steps">${stepsHtml(shown)}</div>` : ""}
      <div class="live-indicator ${waiting ? "live-wait" : ""}"><span class="dot ${waiting ? "waiting" : "working"}"></span>
        ${waiting ? esc(s.state_detail || "확인 필요") : "작업 중"} · <span class="elapsed" data-since="${t.prompt_at}">${dur(Date.now() / 1000 - t.prompt_at)}</span></div>
    </div>`;
  } else if (t.status === "merged") {
    claude = '<div class="msg-claude muted-line">응답 없음 — 중단됐거나 다음 요청과 함께 처리됨</div>';
  } else if (!t.response) {
    claude = '<div class="msg-claude muted-line">응답 없음 (중단됨)</div>';
  } else {
    const long = t.response.length > LONG_RESPONSE && !S.expanded.has(t.id);
    const took = t.prompt_at && t.response_at ? " · " + dur(t.response_at - t.prompt_at) + " 걸림" : "";
    const badge = t.status === "failed" ? '<span class="pill error">실패</span>'
      : t.status === "interrupted" ? '<span class="pill idle">중단</span>' : "";
    const trailOpen = S.trailOpen.has(t.id);
    const trail = S.trail.get(t.id);
    let trailHtml = "";
    if (trailOpen) {
      trailHtml = !trail ? '<div class="steps"><div class="step muted">불러오는 중…</div></div>'
        : trail.length ? `<div class="steps">${stepsHtml(trail, t.response)}</div>`
        : '<div class="steps"><div class="step muted">기록된 작업 과정이 없습니다.</div></div>';
    }
    claude = `<div class="msg-claude">
      <div class="who-line"><span class="who-name">Claude</span>${hm(t.response_at)}${took} ${badge}
        ${t.prompt_at ? `<button class="linkish" data-action="toggle-trail" data-tid="${t.id}" data-sid="${esc(s.session_id)}">${trailOpen ? "작업 과정 접기" : "작업 과정 보기"}</button>` : ""}</div>
      ${trailHtml}
      ${extrasHtml(t)}
      <div class="body md ${long ? "clip" : ""}">${mdCached(t.response)}</div>
      ${thumbsIn(t.response)}
      ${t.response.length > LONG_RESPONSE ? `<button class="linkish" data-action="toggle-resp" data-tid="${t.id}">${long ? "응답 전체 보기" : "접기"}</button>` : ""}
    </div>`;
  }
  return `<article class="turn" data-tid="${t.id}">${user}${claude}</article>`;
}
// 마지막 메시지 앞에 나온 것들(중간 메시지·보낸 파일·끼어든 알림). 훅은 마지막 메시지만 줘서 서버가 기록 파일에서 채운다
const EXTRAS_SHOWN = 3;
function extrasHtml(t) {
  let items;
  try { items = JSON.parse(t.extras || "[]"); } catch (e) { items = []; }
  if (!items.length) return "";
  const open = S.midOpen.has(t.id);
  const hidden = open ? 0 : Math.max(0, items.length - EXTRAS_SHOWN);
  const rows = items.slice(hidden).map(it => {
    if (it.type === "notice") {
      const n = taskNotice(it.text);
      return n ? noticeHtml(n, "") : "";
    }
    if (it.type === "files") {
      return `<div class="mid-files">${it.files.map(p => IMG_PATH.test(p) ? thumbHtml(p)
        : `<span class="mid-file">${linkifyPaths(esc(p))}</span>`).join("")}</div>`;
    }
    return `<div class="mid-text md">${mdCached(it.text)}</div>${thumbsIn(it.text)}`;
  }).join("");
  const more = items.length > EXTRAS_SHOWN
    ? `<button class="linkish" data-action="toggle-mid" data-tid="${t.id}">${open ? "중간 메시지 접기" : `앞선 중간 메시지 ${hidden}개 보기`}</button>` : "";
  return `<div class="mid">${more}${rows}</div>`;
}
// 글에 적힌 이미지 경로도 썸네일로(없는 파일이면 숨긴다 — 아래 error 처리)
const IMG_PATH = /\.(png|jpe?g|gif|webp)$/i;
function thumbHtml(p) {   // 누르면 이미지 팝업(← → 로 넘기기 · 다운로드 · 기본 앱으로 열기). ⌥ 클릭은 Finder
  const u = "/api/sent-file?path=" + encodeURIComponent(p);
  return `<a class="zoom-img resp-img" href="${u}" data-path="${esc(p)}" title="${esc(base(p))} — 눌러서 크게 보기 (⌥ 클릭: Finder)"><img src="${u}" alt="" loading="lazy"></a>`;
}
function thumbsIn(text) {
  const paths = [...new Set((text || "").match(PATH_RE) || [])].filter(p => IMG_PATH.test(p)).slice(0, 12);
  return paths.length ? `<div class="mid-files resp-thumbs">${paths.map(thumbHtml).join("")}</div>` : "";
}
// 요청 글 — 붙인 이미지 자리([Image #N], Claude Code 가 남기는 표시)에 대화 기록 속 그 이미지를 썸네일로. 누르면 새 탭에서 크게
function promptHtml(t) {
  const html = mdCached(t.prompt);
  if (!t.id || !html.includes("[Image #")) return html;
  let k = 0;
  return html.replace(/\[Image #(\d+)\]/g, (m, n) => {
    const url = `/api/turns/${t.id}/image/${n}?i=${k++}`;
    return `<a class="prompt-img" href="${url}" target="_blank" rel="noopener" title="Image #${n} — 눌러서 크게 보기"><img src="${url}" alt="${m}" loading="lazy"></a>`;
  });
}
// 크게 보기 — 보드 위 팝업. 아무 데나 누르거나 Esc 로 닫는다(⌘·Ctrl·Shift 클릭은 새 탭 그대로)
// 이미지 팝업: 보낸 메시지·넥스트 액션·변경 보기의 이미지를 크게. 같은 묶음(같은 메시지·넥스트 액션·변경 보기)에 여러 장이면
// ← → (또는 ‹ › 버튼)으로 넘긴다. 떠 있는 동안 키는 팝업만 받는다 — 뒤의 보드에서 칸이 바뀌거나 입력창이 잡히지 않게
const IMGV = { list: [], i: 0 };
function openImgView(src, alt, list) {
  let v = $("#imgView");
  if (!v) {
    v = document.createElement("div");
    v.id = "imgView";
    v.className = "img-view";
    v.hidden = true;
    v.innerHTML = `<img alt=""><button type="button" class="img-view-x" title="닫기 (Esc)" aria-label="닫기">×</button>
      <div class="img-view-bar"><a class="img-view-btn" data-imgv-dl download title="이 이미지를 내려받기">⬇ 다운로드</a>
        <button type="button" class="img-view-btn" data-imgv-open title="Mac 기본 앱(미리보기 등)으로 열기">↗ 기본 앱으로 열기</button>
        <button type="button" class="img-view-btn" data-imgv-reveal title="Finder 에서 보기">📂 Finder</button></div>
      <button type="button" class="img-view-nav prev" data-imgv="-1" title="이전 이미지 ( ← )" aria-label="이전">‹</button>
      <button type="button" class="img-view-nav next" data-imgv="1" title="다음 이미지 ( → )" aria-label="다음">›</button>
      <span class="img-view-n"></span>`;
    v.addEventListener("click", e => {
      const nav = e.target.closest("[data-imgv]");
      if (nav) return stepImgView(+nav.dataset.imgv);
      if (e.target.closest("[data-imgv-dl]")) return;   // 내려받기는 링크 그대로(팝업은 열어 둔다)
      const op = e.target.closest("[data-imgv-open], [data-imgv-reveal]");
      if (op) {
        const x = IMGV.list[IMGV.i], reveal = op.hasAttribute("data-imgv-reveal");
        if (x && x.path) api("POST", "/api/open", { path: x.path, reveal })
          .then(r => toast(r.how === "reveal" ? "Finder 에서 보여 줍니다" : "기본 앱으로 열었습니다")).catch(err => toast(err.message));
        return;
      }
      v.hidden = true;   // 그 밖(이미지·바깥·×)을 누르면 닫힌다
    });
    document.body.appendChild(v);
  }
  IMGV.list = list && list.length ? list : [{ src, alt }];
  IMGV.i = Math.max(0, IMGV.list.findIndex(x => x.src === src));
  showImgView();
  v.hidden = false;
}
function showImgView() {
  const v = $("#imgView"), x = IMGV.list[IMGV.i], many = IMGV.list.length > 1;
  const img = v.querySelector("img");
  img.src = x.src;
  img.alt = x.alt || "";
  v.querySelectorAll(".img-view-nav").forEach(b => { b.hidden = !many; });
  // 다운로드는 늘, 기본 앱·Finder 는 파일 경로를 아는 이미지(응답·넥스트 액션·보낸 파일)만
  const dl = v.querySelector("[data-imgv-dl]");
  dl.href = x.src;
  dl.download = x.path ? base(x.path) : `image-${IMGV.i + 1}.png`;
  v.querySelectorAll("[data-imgv-open], [data-imgv-reveal]").forEach(b => { b.hidden = !x.path; });
  v.querySelector(".img-view-n").textContent = many ? `${IMGV.i + 1} / ${IMGV.list.length}` : "";
}
function stepImgView(d) {
  if (IMGV.list.length < 2) return;
  IMGV.i = (IMGV.i + d + IMGV.list.length) % IMGV.list.length;
  showImgView();
}
document.addEventListener("click", e => {
  const a = e.target.closest && e.target.closest(".prompt-img, .zoom-img");
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
  e.preventDefault();
  if (e.altKey && a.dataset.path) {   // 넥스트 액션 이미지: ⌥ 클릭이면 Finder 에서 보기(예전 동작)
    api("POST", "/api/open", { path: a.dataset.path, reveal: true }).then(() => toast("Finder 에서 보여 줍니다")).catch(err => toast(err.message));
    return;
  }
  // 같은 묶음의 이미지들: 넥스트 액션 하나·변경 보기·보낸 메시지(턴) 하나
  const group = a.closest(".note-imgs, .dv-imgs, .turn, .act-dbody") || a.parentElement;
  // 불러오지 못해 숨긴 썸네일(없는 파일)은 넘기기에서 뺀다
  const list = [...group.querySelectorAll(".prompt-img, .zoom-img")].filter(x => !x.hidden)
    .map(x => ({ src: x.getAttribute("href"), alt: (x.querySelector("img") || {}).alt || "", path: x.dataset.path || "" }));
  openImgView(a.getAttribute("href"), a.querySelector("img") && a.querySelector("img").alt, list);
});
// 팝업이 떠 있는 동안: Esc 닫기 · ← → 넘기기 · 나머지 키는 뒤로 넘기지 않는다(브라우저 단축키 ⌘R 등은 그대로 동작)
for (const type of ["keydown", "keyup"]) {
  document.addEventListener(type, e => {
    const v = $("#imgView");
    if (!v || v.hidden) return;
    e.stopImmediatePropagation();
    if (type === "keyup") return;
    if (e.key === "Escape") { e.preventDefault(); v.hidden = true; }
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); stepImgView(-1); }
    else if (e.key === "ArrowRight" || e.key === "ArrowDown" || e.key === " ") { e.preventDefault(); stepImgView(1); }
    else if (!e.metaKey && !e.ctrlKey) e.preventDefault();   // 입력창에 글자가 들어가지 않게
  }, true);
}
document.addEventListener("error", e => {
  if (e.target.tagName !== "IMG") return;
  if (e.target.closest(".mid-files")) {   // 글에 적힌 경로가 없는 파일이면 썸네일을 링크째 숨긴다
    const a = e.target.closest(".zoom-img");
    (a || e.target).hidden = true;
    return;
  }
  const a = e.target.closest(".prompt-img");   // 기록에서 이미지를 못 찾으면 원래 글자로
  if (a) a.replaceWith(document.createTextNode(e.target.alt));
  const z = e.target.closest(".zoom-img");   // 변경 보기의 이미지 — 못 읽으면 한 줄 안내로
  if (z) z.outerHTML = '<div class="dv-note">이미지를 불러오지 못했습니다</div>';
}, true);
function noticeHtml(n, time) {
  const label = { completed: "끝남", failed: "실패", killed: "중단", stopped: "중단" }[n.status.toLowerCase()] || n.status;
  return `<div class="msg-notice" title="Claude Code 가 넣은 자동 메시지 — 사용자가 보낸 요청이 아닙니다">
      <span class="dot ${n.ok ? "done" : "error"}"></span><span>백그라운드 작업 ${esc(label)}</span>
      ${n.summary ? `<span class="notice-sum">· ${esc(n.summary)}</span>` : ""}<span class="muted"> ${time}</span></div>`;
}
// 백그라운드 작업(명령·에이전트)이 끝났을 때 Claude Code 가 대화에 넣는 알림. 요청 훅으로 와서 요청처럼 기록된다
function taskNotice(prompt) {
  if (!prompt || !/^\s*<task-notification>/.test(prompt)) return null;
  const tag = name => { const m = new RegExp(`<${name}>([\\s\\S]*?)</${name}>`).exec(prompt); return m ? m[1].trim() : ""; };
  const status = tag("status") || "끝남";
  return { status, ok: /^completed$/i.test(status), summary: tag("summary").replace(/^Background command /, "") };
}
function threadHtml(s, turns, hiddenCount, asides) {
  const older = hiddenCount > 0
    ? `<div class="older-turns"><button class="linkish" data-action="more-turns" data-sid="${esc(s.session_id)}">이전 대화 더 보기 (${hiddenCount}턴 남음)</button></div>` : "";
  const run = runningTurn(turns);
  // 곁질문(/btw)은 대화에 들어가지 않지만 보낸 시각 자리에 따로 끼워 보여 준다(불러온 턴보다 앞선 것은 뺀다)
  const at = t => t.prompt_at || t.response_at || 0;
  const first = hiddenCount > 0 && turns.length ? at(turns[0]) : 0;
  const side = (asides || []).filter(a => a.at >= first);
  let html = "", i = 0;
  // 이 세션에서 갈라져 나간 갈래들: 갈라진 시각 뒤 첫 턴 앞에 「⑂ 여기서 갈래」
  const kids = s._fam ? s._fam.members.filter(m => m.continued_from === s.session_id && isBranch(m) && m.branch_at) : [];
  const markAt = new Map();
  for (const k of kids) {
    const idx = turns.findIndex(t => at(t) > k.branch_at);
    const key = idx < 0 ? turns.length : idx;
    if (!markAt.has(key)) markAt.set(key, []);
    markAt.get(key).push(k);
  }
  const mark = list => `<div class="br-mark">⑂ 여기서 갈래 ${list.map(k => `<a class="sref" data-action="fam-tab" data-root="${esc(s._fam.root)}" data-sid="${esc(k.session_id)}">「${esc(clamp(sessionTitle(k), 30))}」</a>`).join(" ")}</div>`;
  const rwOpen = S.rwOpen.has(s.session_id);
  let rwRun = 0;
  const flushRw = () => {   // 되돌리기(/rewind)로 대화에서 지운 턴 묶음 — 기본은 한 줄로 접는다
    if (!rwRun) return;
    html += `<div class="rw-fold ${S.rwJust === s.session_id ? "rw-new" : ""}">↶ 되돌려 지운 대화 ${rwRun}턴 <button class="linkish" data-action="rw-toggle" data-sid="${esc(s.session_id)}">${rwOpen ? "접기" : "펼치기"}</button></div>`;
    rwRun = 0;
  };
  turns.forEach((t, n) => {
    if (markAt.has(n)) html += mark(markAt.get(n));
    while (i < side.length && side[i].at < at(t)) html += asideHtml(side[i++]);
    if (t.status === "rewound") {
      rwRun++;
      if (rwOpen) html += `<div class="rw-turn">${turnHtml(t, s, run && run.id)}</div>`;
      return;
    }
    flushRw();
    // 되돌리는 중(서버가 터미널의 /rewind 를 조작하는 몇 초): 그 요청부터 지워질 턴을 흐리게, 위에 진행 줄
    const pend = S.rwPending && S.rwPending.sid === s.session_id && t.id >= S.rwPending.tid ? S.rwPending : null;
    if (pend && t.id === pend.first) html += `<div class="rw-banner"><span class="dot working"></span>↶ ${pend.removes ? "여기서부터 되돌리는 중…" : "되돌리는 중…"} <span class="muted">터미널의 /rewind 를 조작하고 있습니다</span></div>`;
    const th = turnHtml(t, s, run && run.id);
    html += pend && pend.removes ? th.replace('<article class="turn"', '<article class="turn rw-pending"') : th;
  });
  flushRw();
  if (markAt.has(turns.length)) html += mark(markAt.get(turns.length));
  while (i < side.length) html += asideHtml(side[i++]);
  // 갈래 칸: 맨 위에 어디서 갈라졌는지(앞 대화는 원본 탭에 있다 — 여기서는 갈라진 뒤만)
  const parent = isBranch(s) && s._fam && s._fam.members.find(m => m.session_id === s.continued_from);
  const head = parent ? `<div class="br-split" title="갈라지기 전 대화는 원본 쪽 탭에서 볼 수 있습니다">⑂ <a class="sref" data-action="fam-tab" data-root="${esc(s._fam.root)}" data-sid="${esc(parent.session_id)}">「${esc(clamp(sessionTitle(parent), 30))}」</a> ${s.branch_turns ? s.branch_turns + "턴째에서" : "에서"} 갈라짐${s.bg ? " · 백그라운드" : ""}</div>` : "";
  return head + older + (html || '<div class="muted" style="padding:16px 0">아직 요청이 없습니다.</div>') + compactHtml(s);
}
function asideHtml(a) {
  const body = a.status === "pending"
    ? '<div class="aside-wait muted"><span class="ibc-spin"></span> 답을 기다리는 중…</div>'
    : a.status === "error" ? `<div class="aside-err">⚠ ${esc(a.answer || "답을 읽지 못했습니다")}</div>`
    : `<div class="aside-a md">${mdCached(a.answer || "")}</div>`;
  return `<div class="aside-card ${a.status}" data-aside="${a.id}">
    <div class="aside-head"><span class="aside-tag">💭 곁질문</span><span class="muted">/btw · 대화에 들어가지 않음 · ${hm(a.at)}</span></div>
    <div class="aside-q">${esc(a.question)}</div>${body}</div>`;
}
// 컴팩트 진행: 얼마나 됐는지와 지난번에 걸린 시간(진행률은 Claude Code 가 알려 주지 않는다)
function compactHtml(s) {
  if (!s.compact_at) return "";
  const auto = s.compact_trigger === "auto";
  const last = s.compact_ms ? ` · 지난번 ${dur(s.compact_ms / 1000)} 걸림` : "";
  return `<div class="compact-line">
    <div class="live-indicator"><span class="ctx-ring spin"></span>
      ${auto ? "컨텍스트가 차서 자동으로 요약하는 중" : "대화를 요약하는 중(/compact)"} · <span class="elapsed" data-since="${s.compact_at}">${dur(Date.now() / 1000 - s.compact_at)}</span></div>
    <div class="muted">끝나면 요약본으로 이어 갑니다${last}${auto ? " — 하던 작업은 요약 뒤 계속됩니다" : ""}</div>
  </div>`;
}
function noteImages(n) {
  try { return JSON.parse(n.images || "[]"); } catch (e) { return []; }
}
function noteImgsHtml(n) {
  const imgs = noteImages(n);
  // 누르면 보낸 메시지의 이미지처럼 팝업으로 크게(⌥ 클릭: Finder 에서 보기)
  return imgs.length ? `<div class="note-imgs">${imgs.map(p => { const u = "/api/upload/" + encodeURIComponent(base(p));
    return `<a class="zoom-img note-img" href="${u}" data-path="${esc(p)}" title="눌러서 크게 보기 (⌥ 클릭: Finder)"><img src="${u}" alt=""></a>`; }).join("")}</div>` : "";
}
function findNote(nid) {
  for (const c of S.boardCache.values()) {
    const n = c.detail && c.detail.notes.find(x => String(x.id) === String(nid));
    if (n) return n;
  }
  return null;
}
// 할 일은 마크다운으로 그린다 — 고치기·입력창에 넣기는 그린 글이 아니라 원문으로
function noteRaw(el) { const n = el && findNote(el.dataset.nid); return n ? n.text : (el ? el.innerText : ""); }
// 예약 순서: 칸의 예약 목록(보낼 순서) 그대로 id 배열 — 바꾼 순서를 서버에 보낸다
function queuedIds(sid) {
  const c = S.boardCache.get(sid);
  return ((c && c.detail && c.detail.notes) || []).filter(n => n.queued_at && !n.done).sort((p, q) => p.queued_at - q.queued_at).map(n => n.id);
}
async function setQueueOrder(sid, order) {
  try { await api("POST", "/api/notes/queue-order", { session_id: sid, order }); }
  catch (err) { toast(err.message); }
  await refresh(sid);
}
function moveQueued(sid, nid, d) {
  const order = queuedIds(sid), i = order.indexOf(nid), j = i + d;
  if (i < 0 || j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  return setQueueOrder(sid, order);
}
const QDRAG = { nid: null, sid: null };
document.addEventListener("dragstart", e => {
  const g = e.target.closest && e.target.closest(".note-grip");
  if (!g) return;
  const li = g.closest("li.note");
  Object.assign(QDRAG, { nid: +g.dataset.nid, sid: li.closest(".col").dataset.col });
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", "");   // Firefox 는 무언가 실어야 끈다
  try { e.dataTransfer.setDragImage(li, 12, 12); } catch (err) { /* 그림 없이 */ }
  li.classList.add("q-dragging");
});
function qDropTarget(e) {
  const li = QDRAG.nid && e.target.closest && e.target.closest("li.note.queued");
  return li && li.closest(".col").dataset.col === QDRAG.sid ? li : null;
}
document.addEventListener("dragover", e => {
  const li = qDropTarget(e);
  if (!li) return;
  e.preventDefault();
  const r = li.getBoundingClientRect(), after = e.clientY > r.top + r.height / 2;
  document.querySelectorAll("li.note.q-before, li.note.q-after").forEach(x => x.classList.remove("q-before", "q-after"));
  li.classList.add(after ? "q-after" : "q-before");
});
document.addEventListener("drop", e => {
  const li = qDropTarget(e);
  if (!li) return;
  e.preventDefault();
  const after = li.classList.contains("q-after"), target = +li.dataset.nid, { sid, nid } = QDRAG;
  const order = queuedIds(sid).filter(x => x !== nid);
  const at = order.indexOf(target);
  if (at < 0) return;
  order.splice(after ? at + 1 : at, 0, nid);
  setQueueOrder(sid, order);
});
document.addEventListener("dragend", () => {
  QDRAG.nid = null;
  document.querySelectorAll("li.note.q-before, li.note.q-after, li.note.q-dragging").forEach(x => x.classList.remove("q-before", "q-after", "q-dragging"));
});
function noteHtml(n, qNo, qCount) {
  const meta = n.done && n.done_at ? `<span class="note-meta">완료 ${rel(n.done_at)}</span>`
    : `<span class="note-meta">${rel(n.created_at)}${qNo ? ` · <span class="note-q-tag">⏭ 응답 끝나면 ${qNo}번째로 보냄</span>` : ""}</span>`;
  // 응답이 끝나면 이 할 일을 그 세션에 보내도록 예약(다시 누르면 풀기)
  const q = n.session_id && !n.done ? `<button class="note-q ${qNo ? "on" : ""}" data-action="note-queue" data-nid="${n.id}" data-sid="${esc(n.session_id)}"
    title="${qNo ? "예약 풀기 ( ⌘Enter )" : "응답이 끝나면 이 할 일을 바로 보내도록 예약 ( ⌘Enter )"}">⏭</button>` : "";
  // 예약이 둘 이상이면 순서 바꾸기: 손잡이(⠿)로 끌기 · ▲▼ · 고른 채 ⌥↑ ⌥↓
  const move = qNo && qCount > 1 ? `<button class="note-qmove" data-action="note-qmove" data-nid="${n.id}" data-dir="-1" title="먼저 보내기 ( ⌥↑ )" ${qNo === 1 ? "disabled" : ""}>▲</button><button class="note-qmove" data-action="note-qmove" data-nid="${n.id}" data-dir="1" title="나중에 보내기 ( ⌥↓ )" ${qNo === qCount ? "disabled" : ""}>▼</button>` : "";
  // 버튼은 글 옆이 아니라 아래 시각 줄 오른쪽에 — 글 칸이 칸 너비를 다 쓰게
  return `<li class="note ${n.done ? "done" : ""} ${qNo ? "queued" : ""}" data-nid="${n.id}" tabindex="-1">
    ${qNo && qCount > 1 ? `<span class="note-grip" draggable="true" data-nid="${n.id}" title="끌어서 보낼 순서 바꾸기">⠿</span>` : ""}
    <input type="checkbox" ${n.done ? "checked" : ""} data-action="note-done" data-nid="${n.id}">
    <div><div class="note-text md" data-action="note-edit" data-nid="${n.id}" title="더블클릭해서 고치기">${mdCached(n.text)}</div>${noteImgsHtml(n)}
      <div class="note-foot">${meta}<span class="note-acts">${move}${n.session_id && !n.done ? `<button class="note-use" data-action="note-use" data-nid="${n.id}" data-sid="${esc(n.session_id)}" title="이 할 일을 그 세션 입력창에 넣기(전송은 직접)">↳ 입력</button>` : ""}${q}<button class="note-del" data-action="note-del" data-nid="${n.id}" title="삭제">×</button></span></div></div>
  </li>`;
}

// ------------------------------------------------------------------ 보드 (세션 = 세로 칸, 최근 대화 순)
// 정렬: 고정한 칸이 맨 앞(먼저 고정한 것이 왼쪽, 활동해도 자리 유지), 그 뒤로 새 이벤트 순
function projectKey(s) { return s.project_root || s.cwd || ""; }
// 프로젝트 필터와 상관없는 전체(상단 카운터는 다른 프로젝트의 확인 필요도 놓치지 않게 이걸로 센다)
// 살아 있는 브레인 세션만 칸에서 뺀다(브레인 패널에 있으니). /clear 로 끝난 예전 브레인 세션은 다른 종료 세션처럼
// 「종료·보관 세션도 보기」에서 칸으로 보인다 — 검색 결과에서 그 대화로 갈 수 있게
function liveBrain(s) { return s.brain && s.state !== "ended" && !s.archived; }
// 갈래 가족(/branch·/fork): 원본에서 갈라져 나온 세션들은 칸 하나로 묶고 칸 위 탭으로 고른다.
// 칸에는 고른 갈래(없으면 가장 최근에 움직인 것)가 서고, s._fam = { root, members } 가 붙는다
const isBranch = s => s.branch_kind === "branch" || s.branch_kind === "fork";
// 에이전트 팀의 팀원(팀장이 tmux 창 등에 띄운 세션): 칸을 따로 세우지 않고 팀장 칸 위 탭으로
const isTeammate = s => s.branch_kind === "team";
// 보드 터미널: 칸 안에서 터미널을 켜 둔 세션(open) · 붙어 있는 xterm(live: 칸의 .col-term → 연결)
const TERM = { lib: null, token: null, open: new Set(load("csl.termOpen", [])), live: new Map() };
S.famSel = load("csl.famSel", {});   // 가족 뿌리 session_id → 고른 갈래
function famRootOf(s, byId) {
  let cur = s;
  for (let i = 0; i < 20 && (isBranch(cur) || isTeammate(cur)) && byId.has(cur.continued_from); i++) cur = byId.get(cur.continued_from);
  return cur.session_id;
}
function boardFamilies() {
  const byId = new Map(S.sessions.map(s => [s.session_id, s]));
  const fams = new Map();
  for (const s of S.sessions.concat(S.boards || [])) {
    if (liveBrain(s) || s.archived && !S.showEnded && !S.reveal.has(s.session_id)) continue;
    const r = famRootOf(s, byId);
    if (!fams.has(r)) fams.set(r, []);
    fams.get(r).push(s);
  }
  const out = [];
  for (const [root, members] of fams) {
    if (!members.some(matches)) continue;   // 가족 중 하나라도 보이면(원본이 끝났어도 갈래가 살아 있으면) 칸을 둔다
    const recent = (p, q) => q.last_activity - p.last_activity;
    // 고른 탭이 없으면 가장 최근에 움직인 갈래 — 팀원은 팀장 대신 서지 않는다(팀원이 일할 때마다 칸이 바뀌지 않게)
    const sel = members.find(m => m.session_id === S.famSel[root])
      || members.filter(m => m.state !== "ended" && !isTeammate(m)).sort(recent)[0]
      || members.filter(m => !isTeammate(m)).sort(recent)[0] || [...members].sort(recent)[0];
    sel._fam = members.length > 1 ? { root, members: [...members].sort((p, q) => (p.started_at || 0) - (q.started_at || 0)) } : null;
    sel._famPin = Math.max(...members.map(m => m.pinned || 0));
    sel._famAct = Math.max(...members.map(m => m.last_activity || 0));
    out.push(sel);
  }
  return out;
}
// 고정도 가족(원본·갈래·팀원) 단위 — 칸에 어느 탭이 서 있든 가족 중 고정된 세션의 값으로 그린다(탭을 바꿔도 고정 묶음에 남게)
function pinOf(s) { return s._famPin !== undefined ? s._famPin : (s.pinned || 0); }
// 그 칸의 고정을 실제로 가진 세션들(없으면 빈 목록). 새로 고정할 때는 가족의 원본에 건다
function pinHolders(sid) {
  const s = sessionById(sid);
  return s ? (s._fam ? s._fam.members : [s]).filter(m => m.pinned).sort((a, b) => b.pinned - a.pinned).map(m => m.session_id) : [];
}
function pinRoot(sid) { const s = sessionById(sid); return s && s._fam ? s._fam.root : sid; }
// 카드 보기의 가족 표시: 갈래 ⑂ N · 팀원 👥 N
function famChip(fam) {
  const team = fam.members.filter(isTeammate).length, br = fam.members.length - 1 - team;
  return (br ? `<span class="chip fam" title="갈래 ${br}개 — 칸 보기의 탭에서 고릅니다">⑂ ${br}</span>` : "") +
    (team ? `<span class="chip fam" title="에이전트 팀의 팀원 ${team}명 — 칸 보기의 탭에서 고릅니다">👥 ${team}</span>` : "");
}
function allBoardSessions() {
  return boardFamilies().sort((a, b) => (!!b._famPin - !!a._famPin) ||
    (a._famPin && b._famPin ? a._famPin - b._famPin : b._famAct - a._famAct));
}
// 프로젝트 필터를 건 보드 순서(정렬은 안정 정렬이라 걸러도 순서가 같다)
function boardSessions() {
  const all = allBoardSessions();
  return S.projFilter ? all.filter(s => projectKey(s) === S.projFilter) : all;
}
function sessionById(sid) { return sid ? S.sessions.find(s => s.session_id === sid) || (S.boards || []).find(s => s.session_id === sid) || null : null; }
// 칸 대화(세션 상세)를 받아 보드 캐시에. 칸 보기·브레인 패널·펼친 카드가 같이 쓴다. 여러 칸이면 요청 한 번으로
// (/api/details — 칸마다 따로 받지 않게). 실패하면 예외를 그대로
async function loadDetails(list) {
  if (!list.length) return {};
  const ids = list.map(s => s.session_id + ":" + (S.colLimit.get(s.session_id) || BOARD_TURNS)).join(",");
  const r = await api("GET", "/api/details?ids=" + encodeURIComponent(ids));
  for (const s of list) {
    const detail = r.details[s.session_id];
    if (detail) S.boardCache.set(s.session_id, { sig: sigOf(s), detail });
  }
  return r.details;
}
async function loadDetail(s) {
  const detail = (await loadDetails([s]))[s.session_id];
  if (!detail) throw new Error("no session");
  return detail;
}
async function refreshBoard(force) {
  const all = boardSessions();
  const cols = all.slice(0, S.boardLimit);
  // 화면에 보이는 칸만 대화를 불러온다. 나머지는 스크롤해서 보일 때(IntersectionObserver)
  const stale = cols.filter(s => !s.board && S.visibleCols.has(s.session_id) && (force || !S.boardCache.has(s.session_id) ||
    S.boardCache.get(s.session_id).sig !== sigOf(s)));
  // 브레인 패널이 열려 있으면 브레인 대화도 같은 요청에 싣는다(따로 한 번 더 받지 않게)
  const b = S.brainOpen && brainSession();
  if (b && (force || !S.boardCache.has(b.session_id) || S.boardCache.get(b.session_id).sig !== sigOf(b))) stale.push(b);
  try { await loadDetails(stale); } catch (e) { /* 다음 폴링에서 다시 */ }
  // 보드 칸(터미널·문서)은 대화를 불러오지 않으니, 바뀐 것(문서 저장·AI 끝남·셸 닫힘 …)을 순서 키에 실어 다시 그리게 한다
  const order = cols.map(s => s.session_id + (s.board ? ":" + [s.title, s.mtime, s.msg_sig, s.ai_running, s.alive, s.shell, s.pinned, s.archived].join("/") : "")).join(",") + "|" + all.length;
  if (stale.length || order !== S.boardOrder) {
    S.boardOrder = order;
    renderBoard();
  }
}
// 화면 좌우 한 칸 정도 여유를 두고 보이는 칸을 추적한다
let _colObserver = null;
function colObserver() {
  if (_colObserver) return _colObserver;
  _colObserver = new IntersectionObserver(entries => {
    let added = false;
    for (const en of entries) {
      const sid = en.target.dataset.col;
      if (en.isIntersecting) { if (!S.visibleCols.has(sid)) { S.visibleCols.add(sid); added = true; } }
      else S.visibleCols.delete(sid);
    }
    // 새로 보인 칸: 캐시가 최신이어도 대화를 그려야 하므로 불러온 뒤 항상 다시 그린다
    if (added) refreshBoard().then(renderBoard);
  }, { root: $("#board"), rootMargin: "0px 500px" });
  return _colObserver;
}

// 칸은 한 번 만들고 부분만 갈아 끼운다: 입력창·메모 입력이 다시 만들어지지 않아 한글 조합·포커스가 유지된다.
// 순서는 DOM 을 옮기지 않고 CSS order 로 바꾼다(옮기면 포커스가 풀린다).
function renderBoard() {
  if (S.dragging) return;   // 칸을 끄는 중(빈자리·숨긴 칸이 있다) — 놓은 뒤 다시 그린다
  const board = $("#board");
  // 맨 왼쪽(최근 세션)을 보고 있었다면, 칸 순서가 바뀌어도 맨 왼쪽에 머문다
  const atStart = board.scrollLeft < 40;
  const all = boardSessions();
  const cols = all.slice(0, S.boardLimit);
  const empty = board.querySelector(".board-empty");
  if (!cols.length) {
    board.querySelectorAll(".col, .col-more").forEach(el => el.remove());
    // 처음 쓰는 사람에게는 다음에 할 일을 — 세션이 하나도 없을 때(프로젝트 필터로 가려진 것과 구분)
    const msg = S.projFilter ? "이 프로젝트에 지금 보이는 세션이 없습니다 — 위 프로젝트 버튼에서 「전체 프로젝트」로"
      : `<b>아직 기록된 세션이 없습니다</b>
         <p>Claude Code 에서 요청을 하나 보내면 그 세션이 여기에 나타납니다 — 이미 열려 있는 세션도 다음 요청부터 보입니다.</p>
         <p><span>훅을 설치하기 전의 대화도 가져오려면</span>: <code>/usr/bin/python3 csl/backfill.py --days 7</code></p>
         <p><span>설치 상태 확인</span>: <code>/usr/bin/python3 install.py --check</code></p>`;
    // 같은 글이면 다시 쓰지 않는다(번역된 글을 매번 한국어로 되돌리지 않게)
    if (!empty) { board.insertAdjacentHTML("beforeend", `<div class="board-empty"></div>`); }
    const box = board.querySelector(".board-empty");
    if (box.dataset.msg !== msg) { box.innerHTML = msg; box.dataset.msg = msg; }
    return;
  }
  if (empty) empty.remove();
  const want = new Set(cols.map(s => s.session_id));
  board.querySelectorAll(".col").forEach(el => {
    if (!want.has(el.dataset.col)) { colObserver().unobserve(el); S.visibleCols.delete(el.dataset.col); el.remove(); }
  });
  const br = board.getBoundingClientRect();
  let needFetch = false;
  cols.forEach((s, i) => {
    let col = board.querySelector(`.col[data-col="${CSS.escape(s.session_id)}"]`);
    if (!col) {
      board.insertAdjacentHTML("beforeend", s.board ? boardColSkeleton(s) : colSkeleton(s.session_id));
      col = board.lastElementChild;
      colObserver().observe(col);
      if (!s.board) {
        setupThread(col.querySelector(".col-thread"));
        restoreComposerDraft(col.querySelector(".composer"));
      }
      // 관찰 신호는 한 박자 늦게 온다. 새 칸이 이미 화면 안이면 바로 보이는 칸으로 치고 불러온다
      col.style.order = i + 1;
      const r = col.getBoundingClientRect();
      if (br.width && r.right > br.left - 500 && r.left < br.right + 500) {
        S.visibleCols.add(s.session_id);
        if (!S.boardCache.has(s.session_id)) needFetch = true;
      }
    }
    // 고정 칸 묶음의 마지막 칸 뒤에 간격을 둬서 고정/나머지를 구분
    const lastPin = !!pinOf(s) && !(cols[i + 1] && pinOf(cols[i + 1]));
    if (s.board) updateBoardCol(col, s, i + 1, lastPin); else updateCol(col, s, i + 1, lastPin);
  });
  if (needFetch) setTimeout(() => refreshBoard().then(renderBoard), 0);
  if (atStart && board.scrollLeft !== 0) board.scrollLeft = 0;
  let more = board.querySelector(".col-more");
  if (all.length > cols.length) {
    if (!more) {
      board.insertAdjacentHTML("beforeend", '<div class="col-more"><button class="btn" data-action="board-more"></button></div>');
      more = board.lastElementChild;
    }
    more.style.order = 100000;
    more.querySelector("button").textContent = `세션 ${all.length - cols.length}개 더 보기`;
  } else if (more) {
    more.remove();
  }
  if (S.expandedCard) updateExpandedCard();   // 카드 보기에서 펼친 카드의 칸도 같은 내용으로
}
// 칸 머리 첫 줄이 칸보다 넓으면(칩이 많을 때) 글자를 줄인다: 먼저 원격·고정·터미널 버튼을 아이콘만(tight),
// 그래도 넘치면 「몇 분 전」도 숨긴다(tighter). 메뉴(⋮)와 터미널 버튼은 늘 보인다. 칸 너비가 바뀌어도 다시 잰다
function fitTop(top) {
  if (!top || !top.offsetParent) return;
  const over = () => top.scrollWidth > top.clientWidth + 7;   // ⋮ 버튼은 원래 6px 바깥으로 나가 있다(margin-right: -6px)
  top.classList.remove("tight", "tighter");
  if (!over()) return;
  top.classList.add("tight");
  if (over()) top.classList.add("tighter");
}
const TOP_RO = new ResizeObserver(es => { for (const e of es) fitTop(e.target.querySelector(".col-top")); });
function colSkeleton(sid) {
  return `<div class="col" data-col="${esc(sid)}" data-drop-sid="${esc(sid)}">
    <div class="col-head"></div>
    <div class="fam-tabs" hidden></div>
    <div class="col-fixed">
      <div class="cf-label"></div>
      <form class="note-add" data-form="col-note" data-sid="${esc(sid)}">
        <textarea class="note-input" rows="1" placeholder="다음에 할 일  Enter 추가 · ⌘Enter 추가+끝나면 보내기 · Shift+Enter 줄바꿈" autocomplete="off"></textarea>
        <div class="note-add-imgs cmp-images"></div>
      </form>
      <div class="cf-notes"></div>
      <div class="cf-recap"></div>
    </div>
    <div class="col-thread"></div>
    <div class="col-term" hidden></div>
    <div class="ask-card" data-sid="${esc(sid)}" hidden></div>
    <div class="ask-card prompt-card" data-sid="${esc(sid)}" hidden></div>
    ${composerHtml(sid)}
    <div class="col-resize" data-resize-sid="${esc(sid)}" title="끌어서 너비 조절 · 더블클릭: 기본 너비"></div>
  </div>`;
}
function updateCol(col, s, rank, lastPin) {
  const sid = s.session_id;
  const d = displayState(s);
  const cached0 = S.boardCache.get(sid);
  const live = S.live.get(sid);
  const key = [sigOf(s), famSig(s), bgSig(s), dirtySig(s), rank, live ? live.key : "", S.renaming === sid,
    S.recapOpen.has(sid), S.uiVer, S.visibleCols.has(sid), S.activeCol === sid, lastPin, S.notesFolded.has(sid), isImporting(s),
    s.term_label, s.term_name, s.bg, s.running].join("|");
  if (col._key === key && col._detail === (cached0 && cached0.detail)) return;
  col._key = key;
  col._detail = cached0 && cached0.detail;
  const pinned = !!pinOf(s);
  col.className = "col " + d + (S.activeCol === sid ? " active" : "") + (pinned ? " pinned" : "") + (lastPin ? " pin-last" : "") +
    (S.notesFolded.has(sid) ? " notes-folded" : "") + (isImporting(s) ? " importing" : "");
  // 보드 터미널이 아닌 곳에서 도는 세션: 머리 아래 띠로 어디서 도는지 보이고 바로 가져오게
  const away = elsewhere(s);
  col.classList.toggle("elsewhere", !!away);
  col.classList.toggle("off-col", !!(away && away.btn));
  let band = col.querySelector(".col-elsewhere");
  if (away) {
    if (!band) {
      band = document.createElement("div");
      band.className = "col-elsewhere";
      col.querySelector(".col-head").after(band);
    }
    const html = `<span class="ce-where">${esc(away.where)}</span><span class="ce-note" title="${esc(away.note)}">${esc(away.note)}</span>
      <button class="ce-btn" data-action="import-open" data-sid="${esc(sid)}" title="⇄ 보드 터미널로 가져오기 — 같은 대화로 보드 터미널에서 다시 엽니다">${away.btn || "⇄ 가져오기"}</button>`;
    band.classList.toggle("off", !!away.btn);
    setHtml(band, html);
  } else if (band) band.remove();
  let imp = col.querySelector(".col-importing");
  if (isImporting(s) && !imp) {
    imp = document.createElement("div");
    imp.className = "col-importing";
    imp.innerHTML = `<span class="spin"></span> 보드 터미널로 가져오는 중… <span class="muted">지금 터미널에서 끝내고 같은 대화로 다시 엽니다</span>`;
    col.append(imp);
  } else if (!isImporting(s) && imp) imp.remove();
  col.style.order = rank;
  col.style.setProperty("--proj", projColor(s.project_root || s.cwd));
  applyColWidth(col, sid);
  const cached = S.boardCache.get(sid);
  const detail = cached && cached.detail;
  const turns = detail ? detail.turns : [];
  const notes = detail ? detail.notes : [];
  const open = notes.filter(n => !n.done).length;
  const hidden = Math.max(0, (s.turn_count || 0) - turns.length);

  if (S.renaming !== sid) {
    setHtml(col.querySelector(".col-head"), `
      <div class="col-top">
        ${s.unread ? `<span class="unread-badge" title="확인 안 한 새 응답">${BELL_SVG}</span>` : ""}
        <span class="rank">${rank}</span>
        <span class="pill ${d}"><span class="dot ${d}"></span>${s.compact_at ? "컴팩트 중" : STATE_LABEL[d]}</span>
        <span class="ago" data-ts="${s.last_activity}">${rel(s.last_activity)}</span>
        ${ctxChip(s, d)}
        ${bgtChip(s)}
        <span class="spacer"></span>
        ${remoteBadge(s, d)}
        ${pinned ? `<button class="col-pin on" data-action="pin" data-sid="${esc(sid)}" title="고정 해제 ( P )" data-icon="📌">📌 고정됨</button>` : ""}
        ${forkChip(s, d)}
        ${s.term_attach && d !== "ended" ? `<button class="col-term-btn ${TERM.open.has(sid) ? "on" : ""}" data-action="term-toggle" data-sid="${esc(sid)}" data-icon="${TERM.open.has(sid) ? "💬" : "⌨"}"
          title="${TERM.open.has(sid) ? "대화 보기로" : s.term_web ? "이 세션의 터미널을 칸 안에서 열기(보드 터미널)" : "이 세션의 tmux 창을 칸 안에서 열기 — 닫아도 tmux 세션은 그대로"}">${TERM.open.has(sid) ? "💬 대화" : "⌨ 터미널"}</button>` : ""}
        <button class="col-zoom-btn" data-action="col-zoom" data-sid="${esc(sid)}" title="이 칸 크게 보기 — 누를 때마다 반 → 가득 → 평소 ( Z )" aria-label="이 칸 크게 보기"><span class="cz-in">⤢</span><span class="cz-out">⤡</span></button>
        <button class="col-menu-btn" data-action="col-menu" data-sid="${esc(sid)}" title="보관 · 원격 · 고정" aria-label="칸 메뉴">⋮</button>
      </div>
      <div class="title-line col-title-line">
        <span class="col-title title-text" title="${esc(sessionTitle(s))}">${esc(sessionTitle(s))}</span>
        ${renameBtn(sid, d)}
      </div>
      ${s.brain /* 브레인은 늘 같은 폴더라 프로젝트·브랜치 칩은 빼고 모델·권한만(패널이 좁아 윗줄에 두면 넘친다) */
        ? `<div class="col-meta">${modelChip(s, d)}${permChip(s, d)}</div>`
        : `<div class="col-meta"><span class="chip proj" title="${esc(s.project_root || "")}">${esc(s.project_name || base(s.project_root))}</span>${placeChips(s)}${diffChip(s)}${modelChip(s, d)}${permChip(s, d)}</div>`}
      ${goalChip(s, d)}
      ${s.state_detail && (d === "waiting" || d === "error") ? `<div class="col-detail">${esc(s.state_detail)}</div>` : ""}`);
  }
  const folded = S.notesFolded.has(sid);
  // 「끝나면 보내기」 예약: 건 순서대로 번호(응답이 끝날 때마다 1번부터 하나씩 보낸다)
  const queued = notes.filter(n => n.queued_at && !n.done).sort((p, q) => p.queued_at - q.queued_at);
  const queuedNo = new Map(queued.map((n, i) => [n.id, i + 1]));
  setHtml(col.querySelector(".cf-label"), `<button class="cf-toggle" data-action="toggle-notes" data-sid="${esc(sid)}"
      aria-expanded="${!folded}" title="${folded ? "넥스트 액션 펼치기" : "넥스트 액션 접기"}">
      <span class="cf-chev">${folded ? "▸" : "▾"}</span>넥스트 액션
      ${open ? `<span class="note-count" title="남은 할 일">할 일 ${open}</span>` : ""}
      ${notes.length - open ? `<span class="note-count done" title="완료한 할 일">✓ 완료 ${notes.length - open}</span>` : ""}
      ${queued.length ? `<span class="note-count queued" title="응답이 끝나면 차례로 보낼 할 일">⏭ 예약 ${queued.length}</span>` : ""}</button>`);
  // 고치는 중인 할 일이 있으면 목록을 갈지 않는다(갈면 편집칸이 사라지며 쓰다 만 글이 저장되거나 편집이 끊긴다)
  if (!col.querySelector('.note-text[contenteditable="true"]'))
    setHtml(col.querySelector(".cf-notes"), notes.length ? `<ul class="notes">${queued.concat(notes.filter(n => !queuedNo.has(n.id)))
      .map(n => noteHtml(n, queuedNo.get(n.id), queued.length)).join("")}</ul>` : "");
  // 완료·삭제 뒤 목록을 다시 그리면 포커스가 사라지므로, 키보드로 고르던 할 일(또는 그다음 것)에 되돌린다
  if (S.noteFocus && (!document.activeElement || document.activeElement === document.body)) {
    const li = col.querySelector(`.note[data-nid="${S.noteFocus}"]`);
    if (li) li.focus({ preventScroll: true });
  }
  setHtml(col.querySelector(".cf-recap"), s.recap
    ? `<div class="col-recap ${S.recapOpen.has(sid) ? "open" : ""}" data-action="toggle-recap" data-sid="${esc(sid)}"
        title="${S.recapOpen.has(sid) ? "접기" : "눌러서 전체 보기"}"><div class="recap-clamp"><b>recap</b>${recapAge(s)} ${esc(s.recap)}</div></div>` : "");
  const xm = $("#xMenu");   // 펼쳐 둔 백그라운드 작업 목록이면 내용만 새로
  if (!xm.hidden && xm.dataset.kind === "bgt" && xm.dataset.sid === sid) setHtml(xm, bgTasksHtml(s) || BGT_EMPTY);
  const thread = col.querySelector(".col-thread");
  const visible = S.visibleCols.has(sid);
  // 안 보이는 칸은 이미 그린 대화를 그대로 두고(다시 보일 때 갱신), 한 번도 안 그렸으면 자리만 표시
  if (visible || !thread._html) {
    const html = detail ? threadHtml(s, turns, hidden, detail.asides)
      : `<div class="col-lazy">${visible ? "불러오는 중…" : "스크롤해서 보이면 대화를 불러옵니다"}</div>`;
    if (setHtml(thread, html)) {
      const a = S.anchor.get(sid);
      if (a && detail) {
        // 위에 이전 대화가 붙은 만큼 내려서, 읽던 곳이 그대로 보이게
        thread.scrollTop = thread.scrollHeight - a.h + a.top;
        S.anchor.delete(sid);
      } else if (thread._pinned) {
        stickBottom(thread);
      }
    }
  }
  updateFamily(col, s);
  syncTerm(col, s);
  updateComposer(col.querySelector(".composer"), s);
  updateAsk(col.querySelector(".ask-card"), s);
  updatePrompt(col.querySelector(".prompt-card"), s);
  fitTop(col.querySelector(".col-top"));
  if (!col._topRo) { col._topRo = true; TOP_RO.observe(col); }
}
function famSig(s) {
  return s._fam ? s._fam.members.map(m => m.session_id + ":" + displayState(m) + ":" + (m.title || "") + ":" + (m.term_handle || "")).join(",") : "";
}
// 갈래 탭(칸 위) + 고른 갈래가 지금 터미널에 없을 때의 「전환 · 열기」 줄(입력창 위)
function updateFamily(col, s) {
  const box = col.querySelector(".fam-tabs");
  if (!box) return;
  const fam = s._fam;
  box.hidden = !fam;
  setHtml(box, fam ? fam.members.map(m => {
    const d = displayState(m), team = isTeammate(m);
    const label = m.session_id === fam.root ? (fam.members.some(isTeammate) && !fam.members.some(isBranch) ? "팀장" : "원본")
      : team ? "👥 " + (m.team_agent || sessionTitle(m)) : "⑂ " + sessionTitle(m);
    const tip = team ? `에이전트 팀의 팀원 ${m.team_agent || ""}${m.title ? " — " + sessionTitle(m) : ""}` : sessionTitle(m) + (m.bg ? " · 백그라운드(/fork)" : "");
    return `<button class="fam-tab ${m.session_id === s.session_id ? "on" : ""} ${team ? "team" : ""}" data-action="fam-tab" data-root="${esc(fam.root)}" data-sid="${esc(m.session_id)}"
      title="${esc(tip)}"><span class="dot ${d}"></span><span class="${m.session_id === fam.root ? "fam-tab-o" : "fam-tab-t"}">${esc(clamp(label, 28))}</span>${m.bg ? '<span class="fam-bg">백그라운드</span>' : ""}${
      m.session_id === fam.root || team ? "" : `<span class="fam-x" data-action="fam-close" data-sid="${esc(m.session_id)}" title="이 갈래 닫기(두 번 눌러 확인)">✕</span>`}</button>`;
  }).join("") : "");
  let bar = col.querySelector(".fam-bar");
  // 팀원은 팀장이 관리한다 — 끝났거나 터미널이 없어도 전환·이어 열기를 권하지 않는다
  const need = fam && !isTeammate(s) && (s.state === "ended" || (s.bg && !s.term_handle));
  if (!need) { if (bar) bar.remove(); return; }
  if (!bar) {
    bar = document.createElement("div");
    bar.className = "fam-bar";
    col.querySelector(".composer").before(bar);
  }
  const live = fam.members.find(m => m !== s && m.state !== "ended" && m.term_handle && m.term_handle === s.term_handle);
  setHtml(bar, s.bg && !s.term_handle
    ? `<span>🛰 터미널 없이 백그라운드에서 도는 갈래입니다</span><span class="spacer"></span><button class="btn primary" data-action="fam-attach" data-sid="${esc(s.session_id)}">열기 · 이어서 대화</button>`
    : live ? `<span>이 터미널은 지금 「${esc(clamp(sessionTitle(live), 24))}」에 있습니다</span><span class="spacer"></span><button class="btn primary" data-action="fam-switch" data-sid="${esc(s.session_id)}">이 갈래로 전환</button>`
    : `<span>이 갈래는 지금 어느 터미널에도 열려 있지 않습니다</span><span class="spacer"></span><button class="btn primary" data-action="fam-switch" data-sid="${esc(s.session_id)}">새 탭에서 이어 열기</button>`);
}
// 떠 있는 메뉴·창은 바깥을 누르면 닫는다: [메뉴, 눌러도 닫지 않을 곳(메뉴 자신과 여는 버튼)]
const CLOSE_OUTSIDE = [
  ["#projMenu", "#projMenu, #projBtn"], ["#colMenu", "#colMenu, .col-menu-btn"], ["#newMenu", "#newMenu, #newBtn"],
  ["#ctxMenu", "#ctxMenu, .ctx-chip"], ["#modelMenu", "#modelMenu, .model-chip"], ["#topMenu", ".top-menu-wrap"],
  ["#searchPanel", "#searchPanel, #search"],
  ["#xMenu", "#xMenu, .turn-rw, .perm-chip, .goal-chip, #agentsBtn, .col-menu-btn, #colMenu, .bgt-chip, .fork-chip"],
];
document.addEventListener("pointerdown", e => {
  for (const [menu, keep] of CLOSE_OUTSIDE) {
    const m = $(menu);
    if (m && !m.hidden && !(e.target.closest && e.target.closest(keep))) m.hidden = true;
  }
});
// ------------------------------------------------------------------ Claude Code 명령을 보드에서: 되돌리기·권한 모드·목표·갈래·백그라운드·변경
// 공용 작은 메뉴(#xMenu) — 고른 버튼 아래에 뜬다. 바깥을 누르거나 Esc 면 닫힌다
S.rwOpen = new Set();   // 되돌려 지운 턴을 펼쳐 본 세션
function btnOfCol(sid, sel) { const c = colEl(sid); return c && c.querySelector(sel); }
function openXMenu(anchor, html, align = "left") {
  const m = $("#xMenu");
  m.classList.remove("wide", "bgt-menu", "ib-pick-menu");
  m.style.width = "";
  m.dataset.kind = "";
  m.innerHTML = html;
  m.hidden = false;
  if (anchor) placeMenu(m, anchor, align);
  const first = m.querySelector("input, textarea");
  if (first) first.focus();
  return m;
}
function closeXMenu() { $("#xMenu").hidden = true; }
async function sendCmd(sid, text) {
  try {
    const r = await api("POST", `/api/sessions/${sid}/send`, { text, images: [] });
    toast(r.message || "보냈습니다");
  } catch (err) { toast(err.message); }
  return refresh(sid);
}
// 되돌리기(/rewind): 이 요청 앞으로 — 대화만 / 코드와 대화 / 코드만 / 요약
const RW_OPTS = [["conversation", "↶ 대화 되돌리기", "이 요청과 그 뒤 대화를 지웁니다(코드는 그대로)"],
  ["both", "↶ 코드와 대화 되돌리기", "그 뒤에 바뀐 파일도 되돌립니다"], ["code", "↶ 코드만 되돌리기", "대화는 두고 파일만"],
  ["summarize_from", "≋ 여기부터 요약", "이 요청부터의 대화를 요약으로 줄입니다"], ["summarize_upto", "≋ 여기까지 요약", "이 요청 앞까지를 요약으로 줄입니다"]];
function openRewindMenu(btn) {
  const sid = btn.dataset.sid, tid = btn.dataset.tid;
  openXMenu(btn, `<div class="xm-head">이 요청 앞으로 <span class="muted">/rewind</span></div>` +
    RW_OPTS.map(([k, label, hint]) => `<button class="cm-item xm-opt" data-rw="${k}" data-sid="${esc(sid)}" data-tid="${tid}">${label}<span class="muted">${hint}</span></button>`).join("") +
    `<div class="xm-note muted">코드 되돌리기는 그 뒤에 파일을 바꿨을 때만 고를 수 있습니다</div>`);
}
async function doRewind(b) {
  closeXMenu();
  const sid = b.dataset.sid, tid = +b.dataset.tid, mode = b.dataset.rw;
  const removes = mode === "conversation" || mode === "both";
  const det = S.boardCache.get(sid);
  const first = det && det.detail ? (det.detail.turns.find(t => t.id >= tid && t.status !== "rewound") || {}).id : tid;
  S.rwPending = { sid, tid, removes, first: first || tid };
  S.uiVer++;
  renderBoard();
  try {
    const r = await api("POST", `/api/sessions/${sid}/rewind`, { turn_id: tid, mode });
    if (removes) await collapseRewound(sid);   // 지워질 턴이 접혀 들어간 뒤에 새 상태로
    S.rwPending = null;
    S.rwJust = sid;
    setTimeout(() => { if (S.rwJust === sid) S.rwJust = null; }, 1500);
    toast(r.message + (r.prompt ? " — 지운 요청을 입력창에 넣었습니다" : ""));
    await refresh(sid);
    if (r.prompt && await fillComposer(sid, r.prompt)) {
      const cmp = colEl(sid) && colEl(sid).querySelector(".composer");
      if (cmp) { cmp.classList.remove("rw-flash"); void cmp.offsetWidth; cmp.classList.add("rw-flash"); }
    }
  } catch (err) {
    S.rwPending = null;
    S.uiVer++;
    renderBoard();
    toast(err.message);
  }
}
// 지워질 턴(흐리게 표시한 것)과 진행 줄을 높이·투명도를 줄이며 접는다
async function collapseRewound(sid) {
  const col = colEl(sid);
  const els = col ? [...col.querySelectorAll(".turn.rw-pending, .rw-banner")] : [];
  if (!els.length || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  for (const el of els) el.style.height = el.offsetHeight + "px";
  void col.offsetHeight;   // 지금 높이를 확정한 뒤 0 으로 — 그래야 전환이 걸린다
  for (const el of els) { el.classList.add("rw-collapsing"); el.style.height = "0px"; }
  await new Promise(res => setTimeout(res, 520));
}
// 권한 모드 칩(⏵⏵ 자동 · ⏸ 수동 · 편집 허용 · 계획) — 누르면 고르기, 서버가 Shift+Tab 으로 맞춘다
const PERM = { auto: ["⏵⏵", "자동"], default: ["⏸", "수동"], acceptEdits: ["⏵⏵", "편집 허용"], plan: ["⏸", "계획"], bypassPermissions: ["⚠", "전부 허용"] };
// 갈래·곁일(/branch·/fork·/subtask) — 입력창 옆에 두면 입력칸이 좁아지고, 칩 줄에 두면 줄이 넘쳐 머리가 길어져서 ⋮ 옆 아이콘으로
function forkChip(s, d) {
  if (d === "ended" || s.bg && !s.term_handle) return "";
  return `<button class="fork-chip" data-action="fork-menu" data-sid="${esc(s.session_id)}" title="갈래·곁일 — /branch(여기서 갈라 대화) · /fork(갈라서 맡기기) · /subtask(곁일)" aria-label="갈래·곁일">⑂</button>`;
}
function permChip(s, d) {
  const p = PERM[s.permission_mode];
  if (!p || d === "ended" || s.bg && !s.term_handle) return "";
  return `<button class="perm-chip pm-${esc(s.permission_mode)}" data-action="perm-menu" data-sid="${esc(s.session_id)}" title="권한 모드 — 눌러서 바꾸기(터미널의 Shift+Tab)">${p[0]} ${p[1]} ▾</button>`;
}
// keyboard: ⇧Tab 으로 연 것 — 지금 모드에 포커스를 두고 ⇧Tab·↓ 다음 · ↑ 이전 · Enter 적용 · Esc 닫기(포커스는 입력칸으로 돌아간다)
function openPermMenu(btn, keyboard) {
  const s = sessionById(btn.dataset.sid);
  if (!s) return;
  S.permReturn = keyboard ? document.activeElement : null;
  const opts = [["auto", "Claude 가 판단해 진행(위험한 것만 묻기)"], ["default", "도구를 쓸 때마다 묻기"], ["acceptEdits", "파일 고치기는 묻지 않기"], ["plan", "계획만 세우고 고치지 않기"]];
  const m = openXMenu(btn, `<div class="xm-head">권한 모드 <span class="muted">⇧Tab·↑↓ 고르기 · Enter 바꾸기 · Esc 닫기</span></div>` + opts.map(([k, hint]) =>
    `<button class="cm-item xm-opt ${s.permission_mode === k ? "on" : ""}" data-perm="${k}" data-sid="${esc(s.session_id)}">${PERM[k][0]} ${PERM[k][1]}<span class="muted">${hint}</span></button>`).join(""));
  m.dataset.kind = "perm";
  (m.querySelector("[data-perm].on") || m.querySelector("[data-perm]")).focus();
}
function permMenuMove(d) {
  const opts = [...$("#xMenu").querySelectorAll("[data-perm]")];
  if (!opts.length) return;
  const i = opts.indexOf(document.activeElement);
  opts[i < 0 ? (d > 0 ? 0 : opts.length - 1) : (i + d + opts.length) % opts.length].focus();
}
function closePermMenu() {
  closeXMenu();
  const back = S.permReturn;
  S.permReturn = null;
  if (back && back.isConnected) back.focus();
}
async function doPerm(b) {
  closePermMenu();
  try { await api("POST", `/api/sessions/${b.dataset.sid}/permission`, { mode: b.dataset.perm }); toast(`권한 모드: ${PERM[b.dataset.perm][1]}`); }
  catch (err) { toast(err.message); }
  return refresh(b.dataset.sid);
}
// ⇧Tab: 그 칸의 권한 모드 메뉴(터미널의 Shift+Tab 처럼 바로 돌리지 않고 골라서 Enter). 입력칸에서, 또는 칸만 골라 둔 상태에서.
// 서식 입력칸의 목록 안에서는 내어쓰기로 쓰이므로 두고, 권한 모드를 모르는(칩이 없는) 세션도 브라우저 기본대로
document.addEventListener("keydown", e => {
  if (e.key !== "Tab" || !e.shiftKey || e.metaKey || e.ctrlKey || e.altKey || imeBusy(e)) return;
  const m = $("#xMenu");
  if (!m.hidden && m.dataset.kind === "perm") { e.preventDefault(); e.stopImmediatePropagation(); return permMenuMove(1); }
  const t = e.target;
  let sid = null;
  if (t.classList && t.classList.contains("cmp-text")) sid = t.closest(".composer").dataset.sid;
  else if (t.classList && t.classList.contains("cmp-rich")) {
    const a = document.getSelection().anchorNode;
    if (a && (a.nodeType === 1 ? a : a.parentElement).closest("li")) return;
    sid = t.closest(".composer").dataset.sid;
  } else if (!isTyping()) sid = S.activeCol;
  const chip = sid && visibleFirst(`.perm-chip[data-sid="${CSS.escape(sid)}"]`);
  if (!chip) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  openPermMenu(chip, true);
}, true);
// 목표(/goal): 칸 머리에 🎯 — 달성하면 ✔(30분 동안 보인다)
function goalChip(s, d) {
  const g = s.goal;
  if (!g || d === "ended" || g.met && (!g.at || Date.now() / 1000 - g.at > 1800)) return "";
  return `<button class="goal-chip ${g.met ? "met" : ""}" data-action="goal-menu" data-sid="${esc(s.session_id)}" title="${esc(g.condition)}">${g.met ? "✔ 목표 달성" : "🎯 목표"} <span class="goal-t">${esc(clamp(g.condition, 60))}</span></button>`;
}
function openGoalMenu(sid, anchor) {
  const s = sessionById(sid);
  if (!s) return;
  const g = s.goal && !s.goal.met ? s.goal : null;
  openXMenu(anchor, `<div class="xm-head">🎯 목표 <span class="muted">/goal — 멈추기 전에 이 조건을 확인합니다</span></div>
    <form class="xm-form" data-goal-form data-sid="${esc(sid)}">
      <textarea rows="2" name="cond" placeholder="예: 테스트가 모두 통과하고 린트 오류가 없는지">${esc(g ? g.condition : "")}</textarea>
      <div class="xm-btns">${g ? `<button type="button" class="btn" data-goal-clear>풀기</button>` : ""}<span class="spacer"></span><button class="btn primary">${g ? "바꾸기" : "걸기"}</button></div>
    </form>`, "right");
}
// ⑂ 갈래·곁일: /branch(여기서 갈라 대화) · /fork(갈라서 맡기기, 백그라운드) · /subtask(곁일 — 결과가 이 대화로)
function openForkMenu(btn) {
  const sid = btn.dataset.sid, s = sessionById(sid);
  if (!s) return;
  const busy = ["working", "waiting"].includes(s.state);
  openXMenu(btn, `<div class="xm-head">⑂ 갈래·곁일</div>
    ${busy ? '<div class="xm-note warn">작업 중에는 쓸 수 없습니다 — 끝난 뒤에(명령이 요청으로 끼어들지 않게)</div>' : ""}
    <form class="xm-form" data-fork-form="branch" data-sid="${esc(sid)}"><b>⑂ 여기서 갈라 대화하기</b> <span class="muted">/branch — 이 터미널이 갈래로 넘어가고, 원본은 탭에 남습니다</span>
      <div class="xm-row"><input name="v" placeholder="갈래 이름(비우면 첫 요청에서)"><button class="btn" ${busy ? "disabled" : ""}>갈라 대화</button></div></form>
    <form class="xm-form" data-fork-form="fork" data-sid="${esc(sid)}"><b>🛰 갈라서 맡기기</b> <span class="muted">/fork — 대화를 물려받은 백그라운드 세션이 따로 일합니다</span>
      <div class="xm-row"><input name="v" placeholder="맡길 일" required><button class="btn" ${busy ? "disabled" : ""}>맡기기</button></div></form>
    <form class="xm-form" data-fork-form="subtask" data-sid="${esc(sid)}"><b>↪ 곁일 맡기기</b> <span class="muted">/subtask — 서브에이전트가 하고 결과를 이 대화로 돌려줍니다</span>
      <div class="xm-row"><input name="v" placeholder="곁일" required><button class="btn" ${busy ? "disabled" : ""}>맡기기</button></div></form>`);
}
$("#xMenu").addEventListener("click", e => {
  const ip = e.target.closest("[data-ibpick]");
  if (ip) return pickInboxSession(ip);
  const so = e.target.closest("[data-sendopt]");
  if (so) {
    closeXMenu();
    const el = visibleFirst(`.composer[data-sid="${CSS.escape(so.dataset.sid)}"]`);
    if (!el) return;
    if (!composerText(el).trim() && !(S.compose.get(so.dataset.sid) || []).length) { toast("입력창이 비어 있습니다"); return; }
    return so.dataset.sendopt === "now" ? sendCompose(el) : queueCompose(el, so.dataset.sendopt === "queue");
  }
  const rw = e.target.closest("[data-rw]");
  if (rw) return doRewind(rw);
  const pm = e.target.closest("[data-perm]");
  if (pm) return doPerm(pm);
  const clr = e.target.closest("[data-goal-clear]");
  if (clr) { const f = clr.closest("form"); closeXMenu(); return sendCmd(f.dataset.sid, "/goal clear"); }
  const ag = e.target.closest("[data-agent]");
  if (ag) return agentAction(ag);
  const go = e.target.closest("[data-agent-go]");
  if (go) { closeXMenu(); setView("board"); return focusCol(go.dataset.agentGo); }
});
$("#xMenu").addEventListener("submit", e => {
  e.preventDefault();
  const f = e.target;
  if (f.matches("[data-goal-form]")) {
    const v = f.cond.value.trim();
    if (!v) return;
    closeXMenu();
    return sendCmd(f.dataset.sid, "/goal " + v.replace(/\s+/g, " "));
  }
  if (f.matches("[data-fork-form]")) {
    const v = f.v.value.trim().replace(/\s+/g, " ");
    const kind = f.dataset.forkForm;
    if (kind !== "branch" && !v) return;
    closeXMenu();
    return sendCmd(f.dataset.sid, "/" + kind + (v ? " " + v : ""));
  }
});
$("#xMenu").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !imeBusy(e) && e.target.matches("textarea")) { e.preventDefault(); e.target.form.requestSubmit(); }
  if ($("#xMenu").dataset.kind === "perm") {   // 권한 모드 메뉴: 보드의 전역 단축키(Enter·화살표)로 새지 않게 여기서 끝낸다
    const k = e.key;
    if (k === "ArrowDown" || k === "ArrowUp") { e.preventDefault(); e.stopPropagation(); return permMenuMove(k === "ArrowDown" ? 1 : -1); }
    if (k === "Enter" && e.target.matches("[data-perm]")) { e.preventDefault(); e.stopPropagation(); return doPerm(e.target); }
    if (k === "Escape") { e.preventDefault(); e.stopPropagation(); return closePermMenu(); }
  }
});
// 백그라운드 세션(claude agents): 상단 🛰 N — 상태·이름·폴더, 열기(새 탭에 attach)·멈추기
S.agents = [];
async function loadAgents() {
  try { S.agents = (await api("GET", "/api/agents")).items; } catch (e) { return; }
  const b = $("#agentsBtn");
  const n = S.agents.filter(x => x.alive && x.state !== "done").length;
  b.hidden = !S.agents.length;
  setHtml(b, `🛰 ${S.agents.length}${n ? ` <span class="ag-live">· 진행 ${n}</span>` : ""}`);
  if (!$("#xMenu").hidden && $("#xMenu").dataset.kind === "agents") drawAgents();
}
// done 은 「응답을 마치고 다음 지시를 기다림」(프로세스는 살아 있다). pid 가 없으면 프로세스 없이 기록만 남은 것
const AG_STATE = { working: ["working", "작업 중"], blocked: ["waiting", "확인 필요"], done: ["done", "응답 완료 · 대기"], idle: ["idle", "대기"] };
function drawAgents(anchor) {
  const rows = S.agents.map(x => {
    const [cls, label] = !x.alive ? ["ended", "꺼짐 · 기록만 남음"] : AG_STATE[x.state] || ["idle", x.state || x.status || ""];
    const k = x.board;
    const on = k && !k.archived && x.alive;
    // 무엇을 하던 세션인지: 보드에 기록된 마지막 요청·응답(없으면 이름만)
    const last = k && (k.lp_prompt || k.lr_response) ? `<div class="ag-last">${k.lp_prompt ? `<span>↳ ${esc(k.lp_prompt)}</span>` : ""}${
      k.lr_response ? `<span class="muted">${esc(k.lr_response)}</span>` : ""}</div>` : "";
    const btns = x.alive
      ? `${on ? `<button class="btn ghost" data-agent-go="${esc(x.sessionId)}" title="보드의 그 세션 칸(대화 전체)으로">칸으로</button>` : ""}
        <button class="btn ghost" data-agent="attach" data-id="${esc(x.id)}" title="Orca 새 탭에서 claude attach — 그 세션 화면에 붙어 직접 조작">열기</button>
        <button class="btn ghost" data-agent="stop" data-id="${esc(x.id)}" title="claude stop — 프로세스를 끝냄(대화는 남음)">멈추기</button>`
      : `<button class="btn ghost" data-agent="rm" data-id="${esc(x.id)}" title="claude rm — 목록에서 지움(두 번 누르기)">목록에서 지우기</button>`;
    return `<div class="ag-row ${x.alive ? "" : "dead"}"><span class="dot ${cls}"></span>
      <div class="ag-main"><b>${esc(clamp(x.name || x.id, 50))}</b><div class="muted">${esc(label)} · ${esc(base(x.cwd || ""))}${
        x.startedAt ? " · " + rel(x.startedAt / 1000) + " 시작" : ""}${k && k.lr_at ? " · 마지막 응답 " + rel(k.lr_at) : ""}</div>${last}</div>
      <div class="ag-btns">${btns}</div></div>`;
  }).join("") || '<div class="muted xm-note">백그라운드 세션이 없습니다</div>';
  const m = openXMenu(anchor || null, `<div class="xm-head">🛰 백그라운드 세션 <span class="muted">claude agents · 터미널 없이 도는 세션(/fork·/background·--bg·웹에서 시작)</span></div>${rows}`, "right");
  m.dataset.kind = "agents";
  m.classList.add("wide");
}
const AG_RM = { id: "", at: 0 };
async function agentAction(b) {
  if (b.dataset.agent === "rm" && !(AG_RM.id === b.dataset.id && Date.now() - AG_RM.at < 3000)) {
    AG_RM.id = b.dataset.id; AG_RM.at = Date.now();
    b.textContent = "한 번 더 누르면 지움";
    b.classList.add("armed");
    return;
  }
  b.disabled = true;
  try { toast((await api("POST", `/api/agents/${b.dataset.id}/${b.dataset.agent}`, {})).message); }
  catch (err) { toast(err.message); }
  closeXMenu();
  loadAgents();
  return refresh();
}
$("#agentsBtn").addEventListener("click", e => {
  const m = $("#xMenu");
  if (!m.hidden && m.dataset.kind === "agents") return closeXMenu();
  drawAgents(e.currentTarget);
});
setInterval(() => { if (!document.hidden) loadAgents(); }, 10000);
setTimeout(loadAgents, 1500);
// ------------------------------------------------------------------ 세션이 띄운 백그라운드 작업(서브에이전트·백그라운드 명령)
// 칸 머리(넥스트 액션 아래)에 목록 — 진행 중인 것과 끝난 지 30분 안 된 것. 누르면 그 작업이 지금 무엇을 하는지 창으로
const BGT_ICON = { agent: "🤖", bash: "⌁" };
const BGT_STATUS = { completed: ["done", "끝남"], failed: ["error", "실패"], killed: ["ended", "중단"], stopped: ["ended", "중단"] };
function bgSig(s) {
  const ts = s.bg_tasks || [];
  // 진행 중이면 경과 시간(분)이 바뀌므로 30초마다 다시 그린다
  return ts.map(x => x.id + x.status).join() + (ts.some(x => x.status === "running") ? Math.floor(Date.now() / 30000) : "");
}
function bgTaskTime(x) {
  if (x.status === "running") return mins(Date.now() / 1000 - x.started_at) + "째";
  return x.finished_at ? rel(x.finished_at) + " · " + mins(x.finished_at - x.started_at) : "";
}
function mins(sec) { return sec < 60 ? Math.max(1, Math.round(sec)) + "초" : Math.round(sec / 60) + "분"; }
function bgTasksHtml(s) {
  const ts = s.bg_tasks || [];
  if (!ts.length) return "";
  const run = ts.filter(x => x.status === "running").length;
  return `<div class="bgt"><div class="bgt-head">⚙ 백그라운드 작업 ${run ? `<b>진행 ${run}</b>` : ""}${ts.length - run ? ` <span class="muted">끝남 ${ts.length - run}</span>` : ""}
      <span class="muted bgt-hint">누르면 무엇을 하는지 · 끝난 것은 30분 뒤 사라짐</span></div>` +
    ts.map(x => {
      const [cls, label] = x.status === "running" ? ["working", "진행 중"] : BGT_STATUS[x.status] || ["done", x.status];
      return `<button class="bgt-row ${cls}" data-action="bg-task" data-sid="${esc(s.session_id)}" data-tid="${esc(x.id)}" title="눌러서 무엇을 하는지 보기">
        <span class="dot ${cls}"></span><span class="bgt-kind">${BGT_ICON[x.kind] || "⚙"}</span><span class="bgt-desc">${esc(x.desc)}</span>
        <span class="bgt-time muted">${esc(label)} · ${esc(bgTaskTime(x))}</span></button>`;
    }).join("") + "</div>";
}
const BGT_EMPTY = '<div class="bgt"><div class="muted xm-note">백그라운드 작업이 끝났습니다</div></div>';
// 칸 머리 줄의 칩 — 대화를 가리지 않게 목록은 눌렀을 때만 머리 아래로 칸 너비만큼 펼친다
function bgtChip(s) {
  const ts = s.bg_tasks || [];
  if (!ts.length) return "";
  const run = ts.filter(x => x.status === "running").length;
  return `<button class="bgt-chip ${run ? "running" : ""}" data-action="bg-tasks" data-sid="${esc(s.session_id)}"
    title="이 세션이 띄운 백그라운드 작업(서브에이전트·명령) — 눌러서 목록">⚙ ${run ? `<b>${run}</b> 진행` : ""}${run && ts.length - run ? " · " : ""}${ts.length - run ? `${ts.length - run} 끝남` : ""}</button>`;
}
function toggleBgtMenu(chip) {
  const sid = chip.dataset.sid, m = $("#xMenu");
  if (!m.hidden && m.dataset.kind === "bgt" && m.dataset.sid === sid) return closeXMenu();
  const s = sessionById(sid), col = chip.closest(".col");
  if (!s || !col) return;
  openXMenu(null, bgTasksHtml(s) || BGT_EMPTY);
  m.dataset.kind = "bgt";
  m.dataset.sid = sid;
  m.classList.add("bgt-menu");
  // 칸 머리 바로 아래, 칸 너비만큼(양옆 6px 안쪽)
  const cr = col.getBoundingClientRect(), hr = col.querySelector(".col-head").getBoundingClientRect();
  m.style.width = (cr.width - 12) + "px";
  m.style.left = (cr.left + 6) + "px";
  m.style.top = (hr.bottom + 4) + "px";
}
// recap 은 Claude Code 가 자리를 비웠다 돌아올 때 만든 요약이라 그 뒤 대화가 이어졌으면 지난 내용이다 — 언제 요약인지 붙인다
function recapAge(s) {
  const lp = s.last_prompt && s.last_prompt.prompt_at;
  return s.recap_at && lp && lp > s.recap_at + 60 ? ` <span class="recap-age">${esc(rel(s.recap_at))} 요약</span>` : "";
}
const TASKV = { sid: null, tid: null, timer: 0 };
async function openBgTask(sid, tid) {
  Object.assign(TASKV, { sid, tid });
  $("#taskModal").hidden = false;
  $("#taskModal .task-body").innerHTML = '<div class="muted">불러오는 중…</div>';
  return drawBgTask(true);
}
async function drawBgTask(first) {
  clearTimeout(TASKV.timer);
  const box = $("#taskModal");
  if (box.hidden) return;
  const { sid, tid } = TASKV;
  let r;
  try { r = await api("GET", `/api/sessions/${sid}/tasks/${tid}`); }
  catch (err) { box.querySelector(".task-body").textContent = err.message; return; }
  if (box.hidden || TASKV.tid !== tid) return;
  const x = r.task, s = sessionById(sid);
  const [, label] = x.status === "running" ? ["", "진행 중"] : BGT_STATUS[x.status] || ["", x.status];
  box.querySelector(".task-title").textContent = (BGT_ICON[x.kind] || "⚙") + " " + x.desc;
  box.querySelector(".task-meta").textContent = [s ? sessionTitle(s) : "", x.kind === "agent" ? "서브에이전트" : "백그라운드 명령", label,
    bgTaskTime(x), x.summary || ""].filter(Boolean).join(" · ");
  const body = box.querySelector(".task-body");
  const atEnd = first || body.scrollHeight - body.scrollTop - body.clientHeight < 60;
  setHtml(body, r.text != null ? `<pre class="task-out">${esc(r.text) || "(출력 없음)"}</pre>`
    : r.items.length ? `<div class="steps">${stepsHtml(r.items)}</div>` : '<div class="muted">아직 기록이 없습니다</div>');
  if (atEnd) body.scrollTop = body.scrollHeight;   // 최신이 아래 — 읽는 중(위로 올림)이면 그대로
  if (x.status === "running") TASKV.timer = setTimeout(drawBgTask, 3000);
}
$("#taskModal").addEventListener("click", e => {
  if (e.target.id === "taskModal" || e.target.closest("[data-close]")) { $("#taskModal").hidden = true; return; }
  const st = e.target.closest('[data-action="toggle-step"]');
  if (st) {   // 창 안의 도구 호출 펼치기 — 보드가 아니라 창을 다시 그린다
    e.stopPropagation();
    S.stepOpen.has(st.dataset.key) ? S.stepOpen.delete(st.dataset.key) : S.stepOpen.add(st.dataset.key);
    drawBgTask();
  }
});

// ------------------------------------------------------------------ 변경 보기(⋮ 메뉴 · 입력창의 /diff)
// 그 세션 폴더(워크트리)의 변경을 보드에서 — 왼쪽 파일 목록, 오른쪽 파일마다 카드(줄 번호·줄 안에서 바뀐 부분 강조).
// 「커밋 안 한 변경」(새 파일 포함) / 「브랜치 전체」(main 과 갈라진 곳부터), 「한 줄」 / 「나란히」. J·K 파일 이동, V 보기 전환
S.diff = { sid: null, mode: "work", view: load("csl.diffView", "unified"), files: [], cur: 0 };
const DV_BIG = 800;   // 바뀐 줄이 이보다 많은 파일은 접어 둔다(펼치면 그린다)
const DV_LOCK = /(^|\/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|Cargo\.lock|Gemfile\.lock|composer\.lock)$/;
const DV_STATUS = { A: "새 파일", M: "수정", D: "삭제", R: "이름 바꿈" };
function parseDiff(text) {
  const files = [];
  let f = null, h = null, o = 0, n = 0;
  for (const line of (text || "").split("\n")) {
    if (line.startsWith("diff --git ")) {
      const m = /^diff --git a\/(.*) b\/(.*)$/.exec(line);
      f = { old: m ? m[1] : "", path: m ? m[2] : line.slice(11), status: "M", hunks: [], add: 0, del: 0, binary: false };
      files.push(f);
      h = null;
      continue;
    }
    if (!f) continue;
    if (!h) {   // 파일 머리
      if (line.startsWith("new file mode")) { f.status = "A"; continue; }
      if (line.startsWith("deleted file mode")) { f.status = "D"; continue; }
      if (line.startsWith("rename from ")) { f.old = line.slice(12); f.status = "R"; continue; }
      if (line.startsWith("rename to ")) { f.path = line.slice(10); continue; }
      if (line.startsWith("Binary files")) { f.binary = true; continue; }
      if (/^(--- |\+\+\+ |index |similarity |dissimilarity |old mode |new mode |copy )/.test(line)) continue;
    }
    const hm = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/.exec(line);
    if (hm) {
      // range: 이후 파일 기준 줄 범위 — 표 안은 번역하지 않으므로 언어와 상관없는 표기(L80–88)로
      h = { ctx: hm[5], range: hm[3] === "0" && hm[1] === "0" ? "" : `L${hm[3]}${hm[4] && hm[4] !== "1" ? "–" + (+hm[3] + +hm[4] - 1) : ""}`, lines: [] };
      f.hunks.push(h);
      o = +hm[1]; n = +hm[3];
      continue;
    }
    if (!h || !line) continue;
    const c = line[0], s = line.slice(1);
    if (c === "+") { h.lines.push({ t: "add", n: n++, s }); f.add++; }
    else if (c === "-") { h.lines.push({ t: "del", o: o++, s }); f.del++; }
    else if (c === " ") h.lines.push({ t: "ctx", o: o++, n: n++, s });
    else if (c === "\\") h.lines.push({ t: "meta", s: line });
  }
  return files;
}
// 지운 줄 묶음과 바로 뒤 더한 줄 묶음을 짝지어, 줄 안에서 바뀐 가운데 부분만 <mark> 로(앞뒤 같은 부분은 그대로)
function dvMark(a, b) {
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  let q = 0;
  while (q < a.length - p && q < b.length - p && a[a.length - 1 - q] === b[b.length - 1 - q]) q++;
  const same = p + q;
  if (same < 3 || (a.length - same > a.length * 0.85 && b.length - same > b.length * 0.85)) return [esc(a), esc(b)];   // 거의 다 다르면 강조하지 않는다
  const cut = (s) => esc(s.slice(0, p)) + (s.length - same ? `<mark>${esc(s.slice(p, s.length - q))}</mark>` : "") + esc(s.slice(s.length - q));
  return [cut(a), cut(b)];
}
// 덩어리의 줄을 [{kind, del, add}] 묶음으로: ctx 한 줄 · 지움/더함 짝(강조 포함) · meta
function dvBlocks(h) {
  const L = h.lines, out = [];
  for (let i = 0; i < L.length;) {
    if (L[i].t === "del" || L[i].t === "add") {
      let j = i;
      while (j < L.length && L[j].t === "del") j++;
      let k = j;
      while (k < L.length && L[k].t === "add") k++;
      const dels = L.slice(i, j).map(l => ({ ...l, h: esc(l.s) })), adds = L.slice(j, k).map(l => ({ ...l, h: esc(l.s) }));
      for (let x = 0; x < Math.min(dels.length, adds.length); x++) [dels[x].h, adds[x].h] = dvMark(dels[x].s, adds[x].s);
      out.push({ kind: "chg", dels, adds });
      i = k === i ? i + 1 : k;
      continue;
    }
    out.push({ kind: L[i].t, line: { ...L[i], h: esc(L[i].s) } });
    i++;
  }
  return out;
}
const dvCode = (cls, sign, html) => `<td class="dv-code ${cls}"><span class="dv-sg">${sign}</span>${html || " "}</td>`;
const dvLn = (v, cls = "") => `<td class="dv-ln ${cls}">${v ?? ""}</td>`;
function dvHunkRows(h, split) {
  const head = `<tr class="dv-hunk"><td colspan="${split ? 4 : 3}">${h.range ? `<span class="dv-range">${h.range}</span>` : ""}${esc(h.ctx || "")}</td></tr>`;
  const rows = dvBlocks(h).map(b => {
    if (b.kind === "meta") return `<tr class="dv-meta"><td colspan="${split ? 4 : 3}">${b.line.h}</td></tr>`;
    if (b.kind === "ctx") {
      const l = b.line;
      return split ? `<tr>${dvLn(l.o)}${dvCode("", " ", l.h)}${dvLn(l.n)}${dvCode("", " ", l.h)}</tr>`
        : `<tr>${dvLn(l.o)}${dvLn(l.n)}${dvCode("", " ", l.h)}</tr>`;
    }
    if (!split) return b.dels.map(l => `<tr class="del">${dvLn(l.o, "del")}${dvLn("", "del")}${dvCode("del", "−", l.h)}</tr>`).join("") +
      b.adds.map(l => `<tr class="add">${dvLn("", "add")}${dvLn(l.n, "add")}${dvCode("add", "+", l.h)}</tr>`).join("");
    let r = "";
    for (let x = 0; x < Math.max(b.dels.length, b.adds.length); x++) {
      const d = b.dels[x], a = b.adds[x];
      r += `<tr>${d ? dvLn(d.o, "del") + dvCode("del", "−", d.h) : dvLn("", "none") + `<td class="dv-code none"></td>`}` +
        `${a ? dvLn(a.n, "add") + dvCode("add", "+", a.h) : dvLn("", "none") + `<td class="dv-code none"></td>`}</tr>`;
    }
    return r;
  }).join("");
  return head + rows;
}
// 이미지 파일은 바뀌기 전·후를 그림으로(새 파일은 후, 지운 파일은 전만). 누르면 크게(팝업)
const DV_IMG = /\.(png|jpe?g|gif|webp)$/i;
function dvImageBody(f) {
  const url = (side, p) => `/api/sessions/${encodeURIComponent(S.diff.sid)}/diff-image?side=${side}` +
    `&mode=${encodeURIComponent((S.diff.data && S.diff.data.mode) || "work")}&path=${encodeURIComponent(p)}`;
  const fig = (label, u) => `<figure class="dv-img"><figcaption>${label}</figcaption>` +
    `<a class="zoom-img" href="${u}" target="_blank" rel="noopener" title="눌러서 크게 보기"><img src="${u}" alt="" loading="lazy"></a></figure>`;
  return `<div class="dv-imgs">${f.status !== "A" ? fig(f.status === "D" ? "삭제 전" : "이전", url("old", f.old || f.path)) : ""}` +
    `${f.status !== "D" ? fig(f.status === "A" ? "새 파일" : "이후", url("new", f.path)) : ""}</div>`;
}
function dvFileBody(f) {
  if (f.binary && DV_IMG.test(f.path)) return dvImageBody(f);
  if (f.binary) return '<div class="dv-note">바이너리 파일 — 내용은 보이지 않습니다</div>';
  if (!f.hunks.length) return `<div class="dv-note">${f.status === "R" ? "내용은 그대로이고 이름만 바뀌었습니다" : "바뀐 내용이 없습니다(권한·모드 변경)"}</div>`;
  if (f.hunks.every(h => !h.lines.length)) return `<div class="dv-note">${esc(f.hunks[0].ctx || "내용 없음")}</div>`;
  const split = S.diff.view === "split" && f.status !== "A" && f.status !== "D";   // 새 파일·삭제 파일은 한쪽뿐이라 나란히 보기에서도 한 줄로
  return `<table class="dv-tbl ${split ? "split" : "unified"}">${split ? '<colgroup><col class="c-ln"><col><col class="c-ln"><col></colgroup>'
    : '<colgroup><col class="c-ln"><col class="c-ln"><col></colgroup>'}${f.hunks.map(h => dvHunkRows(h, split)).join("")}</table>`;
}
function dvPath(p) {
  const i = p.lastIndexOf("/");
  return i < 0 ? `<b>${esc(p)}</b>` : `<span class="dv-dir">${esc(p.slice(0, i + 1))}</span><b>${esc(p.slice(i + 1))}</b>`;
}
function dvCounts(f) {
  return `<span class="dv-cnt">${f.add ? `<span class="a">+${f.add}</span>` : ""}${f.del ? `<span class="d">−${f.del}</span>` : ""}</span>`;
}
function dvFileCard(f, i) {
  const full = S.diff.dir + "/" + f.path;
  const name = f.status === "R" && f.old !== f.path ? `${dvPath(f.old)} <span class="muted">→</span> ${dvPath(f.path)}` : dvPath(f.path);
  return `<section class="dv-file ${f.collapsed ? "collapsed" : ""}" data-dfi="${i}">
    <header class="dv-fhead" data-dtoggle="${i}"><span class="dv-chev">▾</span><span class="dv-st ${f.status}" title="${DV_STATUS[f.status] || ""}">${f.status}</span>
      <span class="dv-fname">${name}</span>${dvCounts(f)}<span class="spacer"></span>
      ${f.status !== "D" ? `<a class="linkish dv-open" href="#" data-action="open-file" data-path="${esc(full)}" title="기본 앱으로 열기 (⌥ 클릭: Finder)">열기</a>` : ""}</header>
    <div class="dv-fbody">${f.collapsed ? "" : dvFileBody(f)}</div></section>`;
}
function dvSide(d) {
  const files = S.diff.files;
  const commits = d.commits && d.commits.length ? `<div class="dv-sh">커밋 ${d.commits.length}${d.commits.length >= 60 ? "+" : ""}</div>` +
    d.commits.map(c => `<div class="dv-commit" title="${esc(c.subject)}"><code>${esc(c.sha)}</code> ${esc(c.subject)}</div>`).join("") : "";
  return commits + `<div class="dv-sh">파일 ${files.length}</div>` + files.map((f, i) => `<button class="dv-frow ${i === S.diff.cur ? "on" : ""}" data-dfile="${i}" title="${esc(f.path)}">
      <span class="dv-st ${f.status}">${f.status}</span><span class="dv-fname"><b>${esc(base(f.path))}</b>${f.path.includes("/") ? `<span class="dv-dir">${esc(f.path.slice(0, f.path.lastIndexOf("/")))}</span>` : ""}</span>${dvCounts(f)}</button>`).join("");
}
function drawDiff(d) {
  const box = $("#diffModal"), body = box.querySelector(".dv-body");
  const s = sessionById(S.diff.sid);
  box.querySelector(".diff-title").textContent = s ? sessionTitle(s) : base(d.dir);
  box.querySelectorAll("[data-dmode]").forEach(b => b.classList.toggle("on", b.dataset.dmode === S.diff.mode));
  box.querySelectorAll("[data-dview]").forEach(b => b.classList.toggle("on", b.dataset.dview === S.diff.view));
  const files = S.diff.files;
  const add = files.reduce((n, f) => n + f.add, 0), del = files.reduce((n, f) => n + f.del, 0);
  const where = [base(d.dir), d.branch].filter(Boolean).join(" · ");
  box.querySelector(".dv-sub").innerHTML = `${esc(where)}${d.mode === "branch" && d.base ? ` <span class="dv-vs">${esc(d.base)} 과 비교</span>` : ""}` +
    (files.length ? ` · 파일 ${files.length} · <span class="dv-a">+${add}</span> <span class="dv-d">−${del}</span>` : "") +
    (d.head ? ` · <span title="지금 HEAD">HEAD <code>${esc(clamp(d.head, 70))}</code></span>` : "") +
    (d.note ? `<div class="dv-notice">${esc(d.note)}</div>` : "") +
    (d.cut ? '<div class="dv-notice">너무 길어 앞부분만 보였습니다</div>' : "") +
    (d.untracked_skipped ? `<div class="dv-notice">새 파일이 많아 ${d.untracked_skipped}개는 빼고 보였습니다</div>` : "");
  if (!files.length) {
    body.innerHTML = `<div class="dv-empty">${d.mode === "branch" ? "이 브랜치에 바뀐 것이 없습니다" : "커밋 안 한 변경이 없습니다"}` +
      (d.mode === "work" ? ' — <button class="linkish" data-dmode="branch">브랜치 전체 보기</button>' : "") + "</div>";
    return;
  }
  body.innerHTML = `<aside class="dv-side">${dvSide(d)}</aside><div class="dv-main">${files.map(dvFileCard).join("")}</div>`;
  body.querySelector(".dv-main").addEventListener("scroll", dvTrack, { passive: true });
}
// 오른쪽을 스크롤하면 왼쪽 목록에서 지금 보이는 파일을 표시
function dvTrack() {
  if (S.diff._raf) return;
  S.diff._raf = requestAnimationFrame(() => {
    S.diff._raf = 0;
    const main = $("#diffModal .dv-main");
    if (!main) return;
    const top = main.getBoundingClientRect().top + 8;
    let cur = 0;
    main.querySelectorAll(".dv-file").forEach((el, i) => { if (el.getBoundingClientRect().top <= top) cur = i; });
    if (cur !== S.diff.cur) dvSetCur(cur, false);
  });
}
function dvSetCur(i, scroll) {
  const files = S.diff.files;
  if (!files.length) return;
  S.diff.cur = Math.max(0, Math.min(files.length - 1, i));
  const box = $("#diffModal");
  box.querySelectorAll(".dv-frow").forEach(b => b.classList.toggle("on", +b.dataset.dfile === S.diff.cur));
  const row = box.querySelector(`.dv-frow[data-dfile="${S.diff.cur}"]`);
  if (row) row.scrollIntoView({ block: "nearest" });
  if (scroll) {
    const card = box.querySelector(`.dv-file[data-dfi="${S.diff.cur}"]`);
    if (card) card.scrollIntoView({ block: "start" });
  }
}
function dvToggle(i, open) {
  const f = S.diff.files[i], card = $(`#diffModal .dv-file[data-dfi="${i}"]`);
  if (!f || !card) return;
  f.collapsed = open === undefined ? !f.collapsed : !open;
  card.classList.toggle("collapsed", f.collapsed);
  const bodyEl = card.querySelector(".dv-fbody");
  if (!f.collapsed && !bodyEl.innerHTML) bodyEl.innerHTML = dvFileBody(f);   // 접혀 있던 큰 파일은 펼칠 때 그린다
}
async function openDiff(sid, mode) {
  const box = $("#diffModal");
  if (sid !== S.diff.sid) S.diff.mode = "work";
  S.diff.sid = sid;
  if (mode) S.diff.mode = mode;
  box.hidden = false;
  const body = box.querySelector(".dv-body");
  body.innerHTML = '<div class="dv-empty muted">불러오는 중…</div>';
  let d;
  try { d = await api("GET", `/api/sessions/${sid}/diff?mode=${S.diff.mode}`); } catch (err) { body.innerHTML = `<div class="dv-empty">${esc(err.message)}</div>`; return; }
  if (S.diff.sid !== sid || box.hidden) return;
  S.diff.data = d;
  S.diff.dir = d.dir;
  S.diff.cur = 0;
  S.diff.files = parseDiff(d.diff).map(f => ({ ...f, collapsed: (f.binary && !DV_IMG.test(f.path)) || DV_LOCK.test(f.path) || f.add + f.del > DV_BIG }));
  drawDiff(d);
}
function dvRedraw() {   // 보기만 바꿀 때 — 다시 읽지 않고, 스크롤 자리(지금 파일)는 지킨다
  const cur = S.diff.cur;
  drawDiff(S.diff.data);
  dvSetCur(cur, true);
}
$("#diffModal").addEventListener("click", e => {
  const box = $("#diffModal");
  if (e.target.id === "diffModal" || e.target.closest("[data-close]")) { box.hidden = true; return; }
  const t = e.target;
  const md = t.closest("[data-dmode]");
  if (md) { if (md.dataset.dmode !== S.diff.mode || !S.diff.data) openDiff(S.diff.sid, md.dataset.dmode); return; }
  const vw = t.closest("[data-dview]");
  if (vw) { S.diff.view = vw.dataset.dview; save("csl.diffView", S.diff.view); if (S.diff.data) dvRedraw(); return; }
  if (t.closest("[data-dreload]")) { openDiff(S.diff.sid, S.diff.mode); return; }
  const fr = t.closest("[data-dfile]");
  if (fr) { dvToggle(+fr.dataset.dfile, true); dvSetCur(+fr.dataset.dfile, true); return; }
  const tg = t.closest("[data-dtoggle]");
  if (tg && !t.closest("a, button")) dvToggle(+tg.dataset.dtoggle);
});
// 창이 떠 있는 동안: J·K 다음·이전 파일, V 보기 전환, X 지금 파일 접기·펴기(한글 자판도 같은 자리 키로). 뒤의 보드 단축키로 넘기지 않는다
document.addEventListener("keydown", e => {
  if ($("#diffModal").hidden || e.metaKey || e.ctrlKey || e.altKey || isTyping()) return;
  const act = { KeyJ: () => dvSetCur(S.diff.cur + 1, true), KeyK: () => dvSetCur(S.diff.cur - 1, true),
    KeyV: () => { S.diff.view = S.diff.view === "split" ? "unified" : "split"; save("csl.diffView", S.diff.view); if (S.diff.data) dvRedraw(); },
    KeyX: () => dvToggle(S.diff.cur) }[e.code];
  if (!act) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  act();
}, true);

// 펼친 메뉴를 기준 요소 바로 아래에 둔다(왼쪽 또는 오른쪽 끝 맞춤, 화면 밖으로 나가지 않게). 메뉴는 이미 보이는 상태여야 폭을 잰다
function placeMenu(menu, anchor, align = "left", gap = 6) {
  const r = anchor.getBoundingClientRect();
  // 아래 자리가 모자라면(입력창 옆 버튼 등) 위로 펼친다
  const below = r.bottom + gap + menu.offsetHeight <= innerHeight - 8;
  menu.style.top = (below ? r.bottom + gap : Math.max(8, r.top - gap - menu.offsetHeight)) + "px";
  const x = align === "right" ? r.right - menu.offsetWidth : r.left;
  menu.style.left = Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8)) + "px";
}
// 내용이 바뀐 경우에만 갈아 끼운다(깜빡임·선택 영역 유지). 바꿨으면 true
function setHtml(el, html) {
  if (el._html === html) return false;
  el._html = html;
  el.innerHTML = html;
  return true;
}
// 내용을 갈아 끼운 직후에는 배치가 몇 프레임에 걸쳐 끝날 수 있어, 높이가 안정될 때까지 맨 아래로 맞춘다.
// 그사이 사용자가 위로 스크롤하면(_pinned 해제) 멈춘다
function stickBottom(el, frames = 8) {
  let last = -1;
  const step = () => {
    if (!el._pinned || el.scrollHeight === last || frames-- <= 0) return;
    last = el.scrollHeight;
    el.scrollTop = el.scrollHeight;
    requestAnimationFrame(step);
  };
  step();
}

// ------------------------------------------------------------------ 리모트 컨트롤
// 켜져 있으면 claude.ai/code 의 그 세션으로 가는 배지, 보드가 켜 달라고 보낸 직후에는 「연결 중」
// 컨텍스트 사용량. 상태줄에서 받은 값이면 %·막대, 기록 파일로만 알면 토큰 수만
function fmtTokens(n) {
  return n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M" : n >= 1000 ? Math.round(n / 1000) + "k" : String(n || 0);
}
function ctxChip(s, d) {
  if (s.compact_at) {
    return `<button class="ctx-chip compacting" data-action="ctx-menu" data-sid="${esc(s.session_id)}" title="컨텍스트를 요약하는 중">
      <span class="ctx-ring spin"></span><span class="elapsed" data-since="${s.compact_at}">${dur(Date.now() / 1000 - s.compact_at)}</span></button>`;
  }
  const c = s.ctx;
  if (!c || !c.used || c.pct == null || d === "ended") return "";
  const pct = Math.min(100, Math.round(c.pct));
  const lv = pct >= 85 ? "hot" : pct >= 70 ? "warm" : "";
  const tip = `컨텍스트 ${pct}% 사용 · ${fmtTokens(c.used)} / ${fmtTokens(c.size)} 토큰` +
    (c.estimated ? "\n창 크기는 추정(같은 모델이 200k 를 넘겨 쓴 적이 있으면 1M, 아니면 200k)" : "");
  // 도넛: 채워진 각도 = 사용 %
  return `<button class="ctx-chip ${lv}" data-action="ctx-menu" data-sid="${esc(s.session_id)}" title="${esc(tip + "\n누르면 컴팩트(/compact)")}">
    <span class="ctx-ring" style="--p:${pct}"></span>${pct}%</button>`;
}
// 머리의 「📡 원격」: 리모트 컨트롤 켜기·끄기 버튼(켜졌으면 초록, 꺼졌으면 흐리게). claude.ai/code 에서 열기는 ⋮ 메뉴.
// 보드가 그 터미널을 조작할 수 있는 세션만(켜고 끄려면 /remote-control 을 쳐야 한다)
function remoteBadge(s, d) {
  if (d === "ended" || s.running === false || !["web", "tmux", "orca"].includes(s.term_name)) return "";
  const on = !!(s.remote && /^session_[A-Za-z0-9]+$/.test(s.remote));
  if (!on && s.rc_requested_at && Date.now() / 1000 - s.rc_requested_at < 180) {
    return '<span class="rc-badge pending" title="리모트 컨트롤을 켜는 중(다음 응답 때 연결이 확인됩니다)" data-icon="📡">📡 연결 중</span>';
  }
  return `<button class="rc-badge ${on ? "on" : "off"}" data-action="rc-toggle" data-sid="${esc(s.session_id)}" data-on="${on ? 1 : ""}" data-icon="📡"
    title="${on ? "원격(리모트 컨트롤) 켜짐 — 누르면 끄기 · claude.ai/code 에서 열기는 ⋮ 메뉴" : "원격(리모트 컨트롤) 꺼짐 — 누르면 켜기 ( /remote-control )"}">📡 원격</button>`;
}
async function toggleRemote(sid, on) {
  toast(on ? "원격을 끄는 중…" : "원격을 켜는 중…");
  try { toast((await api("POST", `/api/sessions/${sid}/remote`, on ? { off: true } : {})).message); }
  catch (err) { toast(err.message); }
  return refresh(sid);
}
// 이 세션이 열린 터미널 탭으로(단축키 T). 머리의 「↗ 터미널」 버튼은 자리만 차지해 없앴다
let focusing = false;
// 어느 터미널에서도 돌고 있지 않은 세션(종료·탭을 닫음 · 서버가 resume 으로 알려 줌): 새 탭에서 이어 열지 물어본다.
// el(입력칸)을 넘기면 열린 뒤 쓰던 글·이미지를 이어서 보낸다
function openResume(sid, el) {
  const s = sessionById(sid), box = $("#resumeModal");
  if (!s || !box) return;
  const withText = !!(el && (composerText(el).trim() || (S.compose.get(sid) || []).length));
  box._sid = sid;
  box._el = el || null;
  box.querySelector(".resume-name").textContent = sessionTitle(s) || sid.slice(0, 8);
  box.querySelector(".resume-sub").textContent = withText
    ? "새 터미널 탭에서 claude --resume 으로 이어 열고, 쓰던 글을 보냅니다."
    : "새 터미널 탭에서 claude --resume 으로 이어 엽니다.";
  const go = box.querySelector("[data-resume-go]");
  go.textContent = withText ? "새 탭에서 열고 보내기" : "새 탭에서 열기";
  go.disabled = false;
  box.hidden = false;
  go.focus();
}
async function runResume() {
  const box = $("#resumeModal"), sid = box._sid, el = box._el;
  const go = box.querySelector("[data-resume-go]");
  if (!sid || go.disabled) return;
  go.disabled = true;
  box.hidden = true;
  toast("새 터미널 탭에서 여는 중…");
  try {
    const r = await api("POST", `/api/sessions/${sid}/resume`, {});
    toast(r.message);
    await refresh(sid);
    const cur = el && (el.isConnected ? el : document.querySelector(`.composer[data-sid="${CSS.escape(sid)}"]`));
    if (r.ready && cur) sendCompose(cur);
  } catch (err) {
    toast(err.message);
  }
}
$("#resumeModal").addEventListener("click", e => {
  if (e.target.id === "resumeModal" || e.target.closest("[data-close]")) { $("#resumeModal").hidden = true; return; }
  if (e.target.closest("[data-resume-go]")) runResume();
});
async function focusTerminal(sid) {
  const s = sid && sessionById(sid);
  if (!s) return toast("먼저 칸을 고르세요(⌘1~9 또는 ← →)");
  if (s.board === "term") { await focusCol(sid); return focusTermSoon(sid); }
  if (s.board) return;
  if (s.term_web && s.state !== "ended") return openTermIn(sid);   // 보드 터미널 — 바깥 앱이 아니라 칸 안에서
  if (focusing) return;
  focusing = true;
  try {
    const r = await api("POST", `/api/sessions/${sid}/focus`, {});
    if (r.resume) openResume(sid);
    // tmux 세션이 어느 터미널 창에도 붙어 있지 않으면(보여 줄 화면이 없다) 칸 안에서 그 창을 연다
    else if (!r.ok && s.term_attach === "tmux") { openTermIn(sid); toast("tmux 세션이 어느 터미널에도 붙어 있지 않아 칸 안에서 엽니다 — 닫아도 세션은 그대로"); }
    else toast(r.message);
  } catch (err) {
    toast("이동하지 못했습니다");
  } finally {
    focusing = false;
  }
}
// 칸 머리 ⋮ 메뉴: 보관 · 원격 · 고정
function openColMenu(btn) {
  const menu = $("#colMenu");
  const sid = btn.dataset.sid;
  if (!menu.hidden && menu.dataset.sid === sid) { menu.hidden = true; return; }
  const s = sessionById(sid);
  if (!s) return;
  const d = displayState(s);
  const rcOn = s.remote && /^session_[A-Za-z0-9]+$/.test(s.remote);
  const rcPending = !rcOn && s.rc_requested_at && Date.now() / 1000 - s.rc_requested_at < 180;
  const remote = d === "ended" ? `<button class="cm-item" disabled>📡 원격 <span class="muted">종료된 세션</span></button>`
    : rcOn ? `<a class="cm-item" href="https://claude.ai/code/${s.remote}" target="_blank" rel="noopener noreferrer" data-cm-close>📡 원격으로 열기 <span class="muted">claude.ai/code</span></a>
      <button class="cm-item" data-cm="remote-off">📡 원격 끄기 <span class="muted">/remote-control → Disconnect</span></button>`
    : rcPending ? `<button class="cm-item" disabled>📡 원격 연결 중…</button>`
    : `<button class="cm-item" data-cm="remote">📡 원격 켜기 <span class="muted">/remote-control</span></button>`;
  menu.dataset.sid = sid;
  if (s.board) {
    menu.innerHTML = boardMenuHtml(s);
    menu.hidden = false;
    placeMenu(menu, btn, "right", 4);
    return;
  }
  menu.innerHTML = `
    ${s.brain ? "" : `<button class="cm-item" data-cm="archive">🗄 ${s.archived ? "보관 해제" : "보관"} <span class="muted">${s.archived ? "" : "보드에서 숨기기"}</span></button>`}
    ${s.term_web && s.state !== "ended" && s.running !== false ? `<button class="cm-item" data-cm="restart">↻ 다시 열기 <span class="muted">끝내고 같은 대화로 새로</span></button>` : ""}
    ${!s.term_web && !s.brain ? `<button class="cm-item" data-cm="import">⇄ 보드 터미널로 가져오기 <span class="muted">${s.state === "ended" ? "이어 열기" : "다른 터미널에서 옮기기"}</span></button>` : ""}
    ${s.state !== "ended" ? `<button class="cm-item danger" data-cm="quit">⏻ 세션 종료 <span class="muted">Claude Code 를 닫고(/exit) 터미널 탭도 닫기</span></button>` : ""}
    ${remote}
    ${s.brain ? "" /* 브레인은 패널에 따로 떠서 고정·목표·변경·너비가 의미 없다 — 다시 열기·종료·원격만 */ : `<button class="cm-item" data-cm="pin">📌 ${pinOf(s) ? "고정 풀기" : "맨 앞에 고정"} <span class="muted">P</span></button>
    ${d !== "ended" ? `<button class="cm-item" data-cm="goal">🎯 ${s.goal && !s.goal.met ? "목표 바꾸기·풀기" : "목표 걸기"} <span class="muted">/goal</span></button>` : ""}
    <button class="cm-item" data-cm="diff">🧾 변경 보기 <span class="muted">커밋 안 한 변경</span></button>
    ${colWidthOf(sid) ? `<button class="cm-item" data-cm="width-reset">↔ 기본 너비로 <span class="muted">${colWidthOf(sid)}px</span></button>` : ""}`}`;
  menu.hidden = false;
  placeMenu(menu, btn, "right", 4);
}
$("#colMenu").addEventListener("click", async e => {
  const menu = $("#colMenu");
  if (e.target.closest("[data-cm-close]")) { menu.hidden = true; return; }
  const item = e.target.closest("[data-cm]");
  if (!item) return;
  const sid = menu.dataset.sid;
  const s = sessionById(sid);
  if (s && s.board && await boardMenuAction(item, menu, s)) return;
  if (s && item.dataset.cm === "archive" && !s.archived) {
    // 보관은 칸이 보드에서 사라지므로 한 번 더 묻는다(메뉴 안에서, 브라우저 확인 창 없이)
    item.outerHTML = `<div class="cm-confirm">
      <div>이 세션을 보관할까요?<br><span class="muted">보드에서 숨겨집니다 · 오른쪽 위 ⋮ → 「종료·보관 세션도 보기」로 다시 볼 수 있습니다</span></div>
      <div class="cm-confirm-btns"><button class="btn" data-cm="cancel">취소</button><button class="btn danger" data-cm="archive-yes">보관</button></div>
    </div>`;
    menu.querySelector('[data-cm="archive-yes"]').focus();
    return;
  }
  if (s && item.dataset.cm === "restart") {
    // 보드 터미널 세션을 끝내고 같은 대화로 새로 연다 — 환경이 꼬인 세션(다른 세션의 변수를 물려받은 것 등)을 깨끗하게
    const busy = ["working", "waiting"].includes(displayState(s));
    item.outerHTML = `<div class="cm-confirm">
      <div>${busy ? "<b>작업 중이거나 확인을 기다리는 중입니다.</b> 끝난 뒤 다시 열어 주세요." : "이 세션을 다시 열까요?"}<br><span class="muted">Claude Code 를 끝내고(워크트리는 남김) 보드 터미널에서 같은 대화로 새로 엽니다. 모델·effort·권한 모드는 그대로 넘깁니다(권한 확인 건너뛰기 모드는 빼고). 그 세션에서 돌던 백그라운드 작업은 끊깁니다</span></div>
      <div class="cm-confirm-btns"><button class="btn" data-cm="cancel">취소</button>${busy ? "" : `<button class="btn primary" data-cm="restart-yes">다시 열기</button>`}</div>
    </div>`;
    (menu.querySelector('[data-cm="restart-yes"]') || menu.querySelector('[data-cm="cancel"]')).focus();
    return;
  }
  if (s && item.dataset.cm === "import") {
    // 다른 터미널의 세션을 보드 터미널로: 같은 세션 ID 로 이어 연다(Orca·tmux 는 보드가 끝내고, 그 밖의 앱은 직접 끝낸 뒤)
    const busy = ["working", "waiting"].includes(displayState(s));
    const live = s.state !== "ended" && s.running !== false;   // 꺼진 채 「대기」로 남은 세션은 바로 이어 연다
    const how = !live ? "보드 터미널에서 이 세션을 이어 엽니다(claude --resume)."
      : s.bg ? "백그라운드 세션이라 끄지 않고 보드 터미널에서 붙습니다(claude attach)."
      : ["orca", "tmux"].includes(s.term_name) ? "지금 터미널에서 Claude Code 를 끝내고(워크트리는 남김) 보드 터미널에서 같은 대화로 다시 엽니다. 모델·effort·권한 모드도 그대로 넘깁니다(권한 확인 건너뛰기 모드는 빼고)."
      : "보드가 이 터미널을 조작할 수 없습니다 — 그 터미널에서 /exit 로 끝낸 뒤 누르면 보드 터미널에서 이어 엽니다.";
    item.outerHTML = `<div class="cm-confirm">
      <div>${busy && live && !s.bg ? "<b>작업 중입니다.</b> 끝난 뒤 가져와 주세요." : "보드 터미널로 가져올까요?"}<br><span class="muted">${how}</span></div>
      ${live && !s.bg && ["orca", "tmux"].includes(s.term_name) ? `<label class="cm-wt-opt"><input type="checkbox" name="cmStopTasks"> <span>그 세션에서 도는 백그라운드 작업(개발 서버 등)도 끄고 옮기기 <span class="muted">끄면 종료할 때 묻는 창에서 취소합니다</span></span></label>` : ""}
      <div class="cm-confirm-btns"><button class="btn" data-cm="cancel">취소</button>${busy && live && !s.bg ? "" : `<button class="btn primary" data-cm="import-yes">가져오기</button>`}</div>
    </div>`;
    const b = menu.querySelector('[data-cm="import-yes"]') || menu.querySelector('[data-cm="cancel"]');
    b.focus();
    return;
  }
  if (s && item.dataset.cm === "quit") {
    const busy = ["working", "waiting"].includes(displayState(s));
    // claude --worktree 로 만든 워크트리(.claude/worktrees/…)면 종료하면서 남길지 정리할지 함께 묻는다 — Claude Code 가 종료 때
    // 묻는 「Keep worktree / Remove worktree」에 서버가 고른 대로 답한다. 기본은 정리하기 — 다만 커밋 안 한 변경이 있으면
    // 그 변경까지 지워지니 남기기를 기본으로 둔다
    const wt = s.worktree && /\/\.claude\/worktrees\/[^/]+\/?$/.test(s.worktree) ? s.worktree : "";
    const dirty = s.dirty && s.dirty.files ? s.dirty.files : 0;
    const wtBox = wt ? `<div class="cm-wt">
        <div class="cm-wt-head">🌿 워크트리 <b>${esc(base(wt))}</b> <span class="muted">${dirty ? `커밋 안 한 변경 ${dirty}개` : "커밋 안 한 변경 없음"}</span></div>
        <label class="cm-wt-opt"><input type="radio" name="cmWt" value="keep"${dirty ? " checked" : ""}> <span class="cm-wt-name">남기기</span> <span class="muted">폴더와 브랜치를 그대로 — 나중에 이어서 작업</span></label>
        <label class="cm-wt-opt"><input type="radio" name="cmWt" value="remove"${dirty ? "" : " checked"}> <span class="cm-wt-name">정리하기</span> <span class="muted">폴더와 브랜치를 지웁니다${dirty ? " · <b>커밋 안 한 변경도 함께 지워집니다</b>" : ""}</span></label>
        ${dirty ? "" : `<div class="muted cm-wt-note">새 커밋도 없으면 Claude Code 가 묻지 않고 정리할 수 있습니다</div>`}
      </div>` : "";
    item.outerHTML = `<div class="cm-confirm">
      <div>${busy ? "<b>작업 중입니다.</b> 작업을 멈추고 세션을 종료할까요?" : "이 세션을 종료할까요?"}<br><span class="muted">터미널에서 /exit 를 친 것처럼 Claude Code 를 닫고 탭도 닫습니다. 대화는 남아 있어 나중에 claude --resume 으로 이어 갈 수 있습니다</span></div>
      ${wtBox}
      <div class="cm-confirm-btns"><button class="btn" data-cm="cancel">취소</button><button class="btn danger" data-cm="quit-yes" data-force="${busy ? 1 : ""}">${busy ? "멈추고 종료" : "종료"}</button></div>
    </div>`;
    menu.querySelector('[data-cm="quit-yes"]').focus();
    return;
  }
  menu.hidden = true;
  if (!s || item.dataset.cm === "cancel") return;
  if (item.dataset.cm === "restart-yes") {
    const job = { at: Date.now(), done: false };
    S.importing.set(sid, job);
    S.uiVer++;
    renderBoard();
    try { toast((await api("POST", `/api/sessions/${sid}/import`, { restart: true })).message.replace("보드 터미널로 가져왔습니다", "다시 열었습니다")); }
    catch (err) { S.importing.delete(sid); toast(err.message); }
    Object.assign(job, { done: true, at: Date.now() });
    S.uiVer++;
    return refresh(sid);
  }
  if (item.dataset.cm === "import-yes") {
    const job = { at: Date.now(), done: false };
    S.importing.set(sid, job);
    S.uiVer++;
    renderBoard();
    const stop = menu.querySelector('input[name="cmStopTasks"]');
    try { toast((await api("POST", `/api/sessions/${sid}/import`, { stop_tasks: !!(stop && stop.checked) })).message); }
    catch (err) { S.importing.delete(sid); toast(err.message); }
    Object.assign(job, { done: true, at: Date.now() });
    S.uiVer++;
    return refresh(sid);
  }
  if (item.dataset.cm === "quit-yes") {
    toast("종료하는 중…");
    const wtPick = menu.querySelector('input[name="cmWt"]:checked');
    try { toast((await api("POST", `/api/sessions/${sid}/quit`, { force: !!item.dataset.force, worktree: wtPick ? wtPick.value : "" })).message); }
    catch (err) { toast(err.message); }
    return refresh(sid);
  }
  if (item.dataset.cm === "pin") return togglePin(sid);
  if (item.dataset.cm === "goal") return openGoalMenu(sid, btnOfCol(sid, ".col-menu-btn"));
  if (item.dataset.cm === "diff") return openDiff(sid);
  if (item.dataset.cm === "width-reset") return setColWidth(sid, null);
  if (item.dataset.cm === "archive" || item.dataset.cm === "archive-yes") {
    await api("POST", `/api/sessions/${sid}/archive`, { archived: !s.archived });
    toast(s.archived ? "보관 해제했습니다" : "보관했습니다 (⋮ → 「종료·보관 세션도 보기」로 다시 보입니다)");
    return refresh(sid);
  }
  if (item.dataset.cm === "remote" || item.dataset.cm === "remote-off") return toggleRemote(sid, item.dataset.cm === "remote-off");
});
$("#board").addEventListener("scroll", () => {
  $("#colMenu").hidden = true; $("#ctxMenu").hidden = true;
  if ($("#xMenu").dataset.kind === "bgt") closeXMenu();   // 칸 너비에 맞춰 붙인 목록 — 칸이 움직이면 닫는다
}, { passive: true });

// ------------------------------------------------------------------ 컨텍스트 도넛 → 컴팩트(/compact [남길 말])
function openCtxMenu(btn) {
  const menu = $("#ctxMenu"), sid = btn.dataset.sid;
  if (!menu.hidden && menu.dataset.sid === sid) { menu.hidden = true; return; }
  const s = sessionById(sid);
  if (!s || !s.ctx || s.compact_at) return;
  const c = s.ctx, d = displayState(s);
  const blocked = d === "working" ? "작업 중에는 할 수 없습니다 — 끝난 뒤에 해 주세요"
    : d === "waiting" ? "확인 필요 상태입니다 — 터미널에서 먼저 답해 주세요" : d === "ended" ? "종료된 세션입니다" : "";
  menu.dataset.sid = sid;
  menu.innerHTML = `
    <div class="ctx-head"><span class="ctx-ring big" style="--p:${Math.min(100, Math.round(c.pct))}"></span>
      <div><b>컨텍스트 ${Math.round(c.pct)}%</b><div class="muted">${fmtTokens(c.used)} / ${fmtTokens(c.size)} 토큰${c.estimated ? " · 창 크기 추정" : ""}</div></div></div>
    <div class="ctx-sec">지금까지의 대화를 요약해 컨텍스트를 비웁니다(/compact).</div>
    <textarea class="ctx-note" rows="3" placeholder="요약할 때 남길 말(선택) — 예: 지금 고치는 버그와 남은 할 일 위주로" ${blocked ? "disabled" : ""}></textarea>
    <div class="ctx-foot"><span class="muted">${esc(blocked || "Enter 로 실행 · 남길 말은 한 줄로 보냅니다")}</span>
      <button class="btn primary" data-ctx-run ${blocked ? "disabled" : ""}>컴팩트</button></div>`;
  menu.hidden = false;
  placeMenu(menu, btn);
  const ta = menu.querySelector(".ctx-note");
  if (!blocked) ta.focus();
}
async function runCompact() {
  const menu = $("#ctxMenu"), sid = menu.dataset.sid;
  const note = menu.querySelector(".ctx-note").value.replace(/\s+/g, " ").trim();   // 슬래시 명령 인자는 한 줄
  const btn = menu.querySelector("[data-ctx-run]");
  if (btn.disabled) return;   // Enter 두 번 — 보내는 중(메뉴는 열 때마다 새로 그려 풀린다)
  btn.disabled = true;
  btn.textContent = "보내는 중…";
  try {
    const r = await api("POST", `/api/sessions/${sid}/send`, { text: "/compact" + (note ? " " + note : ""), images: [] });
    menu.hidden = true;
    toast("컴팩트를 시작했습니다 · " + r.message);
    setTimeout(refresh, 800);
  } catch (err) {
    toast(err.message);
    btn.disabled = false;
    btn.textContent = "컴팩트";
  }
}
// ------------------------------------------------------------------ 브레인(Argus Brain) 패널
// 저장소 brain/ 폴더에서 띄운 Claude Code 세션. 보드 칸 대신 왼쪽 패널에 같은 칸 모양으로 그려
// 대화·진행 과정·질문 카드·입력창을 그대로 쓴다. 「슬랙 인박스」 탭은 브레인이 남긴 카드(요약·배정 추천·프롬프트)
const BRAIN_KEY = "csl.brainOpen";
try { S.brainOpen = localStorage.getItem(BRAIN_KEY) === "1"; } catch (e) { S.brainOpen = false; }
S.brainTab = "chat";
S.inbox = { items: [], at: 0, sig: "" };
const BRAIN_QUICK = [
  ["전체 현황", "지금 세션들 현황을 짧게 정리해줘. 확인 필요·오류·작업 중을 먼저."],
  ["확인 필요만", "확인이 필요하거나 오류난 세션만 무엇 때문인지 알려줘."],
  ["인박스 정리", "인박스 정리해줘"],
  ["남은 할 일", "세션마다 완료 안 된 넥스트 액션을 정리해줘. 오래된 것·이미 끝난 듯한 것 먼저."],
  ["오늘 한 일", "오늘 세션들에서 끝낸 일을 프로젝트별로 짧게 정리해줘."],
];
function brainSession() {
  return S.sessions.find(s => s.brain && s.state !== "ended" && !s.archived) || null;
}
function setBrainOpen(open, instant) {
  S.brainOpen = open;
  try { localStorage.setItem(BRAIN_KEY, open ? "1" : "0"); } catch (e) { /* 저장소 없음 */ }
  // 열 때는 보이게 한 다음 칸을 넓히고, 닫을 때는 칸을 줄인 다음(애니메이션 끝) 숨긴다
  const layout = $("#layout"), panel = $("#brainPanel");
  clearTimeout(setBrainOpen.timer);
  if (instant) layout.classList.add("no-anim");
  if (open) {
    panel.hidden = false;
    void panel.offsetWidth;   // 보이게 한 상태를 먼저 그려야 넓어지는 게 보인다
    layout.classList.add("brain-open");
  } else {
    layout.classList.remove("brain-open");
    setBrainOpen.timer = setTimeout(() => { if (!S.brainOpen) panel.hidden = true; }, instant ? 0 : 260);
  }
  if (instant) requestAnimationFrame(() => requestAnimationFrame(() => layout.classList.remove("no-anim")));
  $("#brainBtn").classList.toggle("on", open);
  if (open && S.brainTab === "inbox") { openInboxView(); setTimeout(markInboxSeen, 0); }
  if (open) renderBrain();
}
function showBrainTab(tab) {
  S.brainTab = tab;
  document.querySelectorAll(".bp-tab").forEach(b => b.classList.toggle("on", b.dataset.bpTab === tab));
  $("#bpChat").hidden = tab !== "chat";
  $("#bpInbox").hidden = tab !== "inbox";
  if (tab === "inbox") { openInboxView(); loadInbox(true).then(markInboxSeen); }
}
// 인박스 배지는 「마지막으로 인박스를 본 뒤 새로 온 할 일 카드」만 센다(본 시각은 이 브라우저에 기억).
// 칸반·브레인 패널 인박스 탭을 열면 본 것으로 치고, 그때 새로 온 카드에는 NEW 표시가 붙는다
S.inboxSeenAt = load("csl.inboxSeenAt", null);
if (S.inboxSeenAt == null) { S.inboxSeenAt = Date.now() / 1000; save("csl.inboxSeenAt", S.inboxSeenAt); }   // 처음엔 지금 있는 카드를 본 것으로
S.inboxNewSince = S.inboxSeenAt;
function inboxUnseen() { return (S.inboxNewAt || []).filter(t => t > S.inboxSeenAt).length; }
function inboxVisible() { return S.view === "inbox" || (S.brainOpen && S.brainTab === "inbox"); }
function openInboxView() { S.inboxNewSince = S.inboxSeenAt; }   // 열 때: 지난번 본 시각 뒤에 온 카드를 NEW 로
function markInboxSeen() {
  if (!inboxVisible()) return;
  S.inboxSeenAt = Date.now() / 1000;
  save("csl.inboxSeenAt", S.inboxSeenAt);
  renderInboxBadges();
}
function renderInboxBadges() {
  const n = inboxUnseen(), todo = S.inboxNew || 0;
  const tip = `할 일 카드 ${todo}개` + (n ? ` · 새로 온 카드 ${n}개` : "");
  const set = (el, prop, v) => { if (el && el[prop] !== v) el[prop] = v; };   // 같으면 다시 쓰지 않는다(툴팁·번역 유지)
  const badge = $("#brainBadge");
  set(badge, "hidden", !n);
  set(badge, "textContent", String(n));
  set($("#brainBtn"), "title", "Argus Brain 패널 열기·닫기 — 세션 현황·인박스 ( B )\n" + tip);
  set($("#vtInbox"), "textContent", n ? String(n) : "");
  set($(".view-tab[data-view=inbox]"), "title", "인박스 칸반 — 할 일·진행 중·완료 ( I )\n" + tip);
  set($("#bpInboxCount"), "textContent", n ? `· 새 ${n}` : "");
  updateBadge();   // 탭 아이콘 숫자에도(인박스를 보면 줄어든다)
}
async function renderBrain() {
  renderInboxBadges();

  const auto = $("#bpAuto");
  if (document.activeElement !== auto) auto.value = String((S.settings || {}).brain_slack_every || 0);
  if (!S.brainOpen) return;
  const chat = $("#bpChat");
  const b = brainSession();
  if (!b) {
    if (S.brainLaunch) return pollBrainLaunch();   // 띄운 브레인이 아직 세션으로 잡히기 전 — 확인 창·뜨는 중
    pollBrainLaunch();   // 새로고침 뒤에도 뜨는 중이던 브레인을 이어 본다(2초에 한 번만 묻는다)
    if (!chat.querySelector(".bp-empty")) {
      chat.innerHTML = `<div class="bp-empty">
        <p><b>브레인이 꺼져 있습니다.</b><br>저장소의 <code>brain/</code> 폴더에서 Claude Code 를 띄워 세션 현황과 슬랙 인박스를 맡깁니다.
        보드 터미널에서 돌아서 이 패널 안에서 터미널을 열 수 있습니다(tmux 필요).</p>
        <select id="bpModel">${[["sonnet", "Sonnet (추천)"], ["opus", "Opus"], ["haiku", "Haiku"], ["", "기본(설정대로)"]]
          .map(([v, l]) => `<option value="${v}" ${v === ((S.settings || {}).brain_model ?? "sonnet") ? "selected" : ""}>${l}</option>`).join("")}</select>
        <select id="bpPerm" title="권한 모드 — 자동은 Claude 가 판단해 진행하고 위험해 보이는 것만 묻습니다">${[["auto", "⏵⏵ 자동(추천)"], ["manual", "⏸ 수동 — 명령마다 묻기"], ["acceptEdits", "⏵⏵ 편집 허용"], ["plan", "⏸ 계획"]]
          .map(([v, l]) => `<option value="${v}" ${v === ((S.settings || {}).brain_permission_mode || "auto") ? "selected" : ""}>${l}</option>`).join("")}</select>
        <button class="btn primary" data-action="brain-start">브레인 시작</button></div>`;
    }
  } else {
    // 뜨는 동안 창으로 보던 브레인 터미널: 세션으로 잡히면 창을 닫는다
    if (S.brainLaunchPopup && b.term_handle === S.brainLaunchPopup) {
      S.brainLaunchPopup = null;
      closeLaunchTerm();   // 패널은 대화 화면으로(터미널은 「⌨ 터미널」로)
    }
    let col = chat.querySelector(".col");
    if (!col || col.dataset.col !== b.session_id) {
      chat.innerHTML = colSkeleton(b.session_id) +
        `<div class="bp-quick">${BRAIN_QUICK.map(([l, p]) => p === "인박스 정리해줘"   // 인박스 정리는 대화가 아니라 정리 일꾼으로
          ? `<button class="chip" data-action="inbox-run" title="정리 일꾼이 따로 정리합니다 — 결과는 인박스 탭 맨 위">${esc(l)}</button>`
          : `<button class="chip" data-action="brain-quick" data-prompt="${esc(p)}">${esc(l)}</button>`).join("")}</div>`;
      col = chat.querySelector(".col");
      setupThread(col.querySelector(".col-thread"));
      restoreComposerDraft(col.querySelector(".composer"));
      chat.appendChild(chat.querySelector(".bp-quick"));
    }
    S.visibleCols.add(b.session_id);
    const c = S.boardCache.get(b.session_id);
    if (!c || c.sig !== sigOf(b)) {
      try {
        await loadDetail(b);
      } catch (e) { /* 다음 폴링에서 */ }
    }
    updateCol(col, b, 0, false);
    // 빠른 질문은 입력 영역 안, 입력칸 바로 위에
    const quick = chat.querySelector(".bp-quick"), cmp = col.querySelector(".composer");
    if (quick && cmp && quick.parentElement !== cmp) cmp.prepend(quick);
    decorateSessionRefs(col.querySelector(".col-thread"));
  }
  if (S.brainTab === "inbox") loadInbox(false);
}
// 브레인이 쓴 「#번호 세션 제목」을 그 세션으로 가는 링크로. #번호는 브레인이 답할 때의 칸 순서라 바뀔 수 있어서
// 제목으로 세션을 찾고(번호는 무시), 옆에는 지금 칸 순서의 단축키(⌘N)를 보여 준다. 대화를 다시 그릴 때마다 다시 단다
function decorateSessionRefs(root) {
  if (!root) return;
  const sess = S.sessions.filter(s => !s.brain && s.state !== "ended" && sessionTitle(s))
    .map(s => ({ s, t: sessionTitle(s).toLowerCase() })).sort((a, b) => b.t.length - a.t.length);
  const order = boardSessions();
  // 대화(다시 그린 HTML)·세션 제목·칸 순서가 그대로면 할 일이 없다 — 2초마다 대화 전체를 훑지 않게
  const key = [root._html, sess.map(x => x.s.session_id + "=" + x.t).join("|"), order.map(s => s.session_id).join(",")];
  if (root._decoKey && root._decoKey.every((v, i) => v === key[i])) return;
  root._decoKey = key;
  if (sess.length) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: n => n.parentElement.closest("a, code, pre, textarea, .sref") || !/#\d|「/.test(n.nodeValue)
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    const byTitle = new Map(sess.map(x => [x.t, x]));
    for (const n of nodes) {
      const v = n.nodeValue, re = /#(\d{1,2})\s*|「([^」]{1,80})」/g;
      let m, last = 0, frag = null;
      while ((m = re.exec(v))) {
        if (m[2] !== undefined) {   // 「세션 제목」 — 제목이 정확히 같은 세션만
          const hitT = byTitle.get(m[2].trim().toLowerCase());
          if (!hitT) continue;
          frag = frag || document.createDocumentFragment();
          frag.append(v.slice(last, m.index));
          const a = document.createElement("a");
          a.className = "sref";
          a.dataset.sid = hitT.s.session_id;
          a.textContent = m[0];
          frag.append(a);
          last = m.index + m[0].length;
          continue;
        }
        const rest = v.slice(m.index + m[0].length).toLowerCase();
        // 제목 바로 뒤가 글자로 이어지면(「api」 vs 「api-server」) 다른 세션 — 링크하지 않는다
        const hit = sess.find(x => rest.startsWith(x.t) && !/^[\w가-힣-]/.test(rest.slice(x.t.length)));
        if (!hit) continue;
        frag = frag || document.createDocumentFragment();
        frag.append(v.slice(last, m.index));
        const end = m.index + m[0].length + hit.t.length;
        const a = document.createElement("a");
        a.className = "sref";
        a.dataset.sid = hit.s.session_id;
        a.textContent = v.slice(m.index, end);
        frag.append(a);
        last = end;
        re.lastIndex = end;
      }
      if (frag) { frag.append(v.slice(last)); n.replaceWith(frag); }
    }
  }
  root.querySelectorAll(".sref").forEach(a => {
    const r = order.findIndex(s => s.session_id === a.dataset.sid) + 1;
    a.dataset.key = r && r <= 9 ? "⌘" + r : "";
    a.title = "이 세션 칸으로 이동" + (r && r <= 9 ? ` ( ⌘${r} )` : "");
  });
}
// force: 15초 안이어도 다시 받는다. rerender: 같은 내용이어도 브레인 패널 인박스를 다시 그린다(기본은 force 와 같음)
// ------------------------------------------------------------------ 인박스 정리 일꾼(csl/inboxrun.py)의 결과 줄
// 자동 정리·「지금 정리」는 브레인 대화가 아니라 따로 도는 일꾼이 한다. 회차마다 남긴 보고를 인박스 맨 위에 한 줄로 —
// 「요약 ▾」로 그 회차 보고 전문, 「기록」으로 최근 회차들. 끝났는데 급한 카드가 있으면 알린다
S.inboxRuns = null;
S.runOpen = null;   // 펼친 보고(회차 id)
S.runHist = false;  // 최근 회차 목록 펼침
function setInboxRuns(runs) {
  const before = S.inboxRuns;
  S.inboxRuns = runs || [];
  if (before) {   // 처음 읽을 때는 알리지 않는다
    for (const r of S.inboxRuns) {
      const was = before.find(x => x.id === r.id);
      if (was && was.status === "running" && r.status === "done" && r.new_cards) alertInbox(r);
    }
  }
  renderInboxRuns();
}
function alertInbox(r) {
  if (S.sound) chime();
  if (!S.notify || !("Notification" in window) || Notification.permission !== "granted") return;
  const n = new Notification("인박스 정리 · 새 카드 " + r.new_cards + (r.new_high ? " (급함 " + r.new_high + ")" : ""),
    { body: clamp((r.summary || "").replace(/[#*`]/g, ""), 180), tag: "inbox-run-" + r.id });
  n.onclick = () => { window.focus(); S.runOpen = r.id; setView("inbox"); n.close(); };
}
function renderInboxRuns() {
  document.querySelectorAll(".ib-run-box").forEach(el => setHtml(el, inboxRunHtml()));
}
function runLabel(r) {
  const when = hm(r.started_at) + " " + (r.trigger === "auto" ? "자동" : "직접");
  if (r.status === "running") return `⏳ ${when} 정리 중… <span class="muted">${mins(Date.now() / 1000 - r.started_at)}째</span>`;
  if (r.status === "error") return `⚠ ${when} 정리 실패 <span class="muted">${esc(clamp(r.error || "", 80))}</span>`;
  return `🗂 ${when} 정리 · 새 카드 <b>${r.new_cards || 0}</b>${r.new_high ? ` <span class="ib-run-high">급함 ${r.new_high}</span>` : ""}`;
}
// 자동 정리는 모아 둔 새 항목이 있을 때만 돈다 — 건너뛴 것도 보여야 멈춘 것처럼 보이지 않는다
function inboxIdleHtml(after) {
  return S.inboxIdle && S.inboxIdle > (after || 0)
    ? `<span class="ib-run-idle muted" title="자동 정리는 커넥터가 모아 둔 새 항목이 있을 때만 돕니다(사용량을 아끼려고)">· 자동 확인 ${hm(S.inboxIdle)} — 새 항목 없어 건너뜀</span>` : "";
}
function inboxRunHtml() {
  const runs = S.inboxRuns || [];
  if (!runs.length) return `<div class="ib-run muted">아직 정리 기록이 없습니다 — 「지금 정리」나 자동 정리가 돌면 결과가 여기 나옵니다 ${inboxIdleHtml(0)}</div>`;
  const r = runs[0], open = runs.find(x => x.id === S.runOpen);
  return `<div class="ib-run ${r.status}"><span class="ib-run-l">${runLabel(r)}</span>${r.status === "running" ? "" : inboxIdleHtml(r.started_at)}
      ${r.summary ? `<button class="linkish" data-action="inbox-run-sum" data-run="${r.id}">${S.runOpen === r.id ? "요약 접기" : "요약 ▾"}</button>` : ""}
      ${runs.length > 1 ? `<button class="linkish" data-action="inbox-run-hist">${S.runHist ? "기록 접기" : "기록"}</button>` : ""}</div>` +
    (S.runHist ? `<div class="ib-run-hist">${runs.map(x => `<button class="ib-run-row ${S.runOpen === x.id ? "on" : ""}" data-action="inbox-run-sum" data-run="${x.id}">${runLabel(x)}</button>`).join("")}</div>` : "") +
    (open && open.summary ? `<div class="ib-run-sum md">${mdCached(open.summary)}</div>` : "");
}
async function startInboxRun() {
  try {
    const r = await api("POST", "/api/inbox/run", {});
    toast(r.started ? "정리 일꾼을 돌립니다 — 끝나면 인박스 맨 위에 결과가 나옵니다" : r.message);
  } catch (e) { toast(e.message); }
  loadInbox(true, false);
}
async function loadInbox(force, rerender = force) {
  // 정리 일꾼이 도는 동안은 자주(결과 줄·새 카드가 곧 바뀐다)
  const every = (S.inboxRuns || []).some(x => x.status === "running") ? 4000 : 15000;
  if (!force && Date.now() - S.inbox.at < every && S.inbox.newCount === S.inboxNew) return;
  try {
    const [r, f, runs] = await Promise.all([api("GET", "/api/inbox"), api("GET", "/api/connectors").catch(() => null),
      api("GET", "/api/inbox/runs?limit=10").catch(() => null)]);
    S.inbox = { items: r.items, at: Date.now(), checked: r.checked_at, newCount: S.inboxNew, sig: S.inbox.sig, feed: f };
    if (runs) { S.inboxIdle = runs.idle_at || 0; setInboxRuns(runs.runs); }
  } catch (e) { return; }
  const feed = S.inbox.feed;
  // 커넥터 상태: 커넥터마다 마지막 수집·받은 시점·오류, 아직 카드로 안 만든 항목 수
  S.connTypes = feed ? Object.fromEntries(feed.types.map(t => [t.type, t])) : (S.connTypes || {});
  const live = feed ? feed.instances.filter(x => x.enabled && x.role !== "info") : [];   // 정보 전용(캘린더)은 수집 줄에서 뺀다
  const pend = live.reduce((n, x) => n + (x.pending || 0), 0);
  const feedLine = `<div class="bp-feed muted">${live.length ? "수집 " + live.map(x => x.error
    ? `<span class="bp-feed-err" title="${esc(x.error)}">${x.icon} ${esc(x.name)} 오류</span>`
    : `<span title="${x.last ? "이 시각의 메시지까지 받았습니다 — 잠들었다 깨면 여기부터 이어 받습니다" : ""}">${x.icon} <span class="cn-name">${esc(x.name)}</span> ${x.polled_at ? rel(x.polled_at) : "-"}${
      x.last ? ` <span class="muted">(${dateLabel(x.last) === "오늘" ? "" : dateLabel(x.last) + " "}${hm(x.last)}까지)</span>` : ""}</span>`).join(" · ") +
    (pend ? ` · <b>대기 ${pend}</b>` : "") : "연결된 커넥터가 없습니다"} · <button class="linkish" data-action="open-connectors">커넥터 관리</button></div>`;
  // 수집 시각(polled_at)은 2분마다 바뀐다 — 그것 때문에 카드까지 통째로 다시 그리지 않고 수집 줄만 바꾼다
  const feedSig = feed ? JSON.stringify(feed.instances.map(x => [x.id, x.enabled, x.error, x.pending])) : "";
  const sig = JSON.stringify(S.inbox.items.map(x => [x.id, x.status, x.updated_at])) + S.inbox.checked + feedSig;
  const box = $("#bpInbox");
  const oldFeed = box.querySelector(".bp-feed");
  if (oldFeed) oldFeed.outerHTML = feedLine;
  if (sig === S.inbox.sig && !rerender) return;
  if (!rerender && typingIn(box)) return;   // 쓰는 중 — sig 를 안 적어 두어 다음 차례에 그린다
  S.inbox.sig = sig;
  const items = S.inbox.items;
  const opened = openDetails(box);
  const head = `<div class="bp-inbox-head muted">${S.inbox.checked ? "마지막 확인 " + dateLabel(S.inbox.checked) + " " + hm(S.inbox.checked) : "아직 확인한 적 없음"}
    <button class="btn" data-action="inbox-run" title="정리 일꾼이 모인 항목을 읽어 카드로 만듭니다 — 브레인 대화에는 끼어들지 않습니다(사용량이 듭니다)">지금 정리</button></div>
    <div class="ib-run-box">${inboxRunHtml()}</div>${feedLine}`;
  if (!items.length) {
    box.innerHTML = head + '<div class="bp-empty"><p>카드가 없습니다. 「지금 정리」를 누르면 정리 일꾼이 슬랙에서 나를 부른 것들을 모아 옵니다.</p></div>';
    return;
  }
  const drafts = {};
  box.querySelectorAll(".ib-prompt").forEach(t => { if (t.value !== t.defaultValue) drafts[t.className + "#" + t.dataset.id] = t.value; });
  box.querySelectorAll(".ibc-text").forEach(t => { S.chatDraft[t.dataset.id] = t.value; });
  // 새 카드 위, 처리한 카드(입력창에 넣음·넥스트 액션에 넣음·확인 완료)는 아래 따로 — 하루 지나면 사라진다
  const open = items.filter(x => ibCol(x) === "todo"), doing = items.filter(x => ibCol(x) === "doing"),
    reply = items.filter(x => ibCol(x) === "reply"), handled = items.filter(x => ibCol(x) === "done");
  box.innerHTML = head + `<div class="bp-inbox-board"><button class="linkish" data-view-go="inbox">⤢ 칸반으로 보기</button></div>` +
    (open.length ? open.map(inboxCardHtml).join("") : '<div class="bp-empty ib-none"><p>새 카드가 없습니다.</p></div>') +
    (doing.length ? `<div class="ib-sep"><span>진행 중 ${doing.length}개</span><span class="muted">세션에 넘겼거나 내 할 일에 넣은 카드</span></div>` +
      doing.map(inboxCardHtml).join("") : "") +
    (reply.length ? `<div class="ib-sep"><span>답장만 남음 ${reply.length}개</span><span class="muted">세션이 끝낸 일 — 결과와 답장 초안을 보고 보내면 완료</span></div>` +
      reply.map(inboxCardHtml).join("") : "") +
    (handled.length ? `<div class="ib-sep"><span>완료 ${handled.length}개</span><span class="muted">설정한 기간이 지나면 목록에서 사라집니다 · 되돌리기로 다시 꺼낼 수 있습니다</span></div>` +
      handled.map(inboxCardHtml).join("") : "");
  box.querySelectorAll(".ib-prompt").forEach(t => { const k = t.className + "#" + t.dataset.id; if (drafts[k] != null) t.value = drafts[k]; });
  reopenDetails(box, opened);
}
const CHEVRON = '<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M3.5 6 8 10.5 12.5 6" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
function inboxCardHtml(x) {
  // 종류: me(내가 할 일 — 답장·승인·권한·일정·결정) · session/new(Claude Code 세션에 맡길 일) · none(예전 카드)
  const kind = x.rec_type === "me" || x.rec_type === "none" ? "me" : "work";
  const target = x.rec_session ? S.sessions.find(s => s.session_id === x.rec_session && s.state !== "ended") : null;
  const work = x.rec_type === "session"
    ? (target ? `→ <b>${esc(sessionTitle(target))}</b> 세션` : `→ 세션 ${esc((x.rec_session || "").slice(0, 8))} <span class="muted">(보드에 없음)</span>`)
    : x.rec_type === "new" ? `→ <b>새 세션</b> <span class="muted">${esc(base(x.rec_folder || ""))}${x.rec_model ? " · " + esc(x.rec_model) : ""}</span>`
    : target ? `세션에 맡길 부분 → <b>${esc(sessionTitle(target))}</b>` : x.prompt ? "세션에 맡길 부분 → 새 세션" : "";
  const urg = { high: "🔴 급함", normal: "", low: "낮음" }[x.urgency] || "";
  const done = x.status === "done" || x.status === "ignored";
  const canFill = target && x.prompt;
  const canNew = x.prompt && (x.rec_type === "new" || (x.rec_type === "session" && !target) || (kind === "me" && !target));
  const active = ["sent", "working", "waiting", "summarizing", "needs"].includes(x.stage);
  const canDelegate = target && x.prompt && !active && (x.status === "new" || x.status === "doing");
  // ▾ 다른 세션 고르기: 브레인이 고른 세션이 아닐 때 맡기기·입력창에·넥스트 액션에 보낼 세션을 바꾼다
  const canPick = !done && !active && (x.prompt || kind === "work");
  const pick = canPick ? `<button class="btn ${canDelegate || (kind === "work" && canFill) ? "primary" : ""} ib-pick" data-action="ib-pick" data-id="${x.id}" aria-haspopup="true"
    title="다른 세션 고르기 — 맡기기·입력창에·넥스트 액션에 보낼 세션을 바꿉니다">${CHEVRON}</button>` : "";
  const delegateBtn = canDelegate ? `<button class="btn primary ib-fill" data-action="ib-delegate" data-id="${x.id}"
        title="${esc(sessionTitle(target))} 세션에 프롬프트를 보내고, 끝나면 결과 요약과 답장 초안을 이 카드에 넣습니다">▶ <span class="ib-target">${esc(clamp(sessionTitle(target), 18))}</span> <span>에 맡기기</span></button>` : "";
  const fillBtn = canFill ? `<button class="btn ${kind === "work" && !canDelegate ? "primary" : ""} ib-fill" data-action="ib-fill" data-id="${x.id}"
        title="${esc(sessionTitle(target))} — 그 칸의 입력창에 넣기(전송은 직접)">→ <span class="ib-target">${esc(clamp(sessionTitle(target), 22))}</span> <span>입력창에</span></button>` : "";
  // ▾ 는 첫 세션 버튼에 붙인다(입력칸의 보내기 ▾ 처럼). 세션 버튼이 없으면(새 세션 추천·보드에 없는 세션) 따로 「세션 고르기 ▾」
  const split = (b) => pick ? `<span class="ib-split">${b}${pick}</span>` : b;
  const sessionBtns = delegateBtn ? split(delegateBtn) + fillBtn
    : fillBtn ? split(fillBtn)
    : canPick ? `<button class="btn ib-pick-solo" data-action="ib-pick" data-id="${x.id}" aria-haspopup="true" title="맡길 세션 고르기 — 보드에 열린 세션 중에서">세션 고르기 ${CHEVRON}</button>` : "";
  return `<article class="ib-card ${done ? "done" : ""} kind-${kind}" data-id="${x.id}">
    <div class="ib-meta">${x.status === "new" && x.created_at > S.inboxNewSince ? '<span class="ib-new" title="지난번 인박스를 본 뒤 새로 온 카드">NEW</span>' : ""}<span class="ib-kind ${kind}">${kind === "me" ? "🧑 내가 할 일" : "🤖 세션 작업"}</span>${urg ? `<span class="ib-urg ${x.urgency}">${urg}</span>` : ""}<span class="ib-src" title="${esc(((S.connTypes || {})[x.source || "slack"] || {}).name || x.source || "")}">${((S.connTypes || {})[x.source || "slack"] || {}).icon || "💬"}</span><b>${esc(x.channel || "")}</b>
      <span class="muted">${esc(x.author || "")} · ${x.slack_at ? rel(x.slack_at) : ""}</span>
      ${x.sensitive ? '<span class="ib-sens" title="민감정보가 있을 수 있어 요약을 줄였습니다 — 원문은 슬랙에서">민감</span>' : ""}
      ${x.status !== "new" ? `<span class="ib-state">${{ sent: "넘김", doing: "진행 중", reply: "답장만 남음", done: "확인함", ignored: "확인함" }[x.status] || x.status}</span>` : ""}
      <span class="spacer"></span>${x.permalink && /^https:\/\/[\w.-]+\.(slack|google)\.com\//.test(x.permalink) ? `<a href="${esc(x.permalink)}" target="_blank" rel="noopener noreferrer">원문 ↗</a>` : ""}</div>
    ${stageHtml(x)}
    ${x.todo ? `<div class="ib-todo">${esc(x.todo)}</div>` : ""}
    <div class="ib-summary">${esc(x.summary)}</div>
    ${work ? `<div class="ib-rec">${work}${x.rec_reason ? ` <span class="muted">— ${esc(x.rec_reason)}</span>` : ""}</div>` : ""}
    ${x.reply_draft ? `<details class="ib-pd"><summary>답장 초안</summary><div class="code-wrap ib-reply"><button class="code-copy" data-action="copy-reply" data-id="${x.id}">복사</button>
      <textarea class="ib-prompt ib-reply-text" data-id="${x.id}" rows="4">${esc(x.reply_draft)}</textarea></div></details>` : ""}
    ${x.prompt ? `<details class="ib-pd"><summary>${kind === "me" ? "세션에 맡길 부분 — 프롬프트" : "보낼 프롬프트"}</summary><textarea class="ib-prompt ib-work" data-id="${x.id}" rows="6">${esc(x.prompt)}</textarea></details>` : ""}
    <div class="ib-actions">
      ${kind === "me" && brainSession() ? `<button class="btn primary ib-fill" data-action="ib-note" data-id="${x.id}" title="브레인 칸의 넥스트 액션에 내 할 일로 넣기">→ <span>내 할 일에</span></button>` : ""}
      ${sessionBtns}
      ${canNew ? `<button class="btn ${kind === "work" ? "primary" : ""}" data-action="ib-new" data-id="${x.id}">새 세션으로</button>` : ""}
      ${kind === "work" && target ? `<button class="btn ib-fill" data-action="ib-note" data-id="${x.id}" title="${esc(sessionTitle(target))} 칸의 넥스트 액션에 할 일로 넣기">
        → <span class="ib-target">${esc(clamp(sessionTitle(target), 18))}</span> <span>넥스트 액션에</span></button>` : ""}
      ${x.log_count ? `<button class="btn ghost ib-log-btn ${S.logOpen.has(String(x.id)) ? "on" : ""}" data-action="ib-log" data-id="${x.id}" title="이 카드가 어떻게 바뀌어 왔는지">🕘 기록 ${x.log_count}</button>` : ""}
      ${(x.source || "slack") === "slack" && /^[CDG][A-Z0-9]+:\d+\.\d+$/.test(x.key || "") ? `<button class="btn ghost ib-files-btn ${S.filesOpen.has(String(x.id)) ? "on" : ""}"
        data-action="ib-files" data-id="${x.id}" title="원문과 그 스레드에 붙은 파일 — 누르면 슬랙에서 열기">📎 첨부</button>` : ""}
      <button class="btn ghost ib-chat-btn ${S.chatOpen.has(String(x.id)) ? "on" : ""}" data-action="ib-chat" data-id="${x.id}"
        title="이 카드 내용과 슬랙 대화만 두고 따로 묻고 답합니다(브레인 대화와 섞이지 않음)">💬 대화</button>
      <span class="spacer"></span>
      ${x.status === "new" ? `<button class="btn ghost" data-action="ib-status" data-st="doing" data-id="${x.id}" title="진행 중으로 옮깁니다">진행 중</button>` : ""}
      ${x.status === "done" || x.status === "ignored" ? `<button class="btn ghost" data-action="ib-status" data-st="new" data-id="${x.id}">되돌리기</button>`
        : `<button class="btn ghost" data-action="ib-status" data-st="done" data-id="${x.id}" title="완료로 옮깁니다(설정한 기간 뒤 사라짐)">확인 완료</button>`}
    </div>${S.filesOpen.has(String(x.id)) ? `<div class="ib-files" data-id="${x.id}">${cardFilesHtml(x.id)}</div>` : ""}${S.logOpen.has(String(x.id)) ? `<div class="ib-log" data-id="${x.id}">${cardLogHtml(x.id)}</div>` : ""}${S.chatOpen.has(String(x.id)) ? cardChatHtml(x.id) : ""}</article>`;
}
// 「넥스트 액션에」를 넣을 곳: 내가 할 일은 브레인 칸(내 할 일 목록), 세션 작업은 그 세션
function noteTarget(x) {
  if (x.rec_type === "me" || x.rec_type === "none") return brainSession();
  const s = x.rec_session && S.sessions.find(t => t.session_id === x.rec_session && t.state !== "ended");
  return s || brainSession();
}
// ---------------------------------------------------------------- 카드 대화(카드 하나만 두고 따로 묻고 답하기)
// 서버가 카드마다 따로 claude -p 세션을 쓴다(csl/cardchat.py) — 브레인 대화 맥락을 쓰지 않는다
S.chatOpen = new Set();   // 대화 영역을 펼친 카드 id
S.chat = {};              // id → 서버가 준 {messages, running, partial, tool}
S.chatDraft = {};         // id → 쓰다 만 질문(카드 목록을 다시 그려도 남게)
S.chatModel = {};         // id → 고른 모델(화면 이름 — 서버가 정확한 ID 로 바꿔 실행)
S.chatEffort = {};        // id → 고른 effort("" 이면 모델 기본값)
const CHAT_MODELS = ["Sonnet 5.5", "Opus 5.5", "Fable 5.1", "Haiku 5.5"];
const CHAT_EFFORTS = ["", "low", "medium", "high", "xhigh", "max"];
function chatReplyBlock(text) {
  // 답장 초안으로 쓸 글: ``` 블록이 있으면 그 안, 없으면 전체
  const m = /```[^\n]*\n([\s\S]*?)```/.exec(text || "");
  return (m ? m[1] : text || "").trim();
}
function cardChatMsgs(id) {
  const c = S.chat[id];
  if (!c) return '<div class="muted ibc-empty">불러오는 중…</div>';
  const rows = c.messages.map(m => m.role === "user"
    ? `<div class="ibc-u md">${mdCached(m.text)}</div>`
    : m.role === "error" ? `<div class="ibc-err">⚠ ${esc(m.text)}</div>`
    : `<div class="ibc-a" data-mid="${m.id}"><div class="md">${mdCached(m.text)}</div>
        <div class="ibc-acts"><button class="linkish" data-action="ib-chat-draft" data-id="${id}" data-mid="${m.id}" title="이 답(코드 블록이 있으면 그 안)을 카드의 답장 초안으로">답장 초안으로</button>
        <button class="linkish" data-action="ib-chat-todo" data-id="${id}" data-mid="${m.id}" title="이 답의 첫 줄을 카드의 할 일로">할 일로</button>
        <span class="muted">${esc(m.model || "")}</span></div></div>`).join("");
  const live = c.running ? `<div class="ibc-a pending">${c.partial ? `<div class="md">${md(c.partial)}</div>` : ""}
      <div class="muted"><span class="ibc-spin"></span> ${c.tool ? "확인 중: " + esc(c.tool) : "생각 중…"}</div></div>` : "";
  return rows || live ? rows + live : '<div class="muted ibc-empty">이 카드 내용과 슬랙 원문·이어진 대화를 보고 답합니다. 예: 「지금 내가 할 것만」 「답장 초안 다시 써 줘」</div>';
}
function cardChatHtml(id) {
  id = String(id);
  const model = S.chatModel[id] || (S.settings || {}).cardchat_model || "Sonnet 5.5";
  const effort = id in S.chatEffort ? S.chatEffort[id] : (S.settings || {}).cardchat_effort || "";
  return `<div class="ib-chat" data-id="${id}">
    <div class="ibc-msgs">${cardChatMsgs(id)}</div>
    <div class="ibc-input"><textarea class="ibc-text" data-id="${id}" rows="2" placeholder="이 카드에 대해 묻기 · Enter 보내기 · Shift+Enter 줄바꿈">${esc(S.chatDraft[id] || "")}</textarea>
      <div class="ibc-side"><div class="ibc-pick"><select class="ibc-model" data-id="${id}" title="모델(정확한 버전)">${CHAT_MODELS.map(m =>
        `<option ${m === model ? "selected" : ""}>${m}</option>`).join("")}</select>
        <select class="ibc-effort" data-id="${id}" title="effort — 「기본」은 모델 기본값">${CHAT_EFFORTS.map(v =>
        `<option value="${v}" ${v === effort ? "selected" : ""}>${v || "기본"}</option>`).join("")}</select></div>
      <button class="btn primary" data-action="ib-chat-send" data-id="${id}">보내기</button>
      <button class="linkish" data-action="ib-chat-clear" data-id="${id}" title="이 카드의 대화를 지우고 처음부터">지우기</button></div></div>
  </div>`;
}
function drawCardChat(id) {
  document.querySelectorAll(`.ib-chat[data-id="${id}"] .ibc-msgs`).forEach(box => {
    const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.innerHTML = cardChatMsgs(id);
    if (atEnd) box.scrollTop = box.scrollHeight;
  });
}
async function fetchCardChat(id) {
  try { S.chat[id] = await api("GET", `/api/inbox/${id}/chat`); } catch (e) { return; }
  drawCardChat(id);
  clearTimeout((S.chatTimer || {})[id]);
  S.chatTimer = S.chatTimer || {};
  if (S.chat[id].running && S.chatOpen.has(id)) S.chatTimer[id] = setTimeout(() => fetchCardChat(id), 1500);
}
async function sendCardChat(id, root) {
  root = root || document;
  const t = root.querySelector(`.ibc-text[data-id="${id}"]`);
  const text = (t ? t.value : "").trim();
  if (!text) return;
  const sel = root.querySelector(`.ibc-model[data-id="${id}"]`), eff = root.querySelector(`.ibc-effort[data-id="${id}"]`);
  try {
    await api("POST", `/api/inbox/${id}/chat`, { text, model: sel ? sel.value : undefined, effort: eff ? eff.value : undefined });
  } catch (e) { toast(e.message); return; }
  t.value = ""; S.chatDraft[id] = "";
  fetchCardChat(id);
}
async function cardChatAction(a, el, id) {
  id = String(id);
  if (a === "ib-chat") {
    if (S.chatOpen.has(id)) S.chatOpen.delete(id); else S.chatOpen.add(id);
    el.classList.toggle("on", S.chatOpen.has(id));
    const card = el.closest(".ib-card"), cur = card.querySelector(".ib-chat");
    if (cur) cur.remove();
    if (S.chatOpen.has(id)) {
      card.insertAdjacentHTML("beforeend", cardChatHtml(id));
      fetchCardChat(id);
      const t = card.querySelector(".ibc-text");
      if (t) t.focus();
    }
    return;
  }
  if (a === "ib-chat-send") return sendCardChat(id, el.closest(".ib-card"));
  if (a === "ib-chat-clear") {
    try { await api("DELETE", `/api/inbox/${id}/chat`); } catch (e) { toast(e.message); return; }
    return fetchCardChat(id);
  }
  const m = ((S.chat[id] || {}).messages || []).find(x => String(x.id) === el.dataset.mid);
  if (!m) return;
  const body = a === "ib-chat-draft" ? { reply_draft: chatReplyBlock(m.text) }
    : { todo: chatReplyBlock(m.text).split("\n").map(l => l.replace(/^[-*•\d.\s]+/, "").trim()).find(Boolean) || "" };
  try { await api("PATCH", "/api/inbox/" + id, body); } catch (e) { toast(e.message); return; }
  toast(a === "ib-chat-draft" ? "카드의 답장 초안을 바꿨습니다" : "카드의 할 일을 바꿨습니다");
  await loadInbox(true);
  if (S.view === "inbox") loadKanban(true);
}
document.addEventListener("keydown", e => {
  const t = e.target;
  if (!t.classList || !t.classList.contains("ibc-text") || e.key !== "Enter" || e.shiftKey || imeBusy(e)) return;
  e.preventDefault();
  sendCardChat(t.dataset.id, t.closest(".ib-card"));
});
document.addEventListener("change", e => {
  if (e.target.classList && e.target.classList.contains("ibc-model")) S.chatModel[e.target.dataset.id] = e.target.value;
  if (e.target.classList && e.target.classList.contains("ibc-effort")) S.chatEffort[e.target.dataset.id] = e.target.value;
});

// ---------------------------------------------------------------- 카드 맡기기 진행·카드 기록
const STAGE = {
  sent: ["📤", "보냄"], working: ["⚙", "세션 작업 중"], waiting: ["⏳", "세션이 확인을 기다림 — 그 세션에서 답해 주세요"],
  summarizing: ["✍", "결과 정리 중"], needs: ["❓", "세션이 내 답을 기다림 — 그 세션에 답하면 이어서 봅니다"],
  reply: ["✉", "답장만 남음 — 아래 초안 확인 후 보내고 「확인 완료」"], error: ["⚠", "멈춤 — 기록을 보세요"],
};
function stageHtml(x) {
  const st = STAGE[x.stage];
  if (!st || !x.delegated_session) return "";
  const live = ["sent", "working", "summarizing"].includes(x.stage);
  return `<div class="ib-stage st-${x.stage}">${live ? '<span class="ibc-spin"></span>' : st[0]}
    <a class="sref" data-sid="${esc(x.delegated_session)}">${esc(x.delegated_title || "세션")}</a> 에 맡김 · ${st[1]}
    <span class="muted">${x.delegated_at ? rel(x.delegated_at) : ""}</span></div>`;
}
S.logOpen = new Set();
S.cardLog = {};
const LOG_ICON = { create: "🆕", update: "✏", status: "↔", edit: "✏", delegate: "▶", draft: "✉" };
function cardLogHtml(id) {
  const items = S.cardLog[id];
  if (!items) return '<div class="muted">불러오는 중…</div>';
  return items.map(l => `<div class="ibl-row k-${esc(l.kind.split(":")[0])}"><span class="ibl-time">${dateLabel(l.at) === "오늘" ? "" : dateLabel(l.at) + " "}${hm(l.at)}</span>
    <span class="ibl-icon">${LOG_ICON[l.kind] || (STAGE[l.kind.split(":")[1]] || ["•"])[0]}</span>
    <span class="ibl-text">${esc(l.text || l.kind)}</span></div>`).join("") || '<div class="muted">기록이 없습니다</div>';
}
// 카드 원문(과 스레드)의 첨부 파일 — 서버가 슬랙 커넥터로 그때 읽는다. 누르면 슬랙의 파일 페이지(로그인한 브라우저)
S.filesOpen = new Set();
S.cardFiles = {};
function cardFilesHtml(id) {
  const f = S.cardFiles[id];
  if (!f) return '<div class="muted">불러오는 중…</div>';
  if (f.error) return `<div class="muted">${esc(f.error)}</div>`;
  if (!f.files.length) return '<div class="muted">원문과 스레드에 첨부 파일이 없습니다</div>';
  return f.files.map(x => `<a class="ib-file" href="${esc(/^https:\/\/[\w.-]+\.slack\.com\//.test(x.link) ? x.link : f.link || "#")}" target="_blank" rel="noopener noreferrer"
      title="슬랙에서 열기">📎 <b>${esc(x.name)}</b> <span class="muted">${esc(x.type || "")} · ${esc(x.who || "")} · ${x.at ? rel(x.at) : ""}</span></a>`).join("");
}
async function toggleCardFiles(id, el) {
  id = String(id);
  if (S.filesOpen.has(id)) S.filesOpen.delete(id); else S.filesOpen.add(id);
  const card = el.closest(".ib-card"), cur = card.querySelector(".ib-files");
  el.classList.toggle("on", S.filesOpen.has(id));
  if (cur) cur.remove();
  if (!S.filesOpen.has(id)) return;
  const box = document.createElement("div");
  box.className = "ib-files";
  box.dataset.id = id;
  box.innerHTML = cardFilesHtml(id);
  (card.querySelector(".ib-log, .ib-chat") ? card.querySelector(".ib-log, .ib-chat").before(box) : card.append(box));
  try { S.cardFiles[id] = await api("GET", `/api/inbox/${id}/files`); }
  catch (e) { S.cardFiles[id] = { error: e.message, files: [] }; }
  const now = card.querySelector(".ib-files");
  if (now) now.innerHTML = cardFilesHtml(id);
}
async function toggleCardLog(id, el) {
  id = String(id);
  if (S.logOpen.has(id)) S.logOpen.delete(id); else S.logOpen.add(id);
  const card = el.closest(".ib-card"), cur = card.querySelector(".ib-log");
  el.classList.toggle("on", S.logOpen.has(id));
  if (cur) cur.remove();
  if (!S.logOpen.has(id)) return;
  try { S.cardLog[id] = (await api("GET", `/api/inbox/${id}/log`)).items; } catch (e) { S.cardLog[id] = []; }
  const html = `<div class="ib-log" data-id="${id}">${cardLogHtml(id)}</div>`;
  const chat = card.querySelector(".ib-chat");
  chat ? chat.insertAdjacentHTML("beforebegin", html) : card.insertAdjacentHTML("beforeend", html);
}

function inboxItem(id) { return S.inbox.items.find(x => String(x.id) === String(id)); }
function inboxPrompt(id, el) {
  const t = ((el && el.closest(".ib-card")) || document).querySelector(`.ib-work[data-id="${id}"]`);
  return t ? t.value : (inboxItem(id) || {}).prompt || "";
}
async function setInboxStatus(id, status) {
  try { await api("PATCH", "/api/inbox/" + id, { status }); } catch (e) { toast(e.message); }
  await poll();
  await loadInbox(true);
  if (S.view === "inbox") loadKanban(true);
}
// 인박스 카드 ▾: 맡길 세션 고르기 — 보드에 열린 세션(브레인·종료·보관 제외), 최근 것부터. 고르면 카드의 세션이 바뀐다
function openInboxPick(btn, x) {
  const list = S.sessions.filter(s => !s.brain && s.state !== "ended" && !s.archived)
    .sort((a, b) => (b.last_activity || 0) - (a.last_activity || 0));
  if (!list.length) return toast("보드에 열린 세션이 없습니다");
  const rows = list.map(s => {
    const cur = s.session_id === x.rec_session;
    return `<button class="cm-item xm-opt ibp-row ${cur ? "on" : ""}" data-ibpick="${esc(s.session_id)}" data-id="${x.id}" ${cur ? "disabled" : ""}>
      <span class="ibp-main"><span class="dot ${displayState(s)}"></span><span class="ibp-t">${esc(clamp(sessionTitle(s) || s.session_id.slice(0, 8), 40))}</span>${cur ? '<span class="ibp-cur">지금</span>' : ""}</span>
      <span class="muted ibp-sub">${esc(s.project_name || base(s.cwd || ""))}${s.last_activity ? " · " + rel(s.last_activity) : ""}</span></button>`;
  }).join("");
  const m = openXMenu(btn, `<div class="xm-head">맡길 세션 고르기 <span class="muted">맡기기·입력창에·넥스트 액션에 이 세션으로 보냅니다</span></div><div class="ibp-list">${rows}</div>`);
  m.classList.add("ib-pick-menu");
  placeMenu(m, btn, "left");   // 폭을 바꾼 뒤 다시 맞춘다
}
async function pickInboxSession(el) {
  closeXMenu();
  const s = sessionById(el.dataset.ibpick);
  try {
    await api("PATCH", "/api/inbox/" + el.dataset.id, { rec_session: el.dataset.ibpick });
    toast(`「${s ? sessionTitle(s) : el.dataset.ibpick.slice(0, 8)}」 세션으로 바꿨습니다`);
  } catch (e) { toast(e.message); }
  await loadInbox(true);
  if (S.view === "inbox") loadKanban(true);
}
async function inboxAction(a, el) {
  const id = el.dataset.id, x = inboxItem(id);
  if (!x) return;
  if (a === "ib-status") return setInboxStatus(id, el.dataset.st);
  if (a.startsWith("ib-chat")) return cardChatAction(a, el, id);
  if (a === "ib-log") return toggleCardLog(id, el);
  if (a === "ib-files") return toggleCardFiles(id, el);
  if (a === "ib-delegate") {
    const target = x.rec_session && sessionById(x.rec_session);
    if (!target) return toast("맡길 세션이 없습니다");
    try {
      const r = await api("POST", `/api/inbox/${id}/delegate`, { session_id: target.session_id, prompt: inboxPrompt(id, el) });
      toast(r.message);
    } catch (e) { toast(e.message); }
    await poll();
    await loadInbox(true);
    if (S.view === "inbox") loadKanban(true);
    return;
  }
  if (a === "ib-pick") return openInboxPick(el, x);
  if (a === "ib-fill") {
    // 보내지는 않고 그 세션 입력창에 채워 둔다 — 확인하고 Enter 는 직접
    if (!await fillComposer(x.rec_session, inboxPrompt(id, el))) return;   // 칸을 못 꺼냄(focusCol 이 안내함) — 카드는 그대로
    toast("그 세션 입력창에 넣었습니다 · 확인하고 Enter 로 보내세요");
    return setInboxStatus(id, "sent");
  }
  if (a === "ib-new") {
    $("#newMenu").hidden = true;   // 이미 열려 있으면 openNewMenu 가 닫기만 한다
    await openNewMenu();
    const m = $("#newMenu");
    if (x.rec_folder) { m.querySelector(".nm-cwd").value = x.rec_folder; markFolder(m); }
    m.querySelector(".nm-prompt").value = inboxPrompt(id, el);
    if (x.rec_model) m.querySelector(".nm-model").value = x.rec_model;
    m.dataset.inbox = id;   // 이 메뉴로 띄우면 그 카드를 「넘김」으로(메뉴를 새로 열면 지워진다)
    return;
  }
  if (a === "ib-note") {
    const target = noteTarget(x);
    if (!target) return toast("넣을 세션이 없습니다 — 브레인을 시작하거나 세션을 연 뒤에 해 주세요");
    const text = (x.todo || x.summary) + (x.permalink ? "\n" + x.permalink : "");
    await api("POST", "/api/notes", { session_id: target.session_id, text });
    toast(target.brain ? "내 할 일(브레인 넥스트 액션)에 넣었습니다" : `「${sessionTitle(target)}」의 넥스트 액션에 넣었습니다`);
    return setInboxStatus(id, "doing");
  }
}
$("#brainBtn").addEventListener("click", () => setBrainOpen(!S.brainOpen));
$("#bpClose").addEventListener("click", () => setBrainOpen(false));
document.querySelectorAll(".bp-tab").forEach(b => b.addEventListener("click", () => showBrainTab(b.dataset.bpTab)));
$("#bpAuto").addEventListener("change", async e => {
  try {
    await api("POST", "/api/brain/settings", { slack_every: +e.target.value });
    toast(+e.target.value ? `수집된 항목이 있으면 ${e.target.selectedOptions[0].textContent} 브레인이 카드로 정리합니다(평일 9~20시, 쉬고 있을 때)` : "자동 정리를 껐습니다 — 「지금 정리」나 「인박스 정리」로 정리합니다");
  } catch (err) { toast(err.message); }
});
async function startBrain() {
  const model = ($("#bpModel") || {}).value || "", permission_mode = ($("#bpPerm") || {}).value || "";
  try {
    const r = await api("POST", "/api/brain/start", { model, permission_mode });
    toast("브레인을 보드 터미널에 띄웠습니다");
    S.brainLaunch = { state: "starting", handle: r.handle, terminal: r.terminal };
    S.brainLaunchAt = 0;
    renderBrainLaunch();
    pollBrainLaunch();
  } catch (e) { toast("브레인 시작 실패: " + e.message); }
}
// 띄운 브레인이 세션으로 잡히기 전(훅은 Claude Code 가 다 뜬 뒤에 온다): 처음 실행이면 폴더 신뢰·권한 모드 확인 창이
// 터미널에 떠서 멈춘다 — 그 창의 선택지를 패널에 보여 보드에서 고르거나 터미널로 가게 한다
S.brainLaunch = null;   // {state: starting·prompt·ready, prompt?, screen?}
S.brainLaunchAt = 0;
async function pollBrainLaunch(force) {
  if (!S.brainOpen || brainSession() || (!force && Date.now() - S.brainLaunchAt < 2000)) return;
  S.brainLaunchAt = Date.now();
  let L;
  try { L = await api("GET", "/api/brain/launch"); } catch (e) { return; }
  if (brainSession()) return;
  if (L.state === "none" || L.state === "running") {
    if (S.brainLaunch) { S.brainLaunch = null; $("#bpChat").innerHTML = ""; renderBrain(); }
    return;
  }
  if (L.state === "gone") {
    S.brainLaunch = null;
    toast("브레인 탭이 닫혔습니다 — 다시 시작해 주세요");
    $("#bpChat").innerHTML = "";
    return renderBrain();
  }
  S.brainLaunch = L;
  renderBrainLaunch();
  setTimeout(() => pollBrainLaunch(), 2100);   // 잡힐 때까지 이어서 본다
}
function renderBrainLaunch() {
  const L = S.brainLaunch, chat = $("#bpChat");
  if (!L) return;
  const key = JSON.stringify(L);
  if (chat._launchKey === key && chat.querySelector(".bp-launch")) return;
  chat._launchKey = key;
  const v = L.state === "prompt" && L.prompt;
  // 폴더 신뢰 창은 1번이 「No, exit」다 — 거절이 아닌 첫 선택지를 강조한다. 안내 문장은 보통 글, 경로만 고정폭
  const isNo = o => /^(no|exit|cancel|remove|delete|discard)\b|아니|삭제|지우/i.test(o.label);
  const main = v && (v.options.find(o => !isNo(o)) || v.options[0]);
  const body = v ? `<div class="prompt-card bp-launch-card"><div class="ask-head"><b>🔐 확인 필요</b><span class="muted">처음 실행 확인 창 — 여기서 고르면 터미널에 넣습니다</span></div>
      <div class="pc-head">${v.head.map((ln, i) => `<div class="${i === 0 ? "pc-title" : /^[\/~]/.test(ln) ? "pc-line" : "pc-desc"}">${esc(ln)}</div>`).join("")}</div>
      <div class="pc-opts">${v.options.map(o => `<button class="btn pc-opt ${o === main ? "primary" : ""} ${isNo(o) ? "pc-no" : ""}"
        data-action="brain-launch-pick" data-n="${o.n}" data-label="${esc(o.label)}" title="${esc(o.hint || o.label)}">
        <b>${o.n}.</b> ${esc(o.label)}${o.hint ? `<span class="muted">${esc(o.hint)}</span>` : ""}</button>`).join("")}</div>
      <div class="ask-msg muted pc-msg"></div></div>`
    : L.state === "ready" ? `<p class="muted">입력칸까지 떴습니다 — 곧 이 패널에 나타납니다</p>`
    : `<p class="muted">터미널에서 Claude Code 가 뜨기를 기다리는 중입니다</p>${L.screen ? `<div class="bp-launch-screen">${esc(L.screen)}</div>` : ""}`;
  chat.innerHTML = `<div class="bp-empty bp-launch"><div class="bp-launch-head"><b>브레인이 뜨는 중…</b><span class="spacer"></span>
    <button class="btn" data-action="brain-launch-term" title="브레인이 뜨고 있는 터미널 — 보드 터미널·tmux 면 창으로 엽니다">${L.terminal === "web" || L.terminal === "tmux" ? "⌨ 터미널 보기" : "터미널로 이동 ↗"}</button></div>${body}</div>`;
}
async function brainLaunchAct(el) {
  if (el.dataset.action === "brain-launch-term") {
    const L = S.brainLaunch || {};
    if ((L.terminal === "web" || L.terminal === "tmux") && L.handle) { S.brainLaunchPopup = L.handle; return openLaunchTerm(L.handle, L.terminal); }   // 바깥 앱이 없다 — 창으로
    try { toast((await api("POST", "/api/brain/launch/focus", {})).message); } catch (e) { toast(e.message); }
    return;
  }
  const card = el.closest(".bp-launch-card");
  card.querySelectorAll(".pc-opt").forEach(x => { x.disabled = true; });
  try {
    toast((await api("POST", "/api/brain/launch/answer", { n: +el.dataset.n, label: el.dataset.label })).message);
  } catch (e) {
    card.querySelector(".pc-msg").textContent = e.message;
    card.querySelectorAll(".pc-opt").forEach(x => { x.disabled = false; });
  }
  setTimeout(() => pollBrainLaunch(true), 700);   // 다음 창이 이어 뜰 수 있다(신뢰 → 권한 모드)
}
async function brainQuick(prompt) {
  const b = brainSession();
  if (!b) { toast("먼저 브레인을 시작하세요"); setBrainOpen(true); showBrainTab("chat"); return; }
  if (S.brainTab !== "chat") showBrainTab("chat");
  const el = $("#bpChat .composer");
  if (!el) return;
  const ta = el.querySelector(".cmp-text");
  ta.value = prompt;
  S.drafts.set(b.session_id, prompt);
  sendCompose(el);
}
setBrainOpen(S.brainOpen, true);   // 처음 열 때는 애니메이션 없이
// 브레인 대화 비우기(/clear, 패널 머리 ↺ 버튼) — 지침(brain/CLAUDE.md)을 새로 읽히거나 대화가 길어졌을 때. 한 번 누르면 묻고,
// 3초 안에 한 번 더 누르면 보낸다. 쓰던 글(입력창)은 건드리지 않고 /clear 만 따로 보낸다
const BRAIN_CLEAR = { armed: 0 };
async function brainClear() {
  const b = brainSession();
  if (!b) return toast("브레인 세션이 없습니다");
  if (b.state === "working") return toast("브레인이 작업 중입니다 — 끝난 뒤 비우세요");
  if (Date.now() - BRAIN_CLEAR.armed > 3000) {
    BRAIN_CLEAR.armed = Date.now();
    $("#bpClear").classList.add("armed");
    setTimeout(() => $("#bpClear").classList.remove("armed"), 3000);
    return toast("한 번 더 누르면 브레인 대화를 비웁니다(/clear)");
  }
  BRAIN_CLEAR.armed = 0;
  $("#bpClear").classList.remove("armed");
  try {
    await api("POST", `/api/sessions/${b.session_id}/send`, { text: "/clear", images: [] });
    toast("브레인 대화를 비웠습니다 — 지침을 새로 읽습니다");
    setBrainOpen(true);
    showBrainTab("chat");
    setTimeout(() => { refreshBoard().then(renderBrain); }, 1500);
  } catch (e) { toast("비우지 못했습니다: " + e.message); }
}
$("#bpClear").addEventListener("click", brainClear);

// ------------------------------------------------------------------ 새 세션: 폴더를 골라 보드 터미널에 claude 를 띄운다
// 세션은 훅이 기록해 칸이 생긴다. 첫 메시지는 칸이 생긴 뒤 보드의 보내기로 보낸다(여러 줄도 그대로)
const NEW_KEY = "csl.newSession";
const NEW_EFFORTS = ["low", "medium", "high", "xhigh", "max"];   // claude --effort (처음 골라진 값은 설정의 「새 세션 기본 effort」)
async function openNewMenu() {
  const menu = $("#newMenu");
  if (!menu.hidden) { menu.hidden = true; return; }
  menu.dataset.inbox = "";   // 카드에서 연 것이 아니면 띄워도 카드를 옮기지 않는다
  let last = {};
  try { last = JSON.parse(localStorage.getItem(NEW_KEY) || "{}"); } catch (e) { /* 저장소 없음 */ }
  const kind = ["claude", "term", "md"].includes(last.kind) ? last.kind : "claude";
  menu.className = menu.className.replace(/\bnm-kind-\w+/g, "").trim() + " nm-kind-" + kind;
  menu.innerHTML = `
    <div class="ctx-head"><b>새 보드</b></div>
    <div class="nm-kinds" role="tablist">${[["claude", "✳ Claude Code"], ["term", "⌨ 터미널"], ["md", "📝 마크다운"]]
      .map(([k, l]) => `<button type="button" class="nm-kind ${k === kind ? "on" : ""}" data-nm-kind="${k}">${l}</button>`).join("")}</div>
    <label class="nm-label">폴더</label>
    <div class="nm-folders"><div class="muted nm-loading">Orca 프로젝트 불러오는 중…</div></div>
    <input class="ctx-note nm-cwd" placeholder="또는 경로 직접 입력 — /Users/…" value="${esc(last.cwd || "")}" spellcheck="false">
    <div class="nm-board-only"><label class="nm-label">이름 <span class="muted">(선택)</span></label>
      <input class="ctx-note nm-title" placeholder="비워 두면 폴더 이름·날짜로" spellcheck="false"></div>
    <div class="nm-prompt-wrap"><label class="nm-label"><span class="nm-claude-only">첫 메시지</span><span class="nm-md-only">AI 에게 첫 요청</span> <span class="muted">(선택 · Shift+Enter 줄바꿈)</span></label>
    <textarea class="ctx-note nm-prompt" rows="3" placeholder="비워 두면 빈 칸으로 시작합니다"></textarea></div>
    <div class="nm-pair nm-claude-only">
      <div><label class="nm-label">모델</label>
        <select class="nm-model">${[["", "기본(설정대로)"], ["opus", "Opus"], ["sonnet", "Sonnet"], ["haiku", "Haiku"], ["fable", "Fable"]]
          .map(([v, l]) => `<option value="${v}" ${v === (last.model ?? (S.settings || {}).new_default_model ?? "") ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div><label class="nm-label">effort</label>
        <select class="nm-model nm-effort">${NEW_EFFORTS.map(v => `<option value="${v}" ${v === ((S.settings || {}).new_default_effort || "high") ? "selected" : ""}>${v}</option>`).join("")}</select></div>
    </div>
    <label class="nm-check nm-claude-only" title="claude --worktree — 그 저장소의 .claude/worktrees/ 에 새 워크트리(새 브랜치)를 만들어 거기서 시작합니다. 끄면 고른 폴더에서 그대로"><input type="checkbox" class="nm-wt" checked> 새 워크트리로 시작 <span class="muted">claude -w</span></label>
    <div class="ctx-foot"><span class="muted nm-term-hint"><span class="nm-claude-only">보드 터미널에 띄웁니다 — 칸 머리 「⌨ 터미널」로 터미널을 엽니다</span><span class="nm-term-only">그 폴더에서 셸을 엽니다 — 칸 안이 터미널입니다</span><span class="nm-md-only">그 폴더의 .argus/boards/ 에 .md 로 저장합니다(git 에는 안 올라감)</span></span>
      <button class="btn primary" data-new-run>만들기</button></div>`;
  menu.hidden = false;
  placeMenu(menu, $("#newBtn"), "right");
  const cwd = menu.querySelector(".nm-cwd");
  menu.querySelector(".nm-prompt").focus();
  let f = { projects: [], recent: [] };
  try { f = await api("GET", "/api/folders"); } catch (e) { /* 후보 없이 직접 입력 */ }
  const row = (p, name, meta) => `<button type="button" class="nm-folder" data-path="${esc(p)}" title="${esc(p)}">
    <b>${esc(name)}</b><span class="muted">${esc(meta)}</span></button>`;
  menu.querySelector(".nm-folders").innerHTML =
    (f.projects.length ? `<div class="nm-sec">Orca 에 열린 프로젝트</div>` +
      f.projects.map(p => row(p.path, p.name, [p.branch, p.terminals ? `터미널 ${p.terminals}` : ""].filter(Boolean).join(" · "))).join("") : "") +
    (f.recent.length ? `<div class="nm-sec">최근 세션 폴더</div>` +
      f.recent.map(p => row(p, base(p), p.replace(/^\/Users\/[^/]+/, "~"))).join("") : "") ||
    '<div class="muted">후보가 없습니다 — 경로를 직접 입력하세요</div>';
  if (!cwd.value && f.projects.length) cwd.value = f.projects[0].path;
  markFolder(menu);
}
// 고른 폴더 줄 표시(입력칸 값과 같은 줄)
function markFolder(menu) {
  const v = menu.querySelector(".nm-cwd").value.trim();
  menu.querySelectorAll(".nm-folder").forEach(b => b.classList.toggle("on", b.dataset.path === v));
}
async function runNewSession() {
  const menu = $("#newMenu"), btn = menu.querySelector("[data-new-run]");
  const cwd = menu.querySelector(".nm-cwd").value.trim();
  const prompt = menu.querySelector(".nm-prompt").value.trim();
  const kind = (menu.querySelector(".nm-kind.on") || {}).dataset?.nmKind || "claude";
  if (kind !== "claude") return runNewBoard(kind, cwd, menu.querySelector(".nm-title").value.trim(), prompt);
  const model = menu.querySelector(".nm-model").value, effort = menu.querySelector(".nm-effort").value;
  const worktree = menu.querySelector(".nm-wt").checked;
  if (!cwd) { menu.querySelector(".nm-cwd").focus(); return; }
  if (btn.disabled) return;   // Enter 두 번 — 터미널이 두 개 뜨던 것
  btn.disabled = true;
  btn.textContent = "띄우는 중…";
  try {
    // 새 세션은 늘 보드 터미널에 뜬다(서버가 정한다)
    const r = await api("POST", "/api/sessions/new", { cwd, model, effort, worktree });
    try { localStorage.setItem(NEW_KEY, JSON.stringify({ cwd, model, effort, kind: "claude" })); } catch (e) { /* 저장소 없음 */ }
    menu.hidden = true;
    S.pendingNew = { handle: r.handle, prompt, at: Date.now(), sid: null, label: r.label || "보드 터미널" };
    if (menu.dataset.inbox) api("PATCH", "/api/inbox/" + menu.dataset.inbox, { status: "sent" }).catch(() => {});
    openLaunchTerm(r.handle, r.terminal);   // 칸이 생기기 전(폴더 신뢰 확인 창 등)에도 보이게 — 칸이 생기면 닫고 대화 화면으로
  } catch (e) {
    toast("새 세션 실패: " + e.message);
    btn.disabled = false;
    btn.textContent = "시작";
  }
}
// 띄운 세션의 칸이 생기면 그 칸으로 가고, 첫 메시지가 있으면 보낸다
async function watchNewSession() {
  const p = S.pendingNew;
  if (!p) return;
  const s = S.sessions.find(x => x.term_handle === p.handle && x.state !== "ended");
  if (!s) {
    if (Date.now() - p.at > 600000) {   // 창에서 확인 창을 직접 보고 있다 — 넉넉히
      S.pendingNew = null;
      toast(`새 세션이 아직 안 보입니다 — ${p.label} 을 확인하세요(폴더 신뢰 확인 창일 수 있습니다)`);
    }
    return;
  }
  if (!p.sid) {
    p.sid = s.session_id;
    closeLaunchTerm();
    // 칸은 대화 화면으로 연다(터미널은 칸 머리 「⌨ 터미널」로)
    if (TERM.open.delete(s.session_id)) { saveTermOpen(); S.uiVer++; }
    await focusCol(s.session_id);
  }
  if (!p.prompt) { S.pendingNew = null; return; }
  if (s.state !== "idle" && s.state !== "done") return;   // 시작이 끝나 입력을 받을 수 있을 때
  const text = p.prompt;
  S.pendingNew = null;
  try {
    const r = await api("POST", `/api/sessions/${s.session_id}/send`, { text, images: [] });
    toast("첫 메시지를 보냈습니다 · " + r.message);
  } catch (e) {
    fillComposer(s.session_id, text, []);
    toast("첫 메시지를 보내지 못해 입력창에 넣어 두었습니다: " + e.message);
  }
}
// 터미널·마크다운 칸 만들기. 마크다운은 첫 요청이 있으면 만들자마자 AI 에게 보낸다
async function runNewBoard(kind, cwd, title, prompt) {
  const menu = $("#newMenu"), btn = menu.querySelector("[data-new-run]");
  if (!cwd) { menu.querySelector(".nm-cwd").focus(); return; }
  if (btn.disabled) return;
  btn.disabled = true;
  btn.textContent = "만드는 중…";
  try {
    const body = { kind, cwd, title };
    const r = kind === "term" ? await termApi("POST", "/api/boards", body) : await api("POST", "/api/boards", body);
    let last = {};
    try { last = JSON.parse(localStorage.getItem(NEW_KEY) || "{}"); } catch (e) { /* 저장소 없음 */ }
    try { localStorage.setItem(NEW_KEY, JSON.stringify(Object.assign(last, { cwd, kind }))); } catch (e) { /* 저장소 없음 */ }
    menu.hidden = true;
    if (kind === "md" && prompt) {
      try { await api("POST", `/api/boards/${r.board.board_id}/ai`, { text: prompt }); } catch (e) { toast("첫 요청을 보내지 못했습니다: " + e.message); }
    }
    await poll();
    await focusCol(r.board.session_id);
    if (kind === "term") focusTermSoon(r.board.session_id);
    else if (!prompt) { const c = colEl(r.board.session_id), ta = c && c.querySelector(".md-edit"); if (ta && !ta.hidden) ta.focus(); }
  } catch (e) {
    toast("만들지 못했습니다: " + e.message);
    btn.disabled = false;
    btn.textContent = "만들기";
  }
}
$("#newBtn").addEventListener("click", openNewMenu);
$("#newMenu").addEventListener("click", e => {
  if (e.target.closest("[data-new-run]")) return runNewSession();
  const k = e.target.closest("[data-nm-kind]");
  if (k) {
    const menu = $("#newMenu");
    menu.querySelectorAll(".nm-kind").forEach(b => b.classList.toggle("on", b === k));
    menu.className = menu.className.replace(/\bnm-kind-\w+/g, "").trim() + " nm-kind-" + k.dataset.nmKind;
    (menu.querySelector(k.dataset.nmKind === "claude" ? ".nm-prompt" : ".nm-title") || {}).focus?.();
    return;
  }
  const f = e.target.closest(".nm-folder");
  if (f) {
    const menu = $("#newMenu");
    menu.querySelector(".nm-cwd").value = f.dataset.path;
    markFolder(menu);
    menu.querySelector(".nm-prompt").focus();
  }
});
$("#newMenu").addEventListener("input", e => { if (e.target.matches(".nm-cwd")) markFolder($("#newMenu")); });
$("#newMenu").addEventListener("keydown", e => {
  if (e.key !== "Enter" || e.shiftKey || imeBusy(e)) return;
  if (e.target.matches(".nm-cwd, .nm-prompt, .nm-title")) { e.preventDefault(); runNewSession(); }
});
$("#ctxMenu").addEventListener("click", e => { if (e.target.closest("[data-ctx-run]")) runCompact(); });
$("#ctxMenu").addEventListener("keydown", e => {
  if (e.target.classList.contains("ctx-note") && e.key === "Enter" && !e.shiftKey && !imeBusy(e)) {
    e.preventDefault();
    runCompact();
  }
});
// ------------------------------------------------------------------ 칸 머리·카드 끌어 놓기(고정 순서 바꾸기)
// 고정 영역 안에 놓으면 그 자리에 고정(이미 고정된 칸은 순서만 바뀜), 고정 칸을 바깥에 놓으면 고정을 푼다.
// 고정 안 한 칸끼리는 새 이벤트 순이라 자리를 정할 수 없다. 카드 보기(격자)에서도 같다 — 카드 자체를 끈다.
// 모양은 인박스 칸반과 같게: 끄는 것의 반투명 사본이 포인터를 따라오고, 놓일 자리에 강조된 빈자리가 생겨 나머지가 비켜 선다
const DRAG = { col: null, sid: null, x0: 0, y0: 0, on: false, x: 0, y: 0, target: null, raf: 0, cards: false, dx: 0, dy: 0 };
function boardCols() {
  return [...document.querySelectorAll("#board .col")].sort((a, b) => a.style.order - b.style.order);
}
function dragItems() {
  return DRAG.cards ? [...document.querySelectorAll("#cards .sc-grid > .scard")] : boardCols();
}
function itemSid(el) { return el.dataset.col || el.dataset.scard || el.dataset.scardOpen; }
function dragTarget(x, y) {
  // 포인터가 빈자리 위에 있으면 그대로(빈자리가 생기며 밀린 칸들 때문에 앞뒤로 떨리지 않게)
  const slot = $("#dragSlot");
  if (slot && DRAG.target) {
    const r = slot.getBoundingClientRect();
    if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return DRAG.target;
  }
  const others = dragItems().filter(c => c !== DRAG.col && itemSid(c));
  const sids = others.map(itemSid);
  const pinnedCount = sids.filter(id => DRAG.pinned.has(id)).length;
  let idx;
  if (DRAG.cards) {
    // 격자: 포인터에 가장 가까운 카드를 찾아 그 카드의 왼쪽 반이면 앞, 오른쪽 반이면 뒤
    let best = -1, bd = Infinity;
    others.forEach((c, i) => {
      const r = c.getBoundingClientRect();
      const dx = Math.max(r.left - x, 0, x - r.right), dy = Math.max(r.top - y, 0, y - r.bottom);
      const d = dx * dx + dy * dy * 4;   // 같은 줄을 더 가깝게
      if (d < bd) { bd = d; best = i; }
    });
    if (best < 0) idx = 0;
    else { const r = others[best].getBoundingClientRect(); idx = x < r.left + r.width / 2 ? best : best + 1; }
  } else {
    idx = others.findIndex(c => { const r = c.getBoundingClientRect(); return x < r.left + r.width / 2; });
    if (idx < 0) idx = others.length;
  }
  const kind = idx <= pinnedCount ? "pin" : DRAG.pinned.has(DRAG.sid) ? "unpin" : "none";
  return { idx, pinnedCount, sids, kind, others };
}
// 놓일 자리: 칸(카드) 하나 크기의 빈자리를 그 순서에 끼운다. 칸은 CSS order 로 줄 서므로 같은 order 를 주고 DOM 에서 바로 앞에 둔다
function placeSlot(t) {
  let slot = $("#dragSlot");
  if (!slot) {
    slot = document.createElement("div");
    slot.id = "dragSlot";
    slot.innerHTML = "<span></span>";
    if (!DRAG.cards) slot.style.flex = `0 0 ${DRAG.w}px`;   // 끄는 칸과 같은 너비(칸은 이미 숨겨져 있어 미리 재 둔 값)
  }
  // 클래스에 col·scard 를 쓰지 않는다 — 칸·카드 목록(#board .col)에 빈자리가 섞여 ID 없는 항목이 서버로 갔다
  slot.className = "drag-slot " + (DRAG.cards ? "slot-card " : "slot-col ") + t.kind;
  slot.querySelector("span").textContent = t.kind === "pin" ? (t.idx + 1) + "번에 고정" : t.kind === "unpin" ? "고정 풀기" : "고정 영역에만 놓을 수 있어요";
  if (slot.dataset.idx === String(t.idx) && slot.isConnected) return;
  slot.dataset.idx = t.idx;
  const before = t.others[t.idx], last = t.others[t.others.length - 1];
  if (before) before.before(slot);
  else if (last) last.after(slot);
  else DRAG.col.after(slot);
  if (!DRAG.cards) slot.style.order = before ? before.style.order : (+(last && last.style.order) || 0) + 1;
}
function dragLoop() {
  if (!DRAG.on) return;
  const ghost = $("#dragGhost");
  if (ghost) ghost.style.transform = `translate(${DRAG.x - DRAG.dx}px, ${DRAG.y - DRAG.dy}px)`;
  // 가장자리에 가까이 대면 굴린다(칸은 옆으로, 카드는 위아래로)
  const b = DRAG.cards ? $("#cards") : $("#board"), br = b.getBoundingClientRect();
  if (DRAG.cards) {
    if (DRAG.y < br.top + 60) b.scrollTop -= 16;
    else if (DRAG.y > br.bottom - 60) b.scrollTop += 16;
  } else if (DRAG.x < br.left + 70) b.scrollLeft -= 18;
  else if (DRAG.x > br.right - 70) b.scrollLeft += 18;
  DRAG.target = dragTarget(DRAG.x, DRAG.y);
  placeSlot(DRAG.target);
  DRAG.raf = requestAnimationFrame(dragLoop);
}
// 포인터를 따라오는 반투명 사본: 카드는 카드 그대로, 칸은 머리(제목·상태)만
function makeGhost(el) {
  const head = DRAG.cards ? null : el.querySelector(".col-head");
  const r = (head || el).getBoundingClientRect();
  DRAG.w = el.getBoundingClientRect().width;
  // 칸은 껍데기(.col — 클래스·색 변수 유지)만 복사해 머리만 담는다. 대화 전체를 복사하면 무겁다
  const g = head ? el.cloneNode(false) : el.cloneNode(true);
  if (head) g.appendChild(head.cloneNode(true));
  g.id = "dragGhost";
  g.classList.add("drag-ghost");
  g.classList.remove("dragging", "active", "flash");
  for (const k of ["data-col", "data-scard"]) g.removeAttribute(k);   // 사본이 칸·카드 찾기에 걸리지 않게
  Object.assign(g.style, { width: r.width + "px", height: r.height + "px", left: 0, top: 0, flex: "none", order: "" });
  DRAG.dx = DRAG.x0 - r.left;
  DRAG.dy = DRAG.y0 - r.top;
  g.style.transform = `translate(${DRAG.x - DRAG.dx}px, ${DRAG.y - DRAG.dy}px)`;
  document.body.appendChild(g);
}
document.addEventListener("pointerdown", e => {
  if (e.button !== 0) return;
  if (e.target.closest("button, a, input, textarea, select, [contenteditable=true]")) return;
  const head = e.target.closest("#board .col-head");
  const card = !head && e.target.closest("#cards .sc-grid > .scard[data-scard]");   // 펼친 카드는 그 안에서 대화하므로 끌지 않는다
  if (!head && !card) return;
  const el = head ? head.closest(".col") : card;
  if (head && S.renaming === el.dataset.col) return;
  Object.assign(DRAG, { col: el, sid: itemSid(el), x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, on: false, target: null, cards: !!card });
});
document.addEventListener("pointermove", e => {
  if (!DRAG.col) return;
  DRAG.x = e.clientX;
  DRAG.y = e.clientY;
  if (!DRAG.on) {
    if (Math.hypot(e.clientX - DRAG.x0, e.clientY - DRAG.y0) < 8) return;
    DRAG.on = true;
    S.dragging = true;
    DRAG.pinned = new Set(boardSessions().filter(pinOf).map(s => s.session_id));   // 칸에 선 세션 기준(가족의 고정)   // 끄는 동안 매 프레임 찾지 않게 한 번만
    document.body.classList.add("col-dragging");
    makeGhost(DRAG.col);
    DRAG.col.classList.add("dragging");   // 원래 자리는 비우고(숨김) 빈자리가 대신 선다
    const sel = document.getSelection();
    if (sel) sel.removeAllRanges();
    dragLoop();
  }
});
function endDrag(cancel, noClick) {
  const { on, target, sid, col, cards } = DRAG;
  cancelAnimationFrame(DRAG.raf);
  Object.assign(DRAG, { col: null, sid: null, on: false, target: null, cards: false });
  if (!on) return;
  S.dragging = false;
  document.body.classList.remove("col-dragging");
  col.classList.remove("dragging");
  for (const id of ["dragSlot", "dragGhost"]) { const n = document.getElementById(id); if (n) n.remove(); }
  // 놓은 직후(또는 Esc 로 취소한 뒤 버튼을 뗄 때) 따라오는 click(확인 처리·카드 펼치기 등)은 먹는다.
  // 창을 떠나거나(blur) 포인터가 취소되면 따라오는 click 이 없으니 걸지 않는다 — 걸면 다음 정상 클릭 하나가 먹힌다
  if (!noClick) window.addEventListener("click", ev => { ev.stopPropagation(); ev.preventDefault(); }, { capture: true, once: true });
  if (cancel || !target || target.kind === "none") {
    if (!cancel && target && target.kind === "none") toast("고정 안 한 칸은 새 이벤트 순이라 자리를 정할 수 없습니다 — 고정 영역으로 끌어 놓으면 고정됩니다");
    renderBoard();
    if (cards) renderCards(true);
    return;
  }
  // 칸에 선 세션(팀원·갈래 탭일 수 있다) 대신 그 칸의 고정을 가진 세션으로 — 새로 고정하면 가족의 원본에
  const holder = id => pinHolders(id)[0] || pinRoot(id);
  const pinned = target.sids.slice(0, target.pinnedCount).map(holder);
  const body = { order: pinned, unpin: [] };
  if (target.kind === "pin") pinned.splice(target.idx, 0, holder(sid));
  else body.unpin.push(...pinHolders(sid));
  const unpin = new Set(body.unpin);
  // 다음 폴링 전에도 바로 자리 이동
  S.sessions.forEach(s => {
    const i = pinned.indexOf(s.session_id);
    if (i >= 0) s.pinned = i + 1;
    else if (unpin.has(s.session_id)) s.pinned = 0;
  });
  renderBoard();
  if (cards) renderCards(true);
  api("POST", "/api/pins", body)
    .then(() => toast(target.kind === "pin" ? `${target.idx + 1}번 자리에 고정했습니다` : "고정을 풀었습니다"))
    .catch(err => toast(err.message))
    .finally(refresh);
}
document.addEventListener("pointerup", () => endDrag(false));
document.addEventListener("pointercancel", () => endDrag(true, true));
window.addEventListener("blur", () => endDrag(true, true));

// ------------------------------------------------------------------ 칸 너비 조절(오른쪽 가장자리 끌기)
// 칸마다 브라우저에 기억한다. 더블클릭이나 ⋮ → 「기본 너비로」면 원래대로
const COL_MIN = 320, COL_MAX = 1600;
// 너비는 가족(원본·갈래·팀원) 단위로 — 팀원·갈래 탭으로 바꿔도 칸 너비가 그대로. 예전에 갈래 id 로 남은 값도 읽는다
const SESS_IDX = { has: id => !!sessionById(id), get: sessionById };
function widthKey(sid) { const s = sessionById(sid); return s ? famRootOf(s, SESS_IDX) : sid; }
function colWidthOf(sid) { return S.colWidths[widthKey(sid)] || S.colWidths[sid] || 0; }
function applyColWidth(col, sid) {
  const w = colWidthOf(sid);
  const flex = w ? `0 0 ${w}px` : "";
  if (col.style.flex !== flex) col.style.flex = flex;
  col.classList.toggle("custom-width", !!w);
}
function setColWidth(sid, w) {
  const key = widthKey(sid);
  if (key !== sid) delete S.colWidths[sid];
  if (w) S.colWidths[key] = Math.round(w); else delete S.colWidths[key];
  save("csl.colWidths", S.colWidths);
  const col = colEl(sid);
  if (col) applyColWidth(col, sid);
  if (!w) toast("기본 너비로 되돌렸습니다");
}
const RESIZE = { sid: null, col: null, x0: 0, w0: 0 };
document.addEventListener("pointerdown", e => {
  const h = e.target.closest && e.target.closest(".col-resize");
  if (!h || e.button !== 0) return;
  e.preventDefault();
  const col = h.closest(".col");
  Object.assign(RESIZE, { sid: h.dataset.resizeSid, col, x0: e.clientX, w0: col.getBoundingClientRect().width });
  h.setPointerCapture(e.pointerId);
  document.body.classList.add("col-resizing");
});
document.addEventListener("pointermove", e => {
  if (!RESIZE.col) return;
  const w = Math.max(COL_MIN, Math.min(COL_MAX, RESIZE.w0 + e.clientX - RESIZE.x0));
  RESIZE.col.style.flex = `0 0 ${w}px`;
});
function endResize() {
  if (!RESIZE.col) return;
  const w = RESIZE.col.getBoundingClientRect().width;
  const sid = RESIZE.sid;
  Object.assign(RESIZE, { sid: null, col: null });
  document.body.classList.remove("col-resizing");
  setColWidth(sid, w);
}
document.addEventListener("pointerup", endResize);
document.addEventListener("pointercancel", endResize);
document.addEventListener("dblclick", e => {
  const h = e.target.closest && e.target.closest(".col-resize");
  if (h) setColWidth(h.dataset.resizeSid, null);
});

// 브레인 패널 너비: 오른쪽 가장자리를 끌어 조절(브라우저에 기억), 더블클릭이면 기본 너비
const BP_MIN = 320;
function applyBrainWidth(w) {
  const layout = $("#layout");
  if (w) layout.style.setProperty("--bp-w", Math.round(w) + "px"); else layout.style.removeProperty("--bp-w");
}
applyBrainWidth(load("csl.brainWidth", null));
const BPR = { on: false, x0: 0, w0: 0 };
$("#bpResize").addEventListener("pointerdown", e => {
  if (e.button !== 0) return;
  e.preventDefault();
  Object.assign(BPR, { on: true, x0: e.clientX, w0: $("#brainPanel").getBoundingClientRect().width });
  e.target.setPointerCapture(e.pointerId);
  document.body.classList.add("bp-resizing");
});
$("#bpResize").addEventListener("pointermove", e => {
  if (!BPR.on) return;
  applyBrainWidth(Math.max(BP_MIN, Math.min(window.innerWidth - 360, BPR.w0 + e.clientX - BPR.x0)));
});
function endBrainResize() {
  if (!BPR.on) return;
  BPR.on = false;
  document.body.classList.remove("bp-resizing");
  save("csl.brainWidth", Math.round($("#brainPanel").getBoundingClientRect().width));
}
$("#bpResize").addEventListener("pointerup", endBrainResize);
$("#bpResize").addEventListener("pointercancel", endBrainResize);
$("#bpResize").addEventListener("dblclick", () => {
  applyBrainWidth(null);
  save("csl.brainWidth", null);
  toast("브레인 패널을 기본 너비로 되돌렸습니다");
});

// 넥스트 액션 접기·펴기(칸마다, 브라우저에 기억). 접어도 개수(✓ N)와 recap 은 보인다
function toggleNotes(sid) {
  S.notesFolded.has(sid) ? S.notesFolded.delete(sid) : S.notesFolded.add(sid);
  save("csl.notesFolded", [...S.notesFolded]);
  S.uiVer++;
  renderBoard();
}
// ------------------------------------------------------------------ 칸 안 세로 이동(키보드)
// 넥스트 액션 입력칸 ↕ 할 일(위→아래) ↕ 칸 선택 ↕ 채팅 입력창. 할 일에서 Space 완료, Delete 삭제, Enter 입력창에 넣기
// 마우스로 다른 곳을 누르면 키보드로 고르던 할 일은 잊는다(다시 그릴 때 포커스를 되가져오지 않게)
document.addEventListener("pointerdown", e => { if (!e.target.closest("li.note")) S.noteFocus = null; }, true);
// 같은 세션 칸이 칸 보기(카드 보기일 땐 숨김)와 펼친 카드에 함께 있을 수 있다 — 화면에 보이는 것을 먼저 고른다
function visibleFirst(selector) {
  const all = [...document.querySelectorAll(selector)];
  return all.find(el => el.offsetParent !== null) || all[0] || null;
}
function colEl(sid) { return sid ? visibleFirst(`.col[data-col="${CSS.escape(sid)}"]`) : null; }
function noteItems(col) { return col ? [...col.querySelectorAll(".cf-notes li.note")] : []; }
function focusNote(li) {
  S.noteFocus = li.dataset.nid;
  li.focus({ preventScroll: true });
  li.scrollIntoView({ block: "nearest" });
}
function toColumnState(sid) {
  S.noteFocus = null;
  const a = document.activeElement;
  if (a && a !== document.body) a.blur();
  setActive(sid);
}
function goUpFromColumn(sid) {
  if (!sid) return;
  if (S.notesFolded.has(sid)) toggleNotes(sid);   // 접혀 있으면 펴고 들어간다
  const col = setActive(sid, { scroll: true });
  const items = noteItems(col);
  if (items.length) return focusNote(items[items.length - 1]);
  const input = col && col.querySelector(".note-input");
  if (input) input.focus({ preventScroll: true });
}
function noteKey(e) {
  const li = e.target;
  // ⌘↵ / Ctrl+↵: 「응답 끝나면 보내기」 예약·풀기(Enter 는 입력창에 넣기만)
  if (li.matches && li.matches("li.note") && e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.altKey && !e.isComposing) {
    e.preventDefault();
    const q = li.querySelector(".note-q");
    if (!q) { toast(li.classList.contains("done") ? "완료한 할 일입니다" : "세션에 붙은 할 일만 예약할 수 있습니다"); return true; }
    S.noteFocus = li.dataset.nid;   // 다시 그린 뒤에도 그 할 일에 머문다
    q.click();
    return true;
  }
  // ⌥↑ / ⌥↓: 예약한 할 일의 보낼 순서 바꾸기
  if (li.matches && li.matches("li.note.queued") && e.altKey && !e.metaKey && !e.ctrlKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
    e.preventDefault();
    S.noteFocus = li.dataset.nid;
    moveQueued(li.closest(".col").dataset.col, +li.dataset.nid, e.key === "ArrowUp" ? -1 : 1);
    return true;
  }
  if (!li.matches || !li.matches("li.note") || e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return false;
  const col = li.closest(".col"), sid = col.dataset.col, items = noteItems(col), i = items.indexOf(li);
  const nid = li.dataset.nid;
  const key = (!e.shiftKey && VIM_KEYS[e.code]) || e.key;   // vim: j k ↓↑ · h l ← →
  // E · F2: 고치기(더블클릭과 같음) — 키 자리(e.code)로 봐서 한글 자판에서도. 끝나면 그 할 일이 다시 골라진 채로
  if ((e.code === "KeyE" && !e.shiftKey) || e.key === "F2") {
    e.preventDefault();
    S.noteFocus = nid;
    startEdit(li.querySelector(".note-text"));
    return true;
  }
  switch (key) {
    case "ArrowUp":
      e.preventDefault();
      if (i > 0) focusNote(items[i - 1]);
      else { S.noteFocus = null; col.querySelector(".note-input").focus({ preventScroll: true }); }
      return true;
    case "ArrowDown":
      e.preventDefault();
      if (i < items.length - 1) focusNote(items[i + 1]); else toColumnState(sid);
      return true;
    case " ":
      e.preventDefault();
      api("PATCH", "/api/notes/" + nid, { done: !li.classList.contains("done") }).then(refresh).catch(err => toast(err.message));
      return true;
    case "Delete":
    case "Backspace": {
      e.preventDefault();
      // 실수로 지우지 않게 한 번 더 눌러야 지운다(2초 안에)
      if (!li.classList.contains("confirm-del")) {
        li.classList.add("confirm-del");
        toast("한 번 더 누르면 이 할 일을 지웁니다");
        clearTimeout(li._delTimer);
        li._delTimer = setTimeout(() => li.classList.remove("confirm-del"), 2000);
        return true;
      }
      const next = items[i + 1] || items[i - 1];
      if (next) S.noteFocus = next.dataset.nid; else toColumnState(sid);
      api("DELETE", "/api/notes/" + nid).then(() => { toast("할 일을 지웠습니다"); return refresh(sid); }).catch(err => toast(err.message));
      return true;
    }
    case "Enter":
      e.preventDefault();
      if (li.classList.contains("done")) { toast("완료한 할 일입니다 — Space 로 완료를 풀면 넣을 수 있습니다"); return true; }
      S.noteFocus = null;
      fillComposer(sid, noteRaw(li.querySelector(".note-text")), noteImages(findNote(nid) || {}));
      return true;
    case "Escape":
      e.preventDefault();
      toColumnState(sid);
      return true;
    case "ArrowLeft":
    case "ArrowRight":
      toColumnState(sid);   // 칸 선택 상태로 돌린 뒤 ← → 칸 이동은 아래 공통 처리에 맡긴다
      return false;
  }
  return false;
}
async function togglePin(sid) {
  const s = sessionById(sid);
  if (!s) return;
  const pin = !pinOf(s);
  // 가족 단위: 고정은 원본에 걸고, 풀 때는 가족 중 고정된 세션을 모두 푼다
  for (const id of pin ? [pinRoot(sid)] : pinHolders(sid)) {
    await api("POST", `/api/sessions/${id}/pin`, { pinned: pin });
    sessionById(id).pinned = pin ? Date.now() / 1000 : 0;   // 다음 폴링 전에도 바로 자리 이동
  }
  toast(pin ? "맨 앞에 고정했습니다" : "고정을 풀었습니다");
  return refresh(sid);
}
function renderRemoteBtn() {
  const on = !!S.settings.auto_remote;
  const b = $("#remoteBtn");
  const label = on ? "📡 자동 리모트 켜짐" : "📡 자동 리모트 꺼짐";
  if (b.textContent !== label) b.textContent = label;
  b.classList.toggle("on", on);
}
$("#remoteBtn").addEventListener("click", async () => {
  const on = !S.settings.auto_remote;
  await api("POST", "/api/settings", { auto_remote: on });
  S.settings.auto_remote = on;
  renderRemoteBtn();
  toast(on ? "보드에서 대화를 보낸 세션은 리모트 컨트롤이 꺼져 있으면 켭니다" : "자동 리모트 컨트롤을 껐습니다");
});

// ------------------------------------------------------------------ 사용량(계정 전체: 5시간·주간)
function untilText(ts) {
  const d = ts - Date.now() / 1000;
  if (!ts || d <= 0) return "";
  if (d < 3600) return Math.ceil(d / 60) + "분 후";
  if (d < 86400) return Math.floor(d / 3600) + "시간 " + Math.floor((d % 3600) / 60) + "분 후";
  return Math.floor(d / 86400) + "일 " + Math.floor((d % 86400) / 3600) + "시간 후";
}
// 초기화 시각: 오늘이면 「15:40」, 내일 이후면 「일 10:00」(요일)
function resetAt(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000), today = new Date();
  const wd = d.toLocaleDateString(LANG === "ko" ? "ko-KR" : LANG, { weekday: "short" });
  return (d.toDateString() === today.toDateString() ? "" : wd + " ") + hm(ts);
}
// 권장 속도: 창(5시간·7일) 중 지나간 시간 비율만큼 쓰면 끝날 때 딱 100%. 막대 위 눈금과 「권장 N%」로 보인다
const USAGE_WINDOW = { five: 5 * 3600, week: 7 * 86400 };
function pacePct(reset, windowSec) {
  if (!reset) return null;
  const left = reset - Date.now() / 1000;
  if (left <= 0 || left > windowSec) return null;
  return Math.round((1 - left / windowSec) * 100);
}
function renderUsage(u) {
  const el = $("#usage");
  if (!u || (u.five_hour == null && u.seven_day == null)) { el.hidden = true; return; }
  const part = (label, pct, reset, windowSec) => {
    if (pct == null) return "";
    const pace = pacePct(reset, windowSec);
    const ahead = pace != null && pct > pace + 5;
    // 색은 권장 속도 기준: 권장 안쪽 초록, 권장보다 빠르면 주황, 80% 넘으면 빨강(권장 정보가 없으면 50%부터 주황)
    const lv = pct >= 80 ? "hot" : (pace != null ? ahead : pct >= 50) ? "warm" : "";
    const perDay = windowSec > 86400 ? " · 하루 평균 " + (100 / 7).toFixed(1) + "%" : " · 1시간 평균 20%";
    const tip = `${label} 사용량 ${Math.round(pct)}%` + (reset ? ` · ${untilText(reset)} 초기화` : "") +
      (pace == null ? "" : `\n권장 ${pace}% — 지금까지 지난 시간만큼 고르게 썼을 때의 양${perDay}` +
        (ahead ? `\n권장보다 ${Math.round(pct - pace)}%p 빠릅니다` : pct < pace ? `\n권장보다 ${Math.round(pace - pct)}%p 여유` : ""));
    // 이름은 막대 위에 작게, 권장·초기화 시각은 마우스를 올렸을 때(title)만 — 상단 막대 자리를 적게 쓴다
    return `<span class="u-item ${lv} ${ahead ? "ahead" : ""}" title="${esc(tip)}">
      <span class="u-stack"><span class="u-label">${label}</span>
      <span class="u-bar">${pace != null ? `<span class="u-pace" style="width:${pace}%"></span>` : ""}<i style="width:${Math.min(100, pct)}%"></i>${pace != null && pct > pace ? `<span class="u-notch" style="left:${pace}%"></span>` : ""}</span></span>
      <b>${Math.round(pct)}%</b>${reset ? `<span class="u-at">↻ ${resetAt(reset)}</span>` : ""}</span>`;
  };
  const html = part("5시간", u.five_hour, u.five_hour_reset, USAGE_WINDOW.five) +
    part("주간", u.seven_day, u.seven_day_reset, USAGE_WINDOW.week);
  el.hidden = false;
  if (el._html !== html) { el._html = html; el.innerHTML = html; }
}

// ------------------------------------------------------------------ 세션 모델·effort
const MODEL_CHOICES = ["Opus 5.5", "Fable 5.1", "Sonnet 5.5", "Haiku 5.5"];
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
// claude-sonnet-5-5[1m] → Sonnet 5.5 (보드에서 바꾼 직후엔 이미 「Sonnet 5.5」 형태)
function modelName(m) {
  if (!m) return "";
  const id = m.replace(/\[.*\]$/, "");
  const g = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(id);
  if (!g) return m;
  return g[1][0].toUpperCase() + g[1].slice(1) + " " + g[2] + (g[3] ? "." + g[3] : "");
}
function modelChip(s, d) {
  if (!s.model && !s.effort) return "";
  const label = [modelName(s.model), s.effort].filter(Boolean).join(" · ");
  const can = d !== "ended";
  return `<button class="model-chip" ${can ? `data-action="model-menu" data-sid="${esc(s.session_id)}"` : "disabled"}
    title="이 세션의 모델·effort${can ? " — 눌러서 바꾸기(이 세션만)" : ""}">${esc(label)}${can ? " ▾" : ""}</button>`;
}
function openModelMenu(btn) {
  const sid = btn.dataset.sid;
  const s = sessionById(sid);
  if (!s) return;
  const menu = $("#modelMenu");
  const cur = modelName(s.model);
  menu.dataset.sid = sid;
  menu._pick = { model: MODEL_CHOICES.includes(cur) ? cur : null, effort: EFFORTS.includes(s.effort) ? s.effort : null };
  const busy = !["idle", "done", "error"].includes(s.state);
  const draw = () => {
    const p = menu._pick;
    menu.innerHTML = `<div class="mm-title">모델 <span class="muted">지금: ${esc(cur || "알 수 없음")}</span></div>
      <div class="mm-row">${MODEL_CHOICES.map(m => `<button class="mm-opt ${p.model === m ? "on" : ""}" data-mm-model="${m}">${m}</button>`).join("")}</div>
      <div class="mm-title">effort <span class="muted">지금: ${esc(s.effort || "알 수 없음")}</span></div>
      <div class="mm-row">${EFFORTS.map(e => `<button class="mm-opt ${p.effort === e ? "on" : ""}" data-mm-effort="${e}">${e}</button>`).join("")}</div>
      <div class="mm-foot">
        <span class="muted">${busy ? "작업 중에는 바꿀 수 없습니다" : "이 세션에만 적용 · 기본값은 그대로"}</span>
        <button class="btn primary" data-mm-apply ${busy ? "disabled" : ""}>적용</button>
      </div>`;
  };
  draw();
  menu._draw = draw;
  const r = btn.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(r.left, innerWidth - 380)) + "px";
  menu.style.top = (r.bottom + 6) + "px";
  menu.hidden = false;
}
$("#modelMenu").addEventListener("click", async e => {
  const menu = $("#modelMenu");
  const m = e.target.closest("[data-mm-model]"), ef = e.target.closest("[data-mm-effort]");
  if (m) { menu._pick.model = m.dataset.mmModel; return menu._draw(); }
  if (ef) { menu._pick.effort = ef.dataset.mmEffort; return menu._draw(); }
  const apply = e.target.closest("[data-mm-apply]");
  if (!apply) return;
  const sid = menu.dataset.sid;
  const s = sessionById(sid) || {};
  const body = {};
  if (menu._pick.model && menu._pick.model !== modelName(s.model)) body.model = menu._pick.model;
  if (menu._pick.effort && menu._pick.effort !== s.effort) body.effort = menu._pick.effort;
  if (!body.model && !body.effort) { menu.hidden = true; return toast("바뀐 것이 없습니다"); }
  apply.disabled = true;
  apply.textContent = "바꾸는 중…";
  try {
    const r = await api("POST", `/api/sessions/${sid}/model`, body);
    toast(r.message);
    menu.hidden = true;
    await refresh(sid);
  } catch (err) {
    toast(err.message);
    apply.disabled = false;
    apply.textContent = "적용";
  }
});

// ------------------------------------------------------------------ 하루 정리(작업 일지)
// 그날 모든 세션의 요청·응답 요약을 서버가 claude -p(세션 기록 안 남김)로 정리해 날짜별로 저장한다
const DL = { day: null, days: [], timer: null };
function todayStr() {
  const d = new Date();
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}
async function openDaily() {
  $("#daily").hidden = false;
  $("#dailyAuto").checked = S.settings.auto_daily !== false;
  await loadDays();
  await showDay(DL.day || (DL.days[0] && DL.days[0].day) || todayStr());
}
function closeDaily() {
  $("#daily").hidden = true;
  clearTimeout(DL.timer);
}
async function loadDays() {
  DL.days = (await api("GET", "/api/daily")).days;
  renderDays();
}
function renderDays() {
  const label = d => {
    const [y, m, dd] = d.split("-").map(Number);
    const dt = new Date(y, m - 1, dd);
    return dateLabel(dt.getTime() / 1000) + (d === todayStr() || /어제/.test(dateLabel(dt.getTime() / 1000)) ? " · " + m + "/" + dd : "");
  };
  $("#dailyDays").innerHTML = '<div class="dd-title">날짜</div>' + (DL.days.map(d => `
    <button class="dd-item ${d.day === DL.day ? "on" : ""}" data-day="${d.day}">
      <span class="dd-date">${esc(label(d.day))}</span>
      <span class="dd-meta">세션 ${d.sessions} · ${d.turns}턴</span>
      <span class="dd-status ${d.status || ""}">${d.status === "done" ? "✓ 정리됨" : d.status === "running" ? "정리 중…" : d.status === "error" ? "실패" : ""}</span>
    </button>`).join("") || '<div class="muted" style="padding:8px">기록된 날이 없습니다</div>');
}
async function showDay(day) {
  DL.day = day;
  renderDays();
  clearTimeout(DL.timer);
  let rep;
  try {
    rep = await api("GET", "/api/daily?day=" + day);
  } catch (e) {
    if (DL.day !== day) return;
    // 정리 중 지켜보다 한 번 실패해도 멈춘 채(버튼 잠김) 남지 않게 다시 본다
    $("#dailyMeta").textContent = "불러오지 못했습니다(다시 시도합니다): " + e.message;
    DL.timer = setTimeout(() => { if (!$("#daily").hidden && DL.day === day) showDay(day).then(loadDays); }, 5000);
    return;
  }
  if (DL.day !== day) return;   // 그사이 다른 날을 골랐다 — 늦게 온 응답이 그 날 화면을 덮지 않게
  const info = DL.days.find(d => d.day === day);
  $("#dailyTitle").textContent = day + " 작업 일지";
  const run = $("#dailyRun");
  const copyBtn = $("#dailyCopy");
  copyBtn.hidden = !rep.content;
  if (rep.status === "running") {
    run.disabled = true;
    run.textContent = "정리하는 중…";
    $("#dailyMeta").textContent = "세션 " + (info ? info.sessions : "?") + "개 · 보통 30초~1분 걸립니다";
    if (!rep.content) $("#dailyBody").innerHTML = '<div class="daily-wait"><span class="dot working"></span> 그날의 요청·응답을 모아 정리하고 있습니다…</div>';
    DL.timer = setTimeout(() => { if (!$("#daily").hidden && DL.day === day) showDay(day).then(loadDays); }, 2500);
    return;
  }
  run.disabled = !info;
  run.textContent = rep.content ? "다시 정리" : "정리하기";
  $("#dailyMeta").textContent = rep.content
    ? `${rep.model || ""} · ${rep.created_at ? dateLabel(rep.created_at) + " " + hm(rep.created_at) + " 정리" : ""} · 세션 ${rep.sessions}개 · ${rep.turns}턴`
    : info ? `세션 ${info.sessions}개 · ${info.turns}턴` : "기록된 대화가 없는 날입니다";
  let body = rep.content ? mdCached(rep.content) : "";
  if (rep.status === "error") body = `<div class="daily-error">정리하지 못했습니다: ${esc(rep.error || "")}</div>` + body;
  if (!body) body = '<div class="daily-wait">아직 정리하지 않은 날입니다. 「정리하기」를 누르면 그날 모든 세션의 요청·응답을 모아 Claude 가 작업 일지로 정리합니다. 세션 기록은 남기지 않고, 계정 사용량을 조금 씁니다.</div>';
  $("#dailyBody").innerHTML = body;
  $("#dailyBody")._content = rep.content || "";
}
$("#dailyBtn").addEventListener("click", openDaily);
$("#dailyAuto").addEventListener("change", async e => {
  await api("POST", "/api/settings", { auto_daily: e.target.checked });
  S.settings.auto_daily = e.target.checked;
  toast(e.target.checked ? "매일 아침 6시가 지나면 전날 일지를 자동으로 정리합니다" : "자동 정리를 껐습니다");
});
$("#daily").addEventListener("click", async e => {
  if (e.target === $("#daily") || e.target.closest("[data-daily-close]")) return closeDaily();
  const item = e.target.closest("[data-day]");
  if (item) return showDay(item.dataset.day);
});
$("#dailyRun").addEventListener("click", async () => {
  if (!DL.day) return;
  await api("POST", "/api/daily", { day: DL.day });
  await loadDays();
  await showDay(DL.day);
});
$("#dailyCopy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#dailyBody")._content || "");
    toast("일지를 마크다운으로 복사했습니다");
  } catch (e) {
    toast("복사하지 못했습니다");
  }
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !$("#daily").hidden) { e.stopPropagation(); closeDaily(); }
}, true);

// ------------------------------------------------------------------ 세션 이름 바꾸기
function renameBtn(sid, d) {
  return d === "ended" ? "" : `<button class="rename-btn" data-action="rename" data-sid="${esc(sid)}" title="세션 이름 바꾸기(/rename)">✎</button>`;
}
function startRename(btn) {
  const sid = btn.dataset.sid;
  const line = btn.closest(".title-line");
  const s = sessionById(sid) || {};
  const current = sessionTitle(s);
  S.renaming = sid;
  line.innerHTML = `<input class="rename-input" maxlength="100" value="${esc(s.title || "")}" placeholder="${esc(current)}">`;
  const input = line.querySelector("input");
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    if (commit && title && title !== s.title) {
      // 서버가 터미널에 /rename 을 보내고 확인하는 데 몇 초 걸린다 — 그동안 입력칸이 그대로면 Enter 가 안 먹은 것처럼 보여
      // 바로 새 이름으로 바꿔 보이고 「저장 중…」을 붙인다(입력칸이 빠지며 오는 blur 는 done 으로 막힌다)
      line.innerHTML = `<span class="col-title title-text">${esc(title)}</span><span class="muted rename-saving">저장 중…</span>`;
      const live = sessionById(sid);
      if (live) live.title = title;
      try {   // 저장이 끝날 때까지 S.renaming 을 두어 폴링이 머리를 옛 이름으로 다시 그리지 않게
        const r = await api("POST", `/api/sessions/${sid}/rename`, { title });
        toast(r.message);
      } catch (err) {
        toast(err.message);
      }
    }
    S.renaming = null;
    // 머리 부분을 강제로 다시 그리게
    document.querySelectorAll(".col-head").forEach(el => { el._html = null; });
    await refresh(sid);
  };
  input.addEventListener("keydown", e => {
    if (imeBusy(e)) return;
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

// ------------------------------------------------------------------ 보내기(Orca terminal send)
function composerHtml(sid) {
  return `<div class="composer" data-sid="${esc(sid)}" data-drop-sid="${esc(sid)}">
    <div class="slash-pop" hidden></div>
    <div class="cmp-images"></div>
    <div class="cmp-box">
      <button type="button" class="cmp-attach" data-action="cmp-attach" title="이미지 첨부(붙여넣기·끌어다 놓기도 됨)">＋</button>
      <textarea class="cmp-text" rows="1" placeholder="${CMP_PLACEHOLDER}"></textarea>
      <div class="cmp-rich md" contenteditable="true" hidden title="서식 모드 — 붙여넣은 서식이 보이는 그대로. 보낼 때 마크다운으로 바뀝니다 · Enter 전송 · Shift+Enter 줄바꿈(빈 목록 항목에선 목록 나가기, 코드·인용 끝에서 두 번이면 블록 밖으로) · 목록·인용·코드 맨 앞 Backspace 서식 풀기 · Tab/⇧Tab 목록 들여쓰기 · ⌘\\ 서식 끄기(마크다운 글로)"></div>
      <button type="button" class="cmp-stop" data-action="cmp-stop" data-sid="${esc(sid)}" title="Claude 작업 중단(터미널의 Esc)" hidden>■</button>
      <span class="cmp-sendgrp"><button type="button" class="cmp-send" data-action="cmp-send" title="보내기 ( Enter )">↑</button><button type="button" class="cmp-more" data-action="cmp-more" title="보내기 방법 — 응답이 끝나면 보내기 · 넥스트 액션에만 넣기" aria-haspopup="true"><svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M3.5 6 8 10.5 12.5 6" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg></button></span>
    </div>
    <div class="cmp-hint"></div>
    <input class="cmp-file" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden>
  </div>`;
}
function restoreComposerDraft(el) {
  const sid = el.dataset.sid;
  const ta = el.querySelector(".cmp-text");
  ta.value = S.drafts.get(sid) || "";
  autoGrow(ta);
  const rich = S.richDrafts && S.richDrafts.get(sid);
  if (rich) {   // 서식 모드로 쓰던 글이면 서식 그대로 되살린다
    ta.hidden = true;
    const r = richOf(el);
    r.innerHTML = rich;
    r.hidden = false;
    el.classList.add("rich");
  }
  renderComposerImages(sid);
}
const CMP_PLACEHOLDER = "Claude 에게 보내기 · Enter 전송 · Shift+Enter 줄바꿈";
function suggestionFor(s) {
  const d = displayState(s);
  if (d === "working" || d === "waiting" || d === "ended" || s.compact_at) return null;
  const g = S.sugg.get(s.session_id);
  return g && g.key === s.state_at ? g.text : null;
}
// ------------------------------------------------------------------ 질문 창(AskUserQuestion) 보드에서 답하기
// 훅이 남긴 질문·선택지를 입력창 위 카드로 그린다. 보내면 서버가 그 터미널 질문 창에 키를 넣는다(화면을 확인하며).
// 질문이 바뀔 때만 다시 그려 고르던 것·쓰던 글이 지워지지 않게 한다
// ------------------------------------------------------------------ 터미널의 선택 창(권한 확인·계획 승인 …) 고르기
// 「확인 필요」인데 질문 창(AskUserQuestion)이 아니면, 그 터미널 화면의 선택지를 읽어 버튼으로 보여 준다. 누르면 그 번호 키만
// 보낸다(서버가 같은 창·같은 줄인지 다시 확인). 확인 필요인 동안만 2.5초마다 다시 읽는다
S.prompts = new Map();   // session_id → 선택 창 {head, options} 또는 null
S.promptAt = new Map();
function promptWanted(s) { return s && s.state === "waiting" && !askOf(s); }
// 질문 카드가 보이는 칸: 터미널에 그 질문 창이 정말 있는지 5초에 한 번 묻는다. 없으면(평소 입력칸) 서버가 지운다 —
// 질문 훅만 오고 창은 뜨지 않으면 「질문에 답할 차례」로 입력창이 막히고 답도 넣을 곳이 없었다
S.askAt = new Map();
async function syncAsks() {
  for (const s of S.sessions) {
    if (!askOf(s) || !document.querySelector(`.col[data-col="${CSS.escape(s.session_id)}"]`)) continue;
    if (Date.now() - (S.askAt.get(s.session_id) || 0) < 5000) continue;
    S.askAt.set(s.session_id, Date.now());
    try {
      if ((await api("POST", `/api/sessions/${s.session_id}/ask-check`, {})).cleared) {
        toast("터미널에 질문 창이 없어 보드에 남아 있던 질문을 지웠습니다 — 입력창을 쓸 수 있습니다");
        refresh(s.session_id);
      }
    } catch (e) { /* 다음에 */ }
  }
}
async function syncPrompts() {
  const want = new Set();
  for (const s of S.sessions) {
    if (!promptWanted(s) || !document.querySelector(`.col[data-col="${CSS.escape(s.session_id)}"]`)) continue;
    want.add(s.session_id);
    if (Date.now() - (S.promptAt.get(s.session_id) || 0) < 2500) continue;
    S.promptAt.set(s.session_id, Date.now());
    let v = null;
    try { v = (await api("GET", `/api/sessions/${s.session_id}/prompt`)).prompt; } catch (e) { /* 터미널을 못 찾음 */ }
    S.prompts.set(s.session_id, v);
    document.querySelectorAll(`.col[data-col="${CSS.escape(s.session_id)}"] .prompt-card`).forEach(el => updatePrompt(el, s));
  }
  for (const sid of [...S.prompts.keys()]) if (!want.has(sid)) { S.prompts.delete(sid); S.promptAt.delete(sid); }
}
// 창 머리 줄: 첫 줄은 제목, 물음표로 끝나는 마지막 줄은 질문, 「Bash command / 설명 / 명령 / 질문」처럼 4줄 이상이면
// 둘째 줄은 설명이고 나머지가 명령(고정폭). 짧은 창(종료 때 워크트리 묻기 등)은 안내 문장이라 고정폭으로 쓰지 않는다
function pcLineClass(head, i) {
  const last = head.length - 1;
  if (i === 0) return "pc-title";
  if (i === last && /\?$/.test(head[i])) return "pc-q";
  return head.length > 3 && i > 1 ? "pc-line" : "pc-desc";
}
function updatePrompt(el, s) {
  if (!el) return;
  const v = promptWanted(s) ? S.prompts.get(s.session_id) : null;
  if (!v || !v.options || !v.options.length) { el.hidden = true; el.dataset.key = ""; return; }
  el.hidden = false;
  const key = JSON.stringify(v);
  if (el.dataset.key === key) return;
  el.dataset.key = key;
  const sid = s.session_id;
  el.innerHTML = `<div class="ask-head"><b>🔐 확인 필요</b><span class="muted">터미널의 선택 창 — 골라서 넣습니다</span>
      <span class="spacer"></span><button class="linkish" data-action="ask-term" data-sid="${esc(sid)}">터미널로 ↗</button></div>
    <div class="pc-head">${v.head.map((ln, i) => `<div class="${pcLineClass(v.head, i)}">${esc(ln)}</div>`).join("")}</div>
    <div class="pc-opts">${v.options.map(o => `<button class="btn pc-opt ${o.n === 1 ? "primary" : ""} ${/^(no|remove|delete|discard)\b|아니|삭제|지우/i.test(o.label) ? "pc-no" : ""}"
        data-action="prompt-pick" data-sid="${esc(sid)}" data-n="${o.n}" data-label="${esc(o.label)}" title="${esc(o.hint || o.label)}">
        <b>${o.n}.</b> ${esc(o.label)}${o.hint ? `<span class="muted">${esc(o.hint)}</span>` : ""}</button>`).join("")}</div>
    <div class="ask-msg muted pc-msg"></div>`;
}
async function pickPrompt(b) {
  const card = b.closest(".prompt-card"), sid = b.dataset.sid;
  card.querySelectorAll(".pc-opt").forEach(x => { x.disabled = true; });
  try {
    toast((await api("POST", `/api/sessions/${sid}/prompt`, { n: +b.dataset.n, label: b.dataset.label })).message);
    S.prompts.delete(sid);
    card.hidden = true;
    card.dataset.key = "";
    setTimeout(() => refresh(sid), 500);
  } catch (err) {
    card.querySelector(".pc-msg").textContent = err.message;
    card.dataset.key = "";   // 다음 읽기에 새로 그린다
    S.promptAt.delete(sid);
    card.querySelectorAll(".pc-opt").forEach(x => { x.disabled = false; });
  }
}
// 질문 카드 「💬 답 대신 대화로」 — 터미널의 「Chat about this」. 질문을 넘긴 뒤 그 칸 입력창으로
async function chatAsk(b) {
  const sid = b.dataset.sid, s = sessionById(sid), ask = s && askOf(s), card = b.closest(".ask-card");
  if (!ask) return;
  card.querySelectorAll("button").forEach(x => { x.disabled = true; });
  try {
    toast((await api("POST", `/api/sessions/${sid}/answer`, { id: ask.id, chat: true })).message);
    card.hidden = true;
    card.dataset.key = "";
    // 넘긴 직후엔 아직 「확인 필요」라 입력창이 잠겨 있다 — 풀리면(훅이 상태를 바꾸면) 그 칸 입력창으로
    for (let i = 0; i < 8; i++) {
      await refresh(sid);
      const ta = colEl(sid) && colEl(sid).querySelector(".cmp-text");
      if (ta && !ta.disabled) { ta.focus(); break; }
      await new Promise(r => setTimeout(r, 700));
    }
  } catch (e) {
    if (e.data && e.data.cleared) { toast(e.message); card.hidden = true; card.dataset.key = ""; return refresh(sid); }
    card.querySelector(".ask-msg").textContent = e.message;
    card.querySelectorAll("button").forEach(x => { x.disabled = false; });
  }
}
function askOf(s) {
  if (s.state !== "waiting" || !s.ask) return null;
  try { const a = JSON.parse(s.ask); return a && Array.isArray(a.questions) && a.questions.length ? a : null; } catch (e) { return null; }
}
function updateAsk(el, s) {
  if (!el) return;
  const ask = askOf(s);
  if (!ask) { el.hidden = true; el.dataset.key = ""; return; }
  el.hidden = false;
  if (el.dataset.key === ask.id) return;
  el.dataset.key = ask.id;
  const sid = s.session_id;
  const preview = ask.questions.some(q => (q.options || []).some(o => o.preview));
  el.innerHTML = `
    <div class="ask-head"><b>❓ 질문에 답하기</b><span class="muted">터미널 질문 창에 대신 넣습니다</span>
      <span class="spacer"></span><button class="linkish" data-action="ask-term" data-sid="${esc(sid)}">터미널로 ↗</button></div>
    ${preview ? '<div class="muted ask-msg">미리보기가 있는 질문이라 터미널에서 답해 주세요.</div>' : ask.questions.map((q, qi) => {
      const type = q.multiSelect ? "checkbox" : "radio", name = `ask-${sid}-${qi}`;
      return `<div class="ask-q" data-qi="${qi}">
        <div class="ask-qhead">${q.header ? `<span class="chip">${esc(q.header)}</span>` : ""}${esc(q.question || "")}
          ${q.multiSelect ? '<span class="muted">· 여러 개</span>' : ""}</div>
        ${(q.options || []).map((o, oi) => `<label class="ask-opt"><input type="${type}" name="${name}" value="${oi + 1}">
          <span><b>${esc(o.label)}</b>${o.description ? `<span class="muted">${esc(o.description)}</span>` : ""}</span></label>`).join("")}
        <label class="ask-opt other"><input type="${type}" name="${name}" value="other">
          <input class="ask-other" type="text" placeholder="직접 입력" maxlength="2000"></label>
      </div>`;
    }).join("")}
    <div class="ask-foot"><span class="ask-msg muted"></span>
      <button class="btn ghost" data-action="ask-chat" data-sid="${esc(sid)}" title="Chat about this — 질문에 답하지 않고 넘긴 뒤 입력창에서 대화로 이어 갑니다">💬 답 대신 대화로</button>
      ${preview ? "" : `<button class="btn primary" data-action="ask-send" data-sid="${esc(sid)}">답 보내기</button>`}</div>`;
}
async function sendAsk(el) {
  const card = el.closest(".ask-card"), sid = card.dataset.sid;
  const s = sessionById(sid), ask = s && askOf(s);
  const msg = card.querySelector(".ask-msg");
  if (!ask) return;
  const answers = [];
  for (const qEl of card.querySelectorAll(".ask-q")) {
    const picked = [...qEl.querySelectorAll("input[type=radio]:checked, input[type=checkbox]:checked")].map(i => i.value);
    const other = qEl.querySelector(".ask-other").value.trim();
    const useOther = picked.includes("other") && other;
    const choices = picked.filter(v => v !== "other").map(Number);
    if (!choices.length && !useOther) {
      msg.textContent = "답하지 않은 질문이 있습니다";
      qEl.querySelector("input").focus();
      return;
    }
    answers.push({ choices, other: useOther ? other : "" });
  }
  el.disabled = true;
  el.textContent = "넣는 중…";
  msg.textContent = "터미널 질문 창에 답을 넣고 있습니다";
  try {
    const r = await api("POST", `/api/sessions/${sid}/answer`, { id: ask.id, answers });
    toast(r.message);
    card.hidden = true;
    card.dataset.key = "";   // 질문이 아직 남아 다시 보이면 새로 그린다(버튼이 「넣는 중…」으로 잠긴 채 남던 것)
    setTimeout(refresh, 600);
  } catch (e) {
    if (e.data && e.data.cleared) {   // 터미널에 그 질문 창이 없었다 — 서버가 지웠으니 카드를 걷고 입력창을 연다
      toast(e.message);
      card.hidden = true;
      card.dataset.key = "";
      return refresh(sid);
    }
    msg.textContent = e.message;
    el.disabled = false;
    el.textContent = "답 보내기";
  }
}
// 직접 입력 칸에 쓰면 그 줄을 고른 것으로
document.addEventListener("input", e => {
  if (!e.target.classList || !e.target.classList.contains("ask-other")) return;
  const box = e.target.closest(".ask-opt").querySelector("input[value=other]");
  if (box && e.target.value.trim()) box.checked = true;
});
function updateComposer(el, s) {
  const sug = suggestionFor(s);
  const ph = sug ? `${sug.replace(/\s+/g, " ")}   ⇥ Tab` : CMP_PLACEHOLDER;
  const pta = el.querySelector(".cmp-text");
  if (pta.placeholder !== ph) pta.placeholder = ph;
  el.classList.toggle("has-sugg", !!sug);
  pta.title = sug ? "터미널 입력칸에 보이는 글(Claude Code 의 다음 요청 제안 또는 터미널에 쳐 둔 글) — Tab 으로 채우기" : "";
  const d = displayState(s);
  // 확인 필요라도 질문 카드가 없으면 막지 않는다 — 보낼 때 서버가 화면을 보고 승인 창이 떠 있으면 거절한다
  // (창을 거절·중단하면 훅이 오지 않아 「확인 필요」만 남는 경우가 있다)
  const blocked = d === "waiting" && askOf(s) ? "질문에 답할 차례 — 위 카드에서 답하거나 터미널에서 답해 주세요"
    : d === "ended" && isTeammate(s) ? "일을 마친 팀원입니다 — 이어서 시킬 일은 팀장 탭에서"
    : d === "ended" && s._fam ? "이 갈래는 지금 터미널에 없습니다 — 위 버튼으로 전환해 주세요"
    : s.bg && !s.term_handle ? "백그라운드 갈래 — 위 「열기」로 터미널에 붙이면 보낼 수 있습니다" : "";
  const hint = blocked || (s.state === "ended" ? "종료된 세션 — 보내면 새 터미널 탭에서 이어 열지 물어봅니다"
    : d === "waiting" ? "확인 필요 — 터미널에 승인 창이 떠 있으면 보내지 않습니다(창이 닫혀 있으면 그대로 보냅니다)"
    : s.compact_at ? "컴팩트 중 — 보내면 요약이 끝난 뒤 처리됩니다"
    : d === "working" ? "보내면 지금 단계가 끝나는 대로 Claude 가 읽어 이번 작업에 반영합니다" : "");
  el.classList.toggle("blocked", !!blocked);
  el.querySelector(".cmp-text").disabled = !!blocked;
  const rich = richOf(el);
  if (rich && !el.classList.contains("sending")) rich.contentEditable = blocked ? "false" : "true";
  el.querySelector(".cmp-send").disabled = !!blocked || el.classList.contains("sending");
  el.querySelector(".cmp-stop").hidden = !(d === "working" || s.compact_at);   // 작업 중일 때만 중단 버튼
  el._hint = hint;
  if (!el.classList.contains("sending")) setHint(el, hint);
}
function setHint(el, text) {
  const h = el.querySelector(".cmp-hint");
  if (h.textContent !== text) h.textContent = text;
}
const FIELD_SIZING = window.CSS && CSS.supports && CSS.supports("field-sizing", "content");
function autoGrow(ta) {
  // 높이를 JS 로 재면 키마다 페이지 전체 배치를 다시 계산해 느려진다. 지원 브라우저는 CSS 에 맡긴다
  if (FIELD_SIZING) return;
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
}
function renderComposerImages(sid) {
  const imgs = (S.compose.get(sid) || []);
  document.querySelectorAll(`.composer[data-sid="${CSS.escape(sid)}"] .cmp-images`).forEach(box => {
    box.innerHTML = imgs.map((im, i) => `<div class="cmp-thumb" title="${esc(im.name)}">
      <img src="${im.data || im.url}" alt=""><span class="cmp-num">${i + 1}</span><button type="button" data-action="cmp-remove" data-sid="${esc(sid)}" data-idx="${i}" title="빼기">×</button></div>`).join("");
  });
}
// 이미지 파일을 data URL 로 읽어 store(session_id → 목록)에 붙인다. 이미지가 하나도 없으면 false
function readImages(files, store, sid, render, after) {
  const list = [...files].filter(f => /^image\/(png|jpeg|gif|webp)$/.test(f.type));
  if (!list.length) return false;
  for (const f of list) {
    if (f.size > 15 * 1024 * 1024) { toast(f.name + ": 15MB 를 넘는 이미지는 붙일 수 없습니다"); continue; }
    const reader = new FileReader();
    reader.onload = () => {
      const arr = store.get(sid) || [];
      if (arr.length >= 6) { toast("이미지는 한 번에 6장까지 붙일 수 있습니다"); return; }
      arr.push({ name: f.name || "image", data: reader.result });
      store.set(sid, arr);
      if (after) after(sid, arr.length);
      render(sid);
    };
    reader.readAsDataURL(f);
  }
  return true;
}
function addImages(sid, files) { return readImages(files, S.compose, sid, renderComposerImages, insertImageToken); }
// 입력창 커서 자리에 [이미지 N] 을 넣는다. 보낼 때 그 자리에 N 번째 이미지가 붙는다(자리가 없으면 끝에)
const IMG_TOKEN = /\[이미지 (\d+)\]/g;
function insertImageToken(sid, n) {
  const ta = visibleFirst(`.composer[data-sid="${CSS.escape(sid)}"] .cmp-text`);
  if (!ta) return;
  const v = ta.value, at = Math.min(ta.selectionStart ?? v.length, v.length), end = Math.min(ta.selectionEnd ?? at, v.length);
  const before = v.slice(0, at), after = v.slice(end);
  const tok = (before && !/\s$/.test(before) ? " " : "") + `[이미지 ${n}]` + (after && /^\s/.test(after) ? "" : " ");
  ta.value = before + tok + after;
  const pos = before.length + tok.length;
  ta.setSelectionRange(pos, pos);
  S.drafts.set(sid, ta.value);
  autoGrow(ta);
}
// 이미지를 빼면 그 자리 표시를 지우고 뒤 번호를 하나씩 당긴다
function removeImageToken(sid, n) {
  const ta = visibleFirst(`.composer[data-sid="${CSS.escape(sid)}"] .cmp-text`);
  if (!ta) return;
  ta.value = ta.value.replace(IMG_TOKEN, (m, k) => (+k === n ? "" : +k > n ? `[이미지 ${+k - 1}]` : m)).replace(/ {2,}/g, " ");
  S.drafts.set(sid, ta.value);
  autoGrow(ta);
}
function addNoteImages(sid, files) { return readImages(files, S.noteImgs, sid, renderNoteImages); }
function renderNoteImages(sid) {
  const imgs = S.noteImgs.get(sid) || [];
  document.querySelectorAll(`form[data-form="col-note"][data-sid="${CSS.escape(sid)}"] .note-add-imgs`).forEach(box => {
    box.innerHTML = imgs.map((im, i) => `<div class="cmp-thumb" title="${esc(im.name)}">
      <img src="${im.data}" alt=""><button type="button" data-action="note-img-remove" data-sid="${esc(sid)}" data-idx="${i}" title="빼기">×</button></div>`).join("");
  });
}
async function sendCompose(el) {
  const sid = el.dataset.sid;
  const ta = el.querySelector(".cmp-text");
  const text = composerText(el).trim();
  const images = S.compose.get(sid) || [];
  if ((!text && !images.length) || el.classList.contains("sending") || el.classList.contains("blocked")) return;
  // /diff 는 터미널의 선택 창 대신 보드의 변경 보기 창으로(파일 목록·나란히 보기·줄 안 강조)
  if (/^\/diff\s*$/.test(text) && !images.length) {
    ta.value = "";
    exitRich(el, false);
    autoGrow(ta);
    S.drafts.delete(sid);
    openDiff(sid);
    return;
  }
  // 보내는 동안은 흐리게 하고 고칠 수 없게(멈춘 것처럼 보이지 않도록 버튼에 회전 표시)
  el.classList.add("sending");
  ta.readOnly = true;
  if (richOf(el)) richOf(el).contentEditable = "false";
  el.querySelector(".cmp-send").disabled = true;
  setHint(el, "보내는 중…");
  // 보낸 대화가 보이도록 그 칸 대화를 바로 맨 아래로
  const th = el.closest(".col") && el.closest(".col").querySelector(".col-thread");
  if (th) { th._pinned = true; stickBottom(th); }
  let sent = false;
  try {
    // 「!」로 시작하면 Claude Code 가 셸 명령으로 바로 실행한다 — 직접 친 것임을 보드 화면의 토큰으로 알린다(브레인·예약은 막힘)
    const direct = /^\s*!/.test(text) ? { "X-Argus-Term": await termToken(true) } : undefined;
    const r = await api("POST", `/api/sessions/${sid}/send`, { text, images }, direct);
    sent = true;
    const g = S.sugg.get(sid);
    if (g) g.text = null;   // 보낸 뒤엔 그 제안은 지난 것
    ta.value = "";
    exitRich(el, false);
    autoGrow(ta);
    S.drafts.delete(sid);
    S.compose.delete(sid);
    renderComposerImages(sid);
    toast(r.message);
    // 선택 창을 띄우는 명령(/resume·/model 등)을 인자 없이 보냈으면 그 터미널로 가서 고르게 한다
    const cm = /^\/([\w:-]+)$/.exec(text);
    const cmd = cm && (S.cmds.get(sid) || []).find(c => c.name === cm[1]);
    if (cmd && cmd.kind === "interactive") setTimeout(() => focusTerminal(sid), 400);
    setTimeout(refresh, 600);
  } catch (err) {
    if (err.data && err.data.resume) openResume(sid, el);
    else toast(err.message);
  } finally {
    el.classList.remove("sending");
    ta.readOnly = false;
    if (richOf(el)) richOf(el).contentEditable = el.classList.contains("blocked") ? "false" : "true";
    el.querySelector(".cmp-send").disabled = el.classList.contains("blocked");
    setHint(el, el._hint || "");
    if (!sent && $("#resumeModal").hidden) (richOn(el) ? richOf(el) : ta).focus();   // 이어 열기 팝업이 떴으면 그 버튼에 둔다
  }
}
// 보내기 ▾ (슬랙의 보내기 옆 메뉴처럼): 지금 보내기 · 응답이 끝나면 보내기 · 넥스트 액션에만 넣기
function openSendMenu(btn) {
  const el = btn.closest(".composer"), sid = el.dataset.sid;
  const det = S.boardCache.get(sid);
  const waiting = det && det.detail ? det.detail.notes.filter(n => n.queued_at && !n.done).length : 0;
  const s = sessionById(sid), working = s && displayState(s) === "working";
  const opt = (k, label, hint) => `<button class="cm-item xm-opt" data-sendopt="${k}" data-sid="${esc(sid)}"><span>${label}</span><span class="muted">${hint}</span></button>`;
  openXMenu(btn, `<div class="xm-head">보내기</div>` +
    opt("now", "↑ 지금 보내기 <kbd>Enter</kbd>", working ? "작업 중이면 지금 단계가 끝나는 대로 Claude 가 읽어 이번 작업에 반영합니다" : "바로 보냅니다") +
    opt("queue", "⏭ 응답이 끝나면 보내기 <kbd>⌘Enter</kbd>", "넥스트 액션에 넣고 응답이 끝나면 보낼 대기열의 다음 순서로" + (waiting ? ` — 지금 ${waiting}개 대기` : "")) +
    opt("note", "📝 넥스트 액션에만 넣기", "보내지 않고 이 세션의 할 일로 남깁니다"), "right");
}
// 응답이 끝나면 보내기(⌘Enter · ▾): 입력창 글·이미지를 그 세션의 넥스트 액션으로 만들고 바로 예약한다 — 보내기는 예약 보내기
// (csl/sendqueue.py)가 그 세션의 응답이 끝났을 때 한다. 예약은 넥스트 액션 목록에 「⏭ 응답 끝나면 N번째로 보냄」으로 보여
// 풀거나 고칠 수 있다. 보내지면 그 할 일은 완료로 바뀐다. queue=false 면 넥스트 액션에만 넣는다
async function queueCompose(el, queue = true) {
  const sid = el.dataset.sid;
  const ta = el.querySelector(".cmp-text");
  const text = composerText(el).trim();
  const images = S.compose.get(sid) || [];
  if ((!text && !images.length) || el.classList.contains("sending")) return;
  if (queue && /^\/diff\s*$/.test(text)) return sendCompose(el);   // 보드 창 명령은 예약할 것이 없다
  el.classList.add("sending");
  try {
    const r = await api("POST", "/api/notes", { text, images, session_id: sid });
    if (queue) {
      try { await api("PATCH", "/api/notes/" + r.id, { queued: true }); }
      catch (err) { await api("DELETE", "/api/notes/" + r.id).catch(() => {}); throw err; }   // 예약이 거절되면(! 로 시작 등) 만든 할 일도 지운다
    }
    ta.value = "";
    exitRich(el, false);
    autoGrow(ta);
    S.drafts.delete(sid);
    S.compose.delete(sid);
    renderComposerImages(sid);
    const s = sessionById(sid);
    toast(!queue ? "넥스트 액션에 넣었습니다" : s && ["done", "idle"].includes(s.state) ? "예약했습니다 — 지금 쉬고 있어 곧 보냅니다"
      : "예약했습니다 — 응답이 끝나면 보냅니다(넥스트 액션에서 풀거나 고칠 수 있습니다)");
    refresh(sid);
  } catch (err) {
    toast(err.message);
  } finally {
    el.classList.remove("sending");
  }
}
// 넥스트 액션을 그 세션 입력창에 넣는다(전송은 사용자가 직접)
async function fillComposer(sid, text, imagePaths = []) {
  const col = await focusCol(sid);   // 보드에 그 칸이 없으면 꺼내 온다
  const el = col && col.querySelector(".composer");
  if (!el) return false;
  const ta = el.querySelector(".cmp-text");
  if (richOn(el)) exitRich(el, true);   // 서식 모드였으면 마크다운으로 바꿔 이어 붙인다
  const cur = ta.value.trim();
  if (text) ta.value = cur && cur !== text ? cur + "\n" + text : text;
  S.drafts.set(sid, ta.value);
  // 넥스트 액션에 붙인 이미지는 이미 올려 둔 파일이라 경로로 넣는다(보낼 때 서버가 그 파일을 그대로 쓴다)
  if (imagePaths.length) {
    const arr = S.compose.get(sid) || [];
    for (const p of imagePaths) {
      if (arr.length >= 6) { toast("이미지는 한 번에 6장까지 보낼 수 있습니다"); break; }
      if (!arr.some(im => im.path === p)) {
        arr.push({ name: base(p), path: p, url: "/api/upload/" + encodeURIComponent(base(p)) });
        S.compose.set(sid, arr);
        ta.setSelectionRange(ta.value.length, ta.value.length);
        insertImageToken(sid, arr.length);
      }
    }
    S.compose.set(sid, arr);
    renderComposerImages(sid);
  }
  autoGrow(ta);
  ta.focus({ preventScroll: true });
  ta.setSelectionRange(ta.value.length, ta.value.length);
  if (el.classList.contains("blocked")) toast("입력창에 넣었습니다 — 지금은 확인 필요 상태라 보내기가 막혀 있습니다");
  return true;
}
document.addEventListener("input", e => {
  if (!e.target.classList.contains("cmp-text")) return;
  markSeen(e.target.closest(".col"));   // 입력창에 있던 채로 새 응답이 왔으면 타이핑할 때 확인
  S.drafts.set(e.target.closest(".composer").dataset.sid, e.target.value);
  autoGrow(e.target);
  updateSlash(e.target);
});
// ------------------------------------------------------------------ 슬래시 명령 자동완성
// 「/」로 시작하면 Claude Code 처럼 명령 목록을 띄운다. 목록은 서버가 설치된 Claude Code 실행 파일과
// 사용자·프로젝트·플러그인 명령·스킬에서 읽는다. 고르면 그 터미널로 보내 실제로 실행된다
const SLASH_KIND = { interactive: "터미널 창", prompt: "Claude 요청", skill: "스킬", command: "명령", plugin: "플러그인", board: "보드 창" };
const BOARD_SLASH = { diff: "변경 보기 — 보드 창에서(파일 목록·나란히 보기)" };   // 터미널로 보내지 않고 보드가 직접 여는 명령
async function loadCommands(sid) {
  if (!S.cmds.has(sid)) {
    S.cmds.set(sid, []);
    try { S.cmds.set(sid, (await api("GET", `/api/sessions/${sid}/commands`)).commands); } catch (e) { S.cmds.delete(sid); }
  }
  return S.cmds.get(sid);
}
function slashQuery(ta) {
  const v = ta.value;
  const m = /^\/([^\s]*)$/.exec(v.slice(0, ta.selectionStart));
  return m && !v.includes("\n") && !/\s/.test(v.slice(ta.selectionStart).split(/\s/)[0] || "") ? m[1].toLowerCase() : null;
}
async function updateSlash(ta) {
  const el = ta.closest(".composer"), pop = el.querySelector(".slash-pop");
  const q = slashQuery(ta);
  if (q === null) { pop.hidden = true; return; }
  const all = await loadCommands(el.dataset.sid);
  if (slashQuery(ta) !== q) return;   // 불러오는 사이 글이 바뀌었으면 다음 입력에 맡긴다
  const scored = [];
  for (const c of all) {
    const n = c.name.toLowerCase();
    const sc = n.startsWith(q) ? 0 : n.split(/[:-]/).some(p => p.startsWith(q)) ? 1 : n.includes(q) ? 2
      : q && (c.desc || "").toLowerCase().includes(q) ? 3 : -1;
    if (sc >= 0) scored.push([sc, c]);
  }
  // Claude Code 처럼 같은 일치 정도면 짧은 이름이 위(/re → recap, rename, resume, rewind …)
  scored.sort((a, b) => a[0] - b[0] || a[1].name.length - b[1].name.length || a[1].name.localeCompare(b[1].name));
  const list = scored.slice(0, 50).map(x => x[1]);
  pop._list = list;
  pop._sel = Math.min(pop._sel || 0, Math.max(0, list.length - 1));
  if (pop._q !== q) pop._sel = 0;
  pop._q = q;
  drawSlash(pop, q);
}
function drawSlash(pop, q) {
  const list = pop._list || [];
  if (!list.length) { pop.hidden = true; return; }
  pop.innerHTML = list.map((c, i) => {
    const n = esc(c.name);
    const hl = q && c.name.toLowerCase().startsWith(q) ? `<b>${esc(c.name.slice(0, q.length))}</b>${esc(c.name.slice(q.length))}` : n;
    return `<div class="sp-row ${i === pop._sel ? "on" : ""}" data-action="slash-pick" data-idx="${i}">
      <span class="sp-name">/${hl}${c.hint ? ` <span class="sp-hint">${esc(c.hint)}</span>` : ""}</span>
      <span class="sp-desc${BOARD_SLASH[c.name] ? " board" : ""}">${esc(BOARD_SLASH[c.name] || c.desc || "")}</span>
      ${(k => SLASH_KIND[k] ? `<span class="sp-kind ${k}">${SLASH_KIND[k]}</span>` : "")(BOARD_SLASH[c.name] ? "board" : c.kind)}</div>`;
  }).join("");
  pop.hidden = false;
  const on = pop.querySelector(".sp-row.on");
  if (on) on.scrollIntoView({ block: "nearest" });
}
// 고른 명령을 넣는다. Enter: 인자가 꼭 필요한(<…>) 명령이 아니면 바로 보낸다. Tab: 뒤에 인자를 쓸 수 있게 넣기만
function pickSlash(ta, how) {
  const pop = ta.closest(".composer").querySelector(".slash-pop");
  const c = (pop._list || [])[pop._sel || 0];
  pop.hidden = true;
  if (!c) return;
  const needsArg = /^</.test(c.hint || "");
  ta.value = "/" + c.name + (how === "tab" || needsArg ? " " : "");
  S.drafts.set(ta.closest(".composer").dataset.sid, ta.value);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  if (how === "enter" && !needsArg) sendCompose(ta.closest(".composer"));
}
function slashKey(e) {
  const ta = e.target, pop = ta.closest(".composer").querySelector(".slash-pop");
  if (pop.hidden || imeBusy(e) || e.metaKey || e.ctrlKey || e.altKey) return false;
  const n = (pop._list || []).length;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    pop._sel = ((pop._sel || 0) + (e.key === "ArrowDown" ? 1 : -1) + n) % n;
    drawSlash(pop, pop._q);
    return true;
  }
  if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
    e.preventDefault();
    pickSlash(ta, e.key === "Tab" ? "tab" : "enter");
    return true;
  }
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    pop.hidden = true;
    return true;
  }
  return false;
}
document.addEventListener("focusout", e => {
  // 입력창에서 나가면 닫는다(목록을 누르는 동안은 pointerdown 이 먼저라 그 클릭은 처리된다)
  if (e.target.classList && e.target.classList.contains("cmp-text")) {
    const pop = e.target.closest(".composer").querySelector(".slash-pop");
    setTimeout(() => { if (document.activeElement !== e.target) pop.hidden = true; }, 150);
  }
});

// 커서가 입력창의 첫 줄(자동 줄바꿈 포함)에 있는지. 같은 폭·글꼴의 숨은 상자에 커서 앞 글만 넣어 높이로 판단한다
let caretMirror = null;
function mirrorHeight(ta, text) {
  const cs = getComputedStyle(ta);
  if (!caretMirror) {
    caretMirror = document.createElement("div");
    caretMirror.style.cssText = "position:absolute;left:-9999px;top:0;visibility:hidden;white-space:pre-wrap;overflow-wrap:break-word";
    document.body.appendChild(caretMirror);
  }
  for (const k of ["fontFamily", "fontSize", "fontWeight", "letterSpacing", "lineHeight", "boxSizing",
    "paddingLeft", "paddingRight", "borderLeftWidth", "borderRightWidth", "borderLeftStyle", "borderRightStyle", "wordBreak", "tabSize"]) {
    caretMirror.style[k] = cs[k];
  }
  caretMirror.style.width = ta.offsetWidth + "px";
  caretMirror.textContent = text;
  return caretMirror.offsetHeight;
}
function caretOnFirstLine(ta) {
  if (ta.selectionStart !== ta.selectionEnd) return false;
  const before = ta.value.slice(0, ta.selectionStart);
  if (!before) return true;
  if (before.includes("\n")) return false;
  return mirrorHeight(ta, before + "\u200b") <= mirrorHeight(ta, "x");
}
function caretOnLastLine(ta) {
  if (ta.selectionStart !== ta.selectionEnd) return false;
  const after = ta.value.slice(ta.selectionEnd);
  if (!after) return true;
  if (after.includes("\n")) return false;
  return mirrorHeight(ta, ta.value.slice(0, ta.selectionEnd) + "\u200b") >= mirrorHeight(ta, ta.value + "\u200b");
}
document.addEventListener("keydown", e => {
  if (!e.target.classList || !e.target.classList.contains("cmp-text")) return;
  if (slashKey(e)) return;
  // 비어 있을 때 Tab: 터미널 입력칸의 제안을 채운다
  if (e.key === "Tab" && !e.shiftKey && !e.target.value && !imeBusy(e)) {
    const el = e.target.closest(".composer"), s = sessionById(el.dataset.sid);
    const sug = s && suggestionFor(s);
    if (sug) {
      e.preventDefault();
      e.target.value = sug;
      S.drafts.set(el.dataset.sid, sug);
      autoGrow(e.target);
      e.target.setSelectionRange(sug.length, sug.length);
      markSeen(e.target.closest(".col"));
      return;
    }
  }
  if (e.key === "Enter" && !e.shiftKey && !imeBusy(e)) {
    e.preventDefault();
    if (e.metaKey || e.ctrlKey) queueCompose(e.target.closest(".composer"));   // ⌘Enter: 응답이 끝나면 보내기
    else sendCompose(e.target.closest(".composer"));
  }
  // 첫 줄에서 ↑: 입력창을 빠져나와 칸 선택 상태로(쓰던 글은 그대로). 이어서 ← → 로 칸 이동
  if (e.key === "ArrowUp" && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey && !imeBusy(e) &&
      caretOnFirstLine(e.target)) {
    e.preventDefault();
    const sid = e.target.closest(".composer").dataset.sid;
    e.target.blur();
    setActive(sid);
  }
});
document.addEventListener("paste", e => {
  if (e.target.classList && e.target.classList.contains("note-input")) {
    const files = [...(e.clipboardData && e.clipboardData.files || [])];
    if (files.length && addNoteImages(e.target.form.dataset.sid, files) && !e.clipboardData.getData("text/plain")) e.preventDefault();
    return;
  }
  if (!e.target.classList || !(e.target.classList.contains("cmp-text") || e.target.classList.contains("ibc-text"))) return;
  const files = [...(e.clipboardData && e.clipboardData.files || [])];
  if (e.target.classList.contains("cmp-text") && files.length && addImages(e.target.closest(".composer").dataset.sid, files)
      && !e.clipboardData.getData("text/plain")) {
    e.preventDefault();
    return;
  }
  // 서식 있는 글(보드의 응답·웹 페이지 등)은 마크다운으로 바꿔 넣는다 — 입력창은 글만 받으니 굵게·목록·코드·링크를
  // 마크다운 기호로 살린다. ⌘⇧V(서식 없이 붙여넣기)면 브라우저 기본대로 글만
  const html = e.clipboardData && e.clipboardData.getData("text/html");
  if (!html || PLAIN_PASTE.on) return;
  if (e.target.classList.contains("cmp-text")) {
    const clean = sanitizeRich(html);
    if (!clean.replace(/<[^>]*>/g, "").trim()) return;
    e.preventDefault();
    enterRich(e.target.closest(".composer"), clean);
    return;
  }
  const text = htmlToMd(html);
  if (!text) return;
  e.preventDefault();
  const ta = e.target, a = ta.selectionStart, b = ta.selectionEnd;
  ta.setRangeText(text, a, b, "end");
  ta.dispatchEvent(new Event("input", { bubbles: true }));
});
// ⌘⇧V / ⌃⇧V 를 누른 직후의 붙여넣기는 서식 없이(글만)
const PLAIN_PASTE = { on: false, timer: 0 };
document.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.code === "KeyV") {
    PLAIN_PASTE.on = true;
    clearTimeout(PLAIN_PASTE.timer);
    PLAIN_PASTE.timer = setTimeout(() => { PLAIN_PASTE.on = false; }, 500);
  }
}, true);
// 붙여넣은 HTML → 마크다운(제목·굵게·기울임·코드·코드 블록·링크·목록·인용·표·줄바꿈)
// 코드 블록의 글 — 서식 모드에서 Shift+Enter 로 바꾼 줄은 <br>·줄 div 라 textContent 로 읽으면 줄이 붙는다
function preText(n) {
  let out = "";
  const go = x => {
    for (const c of x.childNodes) {
      if (c.nodeType === 3) out += c.nodeValue;
      else if (c.nodeName === "BR") out += "\n";
      else if (c.nodeType === 1) {
        const line = /^(DIV|P)$/.test(c.nodeName);
        if (line && out && !out.endsWith("\n")) out += "\n";
        go(c);
        if (line && !out.endsWith("\n")) out += "\n";
      }
    }
  };
  go(n);
  return out.replace(/\n$/, "");
}
function htmlToMd(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll("script, style, button, .code-copy").forEach(n => n.remove());
  const inline = n => [...n.childNodes].map(walk).join("");
  function list(n, ordered, depth) {
    // 입력창에서 Tab 으로 들여쓰면 브라우저가 li 안이 아니라 목록 바로 아래에 ul 을 만든다 — 그것도 한 단계 안쪽 목록으로
    let i = 0;
    return [...n.children].map(c => {
      if (c.tagName === "UL" || c.tagName === "OL") return list(c, c.tagName === "OL", depth + 1);
      if (c.tagName !== "LI") return "";
      const nested = [...c.children].filter(x => x.tagName === "UL" || x.tagName === "OL");
      nested.forEach(x => x.remove());
      const head = "  ".repeat(depth) + (ordered ? (++i) + ". " : "- ") + inline(c).trim().replace(/\n+/g, " ");
      return [head, ...nested.map(x => list(x, x.tagName === "OL", depth + 1))].join("\n");
    }).filter(Boolean).join("\n");
  }
  function table(n) {
    const rows = [...n.querySelectorAll("tr")].map(tr => [...tr.children].map(c => inline(c).trim().replace(/\|/g, "\\|").replace(/\n+/g, " ")));
    if (!rows.length) return "";
    const w = Math.max(...rows.map(r => r.length));
    const line = r => "| " + [...r, ...Array(w - r.length).fill("")].join(" | ") + " |";
    return [line(rows[0]), "|" + " --- |".repeat(w), ...rows.slice(1).map(line)].join("\n");
  }
  function walk(n) {
    if (n.nodeType === 3) return n.nodeValue.replace(/\s+/g, " ");
    if (n.nodeType !== 1) return "";
    const t = n.tagName;
    if (/^H[1-6]$/.test(t)) return "\n\n" + "#".repeat(+t[1]) + " " + inline(n).trim() + "\n\n";
    if (t === "STRONG" || t === "B") { const x = inline(n); return x.trim() ? "**" + x.trim() + "**" : x; }
    if (t === "EM" || t === "I") { const x = inline(n); return x.trim() ? "*" + x.trim() + "*" : x; }
    if (t === "CODE") return n.closest("pre") ? n.textContent : "`" + n.textContent + "`";
    if (t === "PRE") return "\n\n```\n" + preText(n) + "\n```\n\n";
    if (t === "A") { const x = inline(n).trim(), h = n.getAttribute("href") || ""; return /^https?:/.test(h) && h !== x ? `[${x}](${h})` : x; }
    if (t === "BR") return "\n";
    if (t === "UL" || t === "OL") return "\n\n" + list(n, t === "OL", 0) + "\n\n";
    if (t === "BLOCKQUOTE") return "\n\n" + inline(n).trim().split("\n").map(l => "> " + l).join("\n") + "\n\n";
    if (t === "TABLE") return "\n\n" + table(n) + "\n\n";
    if (t === "HR") return "\n\n---\n\n";
    if (["P", "DIV", "SECTION", "ARTICLE", "LI", "TR"].includes(t)) return "\n" + inline(n) + "\n";
    return inline(n);
  }
  return walk(doc.body).replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}
document.addEventListener("change", e => {
  if (!e.target.classList.contains("cmp-file")) return;
  addImages(e.target.closest(".composer").dataset.sid, e.target.files);
  e.target.value = "";
});
document.addEventListener("dragover", e => {
  const z = e.target.closest && e.target.closest("[data-drop-sid]");
  if (z && e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); z.classList.add("drop-over"); }
});
document.addEventListener("dragleave", e => {
  const z = e.target.closest && e.target.closest("[data-drop-sid]");
  if (z && !z.contains(e.relatedTarget)) z.classList.remove("drop-over");
});
// Finder 에서 끌어 놓은 파일: 이미지는 지금처럼 첨부, 나머지 파일·폴더는 원래 경로를 입력창 커서 자리에 넣는다(터미널의
// Claude Code 처럼). 브라우저는 경로를 주지 않아 이름·크기·수정 시각으로 서버가 찾는다(/api/locate)
const IMG_TYPE = /^image\/(png|jpeg|gif|webp)$/;
function droppedFiles(dt) {
  const items = [...(dt.items || [])].filter(it => it.kind === "file");
  return [...dt.files].map((f, i) => {
    const en = items[i] && items[i].webkitGetAsEntry ? items[i].webkitGetAsEntry() : null;
    return { file: f, dir: !!(en && en.isDirectory) };
  });
}
function pathToken(p) { return /[\s"'`$\\]/.test(p) ? `"${p.replace(/(["\\$`])/g, "\\$1")}"` : p; }
function insertAtCursor(target, text) {
  if (target.classList && target.classList.contains("composer")) {
    if (richOn(target)) {
      const rich = richOf(target);
      rich.focus();
      document.execCommand("insertText", false, text);
      saveRichDraft(target);
      return;
    }
    target = target.querySelector(".cmp-text");
  }
  const v = target.value, at = Math.min(target.selectionStart ?? v.length, v.length), end = Math.min(target.selectionEnd ?? at, v.length);
  const before = v.slice(0, at), after = v.slice(end);
  const tok = (before && !/\s$/.test(before) ? " " : "") + text + (after && /^\s/.test(after) ? "" : " ");
  target.value = before + tok + after;
  target.focus();
  target.setSelectionRange(before.length + tok.length, before.length + tok.length);
  target.dispatchEvent(new Event("input", { bubbles: true }));   // 초안 기억·높이 맞추기는 각 입력칸의 input 처리가 한다
}
async function dropPaths(target, sid, list) {
  if (!list.length) return;
  const slow = setTimeout(() => toast("파일 위치를 찾는 중…"), 500);
  let r;
  try {
    r = await api("POST", "/api/locate", { session_id: sid, files: list.map(x => ({ name: x.file.name, size: x.file.size, mtime: x.file.lastModified, dir: x.dir })) });
  } catch (err) { clearTimeout(slow); toast(err.message); return; }
  clearTimeout(slow);
  const got = r.results.filter(x => x.path), miss = r.results.filter(x => !x.path);
  if (got.length) insertAtCursor(target, got.map(x => pathToken(x.path)).join(" "));
  if (miss.length) toast(`원래 위치를 찾지 못했습니다: ${miss.map(x => x.name).join(", ")}`);
  else if (got.some(x => x.others)) toast("같은 파일이 다른 곳에도 있어 이 세션 폴더 쪽(없으면 처음 찾은 곳)을 넣었습니다 — 경로를 확인해 주세요");
}
document.addEventListener("drop", e => {
  // 넥스트 액션 입력칸 위에 놓으면 그 할 일에, 칸의 나머지 자리에 놓으면 채팅 입력창에 붙인다.
  // 파일이 아닌 것(골라 끈 글 등)은 브라우저 기본 동작대로 — 입력창에 글이 들어간다
  if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
  const all = droppedFiles(e.dataTransfer);   // 폴더인지는 drop 처리 안에서만 알 수 있다
  const imgs = all.filter(x => !x.dir && IMG_TYPE.test(x.file.type)).map(x => x.file), rest = all.filter(x => x.dir || !IMG_TYPE.test(x.file.type));
  const nf = e.target.closest && e.target.closest('form[data-form="col-note"]');
  if (nf) {
    e.preventDefault();
    const z0 = nf.closest("[data-drop-sid]");
    if (z0) z0.classList.remove("drop-over");
    if (imgs.length) addNoteImages(nf.dataset.sid, imgs);
    dropPaths(nf.querySelector(".note-input"), nf.dataset.sid, rest);
    return;
  }
  const z = e.target.closest && e.target.closest("[data-drop-sid]");
  if (!z) return;
  e.preventDefault();
  z.classList.remove("drop-over");
  if (imgs.length) addImages(z.dataset.dropSid, imgs);
  const comp = visibleFirst(`.composer[data-sid="${CSS.escape(z.dataset.dropSid)}"]`);
  if (rest.length && comp && !comp.classList.contains("blocked")) dropPaths(comp, z.dataset.dropSid, rest);
  else if (rest.length) toast("지금은 이 입력창에 넣을 수 없습니다");
});

// ------------------------------------------------------------------ 동작
// 바로 다시 그린다. sid 를 주면 그 칸 대화만 다시 받는다(할 일 체크·고정 같은 작은 조작에 보이는 칸 전부를 다시 받지 않게)
async function refresh(sid) {
  if (sid) S.boardCache.delete(sid);
  else S.boardCache.clear();
  await poll();
}
async function copyCode(btn) {
  const box = btn.closest(".code-wrap");
  const code = box.querySelector("pre code") ? box.querySelector("pre code").textContent : box.querySelector("textarea").value;
  try {
    // 페이지에 포커스가 없으면 clipboard API 가 권한 확인에서 멈출 수 있다 — 1초 안에 안 끝나면 대체 방식으로
    if (!document.hasFocus()) throw new Error("no focus");
    await Promise.race([navigator.clipboard.writeText(code),
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 1000))]);
  } catch (e) {
    const ta = document.createElement("textarea");   // 클립보드 권한이 없을 때
    ta.value = code;
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (!ok) return toast("복사하지 못했습니다");
  }
  btn.textContent = "복사됨 ✓";
  btn.classList.add("done");
  setTimeout(() => { btn.textContent = "복사"; btn.classList.remove("done"); }, 1400);
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 1600);
}
function startEdit(el) {
  if (el.isContentEditable) return;
  S.editingNote = true;
  const before = noteRaw(el), shown = el.innerHTML;   // 고치는 동안은 마크다운 원문, 그만두면 그린 모습으로
  el.textContent = before;
  el.classList.remove("md");
  el.contentEditable = "true";
  el.focus();
  document.getSelection().selectAllChildren(el);
  const finish = async (commit) => {
    el.removeEventListener("blur", onBlur);
    el.removeEventListener("keydown", onKey);
    el.contentEditable = "false";
    S.editingNote = false;
    const text = el.innerText.trim();
    const restore = () => { el.innerHTML = shown; el.classList.add("md"); };
    if (commit && text && text !== before) {
      try { await api("PATCH", "/api/notes/" + el.dataset.nid, { text }); el.innerHTML = mdCached(text); el.classList.add("md"); }
      catch (err) { restore(); toast(err.message); }
    } else restore();
    await refresh(el.closest(".col") && el.closest(".col").dataset.col);
  };
  const onBlur = () => finish(true);
  const onKey = e => {
    if (imeBusy(e)) return;   // 한글 조합 중 Enter 는 조합을 끝낼 뿐 — 저장하지 않는다
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); el.blur(); }
    if (e.key === "Escape") { e.preventDefault(); el.removeEventListener("blur", onBlur); finish(false); }
  };
  el.addEventListener("blur", onBlur);
  el.addEventListener("keydown", onKey);
}
async function toggleTrail(tid, sid) {
  S.uiVer++;
  if (S.trailOpen.has(tid)) { S.trailOpen.delete(tid); return renderBoard(); }
  S.trailOpen.add(tid);
  renderBoard();
  if (S.trail.has(tid)) return;
  const pool = ((S.boardCache.get(sid) || {}).detail || {}).turns;
  const t = (pool || []).find(x => x.id === tid);
  if (!t) return;
  try {
    const r = await api("GET", `/api/sessions/${sid}/activity?since=${t.prompt_at}&until=${t.response_at}`);
    S.trail.set(tid, r.items);
  } catch (e) {
    S.trail.set(tid, []);
  }
  S.uiVer++;
  renderBoard();
}

// 새 응답 확인 처리 — 칸(넥스트 액션 영역 제외)을 마우스로 누르거나, 그 칸의 채팅 입력창에 들어가거나,
// 이미 입력창에 있으면 타이핑할 때만. ← → 나 ⌘숫자로 칸을 지나가며 선택만 된 것은 읽은 것으로 치지 않는다
// 칸을 누르거나 입력창에 들어오면 그 세션을 본 것으로 — 「새 응답」 표시가 있거나, 알림(🔔)이 안 본 것으로 세는
// 활동(확인 필요·질문·오류 포함)이 있을 때만 서버에 알린다
S.unseenSids = new Set();
function markSeen(col) {
  if (!col || !(col.classList.contains("unread") || S.unseenSids.has(col.dataset.col))) return;
  S.unseenSids.delete(col.dataset.col);
  col.classList.remove("unread");
  const badge = col.querySelector(".unread-badge");
  if (badge) badge.remove();
  api("POST", "/api/sessions/" + col.dataset.col + "/seen", {}).then(() => { viewsAt = 0; return poll(); }).catch(() => {});
}
document.addEventListener("click", async e => {
  if (!e.target.closest(".note-add, .cf-notes, .cf-label")) markSeen(e.target.closest(".col"));
  const el = e.target.closest("[data-action]");
  if (!el) return;
  const a = el.dataset.action;
  if (a === "open-diff") return openDiff(el.dataset.sid);
  if (a === "rc-toggle") return toggleRemote(el.dataset.sid, !!el.dataset.on);
  if (a === "note-qmove") { S.noteFocus = el.dataset.nid; return moveQueued(el.closest(".col").dataset.col, +el.dataset.nid, +el.dataset.dir); }
  if (a === "open-file" || a === "reveal-file") {
    e.preventDefault();
    const reveal = a === "reveal-file" || e.altKey;
    try {
      const r = await api("POST", "/api/open", { path: el.dataset.path, reveal });
      toast(r.how === "reveal" ? (reveal ? "Finder 에서 보여 줍니다" : "바로 열지 않는 형식이라 Finder 에서 보여 줍니다") : "열었습니다");
    } catch (err) {
      toast(err.message);
    }
    return;
  }
  if (a === "board-more") { S.boardLimit += 8; return refreshBoard(); }
  if (a === "rewind-menu") return openRewindMenu(el);
  if (a === "rw-toggle") {
    S.rwOpen.has(el.dataset.sid) ? S.rwOpen.delete(el.dataset.sid) : S.rwOpen.add(el.dataset.sid);
    S.uiVer++;
    return renderBoard();
  }
  if (a === "perm-menu") return openPermMenu(el);
  if (a === "goal-menu") return openGoalMenu(el.dataset.sid, el);
  if (a === "fork-menu") return openForkMenu(el);
  if (a === "fam-close") {
    // 두 번 눌러야 닫는다(3초 안에). 닫으면 보드에서 숨기고, 백그라운드 갈래면 Claude Code 쪽 세션도 멈춰 지운다
    e.stopPropagation();
    if (el.dataset.armed !== "1") {
      el.dataset.armed = "1";
      el.classList.add("armed");
      toast("한 번 더 누르면 이 갈래를 닫습니다");
      setTimeout(() => { el.dataset.armed = ""; el.classList.remove("armed"); }, 3000);
      return;
    }
    try { toast((await api("POST", `/api/sessions/${el.dataset.sid}/close-branch`, {})).message); }
    catch (err) { return toast(err.message); }
    return refresh();
  }
  if (a === "fam-tab") {
    S.famSel[el.dataset.root] = el.dataset.sid;
    save("csl.famSel", S.famSel);
    await refreshBoard();
    renderBoard();
    return focusCol(el.dataset.sid);
  }
  if (a === "fam-switch" || a === "fam-attach") {
    el.disabled = true;
    try {
      const r = await api("POST", `/api/sessions/${el.dataset.sid}/${a === "fam-attach" ? "attach" : "switch"}`, {});
      toast(r.message);
    } catch (err) { toast(err.message); el.disabled = false; return; }
    return refresh(el.dataset.sid);
  }
  if (a === "more-turns") {
    const sid = el.dataset.sid;
    const th = el.closest(".col-thread");
    th._pinned = false;
    S.anchor.set(sid, { h: th.scrollHeight, top: th.scrollTop });
    S.colLimit.set(sid, (S.colLimit.get(sid) || BOARD_TURNS) + 40);
    S.boardCache.delete(sid);
    el.disabled = true;
    el.textContent = "불러오는 중…";
    return refreshBoard();
  }
  if (a === "pin") return togglePin(el.dataset.sid);
  if (a === "copy-code") return copyCode(el);
  if (a === "copy-reply") return copyCode(el);
  if (a === "open-connectors") return openConnectors();
  if (a === "ask-send") return sendAsk(el);
  if (a === "ask-chat") return chatAsk(el);
  if (a === "brain-start") return startBrain();
  if (a === "brain-launch-term" || a === "brain-launch-pick") return brainLaunchAct(el);
  if (a === "brain-quick") return brainQuick(el.dataset.prompt);
  if (a === "inbox-run") return startInboxRun();
  if (a === "inbox-run-sum") { const id = +el.dataset.run; S.runOpen = S.runOpen === id ? null : id; return renderInboxRuns(); }
  if (a === "inbox-run-hist") { S.runHist = !S.runHist; return renderInboxRuns(); }
  if (a && a.startsWith("ib-")) return inboxAction(a, el);
  if (a === "cmp-stop") return stopSession(el.dataset.sid, el);
  if (a === "ask-term") return focusTerminal(el.dataset.sid);
  if (a === "col-menu") return openColMenu(el);
  if (a === "col-zoom") { setActive(el.dataset.sid); return setColZoom((S.colZoom + 1) % 3); }
  if (a === "toggle-resp" || a === "toggle-prompt" || a === "toggle-mid") {
    const set = a === "toggle-resp" ? S.expanded : a === "toggle-mid" ? S.midOpen : S.promptOpen;
    const id = +el.dataset.tid;
    // 펼치거나 접어도 그 턴이 보이던 자리에 그대로 있게(맨 아래 따라가기를 풀고, 다시 그린 뒤 위치를 맞춘다)
    const th = el.closest(".col-thread");
    const art = el.closest("article.turn");
    const before = th && art ? art.getBoundingClientRect().top - th.getBoundingClientRect().top : null;
    if (th) th._pinned = false;
    S.uiVer++;
    set.has(id) ? set.delete(id) : set.add(id);
    renderBoard();
    if (before != null) {
      const now = th.querySelector(`article.turn[data-tid="${id}"]`);
      if (now) th.scrollTop += now.getBoundingClientRect().top - th.getBoundingClientRect().top - before;
    }
    return;
  }
  if (a === "toggle-notes") return toggleNotes(el.dataset.sid);
  if (a === "counter") return gotoCounter(el.dataset.kind);
  if (a === "ctx-menu") return openCtxMenu(el);
  if (a === "slash-pick") {
    const pop = el.closest(".slash-pop");
    pop._sel = +el.dataset.idx;
    return pickSlash(pop.closest(".composer").querySelector(".cmp-text"), "enter");
  }
  if (a === "toggle-step") {
    const k = el.dataset.key;
    S.stepOpen.has(k) ? S.stepOpen.delete(k) : S.stepOpen.add(k);
    S.uiVer++;
    return renderBoard();
  }
  if (a === "note-img-remove") {
    const arr = S.noteImgs.get(el.dataset.sid) || [];
    arr.splice(+el.dataset.idx, 1);
    return renderNoteImages(el.dataset.sid);
  }
  if (a === "bg-task") { closeXMenu(); return openBgTask(el.dataset.sid, el.dataset.tid); }
  if (a === "prompt-pick") return pickPrompt(el);
  if (a === "bg-tasks") return toggleBgtMenu(el);
  if (a === "toggle-recap") {
    const sid = el.dataset.sid;
    S.uiVer++;
    S.recapOpen.has(sid) ? S.recapOpen.delete(sid) : S.recapOpen.add(sid);
    return renderBoard();
  }
  if (a === "toggle-trail") return toggleTrail(+el.dataset.tid, el.dataset.sid);
  if (a === "rename") { e.preventDefault(); return startRename(el); }
  if (a === "model-menu") return openModelMenu(el);
  if (a === "note-queue") {
    const on = !el.classList.contains("on");
    try {
      await api("PATCH", "/api/notes/" + el.dataset.nid, { queued: on });
      const s = sessionById(el.dataset.sid);
      toast(!on ? "예약을 풀었습니다" : s && ["done", "idle"].includes(s.state) ? "지금 쉬고 있어 곧 보냅니다" : "응답이 끝나면 보냅니다");
    } catch (err) { toast(err.message); }
    return refresh(el.dataset.sid);
  }
  if (a === "note-use") {
    const li = el.closest(".note");
    return fillComposer(el.dataset.sid, noteRaw(li.querySelector(".note-text")), noteImages(findNote(el.dataset.nid) || {}));
  }
  if (a === "cmp-send") return sendCompose(el.closest(".composer"));
  if (a === "cmp-more") return openSendMenu(el);
  if (a === "cmp-attach") return el.closest(".composer").querySelector(".cmp-file").click();
  if (a === "cmp-remove") {
    const arr = S.compose.get(el.dataset.sid) || [];
    arr.splice(+el.dataset.idx, 1);
    removeImageToken(el.dataset.sid, +el.dataset.idx + 1);
    S.compose.set(el.dataset.sid, arr);
    return renderComposerImages(el.dataset.sid);
  }
  if (a === "note-done" || a === "note-del") {
    // 실패해도 다시 그려 체크박스·목록을 서버 상태로 되돌린다(오류는 아래 unhandledrejection 이 알린다)
    try {
      if (a === "note-done") await api("PATCH", "/api/notes/" + el.dataset.nid, { done: el.checked });
      else await api("DELETE", "/api/notes/" + el.dataset.nid);
    } finally { refresh(el.closest(".col") && el.closest(".col").dataset.col); }
  }
});

document.addEventListener("dblclick", e => {
  const el = e.target.closest('[data-action="note-edit"]');
  if (el) startEdit(el);
});

// 넥스트 액션 입력: Enter 추가, Shift+Enter 줄바꿈(한글 조합 중 Enter 는 무시)
document.addEventListener("keydown", e => {
  if (!e.target.classList || !e.target.classList.contains("note-input")) return;
  if (e.key === "Enter" && !e.shiftKey && !imeBusy(e)) {
    e.preventDefault();
    e.target.form.dataset.queue = e.metaKey || e.ctrlKey ? "1" : "";   // ⌘↵: 추가하고 「응답 끝나면 보내기」 예약까지
    e.target.form.requestSubmit();
  }
  if (e.key === "ArrowDown" && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey && !imeBusy(e) &&
      caretOnLastLine(e.target)) {
    e.preventDefault();
    const col = e.target.closest(".col");
    const items = noteItems(col);
    if (items.length) focusNote(items[0]); else toColumnState(col.dataset.col);
  }
});
document.addEventListener("submit", async e => {
  e.preventDefault();
  const form = e.target;
  const input = form.querySelector(".note-input");
  if (!input) return;
  const text = input.value.trim();
  const images = S.noteImgs.get(form.dataset.sid) || [];
  if (!text && !images.length) return;
  const body = { text, images };
  if (form.dataset.form === "col-note") body.session_id = form.dataset.sid;
  const queue = form.dataset.queue === "1" && !!body.session_id;
  form.dataset.queue = "";
  input.value = "";
  S.noteImgs.delete(form.dataset.sid);
  renderNoteImages(form.dataset.sid);
  try {
    const r = await api("POST", "/api/notes", body);
    if (queue) {
      await api("PATCH", "/api/notes/" + r.id, { queued: true });
      const s = sessionById(form.dataset.sid);
      toast(s && ["done", "idle"].includes(s.state) ? "추가하고 예약했습니다 — 지금 쉬고 있어 곧 보냅니다" : "추가하고 예약했습니다 — 응답이 끝나면 보냅니다");
    }
  } catch (err) {
    input.value = text;
    S.noteImgs.set(form.dataset.sid, images);
    renderNoteImages(form.dataset.sid);
    return toast(err.message);
  }
  await refresh(form.dataset.sid);
  const again = visibleFirst(`form[data-form="col-note"][data-sid="${CSS.escape(form.dataset.sid)}"] .note-input`);
  if (again) again.focus();
});

$("#showEnded").checked = S.showEnded;
$("#showEnded").addEventListener("change", e => { S.showEnded = e.target.checked; save("csl.showEnded", S.showEnded); poll(); });

function renderToggles() {
  const granted = "Notification" in window && Notification.permission === "granted";
  const nb = $("#notifyBtn");
  nb.textContent = S.notify && granted ? "🔔 알림 켜짐" : "🔕 알림 꺼짐";
  nb.classList.toggle("on", S.notify && granted);
  const sb = $("#soundBtn");
  sb.textContent = S.sound ? "🔊 소리" : "🔈 무음";
  sb.classList.toggle("on", S.sound);
}
$("#notifyBtn").addEventListener("click", async () => {
  if (!("Notification" in window)) return toast("이 브라우저는 알림을 지원하지 않습니다");
  if (Notification.permission === "denied") return toast("브라우저 사이트 설정에서 알림을 허용해 주세요");
  if (Notification.permission !== "granted") {
    const p = await Notification.requestPermission();
    S.notify = p === "granted";
  } else {
    S.notify = !S.notify;
  }
  save("csl.notify", S.notify);
  renderToggles();
  if (S.notify) new Notification("Argus", { body: "새 응답·확인 필요를 이렇게 알려 드립니다." });
});
$("#soundBtn").addEventListener("click", () => {
  S.sound = !S.sound;
  save("csl.sound", S.sound);
  renderToggles();
  if (S.sound) chime();
});

// ------------------------------------------------------------------ 단축키
// 키는 글자 값이 아니라 키 위치(e.code)로 판별한다 — 한글 자판 상태에서도 동작하도록
const VIM_KEYS = { KeyH: "ArrowLeft", KeyJ: "ArrowDown", KeyK: "ArrowUp", KeyL: "ArrowRight" };
// 한글·일본어 입력기가 글자를 조합하는 중(Enter·방향키가 조합에 쓰인다). Safari 는 isComposing 대신 keyCode 229 만 준다
function imeBusy(e) { return e.isComposing || e.keyCode === 229; }
function isTyping() {
  const a = document.activeElement;
  return !!a && (/INPUT|TEXTAREA|SELECT/.test(a.tagName) || a.isContentEditable);
}
// 이 상자 안에서 글을 쓰는 중인가 — 폴링 다시 그리기가 포커스·커서·IME 조합을 날리고, 포커스가 body 로 빠진 뒤
// 이어 치는 글자가 전역 단축키(s·a·d …)로 실행되던 것을 막는다. 쓰는 동안은 다시 그리기를 미룬다
function typingIn(box) { const a = document.activeElement; return !!box && !!a && box.contains(a) && isTyping(); }
// 다시 그려도 펼쳐 둔 <details>(답장 초안·보낼 프롬프트)는 펼친 채로 — 카드 id + 카드 안 몇 번째인지로 찾는다
function openDetails(box) {
  return new Set([...box.querySelectorAll("details[open]")].map(d => {
    const card = d.closest("[data-id]");
    return card ? card.dataset.id + ":" + [...card.querySelectorAll("details")].indexOf(d) : "";
  }));
}
function reopenDetails(box, keys) {
  if (!keys.size) return;
  box.querySelectorAll("[data-id]").forEach(card => card.querySelectorAll("details").forEach((d, i) => {
    if (keys.has(card.dataset.id + ":" + i)) d.open = true;
  }));
}
function setActive(sid, opts = {}) {
  if (!sid) return null;
  S.activeCol = sid;
  document.querySelectorAll(".col.active").forEach(c => { if (c.dataset.col !== sid) c.classList.remove("active"); });
  const col = colEl(sid);
  if (col) {
    col.classList.add("active");
    if (opts.scroll) col.scrollIntoView({ inline: "nearest", block: "nearest", behavior: "smooth" });
  }
  return col;
}
// 지금 칸 크게 보기 — 단계: 0 평소 → 1 보드 너비의 반 → 2 보드 가득(다른 칸은 숨김) → 0. ⌘1~9·← → 로 칸을 바꾸면
// 크게 보는 칸도 따라 바뀐다. 지금 칸이 없으면 평소 보드 그대로 보인다(CSS :has). 새로 고쳐도 이어지게 브라우저에 기억
const ZOOM_KEY = "csl.colZoom";
try { S.colZoom = Math.min(2, Math.max(0, +localStorage.getItem(ZOOM_KEY) || 0)); } catch (e) { S.colZoom = 0; }
function zoomClasses() {
  document.body.classList.toggle("col-zoom", S.colZoom > 0);
  document.body.classList.toggle("col-zoom-half", S.colZoom === 1);
  document.body.classList.toggle("col-zoom-full", S.colZoom === 2);
}
zoomClasses();
function setColZoom(level) {
  S.colZoom = level;
  try { localStorage.setItem(ZOOM_KEY, String(level)); } catch (e) { /* 저장소 없음 */ }
  zoomClasses();
  if (level && S.view === "board" && S.boardMode === "cards") setBoardMode("cols");
  const sid = S.activeCol && colEl(S.activeCol) ? S.activeCol : colByRank(1);
  if (sid) setActive(sid, { scroll: true });
}
function colByRank(rank) {
  const s = boardSessions()[rank - 1];
  return s ? s.session_id : null;
}
function activeRank() {
  const i = boardSessions().findIndex(s => s.session_id === S.activeCol);
  return i < 0 ? 0 : i + 1;
}
async function gotoRank(rank) {
  const sid = colByRank(rank);
  if (!sid) return toast(rank + "번 칸이 없습니다");
  if (isTyping()) document.activeElement.blur();
  await focusCol(sid);
}
function focusIn(selector) {
  const sid = S.activeCol || colByRank(1);
  if (!sid) return;
  const col = setActive(sid, { scroll: true });
  // 보드 칸: 문서 칸은 AI 요청칸, 터미널 칸은 터미널로
  if (isBoard(sid) && selector === ".cmp-text") {
    if (col && col.classList.contains("term-col")) return focusTermSoon(sid);
    const ask = col && col.querySelector(".md-ask-text");
    if (ask) ask.focus({ preventScroll: true });
    return;
  }
  const el = col && col.querySelector(selector);
  if (el && !el.disabled) el.focus({ preventScroll: true });
  else if (el && el.disabled) toast("지금은 입력할 수 없는 상태입니다");
}

document.addEventListener("keydown", e => {
  // ⌘/⌃/⌥ + 1~9: N번 칸으로(입력 중에도). Chrome 은 탭 전환(⌘+숫자) 같은 예약되지 않은 단축키를 페이지에 먼저 주므로
  // preventDefault 하면 보드가 가진다. ⌃·⌥ 도 같이 받는다
  // 입력창 쪽 처리기가 이미 처리한 키(예: 입력창 첫 줄 ↑, 넥스트 액션 입력칸 마지막 줄 ↓)는 다시 받지 않는다
  if (e.defaultPrevented) return;
  const digit = /^Digit([1-9])$/.exec(e.code);
  if (digit && (e.metaKey || e.ctrlKey || e.altKey) && !e.shiftKey) {
    e.preventDefault();
    return gotoRank(+digit[1]);
  }
  if (noteKey(e)) return;
  if (imeBusy(e)) return;   // 일본어·한글 조합 중의 Esc 는 변환 취소 — 입력창에서 빠지거나 메뉴를 닫지 않는다
  if (e.key === "Escape") {
    // 위에 뜬 것부터 하나만 닫는다. 같은 Esc 로 펼친 카드까지 접히지 않게 뒤 처리기로 넘기지 않는다
    if (DRAG.on) { endDrag(true); e.stopImmediatePropagation(); return; }
    const open = ["#resumeModal", "#xMenu", "#taskModal", "#diffModal", "#colMenu", "#ctxMenu", "#newMenu", "#projMenu", "#topMenu", "#notifMenu", "#help", "#updateModal", "#settings", "#connectors"].find(id => !$(id).hidden);
    if (open) { $(open).hidden = true; e.stopImmediatePropagation(); return; }
    if (document.activeElement === $("#search") || !$("#searchPanel").hidden) { closeSearch(); e.stopImmediatePropagation(); return; }
    // 입력창에서 빠져나오기(쓰던 글은 그대로). 메모 고치기·이름 바꾸기는 각자 Esc 를 처리한다
    const a = document.activeElement;
    if (a && (a.classList.contains("cmp-text") || a.classList.contains("cmp-rich") || a.classList.contains("md-edit") || a.classList.contains("md-ask-text") || (a.form && a.form.dataset.form === "col-note"))) {
      const col = a.closest(".col");
      if (col && col.dataset.col) { toColumnState(col.dataset.col); if (S.expandedCard === col.dataset.col) setCardSel(col.dataset.col); }
      else a.blur();
      e.stopImmediatePropagation();   // 같은 Esc 로 펼친 카드까지 접히지 않게(한 번 더 누르면 접힌다)
      return;
    }
    // 이 칸만 크게 보던 중이면 평소 보드로(입력창에서는 위에서 먼저 빠져나오고, 한 번 더 누르면)
    if (S.colZoom && S.view === "board" && !isTyping()) { setColZoom(0); e.stopImmediatePropagation(); return; }
    return;
  }
  if (isTyping() || e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
  // 보기 전환: S 세션 · I 인박스 · A 활동
  if (e.code === "KeyA" && !e.shiftKey) { e.preventDefault(); return toggleNotif(); }   // 알림 드롭다운(전체 활동은 그 맨 아래)
  const view = { KeyS: "board", KeyI: "inbox" }[e.code];
  if (view === "board" && !e.shiftKey && S.view === "board") { e.preventDefault(); return setBoardMode(S.boardMode === "cards" ? "cols" : "cards"); }
  if (view && !e.shiftKey) { e.preventDefault(); return setView(view); }
  // ⇧T: 라이트 ↔ 다크(지금 보이는 것의 반대로 고정). 시스템 설정 따르기로 돌리려면 ⋮ 메뉴에서
  if (e.code === "KeyT" && e.shiftKey) { e.preventDefault(); return setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"); }
  // vim 이동: h ← · j ↓ · k ↑ · l → (Shift 없이)
  const code = (!e.shiftKey && VIM_KEYS[e.code]) || e.code;
  // 카드 보기: 방향키·hjkl 로 카드 고르기, Enter 로 펼쳐 대화. T·P 는 고른 카드에
  if (S.view === "board" && S.boardMode === "cards") {
    if (code.startsWith("Arrow")) { e.preventDefault(); return moveCardSel(code); }
    if (code === "Enter") { e.preventDefault(); return enterCardSel(); }
    if (["KeyC", "KeyN", "KeyF"].includes(code)) return;   // 칸 안의 입력칸·검색은 칸 보기에서만
  }
  // 칸을 다루는 키는 세션 보기에서만(인박스·활동 화면에서 누르면 숨은 칸이 바뀌지 않게)
  if (S.view !== "board" && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "KeyC", "Enter", "KeyN", "KeyT", "KeyF", "KeyP", "KeyZ"].includes(code)) return;
  switch (code) {
    case "ArrowLeft":
    case "ArrowRight": {
      e.preventDefault();
      const n = boardSessions().slice(0, S.boardLimit).length;
      const cur = activeRank() || 1;
      const next = code === "ArrowLeft" ? Math.max(1, cur - (S.activeCol ? 1 : 0)) : Math.min(n, cur + (S.activeCol ? 1 : 0));
      return gotoRank(next);
    }
    case "ArrowUp":
      e.preventDefault();
      return goUpFromColumn(S.activeCol || colByRank(1));
    case "KeyC":
    case "Enter":
    case "ArrowDown":
      e.preventDefault();
      return focusIn(".cmp-text");
    case "KeyN":
      e.preventDefault();
      if (S.activeCol && S.notesFolded.has(S.activeCol)) toggleNotes(S.activeCol);   // 접혀 있으면 펴고 입력칸으로
      return focusIn('form[data-form="col-note"] .note-input');
    case "KeyT": {
      e.preventDefault();
      return focusTerminal(S.activeCol);
    }
    case "KeyF":
      e.preventDefault();
      return openSearch(S.activeCol || null);
    case "KeyP":
      e.preventDefault();
      if (S.activeCol) return togglePin(S.activeCol);
      return toast("먼저 칸을 고르세요(⌘1~9 또는 ← →)");
    case "KeyZ":
      e.preventDefault();
      if (e.shiftKey) return;
      return setColZoom((S.colZoom + 1) % 3);
    case "KeyD":
      e.preventDefault();
      return openDaily();
    case "KeyB":
      e.preventDefault();
      return setBrainOpen(!S.brainOpen);
    case "Backquote":
      e.preventDefault();
      return setTermPanelOpen(!TP.open);
    case "KeyG":
      e.preventDefault();
      return openSwitcher();
    case "Slash":
      e.preventDefault();
      if (e.shiftKey) { $("#help").hidden = false; return; }   // ?
      return openSearch(null);
  }
});
// 칸 안을 누르거나 입력창에 들어가면 그 칸이 「지금 칸」
document.addEventListener("focusin", e => {
  S.noteFocus = e.target.matches && e.target.matches("li.note") ? e.target.dataset.nid : null;
  const col = e.target.closest && e.target.closest(".col");
  if (col) setActive(col.dataset.col);
  // 다른 탭·앱에 갔다 돌아오면 브라우저가 입력창에 포커스를 되돌려 focusin 이 다시 온다. 그건 들어온 게 아니라
  // 원래 있던 것이니 확인 처리하지 않는다(그 경우엔 타이핑해야 확인)
  const refocus = e.target === S.winBlurEl;
  S.winBlurEl = null;
  if (col && e.target.classList.contains("cmp-text") && !refocus) markSeen(col);
  // 입력창에 들어오면 터미널 입력칸을 다시 읽는다(그사이 터미널에서 쳤거나 지웠을 수 있다)
  if (col && e.target.classList.contains("cmp-text")) {
    const g = S.sugg.get(col.dataset.col);
    if (g && (!g.at || Date.now() - g.at > 3000)) fetchSuggestion(col.dataset.col);
  }
});
window.addEventListener("blur", () => { S.winBlurEl = document.activeElement; });
document.addEventListener("pointerdown", e => {
  const col = e.target.closest && e.target.closest(".col");
  if (col) setActive(col.dataset.col);
});
$("#helpBtn").addEventListener("click", () => { $("#help").hidden = false; });

// ------------------------------------------------------------------ 보드 터미널(xterm.js)
// 보드 터미널(tmux -L argus-web)에서 도는 세션은 칸 안에서 터미널을 연다. 서버의 /api/term/<세션>/ws 웹소켓이
// `tmux attach` 를 PTY 로 띄워 바이트를 그대로 잇는다(같은 출처·서버를 띄울 때마다 바뀌는 토큰만). 화면은 vendor/xterm
const TERM_THEME = { background: "#1c1b19", foreground: "#e8e4dc", cursor: "#e0805c", cursorAccent: "#1c1b19",
  selectionBackground: "rgba(224,128,92,.35)" };
function loadXterm() {
  if (!TERM.lib) {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = "/vendor/xterm/xterm.css";
    document.head.append(css);
    const js = src => new Promise((ok, no) => {
      const el = document.createElement("script");
      el.src = src;
      el.onload = ok;
      el.onerror = () => no(new Error("터미널 화면(xterm.js)을 불러오지 못했습니다"));
      document.head.append(el);
    });
    TERM.lib = js("/vendor/xterm/xterm.js").then(() => js("/vendor/xterm/addon-fit.js"));
    TERM.lib.catch(() => { TERM.lib = null; });
  }
  return TERM.lib;
}
async function termToken(fresh) {
  if (fresh || !TERM.token) TERM.token = (await api("GET", "/api/term/token")).token;
  return TERM.token;
}
function saveTermOpen() { save("csl.termOpen", [...TERM.open]); }
function toggleTerm(sid) {
  if (TERM.open.has(sid)) TERM.open.delete(sid); else TERM.open.add(sid);
  saveTermOpen();
  S.uiVer++;
  renderBoard();
  if (S.brainOpen && (sessionById(sid) || {}).brain) renderBrain();   // 브레인 칸은 보드가 아니라 브레인 패널에 있다
  if (TERM.open.has(sid)) focusTermSoon(sid);
}
function openTermIn(sid) {
  if (!TERM.open.has(sid)) { TERM.open.add(sid); saveTermOpen(); S.uiVer++; renderBoard(); }
  focusCol(sid).then(() => focusTermSoon(sid));
}
function focusTermSoon(sid) {   // 터미널이 붙을 때까지(스크립트·연결) 잠깐 기다렸다가 포커스
  let n = 0;
  const tick = () => {
    const col = colEl(sid), rec = col && TERM.live.get(col.querySelector(".col-term"));
    if (rec && rec.term) return rec.term.focus();
    if (++n < 30) setTimeout(tick, 100);
  };
  tick();
}
// 칸을 그릴 때마다: 켜 둔 보드 터미널 세션이면 붙이고, 아니면 뗀다(칸이 사라진 터미널도 정리)
function syncTerm(col, s) {
  for (const box of [...TERM.live.keys()]) if (!box.isConnected) disposeTerm(box);
  const on = !!(s.term_attach && s.state !== "ended" && TERM.open.has(s.session_id));
  col.classList.toggle("term-mode", on);
  const box = col.querySelector(".col-term");
  if (!box) return;
  box.hidden = !on;
  const cur = TERM.live.get(box);
  if (cur && (!on || cur.sid !== s.session_id)) disposeTerm(box);
  if (on && !TERM.live.has(box)) mountTerm(box, { sid: s.session_id });
}
async function mountTerm(box, q) {   // q: { sid } · 막 띄운 세션의 { handle, kind } · 터미널 패널 셸 탭의 { shell }
  const rec = { sid: q.sid || null, handle: q.handle || null, kind: q.kind || "web", shell: q.shell || null, board: !!q.board, term: null, fit: null, ws: null, closed: false, tries: 0 };
  TERM.live.set(box, rec);
  setHtml(box, `<div class="term-status muted">터미널에 연결하는 중…</div>`);
  try { await loadXterm(); await termToken(); }
  catch (e) { setHtml(box, `<div class="term-status">${esc(e.message)}</div>`); return; }
  if (TERM.live.get(box) !== rec) return;   // 그사이 뗐다
  box.textContent = "";
  const host = document.createElement("div");
  host.className = "term-host";
  box.append(host);
  const mono = getComputedStyle(document.documentElement).getPropertyValue("--mono").trim() || "Menlo, monospace";
  const term = new Terminal({ fontFamily: mono, fontSize: 12.5, cursorBlink: true, macOptionIsMeta: true, scrollback: 2000, theme: TERM_THEME });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(host);
  try { fit.fit(); } catch (e) { /* 아직 크기가 없다 */ }
  Object.assign(rec, { term, fit });
  const enc = new TextEncoder();
  const send = data => { if (rec.ws && rec.ws.readyState === 1) rec.ws.send(data); };
  term.onData(d => send(enc.encode(d)));
  term.onBinary(d => send(Uint8Array.from(d, c => c.charCodeAt(0) & 255)));
  term.onResize(({ cols, rows }) => send(JSON.stringify({ resize: [cols, rows] })));
  rec.ro = new ResizeObserver(() => { if (box.offsetParent) try { fit.fit(); } catch (e) { /* 숨김 */ } });
  rec.ro.observe(box);
  connectTerm(box, rec);
}
function connectTerm(box, rec) {
  const old = box.querySelector(".term-gone");
  if (old) old.remove();
  const p = new URLSearchParams({ token: TERM.token, cols: rec.term.cols, rows: rec.term.rows });
  if (rec.handle) { p.set("handle", rec.handle); p.set("term", rec.kind); }
  if (rec.shell) p.set("name", rec.shell);
  const ws = new WebSocket(`ws://${location.host}/api/term/${encodeURIComponent(rec.shell ? "shell" : rec.sid || "launch")}/ws?${p}`);
  ws.binaryType = "arraybuffer";
  let opened = false;
  ws.onopen = () => { opened = true; rec.tries = 0; };
  ws.onmessage = ev => rec.term.write(new Uint8Array(ev.data));
  ws.onclose = async () => {
    if (rec.closed || TERM.live.get(box) !== rec) return;
    // 셸 탭에서 exit 하면 tmux 세션이 끝나 연결이 닫힌다 — 목록을 다시 읽어 그 탭을 닫는다(다시 붙지 않는다)
    if (rec.board) refresh();   // 보드의 터미널 칸: exit 했으면 다음 폴링에서 「셸이 닫혔습니다」로 바뀐다
    if (rec.shell && !rec.board) {
      const list = await refreshShells().catch(() => null);
      if (rec.closed || TERM.live.get(box) !== rec || (list && !list.some(x => x.name === rec.shell))) return;
    }
    // 서버를 다시 띄우면 토큰이 바뀐다 — 새 토큰으로 몇 번 다시 붙어 본다. 그래도 안 되면 버튼
    if (rec.tries++ < 3) {
      await new Promise(r => setTimeout(r, opened ? 300 : 1500));
      try { await termToken(true); } catch (e) { /* 서버가 아직 안 떴다 */ }
      if (!rec.closed && TERM.live.get(box) === rec) return connectTerm(box, rec);
    }
    const gone = document.createElement("div");
    gone.className = "term-gone";
    gone.innerHTML = `<span>터미널 연결이 끊겼습니다</span><button class="btn" data-action="term-reconnect">다시 연결</button>`;
    box.append(gone);
  };
  rec.ws = ws;
}
function disposeTerm(box) {
  const rec = TERM.live.get(box);
  if (!rec) return;
  TERM.live.delete(box);
  rec.closed = true;
  if (rec.ro) rec.ro.disconnect();
  if (rec.ws) try { rec.ws.close(); } catch (e) { /* 이미 닫힘 */ }
  if (rec.term) rec.term.dispose();
  box.textContent = "";
}
// 막 띄운 보드 터미널 세션: 훅이 세션을 기록하기 전(폴더 신뢰 확인 창 등)에는 칸이 없으니 창에서 보여 준다
function openLaunchTerm(handle, kind) {
  const m = $("#termModal"), box = m.querySelector(".col-term");
  disposeTerm(box);
  m.hidden = false;
  box.hidden = false;
  mountTerm(box, { handle, kind: kind || "web" }).then(() => { const r = TERM.live.get(box); if (r && r.term) r.term.focus(); });
}
function closeLaunchTerm() {
  const m = $("#termModal");
  disposeTerm(m.querySelector(".col-term"));
  m.hidden = true;
}
$("#termModal").addEventListener("click", e => { if (e.target.closest("[data-term-close]")) closeLaunchTerm(); });
document.addEventListener("click", e => {
  const ib = e.target.closest("[data-action='import-open']");
  if (ib) {
    const btn = colEl(ib.dataset.sid) && colEl(ib.dataset.sid).querySelector(".col-menu-btn");
    if (!btn) return;
    if ($("#colMenu").hidden || $("#colMenu").dataset.sid !== ib.dataset.sid) openColMenu(btn);
    const item = $("#colMenu").querySelector('[data-cm="import"]');
    if (item) item.click();
    return;
  }
  const b = e.target.closest("[data-action='term-toggle'], [data-action='term-reconnect']");
  if (!b) return;
  if (b.dataset.action === "term-toggle") return toggleTerm(b.dataset.sid);
  const box = b.closest(".col-term"), rec = box && TERM.live.get(box);
  if (rec) { rec.tries = 0; termToken(true).catch(() => {}).finally(() => connectTerm(box, rec)); }
});

// ------------------------------------------------------------------ 터미널 패널(오른쪽 서랍) — 셸 탭 여러 개
// 보드 터미널 서버(tmux -L argus-web)에 shell-… 세션으로 로그인 셸을 띄우고 탭마다 xterm 을 붙인다.
// 셸은 서버 쪽에 남으므로 새로고침·서버 재시작 뒤에도 탭이 그대로다. 만들기·닫기·목록은 토큰 헤더(X-Argus-Term)가 있어야 한다
const TP = { open: load("csl.termPanel", false) === true, tabs: [], cur: load("csl.termTab", null), boxes: new Map(), busy: false };
async function termApi(method, path, body) {
  const call = async fresh => {
    const opt = { method, headers: { "X-Argus-Term": await termToken(fresh) } };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    return fetch(path, opt);
  };
  let res = await call(false);
  if (res.status === 403) res = await call(true);   // 서버를 다시 띄워 토큰이 바뀌었다
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${method} ${path} → ${res.status}`);
  return data;
}
const SHELLS = new Set(["zsh", "-zsh", "bash", "-bash", "fish", "-fish", "sh", "-sh", "login"]);
function shellLabel(x, i) {
  const dir = !x.path ? "" : /^\/Users\/[^/]+\/?$/.test(x.path) ? "~" : x.path.split("/").filter(Boolean).pop() || "/";
  const cmd = SHELLS.has(x.command) ? "" : x.command;
  return `${i + 1} ${cmd ? cmd + " · " : ""}${dir}`;
}
function setTermPanelOpen(open, instant) {
  TP.open = open;
  save("csl.termPanel", open);
  const layout = $("#layout"), panel = $("#termPanel");
  clearTimeout(setTermPanelOpen.timer);
  if (instant) layout.classList.add("no-anim");
  if (open) {
    panel.hidden = false;
    void panel.offsetWidth;
    layout.classList.add("term-open");
  } else {
    layout.classList.remove("term-open");
    if (panel.contains(document.activeElement)) document.activeElement.blur();
    setTermPanelOpen.timer = setTimeout(() => { if (!TP.open) panel.hidden = true; }, instant ? 0 : 260);
  }
  if (instant) requestAnimationFrame(() => requestAnimationFrame(() => layout.classList.remove("no-anim")));
  $("#termBtn").classList.toggle("on", open);
  if (open) refreshShells().then(list => list && !list.length && !instant ? newShell() : showShell(TP.cur, !instant)).catch(e => toast(e.message));
}
// 서버의 셸 목록에 맞춰 탭·터미널을 맞춘다(닫힌 셸의 탭은 치우고, 고른 탭이 없으면 마지막 탭)
async function refreshShells() {
  const list = (await termApi("GET", "/api/shells")).shells || [];
  TP.tabs = list;
  const names = new Set(list.map(x => x.name));
  for (const [name, box] of TP.boxes) if (!names.has(name)) { disposeTerm(box); box.remove(); TP.boxes.delete(name); }
  if (!names.has(TP.cur)) { TP.cur = list.length ? list[list.length - 1].name : null; save("csl.termTab", TP.cur); }
  drawShellTabs();
  if (TP.open) showShell(TP.cur, false);
  return list;
}
function drawShellTabs() {
  const html = TP.tabs.map((x, i) => `<button class="tp-tab ${x.name === TP.cur ? "on" : ""}" role="tab" data-shell="${esc(x.name)}" title="${esc(x.path || "")}">
    <span class="tp-name">${esc(shellLabel(x, i))}</span><span class="tp-x" data-shell-close="${esc(x.name)}" title="이 탭 닫기(셸 종료)">×</span></button>`).join("");
  if ($("#tpTabs").dataset.html !== html) { $("#tpTabs").dataset.html = html; setHtml($("#tpTabs"), html); }
  const body = $("#tpBody");
  let empty = body.querySelector(".tp-empty");
  if (!TP.tabs.length && !empty) {
    empty = document.createElement("div");
    empty.className = "tp-empty";
    empty.innerHTML = `열린 터미널이 없습니다<br><button class="btn" data-shell-new>＋ 새 터미널</button>`;
    body.append(empty);
  } else if (TP.tabs.length && empty) empty.remove();
}
function showShell(name, focus) {
  TP.cur = name || null;
  save("csl.termTab", TP.cur);
  drawShellTabs();
  for (const [n, box] of TP.boxes) box.hidden = n !== TP.cur;
  if (!TP.cur || !TP.open) return;
  let box = TP.boxes.get(TP.cur);
  if (!box) {
    box = document.createElement("div");
    box.className = "col-term";
    box.dataset.shell = TP.cur;
    $("#tpBody").append(box);
    TP.boxes.set(TP.cur, box);
  }
  const rec = TERM.live.get(box);
  if (!rec) return void mountTerm(box, { shell: TP.cur }).then(() => { const r = TERM.live.get(box); if (focus && r && r.term) r.term.focus(); });
  requestAnimationFrame(() => { try { rec.fit && rec.fit.fit(); } catch (e) { /* 크기 없음 */ } if (focus && rec.term) rec.term.focus(); });
}
async function newShell() {
  if (TP.busy) return;
  TP.busy = true;
  try {
    const r = await termApi("POST", "/api/shells", {});
    TP.cur = r.name;
    await refreshShells();
    showShell(r.name, true);
  } catch (e) { toast(e.message); }
  finally { TP.busy = false; }
}
async function closeShell(name) {
  try { await termApi("DELETE", "/api/shells/" + encodeURIComponent(name)); } catch (e) { toast(e.message); }
  if (TP.cur === name) {   // 닫은 탭 옆(앞쪽 우선)으로
    const i = TP.tabs.findIndex(x => x.name === name), next = TP.tabs[i - 1] || TP.tabs[i + 1];
    TP.cur = next ? next.name : null;
  }
  await refreshShells().catch(() => {});
  showShell(TP.cur, true);
}
$("#termBtn").addEventListener("click", () => setTermPanelOpen(!TP.open));
$("#tpClose").addEventListener("click", () => setTermPanelOpen(false));
$("#tpAdd").addEventListener("click", newShell);
$("#termPanel").addEventListener("click", e => {
  const x = e.target.closest("[data-shell-close]");
  if (x) { e.stopPropagation(); return closeShell(x.dataset.shellClose); }
  if (e.target.closest("[data-shell-new]")) return newShell();
  const t = e.target.closest(".tp-tab");
  if (t) showShell(t.dataset.shell, true);
});
$("#tpTabs").addEventListener("auxclick", e => {   // 가운데 버튼으로 탭 닫기
  const t = e.button === 1 && e.target.closest(".tp-tab");
  if (t) { e.preventDefault(); closeShell(t.dataset.shell); }
});
// ⌃` : 어디서든(터미널 안에서도) 패널 여닫기. 열 때는 터미널에 바로 포커스
document.addEventListener("keydown", e => {
  if (e.code !== "Backquote" || !e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
  e.preventDefault();
  e.stopPropagation();
  setTermPanelOpen(!TP.open);
}, true);
// 열려 있는 동안 탭 이름(지금 도는 명령·폴더)을 몇 초마다 새로
setInterval(() => { if (TP.open && !document.hidden) refreshShells().catch(() => {}); }, 4000);
// 패널 너비: 왼쪽 가장자리를 끌어 조절(브라우저에 기억), 더블클릭이면 기본 너비
const TP_MIN = 320;
function applyTermWidth(w) {
  const layout = $("#layout");
  if (w) layout.style.setProperty("--tp-w", Math.round(w) + "px"); else layout.style.removeProperty("--tp-w");
}
applyTermWidth(load("csl.termWidth", null));
const TPR = { on: false, x0: 0, w0: 0 };
$("#tpResize").addEventListener("pointerdown", e => {
  if (e.button !== 0) return;
  e.preventDefault();
  Object.assign(TPR, { on: true, x0: e.clientX, w0: $("#termPanel").getBoundingClientRect().width });
  e.target.setPointerCapture(e.pointerId);
  document.body.classList.add("tp-resizing");
});
$("#tpResize").addEventListener("pointermove", e => {
  if (!TPR.on) return;
  applyTermWidth(Math.max(TP_MIN, Math.min(window.innerWidth - 360, TPR.w0 - (e.clientX - TPR.x0))));
});
function endTermResize() {
  if (!TPR.on) return;
  TPR.on = false;
  document.body.classList.remove("tp-resizing");
  save("csl.termWidth", Math.round($("#termPanel").getBoundingClientRect().width));
}
$("#tpResize").addEventListener("pointerup", endTermResize);
$("#tpResize").addEventListener("pointercancel", endTermResize);
$("#tpResize").addEventListener("dblclick", () => {
  applyTermWidth(null);
  save("csl.termWidth", null);
  toast("터미널 패널을 기본 너비로 되돌렸습니다");
});
if (TP.open) setTermPanelOpen(true, true);

// ------------------------------------------------------------------ 커넥터 창
// 바깥 서비스(슬랙·Gmail …)마다 인스턴스를 두고 키·설정을 넣는다. 종류는 csl/connectors/*.py 플러그인에서 온다
const CONN = { data: null, form: null, confirmDel: null };
async function openConnectors() {
  $("#connectors").hidden = false;
  CONN.form = null;
  await loadConnectors();
}
async function loadConnectors() {
  try { CONN.data = await api("GET", "/api/connectors"); } catch (e) { return toast(e.message); }
  drawConnectors();
}
function drawConnectors() {
  const d = CONN.data;
  if (!d) return;
  const list = d.instances.map(x => `<div class="conn-row ${x.enabled ? "" : "off"}">
    <span class="conn-icon">${x.icon}</span>
    <div class="conn-main"><b class="cn-name">${esc(x.name)}</b> <span class="muted">${esc(x.type_name)}${x.account && x.account !== x.name ? " · " + esc(x.account) : ""}</span>
      <div class="conn-status muted">${x.role === "info" ? `정보 전용 · 브레인이 필요할 때 조회${x.enabled ? "" : " · 꺼짐"}${x.error ? ` · <span class="bp-feed-err">오류: ${esc(x.error)}</span>` : ""}`
        : x.error ? `<span class="bp-feed-err">오류: ${esc(x.error)}</span>`
        : (x.polled_at ? `수집 ${rel(x.polled_at)}${x.last ? ` · ${hm(x.last)}까지` : ""}${x.pending ? ` · <b>대기 ${x.pending}</b>` : ""}` : "아직 수집 전") +
          ` · ${x.enabled ? (x.every ? `${x.every}분마다` : "자동 수집 끔") : "꺼짐"}`}</div></div>
    <div class="conn-acts">
      ${x.role === "info" ? "" : `<button class="btn" data-conn="poll" data-id="${x.id}" ${x.enabled ? "" : "disabled"}>지금 가져오기</button>`}
      <button class="btn" data-conn="test" data-id="${x.id}">연결 확인</button>
      <button class="btn" data-conn="edit" data-id="${x.id}">설정</button>
      <button class="btn ghost" data-conn="toggle" data-id="${x.id}">${x.enabled ? "끄기" : "켜기"}</button>
      <button class="btn ghost ${CONN.confirmDel === x.id ? "danger" : ""}" data-conn="del" data-id="${x.id}">${CONN.confirmDel === x.id ? "정말 삭제" : "삭제"}</button>
    </div></div>`).join("") || '<div class="muted conn-empty">아직 연결한 커넥터가 없습니다.</div>';
  const add = `<h3 class="conn-h">커넥터 추가</h3><div class="conn-types">${d.types.map(t => `<button class="conn-type" data-conn="new" data-type="${t.type}">
    <span class="conn-icon">${t.icon}</span><b>${esc(t.name)}</b><span class="muted">${esc(t.description)}</span></button>`).join("")}</div>
    <p class="muted conn-hint">새 종류는 <code>csl/connectors/</code> 에 파일 하나(플러그인)를 넣으면 여기에 나옵니다.</p>`;
  $("#connBody").innerHTML = (CONN.form ? connFormHtml() : "") + `<h3 class="conn-h">연결된 커넥터</h3>${list}` + add;
}
function connFormHtml() {
  const f = CONN.form, t = CONN.data.types.find(x => x.type === f.type);
  const inst = f.id && CONN.data.instances.find(x => x.id === f.id);
  const val = k => inst ? (inst.config || {})[k] : undefined;
  const fields = t.fields.map(fd => {
    const id = "connf-" + fd.key, v = val(fd.key) ?? fd.default;
    const ctl = fd.kind === "secret"
      ? `<input type="password" id="${id}" autocomplete="off" spellcheck="false" ${fd.prefix ? `data-prefix="${esc(fd.prefix)}"` : ""} placeholder="${inst && inst.secrets[fd.key] ? esc(inst.secrets[fd.key] === "저장됨" ? "저장됨 — 바꿀 때만 입력" : inst.secrets[fd.key]) : fd.prefix ? `${esc(fd.prefix)}… 붙여 넣기 (또는 keychain:항목이름)` : "붙여 넣기 (또는 keychain:항목이름)"}">` +
        (fd.prefix && navigator.clipboard && navigator.clipboard.readText ? `<button class="btn" data-conn="paste" data-key="${fd.key}" title="복사해 둔 토큰을 넣고 바로 연결 확인">클립보드에서 붙여넣기</button>` : "")
      : fd.kind === "bool" ? `<input type="checkbox" id="${id}" ${v ? "checked" : ""}>`
      : fd.kind === "int" ? `<input type="number" id="${id}" value="${esc(String(v ?? ""))}">`
      : `<input type="text" id="${id}" value="${esc(String(v ?? ""))}" spellcheck="false">`;
    return `<div class="cfg-row wide"><label for="${id}"><b>${esc(fd.label)}</b>${fd.help ? `<span class="muted">${esc(fd.help)}</span>` : ""}</label><div class="cfg-ctl">${ctl}</div></div>`;
  }).join("");
  const su = t.setup, links = su ? (su.links || []).map(([label, url]) =>
    `<a class="linkish" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`).join(" · ") : "";
  const guide = !su ? "" : inst ? (links ? `<div class="conn-setup-links muted">${links}</div>` : "")
    : `<div class="conn-setup"><div class="conn-setup-head"><b>빠른 연결</b>
        <a class="btn primary" href="${esc(su.url)}" target="_blank" rel="noopener noreferrer">${esc(su.button || "열기 ↗")}</a></div>
      <ol>${(su.steps || []).map(x => `<li>${esc(x)}</li>`).join("")}</ol>${links ? `<div class="conn-setup-links muted">${links}</div>` : ""}</div>`;
  return `<section class="conn-form"><h3 class="conn-h">${t.icon} ${esc(t.name)} ${inst ? "설정" : "추가"}</h3>${guide}
    <div class="cfg-row"><label for="connf-name"><b>이름</b><span class="muted">목록과 인박스에 보이는 이름(예: 워크스페이스·계정)</span></label>
      <div class="cfg-ctl"><input type="text" id="connf-name" value="${esc(inst ? inst.name : t.name)}"></div></div>
    ${t.role === "info" ? `<input type="hidden" id="connf-every" value="0">` : `<div class="cfg-row"><label for="connf-every"><b>수집 간격(분)</b><span class="muted">0 이면 「지금 가져오기」로만</span></label>
      <div class="cfg-ctl"><input type="number" id="connf-every" min="0" max="1440" value="${inst ? inst.every : t.every}"></div></div>`}
    ${fields}
    <div class="settings-foot"><span class="muted" id="connMsg"></span><button class="btn" data-conn="cancel">취소</button>
      <button class="btn primary" data-conn="save">${inst ? "저장" : "추가하고 연결 확인"}</button></div></section>`;
}
async function connAction(btn) {
  const a = btn.dataset.conn, id = btn.dataset.id;
  if (a !== "del") CONN.confirmDel = null;
  if (a === "new") { CONN.form = { type: btn.dataset.type }; drawConnectors(); return $("#connf-name").focus(); }
  if (a === "edit") { const x = CONN.data.instances.find(i => i.id === id); CONN.form = { type: x.type, id }; drawConnectors(); return; }
  if (a === "cancel") { CONN.form = null; return drawConnectors(); }
  if (a === "del") {
    if (CONN.confirmDel !== id) { CONN.confirmDel = id; return drawConnectors(); }
    await api("DELETE", "/api/connectors/" + id);
    CONN.confirmDel = null;
    toast("커넥터를 지웠습니다");
    return loadConnectors();
  }
  if (a === "toggle") {
    const x = CONN.data.instances.find(i => i.id === id);
    await api("POST", "/api/connectors", { id, type: x.type, enabled: !x.enabled });
    return loadConnectors();
  }
  if (a === "poll" || a === "test") {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "…";
    try {
      const r = await api("POST", `/api/connectors/${id}/${a}`, {});
      toast(a === "test" ? "연결됨 · " + r.message : `가져왔습니다 · 새 항목 ${r.added}개`);
    } catch (e) { toast(e.message); }
    btn.textContent = label;
    loadInbox(true);
    return loadConnectors();
  }
  if (a === "paste") {   // 복사해 둔 토큰을 칸에 넣고, 모양이 맞으면 바로 저장·연결 확인
    const el = $("#connf-" + btn.dataset.key);
    let v = "";
    try { v = (await navigator.clipboard.readText()).trim(); } catch (e) { $("#connMsg").textContent = "클립보드를 읽지 못했습니다 — 칸에 직접 붙여 넣어 주세요"; return el.focus(); }
    el.value = v;
    return connSecretReady(el);
  }
  if (a === "save") {
    const f = CONN.form, t = CONN.data.types.find(x => x.type === f.type);
    const bad = [...$("#connBody").querySelectorAll("input[data-prefix]")].map(connPrefixError).find(Boolean);
    if (bad) { $("#connMsg").textContent = bad; return; }
    const config = {};
    for (const fd of t.fields) {
      const el = $("#connf-" + fd.key);
      config[fd.key] = fd.kind === "bool" ? el.checked : fd.kind === "int" ? Number(el.value) : el.value;
    }
    const msg = $("#connMsg");
    if (btn.disabled) return;   // 저장·연결 확인 중에 또 누르면 id 없이 한 번 더 가서 같은 커넥터가 둘 생겼다
    btn.disabled = true;
    try {
      const isNew = !f.id;
      const r = await api("POST", "/api/connectors", { id: f.id, type: f.type, name: $("#connf-name").value, every: Number($("#connf-every").value), config });
      f.id = r.id;   // 이후 저장은 같은 커넥터를 고친다
      if (r.missing.length) { msg.textContent = "아직 비어 있는 칸: " + r.missing.join(", "); return loadConnectors(); }
      if (isNew) {   // 새로 넣었으면 바로 연결 확인
        try { const t2 = await api("POST", `/api/connectors/${r.id}/test`, {}); toast("연결됨 · " + t2.message); }
        catch (e) { toast("저장했지만 연결 확인에 실패했습니다: " + e.message); }
      } else toast("저장했습니다");
      CONN.form = null;
      loadConnectors();
    } catch (e) { msg.textContent = e.message; }
    finally { btn.disabled = false; }
  }
}
// 토큰 칸: 정해진 앞머리(xoxp- 등)가 아니면 무엇이 틀렸는지 알려 준다. keychain:항목이름 은 그대로 둔다
function connPrefixError(el) {
  const v = el.value.trim(), p = el.dataset.prefix;
  if (!v || !p || v.startsWith(p) || v.startsWith("keychain:")) return "";
  if (p === "xoxp-" && /^xoxb-/.test(v)) return "Bot 토큰(xoxb-)입니다 — 같은 화면의 「User OAuth Token」(xoxp-…)을 넣어 주세요";
  return `${p}… 로 시작하는 값이 아닙니다 — 복사한 값을 확인해 주세요`;
}
// 새 커넥터에 토큰을 붙여 넣으면(모양이 맞을 때) 「추가하고 연결 확인」까지 바로
function connSecretReady(el) {
  const err = connPrefixError(el), msg = $("#connMsg");
  if (msg) msg.textContent = err;
  if (err || !el.value.trim().startsWith(el.dataset.prefix) || (CONN.form && CONN.form.id)) return;
  const save = $("#connBody [data-conn='save']");
  if (save) connAction(save);
}
$("#connectors").addEventListener("input", e => {
  const el = e.target.closest("input[data-prefix]");
  if (!el) return;
  if (e.inputType === "insertFromPaste") connSecretReady(el);
  else if (!connPrefixError(el)) $("#connMsg").textContent = "";
});
$("#connectorsBtn").addEventListener("click", openConnectors);
$("#connectors").addEventListener("click", e => {
  if (e.target.closest("[data-conn-close]") || e.target === $("#connectors")) { $("#connectors").hidden = true; return; }
  const b = e.target.closest("[data-conn]");
  if (b) connAction(b);
});

// ------------------------------------------------------------------ 설정 창
// 서버의 설정 목록(csl/config.py)을 그대로 그린다. 저장하면 하나라도 틀린 값이 있을 때 아무것도 바꾸지 않는다
let CFG = [];
// 「몇 시」 설정은 이 컴퓨터 시각 기준이다(서버가 이 Mac 의 시계로 판단) — 옆에 그 시각과 UTC 를 같이 보여 준다
const HOUR_KEYS = new Set(["daily_hour", "brain_hour_from", "brain_hour_to"]);
function hourHint(h) {
  h = Number(h);
  if (!Number.isFinite(h)) return "";
  const off = -new Date().getTimezoneOffset() / 60;   // 예: 한국 +9
  const two = n => String(n).padStart(2, "0");
  const fmt = x => two(((x % 24) + 24) % 24) + ":00";
  const u = h - off;
  const shift = u < 0 ? "전날" : u >= 24 ? "다음 날" : "";
  const tz = "UTC" + (off >= 0 ? "+" : "−") + Math.abs(off);
  return `→ <b>${h === 24 ? "24:00" : fmt(h)}</b> <span>이 컴퓨터 시각(${tz}) ·</span> <b>UTC ${fmt(u)}</b>${shift ? ` <span class="muted">${shift}</span>` : ""}`;
}
document.addEventListener("input", e => {
  const k = e.target.id && e.target.id.startsWith("cfg-") ? e.target.id.slice(4) : "";
  if (!HOUR_KEYS.has(k)) return;
  const hint = document.querySelector(`.cfg-time[data-hour-for="${k}"]`);
  if (hint) hint.innerHTML = hourHint(e.target.value);
});
async function openSettings() {
  try { CFG = (await api("GET", "/api/config")).items; } catch (e) { return toast(e.message); }
  const groups = [...new Set(CFG.map(c => c.group))];
  const ctl = c => {
    const id = "cfg-" + c.key;
    if (c.type === "bool") return `<input type="checkbox" id="${id}" ${c.value ? "checked" : ""}>`;
    if (c.type === "choice") {
      return `<select id="${id}">${c.options.map(o => `<option value="${esc(String(o))}" ${String(o) === String(c.value) ? "selected" : ""}>${
        o === "" ? "기본(설정대로)" : o === 0 && c.key === "brain_slack_every" ? "끔" : esc(String(o))}</option>`).join("")}</select>`;
    }
    if (c.type === "int") return `<input type="number" id="${id}" value="${c.value}" min="${c.options[0]}" max="${c.options[1]}">`;
    return `<input type="text" id="${id}" class="cfg-path" value="${esc(c.value)}" spellcheck="false">`;
  };
  $("#settingsBody").innerHTML = groups.map(g => `<section class="cfg-group"><h3>${esc(g)}</h3>${CFG.filter(c => c.group === g).map(c => `
    <div class="cfg-row ${c.type === "path" ? "wide" : ""}">
      <label for="cfg-${c.key}"><b>${esc(c.label)}</b>${HOUR_KEYS.has(c.key) ? `<span class="cfg-time" data-hour-for="${c.key}">${hourHint(c.value)}</span>` : ""}${c.help ? `<span class="muted">${esc(c.help)}</span>` : ""}</label>
      <div class="cfg-ctl">${ctl(c)}${String(c.value) !== String(c.default) ? `<button class="linkish" data-cfg-reset="${c.key}" title="기본값: ${esc(String(c.default === "" ? "기본(설정대로)" : c.default))}">기본값</button>` : ""}</div>
    </div>`).join("")}</section>`).join("") +
    `<section class="cfg-group"><h3>화면</h3><div class="cfg-row"><label for="cfg-lang"><b>언어</b><span class="muted">이 브라우저에만 적용됩니다</span></label>
      <div class="cfg-ctl"><select id="cfg-lang">${Object.entries(I18N_LANGS).map(([k, v]) => `<option value="${k}" ${k === LANG ? "selected" : ""}>${v}</option>`).join("")}</select></div></div>
      ${[["uiFont", "화면 글자 크기", "메뉴·칸 머리·카드 같은 화면 전체 — 바로 적용, 이 브라우저에만"], ["chatFont", "대화 글자 크기", "칸의 대화와 입력창 — 화면 글자 크기와 따로. 바로 적용, 이 브라우저에만"]].map(([k, l, h]) => {
        const cur = load("csl." + k, 1);
        return `<div class="cfg-row"><label for="cfg-${k}"><b>${l}</b><span class="muted">${h}</span></label>
          <div class="cfg-ctl"><select id="cfg-${k}" data-font-scale="${k}">${FONT_STEPS.map(v => `<option value="${v}" ${v === cur ? "selected" : ""}>${Math.round(v * 100)}%${v === 1 ? " (기본)" : ""}</option>`).join("")}</select></div></div>`;
      }).join("")}</section>`;
  $("#settingsMsg").textContent = "";
  $("#settings").hidden = false;
}
async function saveSettings() {
  const body = {};
  for (const c of CFG) {
    const el = $("#cfg-" + c.key);
    if (!el) continue;
    const v = c.type === "bool" ? el.checked : c.type === "int" ? Number(el.value) : el.value;
    if (String(v) !== String(c.value)) body[c.key] = v;
  }
  const lang = $("#cfg-lang").value;
  try {
    if (Object.keys(body).length) await api("POST", "/api/config", body);
  } catch (e) {
    $("#settingsMsg").textContent = e.message;
    return;
  }
  if (body.board_cols) S.boardLimit = body.board_cols;
  if (lang !== LANG) { save("csl.lang", lang); location.reload(); return; }
  $("#settings").hidden = true;
  toast(Object.keys(body).length ? "설정을 저장했습니다" : "바뀐 것이 없습니다");
  refresh();
}
$("#settingsBtn").addEventListener("click", openSettings);

// ------------------------------------------------------------------ 업데이트: GitHub(원격)에 받을 변경·새 릴리스가 있는지 확인하고 받기
// 받기는 fast-forward 만(커밋 안 한 변경·올리지 않은 커밋이 있으면 서버가 거절). 받으면 서버가 새 코드로 다시 뜨고 화면을 새로 고친다.
// 하루에 한 번은 보드를 열 때 조용히 확인해 ⋮ 버튼에 점을 찍는다
const UPD_KEY = "csl.updateCheck";
function updHtml(r) {
  if (!r.ok) return `<p class="upd-err">${esc(r.error || "확인하지 못했습니다")}</p>`;
  const when = r.checked_at ? `${dateLabel(r.checked_at)} ${hm(r.checked_at)}` : "";
  const rel = r.new_release ? `<p class="upd-rel">🎉 새 릴리스 <b>${esc(r.latest_tag)}</b> (지금 ${esc(r.current_tag || "태그 없음")})</p>` : "";
  const list = r.commits.length ? `<ul class="upd-commits">${r.commits.map(c => `<li><code>${esc(c.hash)}</code> ${esc(c.subject)} <span class="muted">${dateLabel(c.at)}</span></li>`).join("")}</ul>` : "";
  const state = r.behind ? `받을 변경 <b>${r.behind}개</b>` : "이미 최신입니다";
  return `<p>${state} <span class="muted">· ${esc(r.branch)} ← ${esc(r.upstream)} · 지금 ${esc(r.head)}${r.current_tag ? " (" + esc(r.current_tag) + ")" : ""} · ${esc(when)} 확인</span></p>
    ${rel}${list}${r.behind && !r.can_apply ? `<p class="upd-err">받을 수 없습니다 — ${esc(r.reason)}. 이 폴더에서 정리한 뒤 다시 확인해 주세요</p>` : ""}`;
}
function updMark(r) {
  const has = !!(r && r.ok && (r.behind || r.new_release));
  $("#updDot").hidden = !has;
  $("#topMenuBtn").classList.toggle("has-update", has);
}
async function checkUpdate(fetch) {
  const r = await api("GET", "/api/update" + (fetch ? "" : "?fetch=0"));
  save(UPD_KEY, { at: Date.now(), behind: r.behind || 0, rel: !!r.new_release, ok: !!r.ok });
  updMark(r);
  return r;
}
async function openUpdate() {
  const m = $("#updateModal");
  m.hidden = false;
  $("#updApply").hidden = true;
  $("#updMsg").textContent = "";
  setHtml($("#updBody"), '<p class="muted">GitHub 에서 확인하는 중…</p>');
  let r;
  try { r = await checkUpdate(true); } catch (e) { r = { ok: false, error: e.message }; }
  setHtml($("#updBody"), updHtml(r));
  $("#updApply").hidden = !(r.ok && r.behind && r.can_apply);
}
async function applyUpdate() {
  const b = $("#updApply");
  b.disabled = true;
  $("#updMsg").textContent = "받는 중…";
  try {
    const r = await api("POST", "/api/update", {});
    $("#updMsg").textContent = r.message;
    // 서버가 새 코드로 다시 뜬다 — 다시 응답하면 화면을 새로 고친다(새 화면 파일도 받게)
    const t0 = Date.now();
    await new Promise(res => setTimeout(res, 1800));
    while (Date.now() - t0 < 30000) {
      try { await api("GET", "/api/update?fetch=0"); location.reload(); return; } catch (e) { await new Promise(res => setTimeout(res, 800)); }
    }
    $("#updMsg").textContent = "보드가 다시 뜨지 않았습니다 — ./ctl.sh open 으로 띄워 주세요";
  } catch (e) {
    $("#updMsg").textContent = e.message;
    b.disabled = false;
  }
}
$("#updateBtn").addEventListener("click", openUpdate);
$("#updApply").addEventListener("click", applyUpdate);
$("#updateModal").addEventListener("click", e => {
  if (e.target.id === "updateModal" || e.target.closest("[data-upd-close]")) $("#updateModal").hidden = true;
  if (e.target.closest("[data-upd-check]")) openUpdate();
});
// 하루에 한 번 조용히 확인(마지막 결과는 바로 점으로)
(() => {
  const last = load(UPD_KEY, null);
  if (last) updMark({ ok: last.ok, behind: last.behind, new_release: last.rel });
  if (!last || Date.now() - last.at > 24 * 3600 * 1000) setTimeout(() => checkUpdate(true).catch(() => {}), 8000);
})();
$("#settingsSave").addEventListener("click", saveSettings);
$("#settings").addEventListener("change", e => {
  const k = e.target.dataset && e.target.dataset.fontScale;
  if (!k) return;
  save("csl." + k, Number(e.target.value));
  applyFontScale();
});
$("#settings").addEventListener("click", e => {
  if (e.target.closest("[data-settings-close]") || e.target === $("#settings")) $("#settings").hidden = true;
  const r = e.target.closest("[data-cfg-reset]");
  if (r) {
    const c = CFG.find(x => x.key === r.dataset.cfgReset), el = $("#cfg-" + c.key);
    if (c.type === "bool") el.checked = !!c.default; else el.value = String(c.default);
  }
});
$("#topMenuBtn").addEventListener("click", () => { $("#topMenu").hidden = !$("#topMenu").hidden; });
$("#topMenu").addEventListener("click", e => { if (e.target.closest("[data-tm-close]")) $("#topMenu").hidden = true; });
$("#help").addEventListener("click", e => { if (e.target === $("#help") || e.target.closest("[data-close]")) $("#help").hidden = true; });

// ------------------------------------------------------------------ 대화 검색(요청·응답 전문)
const SR = { q: "", sid: null, who: "all", results: [], sessions: [], sel: 0, seq: 0, timer: null };
function openSearch(sid) {
  SR.sid = sid;
  const input = $("#search");
  input.focus();
  input.select();
  runSearch();
}
function closeSearch() {
  $("#searchPanel").hidden = true;
  $("#search").blur();
}
function scheduleSearch() {
  clearTimeout(SR.timer);
  SR.timer = setTimeout(runSearch, 180);
}
async function runSearch() {
  SR.q = $("#search").value.trim();
  const seq = ++SR.seq;
  if (!SR.q) { SR.results = []; SR.sessions = []; SR.sel = 0; return renderSearch(); }
  const words = SR.q.toLowerCase().split(/\s+/);
  // 세션 제목·프로젝트·브랜치가 맞는 칸(전체 검색일 때만)
  SR.sessions = SR.sid ? [] : boardSessions().filter(s =>
    words.every(w => [sessionTitle(s), s.project_name, s.branch, wtName(s)].join(" ").toLowerCase().includes(w))).slice(0, 5);
  const p = new URLSearchParams({ q: SR.q, who: SR.who });
  if (SR.sid) p.set("sid", SR.sid);
  SR.error = "";
  // 서버가 잠깐 다시 뜨는 중이면 결과가 통째로 비어 보였다 — 한 번 더 묻고, 그래도 안 되면 「찾지 못함」이 아니라 오류로 보여 준다
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const r = await api("GET", "/api/search?" + p);
      if (seq !== SR.seq) return;   // 더 새 검색이 있으면 버린다
      SR.results = r.results;
      SR.error = "";
      break;
    } catch (e) {
      SR.results = [];
      SR.error = "검색 서버에 연결하지 못했습니다 — 잠시 뒤 다시 입력해 주세요";
      await new Promise(res => setTimeout(res, 800));
      if (seq !== SR.seq) return;
    }
  }
  SR.sel = 0;
  renderSearch();
}
function markWords(text, q) {
  // 원문에서 위치를 찾고 조각마다 이스케이프한다 — 이스케이프한 HTML 에 정규식을 걸면 「lt」가 &lt; 를, 「mark」가 태그를 깨뜨렸다
  text = String(text || "");
  const low = text.toLowerCase(), on = new Array(text.length).fill(false);
  for (const w of q.toLowerCase().split(/\s+/).filter(Boolean)) {
    for (let i = low.indexOf(w); i >= 0; i = low.indexOf(w, i + w.length)) on.fill(true, i, i + w.length);
  }
  let out = "";
  for (let i = 0; i < text.length;) {
    let j = i;
    while (j < text.length && on[j] === on[i]) j++;
    out += on[i] ? "<mark>" + esc(text.slice(i, j)) + "</mark>" : esc(text.slice(i, j));
    i = j;
  }
  return out;
}
function searchItems() {
  return [...SR.sessions.map(s => ({ kind: "session", s })), ...SR.results.map(r => ({ kind: "turn", r }))];
}
function renderSearch() {
  const panel = $("#searchPanel");
  const scopeS = SR.sid && sessionById(SR.sid);
  const who = [["all", "전체"], ["me", "내 요청"], ["claude", "Claude 응답"]]
    .map(([k, l]) => `<button class="sr-chip ${SR.who === k ? "on" : ""}" data-sr-who="${k}">${l}</button>`).join("");
  let html = `<div class="sr-head">${who}
    ${SR.sid ? `<span class="sr-scope" title="이 세션 안에서만 찾는 중">이 세션: ${esc(clamp(scopeS ? sessionTitle(scopeS) : SR.sid, 24))}
      <button data-sr-scope-clear title="전체에서 찾기">×</button></span>` : ""}
    <span class="spacer"></span><span class="muted sr-keys">↑↓ 고르기 · Enter 열기 · Esc 닫기</span></div>`;
  const items = searchItems();
  if (!SR.q) html += '<div class="sr-empty">내가 보낸 요청이나 Claude 응답에서 찾을 말을 입력하세요.</div>';
  else if (!items.length) html += '<div class="sr-empty">찾은 대화가 없습니다.</div>';
  items.forEach((it, i) => {
    if (it.kind === "session") {
      const s = it.s;
      html += `<div class="sr-item ${i === SR.sel ? "sel" : ""}" data-sr-idx="${i}" style="--proj:${projColor(s.project_root || s.cwd)}">
        <div class="sr-meta"><span class="chip proj">${esc(s.project_name || "")}</span><span class="sr-who">세션</span></div>
        <div class="sr-text">${markWords(sessionTitle(s), SR.q)}</div></div>`;
    } else {
      const r = it.r;
      html += `<div class="sr-item ${i === SR.sel ? "sel" : ""}" data-sr-idx="${i}" style="--proj:${projColor(r.project_root)}">
        <div class="sr-meta"><span class="chip proj">${esc(r.project_name || "")}</span>
          <span class="sr-title">${esc(clamp(r.title || "(제목 없음)", 40))}</span>
          <span class="sr-who ${r.in === "prompt" ? "me" : ""}">${r.in === "prompt" ? "나" : "Claude"}</span>
          <span class="muted">${r.at ? dateLabel(r.at) + " " + hm(r.at) : ""}</span></div>
        <div class="sr-text">${markWords(r.snippet, SR.q)}</div></div>`;
    }
  });
  if (SR.results.length >= 60) html += '<div class="sr-empty">최근 60개까지 보여 줍니다. 단어를 더 넣어 좁혀 보세요.</div>';
  if (SR.error) html += `<div class="sr-empty sr-error">${esc(SR.error)}</div>`;
  panel.innerHTML = html;
  panel.hidden = false;
  const sel = panel.querySelector(".sr-item.sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}
async function openSearchItem(i) {
  const it = searchItems()[i];
  if (!it) return;
  closeSearch();
  if (it.kind === "session") { await focusCol(it.s.session_id); return; }
  await jumpToTurn(it.r, SR.q);   // 종료·보관된 세션이면 그 칸만 잠깐 보드에 꺼낸다
}
// 보드의 그 세션 칸에서 그 턴으로(검색·활동 화면). r: { session_id, turn_id, from_end, in }
async function jumpToTurn(r, words) {
  if (S.view !== "board") setView("board");
  if (!boardSessions().some(s => s.session_id === r.session_id) && !S.showEnded) {
    S.reveal = new Set([r.session_id]);   // 설정을 바꾸지 않고 이 세션만 잠깐
    await poll();
  }
  const col = await focusCol(r.session_id);
  if (!col) return;
  if (!r.turn_id) return;
  // 그 턴까지 불러오기(보드는 칸마다 최근 N턴만 불러온다)
  const need = r.from_end + 5;
  if (need > (S.colLimit.get(r.session_id) || BOARD_TURNS)) {
    S.colLimit.set(r.session_id, need);
    S.boardCache.delete(r.session_id);
  }
  // 응답·요청이 접혀 있으면 펼쳐서 찾은 말이 보이게
  (r.in === "response" ? S.expanded : S.promptOpen).add(r.turn_id);
  S.uiVer++;
  S.visibleCols.add(r.session_id);
  if (col.closest("#bpChat")) await renderBrain();   // 브레인 대화는 브레인 패널이 그린다
  else { await refreshBoard(); renderBoard(); }
  let turn = null;
  for (let k = 0; k < 30 && !(turn = col.querySelector(`.turn[data-tid="${r.turn_id}"]`)); k++) {
    await new Promise(res => setTimeout(res, 100));
  }
  if (!turn) return toast("그 대화를 불러오지 못했습니다");
  const th = col.querySelector(".col-thread");
  th._pinned = false;
  const place = () => { th.scrollTop += turn.getBoundingClientRect().top - th.getBoundingClientRect().top - 40; return th.scrollTop; };
  let set = place();
  if (!col.closest("#bpChat")) ensureColVisible(r.session_id);
  turn.classList.remove("hit"); void turn.offsetWidth; turn.classList.add("hit");
  if (words) highlightWords(turn, words);
  // 위쪽 이미지·코드가 늦게 그려지면 그 턴이 아래로 밀린다 — 사용자가 스크롤하지 않았으면 잠깐 동안 다시 맞춘다
  for (const ms of [300, 700, 1500]) {
    await new Promise(res => setTimeout(res, ms));
    if (!turn.isConnected || Math.abs(th.scrollTop - set) > 4) break;
    const off = turn.getBoundingClientRect().top - th.getBoundingClientRect().top;
    if (Math.abs(off - 40) > 12) set = place();
    if (!col.closest("#bpChat")) ensureColVisible(r.session_id);
  }
}
// 찾은 말을 칸 안에서 노란색으로(CSS Custom Highlight — DOM 을 바꾸지 않는다)
function highlightWords(root, q) {
  if (!window.CSS || !CSS.highlights || typeof Highlight === "undefined") return;
  const ranges = [];
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    const t = n.data.toLowerCase();
    for (const w of words) {
      for (let i = t.indexOf(w); i >= 0; i = t.indexOf(w, i + w.length)) {
        const r = new Range();
        r.setStart(n, i); r.setEnd(n, i + w.length);
        ranges.push(r);
      }
    }
  }
  CSS.highlights.set("search-hit", new Highlight(...ranges));
  clearTimeout(highlightWords.timer);
  highlightWords.timer = setTimeout(() => CSS.highlights.delete("search-hit"), 20000);   // 20초 — 칸을 옮겨 다니며 봐도 남게
}
$("#search").addEventListener("input", scheduleSearch);
$("#search").addEventListener("focus", () => { if ($("#searchPanel").hidden) runSearch(); });
$("#search").addEventListener("keydown", e => {
  const n = searchItems().length;
  if (e.key === "ArrowDown") { e.preventDefault(); SR.sel = Math.min(n - 1, SR.sel + 1); renderSearch(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); SR.sel = Math.max(0, SR.sel - 1); renderSearch(); }
  else if (e.key === "Enter" && !imeBusy(e)) { e.preventDefault(); openSearchItem(SR.sel); }
});
$("#searchPanel").addEventListener("mousedown", e => e.preventDefault());   // 패널을 눌러도 검색칸 포커스 유지
$("#searchPanel").addEventListener("click", e => {
  const who = e.target.closest("[data-sr-who]");
  if (who) { SR.who = who.dataset.srWho; return runSearch(); }
  if (e.target.closest("[data-sr-scope-clear]")) { SR.sid = null; return runSearch(); }
  const item = e.target.closest("[data-sr-idx]");
  if (item) openSearchItem(+item.dataset.srIdx);
});

// 경과 시간·「N분 전」은 1초마다 브라우저 시계로 숫자만 갱신한다(다음 응답이 오면 서버 값으로 다시 맞춰짐)
function tickClock() {
  const now = Date.now() / 1000;
  document.querySelectorAll(".elapsed[data-since]").forEach(el => {
    const v = dur(now - +el.dataset.since);
    if (el.textContent !== v) el.textContent = v;
  });
  document.querySelectorAll(".ago[data-ts]").forEach(el => {
    const v = rel(+el.dataset.ts);
    if (el.textContent !== v) el.textContent = v;
  });
}

renderToggles();
// 화면 언어(i18n.js). 바꾸면 다시 불러온다
$("#langSel").innerHTML = Object.entries(I18N_LANGS).map(([k, v]) => `<option value="${k}" ${k === LANG ? "selected" : ""}>${v}</option>`).join("");
$("#langSel").addEventListener("change", e => { save("csl.lang", e.target.value); location.reload(); });
// 테마: 시스템 설정 따르기(기본) · 라이트 · 다크. <html data-theme> 에 실제로 쓸 것(light·dark)을 둔다 — 첫 그림은 index.html 의 머리 스크립트가
const THEME_MQ = matchMedia("(prefers-color-scheme: dark)");
function themePref() { const p = load("csl.theme", "system"); return ["light", "dark"].includes(p) ? p : "system"; }
function applyTheme() {
  const p = themePref();
  document.documentElement.dataset.theme = p === "dark" || (p === "system" && THEME_MQ.matches) ? "dark" : "light";
}
function setTheme(p) {
  save("csl.theme", p);
  $("#themeSel").value = p;
  applyTheme();
}
THEME_MQ.addEventListener("change", applyTheme);   // 시스템 설정을 따를 때 OS 가 바뀌면 바로
$("#themeSel").value = themePref();
$("#themeSel").addEventListener("change", e => setTheme(e.target.value));
applyTheme();
if (location.hash) history.replaceState(null, "", location.pathname);   // 예전 주소(#/s/…) 정리
(async () => {
  await poll();
  let polling = false;
  setInterval(async () => {
    if (polling) return;
    polling = true;
    try { await poll(); } finally { polling = false; }
  }, 2000);
  setInterval(tickClock, 1000);
})();


// ------------------------------------------------------------------ 보기 전환(세션 · 인박스 칸반 · 활동)
S.view = "board";
function setView(v) {
  if (!["board", "inbox", "activity"].includes(v)) v = "board";
  if (v !== "activity") S.act.hold = null;   // 다른 보기로 가면 붙잡아 둔 활동 행을 놓는다
  S.view = v;
  save("csl.view", v);
  document.body.classList.remove("view-board", "view-inbox", "view-activity");
  document.body.classList.add("view-" + v);
  applyBoardVisibility();   // 세션 보기: 칸 또는 카드
  $("#activity").hidden = v !== "activity";
  $("#kanban").hidden = v !== "inbox";
  document.querySelectorAll(".view-tab").forEach(b => b.classList.toggle("on", b.dataset.view === v));
  if (v === "activity") loadActivity(true);
  if (v === "inbox") { openInboxView(); loadKanban(true).then(markInboxSeen); }
}
$("#views").addEventListener("click", e => {
  const b = e.target.closest(".view-tab");
  if (b) setView(b.dataset.view);
});
let viewsAt = 0;
function refreshViews() {
  if (S.view === "board" && S.boardMode === "cards") renderCards();   // 펼친 카드 칸은 renderBoard 끝에서 갱신
  // 탭의 숫자(인박스 할 일, 안 본 활동)와 열린 보기 — 몇 초에 한 번만
  if (Date.now() - viewsAt < 4000) return;
  viewsAt = Date.now();

  api("GET", "/api/events?unseen=1&limit=99" + (S.projFilter ? "&project=" + encodeURIComponent(S.projFilter) : ""))
    .then(r => {
      // 보관(숨긴) 세션은 칸이 없어 눌러 볼 수 없다 — 세지 않는다
      const items = r.items.filter(x => !x.brain && !x.archived);
      S.unseenSids = new Set(items.map(x => x.session_id));
      const n = items.length;
      $("#vtActivity").textContent = n ? (n >= 99 ? "99+" : String(n)) : "";
      $("#notifBtn").classList.toggle("has", !!n);
    }).catch(() => {});
  if (S.view === "activity") loadActivity();
  if (S.view === "inbox") loadKanban().then(markInboxSeen);
  else if (inboxVisible()) markInboxSeen();
}

// ------------------------------------------------------------------ 알림(상단 🔔): 활동을 드롭다운으로
// 안 본 활동은 모두(최대 99, 새것 순), 안 본 것이 적으면 이미 본 최근 활동으로 채워 30개. 고르면 그 세션 칸의 그 대화로 가서 강조
const NOTIF = { items: [], sel: 0, open: false };
async function loadNotif() {
  const kinds = "&kind=" + EV_MAIN.join(",") + (S.projFilter ? "&project=" + encodeURIComponent(S.projFilter) : "");
  let unseen = [], recent = [];
  try {
    [unseen, recent] = await Promise.all([api("GET", "/api/events?unseen=1&limit=99" + kinds), api("GET", "/api/events?limit=30" + kinds)])
      .then(rs => rs.map(r => r.items.filter(x => !x.brain && !x.archived)));
  } catch (e) { /* 다음에 */ }
  const ids = new Set(unseen.map(x => x.id));
  NOTIF.items = unseen.concat(recent.filter(x => !ids.has(x.id)).slice(0, Math.max(0, 30 - unseen.length)));
  NOTIF.items.sort((a, b) => b.at - a.at || b.id - a.id);
}
function drawNotif() {
  const m = $("#notifMenu");
  const any = NOTIF.items.some(x => x.unseen);
  const rows = NOTIF.items.map((x, i) => {
    const [icon, label] = EV[x.kind] || ["•", x.kind];
    return `<button class="nt-row k-${x.kind} ${x.unseen ? "unseen" : ""} ${i === NOTIF.sel ? "sel" : ""}" data-nt="${i}" title="${esc(label)}">
      <span class="nt-icon">${icon}</span>
      <span class="nt-main"><span class="nt-top"><b class="nt-title">${esc(x.title || "(제목 없음)")}</b><span class="nt-time">${rel(x.at)}</span></span>
        <span class="nt-text">${esc(clamp(plainLine(x.text || label), 120))}</span></span>
      ${x.unseen ? '<span class="nt-dot" aria-label="안 봄"></span>' : ""}</button>`;
  }).join("");
  setHtml(m, `<div class="nt-head"><b>알림</b><span class="spacer"></span>
      ${any ? `<button class="btn ghost" data-nt-seen-all title="안 본 활동의 세션을 모두 본 것으로(보드의 「새 응답」도 풀림)">✓ 모두 읽음</button>` : ""}</div>
    <div class="nt-list">${rows || '<div class="nt-empty">알림이 없습니다</div>'}</div>
    <button class="nt-foot" data-nt-all>전체 활동 보기 →</button>`);
  const sel = m.querySelector(".nt-row.sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}
async function toggleNotif(force) {
  const m = $("#notifMenu");
  const open = force !== undefined ? force : m.hidden;
  if (!open) { m.hidden = true; $("#notifBtn").classList.remove("on"); return; }
  NOTIF.sel = 0;
  m.hidden = false;
  $("#notifBtn").classList.add("on");
  if (!NOTIF.items.length) setHtml(m, '<div class="nt-empty">불러오는 중…</div>');
  await loadNotif();
  if (!m.hidden) drawNotif();
}
// 알림 하나로: 본 것으로 표시하고, 그 턴이 있으면 칸에서 그 턴으로(노란 강조), 없으면(확인 필요·질문) 칸으로 가서 카드를 강조
async function gotoNotif(x) {
  toggleNotif(false);
  if (x.unseen) api("POST", `/api/sessions/${x.session_id}/seen`, {}).then(() => { viewsAt = 0; poll(); }).catch(() => {});
  if (x.turn_id) return jumpToTurn({ session_id: x.session_id, turn_id: x.turn_id, from_end: x.from_end || 1, in: "response" });
  if (S.view !== "board") setView("board");
  if (!boardSessions().some(s => s.session_id === x.session_id) && !S.showEnded) {
    S.reveal = new Set([x.session_id]);
    await poll();
  }
  const col = await focusCol(x.session_id);
  if (!col) return;
  const card = [...col.querySelectorAll(".ask-card, .prompt-card")].find(c => !c.hidden);
  const el = card || col.querySelector(".col-head");
  el.classList.remove("nt-hit"); void el.offsetWidth; el.classList.add("nt-hit");
  if (card) card.scrollIntoView({ block: "nearest" });
}
$("#notifBtn").addEventListener("click", () => toggleNotif());
$("#notifMenu").addEventListener("click", async e => {
  if (e.target.closest("[data-nt-all]")) { toggleNotif(false); return setView("activity"); }
  if (e.target.closest("[data-nt-seen-all]")) {
    try {
      const r = await api("POST", "/api/events/seen-all", S.projFilter ? { project: S.projFilter } : {});
      toast(`모두 읽음으로 표시했습니다 · 세션 ${r.sessions}개`);
    } catch (err) { toast(err.message); }
    viewsAt = 0;
    await poll();
    await loadNotif();
    return drawNotif();
  }
  const row = e.target.closest("[data-nt]");
  if (row) gotoNotif(NOTIF.items[+row.dataset.nt]);
});
// 펼친 동안: ↑↓ 고르기 · Enter 그 대화로 (Esc 는 위에 뜬 것 닫기에서)
document.addEventListener("keydown", e => {
  if ($("#notifMenu").hidden || isTyping() || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    e.stopImmediatePropagation();
    NOTIF.sel = Math.max(0, Math.min(NOTIF.items.length - 1, NOTIF.sel + (e.key === "ArrowDown" ? 1 : -1)));
    return drawNotif();
  }
  if (e.key === "Enter" && NOTIF.items[NOTIF.sel]) {
    e.preventDefault();
    e.stopImmediatePropagation();
    gotoNotif(NOTIF.items[NOTIF.sel]);
  }
}, true);
document.addEventListener("pointerdown", e => {   // 바깥을 누르면 닫는다
  if (!$("#notifMenu").hidden && !e.target.closest(".notif-wrap")) toggleNotif(false);
});

// ------------------------------------------------------------------ 활동: 모든 세션의 새 응답·확인 필요·질문·오류
const EV = {
  response: ["💬", "새 응답"], waiting: ["⏳", "확인 필요"], question: ["❓", "질문"], error: ["⚠", "오류"],
  compact: ["🗜", "컴팩트"], start: ["▶", "시작"], end: ["■", "종료"],
};
const EV_MAIN = ["response", "waiting", "question", "error"];
S.act = { kinds: load("csl.actKinds", EV_MAIN), unseen: load("csl.actUnseen", false), brain: load("csl.actBrain", false), items: [], sig: "" };
function plainLine(t) {
  return (t || "").replace(/```[\s\S]*?```/g, " ").replace(/[#*`>|_~]/g, "").replace(/\s+/g, " ").trim();
}
async function loadActivity(force) {
  const q = new URLSearchParams({ limit: "300" });
  if (S.act.kinds.length && S.act.kinds.length < Object.keys(EV).length) q.set("kind", S.act.kinds.join(","));
  if (S.act.unseen) q.set("unseen", "1");
  if (S.projFilter) q.set("project", S.projFilter);
  let r;
  try { r = await api("GET", "/api/events?" + q); } catch (e) { return; }
  let items = r.items.filter(x => S.act.brain || !x.brain);
  if (S.act.unseen && S.act.hold) {
    // 「안 본 것만」에서 행을 눌러 본 것이 됐어도, 활동 목록 바깥으로 나갈 때까지는 그 자리에 둔다(갑자기 사라지지 않게)
    const shown = new Set(items.map(x => x.id));
    for (const x of S.act.hold.values()) if (!shown.has(x.id)) items.push({ ...x, unseen: false });
    items.sort((p, q) => q.at - p.at || q.id - p.id);
    for (const x of items) S.act.hold.set(x.id, x);   // 그사이 새로 온 것도 보이는 동안은 붙잡는다
  }
  // 비교값엔 고른 조건만 — 예전엔 S.act 통째(직전 비교값 포함)를 넣어 늘 달라져 매번 다시 그리고 문자열도 계속 길어졌다
  const sig = JSON.stringify([items.map(x => [x.id, x.unseen]), S.act.kinds, S.act.unseen, S.act.brain, S.actOpen, !!S.act.hold]);
  if (!force && sig === S.act.sig) return;
  S.act.sig = sig;
  S.act.items = items;
  drawActivity();
}
// 활동을 끌어낸 내 요청 한 줄(문맥). 백그라운드 작업 알림이면 알림이라고
function actAskHtml(x) {
  // 꼬리표(나·백그라운드 작업)는 번역하고, 글(.act-ask-t)은 사용자·명령 글이라 그대로
  // 요청 글(사용자 글)은 번역하지 않는 .act-ask-t 안에만 — 마우스를 올리면 보이는 앞 300자도 거기에
  const ask = x.prompt ? `<span class="act-ask"><span class="act-ask-me">나</span><span class="act-ask-t" title="${esc(clamp(x.prompt, 300))}">${esc(clamp(plainLine(x.prompt), 160))}</span></span>` : "";
  // 이 응답이 백그라운드 작업 알림으로 시작됐으면: 내 요청 아래에 그 알림 한 줄
  const n = taskNotice(x.notice);
  const notice = n ? `<span class="act-ask notice">⚙ <span>백그라운드 작업 ${esc({ completed: "끝남", failed: "실패", killed: "중단", stopped: "중단" }[n.status.toLowerCase()] || n.status)}</span>${n.summary ? `<span class="act-ask-t"> · ${esc(clamp(n.summary, 120))}</span>` : ""}</span>` : "";
  return ask + notice;
}
function drawActivity() {
  const box = $("#activity");
  const chips = Object.keys(EV).map(k => `<button class="act-chip ${S.act.kinds.includes(k) ? "on" : ""} k-${k}" data-act-kind="${k}">${EV[k][0]} ${EV[k][1]}</button>`).join("");
  const head = `<div class="act-head"><div class="act-chips">${chips}</div><span class="spacer"></span>
    ${S.act.items.some(x => x.unseen) ? `<button class="btn ghost act-seen-all" data-act-seen-all title="지금 보이는 활동의 세션을 모두 본 것으로(보드의 「새 응답」도 풀림)">✓ 모두 읽음</button>` : ""}
    <label class="act-opt"><input type="checkbox" data-act-opt="unseen" ${S.act.unseen ? "checked" : ""}> 안 본 것만</label>
    <label class="act-opt"><input type="checkbox" data-act-opt="brain" ${S.act.brain ? "checked" : ""}> 브레인 포함</label>
    ${S.projFilter ? `<span class="muted act-proj">프로젝트: ${esc(base(S.projFilter))}</span>` : ""}</div>`;
  let day = "", rows = "";
  for (const x of S.act.items) {
    const d = dateLabel(x.at);
    if (d !== day) { day = d; rows += `<div class="act-day">${esc(d)}</div>`; }
    const [icon, label] = EV[x.kind] || ["•", x.kind];
    const where = [x.project_name, x.worktree && x.branch ? x.branch : ""].filter(Boolean).join(" · ");
    rows += `<button class="act-row k-${x.kind} ${x.unseen ? "unseen" : ""} ${S.actOpen === String(x.id) ? "open" : ""}" data-act-ev="${x.id}">
      <span class="act-time">${hm(x.at)}</span>
      <span class="act-kind">${icon} ${label}</span>
      <span class="act-main"><b>${esc(x.title || "(제목 없음)")}</b> <span class="muted">${esc(where)}</span>
        ${actAskHtml(x)}${x.text ? `<span class="act-text">${esc(clamp(plainLine(x.text), 220))}</span>` : ""}</span>
      ${x.unseen ? '<span class="act-dot" title="아직 안 봄"></span>' : ""}</button>` +
      (S.actOpen === String(x.id) ? `<div class="act-detail" data-act-detail="${x.id}">${actDetailHtml(x)}</div>` : "");
  }
  box.innerHTML = `<div class="act-wrap">${head}${rows || '<div class="bp-empty"><p>보여 줄 활동이 없습니다.</p></div>'}</div>`;
}
$("#activity").addEventListener("click", async e => {
  if (e.target.closest("[data-act-seen-all]")) {
    S.act.hold = null;
    try {
      const r = await api("POST", "/api/events/seen-all", S.projFilter ? { project: S.projFilter } : {});
      toast(`모두 읽음으로 표시했습니다 · 세션 ${r.sessions}개`);
    } catch (err) { toast(err.message); }
    viewsAt = 0;
    await poll();
    return loadActivity(true);
  }
  const chip = e.target.closest("[data-act-kind]");
  if (chip) {
    const k = chip.dataset.actKind, ks = new Set(S.act.kinds);
    ks.has(k) ? ks.delete(k) : ks.add(k);
    S.act.kinds = Object.keys(EV).filter(x => ks.has(x));
    S.act.hold = null;
    save("csl.actKinds", S.act.kinds);
    return loadActivity(true);
  }
  const go = e.target.closest("[data-act-go]");
  if (go) {   // 펼친 활동의 「⇆ 칸에서 보기」
    const x = S.act.items.find(i => String(i.id) === go.dataset.actGo);
    if (!x) return;
    if (x.turn_id) return jumpToTurn({ session_id: x.session_id, turn_id: x.turn_id, from_end: x.from_end || 1, in: "response" });
    setView("board");
    return focusCol(x.session_id);
  }
  const row = e.target.closest("[data-act-ev]");
  if (!row) return;
  // 「안 본 것만」: 지금 보이는 목록을 붙잡는다 — 펼치면 그 세션이 본 것이 되어 목록에서 빠지는데, 보는 중에 사라지면 안 되니
  if (S.act.unseen && !S.act.hold) S.act.hold = new Map(S.act.items.map(x => [x.id, x]));
  // 누르면 그 자리에서 아래로 펼쳐 그 턴(요청·응답)을 바로 본다. 다시 누르면 접힌다
  const id = row.dataset.actEv;
  S.actOpen = S.actOpen === id ? null : id;
  drawActivity();
  const x = S.act.items.find(i => String(i.id) === id);
  if (S.actOpen && x) openActDetail(x);
});
// 활동 펼침: 턴이 있으면 그 턴 전문(요청·중간 메시지·응답), 없으면(확인 필요·질문 등) 활동 글과 지금 상태
S.actOpen = null;
S.actDetail = {};
function actDetailHtml(x) {
  const go = `<div class="act-dacts"><span class="spacer"></span><button class="btn ghost" data-act-go="${x.id}" title="세션 칸 보기에서 이 대화로">⇆ 칸에서 보기</button></div>`;
  if (!x.turn_id) {
    const s = sessionById(x.session_id);
    return `<div class="act-dbody"><div class="md">${mdCached(x.text || "")}</div>${s && s.state === "waiting" && s.state_detail
      ? `<div class="sc-wait"><span aria-hidden="true">⏳</span> ${esc(s.state_detail)}</div>` : ""}</div>${go}`;
  }
  const d = S.actDetail[x.turn_id];
  if (!d) return `<div class="act-dbody muted"><span class="ibc-spin"></span> 불러오는 중…</div>${go}`;
  const t = d.turn;
  // 백그라운드 작업이 끝났을 때 Claude Code 가 넣는 알림은 요청이 아니다 — 칸에서처럼 알림 한 줄로
  const notice = taskNotice(t.prompt);
  return `<div class="act-dbody">
    ${notice ? noticeHtml(notice, t.prompt_at ? hm(t.prompt_at) : "")
      : t.prompt ? `<div class="act-dq"><span class="act-dlabel">요청</span><div class="md">${promptHtml(t)}</div></div>` : ""}
    ${(t.middle || []).map(m => `<div class="act-dmid md">${mdCached(m)}</div>`).join("")}
    ${t.response ? `<div class="act-da"><span class="act-dlabel">응답</span><div class="md">${mdCached(t.response)}</div></div>` : ""}
  </div>${go}`;
}
async function openActDetail(x) {
  if (x.unseen) {   // 펼쳐 봤으면 본 것(칸에서 본 것과 같게)
    api("POST", `/api/sessions/${x.session_id}/seen`, {}).then(() => { viewsAt = 0; poll(); }).catch(() => {});
  }
  if (!x.turn_id || S.actDetail[x.turn_id]) return;
  try { S.actDetail[x.turn_id] = await api("GET", `/api/turns/${x.turn_id}`); } catch (e) { S.actDetail[x.turn_id] = { turn: { response: e.message } }; }
  const box = document.querySelector(`[data-act-detail="${x.id}"]`);
  if (box) box.innerHTML = actDetailHtml(x);
}
$("#activity").addEventListener("change", e => {
  const o = e.target.dataset.actOpt;
  if (!o) return;
  S.act[o] = e.target.checked;
  S.act.hold = null;
  save(o === "unseen" ? "csl.actUnseen" : "csl.actBrain", e.target.checked);
  loadActivity(true);
});
// 활동 목록 바깥을 누르면(다른 곳으로 옮겨 가면) 붙잡아 둔 본 행들을 놓고 다시 거른다
function releaseActHold() {
  if (!S.act.hold) return;
  S.act.hold = null;
  if (S.view === "activity") loadActivity(true);
}
document.addEventListener("pointerdown", e => {
  if (S.act.hold && !(e.target.closest && e.target.closest("#activity"))) releaseActHold();
}, true);


// ------------------------------------------------------------------ 인박스 칸반(할 일 · 진행 중 · 완료), 같은 스레드는 한 묶음
const IB_COLS = [["todo", "할 일", "new"], ["doing", "진행 중", "doing"], ["reply", "답장만 남음", "reply"], ["done", "완료", "done"]];
function ibCol(x) {
  return x.status === "new" ? "todo" : (x.status === "doing" || x.status === "sent") ? "doing" : x.status === "reply" ? "reply" : "done";
}
S.kbOpen = new Set();   // 펼친 묶음(thread_key)
function kanbanGroups(items) {
  // 같은 thread_key 끼리 묶는다. 묶음의 열은 가장 덜 끝난 카드 기준(할 일 > 진행 중 > 완료), 대표 카드는 최신
  const by = new Map();
  for (const x of items) {
    const k = x.thread_key || "card:" + x.id;
    if (!by.has(k)) by.set(k, []);
    by.get(k).push(x);
  }
  const rank = { todo: 0, doing: 1, reply: 2, done: 3 };
  return [...by.entries()].map(([key, cards]) => {
    cards.sort((a, b) => (b.slack_at || 0) - (a.slack_at || 0));
    const col = cards.map(ibCol).sort((a, b) => rank[a] - rank[b])[0];
    const lead = cards.find(c => ibCol(c) === col) || cards[0];
    return { key, cards, col, lead, at: Math.max(...cards.map(c => c.slack_at || 0)) };
  }).sort((a, b) => b.at - a.at);
}
async function loadKanban(force) {
  // 받기만 새로 — 숨어 있는 브레인 패널 인박스는 바뀐 게 있을 때만 다시 그린다(5초마다 통째로 다시 그리던 것)
  if (force || Date.now() - S.inbox.at > 5000) await loadInbox(true, false);
  const sig = JSON.stringify(S.inbox.items.map(x => [x.id, x.status, x.updated_at])) + [...S.kbOpen].join() + [...S.chatOpen].join();
  if (!force && sig === S.kbSig) return;
  const box = $("#kanban");
  if (!force && typingIn(box)) return;   // 쓰는 중 — 다음 차례에
  S.kbSig = sig;
  const opened = openDetails(box);
  const drafts = {};
  box.querySelectorAll(".ib-prompt").forEach(t => { if (t.value !== t.defaultValue) drafts[t.className + "#" + t.dataset.id] = t.value; });
  box.querySelectorAll(".ibc-text").forEach(t => { S.chatDraft[t.dataset.id] = t.value; });
  const groups = kanbanGroups(S.inbox.items);
  const colHtml = ([col, label]) => {
    const gs = groups.filter(g => g.col === col);
    return `<div class="kb-col" data-kb-col="${col}"><div class="kb-col-head"><b>${label}</b> <span class="muted">${gs.length}</span>
        ${col === "done" ? `<span class="muted kb-note">${(S.settings || {}).done_cards_days || 1}일 뒤 사라짐</span>` : ""}</div>
      <div class="kb-list">${gs.map(g => kanbanGroupHtml(g)).join("") || '<div class="kb-empty muted">없음</div>'}</div></div>`;
  };
  box.innerHTML = `<div class="kb-wrap"><div class="kb-top muted">${S.inbox.checked ? "마지막 정리 " + dateLabel(S.inbox.checked) + " " + hm(S.inbox.checked) : ""}
      <button class="btn" data-action="inbox-run" title="정리 일꾼이 모인 항목을 읽어 카드로 만듭니다 — 브레인 대화에는 끼어들지 않습니다">지금 정리</button>
      <span class="muted">카드를 끌어 다른 열로 옮길 수 있습니다 · 같은 스레드의 카드는 한 묶음</span></div>
    <div class="ib-run-box">${inboxRunHtml()}</div>
    <div class="kb-cols">${IB_COLS.map(colHtml).join("")}</div></div>`;
  box.querySelectorAll(".ib-prompt").forEach(t => { const k = t.className + "#" + t.dataset.id; if (drafts[k] != null) t.value = drafts[k]; });
  reopenDetails(box, opened);
  for (const id of S.chatOpen) if (S.chat[id]) drawCardChat(id);
}
function kanbanGroupHtml(g) {
  const more = g.cards.filter(c => c !== g.lead);
  const open = S.kbOpen.has(g.key);
  const hist = more.length ? `<button class="kb-thread linkish" data-kb-toggle="${esc(g.key)}">${open ? "▾" : "▸"} 같은 스레드 ${g.cards.length}건</button>` +
    (open ? `<div class="kb-hist">${more.map(c => `<div class="kb-h ${ibCol(c)}"><span class="muted">${c.slack_at ? dateLabel(c.slack_at) + " " + hm(c.slack_at) : ""} · ${esc(c.author || "")}</span>
        <span class="kb-h-st">${{ todo: "할 일", doing: "진행 중", reply: "답장만 남음", done: "완료" }[ibCol(c)]}</span>
        <div>${esc(c.todo || c.summary || "")}</div></div>`).join("")}</div>` : "") : "";
  return `<div class="kb-group" draggable="true" data-kb-group="${esc(g.key)}">${inboxCardHtml(g.lead)}${hist}</div>`;
}
$("#kanban").addEventListener("click", e => {
  const t = e.target.closest("[data-kb-toggle]");
  if (!t) return;
  const k = t.dataset.kbToggle;
  S.kbOpen.has(k) ? S.kbOpen.delete(k) : S.kbOpen.add(k);
  loadKanban(true);
});
// 끌어 옮기기: 묶음 전체의 카드 상태를 바꾼다
$("#kanban").addEventListener("dragstart", e => {
  const g = e.target.closest && e.target.closest(".kb-group");
  if (!g || e.target.closest("textarea, input, select")) return;
  e.dataTransfer.setData("text/argus-group", g.dataset.kbGroup);
  e.dataTransfer.effectAllowed = "move";
  g.classList.add("dragging");
});
$("#kanban").addEventListener("dragend", e => {
  document.querySelectorAll(".kb-group.dragging, .kb-col.over").forEach(x => x.classList.remove("dragging", "over"));
});
$("#kanban").addEventListener("dragover", e => {
  const col = e.target.closest(".kb-col");
  if (!col || !e.dataTransfer.types.includes("text/argus-group")) return;
  e.preventDefault();
  document.querySelectorAll(".kb-col.over").forEach(x => x !== col && x.classList.remove("over"));
  col.classList.add("over");
});
$("#kanban").addEventListener("drop", async e => {
  const col = e.target.closest(".kb-col");
  const key = e.dataTransfer.getData("text/argus-group");
  document.querySelectorAll(".kb-col.over").forEach(x => x.classList.remove("over"));
  if (!col || !key) return;
  e.preventDefault();
  const to = col.dataset.kbCol, st = IB_COLS.find(c => c[0] === to)[2];
  const g = kanbanGroups(S.inbox.items).find(x => x.key === key);
  if (!g || g.col === to) return;
  // 묶음 안에서 아직 그 열보다 덜 끝난 카드만 옮긴다(완료로 끌면 모두 완료, 할 일로 끌면 모두 다시 할 일) — 요청 한 번으로
  const ids = g.cards.filter(c => ibCol(c) !== to).map(c => c.id);
  if (ids.length) {
    try { await api("POST", "/api/inbox/status", { ids, status: st }); } catch (err) { toast(err.message); }
  }
  await poll();
  loadKanban(true);
});
document.addEventListener("click", e => {
  const go = e.target.closest("[data-view-go]");
  if (go) setView(go.dataset.viewGo);
});
// 브레인 답 속 세션 링크(.sref) — 그 세션 칸으로
document.addEventListener("click", e => {
  const a = e.target.closest && e.target.closest(".sref");
  if (!a) return;
  e.preventDefault();
  if (S.view !== "board") setView("board");
  focusCol(a.dataset.sid);
});
// 작업 중단(터미널에 Esc). 승인·질문 창이 떠 있으면 서버가 거절한다
async function stopSession(sid, btn) {
  if (!sid || btn.disabled) return;
  btn.disabled = true;
  try {
    const r = await api("POST", `/api/sessions/${sid}/interrupt`, {});
    toast(r.message || "중단했습니다");
  } catch (e) { toast(e.message); }
  btn.disabled = false;
  await poll();
}

// ------------------------------------------------------------------ 서식 붙여넣기(입력창의 서식 모드)
// 서식 있는 글을 붙여넣으면 입력창이 서식 모드(contenteditable)로 바뀌어 굵게·제목·목록·코드·표가 보이는 그대로 보인다.
// 보낼 때는 마크다운으로 바꿔 보낸다(htmlToMd). 글을 다 지우거나 보내면 원래 입력창(textarea)으로 돌아간다.
// 평소 입력(/ 명령 자동완성·Tab 제안 등)은 그대로 textarea 가 맡는다. ⌘⇧V 는 서식 없이 글만
const RICH_TAGS = new Set(["B", "STRONG", "I", "EM", "CODE", "PRE", "A", "UL", "OL", "LI", "H1", "H2", "H3", "H4", "H5", "H6",
  "BLOCKQUOTE", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "P", "BR", "HR", "DIV"]);
function sanitizeRich(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll("script, style, button, .code-copy, meta, link, img, svg").forEach(n => n.remove());
  const clean = n => {
    for (const c of [...n.childNodes]) {
      if (c.nodeType === 3) continue;
      if (c.nodeType !== 1) { c.remove(); continue; }
      clean(c);
      if (!RICH_TAGS.has(c.tagName)) { c.replaceWith(...c.childNodes); continue; }
      const href = c.tagName === "A" ? c.getAttribute("href") : null;
      for (const a of [...c.attributes]) c.removeAttribute(a.name);
      if (href && /^https?:/.test(href)) c.setAttribute("href", href);
    }
  };
  clean(doc.body);
  return doc.body.innerHTML;
}
function richOf(el) { return el && el.querySelector(".cmp-rich"); }
function richOn(el) { const r = richOf(el); return !!r && !r.hidden; }
function composerText(el) {
  return richOn(el) ? htmlToMd(richOf(el).innerHTML) : el.querySelector(".cmp-text").value;
}
function enterRich(el, html) {
  const ta = el.querySelector(".cmp-text"), rich = richOf(el);
  // 이미 쓴 글은 줄 단위로 옮기고, 커서 자리에 붙여넣은 서식을 넣는다
  const before = ta.value.slice(0, ta.selectionStart), after = ta.value.slice(ta.selectionEnd);
  const para = t => t ? t.split("\n").map(l => `<div>${esc(l) || "<br>"}</div>`).join("") : "";
  rich.innerHTML = para(before) + html + para(after);
  ta.hidden = true;
  rich.hidden = false;
  el.classList.add("rich");
  setHint(el, el._hint || "");
  rich.focus();
  const r = document.createRange();
  r.selectNodeContents(rich);
  r.collapse(false);
  const sel = getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
  saveRichDraft(el);
}
function exitRich(el, keepText) {
  const ta = el.querySelector(".cmp-text"), rich = richOf(el);
  if (!rich || rich.hidden) return;
  ta.value = keepText ? htmlToMd(rich.innerHTML) : "";
  rich.innerHTML = "";
  rich.hidden = true;
  ta.hidden = false;
  el.classList.remove("rich");
  setHint(el, el._hint || "");
  S.richDrafts.delete(el.dataset.sid);
  autoGrow(ta);
}
S.richDrafts = persistMap("csl.richDrafts");   // session_id → 서식 모드의 HTML(칸을 다시 그려도·새로고침해도 남게)
function saveRichDraft(el) {
  const rich = richOf(el);
  S.richDrafts.set(el.dataset.sid, rich.innerHTML);
  S.drafts.set(el.dataset.sid, htmlToMd(rich.innerHTML));
}
document.addEventListener("input", e => {
  if (!e.target.classList || !e.target.classList.contains("cmp-rich")) return;
  const el = e.target.closest(".composer");
  if (!e.target.textContent.trim() && !e.target.querySelector("hr, br + br")) {   // 다 지웠으면 원래 입력창으로
    exitRich(el, false);
    S.drafts.delete(el.dataset.sid);
    el.querySelector(".cmp-text").focus();
    return;
  }
  saveRichDraft(el);
});
// 들여쓴 블록(목록·인용·코드) 빠져나오기 — 문서 편집기처럼:
//  · 블록 맨 앞에서 Backspace → 한 단계 내어쓰기(목록 맨 바깥이면 보통 줄로, 코드는 줄을 살려서)
//  · 빈 항목·빈 줄에서 Shift+Enter → 블록에서 나와 보통 줄로(내용 있는 목록 항목이면 다음 항목).
//    코드·인용은 끝의 빈 줄에서 — Shift+Enter 두 번이면 블록 아래 새 줄로 나온다
//  · ⇧Tab 내어쓰기 · Tab 들여쓰기(목록 안에서만)
//  · ⌘\ 서식 끄기 — 입력창 전체를 보통 입력창(마크다운 글)으로 되돌린다
function richBlock(rich) {
  const sel = getSelection();
  if (!sel.rangeCount || !rich.contains(sel.anchorNode)) return null;
  let n = sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement;
  const block = n.closest("li, blockquote, pre");
  return block && rich.contains(block) ? block : null;
}
function caretAtStart(block) {
  const sel = getSelection();
  if (!sel.isCollapsed) return false;
  const r = document.createRange();
  r.selectNodeContents(block);
  r.setEnd(sel.anchorNode, sel.anchorOffset);
  return !r.toString().length && !r.cloneContents().querySelector("br, img, hr");
}
function blockEmpty(block) { return !block.textContent.replace(/\u200b/g, "").trim(); }
function caretTo(node, atEnd) {
  const r = document.createRange();
  r.selectNodeContents(node);
  r.collapse(!atEnd);
  getSelection().removeAllRanges();
  getSelection().addRange(r);
}
function leaveBlock(rich, block) {
  if (block.tagName === "LI") { document.execCommand("outdent"); return; }
  if (block.tagName === "PRE") {
    // 코드: 글 속 줄바꿈(\n)은 보통 줄에선 공백이 되어 한 줄로 뭉친다 — 한 줄(div) 안에서 <br> 로 줄을 살린다
    const div = document.createElement("div");
    preText(block).split("\n").forEach((l, i) => {
      if (i) div.appendChild(document.createElement("br"));
      div.appendChild(document.createTextNode(l));
    });
    if (!div.textContent) div.appendChild(document.createElement("br"));
    block.replaceWith(div);
    caretTo(div, false);
    return;
  }
  // 인용: 이 블록을 보통 줄(div)로 풀고 커서는 그 자리
  const div = document.createElement("div");
  while (block.firstChild) div.appendChild(block.firstChild);
  if (!div.childNodes.length) div.appendChild(document.createElement("br"));
  block.replaceWith(div);
  caretTo(div, false);
}
// 커서 앞 마지막 글자·줄바꿈(빈 글 조각은 건너뜀)
function lastLeafBefore(block) {
  const sel = getSelection();
  const r = document.createRange();
  r.selectNodeContents(block);
  r.setEnd(sel.anchorNode, sel.anchorOffset);
  const w = document.createTreeWalker(r.cloneContents(), NodeFilter.SHOW_ALL);
  let last = null;
  for (let n = w.nextNode(); n; n = w.nextNode())
    if ((n.nodeType === 3 && n.textContent) || n.nodeName === "BR") last = n;
  return last;
}
// 코드·인용 블록 끝의 빈 줄에 커서가 있는지(뒤엔 글이 없고, 지금 줄이 비었음)
function atEmptyLastLine(block) {
  const sel = getSelection();
  if (!sel.isCollapsed) return false;
  const after = document.createRange();
  after.selectNodeContents(block);
  after.setStart(sel.anchorNode, sel.anchorOffset);
  if (after.toString().replace(/[\n\u200b]/g, "").trim()) return false;
  const anchor = sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement;
  const line = anchor.closest("div, p");
  if (line && line !== block && block.contains(line) && blockEmpty(line)) return true;   // 줄이 div 인 경우(인용)
  const last = lastLeafBefore(block);
  return !!last && (last.nodeName === "BR" || /\n$/.test(last.textContent));
}
// 블록 끝의 빈 줄(줄바꿈·빈 줄)을 걷어 내고 블록 아래 새 보통 줄로 커서를 옮긴다
function exitBlockBelow(block) {
  for (let i = 0; i < 20; i++) {
    let n = block;
    while (n.lastChild) n = n.lastChild;
    if (n === block) break;
    if (n.nodeName === "BR" || (n.nodeType === 3 && !n.textContent.replace(/\n+$/, "")) ||
        (n.nodeType === 1 && !n.textContent && n.nodeName !== "BR")) { n.remove(); continue; }
    if (n.nodeType === 3 && /\n$/.test(n.textContent)) { n.textContent = n.textContent.replace(/\n+$/, ""); continue; }
    // 줄이 div 인 블록에서 마지막 줄이 빈 div 면 지운다
    const lineEl = block.lastElementChild;
    if (lineEl && /^(DIV|P)$/.test(lineEl.tagName) && !lineEl.textContent.trim()) { lineEl.remove(); continue; }
    break;
  }
  const div = document.createElement("div");
  div.appendChild(document.createElement("br"));
  block.after(div);
  caretTo(div, false);
}
document.addEventListener("keydown", e => {
  if (!e.target.classList || !e.target.classList.contains("cmp-rich")) return;
  if (imeBusy(e)) return;
  const rich = e.target;
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (e.metaKey || e.ctrlKey) queueCompose(rich.closest(".composer"));
    else sendCompose(rich.closest(".composer"));
    return;
  }
  if (e.key === "Escape") { e.preventDefault(); rich.blur(); return; }
  if (e.key === "\\" && (e.metaKey || e.ctrlKey) && !e.altKey) {
    // 서식 끄기: 마크다운 글로 바꿔 보통 입력창에(코드 블록은 ``` 로 둘러싼 글 — 마음대로 고치거나 지운다)
    e.preventDefault();
    const el = rich.closest(".composer"), ta = el.querySelector(".cmp-text");
    exitRich(el, true);
    S.drafts.set(el.dataset.sid, ta.value);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    toast("서식을 껐습니다 — 마크다운 글로 바뀌었습니다");
    return;
  }
  const block = richBlock(rich);
  if (!block) return;
  let done = true;
  if (e.key === "Backspace" && caretAtStart(block)) leaveBlock(rich, block);
  else if (e.key === "Enter" && e.shiftKey && blockEmpty(block)) leaveBlock(rich, block);
  else if (e.key === "Enter" && e.shiftKey && block.tagName !== "LI" && atEmptyLastLine(block)) exitBlockBelow(block);
  else if (e.key === "Enter" && e.shiftKey && block.tagName === "LI") document.execCommand("insertParagraph");
  else if (e.key === "Tab" && block.tagName === "LI") {
    // 첫 항목은 붙을 앞 항목이 없어 들여쓰면 ul 안에 ul 이 바로 생기고(마크다운으로 바꿀 때 사라진다) — 막는다
    if (e.shiftKey) document.execCommand("outdent");
    else if (block.previousElementSibling) document.execCommand("indent");
  }
  else done = false;
  if (!done) return;
  e.preventDefault();
  saveRichDraft(rich.closest(".composer"));
});
document.addEventListener("paste", e => {
  if (!e.target.closest || !e.target.closest(".cmp-rich")) return;
  const rich = e.target.closest(".cmp-rich"), el = rich.closest(".composer");
  const files = [...(e.clipboardData && e.clipboardData.files || [])];
  if (files.length && addImages(el.dataset.sid, files) && !e.clipboardData.getData("text/plain")) { e.preventDefault(); return; }
  e.preventDefault();
  const html = e.clipboardData.getData("text/html");
  if (html && !PLAIN_PASTE.on) document.execCommand("insertHTML", false, sanitizeRich(html));
  else document.execCommand("insertText", false, e.clipboardData.getData("text/plain"));
  saveRichDraft(el);
}, true);


// ------------------------------------------------------------------ 세션 카드 보기(격자) — 전체를 한눈에
// 세션 보기 안에서 「⇆ 칸」과 「▦ 카드」를 고른다(브라우저에 기억). 카드는 칸과 같은 순서(같은 번호·단축키)로,
// 상태·제목·프로젝트·recap(없으면 마지막 응답)·마지막 요청·할 일 수만 작게. 누르면 칸 보기로 가서 그 칸으로 간다
S.boardMode = load("csl.boardMode", "cols");
S.cardLimit = 24;
S.cardSig = "";
function setBoardMode(m) {
  S.boardMode = m === "cards" ? "cards" : "cols";
  save("csl.boardMode", S.boardMode);
  document.querySelectorAll(".mode-btn").forEach(b => b.classList.toggle("on", b.dataset.mode === S.boardMode));
  applyBoardVisibility();
  if (S.boardMode === "cards") renderCards(true);
}
function applyBoardVisibility() {
  const board = S.view === "board";
  $("#board").hidden = !(board && S.boardMode === "cols");
  $("#cards").hidden = !(board && S.boardMode === "cards");
  $("#modeToggle").hidden = !board;
}
function plainOneLine(t, n) { return clamp(plainLine(t || ""), n); }
function sessionCardHtml(s, rank) {
  const d = displayState(s);
  const body = s.recap ? `<div class="sc-recap"><b>recap</b> ${esc(clamp(s.recap, 220))}</div>`
    : s.last_response ? `<div class="sc-recap">${esc(plainOneLine(s.last_response.response, 220))}</div>`
    : '<div class="sc-recap-none muted">아직 응답이 없습니다</div>';
  const lp = s.last_prompt && s.last_prompt.prompt;
  const notes = +s.open_notes || 0;
  const key = rank <= 9 ? `<span class="sc-key">⌘${rank}</span>` : "";
  return `<article class="scard ${d} ${pinOf(s) ? "pinned" : ""}" data-scard="${esc(s.session_id)}" style="--proj:${projColor(s.project_root || s.cwd)}">
    <div class="sc-top"><span class="rank">${rank}</span><span class="pill ${d}"><span class="dot ${d}"></span>${s.compact_at ? "컴팩트 중" : STATE_LABEL[d]}</span>
      <span class="ago" data-ts="${s.last_activity}">${rel(s.last_activity)}</span>${ctxChip(s, d)}<span class="spacer"></span>
      ${remoteBadge(s, d)}${pinOf(s) ? '<span class="sc-pin" title="고정됨">📌</span>' : ""}${key}</div>
    <div class="sc-title">${esc(sessionTitle(s))}</div>
    <div class="sc-meta"><span class="chip proj">${esc(s.project_name || base(s.project_root))}</span>${placeChips(s)}${s._fam ? famChip(s._fam) : ""}</div>
    ${d === "waiting" && s.state_detail ? `<div class="sc-wait"><span aria-hidden="true">⏳</span> ${esc(clamp(s.state_detail, 120))}</div>` : ""}
    ${body}
    <div class="sc-foot">${lp ? `<span class="sc-last" title="${esc(clamp(lp, 400))}">▶ ${esc(plainOneLine(lp, 90))}</span>` : '<span></span>'}
      ${notes ? `<span class="sc-notes" title="남은 넥스트 액션">할 일 ${notes}</span>` : ""}</div>
  </article>`;
}
function renderCards(force) {
  if (S.dragging) return;
  const box = $("#cards");
  if (!box || box.hidden) return;
  const all = boardSessions();
  const list = all.slice(0, S.cardLimit);
  const sig = JSON.stringify(list.map(s => [s.session_id, s.session_id === S.expandedCard ? "open" : sigOf(s),
    s.last_response && s.last_response.len])) + S.cardLimit + all.length + (S.expandedCard || "");
  if (!force && sig === S.cardSig) return;
  // 펼친 카드에서 쓰는 중(입력창·이름 바꾸기)이면 미룬다 — 펼친 칸을 떼었다 붙이면 포커스·IME 조합이 날아간다
  if (!force && (typingIn(box) || S.renaming)) return;
  S.cardSig = sig;
  const more = all.length - list.length;
  if (S.expandedCard && !all.some(s => s.session_id === S.expandedCard)) S.expandedCard = null;
  const keep = S.expandedCard && box.querySelector(".sc-colwrap > .col");   // 펼친 칸은 다시 만들지 않고 옮겨 붙인다(쓰던 글·스크롤 유지)
  if (keep) keep.remove();
  box.innerHTML = `<div class="sc-grid">${list.map((s, i) => s.board ? boardCardHtml(s, i + 1) : S.expandedCard === s.session_id ? expandedCardHtml(s, i + 1) : sessionCardHtml(s, i + 1)).join("")}</div>` +
    (more > 0 ? `<div class="sc-more"><button class="btn" data-cards-more>세션 더 보기 (+${Math.min(more, 24)} / 남은 ${more}개)</button></div>` : "") +
    (!all.length ? '<div class="bp-empty"><p>보여 줄 세션이 없습니다.</p></div>' : "");
  if (S.expandedCard) mountExpanded(keep);
  if (S.cardSel) {
    const el = $(`#cards [data-scard="${CSS.escape(S.cardSel)}"], #cards [data-scard-open="${CSS.escape(S.cardSel)}"]`);
    if (el) el.classList.add("kb-sel");
  }
}
// 키보드로 카드 고르기: 방향키·hjkl 은 화면에서 그 방향에 가장 가까운 카드로(격자라 위아래도 된다), Enter 는 펼쳐 대화.
// 고른 카드가 「지금 칸」이 되어 T(터미널)·P(고정)도 그 카드에 쓴다. 펼친 카드의 입력창에서 Esc 면 그 카드가 골라진 상태로
function setCardSel(sid) {
  S.cardSel = sid;
  if (sid) S.activeCol = sid;
  document.querySelectorAll("#cards .scard.kb-sel").forEach(c => c.classList.remove("kb-sel"));
  const el = sid && $(`#cards [data-scard="${CSS.escape(sid)}"], #cards [data-scard-open="${CSS.escape(sid)}"]`);
  if (el) { el.classList.add("kb-sel"); el.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
  return el;
}
function moveCardSel(dir) {
  const cards = [...document.querySelectorAll("#cards .sc-grid > .scard")];
  if (!cards.length) return;
  const sidOf = c => c.dataset.scard || c.dataset.scardOpen;
  const cur = cards.find(c => sidOf(c) === S.cardSel) || cards.find(c => sidOf(c) === S.activeCol);
  if (!cur) return setCardSel(sidOf(cards[0]));
  const a = cur.getBoundingClientRect(), ax = a.left + a.width / 2, ay = a.top + a.height / 2;
  let best = null, bs = Infinity;
  for (const c of cards) {
    if (c === cur) continue;
    const r = c.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    // 그 방향으로 놓인 카드만: 앞으로 간 거리 + 옆으로 어긋난 거리(어긋남은 무겁게)
    const along = { ArrowLeft: ax - x, ArrowRight: x - ax, ArrowUp: ay - y, ArrowDown: y - ay }[dir];
    const side = dir === "ArrowLeft" || dir === "ArrowRight" ? Math.abs(y - ay) : Math.abs(x - ax);
    if (along <= 4) continue;
    const s = along + side * 3;
    if (s < bs) { bs = s; best = c; }
  }
  if (best) setCardSel(sidOf(best));
}
function enterCardSel() {
  const sid = S.cardSel || S.activeCol;
  if (!sid) return moveCardSel("ArrowRight");
  if (S.expandedCard === sid) {
    const ta = $("#cards .scard.expanded .cmp-text");
    if (ta) ta.focus();
    return;
  }
  openCard(sid);
}
// 카드를 그 자리에서 크게 펼쳐 바로 대화(칸 보기와 같은 칸 — 대화·넥스트 액션·입력창). 한 번에 하나만
function expandedCardHtml(s, rank) {
  return `<article class="scard expanded ${displayState(s)}" data-scard-open="${esc(s.session_id)}" style="--proj:${projColor(s.project_root || s.cwd)}">
    <div class="sc-xbar"><span class="muted">${rank}번 세션 · 카드에서 바로 대화</span><span class="spacer"></span>
      <button class="btn ghost" data-scard-cols="${esc(s.session_id)}" title="가로 칸 보기에서 이 칸으로">⇆ 칸에서 보기</button>
      <button class="btn ghost" data-scard-close title="접기 ( Esc )">✕ 접기</button></div>
    <div class="sc-colwrap"></div></article>`;
}
function mountExpanded(existing) {
  const wrap = $("#cards .sc-colwrap");
  if (!wrap) return;
  const sid = S.expandedCard;
  if (existing && existing.dataset.col === sid) wrap.appendChild(existing);
  else {
    wrap.innerHTML = colSkeleton(sid);
    const col = wrap.querySelector(".col");
    setupThread(col.querySelector(".col-thread"));
    restoreComposerDraft(col.querySelector(".composer"));
  }
  updateExpandedCard();
}
async function updateExpandedCard() {
  const col = $("#cards .sc-colwrap > .col");
  const s = S.expandedCard && sessionById(S.expandedCard);
  if (!col || !s) return;
  S.visibleCols.add(s.session_id);
  const c = S.boardCache.get(s.session_id);
  if (!c || c.sig !== sigOf(s)) {
    try {
      await loadDetail(s);
    } catch (e) { return; }
  }
  updateCol(col, s, boardSessions().findIndex(x => x.session_id === s.session_id) + 1, false);
}
function openCard(sid) {
  if (isBoard(sid)) { S.expandedCard = null; setBoardMode("cols"); return focusCol(sid); }   // 보드 칸은 칸 보기에서
  S.expandedCard = sid;
  S.cardSel = sid;
  renderCards(true);
  const card = $("#cards .scard.expanded");
  if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  const ta = card && card.querySelector(".cmp-text");
  if (ta) setTimeout(() => ta.focus(), 50);
}
function closeCard() {
  if (!S.expandedCard) return;
  const sid = S.expandedCard;
  S.expandedCard = null;
  renderCards(true);
  const c = $(`#cards [data-scard="${CSS.escape(sid)}"]`);
  if (c) c.scrollIntoView({ block: "nearest" });
}
document.addEventListener("click", e => {
  const m = e.target.closest && e.target.closest(".mode-btn");
  if (m) return setBoardMode(m.dataset.mode);
  if (e.target.closest && e.target.closest("[data-cards-more]")) { S.cardLimit += 24; return renderCards(true); }
  if (e.target.closest && e.target.closest("[data-scard-close]")) return closeCard();
  const toCols = e.target.closest && e.target.closest("[data-scard-cols]");
  if (toCols) { S.expandedCard = null; setBoardMode("cols"); return focusCol(toCols.dataset.scardCols); }
  const c = e.target.closest && e.target.closest("[data-scard]");
  if (!c || e.target.closest("a, button")) return;
  openCard(c.dataset.scard);
});
document.addEventListener("keydown", e => {
  if (e.key !== "Escape" || !S.expandedCard || S.view !== "board" || S.boardMode !== "cards") return;
  if (isTyping()) return;   // 입력창 Esc 는 입력창에서 빠져나오기 — 한 번 더 누르면 접힌다
  closeCard();
});

// ------------------------------------------------------------------ 세션 빠른 이동(G · Shift 두 번)
// 작은 창에 세션을 보드 순서대로 보여 주고, 글자를 치면 퍼지(띄엄띄엄 맞는 글자)로 거른다 — IntelliJ 의 Switcher·
// Search Everywhere 처럼. ↑↓ 로 고르고 Enter 면 그 칸으로(브레인은 브레인 패널로). Esc 로 닫는다
const SW = { open: false, sel: 0, items: [] };
function swCandidates() {
  const list = [];
  const b = brainSession();
  if (b) list.push({ s: b, rank: 0 });
  boardSessions().forEach((s, i) => list.push({ s, rank: i + 1 }));
  return list;
}
// 퍼지 점수: 모든 글자가 순서대로 들어 있어야 맞음. 붙어 있을수록·단어 앞일수록·앞쪽일수록 높다. 맞은 글자 위치도 돌려준다
function fuzzy(q, text) {
  const t = text.toLowerCase(), qq = q.toLowerCase().replace(/\s+/g, "");
  if (!qq) return { score: 0, hits: [] };
  const sub = t.indexOf(qq);
  if (sub >= 0) return { score: 1000 - sub + (sub === 0 || /[\s\-_/·(]/.test(t[sub - 1]) ? 200 : 0), hits: [...qq].map((_, k) => sub + k) };
  let score = 0, last = -2, hits = [];
  for (const ch of qq) {
    const i = t.indexOf(ch, last + 1);
    if (i < 0) return null;
    score += i === last + 1 ? 15 : 1;
    if (i === 0 || /[\s\-_/·(]/.test(t[i - 1])) score += 10;
    hits.push(i);
    last = i;
  }
  return { score: score - hits[0] * 0.1, hits };
}
function swMark(text, hits) {
  if (!hits || !hits.length) return esc(text);
  const set = new Set(hits);
  return [...text].map((c, i) => set.has(i) ? `<mark>${esc(c)}</mark>` : esc(c)).join("");
}
function swFilter() {
  const q = $("#swInput").value.trim();
  const out = [];
  for (const c of swCandidates()) {
    const title = sessionTitle(c.s);
    const where = [c.s.project_name, c.s.branch, wtName(c.s)].filter(Boolean).join(" · ");
    if (!q) { out.push({ ...c, title, where, score: 0 }); continue; }
    const ft = fuzzy(q, title), fw = fuzzy(q, where), fid = c.s.session_id.startsWith(q.toLowerCase()) ? { score: 500, hits: [] } : null;
    const best = [ft && { ...ft, on: "t" }, fw && { score: fw.score * 0.6, hits: fw.hits, on: "w" }, fid && { ...fid, on: "id" }]
      .filter(Boolean).sort((a, b) => b.score - a.score)[0];
    if (best) out.push({ ...c, title, where, score: best.score, tHits: best.on === "t" ? best.hits : null, wHits: best.on === "w" ? best.hits : null });
  }
  if (q) out.sort((a, b) => b.score - a.score);
  SW.items = out;
  SW.sel = Math.min(SW.sel, Math.max(0, out.length - 1));
  swRender();
}
function swRender() {
  const list = $("#swList");
  list.innerHTML = SW.items.map((x, i) => {
    const d = displayState(x.s);
    return `<div class="sw-row ${i === SW.sel ? "sel" : ""} ${d}" data-sw="${i}" style="--proj:${projColor(x.s.project_root || x.s.cwd)}">
      <span class="sw-key">${x.rank === 0 ? "B" : x.rank <= 9 ? "⌘" + x.rank : x.rank}</span>
      <span class="dot ${d}"></span>
      <span class="sw-title">${x.rank === 0 ? "🧠 " : ""}${swMark(x.title, x.tHits)}</span>
      <span class="sw-where">${swMark(x.where, x.wHits)}</span>
      <span class="sw-ago">${rel(x.s.last_activity)}</span></div>`;
  }).join("") || '<div class="sw-empty muted">맞는 세션이 없습니다</div>';
  const sel = list.querySelector(".sw-row.sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}
function openSwitcher() {
  if (SW.open) return;
  SW.open = true;
  SW.sel = 0;
  $("#switcher").hidden = false;
  const inp = $("#swInput");
  inp.value = "";
  swFilter();
  // 처음엔 「지금 칸」 다음 것을 골라 둔다(Enter 한 번으로 옆 세션으로 — IntelliJ Switcher 처럼)
  const cur = SW.items.findIndex(x => x.s.session_id === S.activeCol);
  if (cur >= 0 && SW.items.length > 1) { SW.sel = (cur + 1) % SW.items.length; swRender(); }
  inp.focus();
}
function closeSwitcher() {
  SW.open = false;
  $("#switcher").hidden = true;
}
function swGo(i) {
  const x = SW.items[i];
  closeSwitcher();
  if (!x) return;
  if (x.rank > 0 && S.view !== "board") setView("board");   // 브레인은 어느 보기에서든 패널로
  focusCol(x.s.session_id).then(col => {
    const ta = col && col.querySelector(".cmp-text");
    if (ta) ta.focus({ preventScroll: true });   // 고른 세션에 바로 쓸 수 있게
  });
}
$("#swInput").addEventListener("input", () => { SW.sel = 0; swFilter(); });
$("#swInput").addEventListener("keydown", e => {
  // ⌘/⌃/⌥+숫자: 목록에 보이는 그 키의 세션으로(창을 닫고 간다). 창을 둔 채 뒤의 보드에서 칸이 바뀌고 입력창이 잡히던 것 —
  // 보드의 같은 단축키는 preventDefault 된 키를 다시 받지 않는다
  const digit = /^Digit([1-9])$/.exec(e.code);
  if (digit && (e.metaKey || e.ctrlKey || e.altKey) && !e.shiftKey) {
    e.preventDefault();
    const i = SW.items.findIndex(x => x.rank === +digit[1]);
    if (i >= 0) return swGo(i);
    closeSwitcher();
    return gotoRank(+digit[1]);
  }
  if (imeBusy(e)) return;
  const n = SW.items.length;
  if (e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey)) { e.preventDefault(); SW.sel = n ? (SW.sel + 1) % n : 0; swRender(); }
  else if (e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey)) { e.preventDefault(); SW.sel = n ? (SW.sel - 1 + n) % n : 0; swRender(); }
  else if (e.key === "Enter") { e.preventDefault(); swGo(SW.sel); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeSwitcher(); }
});
$("#swList").addEventListener("mousedown", e => e.preventDefault());   // 입력칸 포커스 유지
$("#swList").addEventListener("click", e => {
  const r = e.target.closest("[data-sw]");
  if (r) swGo(+r.dataset.sw);
});
$("#switcher").addEventListener("mousedown", e => { if (e.target.id === "switcher") closeSwitcher(); });
// Shift 만 눌렀다 떼기를 빠르게 두 번 — 입력 중에도 연다. IntelliJ 처럼 뗄 때 판정하고, 그 사이 다른 키가
// 끼면(Shift+A 같은 대문자 입력) 처음부터 다시 센다
const SHIFT2 = { down: false, clean: false, at: 0 };
// Shift+휠(가로 스크롤)·Shift+클릭(범위 선택)도 「다른 동작」 — 그걸 두 번 해도 스위처가 열리지 않게
for (const ev of ["pointerdown", "wheel"]) document.addEventListener(ev, () => { SHIFT2.clean = false; SHIFT2.at = 0; }, { capture: true, passive: true });
document.addEventListener("keydown", e => {
  if (e.key === "Shift") { if (!e.repeat) { SHIFT2.down = true; SHIFT2.clean = true; } return; }
  SHIFT2.clean = false;
  SHIFT2.at = 0;
}, true);
document.addEventListener("keyup", e => {
  if (e.key !== "Shift" || !SHIFT2.down) return;
  if (e.target.closest && e.target.closest(".xterm")) { SHIFT2.down = false; SHIFT2.at = 0; return; }   // 터미널 안의 Shift 는 터미널 것
  SHIFT2.down = false;
  if (!SHIFT2.clean) { SHIFT2.at = 0; return; }
  const now = Date.now();
  if (now - SHIFT2.at < 400) { SHIFT2.at = 0; if (!SW.open) openSwitcher(); }
  else SHIFT2.at = now;
}, true);

setView(load("csl.view", "board"));   // 맨 끝에서 — 칸반·활동 코드가 모두 정의된 뒤
setBoardMode(S.boardMode);

// ------------------------------------------------------------------ 보드 칸(세션이 아닌 칸): ⌨ 터미널 · 📝 마크다운 문서
// 서버 csl/boards.py. 칸 id 는 bd-<번호>이고 순서·고정·보관·크게 보기는 세션 칸과 같이 다룬다(boardFamilies 에 섞인다).
// 마크다운 칸: 위는 문서(편집 ↔ 보기, 고치면 0.7초 뒤 저장), 아래는 AI 대화(보낼 때마다 claude -p 가 그 파일만 고친다)
const BD = { docs: new Map() };   // 칸 id → { text, mtime, msgs, msgSig, loaded, loading, dirty, saving, timer, err, conflict, mode }
const BOARD_KIND = { md: ["📝", "문서"], term: ["⌨", "터미널"] };
function isBoard(sid) { return /^bd-\d+$/.test(sid || ""); }
function boardColSkeleton(s) {
  const sid = esc(s.session_id);
  const body = s.board === "term"
    ? `<div class="col-term bd-term"></div><div class="bd-closed" hidden></div>`
    : `<div class="md-doc">
        <div class="md-bar"><div class="md-mode"><button type="button" data-md-mode="edit">✎ 편집</button><button type="button" data-md-mode="view">👁 보기</button></div>
          <span class="md-status muted"></span></div>
        <textarea class="md-edit" spellcheck="false" placeholder="마크다운으로 적으세요 — 고치면 바로 저장됩니다"></textarea>
        <div class="md-view md" hidden></div>
      </div>
      <div class="md-chat"></div>
      <form class="md-ask" data-sid="${sid}"><textarea class="md-ask-text" rows="1" placeholder="AI 에게 문서 고치기 요청  Enter 보내기 · Shift+Enter 줄바꿈"></textarea>
        <button class="btn primary md-ask-send" type="submit">보내기</button></form>`;
  return `<div class="col board-col ${s.board}-col" data-col="${sid}" data-drop-sid="${sid}">
    <div class="col-head"></div>${body}
    <div class="col-resize" data-resize-sid="${sid}" title="끌어서 너비 조절 · 더블클릭: 기본 너비"></div>
  </div>`;
}
function boardRelPath(s) {
  if (!s.path) return "";
  return s.path.startsWith(s.cwd + "/") ? s.path.slice(s.cwd.length + 1) : s.path;
}
function updateBoardCol(col, s, rank, lastPin) {
  const sid = s.session_id;
  col.style.order = rank;
  col.style.setProperty("--proj", projColor(s.project_root || s.cwd));
  applyColWidth(col, sid);
  const key = [s.title, pinOf(s), s.archived, rank, lastPin, S.activeCol === sid, s.mtime, s.msg_sig, s.ai_running, s.alive, s.shell, S.uiVer].join("|");
  if (col._key === key) return;
  col._key = key;
  const pinned = !!pinOf(s), [icon, label] = BOARD_KIND[s.board] || ["", ""];
  col.className = `col board-col ${s.board}-col ${s.ai_running ? "working" : "idle"}` + (S.activeCol === sid ? " active" : "") +
    (pinned ? " pinned" : "") + (lastPin ? " pin-last" : "");
  const state = s.board === "md" && s.ai_running ? `<span class="pill working"><span class="dot working"></span>AI 고치는 중</span>`
    : s.board === "term" && !s.alive ? `<span class="pill ended"><span class="dot ended"></span>닫힘</span>` : "";
  setHtml(col.querySelector(".col-head"), `
    <div class="col-top">
      <span class="rank">${rank}</span>
      <span class="chip board-kind" data-icon="${icon}">${icon} ${label}</span>
      ${state}
      <span class="ago" data-ts="${s.last_activity}">${rel(s.last_activity)}</span>
      <span class="spacer"></span>
      ${pinned ? `<button class="col-pin on" data-action="pin" data-sid="${esc(sid)}" title="고정 해제 ( P )" data-icon="📌">📌 고정됨</button>` : ""}
      <button class="col-zoom-btn" data-action="col-zoom" data-sid="${esc(sid)}" title="이 칸 크게 보기 — 누를 때마다 반 → 가득 → 평소 ( Z )" aria-label="이 칸 크게 보기"><span class="cz-in">⤢</span><span class="cz-out">⤡</span></button>
      <button class="col-menu-btn" data-action="col-menu" data-sid="${esc(sid)}" title="이름 · 고정 · 보관 · 지우기" aria-label="칸 메뉴">⋮</button>
    </div>
    <div class="title-line col-title-line"><span class="col-title title-text" title="${esc(s.title)}">${esc(s.title)}</span></div>
    <div class="col-meta"><span class="chip proj" title="${esc(s.project_root || "")}">${esc(s.project_name || base(s.project_root))}</span>${
      s.board === "md" ? `<span class="chip bd-path" title="${esc(s.path || "")}">${esc(boardRelPath(s))}</span>`
      : `<span class="chip bd-path" title="${esc(s.cwd)}">${esc(s.cwd.replace(/^\/Users\/[^/]+/, "~"))}</span>`}</div>`);
  if (s.board === "md") syncMd(col, s); else syncBoardTerm(col, s);
}

// ---- 터미널 칸
function syncBoardTerm(col, s) {
  for (const box of [...TERM.live.keys()]) if (!box.isConnected) disposeTerm(box);
  const box = col.querySelector(".bd-term"), closed = col.querySelector(".bd-closed");
  if (!s.alive) {
    disposeTerm(box);
    box.hidden = true;
    closed.hidden = false;
    setHtml(closed, `<p>셸이 닫혔습니다 <span class="muted">(exit 했거나 tmux 서버가 꺼졌습니다)</span></p>
      <button class="btn" data-bd-reopen="${esc(s.session_id)}">↻ 같은 폴더에서 다시 열기</button>`);
    return;
  }
  closed.hidden = true;
  box.hidden = false;
  const cur = TERM.live.get(box);
  if (cur && cur.shell !== s.shell) disposeTerm(box);
  if (!TERM.live.has(box)) mountTerm(box, { shell: s.shell, board: true });
}

// ---- 마크다운 칸
function mdState(s) {
  let st = BD.docs.get(s.session_id);
  if (!st) {
    st = { text: "", mtime: 0, msgs: [], msgSig: null, loaded: false, loading: false, dirty: false, saving: false, timer: 0,
      err: "", conflict: false, mode: load("csl.mdMode." + s.board_id, "edit") === "view" ? "view" : "edit" };
    BD.docs.set(s.session_id, st);
  }
  return st;
}
function syncMd(col, s) {
  const st = mdState(s);
  const ta = col.querySelector(".md-edit");
  ta.readOnly = !!s.ai_running;
  col.querySelector(".md-ask-send").disabled = !!s.ai_running;
  applyMdMode(col, st);
  const stale = s.mtime && s.mtime > st.mtime + 0.001 && !st.dirty && !st.saving;   // 서버 쪽이 더 새것일 때만(AI·다른 곳에서 고침)
  if ((!st.loaded || stale || s.msg_sig !== st.msgSig) && !st.loading) loadMd(s);
  renderMdChat(col, st, s);
  mdStatus(col, st, s);
}
function applyMdMode(col, st) {
  const ta = col.querySelector(".md-edit"), view = col.querySelector(".md-view");
  ta.hidden = st.mode !== "edit";
  view.hidden = st.mode !== "view";
  col.querySelectorAll("[data-md-mode]").forEach(b => b.classList.toggle("on", b.dataset.mdMode === st.mode));
  if (st.mode === "view") setHtml(view, st.text.trim() ? md(st.text, { tasks: true }) : '<p class="muted">빈 문서입니다 — 「✎ 편집」에서 적거나 아래 AI 에게 맡기세요</p>');
}
async function loadMd(s, force) {
  const st = mdState(s), sid = s.session_id;
  st.loading = true;
  try {
    const d = await api("GET", `/api/boards/${s.board_id}`);
    st.msgs = d.msgs;
    st.msgSig = s.msg_sig;
    st.loaded = true;
    if (force || (!st.dirty && !st.saving)) {
      st.text = d.text;
      st.mtime = d.mtime;
      st.dirty = false;
      st.err = "";
      st.conflict = false;
      const col = colEl(sid), ta = col && col.querySelector(".md-edit");
      if (ta && ta.value !== d.text) {
        const focused = document.activeElement === ta, a = ta.selectionStart, b = ta.selectionEnd;
        ta.value = d.text;
        if (focused) ta.setSelectionRange(Math.min(a, d.text.length), Math.min(b, d.text.length));
      }
    }
  } catch (e) {
    st.err = e.message;
  } finally {
    st.loading = false;
  }
  const col = colEl(sid), cur = sessionById(sid);
  if (col && cur) {
    syncMd(col, cur);   // 불러오는 사이 AI 가 끝났으면(기록·문서가 또 바뀜) 여기서 한 번 더 불러온다
  }
}
function mdStatus(col, st, s) {
  const el = col.querySelector(".md-status");
  const html = st.conflict ? `<span class="md-err">그사이 문서가 바뀌었습니다</span> <button class="linkish" data-md-reload="${esc(s.session_id)}">내 글 버리고 새로 불러오기</button>`
    : st.err ? `<span class="md-err">${esc(st.err)}</span>`
    : s.ai_running ? "AI 가 고치는 중 — 끝날 때까지 읽기만"
    : st.saving ? "저장하는 중…" : st.dirty ? "고치는 중…" : st.loaded ? "저장됨" : "불러오는 중…";
  setHtml(el, html);
}
function renderMdChat(col, st, s) {
  const box = col.querySelector(".md-chat");
  const msgs = st.msgs || [];
  const html = msgs.length ? msgs.map(m => m.role === "user" ? `<div class="mdc-u">${esc(m.text)}</div>`
      : m.role === "note" ? `<div class="mdc-note muted">↩ ${esc(m.text)}</div>`
      : m.status === "running" ? `<div class="mdc-ai running"><span class="spin"></span> AI 가 문서를 고치는 중…</div>`
      : `<div class="mdc-ai ${m.status}"><div class="md">${md(m.text || "")}</div><div class="mdc-meta muted">${
          m.status === "error" ? "실패" : m.changed ? "문서를 고쳤습니다" : "문서는 그대로"}${m.model ? " · " + esc(m.model) : ""}${
          m.undoable ? ` · <button class="linkish" data-md-undo="${m.id}" data-sid="${esc(s.session_id)}">되돌리기</button>` : ""}</div></div>`).join("")
    : `<div class="mdc-empty muted">아래에 요청을 적으면 AI 가 이 문서를 고칩니다 — 예: 「회의록 형식으로 정리해줘」 「할 일만 체크리스트로 뽑아줘」</div>`;
  if (box._html === html) return;
  setHtml(box, html);
  box.scrollTop = box.scrollHeight;
}
async function saveMd(sid) {
  const st = BD.docs.get(sid), s = sessionById(sid), col = colEl(sid);
  if (!st || !s || !col) return;
  clearTimeout(st.timer);
  if (st.saving) { st.timer = setTimeout(() => saveMd(sid), 400); return; }
  if (!st.dirty || st.conflict) return;
  const text = col.querySelector(".md-edit").value;
  st.saving = true;
  st.dirty = false;
  mdStatus(col, st, s);
  try {
    const r = await api("POST", `/api/boards/${s.board_id}/doc`, { text, mtime: st.mtime });
    st.mtime = r.mtime;
    s.mtime = r.mtime;
    st.text = text;
    st.err = "";
  } catch (e) {
    st.dirty = true;
    if (e.data && e.data.mtime) st.conflict = true; else st.err = e.message;
  } finally {
    st.saving = false;
  }
  const c = colEl(sid);
  if (c) mdStatus(c, st, sessionById(sid) || s);
}
// 보기 화면의 체크박스: 원문의 그 줄 [ ] ↔ [x] 를 바꿔 편집칸에 넣고 저장한다(편집에서 친 것과 같은 길)
function toggleMdTask(box) {
  const col = box.closest(".col"), s = col && sessionById(col.dataset.col);
  if (!s) return;
  const st = mdState(s), ta = col.querySelector(".md-edit");
  if (s.ai_running || st.conflict) { box.checked = !box.checked; return toast(s.ai_running ? "AI 가 고치는 중입니다" : "문서가 바뀐 것부터 정리해 주세요"); }
  const lines = ta.value.replace(/\r/g, "").split("\n"), n = +box.dataset.line;
  const m = lines[n] && lines[n].match(/^(\s*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\].*)$/);
  if (!m) { box.checked = !box.checked; return toast("그 항목을 원문에서 찾지 못했습니다 — 새로 불러온 뒤 다시"); }
  lines[n] = m[1] + (box.checked ? "x" : " ") + m[3];
  ta.value = st.text = lines.join("\n");
  st.dirty = true;
  clearTimeout(st.timer);
  st.timer = setTimeout(() => saveMd(s.session_id), 300);
  mdStatus(col, st, s);
}
async function sendMdAsk(sid) {
  const s = sessionById(sid), col = colEl(sid);
  if (!s || !col) return;
  const ta = col.querySelector(".md-ask-text"), text = ta.value.trim();
  if (!text || s.ai_running) return;
  const st = mdState(s);
  if (st.conflict) return toast("문서가 바뀐 것부터 정리해 주세요(새로 불러오기)");
  if (st.dirty || st.saving) { st.dirty = true; await saveMd(sid); }   // 고치던 글을 먼저 저장해야 AI 가 그 글을 읽는다
  try {
    await api("POST", `/api/boards/${s.board_id}/ai`, { text });
    ta.value = "";
    s.ai_running = true;
    col._key = "";
    updateBoardCol(col, s, +col.style.order || 1, col.classList.contains("pin-last"));
    refresh();
  } catch (e) { toast(e.message); }
}
document.addEventListener("input", e => {
  if (!e.target.matches || !e.target.matches(".md-edit")) return;
  const col = e.target.closest(".col"), sid = col && col.dataset.col, st = sid && BD.docs.get(sid);
  if (!st) return;
  st.dirty = true;
  clearTimeout(st.timer);
  st.timer = setTimeout(() => saveMd(sid), 700);
  const s = sessionById(sid);
  if (s) mdStatus(col, st, s);
});
document.addEventListener("submit", e => {
  const f = e.target.closest && e.target.closest(".md-ask");
  if (!f) return;
  e.preventDefault();
  sendMdAsk(f.dataset.sid);
});
document.addEventListener("keydown", e => {
  if (!e.target.matches || !e.target.matches(".md-ask-text")) return;
  if (e.key === "Enter" && !e.shiftKey && !imeBusy(e)) { e.preventDefault(); sendMdAsk(e.target.closest(".md-ask").dataset.sid); }
});
document.addEventListener("click", async e => {
  const mode = e.target.closest("[data-md-mode]");
  if (mode) {
    const col = mode.closest(".col"), s = sessionById(col.dataset.col);
    if (!s) return;
    const st = mdState(s);
    if (st.mode === "edit") st.text = col.querySelector(".md-edit").value;
    st.mode = mode.dataset.mdMode;
    save("csl.mdMode." + s.board_id, st.mode);
    applyMdMode(col, st);
    if (st.mode === "edit") col.querySelector(".md-edit").focus();
    return;
  }
  const task = e.target.closest(".md-view .md-task");
  if (task) return toggleMdTask(task);
  const rl = e.target.closest("[data-md-reload]");
  if (rl) { const s = sessionById(rl.dataset.mdReload); if (s) loadMd(s, true); return; }
  const un = e.target.closest("[data-md-undo]");
  if (un) {
    // 되돌리기는 AI 가 고치기 전 내용으로 통째로 — 그 뒤 직접 고친 것도 사라진다. 한 번 더 눌러야 한다
    if (!un.dataset.armed) { un.dataset.armed = "1"; un.textContent = "한 번 더 누르면 되돌립니다(그 뒤에 고친 것도 사라짐)"; return; }
    const s = sessionById(un.dataset.sid);
    if (!s) return;
    try { await api("POST", `/api/boards/${s.board_id}/undo`, { mid: +un.dataset.mdUndo }); toast("AI 가 고치기 전으로 되돌렸습니다"); }
    catch (err) { toast(err.message); }
    return refresh();
  }
  const ro = e.target.closest("[data-bd-reopen]");
  if (ro) {
    const s = sessionById(ro.dataset.bdReopen);
    if (!s) return;
    try { await termApi("POST", `/api/boards/${s.board_id}/reopen`, {}); } catch (err) { toast(err.message); }
    return refresh();
  }
});
document.addEventListener("dblclick", e => {   // 보기에서 두 번 누르면 편집으로
  const v = e.target.closest && e.target.closest(".md-col .md-view");
  if (v) v.closest(".col").querySelector('[data-md-mode="edit"]').click();
});

// ---- 칸 메뉴(보드 칸)
function boardMenuHtml(s) {
  return `
    <button class="cm-item" data-cm="pin">📌 ${pinOf(s) ? "고정 풀기" : "맨 앞에 고정"} <span class="muted">P</span></button>
    <button class="cm-item" data-cm="bd-rename">✎ 이름 바꾸기</button>
    ${s.board === "md" ? `<button class="cm-item" data-cm="bd-copy">📋 파일 경로 복사 <span class="muted">${esc(boardRelPath(s))}</span></button>` : ""}
    ${s.board === "term" && !s.alive ? `<button class="cm-item" data-cm="bd-reopen">↻ 셸 다시 열기</button>` : ""}
    <button class="cm-item" data-cm="archive">🗄 ${s.archived ? "보관 해제" : "보관"} <span class="muted">${s.archived ? "" : "보드에서 숨기기"}</span></button>
    <button class="cm-item danger" data-cm="bd-delete">🗑 칸 지우기 <span class="muted">${s.board === "md" ? "문서 파일은 남김" : "셸도 닫음"}</span></button>
    ${colWidthOf(s.session_id) ? `<button class="cm-item" data-cm="width-reset">↔ 기본 너비로 <span class="muted">${colWidthOf(s.session_id)}px</span></button>` : ""}`;
}
// 처리했으면 true(메뉴 클릭 처리기의 앞부분에서 부른다)
async function boardMenuAction(item, menu, s) {
  const act = item.dataset.cm, sid = s.session_id;
  if (act === "bd-rename") {
    item.outerHTML = `<div class="cm-confirm"><input class="ctx-note bd-name" value="${esc(s.title)}" spellcheck="false">
      <div class="cm-confirm-btns"><button class="btn" data-cm="cancel">취소</button><button class="btn primary" data-cm="bd-rename-yes">바꾸기</button></div></div>`;
    const inp = menu.querySelector(".bd-name");
    inp.focus();
    inp.select();
    inp.addEventListener("keydown", ev => { if (ev.key === "Enter" && !imeBusy(ev)) { ev.preventDefault(); menu.querySelector('[data-cm="bd-rename-yes"]').click(); } });
    return true;
  }
  if (act === "bd-delete") {
    item.outerHTML = `<div class="cm-confirm"><div>이 칸을 지울까요?<br><span class="muted">${s.board === "md"
      ? `AI 대화 기록은 지워지고, 문서 파일(${esc(boardRelPath(s))})은 폴더에 그대로 남습니다`
      : "그 터미널의 셸도 닫습니다 — 돌던 명령이 있으면 끊깁니다"}</span></div>
      <div class="cm-confirm-btns"><button class="btn" data-cm="cancel">취소</button><button class="btn danger" data-cm="bd-delete-yes">지우기</button></div></div>`;
    menu.querySelector('[data-cm="bd-delete-yes"]').focus();
    return true;
  }
  menu.hidden = true;
  if (act === "cancel") return true;
  if (act === "bd-rename-yes") {
    const v = (menu.querySelector(".bd-name") || {}).value || "";
    try { await api("POST", `/api/boards/${s.board_id}/rename`, { title: v }); } catch (err) { toast(err.message); }
    refresh();
    return true;
  }
  if (act === "bd-delete-yes") {
    try { await api("DELETE", `/api/boards/${s.board_id}`); toast("칸을 지웠습니다"); } catch (err) { toast(err.message); }
    BD.docs.delete(sid);
    refresh();
    return true;
  }
  if (act === "bd-copy") {
    try { await navigator.clipboard.writeText(s.path); toast("경로를 복사했습니다"); } catch (err) { toast(s.path); }
    return true;
  }
  if (act === "bd-reopen") {
    try { await termApi("POST", `/api/boards/${s.board_id}/reopen`, {}); } catch (err) { toast(err.message); }
    refresh();
    return true;
  }
  if (act === "archive") {
    try { await api("POST", `/api/sessions/${sid}/archive`, { archived: !s.archived }); toast(s.archived ? "보관을 풀었습니다" : "보관했습니다"); }
    catch (err) { toast(err.message); }
    refresh();
    return true;
  }
  return false;   // pin · width-reset 은 세션 칸과 같은 처리로
}
// 카드 보기의 보드 칸 카드(누르면 칸 보기에서 그 칸으로)
function boardCardHtml(s, rank) {
  const [icon, label] = BOARD_KIND[s.board] || ["", ""];
  const key = rank <= 9 ? `<span class="sc-key">⌘${rank}</span>` : "";
  return `<article class="scard idle ${pinOf(s) ? "pinned" : ""}" data-scard="${esc(s.session_id)}" style="--proj:${projColor(s.project_root || s.cwd)}">
    <div class="sc-top"><span class="rank">${rank}</span><span class="chip board-kind">${icon} ${label}</span>
      <span class="ago" data-ts="${s.last_activity}">${rel(s.last_activity)}</span><span class="spacer"></span>
      ${pinOf(s) ? '<span class="sc-pin" title="고정됨">📌</span>' : ""}${key}</div>
    <div class="sc-title">${esc(s.title)}</div>
    <div class="sc-meta"><span class="chip proj">${esc(s.project_name || base(s.project_root))}</span></div>
    <div class="sc-recap-none muted">${s.board === "md" ? esc(boardRelPath(s)) : esc(s.cwd.replace(/^\/Users\/[^/]+/, "~"))}</div>
  </article>`;
}
