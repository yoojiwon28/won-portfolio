"""
SWING Portfolio - GitHub Actions 자동 업데이트 스크립트 v3
데이터 흐름:
  노션 매매일지 DB → 보유주식 계산 → 현재가 조회
  → 노션 table/DB 동시 업데이트 + 차트 생성
트리거:
  - 매일 KST 09:00 자동실행
  - Make 버튼 클릭 시 즉시실행 (workflow_dispatch)
"""
 
import os, json, time, warnings
from datetime import datetime, timedelta
from pathlib import Path
 
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.ticker as mticker
import yfinance as yf
from pykrx import stock as krx
 
warnings.filterwarnings("ignore")
 
# ─────────────────────────────────────────────
# 환경 변수
# ─────────────────────────────────────────────
NOTION_TOKEN   = os.environ["NOTION_TOKEN"].strip()
NOTION_PAGE_ID = os.environ["NOTION_PAGE_ID"].strip().replace("-", "")
DB_TRADE       = os.environ["DB_TRADE"].strip().replace("-", "")
DB_HOLDINGS    = os.environ["DB_HOLDINGS"].strip().replace("-", "")
DB_ASSETS      = os.environ["DB_ASSETS"].strip().replace("-", "")
# 페이지 맨 마지막 paragraph ID (child_database 다음 빈 블록)
# 이 블록 뒤에 새 섹션들을 추가함
ANCHOR_BLOCK_ID = "3824a83490b980cba7bcca33676983e5"
 
# ID 검증
for _name, _val in [("NOTION_PAGE_ID", NOTION_PAGE_ID),
                     ("DB_TRADE", DB_TRADE),
                     ("DB_HOLDINGS", DB_HOLDINGS),
                     ("DB_ASSETS", DB_ASSETS)]:
    if len(_val) != 32 or not _val.isalnum():
        print(f"  ⚠ {_name} 형식 이상: '{_val}' (길이={len(_val)})")
    else:
        print(f"  ✅ {_name}: {_val[:8]}...{_val[-4:]}")
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "YOUR_USER/swing-portfolio")
GITHUB_BRANCH  = os.environ.get("GITHUB_BRANCH", "main")
 
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}
TODAY     = datetime.today().strftime("%Y-%m-%d")
TODAY_KRX = datetime.today().strftime("%Y%m%d")
CHARTS_DIR   = Path("charts")
DATA_DIR     = Path("data")
CHARTS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
HISTORY_FILE = DATA_DIR / "history.csv"
 
# ─────────────────────────────────────────────
# 한글 폰트
# ─────────────────────────────────────────────
def setup_font():
    nanum = [f for f in fm.findSystemFonts() if "Nanum" in f or "nanum" in f]
    if nanum:
        plt.rcParams["font.family"] = fm.FontProperties(fname=nanum[0]).get_name()
    else:
        plt.rcParams["font.family"] = "DejaVu Sans"
    plt.rcParams["axes.unicode_minus"] = False
 
setup_font()
 
def raw_url(filename):
    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/charts/{filename}"
 
# ─────────────────────────────────────────────
# Notion API 유틸
# ─────────────────────────────────────────────
def nget(path):
    r = requests.get(f"https://api.notion.com/v1/{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()
 
def npatch(path, body):
    r = requests.patch(f"https://api.notion.com/v1/{path}",
                       headers=HEADERS, data=json.dumps(body, ensure_ascii=False))
    r.raise_for_status()
    return r.json()
 
def npost(path, body):
    r = requests.post(f"https://api.notion.com/v1/{path}",
                      headers=HEADERS, data=json.dumps(body, ensure_ascii=False))
    if not r.ok:
        print(f"  ❌ Notion API 오류 ({r.status_code}): {r.text[:300]}")
    r.raise_for_status()
    return r.json()
 
def ndelete(block_id):
    r = requests.delete(f"https://api.notion.com/v1/blocks/{block_id}",
                        headers=HEADERS)
    r.raise_for_status()
 
# ─────────────────────────────────────────────
# rich_text / 블록 헬퍼
# ─────────────────────────────────────────────
def rt(content, bold=False, color=None):
    obj = {"type": "text", "text": {"content": str(content)},
           "annotations": {"bold": bold}}
    if color:
        obj["annotations"]["color"] = color
    return obj
 
def trow(cells):
    return {"object": "block", "type": "table_row",
            "table_row": {"cells": [[rt(c)] for c in cells]}}
 
def image_block(url):
    return {"object": "block", "type": "image",
            "image": {"type": "external", "external": {"url": url}}}
 
def h2_block(text):
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [rt(text, bold=True)]}}
 
def h3_block(text):
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [rt(text, bold=True)]}}
 
def para_block(text, color=None):
    rich = [rt(text)]
    if color:
        rich[0]["annotations"]["color"] = color
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": rich}}
 
def divider_block():
    return {"object": "block", "type": "divider", "divider": {}}
 
def get_all_blocks(page_id):
    result, cursor = [], None
    while True:
        url = f"blocks/{page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        data = nget(url)
        result.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return result
 
def get_table_rows(tid):
    return nget(f"blocks/{tid}/children?page_size=100")["results"]
 
def update_row(row_id, cells):
    npatch(f"blocks/{row_id}",
           {"table_row": {"cells": [[rt(c)] for c in cells]}})
 
def append_row(tid, cells):
    npost(f"blocks/{tid}/children", {"children": [trow(cells)]})
 
def append_blocks(parent_id, children, ignore_errors=False):
    # Notion API는 한번에 100블록 제한
    for i in range(0, len(children), 100):
        try:
            npost(f"blocks/{parent_id}/children", {"children": children[i:i+100]})
        except Exception as e:
            if ignore_errors:
                print(f"  ⚠ 블록 추가 실패 (무시): {e}")
            else:
                raise
 
