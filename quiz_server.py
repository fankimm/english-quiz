#!/usr/bin/env python3
"""로컬 영어 문장 퀴즈 서버 (Day별 · 배치 채점용).

- 매일 5문장씩 Day 단위로 묶음. 브라우저에서 Day 골라서 한국어 뜻 보고 영어로 입력.
- "채점 요청" 누르면 답이 submissions/day_N.json 에 저장됨 (정답 키 포함).
- 그 다음 클로드한테 "Day N 채점해줘" 하면 그 파일을 읽어서 채점 + 뭐가 틀렸는지 설명.
- 순서는 섞지 않음 (입력 순서 그대로).

실행:  python3 quiz_server.py        (기본 포트 4321)
        python3 quiz_server.py 4400   (포트 지정)
"""
import json, os, re, sys, shutil, subprocess, tempfile, unicodedata
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SUB_DIR = os.path.join(HERE, "submissions")
os.makedirs(SUB_DIR, exist_ok=True)

DAY_SIZE = 5  # 하루 5문장

# claude CLI 경로 (headless 채점용)
CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
# 채점 모델: Opus 불필요, Haiku 4.5 로 빠르게 (필요하면 "sonnet" 으로 올리면 됨)
GRADE_MODEL = "haiku"

GRADING_SYS = (
    "너는 영어 작문 채점기다. 절대 번역기로 동작하지 마라. "
    "사용자가 한국어 뜻을 보고 영어로 작문한 것을 채점한다. "
    "어떤 도구도 쓰지 말고 오직 텍스트로만 답하라."
)

ONE_ITEM_RUBRIC = """다음 영어 작문 1개를 채점해라.

채점 기준 (관대하게):
- 대소문자, 마침표·쉼표·물음표 등 구두점, 하이픈, 여분 공백 차이는 전부 무시하고 정답 처리.
- 어퍼스트로피(') 유무만 다른 건 무조건 정답 처리. 예: its=it's, im=I'm, dont=don't, lets=let's, thats=that's, ill=I'll. 절대 어퍼스트로피·축약 표기로 감점하지 마라.
- gonna=going to, kinda=kind of, wanna=want to 같은 구어 축약형은 같은 것으로 인정.
- 의미·핵심 표현·어순이 맞으면 ⭕. 모범답안과 표현이 달라도 뜻이 같고 자연스러운 영어면 ⭕.
- 핵심 의미를 바꾸지 않는 사소한 단어 누락/추가(관사, 약한 부사 등)는 🔺.
- 뜻이 틀리거나 표현이 부적절해 의미가 통하지 않으면 ❌.

출력은 정확히 아래 형식의 플레인 텍스트만. 군더더기·마크다운·인사말 금지:
첫 줄: ⭕ 또는 🔺 또는 ❌ 중 하나만 (다른 글자 붙이지 말 것)
둘째 줄(🔺·❌일 때만): 코멘트: <무엇이 어떻게 틀렸는지 한국어로 1줄>

채점 대상:
"""


def _grade_one(r):
    """단일 문항 채점 → {idx, mark, block} 반환."""
    prompt = (ONE_ITEM_RUBRIC +
              f'한국어 뜻: {r["ko"]}\n모범답안: {r["answer_key"]}\n내가 쓴 답: {r["user"]}')
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--output-format", "text",
             "--model", GRADE_MODEL, "--strict-mcp-config",
             "--append-system-prompt", GRADING_SYS],
            capture_output=True, text=True, timeout=150,
            cwd=tempfile.gettempdir(),  # language/CLAUDE.md(번역 규칙) 격리
        )
        res = (proc.stdout or "").strip() or "❌\n코멘트: [채점 출력 없음]"
    except FileNotFoundError:
        res = f"❌\n코멘트: [claude CLI 없음: {CLAUDE_BIN}]"
    except subprocess.TimeoutExpired:
        res = "❌\n코멘트: [시간 초과]"
    lines = [ln for ln in res.splitlines() if ln.strip()]
    first = lines[0] if lines else "❌"
    mark = "⭕" if "⭕" in first else ("🔺" if "🔺" in first else "❌")
    comment = next((ln.strip() for ln in lines if ln.strip().startswith("코멘트")), "")
    block = f'문항 {r["idx"]+1}  {mark}\n  내 답: {r["user"]}\n  모범답안: {r["answer_key"]}'
    if mark != "⭕" and comment:
        block += f"\n  {comment}"
    return {"idx": r["idx"], "mark": mark, "block": block}


