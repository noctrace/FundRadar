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
# 行业三级（东财 CoreConception ssbk L1/L2/L3，按重仓股聚合）
# ============================================================
# 主路径: emweb F10 CoreConception → BOARD_RANK 1/2/3
# 回退: 东财 f127（约 L2）→ 关键词（修正锂矿≠电池）
# 缓存: data/stock_industry_cache.json  value = {l1,l2,l3,source,updated}
# 饼图聚合键: L3 → 缺省回退 L2 → L1
# 概念标签 / 板块涨跌页: 本期不做（后续用大额持仓推断概念）

_STOCK_INDUSTRY_CACHE_PATH = Path(__file__).parent / "data" / "stock_industry_cache.json"
_stock_industry_cache = None
_http_opener = None
_CACHE_SCHEMA_VERSION = 3  # string-era cache must be re-fetched

# rank>=4 中常见非行业噪声（地域/风格/指数），解析 L1-3 时忽略
_NON_INDUSTRY_BOARD_HINTS = (
    "板块", "风格", "指数", "成份", "成分", "股通", "通", "融资", "融券",
    "MSCI", "富时", "标准普尔", "AH股", "HS300", "上证", "深证", "创业",
    "科创", "大盘", "小盘", "中盘", "价值", "成长", "权重", "龙头", "红利",
    "昨日", "连板", "打板", "破净", "扭亏", "预增", "一季报", "年报",
)


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
    if code.startswith(("5", "6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def _to_em_f10_code(code: str) -> str:
    """东财 F10 代码: SH600000 / SZ000001 / BJ430047。"""
    code = _normalize_stock_code(code)
    if not code:
        return ""
    if code.startswith(("5", "6", "9")):
        return f"SH{code}"
    if code.startswith(("4", "8")):
        return f"BJ{code}"
    return f"SZ{code}"


def _http_get_json(url: str, retries: int = 2, referer: str = "https://quote.eastmoney.com/") -> dict:
    last_err = None
    opener = _get_http_opener()
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": referer,
                    "Accept": "*/*",
                },
            )
            with opener.open(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            return json.loads(raw) if raw else {}
        except Exception as e:
            last_err = e
            time.sleep(0.35 * (i + 1))
    raise last_err


def _empty_industry_info(source: str = "") -> dict:
    return {
        "l1": "",
        "l2": "",
        "l3": "",
        "source": source,
        "updated": time.strftime("%Y-%m-%d"),
    }


def _is_valid_industry_cache_entry(val) -> bool:
    """新 schema：dict 且至少有 l3 或 l2；旧 string 缓存视为失效。"""
    if not isinstance(val, dict):
        return False
    l3 = str(val.get("l3") or "").strip()
    l2 = str(val.get("l2") or "").strip()
    return bool(l3 or l2) and l3 != "未知" and l2 != "未知"


def _display_industry_name(info: dict) -> str:
    """饼图聚合键：优先 L3，其次 L2、L1。"""
    if not isinstance(info, dict):
        return "未知"
    for key in ("l3", "l2", "l1"):
        name = str(info.get(key) or "").strip()
        if name and name not in {"未知", "其他", "-", "None"}:
            return name
    return "未知"


def _parse_ssbk_levels(ssbk: list) -> dict:
    """
    从 CoreConception ssbk 解析 L1/L2/L3。
    BOARD_RANK 1/2/3 对应行业一级/二级/三级（东财 F10 实测）。
    """
    info = _empty_industry_info(source="em_ssbk")
    if not ssbk:
        return info

    by_rank = {}
    for item in ssbk:
        if not isinstance(item, dict):
            continue
        try:
            rank = int(item.get("BOARD_RANK") or 0)
        except (TypeError, ValueError):
            continue
        name = str(item.get("BOARD_NAME") or "").strip()
        if not name or name in {"-", "None"}:
            continue
        # rank 1-3 直接取；若缺 rank 3 不从后续概念补
        if rank in (1, 2, 3) and rank not in by_rank:
            by_rank[rank] = name

    info["l1"] = by_rank.get(1, "")
    info["l2"] = by_rank.get(2, "")
    info["l3"] = by_rank.get(3, "")
    return info


def _fetch_industry_from_core_conception(code: str) -> dict:
    """东财 F10 核心题材/所属板块 → L1/L2/L3。"""
    em_code = _to_em_f10_code(code)
    if not em_code:
        return _empty_industry_info()
    url = (
        "https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax"
        f"?code={em_code}"
    )
    data = _http_get_json(
        url,
        retries=3,
        referer="https://emweb.securities.eastmoney.com/",
    )
    ssbk = data.get("ssbk") if isinstance(data, dict) else None
    info = _parse_ssbk_levels(ssbk or [])
    if info.get("l3") or info.get("l2") or info.get("l1"):
        info["source"] = "em_ssbk"
        info["updated"] = time.strftime("%Y-%m-%d")
        return info
    return _empty_industry_info(source="em_ssbk_empty")


def _fetch_industry_from_f127(code: str) -> dict:
    """回退：东财 push2 f127（约二级行业名，写入 l2，l3 留空）。"""
    secid = _to_secid(code)
    if not secid:
        return _empty_industry_info()
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get"
        f"?fltt=2&invt=2&fields=f57,f58,f127,f128&secid={secid}"
    )
    data = (_http_get_json(url).get("data") or {})
    ind_name = str(data.get("f127") or "").strip()
    if not ind_name or ind_name in {"-", "None", "nan"}:
        return _empty_industry_info(source="em_f127_empty")
    info = _empty_industry_info(source="em_f127")
    info["l2"] = ind_name
    # f127 无三级时用二级名作为展示键，避免饼图全「未知」
    info["l3"] = ind_name
    return info