def create_table_with_rows(parent_id, col_count, has_header, header_cells, data_rows):
    """
    Notion API 제약: 테이블 생성 시 children(행) 포함 불가.
    1) 빈 테이블 생성 → 2) 헤더 행 추가 → 3) 데이터 행 추가
    """
    # 1) 빈 테이블 블록 생성
    resp = npost(f"blocks/{parent_id}/children", {"children": [{
        "object": "block", "type": "table",
        "table": {
            "table_width": col_count,
            "has_column_header": has_header,
            "has_row_header": False,
        }
    }]})
    table_id = resp["results"][0]["id"]
    time.sleep(0.3)
 
    # 2) 헤더 행 추가
    npost(f"blocks/{table_id}/children", {"children": [trow(header_cells)]})
    time.sleep(0.2)
 
    # 3) 데이터 행 추가 (50행씩)
    for i in range(0, len(data_rows), 50):
        batch = data_rows[i:i+50]
        npost(f"blocks/{table_id}/children", {"children": batch})
        time.sleep(0.2)
 
    return table_id
 
# ─────────────────────────────────────────────
# DB 프로퍼티 헬퍼
# ─────────────────────────────────────────────
def prop_title(text):
    return {"title": [{"text": {"content": str(text)}}]}
 
def prop_rich_text(text):
    return {"rich_text": [{"text": {"content": str(text)}}]}
 
def prop_number(value):
    return {"number": float(value) if value is not None else None}
 
def prop_select(name):
    return {"select": {"name": str(name)}}
 
def prop_date(date_str):
    # date_str: "YYYY-MM-DD" 또는 "YYYYMMDD"
    if len(date_str) == 8:
        date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    return {"date": {"start": date_str}}
 
def get_text(prop):
    """Notion 프로퍼티에서 텍스트 추출"""
    ptype = prop.get("type")
    if ptype == "title":
        items = prop.get("title", [])
    elif ptype == "rich_text":
        items = prop.get("rich_text", [])
    else:
        return ""
    return "".join(t.get("plain_text", "") for t in items)
 
def get_number(prop):
    return prop.get("number")
 
def get_select(prop):
    sel = prop.get("select")
    return sel["name"] if sel else ""
 
def get_date(prop):
    d = prop.get("date")
    return d["start"] if d else ""
 
# ─────────────────────────────────────────────
# 노션 매매일지 DB에서 데이터 읽기
# ─────────────────────────────────────────────
def load_trades_from_notion():
    """매매일지 DB 전체 읽기 → trades 리스트 반환"""
    trades = []
    cursor = None
    while True:
        body = {"page_size": 100,
                "sorts": [{"property": "날짜", "direction": "ascending"}]}
        if cursor:
            body["start_cursor"] = cursor
        data = npost(f"databases/{DB_TRADE}/query", body)
 
        for page in data["results"]:
            p = page["properties"]
            date_raw = get_date(p.get("날짜", {}))
            date_str = date_raw.replace("-", "") if date_raw else ""
            trades.append({
                "date":     date_str,
                "name":     get_text(p.get("종목이름", {})),
                "ticker":   get_text(p.get("티커", {})),
                "type":     get_select(p.get("매수매도", {})),
                "qty":      int(get_number(p.get("수량", {})) or 0),
                "price":    int(get_number(p.get("단가", {})) or 0),
                "category": get_select(p.get("분류", {})),
                "reason":   get_text(p.get("사유", {})),
                "page_id":  page["id"],
            })
 
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
 
    print(f"  ✅ 노션 매매일지 DB에서 {len(trades)}건 로드")
    return trades
 
# ─────────────────────────────────────────────
# 보유주식 집계
# ─────────────────────────────────────────────
def aggregate_holdings(trades):
    holdings = {}
    for t in sorted(trades, key=lambda x: x["date"]):
        if not t["ticker"] or not t["qty"] or not t["price"]:
            continue
        tk = t["ticker"]
        if tk not in holdings:
            holdings[tk] = {
                "name":            t["name"],
                "ticker":          tk,
                "qty":             0,
                "total_cost":      0,
                "category":        t["category"],
                "first_buy_date":  t["date"],
            }
        if t["type"] == "매수":
            holdings[tk]["qty"]        += t["qty"]
            holdings[tk]["total_cost"] += t["qty"] * t["price"]
        elif t["type"] == "매도":
            if holdings[tk]["qty"] > 0:
                avg = holdings[tk]["total_cost"] / holdings[tk]["qty"]
                holdings[tk]["qty"]        -= t["qty"]
                holdings[tk]["total_cost"] -= t["qty"] * avg
 
    result = []
    for h in holdings.values():
        if h["qty"] > 0:
            h["avg_price"] = h["total_cost"] / h["qty"]
            buy_dt = datetime.strptime(h["first_buy_date"], "%Y%m%d") \
                     if len(h["first_buy_date"]) == 8 \
                     else datetime.strptime(h["first_buy_date"], "%Y-%m-%d")
            h["hold_days"] = (datetime.today() - buy_dt).days
            result.append(h)
    return result
 
# ─────────────────────────────────────────────
# 현재가 조회
# ─────────────────────────────────────────────
def get_krx_price(ticker):
    for delta in range(5):
        date = (datetime.today() - timedelta(days=delta)).strftime("%Y%m%d")
        try:
            df = krx.get_market_ohlcv_by_date(
                fromdate=date, todate=date, ticker=ticker)
            if not df.empty:
                return int(df.iloc[-1]["종가"])
        except Exception:
            pass
    return None
 
def get_current_price(ticker, category):
    try:
        if category in ("국내종목", "국내ETF", "국내ETF-해외"):
            return get_krx_price(ticker)
        else:
            tk = yf.Ticker(ticker)
            return tk.fast_info["last_price"]
    except Exception as e:
        print(f"    ⚠ {ticker} 가격 조회 실패: {e}")
        return None
 
# ─────────────────────────────────────────────
# 히스토리 저장 (누적 수익 곡선용)
# ─────────────────────────────────────────────
def save_history(total_eval, total_profit, total_rate):
    import csv
    rows = []
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    rows = [r for r in rows if r["date"] != TODAY]
    rows.append({"date": TODAY,
                 "total_eval":   round(total_eval),
                 "total_profit": round(total_profit),
                 "total_rate":   round(total_rate, 4)})
    with open(HISTORY_FILE, "w", newline="", encoding="utf-8") as f:
        import csv as csv2
        w = csv2.DictWriter(f, fieldnames=["date","total_eval","total_profit","total_rate"])
        w.writeheader(); w.writerows(rows)
    print(f"  ✅ 히스토리 저장 ({len(rows)}일치)")
    return rows
 
