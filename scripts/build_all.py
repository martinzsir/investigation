"""
一键构建操作台：导出处置数据 → 生成单文件 HTML → 运行前端逻辑测试。
用法：python scripts/build_all.py
"""
import subprocess, os, sys, shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def step(msg):
    print("\n=== " + msg + " ===")


def main():
    step("[1/4] 导出处置数据快照 (DuckDB → dashboard_data.json)")
    subprocess.run(["python", os.path.join(BASE, "scripts", "export_dashboard.py")], check=True)

    step("[2/4] 生成交互式 HTML (build_dashboard.py)")
    subprocess.run(["python", os.path.join(BASE, "scripts", "build_dashboard.py")], check=True)

    html_path = os.path.join(BASE, "output", "dashboard.html")
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    for needle in ["fileClue", "trans(", "已立案（须固证+法定依据）", "localStorage", "suggestion("]:
        assert needle in html, f"HTML 缺失: {needle}"
    print(f"✓ HTML: {html_path} ({os.path.getsize(html_path)} bytes), 关键函数齐全")

    step("[3/4] 前端逻辑测试 (Node: 状态机/红线/排序)")
    r = subprocess.run(["node", os.path.join(BASE, "tests", "test_dashboard.js")],
                        capture_output=True, text=True)
    print(r.stdout, r.stderr)
    assert r.returncode == 0
    r = subprocess.run(["node", os.path.join(BASE, "tests", "test_dashboard_e2e.js")],
                        capture_output=True, text=True)
    print(r.stdout, r.stderr)
    assert r.returncode == 0

    step("[4/4] 完成")
    print("✅ 操作台原型构建成功")
    print(f"   → 打开: {html_path}  (浏览器双击即开)")
    print(f"   → 说明: {os.path.join(BASE, 'output', 'dashboard_README.md')}")


if __name__ == "__main__":
    main()
