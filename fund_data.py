"""
Fund-Radar 数据层
=================
AKShare 接口调用 + 基金筛选 + 持仓/细行业聚合。
供 generate_data.py（GitHub Actions / 本地）生成静态 JSON，
与前端 index.html 完全解耦。
"""

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

import akshare as ak


# ============================================================
# 基金排行获取与筛选
# ============================================================

def fetch_fund_ranking() -> pd.DataFrame:
    """联网获取开放式基金排行数据。"""
    print("[数据抓取] 正在获取开放式基金排行...")
    df = ak.fund_open_fund_rank_em(symbol="全部")
    print(f"[数据抓取] 成功获取 {len(df)} 条基金数据")
    return df


def screen_funds(df: pd.DataFrame, y1_min, m6_min, m3_min, m1_min) -> pd.DataFrame:
    """四维度交集筛选。"""
    col_map = {"近1年": "y1", "近6月": "m6", "近3月": "m3", "近1月": "m1"}
    rename_map = {}
    for cn_name, en_name in col_map.items():
        for col in df.columns:
            if cn_name in col:
                rename_map[col] = en_name
                break
    df = df.rename(columns=rename_map)

    for col in ["y1", "m6", "m3", "m1"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    mask = (df["y1"] >= y1_min) & (df["m6"] >= m6_min) & (df["m3"] >= m3_min) & (df["m1"] >= m1_min)
    return df[mask].copy()


# ============================================================
# 单只基金持仓数据
# ============================================================

def get_fund_code(row: pd.Series) -> str:
    for col in ["基金代码", "基金编号"]:
        if col in row.index:
            return str(row[col])
    return ""


def get_fund_name(row: pd.Series) -> str:
    for col in ["基金简称", "基金名称"]:
        if col in row.index:
            return str(row[col])
    return ""


def fetch_fund_holdings(fund_code: str, year: str = "2026") -> tuple:
    """获取单只基金的前十大重仓股，返回 (holdings_list, report_date)。"""
    try:
        df = ak.fund_portfolio_hold_em(symbol=fund_code, date=year)
        if df.empty:
            return [], ""
        report_date = str(df.iloc[0, -1]) if len(df.columns) > 0 else ""
        df = df.head(10)
        holdings = []
        for _, row in df.iterrows():
            holdings.append({
                "code": str(row.get("股票代码", "")),
                "name": str(row.get("股票名称", "")),
                "ratio": float(row.get("占净值比例", 0))
            })
        return holdings, report_date
    except Exception as e:
        print(f"  [警告] 获取 {fund_code} 重仓股失败: {e}")
        return [], ""


# ============================================================
# 细分类行业（按重仓股所属东财行业聚合）
# ============================================================

_STOCK_INDUSTRY_CACHE_PATH = Path(__file__).parent / "data" / "stock_industry_cache.json"
_stock_industry_cache = None
_http_opener = None


def _get_http_opener():
    """构建忽略系统代理的 opener，避免本机代理导致东方财富接口失败。"""
    global _http_opener
    if _http_opener is None:
        _http_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _http_opener


def _load_stock_industry_cache() -> dict:
    global _stock_industry_cache
    if _stock_industry_cache is not None:
        return _stock_industry_cache
    if _STOCK_INDUSTRY_CACHE_PATH.exists():
        try:
            _stock_industry_cache = json.loads(
                _STOCK_INDUSTRY_CACHE_PATH.read_text(encoding="utf-8")
            )
        except Exception:
            _stock_industry_cache = {}
    else:
        _stock_industry_cache = {}
    return _stock_industry_cache


def _save_stock_industry_cache() -> None:
    if _stock_industry_cache is None:
        return
    try:
        _STOCK_INDUSTRY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STOCK_INDUSTRY_CACHE_PATH.write_text(
            json.dumps(_stock_industry_cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"  [警告] 写入行业缓存失败: {e}")


def _normalize_stock_code(code: str) -> str:
    code = str(code or "").strip().upper()
    if not code or code in {"NAN", "NONE", "-"}:
        return ""
    # 去掉市场前缀 / 后缀
    code = code.replace("SH", "").replace("SZ", "").replace("BJ", "")
    code = code.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    code = "".join(ch for ch in code if ch.isdigit())
    if not code:
        return ""
    return code.zfill(6)


def _to_secid(code: str) -> str:
    code = _normalize_stock_code(code)
    if not code:
        return ""
    # 沪市: 5/6/9 开头；北交所 4/8 开头用 0. 也可；深市 0/1/2/3
    if code.startswith(("5", "6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def _http_get_json(url: str, retries: int = 2) -> dict:
    last_err = None
    opener = _get_http_opener()
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://quote.eastmoney.com/",
                },
            )
            with opener.open(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            return json.loads(raw) if raw else {}
        except Exception as e:
            last_err = e
            time.sleep(0.25 * (i + 1))
    raise last_err


def fetch_stock_industry(stock_code: str, stock_name: str = "", save_cache: bool = True) -> str:
    """
    获取单只股票的细分类行业（东方财富 f127，如 半导体/电池/白酒）。
    比证监会门类（制造业/采矿业）细很多。
    """
    code = _normalize_stock_code(stock_code)
    name = str(stock_name or "").strip()
    cache_key = code or f"name:{name}"
    if not cache_key or cache_key == "name:":
        return "未知"

    cache = _load_stock_industry_cache()
    if cache_key in cache and cache[cache_key] and cache[cache_key] != "未知":
        return cache[cache_key]

    industry = "未知"

    # 1) 东方财富细行业
    if code:
        secid = _to_secid(code)
        if secid:
            try:
                url = (
                    "https://push2.eastmoney.com/api/qt/stock/get"
                    f"?fltt=2&invt=2&fields=f57,f58,f127,f128&secid={secid}"
                )
                data = (_http_get_json(url).get("data") or {})
                ind_name = str(data.get("f127") or "").strip()
                if ind_name and ind_name not in {"-", "None", "nan"}:
                    industry = ind_name
            except Exception:
                pass

    # 2) 同花顺主营业务关键词
    if industry in {"未知", "其他"} and code:
        try:
            df = ak.stock_zyjs_ths(symbol=code)
            if df is not None and not df.empty:
                text = " ".join(str(x) for x in df.iloc[0].tolist())
                industry = _infer_industry_from_text(text + " " + name)
        except Exception:
            pass

    # 3) 股票名称关键词（覆盖港股/美股无代码、接口失败场景）
    if industry in {"未知", "其他"}:
        industry = _infer_industry_from_text(name)

    if industry not in {"未知", ""}:
        cache[cache_key] = industry
        if save_cache:
            _save_stock_industry_cache()
    return industry if industry else "未知"


def _infer_industry_from_text(text: str) -> str:
    text = text or ""
    rules = [
        ("半导体设备", ["半导体设备", "刻蚀", "薄膜沉积", "光刻", "清洗设备", "CMP", "量测设备", "华海清科", "中微", "拓荆", "北方华创", "盛美", "芯源微"]),
        ("半导体", ["半导体", "芯片", "集成电路", "晶圆", "封测", "存储", "台积电", "中芯", "华虹", "韦尔", "兆易", "寒武纪", "海光", "英伟达", "美光", "闪迪", "西部数据", "Tower"]),
        ("消费电子", ["消费电子", "智能手机", "耳机", "可穿戴", "果链", "立讯", "歌尔"]),
        ("电池", ["锂电池", "动力电池", "储能电池", "电池", "宁德时代", "亿纬", "欣旺达", "赣锋", "天齐"]),
        ("光伏设备", ["光伏", "硅片", "组件", "逆变器", "隆基", "通威", "阳光电源", "协鑫"]),
        ("军工", ["航天", "航空", "军工", "导弹", "卫星", "商业航天", "火箭"]),
        ("通信设备", ["通信设备", "基站", "光模块", "5G", "中兴", "烽火", "新易盛", "中际旭创"]),
        ("计算机设备", ["服务器", "计算机设备", "IT设备", "工业富联"]),
        ("软件开发", ["软件", "云计算", "SaaS", "人工智能", "大数据", "金山", "用友", "恒生电子"]),
        ("白酒", ["白酒", "茅台", "五粮液", "泸州老窖", "汾酒"]),
        ("证券", ["证券", "券商"]),
        ("银行", ["银行"]),
        ("保险", ["保险"]),
        ("医疗器械", ["医疗器械", "体外诊断", "迈瑞"]),
        ("化学制药", ["制药", "化学药", "原料药", "恒瑞", "药明"]),
        ("汽车零部件", ["汽车零部件", "汽车配件", "拓普", "伯特利"]),
        ("新能源车", ["新能源汽车", "电动汽车", "比亚迪", "理想", "蔚来", "小鹏"]),
        ("有色金属", ["锂", "钴", "镍", "铜", "铝", "矿业", "有色", "稀土"]),
        ("电力", ["电力", "水电", "火电", "绿电", "能源"]),
        ("养殖", ["养殖", "牧原", "温氏", "圣农", "肉鸡", "生猪"]),
    ]
    for label, keys in rules:
        if any(k in text for k in keys):
            return label
    return "其他"


def build_industry_from_holdings(holdings: list) -> list:
    """
    根据前十大重仓股聚合细分类行业占比。
    返回: [{name, ratio}, ...]  ratio 为占基金净值比例之和。
    """
    if not holdings:
        return []

    buckets = {}
    for h in holdings:
        ratio = safe_float(h.get("ratio", 0))
        if ratio <= 0:
            continue
        ind = fetch_stock_industry(h.get("code", ""), stock_name=h.get("name", ""), save_cache=False)
        if not ind:
            ind = "未知"
        buckets[ind] = buckets.get(ind, 0.0) + ratio

    industry = [
        {"name": name, "ratio": round(ratio, 2)}
        for name, ratio in buckets.items()
        if ratio > 0
    ]
    industry.sort(key=lambda x: x["ratio"], reverse=True)
    _save_stock_industry_cache()
    return industry


def fetch_fund_industry_coarse(fund_code: str, year: str = "2026") -> list:
    """原始证监会门类行业配置（制造业/采矿业等，较粗，仅作回退）。"""
    try:
        df = ak.fund_portfolio_industry_allocation_em(symbol=fund_code, date=year)
        if df.empty:
            return []
        last_col = df.columns[-1]
        if "时间" in str(last_col) or "期" in str(last_col):
            latest_date = df[last_col].max()
            df = df[df[last_col] == latest_date].copy()

        industry = []
        for _, row in df.iterrows():
            ratio = float(row.iloc[2]) if len(row) > 2 else 0
            if ratio > 0:
                industry.append({
                    "name": str(row.iloc[1]) if len(row) > 1 else "",
                    "ratio": ratio
                })
        return industry
    except Exception as e:
        print(f"  [警告] 获取 {fund_code} 粗粒度行业配置失败: {e}")
        return []


def fetch_fund_industry(fund_code: str, year: str = "2026", holdings: list = None) -> list:
    """
    获取基金行业分布：
    1) 优先按重仓股细分类行业聚合（半导体/电池等）
    2) 若无持仓可聚合，则回退证监会门类
    """
    if holdings is None:
        holdings, _ = fetch_fund_holdings(fund_code, year=year)

    fine = build_industry_from_holdings(holdings or [])
    if fine:
        return fine
    return fetch_fund_industry_coarse(fund_code, year=year)


def safe_float(val, default=0.0):
    """将值转为 float，NaN/Inf 替换为 default。"""
    try:
        result = float(val)
        if pd.isna(result) or result in (float('inf'), float('-inf')):
            return default
        return result
    except (ValueError, TypeError):
        return default


def fetch_single_fund_data(row: pd.Series) -> dict:
    """获取单只基金的完整数据（用于并行处理）。"""
    code = get_fund_code(row)
    name = get_fund_name(row)

    if not code:
        return None

    holdings, report_date = fetch_fund_holdings(code)
    # 先清洗 holdings，再基于持仓聚合细行业
    for h in holdings:
        h["ratio"] = safe_float(h.get("ratio", 0))

    industry = fetch_fund_industry(code, holdings=holdings)
    for ind in industry:
        ind["ratio"] = safe_float(ind.get("ratio", 0))

    return {
        "code": code,
        "name": name,
        "y1": safe_float(row.get("y1", 0)),
        "m6": safe_float(row.get("m6", 0)),
        "m3": safe_float(row.get("m3", 0)),
        "m1": safe_float(row.get("m1", 0)),
        "daily": safe_float(row.get("daily", 0)),
        "holdings": holdings,
        "industry": industry,
        "report_date": report_date
    }


def build_fund_list(filtered_df: pd.DataFrame, max_funds: int = 20) -> list:
    """构建基金列表数据（并行获取持仓）。"""
    funds_to_process = filtered_df.head(max_funds)
    total = len(funds_to_process)

    print(f"[数据处理] 开始并行获取前 {total} 只基金的持仓数据...")

    funds = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch_single_fund_data, row): idx
            for idx, (_, row) in enumerate(funds_to_process.iterrows(), 1)
        }

        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
                if result:
                    funds.append(result)
                    print(f"  [{len(funds)}/{total}] 完成 {result['code']}")
            except Exception as e:
                print(f"  [错误] 第 {idx} 只基金处理失败: {e}")

    code_order = {get_fund_code(row): i for i, (_, row) in enumerate(funds_to_process.iterrows())}
    funds.sort(key=lambda x: code_order.get(x["code"], 999))

    return funds


# ============================================================
# 统一数据获取入口
# ============================================================

def get_daily_surge_data(df=None) -> dict:
    """
    获取当日收益率 >6% 和 <-6% 的基金数据（含持仓+行业）。
    可选传入预取的 DataFrame 以复用排行数据，避免重复 API 调用。
    返回:
        {
            "surge_fund_data": list,   # 当日涨幅 > 6%
            "surge_fund_count": int,
            "plunge_fund_data": list,  # 当日跌幅 < -6%
            "plunge_fund_count": int,
        }
    """
    start_time = time.time()
    if df is None:
        print("[每日飙升] 正在获取基金数据...")
        df = fetch_fund_ranking()
    else:
        print("[每日飙升] 复用已获取的排行数据...")

    # 映射常用列
    col_map = {"近1年": "y1", "近6月": "m6", "近3月": "m3", "近1月": "m1"}
    rename_map = {}
    for cn_name, en_name in col_map.items():
        for col in df.columns:
            if cn_name in col:
                rename_map[col] = en_name
                break
    df = df.rename(columns=rename_map)

    # 找到「日增长率」列
    daily_col = None
    for col in df.columns:
        if "日增" in str(col) or "日涨" in str(col):
            daily_col = col
            break
    if daily_col is None:
        print("[每日飙升] 未找到日增长率列")
        return {"surge_fund_data": [], "surge_fund_count": 0, "plunge_fund_data": [], "plunge_fund_count": 0}

    df["daily"] = pd.to_numeric(df[daily_col], errors="coerce")

    for col in ["y1", "m6", "m3", "m1"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 飙升基金（当日 >6%）
    surge_df = df[df["daily"] > 6].copy()
    if not surge_df.empty:
        surge_df = surge_df.sort_values(by="daily", ascending=False).head(30)
        surge_fund_data = build_fund_list(surge_df, max_funds=30)
        surge_fund_count = len(surge_fund_data)
    else:
        surge_fund_data, surge_fund_count = [], 0

    # 暴跌基金（当日 <-6%）
    plunge_df = df[df["daily"] < -6].copy()
    if not plunge_df.empty:
        plunge_df = plunge_df.sort_values(by="daily", ascending=True).head(30)
        plunge_fund_data = build_fund_list(plunge_df, max_funds=30)
        plunge_fund_count = len(plunge_fund_data)
    else:
        plunge_fund_data, plunge_fund_count = [], 0

    elapsed = time.time() - start_time
    print(f"[每日飙升] 数据处理耗时 {elapsed:.2f} 秒")
    print(f"  当日涨幅 >6%: {surge_fund_count} 只")
    print(f"  当日跌幅 <-6%: {plunge_fund_count} 只")

    return {
        "surge_fund_data": surge_fund_data,
        "surge_fund_count": surge_fund_count,
        "plunge_fund_data": plunge_fund_data,
        "plunge_fund_count": plunge_fund_count,
    }


def get_page_data(y1: float, m6: float, m3: float, m1: float, df=None) -> dict:
    """
    获取页面所需的全部数据（含持仓/行业，用于静态生成）。
    可选传入预取的 DataFrame 以复用排行数据。
    """
    start_time = time.time()

    if df is None:
        df = fetch_fund_ranking()
    else:
        print("[页面数据] 复用已获取的排行数据...")

    # 高收益基金
    filtered = screen_funds(df, y1, m6, m3, m1)
    if filtered.empty:
        fund_data, fund_count = [], 0
    else:
        filtered = filtered.sort_values(by="m1", ascending=False).head(30)
        fund_data = build_fund_list(filtered, max_funds=30)
        fund_count = len(fund_data)

    # 亏损基金
    col_map = {"近1年": "y1", "近6月": "m6", "近3月": "m3", "近1月": "m1"}
    rename_map = {}
    for cn_name, en_name in col_map.items():
        for col in df.columns:
            if cn_name in col:
                rename_map[col] = en_name
                break
    df_loss = df.rename(columns=rename_map)
    df_loss["m1"] = pd.to_numeric(df_loss["m1"], errors="coerce")
    loss_mask = df_loss["m1"] <= -15
    loss_filtered = df_loss[loss_mask].copy()

    if loss_filtered.empty:
        loss_fund_data, loss_fund_count = [], 0
    else:
        loss_filtered = loss_filtered.sort_values(by="m1", ascending=True).head(30)
        loss_fund_data = build_fund_list(loss_filtered, max_funds=30)
        loss_fund_count = len(loss_fund_data)

    elapsed = time.time() - start_time
    print(f"[完成] 数据处理耗时 {elapsed:.2f} 秒")
    print(f"  高收益基金: {fund_count} 只")
    print(f"  亏损基金: {loss_fund_count} 只")

    return {
        "fund_data": fund_data,
        "fund_count": fund_count,
        "loss_fund_data": loss_fund_data,
        "loss_fund_count": loss_fund_count,
    }