# ─────────────────────────────────────────────
# 차트 ① 분류별 파이차트
# ─────────────────────────────────────────────
def chart_pie(holdings):
    cat_amt = {}
    for h in holdings:
        cat_amt[h["category"]] = cat_amt.get(h["category"], 0) + h["eval_amount"]
    if not cat_amt:
        return None
    labels = list(cat_amt.keys())
    sizes  = list(cat_amt.values())
    colors = ["#4E79A7","#F28E2B","#59A14F","#E15759","#76B7B2","#EDC948"]
    total  = sum(sizes)
 
    fig, ax = plt.subplots(figsize=(8, 6), facecolor="white")
    _, texts, autotexts = ax.pie(
        sizes, labels=labels, autopct="%1.1f%%", startangle=90,
        colors=colors[:len(labels)], pctdistance=0.82,
        wedgeprops={"linewidth": 1.5, "edgecolor": "white"})
    for t in texts:    t.set_fontsize(12)
    for at in autotexts:
        at.set_fontsize(11); at.set_fontweight("bold"); at.set_color("white")
 
    ax.set_title(f"보유주식 분류별 비중\n총평가금액 {total:,.0f}원",
                 fontsize=14, fontweight="bold", pad=20)
    ax.legend([f"{l}  {s:,.0f}원 ({s/total*100:.1f}%)"
               for l, s in zip(labels, sizes)],
              loc="lower center", bbox_to_anchor=(0.5, -0.18), ncol=2, fontsize=10)
    ax.text(0.99, 0.01, f"기준: {TODAY}", transform=ax.transAxes,
            fontsize=9, color="gray", ha="right", va="bottom")
    plt.tight_layout()
    path = CHARTS_DIR / "pie_category.png"
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  ✅ 파이차트 저장")
    return "pie_category.png"
 
# ─────────────────────────────────────────────
# 차트 ② 종목별 수익률 바 차트
# ─────────────────────────────────────────────
def chart_bar_profit(holdings):
    if not holdings:
        return None
    names  = [h["name"] for h in holdings]
    rates  = [h["profit_rate"] for h in holdings]
    colors = ["#1f77b4" if r >= 0 else "#d62728" for r in rates]
 
    fig, ax = plt.subplots(figsize=(max(8, len(names) * 1.2), 6), facecolor="white")
    bars = ax.bar(names, rates, color=colors, width=0.6,
                  edgecolor="white", linewidth=0.8)
    for bar, rate in zip(bars, rates):
        ypos  = bar.get_height() + (0.3 if rate >= 0 else -0.3)
        emoji = "📈" if rate >= 0 else "📉"
        ax.text(bar.get_x() + bar.get_width() / 2, ypos,
                f"{emoji} {rate:+.2f}%",
                ha="center", va="bottom" if rate >= 0 else "top",
                fontsize=10, fontweight="bold",
                color="#1f77b4" if rate >= 0 else "#d62728")
    ax.axhline(0, color="black", linewidth=1.0)
    ymin, ymax = ax.get_ylim()
    ax.axhspan(0,    ymax, alpha=0.04, color="#1f77b4")
    ax.axhspan(ymin, 0,   alpha=0.04, color="#d62728")
    ax.set_ylabel("수익률 (%)", fontsize=11)
    ax.set_title(f"종목별 수익률  (현재가 기준, {TODAY})",
                 fontsize=14, fontweight="bold", pad=15)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f%%"))
    ax.tick_params(axis="x", labelsize=10, rotation=20)
    ax.grid(axis="y", alpha=0.3, linestyle="--")
    ax.text(0.99, 0.01, f"기준: {TODAY}", transform=ax.transAxes,
            fontsize=9, color="gray", ha="right", va="bottom")
    plt.tight_layout()
    path = CHARTS_DIR / "bar_profit.png"
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  ✅ 수익률 바 차트 저장")
    return "bar_profit.png"
 
# ─────────────────────────────────────────────
# 차트 ③ 누적 총자산 곡선
# ─────────────────────────────────────────────
def chart_history_curve(history_rows):
    if len(history_rows) < 2:
        print("  ⚠ 히스토리 2일치 이상 필요 — 곡선 생략")
        return None
    dates  = [r["date"]                   for r in history_rows]
    evals  = [int(r["total_eval"])         for r in history_rows]
    rates  = [float(r["total_rate"])       for r in history_rows]
 
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8),
                                   facecolor="white", sharex=True)
    fig.suptitle(f"포트폴리오 총자산 변화  (최근 {len(dates)}일)",
                 fontsize=14, fontweight="bold")
    ax1.plot(dates, evals, color="#1f77b4", linewidth=2.2,
             marker="o", markersize=5)
    ax1.fill_between(dates, evals, min(evals), alpha=0.12, color="#1f77b4")
    ax1.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda x, _: f"{x/10000:.0f}만원"))
    ax1.set_ylabel("총평가금액", fontsize=10)
    ax1.grid(alpha=0.3, linestyle="--")
    ax1.tick_params(axis="x", rotation=30, labelsize=8)
 
    bar_colors = ["#1f77b4" if r >= 0 else "#d62728" for r in rates]
    ax2.bar(dates, rates, color=bar_colors, width=0.6, alpha=0.85)
    ax2.axhline(0, color="black", linewidth=0.8)
    ax2.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.2f%%"))
    ax2.set_ylabel("총수익률 (%)", fontsize=10)
    ax2.grid(axis="y", alpha=0.3, linestyle="--")
    ax2.tick_params(axis="x", rotation=30, labelsize=8)
    plt.tight_layout()
    path = CHARTS_DIR / "history_curve.png"
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  ✅ 누적 총자산 곡선 저장")
    return "history_curve.png"
 
