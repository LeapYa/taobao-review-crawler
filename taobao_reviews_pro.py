#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
淘宝商品评价采集（结构化版 / 生产可用版）

基于 olddove-laoge/Crawling-Taobao-product-reviews 改造，主要改动：
  1. 去掉硬编码 msedgedriver 路径 —— Selenium 4.6+ 由 Selenium Manager 自动匹配驱动
  2. 去掉阻塞式 input() 扫码 —— 支持 cookie 文件注入 / 首次人工扫码（轮询等待，不阻塞回调）
  3. 输出结构化 CSV/JSONL（内容、时间、规格、评分、追评、用户名）—— 可直接做舆情分析
  4. 断点续采 + 内容去重 + 目标条数控制（--target 2000）
  5. 多套选择器兜底 + 页面抓不到时可 --dump-html 保存快照便于调选择器

用法：
    # 1) 浏览器登录淘宝后导出 cookie 到 cookies.json（见 README_使用说明.md）
    python taobao_reviews_pro.py --url "https://item.taobao.com/item.htm?id=xxxx" --target 2000

    # 2) 没有 cookie 文件：首次运行会打开浏览器让你扫码登录，登录状态存进 profile
    python taobao_reviews_pro.py --url "https://item.taobao.com/item.htm?id=xxxx" --target 2000 --manual-login

    # 3) 调试选择器：保存当前评价面板 HTML
    python taobao_reviews_pro.py --url "..." --target 20 --dump-html debug/
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

DATE_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
LOGIN_KEYS = {"unb", "tracknick", "_nk_", "cookie17", "_l_g_", "lgc"}
SKU_HINT = ("颜色分类", "规格", "尺码", "型号", "套餐", "版本", "口味", "尺寸", "分类")
FIELDS = [
    "review_id",
    "product_id",
    "product_name",
    "user",
    "sku",
    "review_time",
    "rating",
    "content",
    "append_content",
    "is_default",
    "raw_text",
    "crawl_time",
    "source_url",
]


