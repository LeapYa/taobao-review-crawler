# taobao-review-crawler

淘宝 / 天猫商品评价采集工具。直连评价接口 `mtop.taobao.rate.detaillist.get`，
一次可采满单商品全量评价（实测 5000 条），输出 32 字段结构化数据集。

只做采集，不做分析——拿到干净 CSV 之后的分析由使用者自己接。

## 为什么不用 Selenium 抓页面

2026 年淘宝/天猫 PC 详情页的「评价模块」已被 x5sec 单独保护：页面只显示
"访问异常提示 / 系统检测到当前访问存在异常…"，DOM 里根本没有评论区。
`docs/legacy-selenium.md` 记录了原 Selenium 方案及其失效原因，`legacy/` 保留原脚本作对照。

| 方案 | 结果 |
|---|---|
| Selenium 驱动 Chrome 抓 DOM | 评价模块被替换，抽取 0 条 |
| 普通 Chrome + CDP 附着（无 webdriver 特征） | 商品页正常，评价模块仍被替换 |
| 全新设备 profile + 注入登录 cookie | 首屏正常，API 仍返回 RGV587 |
| **mtop 评价接口 + 签名直连** | ✅ 稳定，`total=5000, totalPage=100` |

结论：拦住 DOM 的是业务层风控（评价模块单独加 x5sec）+ 账号/请求频率配额，
不是浏览器指纹。所以本项目不再和浏览器指纹较劲，直接走接口。

## 特性

- **接口签名**：`sign = md5(_m_h5_tk[:32] & t & 12574478 & data)`，token 自动从响应刷新
- **风控退避**：命中 `RGV587` 自动等待 45~130s 重试，`--max-punish` 控制耐心
- **断点续采**：按 `rate_id` 去重，中断后 `--start-page` 续跑
- **一次拉全**：默认列表已含好评/中评/差评，无需按类型分开采集（见「数据口径」）
- **32 字段输出**：规格拆分、追评、图文视频、点赞阅读、买家等级
- **原始留存**：`*_raw.jsonl` 保留接口全字段，改解析不用重爬

## 安装

```bash
pip install -r requirements.txt
```

Chrome 驱动不需要手动装：优先用 `drivers/` 目录内的 driver，其次交给 Selenium Manager。
若内网拉不到驱动，可从 `https://registry.npmmirror.com/-/binary/chrome-for-testing/` 下载对应版本，
放到 `drivers/chromedriver.exe`。

## 快速开始

### 1. 登录一次

```bash
python grab_cookie.py
```

弹出 Chrome，用淘宝 App 扫码。成功后登录态写入 `.taobao_profile/` 并导出 `cookies.json`。

### 2. 采集

```bash
# 采满全量（pageSize=50，约 100 页）
python api_crawl.py --item 946520427043 --target 5000 --port 9334

# 已有 cookie 时不需要开浏览器
python api_crawl.py --item 946520427043 --target 5000 --cookie cookies.json

# 差评 / 中评不需要单独拉，它们就在默认列表里（rateType 1/0/-1）
# 若要单独导出，采集后按 rate_type 过滤即可：
#   df[df.rate_type.isin(["0", "-1"])].to_csv("reviews_bad.csv")
```

`--port 9334` 指"带调试端口的普通 Chrome"，先这样启动（比 webdriver 启动更难被识别）：

```bash
chrome.exe --remote-debugging-port=9334 --user-data-dir=.taobao_profile about:blank
```

### 3. 重建数据集（可选）

```bash
python build_dataset.py --raw data/reviews_raw.jsonl --out data/reviews_final.csv
```

`api_crawl.py` 直接产出的那份 CSV 是扁平字段；`build_dataset.py` 会把
`skuMap` / `appendedFeed` / `interactInfo` / `video` 这些嵌套字段展开成 32 列。
字段解析逻辑改了重跑这一步即可，不必重新采集。

## 参数

| 参数 | 说明 |
|---|---|
| `--item` | 商品 ID，多个用逗号分隔 |
| `--target` | 目标条数 |
| `--out` | 输出 CSV |
| `--rate-type` | 一般留空。该接口不按类型过滤，传值不改变结果集（见「数据口径」） |
| `--page-size` | 默认 50（上限 50，100 会报 illegalArgument） |
| `--start-page` | 断点续采起始页 |
| `--delay` | 每页间隔秒数，默认 4.5，风控紧就调大 |
| `--max-punish` | 连续被风控多少次后放弃，长时间等冷却可调大 |
| `--port` | 调试端口，从浏览器读 cookie |
| `--cookie` | cookie 文件，优先于 `--port` |
| `--inspect` | 只打印一条原始记录，用于对字段 |

## 输出字段（32 个）

| 分组 | 字段 |
|---|---|
| 标识 | `review_id` `item_id` `product_name` `sku_id` |
| 用户 | `user`(掩码) `user_grade` `credit_level` `user_star` `gold_user` |
| 评价 | `rate_type` `rate_type_cn` `rating` `review_time` `time_raw` `content` |
| 追评 | `append_content` `append_time` |
| 规格 | `sku_color` `sku_combo` `sku` |
| 媒体 | `pic_count` `has_media` `pic_urls` `has_video` `video_url` |
| 互动 | `read_count` `like_count` `comment_count` `tags` |
| 商家 | `reply` |
| 元数据 | `crawl_time` `source` |

## 数据口径（实测结论，别再重复踩）

### 1. 折叠评价不是独立数据源，不需要单独爬