# ─────────────────────────────────────────────
# 차트 ④ 지수 대비 월별 수익률
# ─────────────────────────────────────────────
INDEX_TICKERS = {"S&P500": "^GSPC", "나스닥100": "^NDX"}  # 코스피200은 별도 처리
KOSPI200_TICKER = "1028"  # pykrx 코스피200 ETF 대용 (KODEX 200)
WATCHLIST = [
    {"name":"테슬라",       "ticker":"TSLA",   "index":"나스닥100"},
    {"name":"구글(알파벳)", "ticker":"GOOG",   "index":"나스닥100"},
    {"name":"엔비디아",     "ticker":"NVDA",   "index":"나스닥100"},
    {"name":"SK하이닉스",   "ticker":"000660", "index":"코스피200"},
    {"name":"현대자동차",   "ticker":"005380", "index":"코스피200"},
    {"name":"삼성전자",     "ticker":"005930", "index":"코스피200"},
    {"name":"삼성전자우",   "ticker":"005935", "index":"코스피200"},
    {"name":"삼성전기",     "ticker":"009150", "index":"코스피200"},
    {"name":"타이거200",    "ticker":"102110", "index":"코스피200"},
    {"name":"월마트",       "ticker":"WMT",    "index":"S&P500"},
    {"name":"존슨앤드존슨", "ticker":"JNJ",    "index":"S&P500"},
    {"name":"코카콜라",     "ticker":"KO",     "index":"S&P500"},
]
 
def get_monthly_returns_yf(ticker, months=6):
    end   = datetime.today()
    start = (end - timedelta(days=months * 31)).replace(day=1)
    df = yf.download(ticker, start=start, end=end,
                     interval="1mo", progress=False, auto_adjust=True)
    if df.empty:
        return {}
    closes = df["Close"].dropna()
    return {closes.index[i+1].strftime("%Y-%m"):
            round(float((closes.iloc[i+1]/closes.iloc[i]-1)*100), 2)
            for i in range(len(closes)-1)}
 
def get_monthly_returns_krx(ticker, months=6):
    end   = datetime.today()
    start = end - timedelta(days=months*31+10)
    try:
        df = krx.get_market_ohlcv_by_date(
            fromdate=start.strftime("%Y%m%d"),
            todate=end.strftime("%Y%m%d"),
            ticker=ticker, freq="m")
        if df.empty: return {}
        closes = df["종가"]
        return {closes.index[i+1].strftime("%Y-%m"):
                round(float((closes.iloc[i+1]/closes.iloc[i]-1)*100), 2)
                for i in range(len(closes)-1)}
    except Exception:
        return {}
 
def chart_index_comparison():
    print("  기준지수 조회 중...")
    index_returns = {}
    for idx_name, idx_ticker in INDEX_TICKERS.items():
        index_returns[idx_name] = get_monthly_returns_yf(idx_ticker, months=6)
        time.sleep(0.5)
    # 코스피200: pykrx로 조회 (069500 = KODEX 200)
    index_returns["코스피200"] = get_monthly_returns_krx("069500", months=6)
    time.sleep(0.3)
 
    all_months = set()
    for r in index_returns.values():
        all_months.update(r.keys())
    months_sorted = sorted(all_months)[-6:]
 
    stock_data, judgements = [], []
    print("  관심종목 조회 중...")
    for item in WATCHLIST:
        is_domestic = item["ticker"].isdigit() and len(item["ticker"]) == 6
        ret = get_monthly_returns_krx(item["ticker"]) if is_domestic \
              else get_monthly_returns_yf(item["ticker"])
        time.sleep(0.3)
 
        idx_ret   = index_returns.get(item["index"], {})
        stock_cum = sum(ret.get(m, 0) for m in months_sorted)
        idx_cum   = sum(idx_ret.get(m, 0) for m in months_sorted)
        diff      = stock_cum - idx_cum
        judgements.append({
            "name": item["name"], "ticker": item["ticker"],
            "index": item["index"],
            "종목누적(%)": round(stock_cum, 2),
            "지수누적(%)": round(idx_cum, 2),
            "차이(%)":    round(diff, 2),
            "판정":       "손절 검토 ⚠️" if diff < -10 else "유지 ✅",
        })
        stock_data.append({**item, "returns": ret})
        print(f"    {item['name']}: {stock_cum:+.1f}%  지수 {idx_cum:+.1f}%")
 
    # 서브플롯
    index_groups = {}
    for sd in stock_data:
        index_groups.setdefault(sd["index"], []).append(sd)
    n = len(index_groups)
    fig, axes = plt.subplots(n, 1, figsize=(13, 4.5*n), facecolor="white")
    if n == 1: axes = [axes]
    colors_s = ["#4E79A7","#F28E2B","#59A14F","#E15759",
                "#76B7B2","#EDC948","#B07AA1","#FF9DA7"]
 
    for ax, (idx_name, stocks) in zip(axes, index_groups.items()):
        idx_ret = index_returns.get(idx_name, {})
        ax.plot(months_sorted, [idx_ret.get(m) for m in months_sorted],
                color="black", linewidth=2.5, linestyle="--",
                marker="D", markersize=6, label=f"[지수] {idx_name}", zorder=5)
        for i, sd in enumerate(stocks):
            ax.plot(months_sorted, [sd["returns"].get(m) for m in months_sorted],
                    color=colors_s[i % len(colors_s)], linewidth=1.8,
                    marker="o", markersize=5, label=sd["name"], alpha=0.85)
        ax.axhline(0,   color="gray",   linewidth=0.8, linestyle="-",  alpha=0.5)
        ax.axhline(-10, color="#E15759", linewidth=0.8, linestyle=":",  alpha=0.6)
        ax.text(months_sorted[-1], -10.5, "-10% 참고선",
                fontsize=8, color="#E15759", ha="right")
        ax.set_title(f"{idx_name} 기준 — 6개월 월별 수익률 비교",
                     fontsize=13, fontweight="bold", pad=10)
        ax.set_ylabel("월별 수익률 (%)", fontsize=10)
        ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f%%"))
        ax.legend(loc="upper left", fontsize=9, framealpha=0.7,
                  ncol=2 if len(stocks) > 4 else 1)
        ax.grid(axis="y", alpha=0.3)
 
    fig.suptitle(f"관심종목 × 기준지수 수익률 비교 ({TODAY})",
                 fontsize=15, fontweight="bold", y=1.01)
    plt.tight_layout()
    path = CHARTS_DIR / "index_comparison.png"
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  ✅ 지수비교 차트 저장")
    return "index_comparison.png", judgements, months_sorted
 
