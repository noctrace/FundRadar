"""
Fund-Radar 数据层
=================
AKShare 接口调用 + 基金筛选 + 持仓数据获取。
与 HTTP 层完全解耦，可独立测试或复用到其他前端/服务中。
"""

import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def fetch_fund_industry(fund_code: str, year: str = "2026") -> list:
    """获取单只基金的行业配置（只取最新一期）。"""
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
        print(f"  [警告] 获取 {fund_code} 行业配置失败: {e}")
        return []


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
    industry = fetch_fund_industry(code)

    # 清洗 holdings 中的 NaN 值
    for h in holdings:
        h["ratio"] = safe_float(h.get("ratio", 0))
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

def get_daily_surge_data() -> dict:
    """
    获取当日收益率 >6% 和 <-6% 的基金数据（含持仓+行业）。
    返回:
        {
            "surge_fund_data": list,   # 当日涨幅 > 6%
            "surge_fund_count": int,
            "plunge_fund_data": list,  # 当日跌幅 < -6%
            "plunge_fund_count": int,
        }
    """
    start_time = time.time()
    print("[每日飙升] 正在获取基金数据...")

    df = fetch_fund_ranking()

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


def get_page_data(y1: float, m6: float, m3: float, m1: float) -> dict:
    """
    一次性获取页面所需的全部数据。

    返回:
        {
            "fund_data": list,
            "fund_count": int,
            "loss_fund_data": list,
            "loss_fund_count": int,
        }
    """
    start_time = time.time()

    # 抓取排行
    df = fetch_fund_ranking()

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
