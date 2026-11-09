#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
淘宝/天猫评价采集（mtop 接口版）—— 稳定拿 2000+ 条结构化数据

背景：PC 详情页的"评价模块"被 x5sec 风控（页面上那行"访问异常提示"就是它），
DOM 抓取拿不到评论。但评价接口本身可用：
    mtop.taobao.rate.detaillist.get  v6.0
只要带上登录 cookie + 正确 sign（md5(token&t&appKey&data)）就能翻页拿全量。

    python api_crawl.py --item 946520427043 --target 2000
    python api_crawl.py --item 946520427043 --cookie cookies.json --target 2000
    python api_crawl.py --item xxx --inspect          # 打印一条原始记录，便于对字段

产出 data/reviews_csv 之外还有 data/reviews_raw.jsonl（原始记录，便于二次分析）。
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
import urllib.parse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
APPKEY = "12574478"
API = "mtop.taobao.rate.detaillist.get"
VERSION = "6.0"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

FIELDS = [
    "rate_id", "item_id", "product_title", "user_nick", "rate_type", "rate_type_cn",
    "user_star", "feedback_date", "create_time", "sku", "sku_id",
    "content", "append_content", "reply", "pic_count", "gold_user",
    "credit_level", "crawl_time", "source",
]

RATE_CN = {"1": "好评", "0": "中评", "-1": "差评"}