# ─────────────────────────────────────────────
# 노션 [table] 블록 업데이트
# ─────────────────────────────────────────────
def find_table_blocks(page_id):
    """페이지에서 table 블록 ID를 헤더 기준으로 매핑"""
    blocks = get_all_blocks(page_id)
    tables = {}
    last_h2 = None
 
    for b in blocks:
        btype = b["type"]
        if btype == "heading_2":
            txt = "".join(r["text"]["content"]
                          for r in b["heading_2"]["rich_text"])
            for key in ("총자산", "보유주식", "최근 매매일지",
                        "수익 분석", "보유기간 트래커", "지수기반 종목분석"):
                if key in txt:
                    last_h2 = key
                    tables.setdefault(key, {})["heading_id"] = b["id"]
                    tables[key]["heading_block"] = b
                    break
        if btype == "table" and last_h2:
            sec = tables.setdefault(last_h2, {})
            if "table_id" not in sec:
                sec["table_id"] = b["id"]
        if btype == "heading_3" and last_h2:
            txt = "".join(r["text"]["content"]
                          for r in b["heading_3"]["rich_text"])
            sec = tables.setdefault(last_h2, {})
            if "분류별 비율"    in txt: sec["pie_h3"]   = (b["id"], blocks.index(b))
            if "수익률 바 차트" in txt: sec["bar_h3"]   = (b["id"], blocks.index(b))
            if "누적 총자산"    in txt: sec["curve_h3"] = (b["id"], blocks.index(b))
            if "비교 차트"      in txt: sec["chart_h3"] = (b["id"], blocks.index(b))
        if btype == "paragraph" and "callout" not in tables:
            txt = "".join(r["text"]["content"]
                          for r in b["paragraph"].get("rich_text", []))
            if "작성일자" in txt:
                tables["paragraph_date"] = b["id"]
 
    return tables, blocks
 
def update_table_total_assets(sec, total_eval, total_profit, total_rate):
    tid = sec.get("table_id")
    if not tid: return
    rows  = get_table_rows(tid)
    emoji = "📈" if total_rate >= 0 else "📉"
    cells = [TODAY, f"{total_eval:,.0f}원",
             f"{total_profit:+,.0f}원", f"{emoji} {total_rate:+.2f}%"]
    if len(rows) >= 2: update_row(rows[1]["id"], cells)
    else:              append_row(tid, cells)
 
def update_table_holdings(sec, holdings):
    """보유주식 테이블 업데이트 — 헤더 컬럼 수 자동 감지해서 맞춤"""
    tid = sec.get("table_id")
    if not tid: return
    all_rows = get_table_rows(tid)
    if not all_rows: return
 
    # 헤더에서 컬럼 수와 현재가 포함 여부 확인
    header_cells = all_rows[0]["table_row"]["cells"]
    header_texts = ["".join(t.get("plain_text","") for t in cell)
                    for cell in header_cells]
    has_current_price = "현재가" in header_texts
    col_count = len(header_texts)
 
    existing = all_rows[1:]
    for i, h in enumerate(holdings):
        emoji = "📈" if h["profit_rate"] >= 0 else "📉"
        if has_current_price:
            # 현재가 컬럼 포함 9컬럼 (평가금액 → 현재가 순서)
            cells = [
                h["name"], h["ticker"],
                f"{h['eval_amount']:,.0f}원",
                f"{h.get('current_price', h['avg_price']):,.0f}원",
                f"{h['profit']:+,.0f}원",
                f"{emoji} {h['profit_rate']:+.2f}%",
                str(h["qty"]),
                f"{h['avg_price']:,.0f}원",
                h["category"],
            ]
        else:
            # 현재가 없는 기존 8컬럼
            cells = [
                h["name"], h["ticker"],
                f"{h['eval_amount']:,.0f}원",
                f"{h['profit']:+,.0f}원",
                f"{emoji} {h['profit_rate']:+.2f}%",
                str(h["qty"]),
                f"{h['avg_price']:,.0f}원",
                h["category"],
            ]
        # 실제 테이블 컬럼 수에 맞게 자르거나 채우기
        cells = cells[:col_count]
        while len(cells) < col_count:
            cells.append("")
        if i < len(existing): update_row(existing[i]["id"], cells)
        else:                  append_row(tid, cells)
        time.sleep(0.15)
 
def update_table_trade_log(sec, trades):
    """최근 날짜 매매일지만 [table]에 표시, heading 날짜도 업데이트"""
    tid = sec.get("table_id")
    hid = sec.get("heading_id")
    if not tid or not trades: return
 
    latest_date = max(t["date"] for t in trades if t["date"])
    latest = [t for t in trades if t["date"] == latest_date]
 
    # heading_2 날짜 업데이트
    if hid:
        date_fmt = f"{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:]}" \
                   if len(latest_date) == 8 else latest_date
        npatch(f"blocks/{hid}", {
            "heading_2": {"rich_text": [rt(f"📝 최근 매매일지 ({date_fmt})", bold=True)]}
        })
 
    existing = get_table_rows(tid)[1:]
    for i, t in enumerate(latest):
        date_fmt = f"{t['date'][:4]}-{t['date'][4:6]}-{t['date'][6:]}" \
                   if len(t["date"]) == 8 else t["date"]
        cells = [date_fmt, t["name"], t["ticker"], t["type"],
                 str(t["qty"]), f"{t['price']:,}원", t["category"], t["reason"]]
        if i < len(existing): update_row(existing[i]["id"], cells)
        else:                  append_row(tid, cells)
        time.sleep(0.15)
 
# ─────────────────────────────────────────────
# 노션 DB 업데이트 (child_database)
# ─────────────────────────────────────────────
def get_db_pages(db_id):
    """DB의 모든 페이지 반환"""
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor: body["start_cursor"] = cursor
        data = npost(f"databases/{db_id}/query", body)
        pages.extend(data["results"])
        if not data.get("has_more"): break
        cursor = data["next_cursor"]
    return pages
 