# --------------------------------------------------------------------------- #
# cookie 载入：支持 Chrome/EditThisCookie 导出的 JSON，也支持 Netscape cookies.txt
# --------------------------------------------------------------------------- #
def load_cookies(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"cookie 文件不存在: {path}")
    raw = open(path, "r", encoding="utf-8-sig").read().strip()

    if raw.startswith("[") or raw.startswith("{"):
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("cookies", [])
        out = []
        for c in data:
            item = {
                "name": c.get("name") or c.get("Name"),
                "value": c.get("value") or c.get("Value"),
                "domain": c.get("domain") or c.get("Domain") or ".taobao.com",
                "path": c.get("path") or c.get("Path") or "/",
            }
            if not item["name"] or item["value"] is None:
                continue
            # selenium 要求 domain 不能带前导点以外的花活，统一收敛到 taobao 域
            dom = str(item["domain"]).lstrip(".")
            if "taobao" in dom or "tmall" in dom:
                item["domain"] = "." + dom
            out.append(item)
        return out

    # Netscape cookies.txt：domain \t flag \t path \t secure \t expiry \t name \t value
    out = []
    for line in raw.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        dom, _flag, path_, _secure, _exp, name, value = parts[:7]
        out.append(
            {
                "name": name,
                "value": value,
                "domain": dom if dom.startswith(".") else "." + dom.lstrip("."),
                "path": path_ or "/",
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 页面抽取 JS：用 class 前缀匹配（淘宝 class 带 hash 后缀，前缀稳定）
# --------------------------------------------------------------------------- #
EXTRACT_JS = r"""
const want = ['comments--', 'Comments--'];
let root = null;
for (const p of want) {
  root = document.querySelector('[class*="' + p + '"]');
  if (root) break;
}
if (!root) {
  // 兜底：找包含"查看全部评价"的抽屉
  root = document.querySelector('div[class*="Drawer--"]') || document.body;
}

const txt = el => (el.innerText || el.textContent || '').replace(/\u00a0/g, ' ').trim();

// 1) 收集"评价正文"节点：class token 以 content-- 开头
const bodies = [];
for (const el of root.querySelectorAll('*')) {
  const cls = el.className;
  if (typeof cls !== 'string') continue;
  if (!cls.split(/\s+/).some(t => t.startsWith('content--'))) continue;
  const t = txt(el);
  if (t.length < 2 || t.length > 1200) continue;
  // 取最内层：后代里如果还有 content-- 且文本几乎相同，则跳过
  let inner = false;
  for (const c of el.querySelectorAll('*')) {
    const cc = c.className;
    if (typeof cc === 'string' && cc.split(/\s+/).some(x => x.startsWith('content--'))) {
      inner = true; break;
    }
  }
  if (inner) continue;
  bodies.push(el);
}
if (!bodies.length) {
  // 2) 兜底：评论卡片里带日期的块
  for (const el of root.querySelectorAll('div,li,section')) {
    const t = txt(el);
    if (t.length > 10 && t.length < 800 && /\d{4}[-/年.]\d{1,2}/.test(t)) {
      let hasChildCard = false;
      for (const c of el.children) {
        const ct = txt(c);
        if (ct.length > 10 && /\d{4}[-/年.]\d{1,2}/.test(ct)) { hasChildCard = true; break; }
      }
      if (!hasChildCard) bodies.push(el);
    }
  }
}

const dates = [];
const out = [];
for (const b of bodies) {
  // 向上爬，找到含日期的卡片容器
  let card = b;
  let cardText = txt(b);
  for (let i = 0; i < 6 && card.parentElement; i++) {
    card = card.parentElement;
    const t = txt(card);
    if (t.length > 2500) break;
    cardText = t;
    if (/\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}/.test(t)) break;
  }
  const dm = cardText.match(/(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})/);
  // 用户名：卡片内 class 含 user/nick 的节点
  let user = '';
  for (const sel of ['[class*="userName"]', '[class*="user--"]', '[class*="nick"]', '[class*="avatar"] + *']) {
    const n = card.querySelector(sel);
    if (n) { const v = txt(n); if (v && v.length < 40) { user = v; break; } }
  }
  // 规格：优先取独立叶子节点，避免 innerText 把日期拼进来
  let sku = '';
  for (const el of card.querySelectorAll('span,div,p')) {
    const s = txt(el);
    if (!s || s.length > 40) continue;
    if (/^(颜色分类|规格|尺码|型号|套餐|版本|口味|尺寸|分类)\s*[:：]/.test(s)) { sku = s; break; }
  }
  if (!sku) {
    const m = cardText.match(/(颜色分类|规格|尺码|型号|套餐|版本|口味|尺寸|分类)\s*[:：]\s*([^\n]{0,30}?)(?=\s*20\d{2}[-/年.]|\s*$)/);
    if (m) sku = m[1] + ': ' + (m[2] || '').trim();
  }
  sku = sku.replace(/20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}.*$/, '').trim();
  // 评分：优先 aria-label / 满星数量
  let rating = '';
  const rn = card.querySelector('[aria-label*="星"]');
  if (rn) {
    const m = (rn.getAttribute('aria-label') || '').match(/([1-5](\.\d)?)/);
    if (m) rating = m[1];
  }
  if (!rating) {
    const full = card.querySelectorAll('[class*="star"][class*="full"], [class*="Star"][class*="full"], [class*="rated"]');
    if (full.length && full.length <= 5) rating = String(full.length);
  }
  out.push({
    user: user,
    sku: sku,
    date: dm ? `${dm[1]}-${String(dm[2]).padStart(2,'0')}-${String(dm[3]).padStart(2,'0')}` : '',
    rating: rating,
    content: txt(b),
    raw: cardText.slice(0, 3000)
  });
}
return { count: out.length, items: out, rootClass: root ? (root.className || '') : '' };
"""


def content_hash(*parts: str) -> str:
    h = hashlib.md5()
    for p in parts:
        h.update((p or "").strip().encode("utf-8", "ignore"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


def parse_product_id(url: str) -> str:
    m = re.search(r"[?&]id=(\d+)", url)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
class TaobaoReviewScraper:
    def __init__(
        self,
        out_csv: str = "data/reviews.csv",
        profile_dir: str = "",
        cookie_file: str = "",
        headless: bool = False,
        manual_login: bool = False,
        login_timeout: int = 300,
        browser: str = "chrome",
        driver_path: str = "",
    ):
        self.out_csv = out_csv
        self.profile_dir = profile_dir or os.path.abspath(".taobao_profile")
        self.cookie_file = cookie_file
        self.headless = headless
        self.manual_login = manual_login
        self.login_timeout = login_timeout
        self.browser = browser.lower()
        self.driver_path = driver_path
        self.driver: webdriver.Chrome | None = None
        self.seen: set[str] = set()

    # ---------------- driver ----------------
    def build_driver(self):
        from selenium.webdriver.chrome.service import Service as ChromeService
        from selenium.webdriver.edge.service import Service as EdgeService

        opts = webdriver.ChromeOptions() if self.browser == "chrome" else webdriver.EdgeOptions()
        opts.add_argument(f"--user-data-dir={self.profile_dir}")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--window-size=1500,1000")
        opts.add_argument("--lang=zh-CN")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        if self.headless:
            opts.add_argument("--headless=new")
        os.makedirs(self.profile_dir, exist_ok=True)

        # driver 解析顺序：显式 --driver > drivers/ 目录内置 > Selenium Manager
        local = self.driver_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "drivers",
            "chromedriver.exe" if self.browser == "chrome" else "msedgedriver.exe",
        )
        svc = None
        if local and os.path.exists(local):
            print(f"[驱动] 使用本地 driver: {local}")
            svc = ChromeService(local) if self.browser == "chrome" else EdgeService(local)

        try:
            if self.browser == "chrome":
                self.driver = webdriver.Chrome(options=opts, service=svc) if svc else webdriver.Chrome(options=opts)
            else:
                self.driver = webdriver.Edge(options=opts, service=svc) if svc else webdriver.Edge(options=opts)
        except WebDriverException as e:
            raise SystemExit(
                f"[驱动] 启动失败: {str(e)[:200]}\n"
                f"把与浏览器大版本一致的 driver 放到 {os.path.dirname(local)}/ 再试；\n"
                f"Chrome 版 driver 可从 https://registry.npmmirror.com/-/binary/chrome-for-testing/ 下载。"
            )
        self.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
        )
        self.driver.set_page_load_timeout(60)
        return self.driver

    # ---------------- login ----------------
    def inject_cookies(self) -> bool:
        if not self.cookie_file:
            return False
        cookies = load_cookies(self.cookie_file)
        self.driver.get("https://www.taobao.com")
        time.sleep(1.5)
        ok = 0
        for c in cookies:
            try:
                self.driver.add_cookie(c)
                ok += 1
            except WebDriverException:
                pass
        print(f"[cookie] 注入 {ok}/{len(cookies)} 条")
        self.driver.refresh()
        time.sleep(2)
        return ok > 0

    def is_logged_in(self) -> bool:
        """淘宝匿名访问也会下发 _tb_token_ / cookie2；只有 unb / tracknick 等才是真登录态。"""
        try:
            if "login." in self.driver.current_url:
                return False
            names = {c["name"] for c in self.driver.get_cookies()}
            if names & LOGIN_KEYS:
                return True
            self.driver.get("https://i.taobao.com/my_taobao.htm")
            time.sleep(2)
            return "login." not in self.driver.current_url
        except WebDriverException:
            return False

    def ensure_login(self) -> bool:
        self.driver.get("https://www.taobao.com")
        time.sleep(2)
        if self.is_logged_in():
            print("[登录] 已登录（profile 或 cookie 有效）")
            return True
        if self.cookie_file and self.inject_cookies() and self.is_logged_in():
            print("[登录] cookie 注入成功")
            return True
        if not self.manual_login:
            print("[登录] 未登录。请加 --manual-login 扫码，或用 --cookie 指定 cookie 文件。")
            return False

        self.driver.get("https://login.taobao.com")
        print(f"[登录] 请在打开的浏览器里扫码登录，最多等待 {self.login_timeout}s ...")
        deadline = time.time() + self.login_timeout
        while time.time() < deadline:
            time.sleep(3)
            if "login." not in self.driver.current_url and self.is_logged_in():
                print("[登录] 扫码成功，登录态已写入 profile 目录，后续可复用")
                return True
        print("[登录] 超时")
        return False

    # ---------------- 打开评价面板 ----------------
    def open_review_panel(self) -> None:
        xpaths = [
            "//div[contains(text(),'查看全部评价')]",
            "//span[contains(text(),'查看全部评价')]",
            "//*[contains(text(),'宝贝评价')]",
            "//*[contains(text(),'累计评价')]",
            "//div[@id='J_Reviews']",
            "//*[@class='tb-rate-count']",
        ]
        for xp in xpaths:
            try:
                el = WebDriverWait(self.driver, 6).until(EC.element_to_be_clickable((By.XPATH, xp)))
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.4)
                try:
                    el.click()
                except (ElementClickInterceptedException, StaleElementReferenceException):
                    self.driver.execute_script("arguments[0].click();", el)
                print(f"[面板] 已点击: {xp}")
                time.sleep(2.0)
                return
            except (TimeoutException, NoSuchElementException, StaleElementReferenceException):
                continue
        print("[面板] 未点到“查看全部评价”，可能已在评价页或页面结构变化（继续尝试直接抽取）")

    def product_name(self) -> str:
        sels = [
            "span.MainTitle--PiA4nmJz",
            "h1",
            "div[class*='Title'] span",
            "div[class*='title'] h1",
        ]
        for s in sels:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, s)
                t = (el.text or "").strip()
                if 2 < len(t) < 200:
                    return t
            except (NoSuchElementException, StaleElementReferenceException):
                continue
        return self.driver.title[:120]

    def scroll_panel(self) -> bool:
        js = r"""
        const cands = [];
        for (const p of ['beautify-scroll-bar','comments--','Comments--','Drawer--']) {
          document.querySelectorAll('[class*="'+p+'"]').forEach(e=>cands.push(e));
        }
        let box = null, best = 0;
        for (const e of cands) {
          const sh = e.scrollHeight, ch = e.clientHeight;
          if (sh > ch + 50 && sh > best) { best = sh; box = e; }
        }
        if (!box) box = document.documentElement;
        const before = box.scrollTop;
        box.scrollTop = box.scrollTop + box.clientHeight * 1.6;
        window.scrollBy(0, 600);
        return { scrolled: box.scrollTop !== before, top: box.scrollTop, h: box.scrollHeight };
        """
        try:
            r = self.driver.execute_script(js)
        except WebDriverException:
            return False

        # 面板到底时尝试点“下一页”
        if r and not r.get("scrolled"):
            for xp in [
                "//button[contains(text(),'下一页')]",
                "//*[contains(@class,'next-next')]",
                "//li[contains(@title,'下一页')]",
            ]:
                try:
                    btn = self.driver.find_element(By.XPATH, xp)
                    if btn.is_enabled():
                        self.driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1.2)
                        return True
                except (NoSuchElementException, WebDriverException):
                    continue
        return bool(r and r.get("scrolled"))

    def extract(self) -> list[dict[str, Any]]:
        try:
            return (self.driver.execute_script(EXTRACT_JS) or {}).get("items", [])
        except WebDriverException as e:
            print(f"[抽取] JS 执行失败: {e}")
            return []

    # ---------------- 主流程 ----------------
    def scrape(self, url: str, target: int = 2000, max_rounds: int = 800, dump_html: str = ""):
        self.driver.get(url)
        time.sleep(3)
        pname = self.product_name()
        pid = parse_product_id(url)
        print(f"[商品] {pname}  (id={pid})")

        self.open_review_panel()
        if dump_html:
            os.makedirs(dump_html, exist_ok=True)
            p = os.path.join(dump_html, f"panel_{int(time.time())}.html")
            open(p, "w", encoding="utf-8").write(self.driver.page_source)
            print(f"[调试] 面板 HTML 已保存: {p}")

        os.makedirs(os.path.dirname(self.out_csv) or ".", exist_ok=True)
        self.load_seen()
        new_file = not os.path.exists(self.out_csv)

        f = open(self.out_csv, "a", newline="", encoding="utf-8-sig")
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()

        total = len(self.seen)
        round_i = 0
        idle = 0
        try:
            while total < target and round_i < max_rounds:
                round_i += 1
                items = self.extract()
                added = 0
                for it in items:
                    row = self.to_row(it, pid, pname, url)
                    if not row:
                        continue
                    if row["review_id"] in self.seen:
                        continue
                    self.seen.add(row["review_id"])
                    w.writerow(row)
                    added += 1
                if added:
                    f.flush()
                    total += added
                    print(f"[采集] +{added} 条，累计 {total}/{target}（第 {round_i} 轮）")
                    idle = 0
                else:
                    idle += 1

                if total >= target:
                    break
                if not self.scroll_panel():
                    idle += 1
                if idle >= 12:
                    print("[采集] 连续 12 轮无新增，页面到底或触发风控，停止")
                    break
                time.sleep(random.uniform(0.35, 0.9))
        finally:
            f.close()
        print(f"[完成] 累计 {total} 条 -> {self.out_csv}")
        return total

    def to_row(self, it: dict[str, Any], pid: str, pname: str, url: str) -> dict[str, Any] | None:
        raw = (it.get("raw") or "").strip()
        content = (it.get("content") or "").strip()
        if not content or len(content) < 2:
            return None
        if content in ("更多", "展开", "收起"):
            return None

        is_default = 0
        if content in ("此用户没有填写评价。", "评价方未及时做出评价,系统默认好评!"):
            is_default = 1

        append = ""
        m = re.search(r"追评[：:]\s*(.+)$", raw, re.S)
        if m:
            append = m.group(1).strip()[:1000]

        user = (it.get("user") or "").strip()[:40]
        date = (it.get("date") or "").strip()
        sku = (it.get("sku") or "").strip()[:60]
        rating = (it.get("rating") or "").strip()

        rid = content_hash(content, user, date)
        return {
            "review_id": rid,
            "product_id": pid,
            "product_name": pname,
            "user": user,
            "sku": sku,
            "review_time": date,
            "rating": rating,
            "content": content,
            "append_content": append,
            "is_default": is_default,
            "raw_text": raw.replace("\n", " | ")[:1500],
            "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_url": url,
        }

    def load_seen(self):
        if not os.path.exists(self.out_csv):
            return
        try:
            with open(self.out_csv, "r", encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("review_id"):
                        self.seen.add(r["review_id"])
            print(f"[续采] 已有 {len(self.seen)} 条历史记录，将跳过重复")
        except Exception as e:
            print(f"[续采] 读取历史失败: {e}")

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            print("[系统] 浏览器已关闭")


def main(argv=None):
    ap = argparse.ArgumentParser(description="淘宝商品评价结构化采集")
    ap.add_argument("--url", required=True, help="商品详情页链接")
    ap.add_argument("--target", type=int, default=2000, help="目标条数，默认 2000")
    ap.add_argument("--out", default="data/reviews.csv", help="输出 CSV 路径")
    ap.add_argument("--cookie", default="", help="cookie 文件：json / cookies.txt")
    ap.add_argument("--profile", default="", help="浏览器 profile 目录（持久化登录态）")
    ap.add_argument("--manual-login", action="store_true", help="允许首次扫码登录")
    ap.add_argument("--login-timeout", type=int, default=300)
    ap.add_argument("--headless", action="store_true", help="无头模式（淘宝风控严，不建议）")
    ap.add_argument("--browser", default="chrome", choices=["chrome", "edge"], help="浏览器")
    ap.add_argument("--driver", default="", help="driver 绝对路径，留空则用 drivers/ 内置或 Selenium Manager")
    ap.add_argument("--max-rounds", type=int, default=800)
    ap.add_argument("--dump-html", default="", help="保存评价面板 HTML 用于调试选择器")
    args = ap.parse_args(argv)

    s = TaobaoReviewScraper(
        out_csv=args.out,
        profile_dir=args.profile,
        cookie_file=args.cookie,
        headless=args.headless,
        manual_login=args.manual_login,
        login_timeout=args.login_timeout,
        browser=args.browser,
        driver_path=args.driver,
    )
    try:
        s.build_driver()
        if not s.ensure_login():
            print("[中止] 未获得登录态。淘宝评价接口需要登录 cookie，请按 README 导出 cookie 后重试。")
            return 2
        s.scrape(args.url, target=args.target, max_rounds=args.max_rounds, dump_html=args.dump_html)
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