# ===== 논클로드(정규화) 선채점: 틀린 것만 Claude로 보내 속도 향상 =====
_CONTRACTIONS = {"gonna": "going to", "kinda": "kind of", "wanna": "want to",
                 "gotta": "got to", "lemme": "let me", "gimme": "give me", "dunno": "dont know"}


def _normalize(s):
    t = unicodedata.normalize("NFC", s.lower())
    t = t.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"[–—]", " ", t)
    t = re.sub(r"[a-z]+", lambda m: _CONTRACTIONS.get(m.group(0), m.group(0)), t)
    t = t.replace("'", "").replace('"', "")
    t = re.sub(r"[.,!?;:()\-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _similarity(a, b):
    m, n = len(a), len(b)
    if m == 0 and n == 0:
        return 1.0
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            cur = dp[j]
            dp[j] = prev if a[i - 1] == b[j - 1] else 1 + min(dp[j], dp[j - 1], prev)
            prev = cur
    return 1 - dp[n] / max(m, n)


def _local_mark(user, ans):
    nu, na = _normalize(user), _normalize(ans)
    if nu == na:
        return "⭕"
    return "🔺" if _similarity(nu.split(), na.split()) >= 0.7 else "❌"


def run_grading(rows):
    """먼저 정규화로 선채점 → ⭕는 즉시 확정, 🔺/❌만 Claude가 검토."""
    from concurrent.futures import ThreadPoolExecutor
    filled = [r for r in rows if r["user"].strip()]
    if not filled:
        return []
    filled.sort(key=lambda r: r["idx"])
    ok = [r for r in filled if _local_mark(r["user"], r["answer_key"]) == "⭕"]
    wrong = [r for r in filled if _local_mark(r["user"], r["answer_key"]) != "⭕"]
    items = [{"idx": r["idx"], "mark": "⭕",
              "block": f'문항 {r["idx"]+1}  ⭕\n  내 답: {r["user"]}\n  모범답안: {r["answer_key"]}'}
             for r in ok]
    if wrong:  # 틀린 것만 Claude 검토 (⭕로 올려줄 수도 있음)
        with ThreadPoolExecutor(max_workers=min(12, len(wrong))) as ex:
            items.extend(ex.map(_grade_one, wrong))
    items.sort(key=lambda x: x["idx"])
    return items

DATA = [
    {"en": "I'm kind of swamped right now.", "ko": "나 지금 일이 좀 밀려서 정신없어."},
    {"en": "Let me get back to you on that.", "ko": "그건 좀 있다 다시 말해줄게."},
    {"en": "It's been a while, huh?", "ko": "오랜만이네, 그치?"},
    {"en": "I'll probably just stay in tonight.", "ko": "오늘 밤엔 그냥 집에 있을 것 같아."},
    {"en": "Honestly, it's not a big deal.", "ko": "솔직히 별일 아니야."},
    {"en": "I'm gonna grab a quick bite — want anything?", "ko": "나 간단히 뭐 좀 먹으려는데 — 너 뭐 필요한 거 있어?"},
    {"en": "Yeah, that works for me.", "ko": "응, 난 그거 괜찮아."},
    {"en": "Sorry, my mind went blank for a sec.", "ko": "미안, 잠깐 머리가 하얘졌어."},
    {"en": "Let's just play it by ear.", "ko": "그냥 상황 봐가면서 하자."},
    {"en": "I'll keep you posted.", "ko": "진행되는 대로 계속 알려줄게."},
    {"en": "I'm not gonna lie, I'm kinda nervous.", "ko": "솔직히 좀 긴장돼."},
    {"en": "It slipped my mind.", "ko": "깜빡했어."},
    {"en": "I'm down for that.", "ko": "나 그거 콜이야."},
    {"en": "I totally lost track of time.", "ko": "시간 가는 줄 완전 몰랐어."},
    {"en": "That's exactly what I meant.", "ko": "내 말이 바로 그거야."},
    {"en": "My bad, I didn't catch that.", "ko": "미안, 못 알아들었어."},
    {"en": "Let's call it a day.", "ko": "오늘은 이만하자."},
    {"en": "It's worth a shot.", "ko": "한번 해볼 만해."},
    {"en": "I'm running a bit late.", "ko": "나 조금 늦을 것 같아."},
    {"en": "That makes sense.", "ko": "그거 말 되네."},
    {"en": "Let's not get ahead of ourselves.", "ko": "너무 앞서가지 말자."},
    {"en": "I'll take a rain check.", "ko": "다음 기회로 미룰게."},
    {"en": "That's easier said than done.", "ko": "말이야 쉽지."},
    {"en": "Don't read too much into it.", "ko": "너무 깊게 해석하지 마."},
    {"en": "I'm on the fence about it.", "ko": "그거 아직 마음 못 정했어."},
    {"en": "Let me sleep on it.", "ko": "좀 더 생각해보고 정할게."},
    {"en": "That came out of nowhere.", "ko": "그거 완전 뜬금없었어."},
    {"en": "I can't wrap my head around it.", "ko": "도무지 이해가 안 돼."},
    {"en": "Let's keep in touch.", "ko": "계속 연락하고 지내자."},
    {"en": "You took the words right out of my mouth.", "ko": "내가 하려던 말이 딱 그거야."},
    {"en": "It's a long story.", "ko": "얘기하자면 길어."},
    {"en": "No worries, it happens.", "ko": "괜찮아, 그럴 수 있지."},
    {"en": "Let's grab a coffee sometime.", "ko": "언제 커피 한잔하자."},
    {"en": "That's news to me.", "ko": "그거 처음 듣는 얘긴데."},
    {"en": "I'll let it slide this time.", "ko": "이번엔 그냥 넘어가 줄게."},
]

NUM_DAYS = (len(DATA) + DAY_SIZE - 1) // DAY_SIZE


def day_indices(day):  # 1-based day → 전역 인덱스 리스트
    start = (day - 1) * DAY_SIZE
    return list(range(start, min(start + DAY_SIZE, len(DATA))))


PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>영어 문장 퀴즈 (클코 채점)</title>
<style>
  :root{--bg:#0f1115;--card:#1a1d24;--card2:#21252e;--text:#e6e8ec;--muted:#8b909a;--accent:#5b9dff;--border:#2c313b;--ok:#34c77b;}
  *{box-sizing:border-box;}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--text);display:flex;justify-content:center;padding:28px 16px 120px;}
  .wrap{width:100%;max-width:640px;}
  h1{font-size:19px;margin:0 0 10px;}
  .sub{color:var(--muted);font-size:13px;margin-bottom:18px;}
  .tabs{display:flex;gap:4px;margin-bottom:18px;border-bottom:1px solid var(--border);}
  .tab{background:none;border:none;color:var(--muted);font-size:15px;font-weight:600;padding:10px 16px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;}
  .tab.active{color:var(--text);border-bottom-color:var(--accent);}
  .ans-day{font-size:13px;color:var(--accent);font-weight:700;margin:20px 0 8px;letter-spacing:.03em;}
  .ans-item{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:8px;}
  .ans-item .ko{font-size:14px;color:var(--muted);font-weight:500;margin-bottom:4px;line-height:1.4;}
  .ans-item .en{font-size:16px;font-weight:600;line-height:1.4;}
  .ans-item .n{font-size:11px;color:var(--muted);margin-bottom:5px;}
  .days{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:22px;}
  .day{background:var(--card2);border:1px solid var(--border);color:var(--text);border-radius:20px;padding:8px 16px;font-size:14px;font-weight:600;cursor:pointer;}
  .day.active{background:var(--accent);color:#fff;border-color:var(--accent);}
  .q{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin-bottom:12px;}
  .num{font-size:12px;color:var(--muted);margin-bottom:6px;}
  .ko{font-size:18px;font-weight:600;margin-bottom:12px;line-height:1.45;}
  input{width:100%;background:var(--card2);border:1px solid var(--border);border-radius:9px;padding:12px 14px;font-size:16px;color:var(--text);outline:none;}
  input:focus{border-color:var(--accent);}
  .barwrap{position:fixed;left:0;right:0;bottom:0;background:rgba(15,17,21,.95);border-top:1px solid var(--border);padding:14px 16px;display:flex;justify-content:center;backdrop-filter:blur(6px);}
  .barinner{width:100%;max-width:640px;display:flex;gap:12px;align-items:center;}
  .count{font-size:13px;color:var(--muted);}
  .count b{color:var(--text);}
  .barinner .count{margin-right:auto;}
  .gradebtn{border:none;border-radius:9px;padding:12px 18px;font-size:14px;font-weight:600;background:var(--accent);color:#fff;cursor:pointer;}
  .gradebtn:disabled{opacity:.4;cursor:default;}
  #gradeDay{background:var(--card2);color:var(--accent);border:1px solid var(--accent);}
  .ghostbtn{border:1px solid var(--border);border-radius:9px;padding:12px 14px;font-size:13px;font-weight:600;background:var(--card2);color:var(--muted);cursor:pointer;}
  .result{display:none;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:14px;}
  .result.show{display:block;}
  .result.ok{border-color:var(--ok);}
  .result .big{font-size:16px;font-weight:700;margin-bottom:10px;}
  .result.ok .big{color:var(--ok);}
  .result .grade{white-space:pre-wrap;font-size:14.5px;line-height:1.65;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
  .result .hint{font-size:13px;color:var(--muted);margin-top:10px;}
  .result .note{font-size:13px;color:var(--warn);margin-bottom:8px;}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px;margin-right:6px;}
  @keyframes spin{to{transform:rotate(360deg);}}
  /* 채점 중 모달 + PiP */
  .modal-back{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;justify-content:center;z-index:50;}
  .modal-back.show{display:flex;}
  .modal-card{background:var(--card);border:1px solid var(--accent);border-radius:14px;padding:26px 30px;font-size:17px;font-weight:600;display:flex;align-items:center;gap:12px;box-shadow:0 12px 48px rgba(0,0,0,.55);max-width:80vw;text-align:center;}
  .modal-hint{font-size:12px;color:var(--muted);font-weight:400;margin-top:8px;}
  .pip{position:fixed;bottom:84px;right:20px;background:var(--card);border:1px solid var(--accent);border-radius:24px;padding:10px 16px;font-size:13.5px;font-weight:600;display:none;align-items:center;gap:8px;z-index:50;cursor:pointer;box-shadow:0 6px 24px rgba(0,0,0,.45);}
  .pip.show{display:flex;}
  .pip .spinner{margin-right:2px;}
</style></head>
<body><div class="wrap">
  <h1>영어 문장 퀴즈</h1>
  <div class="tabs" id="tabs">
    <button class="tab active" data-view="quiz">문제 풀이</button>
    <button class="tab" data-view="answer">정답 보기</button>
  </div>
  <div id="quizView">
    <div class="sub">Day 골라서 영어로 입력 → <b>채점</b> 누르면 채점돼요. 엔터로 다음 문항, 입력·결과는 새로고침해도 유지.</div>
    <div class="days" id="days"></div>
    <div class="result" id="result"></div>
    <div id="list"></div>
  </div>
  <div id="answerView" style="display:none;">
    <div class="sub">정답 보기 — Day 선택 또는 전체. 풀기 전엔 안 보는 걸 추천 🙈</div>
    <div class="days" id="ansDays"></div>
    <div id="answerList"></div>
  </div>
</div>
<div class="barwrap"><div class="barinner">
  <span class="count"><b id="dayLabel">Day 1</b> · 작성 <b id="filled">0</b> / <b id="dayN">0</b></span>
  <button class="ghostbtn" id="resetDay">이 Day 초기화</button>
  <button class="ghostbtn" id="resetAll">전체 초기화</button>
  <button class="gradebtn" id="gradeDay">이 Day 채점</button>
  <button class="gradebtn" id="gradeAll">전체 채점</button>
</div></div>
<div class="modal-back" id="gradingModal"><div class="modal-card">
  <span class="spinner"></span>
  <div><span id="gradingMsg">채점 중…</span><div class="modal-hint">바깥을 누르면 작게 떠 있어요</div></div>
</div></div>
<div class="pip" id="gradingPip"><span class="spinner"></span><span id="pipMsg">채점 중…</span></div>
<script>
const NUM_DAYS = __NUM_DAYS__;
const DAY_DATA = __DAY_DATA__;   // {1:[{idx,ko}], 2:[...], ...}
let curDay = 1;
const escapeHtml = s => s.replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// 입력값 저장소 (idx → 영어답). localStorage 로 Day 전환·새로고침에도 보존
const STORE_KEY = 'quizAnswers';
let store = {};
try{ store = JSON.parse(localStorage.getItem(STORE_KEY) || '{}'); }catch(e){ store = {}; }
const saveStore = ()=>{ try{ localStorage.setItem(STORE_KEY, JSON.stringify(store)); }catch(e){} };

const daysEl = document.getElementById('days');
for(let d=1; d<=NUM_DAYS; d++){
  const b = document.createElement('button');
  b.className = 'day' + (d===1?' active':''); b.textContent = 'Day '+d; b.dataset.day=d;
  b.onclick = ()=>selectDay(d);
  daysEl.appendChild(b);
}

const list = document.getElementById('list');
function selectDay(d){
  curDay = d;
  [...daysEl.children].forEach(b=>b.classList.toggle('active', +b.dataset.day===d));
  document.getElementById('dayLabel').textContent = 'Day '+d;
  list.innerHTML = '';
  DAY_DATA[d].forEach(p=>{
    const el = document.createElement('div'); el.className='q';
    el.innerHTML = `<div class="num">문장 ${p.idx+1}</div><div class="ko"></div>
      <input type="text" data-idx="${p.idx}" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="영어로 입력…">`;
    el.querySelector('.ko').textContent = p.ko;
    el.querySelector('input').value = store[p.idx] || '';
    list.appendChild(el);
  });
  document.getElementById('dayN').textContent = DAY_DATA[d].length;
  bindInputs();
  window.scrollTo(0,0);
}
function bindInputs(){
  const inputs = [...document.querySelectorAll('input')];
  const refresh = ()=>document.getElementById('filled').textContent = inputs.filter(i=>i.value.trim()).length;
  inputs.forEach((inp,i)=>{
    inp.addEventListener('input', ()=>{
      const idx = +inp.dataset.idx;
      if(inp.value.trim()) store[idx] = inp.value; else delete store[idx];
      saveStore(); refresh();
    });
    inp.addEventListener('keydown', e=>{
      if(e.key!=='Enter') return;
      e.preventDefault();
      if(inputs[i+1]){
        inputs[i+1].focus();
        inputs[i+1].scrollIntoView({behavior:'smooth', block:'nearest'});
      } else if(curDay < NUM_DAYS){
        // 그날 마지막 문항 → 다음 Day 첫 문항으로
        selectDay(curDay+1);
        const first = document.querySelector('#list input');
        if(first){ first.focus(); first.scrollIntoView({behavior:'smooth', block:'center'}); }
      }
    });
  });
  refresh();
}
const res = document.getElementById('result');
// 채점 중 팝업 / PiP
const gModal = document.getElementById('gradingModal');
const gPip = document.getElementById('gradingPip');
function showGrading(msg){
  document.getElementById('gradingMsg').textContent = msg;
  document.getElementById('pipMsg').textContent = msg;
  gModal.classList.add('show'); gPip.classList.remove('show');
}
function hideGrading(){ gModal.classList.remove('show'); gPip.classList.remove('show'); }
gModal.addEventListener('click', e=>{ if(e.target===gModal){ gModal.classList.remove('show'); gPip.classList.add('show'); } });
gPip.addEventListener('click', ()=>{ gPip.classList.remove('show'); gModal.classList.add('show'); });

// 채점 결과 캐시: idx -> {ans(채점된 답), mark, block}. 답이 바뀐 문항만 재채점.
const GRADES_KEY = 'quizGrades';
let grades = {};
try{ grades = JSON.parse(localStorage.getItem(GRADES_KEY) || '{}'); }catch(e){ grades = {}; }
const saveGrades = ()=>{ try{ localStorage.setItem(GRADES_KEY, JSON.stringify(grades)); }catch(e){} };

function renderResult(note){
  const idxs = Object.keys(grades).map(Number).filter(i=>(store[i]||'').trim()).sort((a,b)=>a-b);
  if(!idxs.length && !note){ res.classList.remove('show'); return; }
  let score = 0;
  const blocks = idxs.map(i=>{
    const g = grades[i];
    score += g.mark==='⭕' ? 1 : (g.mark==='🔺' ? 0.5 : 0);
    const stale = g.ans !== (store[i]||'');
    return g.block + (stale ? '\\n  ⚠️ 답을 수정했어요 — 다시 채점하면 갱신됩니다' : '');
  });
  let html = '<div class="big">채점 결과</div>';
  if(note) html += `<div class="note">${escapeHtml(note)}</div>`;
  if(idxs.length) html += `<div class="grade">${escapeHtml(blocks.join('\\n\\n'))}\\n\\n점수: ${score} / ${idxs.length}</div>`;
  res.className = 'result show ok';
  res.innerHTML = html;
}

async function gradeScope(candidateIdxs, label){
  let filled = candidateIdxs.filter(i=>(store[i]||'').trim());
  if(!filled.length){ alert(`${label}에 작성한 답이 없어요.`); return; }
  // 마침표 자동: 문장부호로 안 끝나면 . 추가 (입력칸·저장소 반영)
  filled.forEach(i=>{ let v=(store[i]||'').trim();
    if(v && !/[.?!…]$/.test(v)){ v+='.'; store[i]=v;
      const inp=document.querySelector(`#list input[data-idx="${i}"]`); if(inp) inp.value=v; } });
  saveStore();
  // 캐시 없거나 답이 바뀐 문항만 실제 채점
  const need = filled.filter(i=> !grades[i] || grades[i].ans !== store[i]);
  const cached = filled.length - need.length;
  if(!need.length){ renderResult(`${label}: 새로 채점할 문항 없음 (${cached}개 모두 채점됨)`); res.scrollIntoView({behavior:'smooth',block:'start'}); return; }
  const btns = [document.getElementById('gradeDay'), document.getElementById('gradeAll')];
  btns.forEach(b=>b.disabled=true);
  showGrading(`${label} · ${need.length}문항 채점 중…${cached?` (${cached}개는 재사용)`:''}`);
  try{
    const answers = need.map(i=>({idx:i, user:store[i]}));
    const r = await fetch('/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answers})});
    const j = await r.json();
    (j.items||[]).forEach(it=>{ grades[it.idx] = {ans: store[it.idx], mark: it.mark, block: it.block}; });
    saveGrades();
    hideGrading();
    renderResult();
  }catch(e){
    hideGrading();
    res.className = 'result show';
    res.innerHTML = `<div class="big">채점 실패</div><div class="grade">${escapeHtml(String(e))}</div>`;
  }
  btns.forEach(b=>b.disabled=false);
}

document.getElementById('gradeDay').onclick = ()=> gradeScope(DAY_DATA[curDay].map(p=>p.idx), `Day ${curDay}`);
document.getElementById('gradeAll').onclick = ()=> gradeScope(
  Object.keys(DAY_DATA).flatMap(d=>DAY_DATA[d].map(p=>p.idx)), '전체');

document.getElementById('resetDay').onclick = ()=>{
  if(!confirm(`Day ${curDay} 입력과 채점 결과를 지울까요?`)) return;
  DAY_DATA[curDay].forEach(p=>{ delete store[p.idx]; delete grades[p.idx]; });
  saveStore(); saveGrades(); selectDay(curDay); renderResult();
};
document.getElementById('resetAll').onclick = ()=>{
  if(!confirm('모든 Day의 입력과 채점 결과를 전부 지울까요?')) return;
  store = {}; grades = {}; saveStore(); saveGrades(); res.classList.remove('show'); selectDay(curDay);
};

// 탭 전환 (문제 풀이 / 정답 보기)
const tabsEl = document.getElementById('tabs');
const quizView = document.getElementById('quizView');
const answerView = document.getElementById('answerView');
const barwrap = document.querySelector('.barwrap');
let answersLoaded = false;
tabsEl.addEventListener('click', e=>{
  const t = e.target.closest('.tab'); if(!t) return;
  [...tabsEl.children].forEach(b=>b.classList.toggle('active', b===t));
  const quiz = t.dataset.view==='quiz';
  quizView.style.display = quiz ? '' : 'none';
  answerView.style.display = quiz ? 'none' : '';
  barwrap.style.display = quiz ? '' : 'none';
  if(!quiz && !answersLoaded) loadAnswers();
  window.scrollTo(0,0);
});
let ansData = null, ansDay = 1;
async function loadAnswers(){
  const al = document.getElementById('answerList');
  al.innerHTML = '<div class="sub">불러오는 중…</div>';
  try{
    ansData = (await (await fetch('/answers')).json()).answers;
    const days = [...new Set(ansData.map(a=>a.day))];
    const ad = document.getElementById('ansDays'); ad.innerHTML = '';
    days.forEach(d=>{ const b=document.createElement('button'); b.className='day';
      b.textContent='Day '+d; b.dataset.day=d; b.onclick=()=>selectAnsDay(d); ad.appendChild(b); });
    const all=document.createElement('button'); all.className='day'; all.textContent='전체';
    all.dataset.day='all'; all.onclick=()=>selectAnsDay('all'); ad.appendChild(all);
    answersLoaded = true; selectAnsDay(1);
  }catch(e){ al.innerHTML = `<div class="sub">정답 불러오기 실패: ${escapeHtml(String(e))}</div>`; }
}
function selectAnsDay(d){
  ansDay = d;
  [...document.getElementById('ansDays').children].forEach(b=>b.classList.toggle('active', b.dataset.day===String(d)));
  const rows = d==='all' ? ansData : ansData.filter(a=>a.day===d);
  let html='', lastDay=0;
  rows.forEach(a=>{ if(d==='all'&&a.day!==lastDay){ html+=`<div class="ans-day">Day ${a.day}</div>`; lastDay=a.day; }
    html+=`<div class="ans-item"><div class="n">문장 ${a.idx+1}</div><div class="ko">${escapeHtml(a.ko)}</div><div class="en">${escapeHtml(a.en)}</div></div>`;
  });
  document.getElementById('answerList').innerHTML = html; window.scrollTo(0,0);
}

renderResult();   // 로드 시 캐시된 채점 결과 복원
selectDay(1);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            day_data = {d: [{"idx": i, "ko": DATA[i]["ko"]} for i in day_indices(d)]
                        for d in range(1, NUM_DAYS + 1)}
            page = (PAGE.replace("__NUM_DAYS__", str(NUM_DAYS))
                        .replace("__DAY_DATA__", json.dumps(day_data, ensure_ascii=False)))
            self._send(200, page, "text/html")
        elif self.path == "/answers":
            data = [{"idx": i, "day": i // DAY_SIZE + 1,
                     "ko": DATA[i]["ko"], "en": DATA[i]["en"]} for i in range(len(DATA))]
            self._send(200, json.dumps({"answers": data}, ensure_ascii=False))
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path != "/submit":
            self._send(404, json.dumps({"error": "not found"})); return
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        # 들어온 답 전체(어느 Day든)를 채점. idx → 사용자답
        by_idx = {int(a["idx"]): a.get("user", "") for a in payload.get("answers", [])
                  if 0 <= int(a["idx"]) < len(DATA)}
        rows = [{"idx": i, "ko": DATA[i]["ko"], "answer_key": DATA[i]["en"],
                 "user": by_idx.get(i, "")} for i in sorted(by_idx)]
        items = run_grading(rows)
        record = {"submitted_at": datetime.now().isoformat(timespec="seconds"),
                  "graded": len(items), "rows": rows, "items": items}
        with open(os.path.join(SUB_DIR, "latest.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        self._send(200, json.dumps({"ok": True, "items": items}))

    def log_message(self, *a):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4321
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"퀴즈 서버: http://127.0.0.1:{port}  ({NUM_DAYS} days × {DAY_SIZE}문장, 제출 → {SUB_DIR}/day_N.json)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
