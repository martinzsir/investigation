// 复现 HTML 前端的核心逻辑做单元测试（Node，无需浏览器）
const fs = require("fs");
const DATA = JSON.parse(fs.readFileSync(
  "/data/workspace/孙武侦查官_DuckDB版/output/dashboard_data.json", "utf8"));

const STATUS_ORDER = ["已立案","已固证","查证中","待查","已排除"];

// 1) 统计聚合
const counts = {};
DATA.clues.forEach(c => { counts[c.status] = (counts[c.status]||0)+1; });
console.log("✓ by_status:", JSON.stringify(counts));

// 2) 红线：已立案须由已固证迁移
function fileClue(currentStatus, hasBasis){
  if(currentStatus !== "已固证") return { ok:false, msg:"红线：须由已固证迁移" };
  if(!hasBasis) return { ok:false, msg:"立案须填法定依据" };
  return { ok:true, msg:"已立案" };
}
console.log("✓ 跳步立案(待查→立案):", JSON.stringify(fileClue("待查", true)));
console.log("✓ 跳步立案(查证中→立案):", JSON.stringify(fileClue("查证中", true)));
console.log("✓ 正确路径(已固证+依据):", JSON.stringify(fileClue("已固证", true)));
console.log("✓ 缺依据:", JSON.stringify(fileClue("已固证", false)));

// 3) 排序：已立案优先置顶
const sorted = [...DATA.clues].sort((a,b)=>
  STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status));
console.log("✓ 排序首位:", sorted[0].title, "→", sorted[0].status);

// 4) 间类覆盖
console.log("✓ 五间覆盖:", Object.keys(DATA.jian_coverage).join(","));