def db_update_total_assets(total_eval, total_profit, total_rate):
    """총자산 DB: 오늘 날짜 행 업데이트 or 신규 추가"""
    pages = get_db_pages(DB_ASSETS)
    # 오늘 날짜 행 찾기
    today_page = None
    for p in pages:
        title = get_text(p["properties"].get("작성일자", {}))
        if title == TODAY:
            today_page = p
            break
 
    props = {
        "작성일자":   prop_title(TODAY),
        "총평가금액": prop_number(total_eval),
        "총수익":     prop_number(total_profit),
        "총수익률":   prop_rich_text(f"{total_rate:+.2f}%"),
    }
    if today_page:
        npatch(f"pages/{today_page['id']}", {"properties": props})
    else:
        npost("pages", {"parent": {"database_id": DB_ASSETS}, "properties": props})
    print("  ✅ 총자산 DB 업데이트")
 
def db_update_holdings(holdings):
    """보유주식 DB: 전체 동기화 (기존 전체 삭제 후 재작성)"""
    # 기존 페이지 아카이브 (삭제 대신 archived 처리)
    existing = get_db_pages(DB_HOLDINGS)
    for p in existing:
        npatch(f"pages/{p['id']}", {"archived": True})
        time.sleep(0.1)
 
    # 신규 작성
    for h in holdings:
        emoji = "📈" if h["profit_rate"] >= 0 else "📉"
        props = {
            "종목이름":  prop_title(h["name"]),
            "티커":      prop_rich_text(h["ticker"]),
            "평가금액":  prop_number(h["eval_amount"]),
            "수익":      prop_number(h["profit"]),
            "수익률":    prop_rich_text(f"{emoji} {h['profit_rate']:+.2f}%"),
            "보유수량":  prop_number(h["qty"]),
            "매입가":    prop_number(h["avg_price"]),
            "분류":      prop_select(h["category"]),
        }
        npost("pages", {"parent": {"database_id": DB_HOLDINGS}, "properties": props})
        time.sleep(0.2)
    print(f"  ✅ 보유주식 DB 업데이트 ({len(holdings)}종목)")
 
def db_sync_trade_log(trades):
    """
    매매일지 DB 동기화:
    - 기존 DB에 없는 항목만 추가 (날짜+티커+매수매도 기준 중복 방지)
    """
    existing = get_db_pages(DB_TRADE)
    existing_keys = set()
    for p in existing:
        pr = p["properties"]
        date = get_date(pr.get("날짜", {}))
        tk   = get_text(pr.get("티커", {}))
        typ  = get_select(pr.get("매수매도", {}))
        existing_keys.add(f"{date}_{tk}_{typ}")
 
    added = 0
    for t in trades:
        date_fmt = f"{t['date'][:4]}-{t['date'][4:6]}-{t['date'][6:]}" \
                   if len(t["date"]) == 8 else t["date"]
        key = f"{date_fmt}_{t['ticker']}_{t['type']}"
        if key in existing_keys:
            continue  # 이미 있으면 스킵
        props = {
            "종목이름": prop_title(t["name"]),
            "티커":     prop_rich_text(t["ticker"]),
            "날짜":     prop_date(t["date"]),
            "매수매도": prop_select(t["type"]),
            "수량":     prop_number(t["qty"]),
            "단가":     prop_number(t["price"]),
            "분류":     prop_select(t["category"]),
            "사유":     prop_rich_text(t["reason"]),
        }
        npost("pages", {"parent": {"database_id": DB_TRADE}, "properties": props})
        existing_keys.add(key)
        added += 1
        time.sleep(0.2)
    print(f"  ✅ 매매일지 DB 동기화 (신규 {added}건)")
 
# ─────────────────────────────────────────────
# 차트 이미지 블록 upsert
# ─────────────────────────────────────────────
def upsert_image_after_h3(page_id, all_blocks, h3_info, img_url):
    """heading_3 다음 image 블록 URL 교체. 없으면 page에 append."""
    try:
        if h3_info:
            h3_id, h3_idx = h3_info
            next_idx = h3_idx + 1
            if next_idx < len(all_blocks) and all_blocks[next_idx]["type"] == "image":
                npatch(f"blocks/{all_blocks[next_idx]['id']}",
                       {"image": {"type": "external", "external": {"url": img_url}}})
                return
        append_blocks(page_id, [image_block(img_url)], ignore_errors=True)
    except Exception as e:
        print(f"  ⚠ 이미지 블록 업데이트 실패 (무시): {e}")
 
def safe_add_block(page_id, blk, label="블록"):
    """
    블록 하나를 안전하게 추가.
    page_id가 NOTION_PAGE_ID이면 ANCHOR_BLOCK_ID(마지막 paragraph)를
    첫 블록으로 patch하고, 이후는 page children에 append.
    """
    try:
        npost(f"blocks/{page_id}/children", {"children": [blk]})
        time.sleep(0.2)
        return True
    except Exception as e:
        print(f"  ⚠ {label} 추가 실패 (무시): {str(e)[:80]}")
        return False
 
# 앵커 블록 사용 여부 추적
_anchor_used = {"used": False}
 
def anchor_add_block(blk, label="블록"):
    """
    ANCHOR_BLOCK_ID(마지막 paragraph)를 첫 번째 새 블록으로 patch하고
    이후 블록들은 page에 append. child_database 다음 추가 문제 우회.
    """
    global _anchor_used
    try:
        if not _anchor_used["used"]:
            # 첫 블록: paragraph를 새 내용으로 교체
            btype = blk["type"]
            npatch(f"blocks/{ANCHOR_BLOCK_ID}", {btype: blk[btype]})
            _anchor_used["used"] = True
        else:
            # 이후 블록: page children에 append (paragraph 뒤에 자동으로 붙음)
            npost(f"blocks/{NOTION_PAGE_ID}/children", {"children": [blk]})
        time.sleep(0.2)
        return True
    except Exception as e:
        print(f"  ⚠ {label} 추가 실패 (무시): {str(e)[:80]}")
        return False
 
