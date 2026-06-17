
"""
SWING Portfolio - GitHub Actions 자동 업데이트 스크립트 v4
노션 구조에 맞게 각 섹션에 직접 이미지/테이블 삽입
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
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "YOUR_USER/swing-portfolio")
GITHUB_BRANCH  = os.environ.get("GITHUB_BRANCH", "main")
 
# ── 노션 블록 ID 맵 (페이지 구조 고정)
BLK = {
    "paragraph_date":    "1526af4b407e4e1183580a1c4ef07f4f",  # 작성일자
    "callout":           "3824a83490b9806ca575f87ff1a085b2",  # 업데이트 안내
    # 총자산
    "h2_assets":         "160c3d2d50a042fbbcb3d7f8a692bb41",
    "table_assets":      "56cb07cfd067488eb7d422891980cb31",
    # 누적 총자산 곡선 (이미지 삽입 대상)
    "h2_curve":          "3824a83490b980a6982ccd045153b2f4",
    # 보유주식
    "h2_holdings":       "7c739e9d6b724ebca38fe841b7d151fd",
    "table_holdings":    "72e624bd0e4648bba8565fdd0e9d3eb6",
    # 분류별 비율 (이미지 삽입 대상)
    "h2_pie":            "3824a83490b9800eb4f7e2667ccd3632",
    # 보유 기간 트래커 (테이블 삽입 대상)
    "h2_tracker":        "3824a83490b98015994af35dfc0694ff",
    # 최근 매매일지
    "h2_trade":          "c5c4f7a3ce484c5f9104ebe9f7c820dc",
    "child_page_trade":  "3824a83490b98038b9b3e1be1364a884",  # 전체 매매일지 페이지
    "table_trade":       "d863a76f079a4de2a84c84b50b507d01",
    # 종목별 수익률 (이미지 삽입 대상)
    "h2_bar":            "3824a83490b98061 89edc645b72bb7fc".replace(" ", ""),
    # 수익 분석 섹션
    "h2_analysis":       "3824a83490b98021a720e1c62bacd064",
    # 지수기반 종목 분석
    "h2_index":          "3824a83490b980e5a50eecee731fb9a9",
    # 이미지 블록 ID (노션에 미리 생성된 placeholder 이미지)
    "img_curve":         "3824a83490b980b1a217c23db23bc736",  # 누적 총자산 곡선
    "img_pie":           "3824a83490b980d78bffdf746d61323b",  # 분류별 비율
    "img_bar":           "3824a83490b9808b8c8ac12ea8618fc1",  # 종목별 수익률
    "img_index":         "3824a83490b9809eb511d0813daa9bed",  # 지수비교 차트
    # DB
    "db_assets":         "f6aa484832ad463eb353fbbf8e69dc79",
    "db_holdings":       "756fd3fb2db2436f9fa2cb3aeb7bcf41",
    "db_trade":          "e2af5ad75d98427bab5c0f26bd0f7c47",
}
 
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}
# KST = UTC+9
KST_NOW   = datetime.utcnow() + __import__("datetime").timedelta(hours=9)
TODAY     = KST_NOW.strftime("%Y-%m-%d")
TODAY_KRX = KST_NOW.strftime("%Y%m%d")
NOW_STR   = KST_NOW.strftime("%Y-%m-%d %H:%M KST")
CHARTS_DIR   = Path("charts")
DATA_DIR     = Path("data")
CHARTS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
HISTORY_FILE = DATA_DIR / "history.csv"
STATE_FILE   = DATA_DIR / "run_state.json"
 
for _n, _v in [("NOTION_PAGE_ID", NOTION_PAGE_ID),
               ("DB_TRADE", DB_TRADE),
               ("DB_HOLDINGS", DB_HOLDINGS),
               ("DB_ASSETS", DB_ASSETS)]:
    if len(_v) != 32:
        print(f"  ⚠ {_n} 길이 이상: {len(_v)}자")
    else:
        print(f"  ✅ {_n}: {_v[:8]}...{_v[-4:]}")
 
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
    return (f"https://raw.githubusercontent.com/{GITHUB_REPO}"
            f"/{GITHUB_BRANCH}/charts/{filename}?raw=true")
 
# ─────────────────────────────────────────────
# Notion API 유틸
# ─────────────────────────────────────────────
def nget(path):
    r = requests.get(f"https://api.notion.com/v1/{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()
 
def npatch(path, body):
    r = requests.patch(f"https://api.notion.com/v1/{path}",
                       headers=HEADERS,
                       data=json.dumps(body, ensure_ascii=False))
    if not r.ok:
        print(f"  ❌ PATCH {path} ({r.status_code}): {r.text[:200]}")
    r.raise_for_status()
    return r.json()
 
def npost(path, body):
    r = requests.post(f"https://api.notion.com/v1/{path}",
                      headers=HEADERS,
                      data=json.dumps(body, ensure_ascii=False))
    if not r.ok:
        print(f"  ❌ POST {path} ({r.status_code}): {r.text[:200]}")
    r.raise_for_status()
    return r.json()
 
def safe_post(path, body, label=""):
    try:
        return npost(path, body)
    except Exception as e:
        print(f"  ⚠ {label} 실패 (무시): {str(e)[:80]}")
        return None
 
# ─────────────────────────────────────────────
# 블록 헬퍼
# ─────────────────────────────────────────────
def rt(content, bold=False, color=None):
    obj = {"type":"text","text":{"content":str(content)},
           "annotations":{"bold":bold}}
    if color: obj["annotations"]["color"] = color
    return obj
 
def trow(cells):
    return {"object":"block","type":"table_row",
            "table_row":{"cells":[[rt(c)] for c in cells]}}
 
def image_block(url):
    return {"object":"block","type":"image",
            "image":{"type":"external","external":{"url":url}}}
 
def h3_block(text):
    return {"object":"block","type":"heading_3",
            "heading_3":{"rich_text":[rt(text,bold=True)]}}
 
def para_block(text, color=None):
    rich = [rt(text)]
    if color: rich[0]["annotations"]["color"] = color
    return {"object":"block","type":"paragraph",
            "paragraph":{"rich_text":rich}}
 
def divider_block():
    return {"object":"block","type":"divider","divider":{}}
 
def get_children(block_id):
    data = nget(f"blocks/{block_id}/children?page_size=100")
    return data.get("results", [])
 
def get_table_rows(tid):
    return nget(f"blocks/{tid}/children?page_size=100")["results"]
 
def update_row(row_id, cells):
    npatch(f"blocks/{row_id}",
           {"table_row":{"cells":[[rt(c)] for c in cells]}})
 
def append_row(tid, cells):
    npost(f"blocks/{tid}/children", {"children":[trow(cells)]})
 
def append_one(parent_id, blk, label=""):
    """블록 하나 추가. 실패 무시."""
    return safe_post(f"blocks/{parent_id}/children",
                     {"children":[blk]}, label)
 
def update_image_block(img_block_id, img_url, label="이미지"):
    """이미지 블록 ID로 직접 URL 교체"""
    try:
        npatch(f"blocks/{img_block_id}",
               {"image":{"type":"external","external":{"url":img_url}}})
        print(f"  ✅ {label} 이미지 업데이트")
    except Exception as e:
        print(f"  ⚠ {label} 이미지 업데이트 실패: {e}")
 
def upsert_table(parent_id, col_count, has_header,
                  header_cells, data_rows, label="테이블"):
    """
    parent_id 아래 첫 번째 테이블을 찾아 업데이트.
    없으면 parent_id 자식으로 새로 생성.
    parent_id: heading_2 블록 ID 또는 페이지 ID
    """
    children = get_children(parent_id)
    table_id = None
    for b in children:
        if b["type"] == "table":
            table_id = b["id"].replace("-","")
            break
 
    if table_id:
        existing = get_table_rows(table_id)[1:]
        for i, row in enumerate(data_rows):
            if isinstance(row, dict) and "table_row" in row:
                cells = ["".join(t.get("plain_text","") for t in cell)
                         for cell in row["table_row"]["cells"]]
            else:
                cells = row
            if i < len(existing):
                update_row(existing[i]["id"], cells)
            else:
                append_row(table_id, cells)
            time.sleep(0.15)
        print(f"  ✅ {label} 테이블 업데이트")
        return table_id
 
    # 신규 생성
    resp = safe_post(f"blocks/{parent_id}/children", {"children":[{
        "object":"block","type":"table",
        "table":{"table_width":col_count,
                 "has_column_header":has_header,
                 "has_row_header":False}
    }]}, f"{label} 테이블 생성")
    if not resp: return None
    table_id = resp["results"][0]["id"].replace("-","")
    time.sleep(0.3)
    npost(f"blocks/{table_id}/children",{"children":[trow(header_cells)]})
    time.sleep(0.2)
    for i in range(0, len(data_rows), 50):
        batch = data_rows[i:i+50]
        rows_to_add = []
        for row in batch:
            if isinstance(row, dict) and "table_row" in row:
                rows_to_add.append(row)
            else:
                rows_to_add.append(trow(row))
        npost(f"blocks/{table_id}/children",{"children":rows_to_add})
        time.sleep(0.2)
    print(f"  ✅ {label} 테이블 신규 생성")
    return table_id
 
# ─────────────────────────────────────────────
# DB 프로퍼티 헬퍼
# ─────────────────────────────────────────────
def prop_title(t):   return {"title":[{"text":{"content":str(t)}}]}
def prop_rt(t):      return {"rich_text":[{"text":{"content":str(t)}}]}
def prop_num(v):     return {"number": float(v) if v is not None else None}
def prop_sel(n):     return {"select":{"name":str(n)}}
def prop_date(d):
    d = str(d)
    if len(d)==8: d=f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return {"date":{"start":d}}
 
def get_text(prop):
    pt = prop.get("type","")
    items = prop.get(pt,[]) if pt in ("title","rich_text") else []
    return "".join(t.get("plain_text","") for t in items)
 
def get_num(prop):   return prop.get("number")
def get_sel(prop):
    s = prop.get("select")
    return s["name"] if s else ""
def get_date_val(prop):
    d = prop.get("date")
    return d["start"] if d else ""
 
# ─────────────────────────────────────────────
# 노션 매매일지 DB → 거래 데이터 로드
# ─────────────────────────────────────────────
def load_trades_from_notion():
    trades, cursor = [], None
    while True:
        body = {"page_size":100,
                "sorts":[{"property":"날짜","direction":"ascending"}]}
        if cursor: body["start_cursor"] = cursor
        data = npost(f"databases/{DB_TRADE}/query", body)
        for page in data["results"]:
            p = page["properties"]
            d = get_date_val(p.get("날짜",{})).replace("-","")
            trades.append({
                "date":     d,
                "name":     get_text(p.get("종목이름",{})),
                "ticker":   get_text(p.get("티커",{})),
                "type":     get_sel(p.get("매수매도",{})),
                "qty":      int(get_num(p.get("수량",{})) or 0),
                "price":    int(get_num(p.get("단가",{})) or 0),
                "category": get_sel(p.get("분류",{})),
                "reason":   get_text(p.get("사유",{})),
            })
        if not data.get("has_more"): break
        cursor = data["next_cursor"]
    print(f"  ✅ 매매일지 {len(trades)}건 로드")
    return trades
 
# ─────────────────────────────────────────────
# 보유주식 집계
# ─────────────────────────────────────────────
def aggregate_holdings(trades):
    holdings = {}
    for t in sorted(trades, key=lambda x: x["date"]):
        if not t["ticker"] or not t["qty"]: continue
        tk = t["ticker"]
        if tk not in holdings:
            holdings[tk] = {"name":t["name"],"ticker":tk,
                            "qty":0,"total_cost":0,
                            "category":t["category"],
                            "first_buy_date":t["date"]}
        if t["type"]=="매수":
            holdings[tk]["qty"]        += t["qty"]
            holdings[tk]["total_cost"] += t["qty"]*t["price"]
        elif t["type"]=="매도" and holdings[tk]["qty"]>0:
            avg = holdings[tk]["total_cost"]/holdings[tk]["qty"]
            holdings[tk]["qty"]        -= t["qty"]
            holdings[tk]["total_cost"] -= t["qty"]*avg
    result = []
    for h in holdings.values():
        if h["qty"]>0:
            h["avg_price"] = h["total_cost"]/h["qty"]
            bd = str(h["first_buy_date"])
            bd_dt = datetime.strptime(bd,"%Y%m%d") if len(bd)==8 \
                    else datetime.strptime(bd,"%Y-%m-%d")
            h["hold_days"] = (datetime.today()-bd_dt).days
            result.append(h)
    return result
 
# ─────────────────────────────────────────────
# 현재가 조회
# ─────────────────────────────────────────────
def get_krx_price(ticker):
    for delta in range(5):
        date = (datetime.today()-timedelta(days=delta)).strftime("%Y%m%d")
        try:
            df = krx.get_market_ohlcv_by_date(
                fromdate=date,todate=date,ticker=ticker)
            if not df.empty: return int(df.iloc[-1]["종가"])
        except Exception: pass
    return None
 
def get_price(ticker, category):
    try:
        if category in ("국내종목","국내ETF","국내ETF-해외"):
            return get_krx_price(ticker)
        else:
            return yf.Ticker(ticker).fast_info["last_price"]
    except Exception as e:
        print(f"    ⚠ {ticker} 조회 실패: {e}")
        return None
 
# ─────────────────────────────────────────────
# 히스토리 저장
# ─────────────────────────────────────────────
def save_history(total_eval, total_profit, total_rate):
    import csv
    rows = []
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE,newline="",encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    rows = [r for r in rows if r["date"]!=TODAY]
    rows.append({"date":TODAY,"total_eval":round(total_eval),
                 "total_profit":round(total_profit),
                 "total_rate":round(total_rate,4)})
    with open(HISTORY_FILE,"w",newline="",encoding="utf-8") as f:
        import csv as csv2
        w = csv2.DictWriter(f,fieldnames=["date","total_eval","total_profit","total_rate"])
        w.writeheader(); w.writerows(rows)
    print(f"  ✅ 히스토리 {len(rows)}일치 저장")
    return rows
 
def save_state(data):
    with open(STATE_FILE,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False)
 
def load_state():
    if not STATE_FILE.exists(): return None
    with open(STATE_FILE,encoding="utf-8") as f:
        return json.load(f)
 
# ─────────────────────────────────────────────
# 차트 ① 분류별 파이차트
# ─────────────────────────────────────────────
def chart_pie(holdings):
    cat_amt = {}
    for h in holdings:
        cat_amt[h["category"]] = cat_amt.get(h["category"],0)+h["eval_amount"]
    if not cat_amt: return None
    labels = list(cat_amt.keys())
    sizes  = list(cat_amt.values())
    colors = ["#4E79A7","#F28E2B","#59A14F","#E15759","#76B7B2","#EDC948"]
    total  = sum(sizes)
    fig,ax = plt.subplots(figsize=(8,6),facecolor="white")
    _,texts,autotexts = ax.pie(
        sizes,labels=labels,autopct="%1.1f%%",startangle=90,
        colors=colors[:len(labels)],pctdistance=0.82,
        wedgeprops={"linewidth":1.5,"edgecolor":"white"})
    for t in texts:    t.set_fontsize(12)
    for at in autotexts:
        at.set_fontsize(11); at.set_fontweight("bold"); at.set_color("white")
    ax.set_title(f"보유주식 분류별 비중\n총평가금액 {total:,.0f}원",
                 fontsize=14,fontweight="bold",pad=20)
    ax.legend([f"{l}  {s:,.0f}원 ({s/total*100:.1f}%)"
               for l,s in zip(labels,sizes)],
              loc="lower center",bbox_to_anchor=(0.5,-0.18),ncol=2,fontsize=10)
    ax.text(0.99,0.01,f"기준: {TODAY}",transform=ax.transAxes,
            fontsize=9,color="gray",ha="right",va="bottom")
    plt.tight_layout()
    path = CHARTS_DIR/"pie_category.png"
    fig.savefig(path,dpi=150,bbox_inches="tight",facecolor="white")
    plt.close(fig)
    print(f"  ✅ 파이차트 저장")
    return "pie_category.png"
 
# ─────────────────────────────────────────────
# 차트 ② 종목별 수익률 바 차트
# ─────────────────────────────────────────────
def chart_bar(holdings):
    if not holdings: return None
    names  = [h["name"] for h in holdings]
    rates  = [h["profit_rate"] for h in holdings]
    colors = ["#1f77b4" if r>=0 else "#d62728" for r in rates]
    fig,ax = plt.subplots(figsize=(max(8,len(names)*1.2),6),facecolor="white")
    bars = ax.bar(names,rates,color=colors,width=0.6,
                  edgecolor="white",linewidth=0.8)
    for bar,rate in zip(bars,rates):
        emoji = "📈" if rate>=0 else "📉"
        ypos  = bar.get_height()+(0.3 if rate>=0 else -0.3)
        ax.text(bar.get_x()+bar.get_width()/2, ypos,
                f"{emoji} {rate:+.2f}%",
                ha="center",va="bottom" if rate>=0 else "top",
                fontsize=10,fontweight="bold",
                color="#1f77b4" if rate>=0 else "#d62728")
    ax.axhline(0,color="black",linewidth=1.0)
    ymin,ymax = ax.get_ylim()
    ax.axhspan(0,ymax,alpha=0.04,color="#1f77b4")
    ax.axhspan(ymin,0,alpha=0.04,color="#d62728")
    ax.set_ylabel("수익률 (%)",fontsize=11)
    ax.set_title(f"종목별 수익률  (현재가 기준, {TODAY})",
                 fontsize=14,fontweight="bold",pad=15)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f%%"))
    ax.tick_params(axis="x",labelsize=10,rotation=20)
    ax.grid(axis="y",alpha=0.3,linestyle="--")
    plt.tight_layout()
    path = CHARTS_DIR/"bar_profit.png"
    fig.savefig(path,dpi=150,bbox_inches="tight",facecolor="white")
    plt.close(fig)
    print(f"  ✅ 바 차트 저장")
    return "bar_profit.png"
 
# ─────────────────────────────────────────────
# 차트 ③ 누적 총자산 곡선
# ─────────────────────────────────────────────
def chart_curve(history_rows):
    if len(history_rows)<2:
        print("  ⚠ 히스토리 2일치 이상 필요")
        return None
    dates = [r["date"]              for r in history_rows]
    evals = [int(r["total_eval"])   for r in history_rows]
    rates = [float(r["total_rate"]) for r in history_rows]
    fig,(ax1,ax2) = plt.subplots(2,1,figsize=(11,8),
                                  facecolor="white",sharex=True)
    fig.suptitle(f"포트폴리오 총자산 변화  (최근 {len(dates)}일)",
                 fontsize=14,fontweight="bold")
    ax1.plot(dates,evals,color="#1f77b4",linewidth=2.2,marker="o",markersize=5)
    ax1.fill_between(dates,evals,min(evals),alpha=0.12,color="#1f77b4")
    ax1.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda x,_: f"{x/10000:.0f}만원"))
    ax1.set_ylabel("총평가금액",fontsize=10)
    ax1.grid(alpha=0.3,linestyle="--")
    ax1.tick_params(axis="x",rotation=30,labelsize=8)
    bar_colors = ["#1f77b4" if r>=0 else "#d62728" for r in rates]
    ax2.bar(dates,rates,color=bar_colors,width=0.6,alpha=0.85)
    ax2.axhline(0,color="black",linewidth=0.8)
    ax2.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.2f%%"))
    ax2.set_ylabel("총수익률 (%)",fontsize=10)
    ax2.grid(axis="y",alpha=0.3,linestyle="--")
    ax2.tick_params(axis="x",rotation=30,labelsize=8)
    plt.tight_layout()
    path = CHARTS_DIR/"history_curve.png"
    fig.savefig(path,dpi=150,bbox_inches="tight",facecolor="white")
    plt.close(fig)
    print(f"  ✅ 누적 곡선 저장")
    return "history_curve.png"
 
# ─────────────────────────────────────────────
# 차트 ④ 지수 대비 월별 수익률 비교
# ─────────────────────────────────────────────
INDEX_TICKERS = {"S&P500":"^GSPC","나스닥100":"^NDX"}
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
 
def monthly_returns_yf(ticker, months=6):
    end   = datetime.today()
    start = (end-timedelta(days=months*31)).replace(day=1)
    df = yf.download(ticker,start=start,end=end,
                     interval="1mo",progress=False,auto_adjust=True)
    if df.empty: return {}
    c = df["Close"].dropna()
    return {c.index[i+1].strftime("%Y-%m"):
            round(float((c.iloc[i+1]/c.iloc[i]-1)*100),2)
            for i in range(len(c)-1)}
 
def monthly_returns_krx(ticker, months=6):
    end   = datetime.today()
    start = end-timedelta(days=months*31+10)
    try:
        df = krx.get_market_ohlcv_by_date(
            fromdate=start.strftime("%Y%m%d"),
            todate=end.strftime("%Y%m%d"),
            ticker=ticker, freq="m")
        if df.empty: return {}
        c = df["종가"]
        return {c.index[i+1].strftime("%Y-%m"):
                round(float((c.iloc[i+1]/c.iloc[i]-1)*100),2)
                for i in range(len(c)-1)}
    except Exception: return {}
 
def chart_index_comparison():
    print("  기준지수 조회 중...")
    index_returns = {}
    for idx_name,idx_ticker in INDEX_TICKERS.items():
        index_returns[idx_name] = monthly_returns_yf(idx_ticker,months=6)
        time.sleep(0.5)
    # 코스피200: KODEX200 pykrx
    index_returns["코스피200"] = monthly_returns_krx("069500",months=6)
    time.sleep(0.3)
 
    all_months = set()
    for r in index_returns.values(): all_months.update(r.keys())
    months_sorted = sorted(all_months)[-6:]
 
    stock_data, judgements = [], []
    print("  관심종목 조회 중...")
    for item in WATCHLIST:
        is_domestic = item["ticker"].isdigit() and len(item["ticker"])==6
        ret = monthly_returns_krx(item["ticker"]) if is_domestic \
              else monthly_returns_yf(item["ticker"])
        time.sleep(0.3)
 
        idx_ret    = index_returns.get(item["index"],{})
        stock_cum  = sum(ret.get(m,0) for m in months_sorted)
        idx_cum    = sum(idx_ret.get(m,0) for m in months_sorted)
        alpha      = stock_cum - idx_cum
 
        # 판정 기준
        if alpha >= 10:   judgement = "📈 매수 확대"
        elif alpha <= -10: judgement = "⚠️ 손절 검토"
        else:              judgement = "✅ 유지"
 
        judgements.append({
            "name":   item["name"],
            "ticker": item["ticker"],
            "index":  item["index"],
            "stock_cum": round(stock_cum,2),
            "idx_cum":   round(idx_cum,2),
            "alpha":     round(alpha,2),
            "judgement": judgement,
        })
        stock_data.append({**item,"returns":ret})
        print(f"    {item['name']}: {stock_cum:+.1f}%  "
              f"지수 {idx_cum:+.1f}%  α={alpha:+.1f}%  {judgement}")
 
    # 차트
    index_groups = {}
    for sd in stock_data:
        index_groups.setdefault(sd["index"],[]).append(sd)
    n = len(index_groups)
    fig,axes = plt.subplots(n,1,figsize=(13,4.5*n),facecolor="white")
    if n==1: axes=[axes]
    colors_s = ["#4E79A7","#F28E2B","#59A14F","#E15759",
                "#76B7B2","#EDC948","#B07AA1","#FF9DA7"]
    for ax,(idx_name,stocks) in zip(axes,index_groups.items()):
        idx_ret = index_returns.get(idx_name,{})
        ax.plot(months_sorted,[idx_ret.get(m) for m in months_sorted],
                color="black",linewidth=2.5,linestyle="--",
                marker="D",markersize=6,label=f"[지수] {idx_name}",zorder=5)
        for i,sd in enumerate(stocks):
            ax.plot(months_sorted,[sd["returns"].get(m) for m in months_sorted],
                    color=colors_s[i%len(colors_s)],linewidth=1.8,
                    marker="o",markersize=5,label=sd["name"],alpha=0.85)
        ax.axhline(0,color="gray",linewidth=0.8,alpha=0.5)
        ax.axhline(-10,color="#E15759",linewidth=0.8,linestyle=":",alpha=0.6)
        ax.axhline(10,color="#1f77b4",linewidth=0.8,linestyle=":",alpha=0.6)
        ax.text(months_sorted[-1],-10.5,"-10% (손절 검토)",
                fontsize=8,color="#E15759",ha="right")
        ax.text(months_sorted[-1],10.5,"+10% (매수 확대)",
                fontsize=8,color="#1f77b4",ha="right")
        ax.set_title(f"{idx_name} 기준 — 6개월 월별 수익률 비교",
                     fontsize=13,fontweight="bold",pad=10)
        ax.set_ylabel("월별 수익률 (%)",fontsize=10)
        ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f%%"))
        ax.legend(loc="upper left",fontsize=9,framealpha=0.7,
                  ncol=2 if len(stocks)>4 else 1)
        ax.grid(axis="y",alpha=0.3)
    fig.suptitle(f"관심종목 × 기준지수 수익률 비교 ({TODAY})",
                 fontsize=15,fontweight="bold",y=1.01)
    plt.tight_layout()
    path = CHARTS_DIR/"index_comparison.png"
    fig.savefig(path,dpi=150,bbox_inches="tight",facecolor="white")
    plt.close(fig)
    print(f"  ✅ 지수비교 차트 저장")
    return "index_comparison.png", judgements, months_sorted
 
# ─────────────────────────────────────────────
# DB 업데이트 함수
# ─────────────────────────────────────────────
def db_pages(db_id):
    pages, cursor = [], None
    while True:
        body = {"page_size":100}
        if cursor: body["start_cursor"] = cursor
        data = npost(f"databases/{db_id}/query", body)
        pages.extend(data["results"])
        if not data.get("has_more"): break
        cursor = data["next_cursor"]
    return pages
 
def db_update_assets(total_eval, total_profit, total_rate):
    pages = db_pages(DB_ASSETS)
    today_page = next(
        (p for p in pages if get_text(p["properties"].get("작성일자",{}))==TODAY),
        None)
    props = {"작성일자":prop_title(TODAY),
             "총평가금액":prop_num(total_eval),
             "총수익":prop_num(total_profit),
             "총수익률":prop_rt(f"{total_rate:+.2f}%")}
    if today_page:
        npatch(f"pages/{today_page['id']}",{"properties":props})
    else:
        npost("pages",{"parent":{"database_id":DB_ASSETS},"properties":props})
    print("  ✅ 총자산 DB 업데이트")
 
def db_update_holdings(holdings):
    existing = db_pages(DB_HOLDINGS)
    for p in existing:
        npatch(f"pages/{p['id']}",{"archived":True})
        time.sleep(0.1)
    for h in holdings:
        emoji = "📈" if h["profit_rate"]>=0 else "📉"
        props = {
            "종목이름": prop_title(h["name"]),
            "티커":     prop_rt(h["ticker"]),
            "평가금액": prop_num(h["eval_amount"]),
            "수익":     prop_num(h["profit"]),
            "수익률":   prop_rt(f"{emoji} {h['profit_rate']:+.2f}%"),
            "보유수량": prop_num(h["qty"]),
            "매입가":   prop_num(h["avg_price"]),
            "분류":     prop_sel(h["category"]),
        }
        npost("pages",{"parent":{"database_id":DB_HOLDINGS},"properties":props})
        time.sleep(0.2)
    print(f"  ✅ 보유주식 DB 업데이트 ({len(holdings)}종목)")
 
def db_sync_trades(trades):
    existing = db_pages(DB_TRADE)
    exist_keys = set()
    for p in existing:
        pr = p["properties"]
        d  = get_date_val(pr.get("날짜",{}))
        tk = get_text(pr.get("티커",{}))
        tp = get_sel(pr.get("매수매도",{}))
        exist_keys.add(f"{d}_{tk}_{tp}")
    added = 0
    for t in trades:
        d = str(t["date"])
        if len(d)==8: d=f"{d[:4]}-{d[4:6]}-{d[6:]}"
        key = f"{d}_{t['ticker']}_{t['type']}"
        if key in exist_keys: continue
        props = {
            "종목이름": prop_title(t["name"]),
            "티커":     prop_rt(t["ticker"]),
            "날짜":     prop_date(t["date"]),
            "매수매도": prop_sel(t["type"]),
            "수량":     prop_num(t["qty"]),
            "단가":     prop_num(t["price"]),
            "분류":     prop_sel(t["category"]),
            "사유":     prop_rt(t["reason"]),
        }
        npost("pages",{"parent":{"database_id":DB_TRADE},"properties":props})
        exist_keys.add(key)
        added += 1
        time.sleep(0.2)
    print(f"  ✅ 매매일지 DB 동기화 ({added}건 추가)")
 
# ─────────────────────────────────────────────
# Notion 테이블 블록 업데이트
# ─────────────────────────────────────────────
def update_table_block(table_id, data_rows_cells, start_idx=1):
    """테이블 블록 행 업데이트 (start_idx=1: 헤더 제외)"""
    existing = get_table_rows(table_id)[start_idx:]
    for i, cells in enumerate(data_rows_cells):
        if i < len(existing):
            update_row(existing[i]["id"], cells)
        else:
            append_row(table_id, cells)
        time.sleep(0.15)
 
def update_date_para():
    npatch(f"blocks/{BLK['paragraph_date']}",
           {"paragraph":{"rich_text":[rt(f"🕐 마지막 업데이트: {NOW_STR}")]}})
    print(f"  ✅ 업데이트 시간: {NOW_STR}")
 
# ─────────────────────────────────────────────
# 전체 매매일지 child_page 동기화
# ─────────────────────────────────────────────
def sync_child_page_trade(trades):
    """
    전체 매매일지 child_page 안의 테이블을 매매일지 DB와 동일하게 동기화.
    페이지 안에 테이블이 없으면 새로 생성.
    """
    page_id = BLK["child_page_trade"].replace("-","")
    children = get_children(page_id)
 
    table_id = None
    for b in children:
        if b["type"] == "table":
            table_id = b["id"].replace("-","")
            break
 
    headers = ["날짜","종목이름","티커","매수/매도","수량","단가","분류","사유"]
    sorted_trades = sorted(trades, key=lambda x: x["date"], reverse=True)
 
    def fmt_date(d):
        d = str(d)
        return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d)==8 else d
 
    data_rows = [[
        fmt_date(t["date"]), t["name"], t["ticker"], t["type"],
        str(t["qty"]), f"{t['price']:,}원", t["category"], t["reason"]
    ] for t in sorted_trades]
 
    if not table_id:
        # 새로 생성
        resp = safe_post(f"blocks/{page_id}/children",
                         {"children":[{
                             "object":"block","type":"table",
                             "table":{
                                 "table_width":8,
                                 "has_column_header":True,
                                 "has_row_header":False,
                             }
                         }]}, "전체 매매일지 테이블 생성")
        if not resp: return
        table_id = resp["results"][0]["id"].replace("-","")
        time.sleep(0.3)
        npost(f"blocks/{table_id}/children",{"children":[trow(headers)]})
        time.sleep(0.2)
        for i in range(0,len(data_rows),50):
            npost(f"blocks/{table_id}/children",
                  {"children":[trow(r) for r in data_rows[i:i+50]]})
            time.sleep(0.2)
        print(f"  ✅ 전체 매매일지 테이블 신규 생성 ({len(data_rows)}건)")
    else:
        # 기존 테이블 업데이트
        update_table_block(table_id, data_rows)
        print(f"  ✅ 전체 매매일지 테이블 업데이트 ({len(data_rows)}건)")
 
# ─────────────────────────────────────────────
# 지수기반 종목분석 테이블 업데이트
# ─────────────────────────────────────────────
def update_index_table(judgements):
    """h2_index 자식으로 관심종목 판정 테이블 생성/업데이트"""
    headers = ["종목명","기준지수","6개월수익률","기준지수수익률","알파값(α)","판정"]
    data_rows = [
        [j["name"], j["index"],
         f"{j['stock_cum']:+.2f}%",
         f"{j['idx_cum']:+.2f}%",
         f"{j['alpha']:+.2f}%",
         j["judgement"]]
        for j in judgements
    ]
    upsert_table(BLK["h2_index"], 6, True, headers, data_rows, "지수기반 종목분석")
 
# ─────────────────────────────────────────────
# 보유기간 트래커 테이블 업데이트
# ─────────────────────────────────────────────
def update_tracker_table(holdings):
    """h2_tracker 자식으로 보유기간 트래커 테이블 생성/업데이트"""
    headers = ["종목명","티커","분류","최초매수일","보유일수","매입가","현재가","수익률"]
    def fmt_date(d):
        d=str(d)
        return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d)==8 else d
    data_rows = [
        [h["name"], h["ticker"], h["category"],
         fmt_date(h["first_buy_date"]),
         f"{int(h['hold_days'])}일",
         f"{float(h['avg_price']):,.0f}원",
         f"{float(h.get('current_price',h['avg_price'])):,.0f}원",
         f"{'📈' if float(h['profit_rate'])>=0 else '📉'} {float(h['profit_rate']):+.2f}%"]
        for h in holdings
    ]
    upsert_table(BLK["h2_tracker"], 8, True, headers, data_rows, "보유기간 트래커")
 
# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    run_mode = os.environ.get("RUN_MODE","all")
    print(f"\n{'='*55}")
    print(f"  SWING Portfolio v4  {NOW_STR}")
    print(f"  모드: {run_mode}")
    print(f"{'='*55}\n")
 
    if run_mode in ("charts_only","all"):
        print("[1] 매매일지 DB 읽기...")
        trades   = load_trades_from_notion()
        holdings = aggregate_holdings(trades)
        print(f"  보유종목 {len(holdings)}개\n")
 
        print("[2] 현재가 조회...")
        for h in holdings:
            price = get_price(h["ticker"],h["category"])
            h["current_price"] = price or h["avg_price"]
            h["eval_amount"]   = h["current_price"]*h["qty"]
            h["profit"]        = h["eval_amount"]-h["total_cost"]
            h["profit_rate"]   = h["profit"]/h["total_cost"]*100 \
                                 if h["total_cost"] else 0
            emoji = "📈" if h["profit_rate"]>=0 else "📉"
            print(f"  {h['name']}: {h['current_price']:,.0f}원  "
                  f"{emoji}{h['profit_rate']:+.2f}%")
            time.sleep(0.3)
 
        total_eval   = sum(h["eval_amount"] for h in holdings)
        total_cost   = sum(h["total_cost"]  for h in holdings)
        total_profit = total_eval-total_cost
        total_rate   = total_profit/total_cost*100 if total_cost else 0
        print(f"\n  → 총평가금액 {total_eval:,.0f}원  ({total_rate:+.2f}%)")
 
        history_rows = save_history(total_eval,total_profit,total_rate)
 
        print("\n[3] 차트 생성...")
        pie_file   = chart_pie(holdings)
        bar_file   = chart_bar(holdings)
        curve_file = chart_curve(history_rows)
        idx_file, judgements, months_sorted = chart_index_comparison()
 
        save_state({
            "trades":trades,"holdings":holdings,
            "total_eval":total_eval,"total_profit":total_profit,
            "total_rate":total_rate,
            "pie_file":pie_file,"bar_file":bar_file,
            "curve_file":curve_file,"idx_file":idx_file,
            "judgements":judgements,"months_sorted":months_sorted,
        })
        if run_mode=="charts_only":
            print("\n  ✅ 차트 생성 완료")
            return
 
    if run_mode in ("notion_only","all"):
        state = load_state()
        if not state:
            print("  ❌ 상태 파일 없음"); return
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
 
        # ── DB 업데이트
        print("\n[4] Notion DB 업데이트...")
        db_update_assets(total_eval,total_profit,total_rate)
        db_update_holdings(holdings)
        db_sync_trades(trades)
 
        # ── 테이블 블록 업데이트
        print("\n[5] Notion 테이블 업데이트...")
        update_date_para()
 
        # 총자산 테이블
        emoji = "📈" if total_rate>=0 else "📉"
        update_table_block(BLK["table_assets"].replace("-",""), [
            [TODAY, f"{total_eval:,.0f}원",
             f"{total_profit:+,.0f}원", f"{emoji} {total_rate:+.2f}%"]
        ])
        print("  ✅ 총자산 테이블 업데이트")
 
        # 보유주식 테이블
        holdings_rows = []
        all_rows = get_table_rows(BLK["table_holdings"].replace("-",""))
        header_texts = ["".join(t.get("plain_text","") for t in cell)
                        for cell in all_rows[0]["table_row"]["cells"]] \
                       if all_rows else []
        has_cur = "현재가" in header_texts
        for h in holdings:
            em = "📈" if h["profit_rate"]>=0 else "📉"
            if has_cur:
                row = [h["name"],h["ticker"],
                       f"{h['eval_amount']:,.0f}원",
                       f"{h.get('current_price',h['avg_price']):,.0f}원",
                       f"{h['profit']:+,.0f}원",
                       f"{em} {h['profit_rate']:+.2f}%",
                       str(h["qty"]),
                       f"{h['avg_price']:,.0f}원",
                       h["category"]]
            else:
                row = [h["name"],h["ticker"],
                       f"{h['eval_amount']:,.0f}원",
                       f"{h['profit']:+,.0f}원",
                       f"{em} {h['profit_rate']:+.2f}%",
                       str(h["qty"]),
                       f"{h['avg_price']:,.0f}원",
                       h["category"]]
            col_count = len(header_texts) if header_texts else len(row)
            holdings_rows.append(row[:col_count])
        update_table_block(BLK["table_holdings"].replace("-",""), holdings_rows)
        print(f"  ✅ 보유주식 테이블 업데이트 ({len(holdings)}종목)")
 
        # 최근 매매일지 테이블
        if trades:
            latest_date = max(t["date"] for t in trades if t["date"])
            latest = [t for t in trades if t["date"]==latest_date]
            def fmt(d):
                d=str(d)
                return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d)==8 else d
            trade_rows = [[fmt(t["date"]),t["name"],t["ticker"],t["type"],
                           str(t["qty"]),f"{t['price']:,}원",
                           t["category"],t["reason"]] for t in latest]
            update_table_block(BLK["table_trade"].replace("-",""), trade_rows)
            # heading 날짜 업데이트
            npatch(f"blocks/{BLK['h2_trade']}",
                   {"heading_2":{"rich_text":[rt(
                       f"📝 최근 매매일지 ({fmt(latest_date)})",bold=True)]}})
            print(f"  ✅ 최근 매매일지 업데이트 ({latest_date})")
 
        # 전체 매매일지 child_page 동기화
        print("\n[6] 전체 매매일지 페이지 동기화...")
        sync_child_page_trade(trades)
 
        # ── 차트 이미지 업데이트 (미리 만들어둔 이미지 블록 URL 교체)
        print("\n[7] 차트 이미지 업데이트...")
        if curve_file:
            update_image_block(BLK["img_curve"], raw_url(curve_file), "누적 총자산 곡선")
        if pie_file:
            update_image_block(BLK["img_pie"], raw_url(pie_file), "분류별 비율")
        if bar_file:
            update_image_block(BLK["img_bar"], raw_url(bar_file), "종목별 수익률")
        if idx_file:
            update_image_block(BLK["img_index"], raw_url(idx_file), "지수비교 차트")
 
        # ── 수익 분석 섹션 안내 텍스트
        children = get_children(BLK["h2_analysis"])
        if not children:
            safe_post(f"blocks/{BLK['h2_analysis']}/children",
                      {"children":[para_block(
                          "📊 분류별 비율 · 종목별 수익률 · 누적 총자산 곡선을 위 섹션에서 확인하세요.",
                          color="gray")]}, "수익 분석 안내")
 
        # ── 지수기반 종목분석 테이블
        print("\n[8] 지수기반 종목분석 업데이트...")
        update_index_table(judgements)
 
        # ── 보유기간 트래커 테이블
        print("\n[9] 보유기간 트래커 업데이트...")
        update_tracker_table(holdings)
 
    print(f"\n{'='*55}")
    print(f"  ✅ 완료!  총평가금액 {total_eval:,.0f}원  ({total_rate:+.2f}%)")
    print(f"{'='*55}\n")
 
if __name__=="__main__":
    main()
 
