"""
根据 dashboard_data.json 生成单文件交互式 HTML 操作台。
- 内嵌数据（无外部依赖，浏览器双击即可打开）
- 支持：状态筛选、线索搜索、点击改状态（查证/固证/排除/立案）、补证建议、处置审计
注：前端改状态仅 localStorage 暂存演示，不回写 DuckDB（回写需后端 API，见 README）。
"""
import json, os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "output", "dashboard_data.json")
OUT = os.path.join(BASE, "output", "dashboard.html")

with open(DATA, encoding="utf-8") as f:
    payload = json.load(f)

data_json = json.dumps(payload, ensure_ascii=False)

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>孙武侦查官 · 线索处置操作台</title>
<style>
  :root{
    --bg:#0f1419; --panel:#1a2230; --panel2:#232d3f; --line:#2e3a50;
    --txt:#e6edf3; --sub:#8b97a7; --acc:#4f9cff; --acc2:#7ee787;
    --warn:#f0b429; --danger:#f85149; --purple:#bc8cff;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
    background:var(--bg);color:var(--txt);font-size:14px;line-height:1.6}
  header{padding:16px 24px;background:var(--panel);border-bottom:1px solid var(--line);
    display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  header h1{font-size:18px;margin:0;font-weight:600}
  header .sub{color:var(--sub);font-size:12px}
  .stats{display:flex;gap:12px;margin-left:auto;flex-wrap:wrap}
  .stat{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
    padding:8px 14px;min-width:88px}
  .stat .num{font-size:20px;font-weight:700}
  .stat .lbl{font-size:11px;color:var(--sub)}
  .st-待查 .num{color:var(--warn)} .st-查证中 .num{color:var(--acc)}
  .st-已固证 .num{color:var(--purple)} .st-已立案 .num{color:var(--acc2)}
  .st-已排除 .num{color:var(--sub)}
  main{padding:20px 24px;display:grid;grid-template-columns:1fr 360px;gap:20px}
  @media(max-width:900px){main{grid-template-columns:1fr}}
  .toolbar{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}
  input,select,button{background:var(--panel2);border:1px solid var(--line);color:var(--txt);
    border-radius:6px;padding:7px 11px;font-size:13px}
  input{flex:1;min-width:180px}
  button{cursor:pointer}
  button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
  .clue{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:14px 16px;margin-bottom:12px;cursor:pointer;transition:.15s}
  .clue:hover{border-color:var(--acc)}
  .clue.expanded{border-color:var(--acc2)}
  .clue h3{margin:0 0 6px;font-size:15px;display:flex;align-items:center;gap:8px}
  .tags{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
  .tag{font-size:11px;padding:2px 8px;border-radius:10px;background:var(--panel2)}
  .tag.jian{background:rgba(188,140,255,.18);color:var(--purple)}
  .badge{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600}
  .b-待查{background:rgba(240,180,41,.18);color:var(--warn)}
  .b-查证中{background:rgba(79,156,255,.18);color:var(--acc)}
  .b-已固证{background:rgba(188,140,255,.18);color:var(--purple)}
  .b-已立案{background:rgba(126,231,135,.18);color:var(--acc2)}
  .b-已排除{background:rgba(139,151,167,.18);color:var(--sub)}
  .meta{color:var(--sub);font-size:12px}
  .detail{display:none;margin-top:12px;padding-top:12px;border-top:1px dashed var(--line)}
  .clue.expanded .detail{display:block}
  .detail h4{font-size:12px;color:var(--sub);margin:10px 0 4px;text-transform:uppercase}
  .rows{background:var(--bg);border-radius:6px;padding:8px 10px;font-size:12px;
    max-height:160px;overflow:auto;font-family:monospace;white-space:pre-wrap}
  .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
  .actions button{font-size:12px;padding:5px 10px}
  .right{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:16px;position:sticky;top:20px;max-height:calc(100vh - 40px);overflow:auto}
  .right h2{font-size:15px;margin:0 0 12px}
  .jian-coverage{font-size:12px}
  .jian-coverage div{padding:5px 0;border-bottom:1px dashed var(--line)}
  .jian-coverage b{color:var(--purple)}
  .suggestion{background:rgba(240,180,41,.12);border-left:3px solid var(--warn);
    padding:8px 12px;border-radius:4px;font-size:12px;margin-top:8px}
  .audit{font-size:12px;color:var(--sub);margin-top:6px}
  .empty{color:var(--sub);text-align:center;padding:40px}
  footer{padding:14px 24px;color:var(--sub);font-size:11px;border-top:1px solid var(--line)}
  .redline{color:var(--danger);font-weight:600}
</style>
</head>
<body>
<header>
  <div>
    <h1>🛡️ 孙武侦查官 · 线索处置操作台</h1>
    <div class="sub">DuckDB 单机栈 · 数据驱动假设覆盖 · AI 不出定性，须正兵依法定程序置位</div>
  </div>
  <div class="stats" id="stats"></div>
</header>
<main>
  <section>
    <div class="toolbar">
      <input id="search" placeholder="🔍 搜索线索标题 / 详情...">
      <select id="filter">
        <option value="">全部状态</option>
        <option value="待查">待查</option>
        <option value="查证中">查证中</option>
        <option value="已固证">已固证</option>
        <option value="已立案">已立案</option>
        <option value="已排除">已排除</option>
      </select>
      <button onclick="resetAll()">重置筛选</button>
    </div>
    <div id="list"></div>
  </section>
  <aside class="right">
    <h2>📋 处置说明</h2>
    <div id="crossLevel" style="font-size:13px;margin-bottom:12px"></div>
    <h2>🎯 五间覆盖</h2>
    <div class="jian-coverage" id="coverage"></div>
    <h2 style="margin-top:16px">💡 使用提示</h2>
    <div class="audit">
      · 点击线索卡片展开详情 / 溯源行<br>
      · 点状态按钮可本地切换（演示用，存 localStorage）<br>
      · <span class="redline">已立案</span> 须由具名正兵经"已固证"迁移并填法定依据<br>
      · 生产环境回写需对接后端 API
    </div>
  </aside>
</main>
<footer>
  孙武侦查官 DuckDB 版 · 六段 = 线索流投影 · 假设覆盖完整性由血缘去重 + 优先级 + 处置状态闭环保障
</footer>

<script>
const DATA = __DATA__;
const STATUS_ORDER = ["已立案","已固证","查证中","待查","已排除"];
const STORAGE_KEY = "sunwu_disposal_v1";

// 本地处置覆盖层（演示：不回写 DuckDB）
let local = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
function save(){ localStorage.setItem(STORAGE_KEY, JSON.stringify(local)); }

function getStatus(c){ return local[c.clue_id]?.status || c.status; }
function getNote(c){ return local[c.clue_id]?.note || c.note; }

function renderStats(){
  const counts = {};
  DATA.clues.forEach(c => { const s = getStatus(c); counts[s] = (counts[s]||0)+1; });
  const order = ["查证中","已立案","已固证","待查","已排除"];
  const html = order.filter(s=>counts[s]).map(s=>{
    const cls = s.replace(/[^a-z]/gi,"");
    return `<div class="stat st-${cls}"><div class="num">${counts[s]}</div><div class="lbl">${s}</div></div>`;
  }).join("");
  document.getElementById("stats").innerHTML = html;
}

function suggestion(clue){
  const s = getStatus(clue);
  const jian = (clue.jian_types||[]).join("");
  if(s==="已立案") return "已立案：依法定程序推进，做好卷宗归档与证据链闭环";
  if(s==="已固证" && jian.includes("反间")) return "补证建议：建议正兵取言词、固定电子数据";
  if(s==="已固证") return "已固证：证据链完整，可提请立案审查";
  if(s==="查证中" && jian.includes("生间")) return "补证建议：调取银行流水原件、询问证人（生间）";
  if(s==="查证中" && jian.includes("反间")) return "补证建议：调取工商内档、追查资金穿透（反间）";
  if(s==="已排除") return "提示：已排除，记录存档备查";
  return "待查：建议先采样预演，再决定查证方向";
}

function renderList(){
  const q = document.getElementById("search").value.trim().toLowerCase();
  const f = document.getElementById("filter").value;
  let list = DATA.clues.filter(c=>{
    const match = !q || c.title.toLowerCase().includes(q) || (c.detail||"").toLowerCase().includes(q);
    return match && (!f || getStatus(c)===f);
  });
  list.sort((a,b)=> STATUS_ORDER.indexOf(getStatus(a)) - STATUS_ORDER.indexOf(getStatus(b)));

  const box = document.getElementById("list");
  if(!list.length){ box.innerHTML = `<div class="empty">无匹配线索</div>`; return; }
  box.innerHTML = list.map(c=>{
    const s = getStatus(c);
    const rows = (c.source_rows||[]).slice(0,8);
    const rowsTxt = rows.length ? JSON.stringify(rows,null,1) : "（无独立原始行，由交叉升格生成）";
    const sug = suggestion(c);
    return `
    <div class="clue" id="card_${c.clue_id}" onclick="toggle('${c.clue_id}')">
      <h3>${c.title} <span class="badge b-${s}">${s}</span></h3>
      <div class="meta">${c.detail||""}</div>
      <div class="tags">
        ${(c.jian_types||[]).map(j=>`<span class="tag jian">${j}</span>`).join("")}
        ${(c.assumption_chain||[]).map(h=>`<span class="tag">${h}</span>`).join("")}
        ${c.needs_human_review ? '<span class="tag" style="color:var(--danger)">须人工复核</span>' : ""}
      </div>
      ${sug ? `<div class="suggestion">${sug}</div>` : ""}
      <div class="detail">
        <h4>定性策略</h4><div class="audit">${c["定性_policy"]||"AI 不出定性，须言词证据 + 法定程序"}</div>
        <h4>溯源原始行（${rows.length}）</h4><div class="rows">${rowsTxt}</div>
        <h4>处置审计</h4><div class="audit">操作人：${c.operator||"—"} · 更新：${c.updated_at||"—"} · 备注：${getNote(c)||"—"}</div>
        <div class="actions" onclick="event.stopPropagation()">
          <button onclick="trans('${c.clue_id}','查证中','')">→ 查证中</button>
          <button onclick="trans('${c.clue_id}','已固证','')">→ 已固证</button>
          <button onclick="trans('${c.clue_id}','已排除',prompt('排除理由：')||'')">→ 已排除</button>
          <button class="pri" onclick="fileClue('${c.clue_id}')">⚖️ 已立案（须固证+法定依据）</button>
        </div>
      </div>
    </div>`;
  }).join("");
}

function toggle(id){ document.getElementById("card_"+id).classList.toggle("expanded"); }

function trans(id, status, note){
  if(status==="已排除" && !note){ alert("已排除须填写理由"); return; }
  local[id] = { status, note, operator: "正兵（演示）", updated_at: new Date().toISOString() };
  save(); renderStats(); renderList();
}

function fileClue(id){
  const cur = local[id]?.status || (DATA.clues.find(c=>c.clue_id===id)||{}).status;
  if(cur !== "已固证"){ alert("红线：须由「已固证」迁移，禁止跳步直接立案"); return; }
  const basis = prompt("请输入法定依据（案号 / 审批文号）：");
  if(!basis){ alert("立案须填写法定依据"); return; }
  local[id] = { status:"已立案", note:"法定程序完备："+basis, operator:"王检察官（演示）", updated_at:new Date().toISOString() };
  save(); renderStats(); renderList();
  alert("✅ 已立案（演示态）。生产环境须由具名正兵依法定程序置位。");
}

function resetAll(){ document.getElementById("search").value=""; document.getElementById("filter").value=""; renderList(); }

function renderCoverage(){
  const cov = DATA.jian_coverage||{};
  document.getElementById("coverage").innerHTML = Object.entries(cov).map(([k,v])=>
    `<div><b>${k}</b>（${v.length}）：<span style="color:var(--sub)">${v.join(", ")}</span></div>`).join("");
  document.getElementById("crossLevel").innerHTML =
    `交叉等级：<b style="color:var(--acc2)">${DATA.cross_level||"—"}</b><br><span class="meta">覆盖间类 ${Object.keys(cov).length}/5</span>`;
}

document.getElementById("search").oninput = renderList;
document.getElementById("filter").onchange = renderList;
renderStats(); renderCoverage(); renderList();
</script>
</body>
</html>
"""

html = HTML.replace("__DATA__", data_json)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print("written:", OUT, "size:", os.path.getsize(OUT), "bytes")