def anchor_create_table(col_count, has_header, header_cells, data_rows):
    """앵커 방식으로 테이블 생성"""
    global _anchor_used
    try:
        if not _anchor_used["used"]:
            # paragraph를 table로 교체 불가 (타입 변경 불가)
            # → paragraph 다음에 table 추가를 위해 먼저 paragraph를 divider로 교체
            npatch(f"blocks/{ANCHOR_BLOCK_ID}", {"paragraph": {"rich_text": []}})
            _anchor_used["used"] = True
            time.sleep(0.2)
        resp = npost(f"blocks/{NOTION_PAGE_ID}/children", {"children": [{
            "object": "block", "type": "table",
            "table": {
                "table_width": col_count,
                "has_column_header": has_header,
                "has_row_header": False,
            }
        }]})
        table_id = resp["results"][0]["id"]
        time.sleep(0.3)
        npost(f"blocks/{table_id}/children", {"children": [trow(header_cells)]})
        time.sleep(0.2)
        for i in range(0, len(data_rows), 50):
            npost(f"blocks/{table_id}/children", {"children": data_rows[i:i+50]})
            time.sleep(0.2)
        return table_id
    except Exception as e:
        print(f"  ⚠ 테이블 생성 실패: {str(e)[:120]}")
        return None
 
def upsert_analysis_section(page_id, tables, all_blocks,
                             pie_file, bar_file, curve_file):
    sec = tables.get("수익 분석", {})
    if not sec.get("heading_id"):
        anchor_add_block(divider_block(), "divider")
        anchor_add_block(h2_block("📊 수익 분석"), "h2")
        if pie_file:
            anchor_add_block(h3_block("📊 분류별 비율"), "h3")
            anchor_add_block(image_block(raw_url(pie_file)), "파이차트 이미지")
        if bar_file:
            anchor_add_block(h3_block("📊 종목별 수익률 바 차트"), "h3")
            anchor_add_block(image_block(raw_url(bar_file)), "바차트 이미지")
        if curve_file:
            anchor_add_block(h3_block("📈 누적 총자산 변화 곡선"), "h3")
            anchor_add_block(image_block(raw_url(curve_file)), "곡선 이미지")
        print("  ✅ '수익 분석' 섹션 신규 생성")
        return
    if pie_file:
        upsert_image_after_h3(page_id, all_blocks,
                              sec.get("pie_h3"),   raw_url(pie_file))
    if bar_file:
        upsert_image_after_h3(page_id, all_blocks,
                              sec.get("bar_h3"),   raw_url(bar_file))
    if curve_file:
        upsert_image_after_h3(page_id, all_blocks,
                              sec.get("curve_h3"), raw_url(curve_file))
    print("  ✅ '수익 분석' 차트 업데이트")
 
def upsert_hold_tracker(page_id, tables, holdings):
    sec = tables.get("보유기간 트래커", {})
    header = ["종목명","티커","분류","최초매수일","보유일수","매입가","현재가","수익률"]
    if not sec.get("heading_id"):
        # 앵커 블록 방식으로 추가
        anchor_add_block(divider_block(), "divider")
        anchor_add_block(h2_block("⏱ 보유기간 트래커"), "h2")
        anchor_add_block(para_block("종목별 최초 매수일 기준 보유일수 (매일 자동 갱신)", color="gray"), "para")
        # 날짜 형식 통일 (YYYYMMDD → YYYY-MM-DD)
        def fmt_date(d):
            d = str(d)
            return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
        data_rows = [trow([
            str(h["name"]), str(h["ticker"]), str(h["category"]),
            fmt_date(h["first_buy_date"]),
            f"{int(h['hold_days'])}일",
            f"{float(h['avg_price']):,.0f}원",
            f"{float(h.get('current_price', h['avg_price'])):,.0f}원",
            f"{'📈' if float(h['profit_rate'])>=0 else '📉'} {float(h['profit_rate']):+.2f}%",
        ]) for h in holdings]
        anchor_create_table(8, True, header, data_rows)
        print("  ✅ '보유기간 트래커' 신규 생성")
        return
    tid = sec.get("table_id")
    if not tid: return
    existing = get_table_rows(tid)[1:]
    for i, h in enumerate(holdings):
        cells = [h["name"], h["ticker"], h["category"],
                 h["first_buy_date"], f"{h['hold_days']}일",
                 f"{h['avg_price']:,.0f}원",
                 f"{h.get('current_price', h['avg_price']):,.0f}원",
                 f"{'📈' if h['profit_rate']>=0 else '📉'} {h['profit_rate']:+.2f}%"]
        if i < len(existing): update_row(existing[i]["id"], cells)
        else:                  append_row(tid, cells)
        time.sleep(0.15)
    print(f"  ✅ 보유기간 트래커 업데이트")
 
def upsert_index_section(page_id, tables, all_blocks,
                         judgements, months_sorted, chart_file):
    sec       = tables.get("지수기반 종목분석", {})
    chart_url = raw_url(chart_file)
    j_header  = ["종목명","티커","기준지수","종목누적(%)","지수누적(%)","차이(%)","판정"]
 
    if not sec.get("heading_id"):
        j_rows = [trow([j["name"],j["ticker"],j["index"],
                        str(j["종목누적(%)"]),str(j["지수누적(%)"]),
                        str(j["차이(%)"]),j["판정"]]) for j in judgements]
        anchor_add_block(divider_block(), "divider")
        anchor_add_block(h2_block("📈 지수기반 종목분석"), "h2")
        anchor_add_block(h3_block("📋 지수 분석 대상 및 판정"), "h3")
        anchor_create_table(7, True, j_header, j_rows)
        time.sleep(0.3)
        anchor_add_block(para_block("※ 6개월 누적 수익률이 기준지수 대비 -10%p 이하 → 손절 검토", color="gray"), "para")
        anchor_add_block(h3_block("📉 기준지수 대비 월별 수익률 비교 차트"), "h3")
        anchor_add_block(image_block(chart_url), "지수비교 이미지")
        print("  ✅ '지수기반 종목분석' 섹션 신규 생성")
        return
 
    tid = sec.get("table_id")
    if tid:
        existing = get_table_rows(tid)[1:]
        for i, j in enumerate(judgements):
            cells = [j["name"],j["ticker"],j["index"],
                     str(j["종목누적(%)"]),str(j["지수누적(%)"]),
                     str(j["차이(%)"]),j["판정"]]
            if i < len(existing): update_row(existing[i]["id"], cells)
            else:                  append_row(tid, cells)
            time.sleep(0.15)
    upsert_image_after_h3(page_id, all_blocks,
                          sec.get("chart_h3"), chart_url)
    print("  ✅ '지수기반 종목분석' 업데이트")
 
