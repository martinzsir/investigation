"""把 workspace 根的测试脚本迁入包内 tests/，路径改为相对路径（自包含）。"""
import re, os

BASE = "/data/workspace/孙武侦查官_DuckDB版"
root_js = "/data/workspace/test_dashboard.js"
root_e2e = "/data/workspace/test_dashboard_e2e.js"

# 相对路径改写：output/* 位于 ../output/
REL_MAP = {
    r"/data/workspace/孙武侦查官_DuckDB版/output/dashboard\.html":
        'process.argv[1].replace(/tests\\/[^/]+$/, "output/dashboard.html")',
    r"/data/workspace/孙武侦查官_DuckDB版/output/dashboard_data\.json":
        'process.argv[1].replace(/tests\\/[^/]+$/, "output/dashboard_data.json")',
}


def rel_path_lazy():
    return '(function(){ var p=require("path"); return p.join(__dirname,"..","output"); })()'


for src, dst in [(root_js, os.path.join(BASE, "tests", "test_dashboard.js")),
                 (root_e2e, os.path.join(BASE, "tests", "test_dashboard_e2e.js"))]:
    with open(src) as f:
        code = f.read()
    # 把绝对路径常量替换为基于 __dirname 的懒计算
    code = code.replace(
        'const html = fs.readFileSync("/data/workspace/孙武侦查官_DuckDB版/output/dashboard.html", "utf8");',
        'const html = fs.readFileSync(require("path").join(__dirname,"..","output","dashboard.html"), "utf8");')
    code = code.replace(
        'const REAL = JSON.parse(fs.readFileSync(\n  "/data/workspace/孙武侦查官_DuckDB版/output/dashboard_data.json", "utf8"));',
        'const REAL = JSON.parse(fs.readFileSync(\n  require("path").join(__dirname,"..","output","dashboard_data.json"), "utf8"));')
    with open(dst, "w") as f:
        f.write(code)
    print("✓", os.path.relpath(dst, BASE))
