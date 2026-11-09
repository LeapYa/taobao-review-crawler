#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
离线验证抽取逻辑：用一份"仿淘宝评价面板"的本地 HTML 跑 EXTRACT_JS，
确认选择器 + 字段解析 + 去重 + CSV 输出整条链路可用（不需要登录、不碰线上）。

python test_extract.py
"""
import csv
import os
import sys

from selenium import webdriver
from selenium.webdriver.chrome.service import Service

from taobao_reviews_pro import EXTRACT_JS, FIELDS, TaobaoReviewScraper

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "fake_review_panel.html")
DRIVER = os.path.join(HERE, "drivers", "chromedriver.exe")

CARDS = [
    ("星***5", "颜色分类: 曜石黑", "2026-03-12", "降噪效果非常明显，地铁上基本听不到报站声，续航够一天通勤。", "追评：用了两个月稳定性可以"),
    ("l***8", "颜色分类: 星光白", "2026-04-02", "戴一整天耳朵不痛，耳塞有三种尺寸可以换，好评。", ""),
    ("a***1", "规格: 尊享版", "2026-04-18", "用了一周右耳突然没声音了，联系客服只说返修，体验很差。", ""),
    ("m***3", "颜色分类: 雾霾蓝", "2026-05-06", "东西还行，就是包装有点简单，没有想象中精致。", ""),
    ("q***9", "颜色分类: 曜石黑", "2026-05-21", "连接经常断，尤其在人多的商场，根本没法用。", "追评：售后给换了新的"),
    ("z***2", "规格: 标准版", "2026-06-01", "此用户没有填写评价。", ""),
    ("w***7", "颜色分类: 星光白", "2026-06-15", "音质在这个价位很能打，低频有下潜但不浑，人声清晰。", ""),
    ("e***4", "颜色分类: 曜石黑", "2026-07-03", "客服态度差，问三句回一句，售后流程拖了半个月。", ""),
]


def build_fixture():
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    items = []
    for i, (user, sku, date, content, ap) in enumerate(CARDS):
        ap_html = f'<div class="appendContent--a1">追评：{ap.replace("追评：","")}</div>' if ap else ""
        items.append(f"""
      <div class="commentItem--c{i}">
        <div class="userInfo--u"><span class="userName--un">{user}</span></div>
        <div class="skuInfo--s"><span>{sku}</span><span class="date--d">{date}</span></div>
        <div class="content--uonoOhaz">{content}</div>
        {ap_html}
        <div class="footer--f"><span>点赞 3</span></div>
      </div>""")
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>fixture</title></head>
<body>
<div class="Drawer--nzZd5HyY detailClassName--hEQeQoBa">
  <div class="content--nuxTngci detailContentClassName--L4MaK8TB">
    <div class="Comments--eCO6Uz4o clearfix">
      <div class="comments--ChxC7GEN beautify-scroll-bar">
        {''.join(items)}
      </div>
    </div>
  </div>
</div>
</body></html>"""
    with open(FIXTURE, "w", encoding="utf-8") as f:
        f.write(html)
    return FIXTURE


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    path = build_fixture()
    print(f"[fixture] {path}")

    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,900")
    svc = Service(DRIVER) if os.path.exists(DRIVER) else None
    d = webdriver.Chrome(options=opts, service=svc) if svc else webdriver.Chrome(options=opts)
    try:
        d.get("file:///" + path.replace("\\", "/"))
        res = d.execute_script(EXTRACT_JS)
    finally:
        d.quit()

    items = res.get("items", [])
    print(f"[抽取] root={res.get('rootClass','')[:40]}  items={len(items)}")
    for it in items[:3]:
        print("   ", {k: (v[:30] if isinstance(v, str) else v) for k, v in it.items() if k != "raw"})

    s = TaobaoReviewScraper(out_csv=os.path.join(HERE, "data", "reviews_fixture.csv"))
    rows = [r for r in (s.to_row(it, "999999", "fixture 商品", "file://fixture") for it in items) if r]
    out = os.path.join(HERE, "data", "reviews_fixture.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"[CSV] {len(rows)} 行 -> {out}")

    ok = len(items) == len(CARDS) and len(rows) >= len(CARDS) - 1
    print("[结果]", "PASS ✅" if ok else "FAIL ❌")
    for r in rows[:8]:
        print(f"   {r['review_time'] or '----------'} | {r['user']:<8} | {r['sku'][:12]:<12} | {r['content'][:34]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
