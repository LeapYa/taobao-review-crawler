#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一次性登录助手：打开浏览器让你扫码登录淘宝，登录成功自动把 cookie 写成 cookies.json。
之后 taobao_reviews_pro.py 就能复用登录态，不必每次扫码。

    python grab_cookie.py                 # 扫码登录并导出 cookies.json
    python grab_cookie.py --check         # 只检查现有登录态是否还有效

说明：cookie 只写入本机 cookies.json，用完后建议删除。
"""
import argparse
import json
import os
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, ".taobao_profile")
DRIVER = os.path.join(HERE, "drivers", "chromedriver.exe")


def build(headless=False):
    o = webdriver.ChromeOptions()
    o.add_argument(f"--user-data-dir={PROFILE}")
    o.add_argument("--disable-blink-features=AutomationControlled")
    o.add_argument("--window-size=1440,980")
    o.add_argument("--lang=zh-CN")
    o.add_experimental_option("excludeSwitches", ["enable-automation"])
    o.add_experimental_option("useAutomationExtension", False)
    if headless:
        o.add_argument("--headless=new")
    svc = Service(DRIVER) if os.path.exists(DRIVER) else None
    d = webdriver.Chrome(options=o, service=svc) if svc else webdriver.Chrome(options=o)
    d.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
    )
    return d


LOGIN_KEYS = {"unb", "tracknick", "_nk_", "cookie17", "_l_g_", "lgc"}


def logged_in(d) -> bool:
    """淘宝匿名访问也会下发 _tb_token_ / cookie2，只有 unb/tracknick 这类才是真登录态。"""
    try:
        if "login." in d.current_url:
            return False
        names = {c["name"] for c in d.get_cookies()}
        if names & LOGIN_KEYS:
            return True
        # 兜底：访问需要登录的页面，看是否被踢回登录页
        d.get("https://i.taobao.com/my_taobao.htm")
        time.sleep(2)
        return "login." not in d.current_url
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查登录态")
    ap.add_argument("--out", default=os.path.join(HERE, "cookies.json"))
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    d = build(headless=args.check)
    try:
        d.get("https://www.taobao.com")
        time.sleep(3)
        if args.check:
            ok = logged_in(d)
            print("[检查] 登录态：" + ("有效 ✅" if ok else "已失效 ❌"))
            return 0 if ok else 1

        if logged_in(d):
            print("[登录] profile 里已有有效登录态，直接导出 cookie")
        else:
            d.get("https://login.taobao.com")
            print(f"[登录] 请在弹出的浏览器里用淘宝 App 扫码登录（最多等 {args.timeout}s）")
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                time.sleep(3)
                if "login." not in d.current_url and logged_in(d):
                    print("[登录] 成功")
                    break
            else:
                print("[登录] 超时，未获取到登录态")
                return 2

        cookies = d.get_cookies()
        keep = [c for c in cookies if "taobao" in c.get("domain", "") or "tmall" in c.get("domain", "") or "alicdn" in c.get("domain", "")]
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(keep, f, ensure_ascii=False, indent=1)
        print(f"[导出] {len(keep)} 条 cookie -> {args.out}")
        print("[提示] 采集结束后可删除 cookies.json；profile 目录 .taobao_profile 也保存了登录态")
        return 0
    finally:
        d.quit()


if __name__ == "__main__":
    sys.exit(main())