def _infer_industry_info_from_text(text: str) -> dict:
    """
    关键词兜底 → 尽量对齐 L3 命名。
    禁忌：锂矿公司名不得进「锂电池/电池」规则。
    """
    text = text or ""
    # 更具体的标签必须排在更泛之前
    rules = [
        ("半导体设备", "半导体", "电子", ["半导体设备", "刻蚀", "薄膜沉积", "光刻", "清洗设备", "CMP", "量测设备", "华海清科", "中微", "拓荆", "北方华创", "盛美", "芯源微", "中科飞测"]),
        ("集成电路制造", "半导体", "电子", ["晶圆代工", "集成电路制造", "中芯国际", "华虹"]),
        ("数字芯片设计", "半导体", "电子", ["芯片设计", "MCU", "SoC", "兆易", "韦尔", "寒武纪", "海光", "瑞芯微", "全志"]),
        ("半导体材料", "半导体", "电子", ["半导体材料", "光刻胶", "电子特气", "硅片材料", "沪硅"]),
        ("半导体", "半导体", "电子", ["半导体", "芯片", "集成电路", "封测", "存储芯片", "台积电", "英伟达", "美光", "闪迪", "西部数据", "Tower"]),
        ("锂", "能源金属", "有色金属", ["锂矿", "锂业", "锂盐", "碳酸锂", "氢氧化锂", "赣锋", "天齐", "中矿资源", "融捷", "永兴材料", "藏格", "雅化"]),
        ("锂电池", "电池", "电力设备", ["锂电池", "动力电池", "储能电池", "宁德时代", "亿纬", "欣旺达", "国轩", "蜂巢能源"]),
        ("电池化学品", "电池", "电力设备", ["电解液", "隔膜", "正极", "负极", "六氟", "恩捷", "天赐", "璞泰来", "当升", "德方"]),
        ("光伏设备", "光伏设备", "电力设备", ["光伏", "硅片", "组件", "逆变器", "隆基", "通威", "阳光电源", "协鑫", "天合", "晶科", "晶澳"]),
        ("消费电子", "消费电子", "电子", ["消费电子", "智能手机", "耳机", "可穿戴", "果链", "立讯", "歌尔"]),
        ("军工", "军工", "国防军工", ["航天", "航空", "军工", "导弹", "卫星", "商业航天", "火箭"]),
        ("通信设备", "通信设备", "通信", ["通信设备", "基站", "光模块", "5G", "中兴", "烽火", "新易盛", "中际旭创"]),
        ("计算机设备", "计算机设备", "计算机", ["服务器", "计算机设备", "IT设备", "工业富联"]),
        ("软件开发", "软件开发", "计算机", ["软件", "云计算", "SaaS", "人工智能", "大数据", "金山", "用友", "恒生电子"]),
        ("白酒", "白酒", "食品饮料", ["白酒", "茅台", "五粮液", "泸州老窖", "汾酒"]),
        ("证券", "证券", "非银金融", ["证券", "券商"]),
        ("银行", "银行", "银行", ["银行"]),
        ("保险", "保险", "非银金融", ["保险"]),
        ("医疗器械", "医疗器械", "医药生物", ["医疗器械", "体外诊断", "迈瑞"]),
        ("化学制药", "化学制药", "医药生物", ["制药", "化学药", "原料药", "恒瑞", "药明"]),
        ("汽车零部件", "汽车零部件", "汽车", ["汽车零部件", "汽车配件", "拓普", "伯特利"]),
        ("乘用车", "乘用车", "汽车", ["新能源汽车", "电动汽车", "比亚迪", "理想", "蔚来", "小鹏", "乘用车"]),
        ("能源金属", "能源金属", "有色金属", ["钴", "镍", "稀土", "能源金属"]),
        ("有色金属", "有色金属", "有色金属", ["铜", "铝", "矿业", "有色"]),
        ("电力", "电力", "公用事业", ["电力", "水电", "火电", "绿电"]),
        ("养殖业", "养殖业", "农林牧渔", ["养殖", "牧原", "温氏", "圣农", "肉鸡", "生猪"]),
    ]
    for l3, l2, l1, keys in rules:
        if any(k in text for k in keys):
            info = _empty_industry_info(source="keyword")
            info["l1"], info["l2"], info["l3"] = l1, l2, l3
            return info
    info = _empty_industry_info(source="keyword")
    info["l3"] = "其他"
    info["l2"] = "其他"
    return info


