#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
批量采集：多个商品链接写进同一个 CSV，直到凑够目标条数。
单商品评价分页有限，用多个同款链接合并是拿到 2000 条的常规做法。

    python batch_crawl.py --urls 946520427043,917814363641 --target 2000
    python batch_crawl.py --urls-file urls.txt --target 2000 --out data/reviews.csv
"""
import argparse
import sys
import time

from taobao_reviews_pro import TaobaoReviewScraper


def norm(u: str) -> str:
    u = u.strip()
    if not u:
        return ""
    if u.isdigit():
        return f"https://item.taobao.com/item.htm?id={u}"
    return u


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", default="", help="逗号分隔的链接或商品 id")
    ap.add_argument("--urls-file", default="", help="每行一个链接的文件")
    ap.add_argument("--target", type=int, default=2000)
    ap.add_argument("--out", default="data/reviews.csv")
    ap.add_argument("--browser", default="chrome", choices=["chrome", "edge"])
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    urls = [norm(x) for x in args.urls.split(",") if x.strip()]
    if args.urls_file:
        with open(args.urls_file, "r", encoding="utf-8") as f:
            urls += [norm(x) for x in f if x.strip()]
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls:
        print("[错误] 没有可用链接")
        return 2
    print(f"[计划] {len(urls)} 个商品链接，目标 {args.target} 条 -> {args.out}")

    s = TaobaoReviewScraper(out_csv=args.out, browser=args.browser, headless=args.headless)
    try:
        s.build_driver()
        if not s.ensure_login():
            print("[中止] 登录态无效")
            return 2
        s.load_seen()
        for i, url in enumerate(urls, 1):
            if len(s.seen) >= args.target:
                break
            print(f"\n===== [{i}/{len(urls)}] {url} =====")
            try:
                s.scrape(url, target=args.target, max_rounds=400)
            except Exception as e:
                print(f"[跳过] 该商品采集失败: {str(e)[:160]}")
            time.sleep(3)
        print(f"\n[总结] 共 {len(s.seen)} 条 -> {args.out}")
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
