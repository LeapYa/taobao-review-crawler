#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
反检测版采集：连"正常启动"的 Chrome，而不是 Selenium 启动的 Chrome。

原理：淘宝对 webdriver 启动的实例有特征检测（navigator.webdriver / CDP 注入 / 启动参数），
直接返回"访问异常"。改为
    1) 用普通命令行参数启动 Chrome（带 --remote-debugging-port，但不带任何 automation 参数）
    2) Selenium 通过 debuggerAddress 附着上去
这样 Chrome 侧看到的就是一个普通浏览器，并且复用 .taobao_profile 里已有的登录态。

    python attached_crawl.py --url "https://item.taobao.com/item.htm?id=946520427043" --target 2000
    python attached_crawl.py --url ... --target 30 --keep-open    # 只做冒烟，不关窗口
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.request

from selenium import webdriver

from taobao_reviews_pro import TaobaoReviewScraper

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, ".taobao_profile")
DRIVER = os.path.join(HERE, "drivers", "chromedriver.exe")
PORT = 9333

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("找不到 chrome.exe")


def launch_chrome(url: str = "about:blank") -> subprocess.Popen:
    chrome = find_chrome()
    args = [
        chrome,
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1500,1000",
        "--lang=zh-CN",
        url,
    ]
    print(f"[启动] 普通模式 Chrome（非 webdriver）: {' '.join(args[:3])} ...")
    return subprocess.Popen(args)


def wait_port(timeout=30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2).read()
            return True
        except Exception:
            time.sleep(0.6)
    return False


def attach() -> webdriver.Chrome:
    o = webdriver.ChromeOptions()
    o.add_experimental_option("debuggerAddress", f"127.0.0.1:{PORT}")
    from selenium.webdriver.chrome.service import Service

    svc = Service(DRIVER) if os.path.exists(DRIVER) else None
    d = webdriver.Chrome(options=o, service=svc) if svc else webdriver.Chrome(options=o)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--target", type=int, default=2000)
    ap.add_argument("--out", default="data/reviews.csv")
    ap.add_argument("--max-rounds", type=int, default=600)
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--reuse", action="store_true", help="已经开着带调试端口的 Chrome 时直接附着")
    ap.add_argument("--dump-html", default="")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    proc = None
    if not args.reuse:
        proc = launch_chrome("about:blank")
        if not wait_port():
            raise SystemExit("[错误] 调试端口没起来，可能有别的 Chrome 占用了 profile")
        time.sleep(1.5)

    d = attach()
    s = TaobaoReviewScraper(out_csv=args.out)
    s.driver = d
    try:
        d.get("https://www.taobao.com")
        time.sleep(2)
        print("[登录] 有效" if s.is_logged_in() else "[登录] 无效（profile 掉登录了？）")
        n = s.scrape(args.url, target=args.target, max_rounds=args.max_rounds, dump_html=args.dump_html)
        print(f"[完成] {n} 条")
        return 0
    finally:
        if not args.keep_open:
            try:
                d.quit()
            except Exception:
                pass
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