# ─────────────────────────────────────────────
# 날짜 paragraph 업데이트
# ─────────────────────────────────────────────
def update_date_paragraph(tables):
    pid = tables.get("paragraph_date")
    if not pid: return
    npatch(f"blocks/{pid}", {
        "paragraph": {"rich_text": [
            rt(f"작성일자: {TODAY}　　　　🔄 새로고침")
        ]}
    })
    print("  ✅ 작성일자 업데이트")
 
# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
# 중간 상태 저장 파일
STATE_FILE = DATA_DIR / "run_state.json"
 
def save_state(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
 
def load_state():
    if not STATE_FILE.exists():
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)
 
def main():
    run_mode = os.environ.get("RUN_MODE", "all")  # charts_only / notion_only / all
    print(f"\n{'='*55}")
    print(f"  SWING Portfolio v3  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  모드: {run_mode}")
    print(f"{'='*55}\n")
 
    if run_mode in ("charts_only", "all"):
        # ── 1. 노션 매매일지 DB에서 데이터 읽기
        print("[1] 노션 매매일지 DB 읽기...")
        trades   = load_trades_from_notion()
        holdings = aggregate_holdings(trades)
        print(f"  보유종목 {len(holdings)}개\n")
 
        # ── 2. 현재가 조회 & 수익 계산
        print("[2] 현재가 조회...")
        for h in holdings:
            price = get_current_price(h["ticker"], h["category"])
            h["current_price"] = price or h["avg_price"]
            h["eval_amount"]   = h["current_price"] * h["qty"]
            h["profit"]        = h["eval_amount"] - h["total_cost"]
            h["profit_rate"]   = h["profit"] / h["total_cost"] * 100 \
                                 if h["total_cost"] else 0
            print(f"  {h['name']}: {h['current_price']:,.0f}원  "
                  f"{'📈' if h['profit_rate']>=0 else '📉'}{h['profit_rate']:+.2f}%")
            time.sleep(0.3)
 
        total_eval   = sum(h["eval_amount"] for h in holdings)
        total_cost   = sum(h["total_cost"]  for h in holdings)
        total_profit = total_eval - total_cost
        total_rate   = total_profit / total_cost * 100 if total_cost else 0
        print(f"\n  → 총평가금액 {total_eval:,.0f}원  ({total_rate:+.2f}%)")
 
        # ── 3. 히스토리 저장
        history_rows = save_history(total_eval, total_profit, total_rate)
 
        # ── 4. 차트 생성
        print("\n[3] 차트 생성...")
        pie_file   = chart_pie(holdings)
        bar_file   = chart_bar_profit(holdings)
        curve_file = chart_history_curve(history_rows)
        idx_file, judgements, months_sorted = chart_index_comparison()
 
        # 중간 상태 저장 (notion_only 단계에서 사용)
        save_state({
            "trades":        trades,
            "holdings":      holdings,
            "total_eval":    total_eval,
            "total_profit":  total_profit,
            "total_rate":    total_rate,
            "pie_file":      pie_file,
            "bar_file":      bar_file,
            "curve_file":    curve_file,
            "idx_file":      idx_file,
            "judgements":    judgements,
            "months_sorted": months_sorted,
        })
 
        if run_mode == "charts_only":
            print("\n  ✅ 차트 생성 완료 — Notion 업데이트는 STEP 3에서 진행")
            return
 
    if run_mode in ("notion_only", "all"):
        # 저장된 상태 로드
        state = load_state()
        if not state:
            print("  ❌ 상태 파일 없음. charts_only 먼저 실행하세요.")
            return
        trades        = state["trades"]
        holdings      = state["holdings"]
        total_eval    = state["total_eval"]
        total_profit  = state["total_profit"]
        total_rate    = state["total_rate"]
        pie_file      = state["pie_file"]
        bar_file      = state["bar_file"]
        curve_file    = state["curve_file"]
        idx_file      = state["idx_file"]
        judgements    = state["judgements"]
        months_sorted = state["months_sorted"]
 
        # ── 5. Notion DB 업데이트
        print("\n[4] Notion DB 업데이트...")
        db_update_total_assets(total_eval, total_profit, total_rate)
        db_update_holdings(holdings)
        db_sync_trade_log(trades)
 
        # ── 6. Notion [table] 블록 업데이트
        print("\n[5] Notion 테이블 블록 업데이트...")
        tables, all_blocks = find_table_blocks(NOTION_PAGE_ID)
        update_date_paragraph(tables)
        update_table_total_assets(tables.get("총자산", {}),
                                  total_eval, total_profit, total_rate)
        update_table_holdings(tables.get("보유주식", {}), holdings)
        update_table_trade_log(tables.get("최근 매매일지", {}), trades)
 
        # ── 7. 차트 섹션 upsert
        print("\n[6] 차트·추가 섹션 업데이트...")
        upsert_analysis_section(NOTION_PAGE_ID, tables, all_blocks,
                                pie_file, bar_file, curve_file)
        upsert_hold_tracker(NOTION_PAGE_ID, tables, holdings)
        upsert_index_section(NOTION_PAGE_ID, tables, all_blocks,
                             judgements, months_sorted, idx_file)
 
    print(f"\n{'='*55}")
    print(f"  ✅ 완료!  총평가금액 {total_eval:,.0f}원  ({total_rate:+.2f}%)")
    print(f"{'='*55}\n")
 
 
if __name__ == "__main__":
    main()
 