# --------------------------------------------------------------- cookies
def norm_item(s: str) -> str:
    """接受纯数字 ID，也接受商品链接（item.taobao.com / detail.tmall.com 等）。"""
    s = (s or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return s
    m = re.search(r"[?&](?:id|itemId|item_id)=(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"(\d{6,})", s)
    return m.group(1) if m else ""


def cookies_from_browser(port: int) -> dict:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    o = webdriver.ChromeOptions()
    o.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    drv_path = os.path.join(HERE, "drivers", "chromedriver.exe")
    d = webdriver.Chrome(options=o, service=Service(drv_path)) if os.path.exists(drv_path) \
        else webdriver.Chrome(options=o)
    d.get("https://www.taobao.com")
    time.sleep(1.5)
    return {c["name"]: c["value"] for c in d.get_cookies()}


def cookies_from_file(path: str) -> dict:
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cookies", [])
    return {c["name"]: c["value"] for c in data if c.get("name")}


# --------------------------------------------------------------- collector
class RateCollector:
    def __init__(self, cookies: dict, out_csv: str, resume: bool = True, delay: float = 4.5):
        self.ck = dict(cookies)
        self.delay = delay
        self.out_csv = out_csv
        self.raw_path = os.path.splitext(out_csv)[0] + "_raw.jsonl"
        self.seen: set[str] = set()
        self.written = 0
        self.resume = resume
        if resume:
            self._load_seen()

    def _load_seen(self):
        if os.path.exists(self.out_csv):
            try:
                with open(self.out_csv, encoding="utf-8-sig", newline="") as f:
                    for r in csv.DictReader(f):
                        if r.get("rate_id"):
                            self.seen.add(r["rate_id"])
                print(f"[续采] 已有 {len(self.seen)} 条")
            except Exception:
                pass

    def _cookie_str(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.ck.items())

    def _absorb_cookies(self, r: requests.Response):
        try:
            raw = r.raw.headers
            items = raw.getlist("Set-Cookie") if hasattr(raw, "getlist") else []
        except Exception:
            items = []
        for h in items:
            m = re.match(r"\s*(_m_h5_tk|_m_h5_tk_enc|_tb_token_|cookie2)=([^;]+)", h)
            if m:
                self.ck[m.group(1)] = m.group(2)

    def fetch(self, item: str, page: int, size: int = 20, rate_type: str = "") -> dict:
        token = (self.ck.get("_m_h5_tk") or "").split("_")[0]
        data = {"auctionNumId": item, "itemId": item, "pageNo": page, "pageSize": size,
                "rateType": rate_type, "orderType": "", "filter": ""}
        ds = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        t = str(int(time.time() * 1000))
        sign = hashlib.md5(f"{token}&{t}&{APPKEY}&{ds}".encode()).hexdigest()
        q = {"jsv": "2.7.5", "appKey": APPKEY, "t": t, "sign": sign, "api": API, "v": VERSION,
             "type": "originaljson", "dataType": "json", "timeout": "20000", "data": ds, "ecode": "1"}
        url = f"https://h5api.m.tmall.com/h5/{API}/{VERSION}/?" + urllib.parse.urlencode(q)
        r = requests.get(url, headers={"Cookie": self._cookie_str(), "User-Agent": UA,
                                       "Referer": "https://detail.tmall.com/"}, timeout=25)
        self._absorb_cookies(r)
        try:
            return r.json()
        except Exception:
            return {"ret": ["FAIL_PARSE"], "raw": r.text[:300]}

    @staticmethod
    def _append_text(rec: dict) -> str:
        af = rec.get("appendedFeed")
        if not af:
            return ""
        try:
            d = af if isinstance(af, dict) else json.loads(af)
        except Exception:
            return ""
        for k in ("appendedFeedback", "appendContent", "feedback", "content"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def to_row(self, rec: dict, item: str) -> dict:
        pics = rec.get("feedPicPathList") or "[]"
        try:
            pic_count = len(pics if isinstance(pics, list) else json.loads(pics))
        except Exception:
            pic_count = 0
        rt = str(rec.get("rateType") or "")
        return {
            "rate_id": str(rec.get("id") or ""),
            "item_id": item,
            "product_title": rec.get("auctionTitle") or "",
            "user_nick": rec.get("reduceUserNick") or rec.get("userNick") or "",
            "rate_type": rt,
            "rate_type_cn": RATE_CN.get(rt, "未知"),
            "user_star": str(rec.get("userStar") or ""),
            "feedback_date": rec.get("feedbackDate") or "",
            "create_time": rec.get("createTime") or "",
            "sku": rec.get("skuValueStr") or "",
            "sku_id": str(rec.get("skuId") or ""),
            "content": (rec.get("feedback") or "").strip(),
            "append_content": self._append_text(rec),
            "reply": (rec.get("reply") or "").strip(),
            "pic_count": pic_count,
            "gold_user": str(rec.get("goldUser") or ""),
            "credit_level": str(rec.get("creditLevel") or ""),
            "crawl_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": f"{API}/v{VERSION}",
        }

    def run(self, items: list[str], target: int = 2000, page_size: int = 20,
            start_page: int = 1, inspect: bool = False, rate_type: str = "",
            max_punish: int = 5) -> int:
        os.makedirs(os.path.dirname(self.out_csv) or ".", exist_ok=True)
        new_file = not os.path.exists(self.out_csv)
        fcsv = open(self.out_csv, "a", newline="", encoding="utf-8-sig")
        fraw = open(self.raw_path, "a", encoding="utf-8")
        w = csv.DictWriter(fcsv, fieldnames=FIELDS)
        if new_file:
            w.writeheader()

        total_written = len(self.seen)
        try:
            for item in items:
                if total_written >= target:
                    break
                page = start_page
                punish_streak = 0
                print(f"\n===== item {item} 起始第 {page} 页 =====")
                while total_written < target:
                    try:
                        j = self.fetch(item, page, page_size, rate_type)
                    except Exception as e:
                        print(f"[网络] 第 {page} 页异常: {str(e)[:120]}")
                        time.sleep(5)
                        punish_streak += 1
                        if punish_streak > 6:
                            break
                        continue

                    ret = " ".join(j.get("ret") or [])
                    if "SUCCESS" not in ret:
                        if "USER_VALIDATE" in ret or "RGV587" in ret:
                            wait = random.randint(45, 75) + punish_streak * 15
                            print(f"[风控] 第 {page} 页触发验证，等待 {wait}s 后重试（第 {punish_streak+1} 次）")
                            time.sleep(wait)
                            punish_streak += 1
                            if punish_streak > max_punish:
                                print("[风控] 连续触发，停止本商品")
                                break
                            continue
                        print(f"[接口] 第 {page} 页返回: {ret[:120]}")
                        break

                    punish_streak = 0
                    data = j.get("data") or {}
                    rates = data.get("rateList") or []
                    if inspect:
                        print(json.dumps(rates[0] if rates else {}, ensure_ascii=False, indent=1)[:2500])
                        return total_written
                    if not rates:
                        print("[结束] 该页没有数据")
                        break

                    added = 0
                    for rec in rates:
                        row = self.to_row(rec, item)
                        if not row["rate_id"] or row["rate_id"] in self.seen:
                            continue
                        self.seen.add(row["rate_id"])
                        w.writerow(row)
                        fraw.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        added += 1
                    fcsv.flush()
                    fraw.flush()
                    total_written += added

                    tp = data.get("totalPage")
                    tot = data.get("total")
                    print(f"[{page}/{tp}] +{added} 累计 {total_written}/{target}（评论总数 {tot}）")

                    if not data.get("hasNext", False) or (tp and page >= int(tp)):
                        print("[结束] 已到最后一页")
                        break
                    page += 1
                    time.sleep(random.uniform(self.delay * 0.75, self.delay * 1.35))
        finally:
            fcsv.close()
            fraw.close()
        return total_written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--item", required=True, help="商品 id，多个用逗号分隔")
    ap.add_argument("--target", type=int, default=2000)
    ap.add_argument("--out", default=os.path.join(HERE, "data", "reviews.csv"))
    ap.add_argument("--cookie", default="", help="cookies.json 路径；留空则从调试端口浏览器读")
    ap.add_argument("--port", type=int, default=9334)
    ap.add_argument("--page-size", type=int, default=50)
    ap.add_argument("--start-page", type=int, default=1)
    ap.add_argument("--delay", type=float, default=4.5, help="每页间隔秒数（越小越容易被风控）")
    ap.add_argument("--rate-type", default="", help="留空=全部（接口本身不按类型过滤）")
    ap.add_argument("--max-punish", type=int, default=5, help="连续被风控多少次后放弃（配合 --delay 可长时间等冷却）")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if args.cookie:
        ck = cookies_from_file(args.cookie)
        print(f"[cookie] 文件 {args.cookie}: {len(ck)} 条")
    else:
        ck = cookies_from_browser(args.port)
        print(f"[cookie] 浏览器端口 {args.port}: {len(ck)} 条")
    if not ({"unb", "tracknick", "_nk_"} & set(ck)):
        print("[警告] cookie 里没有登录标识（unb/tracknick），接口会返回未登录错误")

    items = [norm_item(x) for x in args.item.split(",") if x.strip()]
    items = [x for x in items if x]
    if not items:
        print("[错误] --item 里没解析出商品 ID")
        return 2
    print(f"[目标] {len(items)} 个商品: {', '.join(items)}")
    c = RateCollector(ck, args.out, resume=not args.no_resume, delay=args.delay)
    n = c.run(items, target=args.target, page_size=args.page_size,
              start_page=args.start_page, inspect=args.inspect, rate_type=args.rate_type,
              max_punish=args.max_punish)
    print(f"\n[完成] 共 {n} 条 -> {args.out}")
    print(f"[原始] {c.raw_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