def fetch_stock_industry_info(
    stock_code: str,
    stock_name: str = "",
    save_cache: bool = True,
    force_refresh: bool = False,
) -> dict:
    """
    获取个股行业三级信息 {l1,l2,l3,source,updated}。
    优先级: 缓存(新schema) → CoreConception ssbk → f127 → 同花顺主营/名称关键词。
    """
    code = _normalize_stock_code(stock_code)
    name = str(stock_name or "").strip()
    cache_key = code or (f"name:{name}" if name else "")
    if not cache_key:
        return _empty_industry_info()

    cache = _load_stock_industry_cache()
    if not force_refresh and _is_valid_industry_cache_entry(cache.get(cache_key)):
        return cache[cache_key]

    info = _empty_industry_info()

    # 1) 东财 CoreConception（L1/L2/L3）
    if code:
        try:
            info = _fetch_industry_from_core_conception(code)
            time.sleep(0.12)  # 轻限流，避免 Actions 并发打爆
        except Exception as e:
            print(f"  [警告] CoreConception 失败 {code}: {e}")

    # 2) f127 回退
    if not (info.get("l3") or info.get("l2")) and code:
        try:
            info = _fetch_industry_from_f127(code)
            time.sleep(0.08)
        except Exception:
            pass

    # 3) 仅当无有效 L2/L3 时用关键词；有 L2 无 L3 时用 L2 顶上，不覆盖 ssbk
    has_l2 = bool(str(info.get("l2") or "").strip())
    has_l3 = (
        bool(str(info.get("l3") or "").strip())
        and str(info.get("l3")).strip() not in {"其他", "未知"}
    )
    if not has_l3 and has_l2:
        info["l3"] = str(info.get("l2")).strip()
    elif not has_l2 and not has_l3:
        text = name
        if code:
            try:
                df = ak.stock_zyjs_ths(symbol=code)
                if df is not None and not df.empty:
                    text = " ".join(str(x) for x in df.iloc[0].tolist()) + " " + name
            except Exception:
                pass
        info = _infer_industry_info_from_text(text)

    if not info.get("updated"):
        info["updated"] = time.strftime("%Y-%m-%d")

    display = _display_industry_name(info)
    if display and display != "未知":
        cache[cache_key] = info
        if save_cache:
            _save_stock_industry_cache()
    return info