接口返回的 `foldCount=2509` 容易被误解成"还有 2509 条折叠评价没采到"。实际不是：

| 请求 | total | 说明 |
|---|---|---|
| 默认（不带 foldFlag） | 5000 | 主列表 |
| `foldFlag=1` | 2000 | 折叠**筛选视图**，接口截断，非全量 |

用 `foldFlag=1` 抓了 600 条与主表做 `rate_id` 比对：**交集 600/600，100% 命中主表**。
也就是说折叠评价本来就混在默认列表里，`foldFlag=1` 只是换个排序把带"折叠"标记的挑出来，
并不存在一批遗漏数据。

折叠桶的特征（600 条样本）：正文重复率 **93.0%**、好评占比 98.2%、平均 17.3 字，
典型内容形如 `'夯'`、`'。'`、`'该用户觉得商品非常好，给出5星好评'`。
可见淘宝折叠的是**低质量内容**，不是隐藏差评——104 条差评全部出现在主列表里。

结论：只采默认列表即可，不要加 foldFlag，也不要把它当成"补数据"的途径。

### 2. 差评中评就在默认列表里，不需要按类型分别拉

`rateType` 的取值是 `1=好评 / 0=中评 / -1=差评`（不是 1/2/3）。
默认列表已包含全部三种，实测 5000 条：好评 4837、中评 59、差评 104，好评率 96.74%，
与店铺展示的 97% 一致。

传 `rateType=0/1/2/3` 或 `filter=0/3/5` 都不会产生过滤效果（`0` 直接返回空，
`3` 返回的还是好评子集），该接口没有按评价类型分页的能力。

### 3. 5000 是展示上限，不是真实评价总数

`fuzzyRateCount` 显示 `5000+`、`range=5000`，说明页面只暴露前 5000 条。
`total` 会随筛选条件在 5000 / 2000 之间跳，以 `rateList` 实际返回为准。
想要更多样本只能换商品链接，不是调分页。

### 4. 分页无深度上限

`pageSize=50` 时 `totalPage=100`，`pageSize=20` 时 `totalPage=250`，
末尾页 `hasNext=false`。中途的 `RGV587` 是限流，不是触顶，等冷却后同一页能继续。

## 数据质量（2600 条样本实测）

| 字段 | 覆盖率 | 说明 |
|---|---|---|
| 正文 / 日期 / 规格 / 评价类型 | 100% | 可直接用 |
| 追评 `append_content` | 48.3% | 含追评日期 |
| 图片 `has_media` | 69.7% | `pic_urls` 存前 3 张，原始 JSONL 有全量 |
| 视频 `has_video` | 35.7% | 含 `cloudVideoUrl` |
| `read_count` 非零 | 74.3% | 可当曝光度代理 |
| `like_count` 非零 | 8.3% | 点赞稀疏，别当权重用 |
| `user_star` 有效 | 11.6% | 多数为 0，不可用 |
| `credit_level` | 100% | 但 87% 是 -1（未评级） |
| `tags` / `reply` | 0% | 接口返回全空 |

### 用之前必须知道的两件事

1. **近一半是系统模板文本**。5000 条里 2308 条（46.2%）是
   `该用户觉得商品非常好，给出5星好评`（2169 条）、`该用户未填写评价内容`（139 条）之类的占位内容，
   去掉后有效正文只剩 2692 条。不做清洗直接跑词频/情感，结果会被这些同文本拉偏。
2. **`user` 不能当用户 ID**。`userId` 恒为 0；昵称掩码规则是"首字符 + `**` + 末位"，
   5000 条只有 1778 个不同昵称，`t**0`~`t**9` 这 10 个字符串就覆盖了 54.6% 的记录。
   唯一键只能用 `review_id`，任何用户级分析（复购、分层、去重）都做不了。

## 风控与合规注意

- **配额**：约 3 页 / 60 秒窗口。3000 条以上建议 `--delay 15` 以上，跑 30~40 分钟。
- **冷却**：命中 `RGV587` 只能等，硬刚会把账号风险分拉满，导致滑块验证或短时限制下单。
- **账号**：不要用主力购物账号跑。
- **数据**：采集结果含第三方用户评价内容，仅本地研究使用，不要公开分发；
  `cookies.json` 等价于账号凭证，用完请删除。

## 局限

接口本身不提供、任何采集方式都拿不到的字段：商家回复（`reply` 恒空）、
用户唯一 ID（`userId` 恒 0，昵称已掩码）、平台结构标签（`rateTagList` 空）、
交易信息（价格/购买时间/是否退货）、分项评分（物流/服务）。

## 目录结构

```
api_crawl.py            接口版采集（主力）
build_dataset.py        原始 JSONL -> 32 字段数据集
grab_cookie.py          扫码登录 + 导出 cookie
batch_crawl.py          多商品合并采集
attached_crawl.py       CDP 附着真浏览器的 DOM 采集
taobao_reviews_pro.py   DOM 采集核心（选择器兜底/断点续采）
test_extract.py         本地 fixture 离线验证抽取逻辑
docs/legacy-selenium.md 原 Selenium 方案文档（历史参考）
legacy/                 原 Selenium 脚本（历史参考）
```

## 致谢

`legacy/taobao_original.py` 与 `docs/legacy-selenium.md` 来自
[olddove-laoge/Crawling-Taobao-product-reviews](https://github.com/olddove-laoge/Crawling-Taobao-product-reviews)，
版权归原作者，本项目仅作历史对照保留；其余代码为独立实现。
