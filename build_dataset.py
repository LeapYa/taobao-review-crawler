#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把 api_crawl.py 的原始记录（data/reviews_raw.jsonl）重建成干净的分析用数据集。
好处：字段解析可以随时改，不用重新爬。

    python build_dataset.py                                  # raw -> data/reviews_final.csv
    python build_dataset.py --raw data/reviews_raw.jsonl --out data/reviews_final.csv
"""
import argparse
import json
import os
import re
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RATE_CN = {"1": "好评", "2": "中评", "3": "差评", "0": "其他"}


def date_norm(s: str) -> str:
    s = str(s or "").strip()
    if not s:
        return ""
    m = re.search(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"^(\d{1,2})\s*月\s*(\d{1,2})\s*日?$", s)
    if m:
        return f"{time.localtime().tm_year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.search(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    return ""


def jload(v, default):
    if not v:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=os.path.join(HERE, "data", "reviews_raw.jsonl"))
    ap.add_argument("--out", default=os.path.join(HERE, "data", "reviews_final.csv"))
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    rows, seen = [], set()
    with open(args.raw, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            rid = str(r.get("id") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)

            sku_map = jload(r.get("skuMap"), {})
            if not isinstance(sku_map, dict):
                sku_map = {}
            af = jload(r.get("appendedFeed"), {})
            if not isinstance(af, dict):
                af = {}
            pics = jload(r.get("feedPicPathList"), [])
            inter = jload(r.get("interactInfo"), {})
            if not isinstance(inter, dict):
                inter = {}
            vid = jload(r.get("video"), {})
            if not isinstance(vid, dict):
                vid = {}
            extra = jload(r.get("extraInfoMap"), {})
            if not isinstance(extra, dict):
                extra = {}
            tags = jload(r.get("rateTagList"), [])
            if isinstance(tags, list):
                tags = "；".join(str(t.get("name") if isinstance(t, dict) else t) for t in tags)
            else:
                tags = ""
            rt = str(r.get("rateType") or "")

            rows.append({
                "review_id": rid,
                "item_id": str(r.get("auctionNumId") or ""),
                "product_name": r.get("auctionTitle") or "",
                "user": r.get("reduceUserNick") or r.get("userNick") or "",
                "rate_type": rt,
                "rate_type_cn": RATE_CN.get(rt, "未知"),
                "rating": {"1": 5, "2": 3, "3": 1}.get(rt, ""),
                "review_time": date_norm(r.get("feedbackDate")) or date_norm(r.get("createTime")),
                "time_raw": r.get("feedbackDate") or r.get("createTime") or "",
                "sku_color": sku_map.get("颜色分类", ""),
                "sku_combo": sku_map.get("套餐类型", ""),
                "sku": r.get("skuValueStr") or "",
                "sku_id": str(r.get("skuId") or ""),
                "content": (r.get("feedback") or "").strip(),
                "append_content": (af.get("appendedFeedback") or "").strip(),
                "append_time": date_norm(af.get("createTime")),
                "reply": (r.get("reply") or "").strip(),
                "pic_count": len(pics) if isinstance(pics, list) else 0,
                "has_media": 1 if (isinstance(pics, list) and pics) else 0,
                "pic_urls": ";".join(pics[:3]) if isinstance(pics, list) else "",
                "has_video": 1 if vid.get("cloudVideoUrl") else 0,
                "video_url": vid.get("cloudVideoUrl") or "",
                "like_count": inter.get("likeCount") or 0,
                "read_count": inter.get("readCount") or 0,
                "comment_count": inter.get("commentCount") or 0,
                "tags": tags,
                "user_star": str(r.get("userStar") or ""),
                "user_grade": str(extra.get("userGrade") or ""),
                "gold_user": 1 if str(r.get("goldUser")).lower() == "true" else 0,
                "credit_level": str(r.get("creditLevel") or ""),
                "crawl_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": "mtop.taobao.rate.detaillist.get/6.0",
            })

    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"[完成] {len(df)} 条 -> {args.out}")
    if len(df):
        print("好评/中评/差评:", df["rate_type_cn"].value_counts().to_dict())
        print("有追评:", int((df["append_content"] != "").sum()), "| 有图/视频:", int(df["has_media"].sum()))
        print("时间范围:", df["review_time"].min(), "~", df["review_time"].max())
        print("颜色分类 Top:", df["sku_color"].value_counts().head(5).to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