def fetch_stock_industry(stock_code: str, stock_name: str = "", save_cache: bool = True) -> str:
    """兼容旧接口：返回用于聚合的展示行业名（优先 L3）。"""
    info = fetch_stock_industry_info(stock_code, stock_name=stock_name, save_cache=save_cache)
    return _display_industry_name(info)


def build_industry_from_holdings(holdings: list) -> list:
    """
    根据前十大重仓股按行业三级（L3）聚合占比。
    返回: [{name, ratio, level}, ...]  ratio 为占基金净值比例之和。
    概念归属不在此写入：后续可用大额持仓的 L3/主题再推断。
    """
    if not holdings:
        return []

    buckets = {}
    level_of = {}
    for h in holdings:
        ratio = safe_float(h.get("ratio", 0))
        if ratio <= 0:
            continue
        info = fetch_stock_industry_info(
            h.get("code", ""),
            stock_name=h.get("name", ""),
            save_cache=False,
        )
        name = _display_industry_name(info)
        if not name:
            name = "未知"
        # 记录所用层级，便于前端/排查
        if info.get("l3") and name == str(info.get("l3")).strip():
            level = "l3"
        elif info.get("l2") and name == str(info.get("l2")).strip():
            level = "l2"
        elif info.get("l1") and name == str(info.get("l1")).strip():
            level = "l1"
        else:
            level = "l3"
        buckets[name] = buckets.get(name, 0.0) + ratio
        level_of[name] = level

    industry = [
        {"name": name, "ratio": round(ratio, 2), "level": level_of.get(name, "l3")}
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
                    "ratio": ratio,
                    "level": "coarse",
                })
        return industry
    except Exception as e:
        print(f"  [警告] 获取 {fund_code} 粗行业失败: {e}")
        return []


def fetch_fund_industry(fund_code: str, year: str = "2026", holdings: list = None) -> list:
    """
    获取基金行业分布：
    1) 优先按重仓股 L3 聚合（锂/锂电池/半导体设备/集成电路制造…）
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


# ============================================================
# 主题板块涨跌（概念大方向，非三级行业）
# ============================================================
# 优先东财概念板块日 K（连涨/连跌、月内上涨天、月增幅）。
# K 线不可达时：ulist 快照（今日涨跌 + f109≈近1月）+ 本地日线缓存累积连涨/上涨天。

_SECTOR_HTTP_TIMEOUT = 15
_SECTOR_LIST_HOSTS = (
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
)
_SECTOR_HIST_HOSTS = (
    "https://82.push2his.eastmoney.com",
    "https://83.push2his.eastmoney.com",
    "https://91.push2his.eastmoney.com",
    "https://push2his.eastmoney.com",
)
_SECTOR_QUOTE_HOSTS = (
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
)
_SECTOR_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Connection": "close",
}
_SECTOR_DAILY_CACHE = Path(__file__).resolve().parent / "data" / "sector_daily_cache.json"

# display_name -> preferred East Money concept board names (first hit wins)
SECTOR_THEMES = [
    ("商业航天", ["商业航天"]),
    ("半导体", ["半导体概念", "芯片概念"]),
    ("锂矿", ["锂矿概念"]),
    ("PCB", ["PCB"]),
    ("存储芯片", ["存储芯片"]),
    ("CPO", ["CPO概念", "光模块概念"]),
    ("固态电池", ["固态电池"]),
    ("人工智能", ["人工智能", "ChatGPT概念", "AIGC概念"]),
    ("人形机器人", ["人形机器人", "机器人概念"]),
    ("光伏", ["光伏概念"]),
    ("新能源车", ["新能源车"]),
    ("军工", ["军工"]),
    ("低空经济", ["低空经济"]),
    ("液冷", ["液冷概念"]),
    ("算力", ["算力概念"]),
    ("数据中心", ["数据中心"]),
    ("国产芯片", ["国产芯片"]),
    ("消费电子", ["消费电子概念"]),
    ("华为概念", ["华为概念"]),
    ("第三代半导体", ["第三代半导体"]),
]


def _sector_http_json(url: str, retries: int = 2, opener=None):
    """GET JSON; rejects HTML / empty payloads."""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=_SECTOR_UA)
            if opener is not None:
                resp = opener.open(req, timeout=_SECTOR_HTTP_TIMEOUT)
            else:
                resp = urllib.request.urlopen(req, timeout=_SECTOR_HTTP_TIMEOUT)
            with resp:
                raw = resp.read().decode("utf-8-sig", errors="replace").strip()
                if not raw or raw[:1] not in "{[":
                    raise ValueError(f"non-json body: {raw[:60]!r}")
                return json.loads(raw)
        except Exception as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise last


def _sector_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_concept_name_map(opener=None) -> dict:
    """Return {board_name: BK_code} for all East Money concept boards."""
    last_err = None
    for host in _SECTOR_LIST_HOSTS:
        try:
            rows = []
            total = None
            for pn in range(1, 40):
                url = (
                    f"{host}/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
                    f"&fs=m:90+t:3+f:!50&fields=f12,f14"
                )
                data = _sector_http_json(url, opener=opener)
                diff = (data.get("data") or {}).get("diff") or []
                if not diff:
                    break
                rows.extend(diff)
                total = (data.get("data") or {}).get("total") or total
                if total and len(rows) >= int(total):
                    break
                time.sleep(0.12)
            name_map = {}
            for d in rows:
                n = str(d.get("f14") or "").strip()
                c = str(d.get("f12") or "").strip()
                if n and c:
                    name_map[n] = c
            if name_map:
                print(f"[板块] 概念列表 {len(name_map)} 个 (host={host})")
                return name_map
        except Exception as e:
            last_err = e
            print(f"[板块] 列表失败 {host}: {e}")
            continue
    if last_err:
        raise last_err
    return {}


def _resolve_theme_board(display: str, candidates: list, name_map: dict):
    """Pick (em_name, bk_code) for a theme."""
    for cand in candidates:
        if cand in name_map:
            return cand, name_map[cand]
    for cand in candidates:
        hits = [(n, c) for n, c in name_map.items() if cand in n]
        if hits:
            hits.sort(key=lambda x: len(x[0]))
            return hits[0]
    return None, None


def _fetch_board_daily_bars(bk_code: str, lookback_calendar_days: int = 55, opener=None) -> list:
    """Daily bars from EM kline: [{date, close, pct}, ...] ascending."""
    lmt = max(40, int(lookback_calendar_days * 0.7))
    last_err = None
    for host in _SECTOR_HIST_HOSTS:
        try:
            url = (
                f"{host}/api/qt/stock/kline/get?secid=90.{bk_code}"
                f"&ut=fa5fd1943c7b386f172d6893dbfba10b"
                f"&fields1=f1,f2,f3,f4,f5,f6"
                f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
                f"&klt=101&fqt=0&end=20500101&lmt={lmt}"
            )
            data = _sector_http_json(url, retries=1, opener=opener)
            if not isinstance(data, dict):
                raise ValueError("bad payload")
            klines = (data.get("data") or {}).get("klines") or []
            rows = []
            for line in klines:
                parts = str(line).split(",")
                if len(parts) < 9:
                    continue
                try:
                    pct_raw = parts[8]
                    pct = float(pct_raw) if pct_raw not in ("", "-") else 0.0
                    close = float(parts[2])
                except (TypeError, ValueError):
                    continue
                rows.append({"date": parts[0], "close": close, "pct": pct})
            if rows:
                return rows
            raise ValueError("empty klines")
        except Exception as e:
            last_err = e
            continue
    if last_err:
        # quiet: caller may fall back to snapshot/cache
        pass
    return []


def _fetch_board_snapshots(bk_codes: list, opener=None) -> dict:
    """Batch quote via ulist. Return {BK: {close, pct, change_1m, date}}."""
    if not bk_codes:
        return {}
    # f2 price, f3 today%, f18 preclose, f109 ~近1月%, f124 ts
    fields = "f12,f14,f2,f3,f18,f109,f124"
    out = {}
    # chunk to keep URL short
    for i in range(0, len(bk_codes), 40):
        chunk = bk_codes[i:i + 40]
        secids = ",".join(f"90.{c}" for c in chunk)
        last_err = None
        for host in _SECTOR_QUOTE_HOSTS:
            try:
                url = (
                    f"{host}/api/qt/ulist.np/get?fltt=2&secids={secids}&fields={fields}"
                )
                data = _sector_http_json(url, retries=2, opener=opener)
                diff = (data.get("data") or {}).get("diff") or []
                for d in diff:
                    code = str(d.get("f12") or "").strip()
                    if not code:
                        continue
                    try:
                        close = float(d.get("f2") or 0)
                    except (TypeError, ValueError):
                        close = 0.0
                    try:
                        pct = float(d.get("f3") or 0)
                    except (TypeError, ValueError):
                        pct = 0.0
                    try:
                        m1 = float(d.get("f109") or 0)
                    except (TypeError, ValueError):
                        m1 = 0.0
                    # f124 is unix ts; fall back to today
                    day = time.strftime("%Y-%m-%d")
                    try:
                        ts = int(d.get("f124") or 0)
                        if ts > 1_000_000_000:
                            day = time.strftime("%Y-%m-%d", time.localtime(ts))
                    except (TypeError, ValueError):
                        pass
                    out[code] = {
                        "close": close,
                        "pct": pct,
                        "change_1m": m1,
                        "date": day,
                        "name": str(d.get("f14") or ""),
                    }
                if out:
                    break
            except Exception as e:
                last_err = e
                continue
        if last_err and not out:
            print(f"  [警告] 板块快照失败: {last_err}")
    return out


def _load_sector_daily_cache() -> dict:
    try:
        if _SECTOR_DAILY_CACHE.exists():
            return json.loads(_SECTOR_DAILY_CACHE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_sector_daily_cache(cache: dict) -> None:
    try:
        _SECTOR_DAILY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _SECTOR_DAILY_CACHE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"  [警告] 写入板块日线缓存失败: {e}")


def _merge_bars_into_cache(cache: dict, bk: str, bars: list) -> None:
    """Merge kline bars into cache[bk] = {date: {close,pct}}."""
    if not bars:
        return
    bucket = cache.setdefault(bk, {})
    for b in bars:
        d = str(b.get("date") or "")
        if not d:
            continue
        bucket[d] = {
            "close": float(b.get("close") or 0),
            "pct": float(b.get("pct") or 0),
        }


def _append_snapshot_to_cache(cache: dict, bk: str, snap: dict) -> None:
    if not snap:
        return
    d = str(snap.get("date") or time.strftime("%Y-%m-%d"))
    bucket = cache.setdefault(bk, {})
    bucket[d] = {
        "close": float(snap.get("close") or 0),
        "pct": float(snap.get("pct") or 0),
    }
    # prune > 80 calendar entries
    if len(bucket) > 80:
        for old in sorted(bucket.keys())[:-80]:
            bucket.pop(old, None)


def _bars_from_cache(cache: dict, bk: str) -> list:
    bucket = cache.get(bk) or {}
    rows = []
    for d in sorted(bucket.keys()):
        item = bucket[d] or {}
        rows.append({
            "date": d,
            "close": float(item.get("close") or 0),
            "pct": float(item.get("pct") or 0),
        })
    return rows


def _compute_board_metrics(bars: list, trading_days: int = 22, snap_change_1m=None) -> dict:
    """streak_days >0 up streak, <0 down; up_days_1m; change_1m; latest_pct/date."""
    if not bars:
        m1 = 0.0 if snap_change_1m is None else float(snap_change_1m)
        return {
            "streak_days": 0,
            "up_days_1m": 0,
            "trading_days_1m": 0,
            "change_1m": round(m1, 2),
            "latest_pct": 0.0,
            "latest_date": "",
        }
    latest = bars[-1]
    latest_pct = float(latest.get("pct") or 0.0)
    streak = 0
    if latest_pct > 0:
        for b in reversed(bars):
            if float(b.get("pct") or 0) > 0:
                streak += 1
            else:
                break
    elif latest_pct < 0:
        for b in reversed(bars):
            if float(b.get("pct") or 0) < 0:
                streak -= 1
            else:
                break

    window = bars[-trading_days:] if len(bars) >= trading_days else bars[:]
    up_days = sum(1 for b in window if float(b.get("pct") or 0) > 0)
    change_1m = 0.0
    if len(window) >= 2 and float(window[0].get("close") or 0) > 0:
        change_1m = (float(window[-1]["close"]) / float(window[0]["close"]) - 1.0) * 100.0
    elif snap_change_1m is not None:
        change_1m = float(snap_change_1m)

    return {
        "streak_days": int(streak),
        "up_days_1m": int(up_days),
        "trading_days_1m": int(len(window)),
        "change_1m": round(change_1m, 2),
        "latest_pct": round(latest_pct, 2),
        "latest_date": str(latest.get("date") or ""),
    }


def get_sector_board_data(trading_days: int = 22) -> dict:
    """拉取白名单主题概念板块涨跌统计。"""
    print(f"\n{'─'*40}")
    print("[板块] 主题板块涨跌（概念大方向）")
    opener = _sector_opener()
    try:
        name_map = _fetch_concept_name_map(opener=opener)
    except Exception as e:
        print(f"[板块] 获取概念列表失败: {e}")
        return {
            "sectors": [],
            "sector_count": 0,
            "update_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "em_concept",
            "error": str(e),
        }

    # resolve themes first
    resolved = []
    for display, candidates in SECTOR_THEMES:
        em_name, bk = _resolve_theme_board(display, candidates, name_map)
        if not bk:
            print(f"  [跳过] {display}: 未匹配东财概念")
            continue
        resolved.append((display, em_name, bk))

    cache = _load_sector_daily_cache()
    snaps = _fetch_board_snapshots([bk for _, _, bk in resolved], opener=opener)
    print(f"[板块] 快照 {len(snaps)}/{len(resolved)} 个")

    kline_ok = 0
    sectors = []
    for display, em_name, bk in resolved:
        bars = _fetch_board_daily_bars(bk, opener=opener)
        src = "kline"
        if bars:
            kline_ok += 1
            _merge_bars_into_cache(cache, bk, bars)
            time.sleep(0.25)
        else:
            # fallback: snapshot + accumulated daily cache
            snap = snaps.get(bk) or {}
            _append_snapshot_to_cache(cache, bk, snap)
            bars = _bars_from_cache(cache, bk)
            src = "snapshot+cache" if bars else "empty"
            time.sleep(0.05)

        snap = snaps.get(bk) or {}
        metrics = _compute_board_metrics(
            bars,
            trading_days=trading_days,
            snap_change_1m=snap.get("change_1m"),
        )
        # if bars empty but snap exists, still surface today / 1m
        if not bars and snap:
            metrics["latest_pct"] = round(float(snap.get("pct") or 0), 2)
            metrics["latest_date"] = str(snap.get("date") or "")
            metrics["change_1m"] = round(float(snap.get("change_1m") or 0), 2)
            # single-day streak from today
            p = float(snap.get("pct") or 0)
            metrics["streak_days"] = 1 if p > 0 else (-1 if p < 0 else 0)
            metrics["up_days_1m"] = 1 if p > 0 else 0
            metrics["trading_days_1m"] = 1

        item = {
            "name": display,
            "board_name": em_name or snap.get("name") or display,
            "code": bk,
            "source": src,
            **metrics,
        }
        sectors.append(item)
        print(
            f"  {display:8s} ({em_name}/{bk}) "
            f"streak={item['streak_days']:+d} "
            f"up={item['up_days_1m']}/{item['trading_days_1m']} "
            f"m1={item['change_1m']:+.2f}% "
            f"today={item['latest_pct']:+.2f}% "
            f"[{src}]"
        )

    _save_sector_daily_cache(cache)

    sectors.sort(key=lambda x: x.get("change_1m") or 0, reverse=True)
    result = {
        "sectors": sectors,
        "sector_count": len(sectors),
        "update_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "em_concept",
        "trading_days_window": trading_days,
        "kline_ok": kline_ok,
        "note": (
            "日K可用时直接计算；否则用快照(f109≈近1月)并写入 data/sector_daily_cache.json，"
            "随每日 generate 累积连涨/月内上涨天。"
        ),
    }
    print(f"[板块] 完成 {len(sectors)} 个主题 (kline={kline_ok})")
    return result
