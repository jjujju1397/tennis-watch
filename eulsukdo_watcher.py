"""
부산 테니스장(을숙도, 사직 실내) 빈 자리/취소표 감시 -> Gmail 알림 + 현황 페이지
- 오늘 이후 일정만 확인 (을숙도: 이번 달+다음 달, 사직: 예약 가능 기간 전체)
- 자동 실행(5분마다): 새로 생긴 자리(취소/신규 오픈)가 있을 때만 메일
- 수동 실행(Run workflow): 현재 빈 자리 전체 + 진단 정보를 메일로 보냄
- 매 실행마다 site/index.html(현황 페이지)을 만든다
"""
import os, re, json, html, smtplib
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://saha-eulsukdogym.kr"
LOGIN_URL = f"{BASE}/login"
COURT_URL = f"{BASE}/html/?pCode=7"
SAJIK_URL = "https://www.sajiktennis.kr/sub/court_booking.php?pn=0501"
URLS = {"을숙도": COURT_URL, "사직": SAJIK_URL}
FULL_WORDS = ["예약완료", "마감", "불가"]   # 이 단어가 붙은 시간은 '찬 자리'
STATE = "seen.json"
STATE_VER = 3
SITE_DIR = "site"
KST = ZoneInfo("Asia/Seoul")
WINDOW_DAYS = 7   # 오늘부터 며칠 뒤까지가 실제 예약 가능 범위인지 (바꾸고 싶으면 이 숫자만 수정)
WD = "월화수목금토일"


# ---------- 공통 도우미 ----------
def norm_slot(t):
    """'6:00 ~ 8:00' -> '06:00~08:00'"""
    m = re.search(r"(\d{1,2})\s*:\s*(\d{2})\s*~\s*(\d{1,2})\s*:\s*(\d{2})", t)
    if not m:
        return re.sub(r"\s+", "", t)
    a, b, c, d = m.groups()
    return f"{int(a):02d}:{b}~{int(c):02d}:{d}"


def date_label(d):
    try:
        wd = WD[datetime.strptime(d, "%Y-%m-%d").weekday()]
        return f"{d[5:7]}/{d[8:10]}({wd})"
    except ValueError:
        return d


def in_window(date):
    """예약이 실제로 열리는 범위(오늘 ~ 오늘+WINDOW_DAYS일, 한국시간) 안인지"""
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return True
    today = datetime.now(KST).date()
    return 0 <= (d - today).days <= WINDOW_DAYS


def is_upcoming(date, slot):
    """오늘(한국시간) 이후 일정만. 오늘은 이미 시작한 시간대 제외."""
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return True
    now = datetime.now(KST)
    if d > now.date():
        return True
    if d < now.date():
        return False
    m = re.search(r"(\d{1,2})\D*(\d{2})\s*~", slot)
    if not m:
        return True
    return (int(m.group(1)), int(m.group(2))) > (now.hour, now.minute)


def court_sort(c):
    m = re.match(r"(\D*?)(\d+)", c)
    return (m.group(1), int(m.group(2))) if m else (c, 0)


def sort_keys(keys):
    # 키 = "시설 N번 코트|날짜|시간" -> 날짜, 시간, 코트 순
    return sorted(keys, key=lambda k: (k.split("|")[1], k.split("|")[2], court_sort(k.split("|")[0])))


def slot_line(k, tag=""):
    c, d, t = k.split("|")
    return f"• {date_label(d)} {t} · {c}" + (f" ({tag})" if tag else "")


def send_mail(subject, body):
    user = os.environ["GMAIL_USER"]
    to = [x.strip() for x in os.environ.get("MAIL_TO", user).split(",") if x.strip()]
    msg = EmailMessage()
    msg["From"], msg["To"] = user, ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, os.environ["GMAIL_APP_PW"])
        s.send_message(msg)


def alert_mail(new_tags, avail=()):
    """new_tags: {키: '취소' 또는 '신규 오픈'} -> 시설별로 따로 메일 발송"""
    by_fac = {}
    for k in new_tags:
        fac, _, court = k.split("|")[0].partition(" ")
        by_fac.setdefault(fac, []).append(k)
    for fac, keys in by_fac.items():
        ks = sort_keys(keys)
        c, d, t = ks[0].split("|")
        subject = f"[{fac}] {date_label(d)} {t.split('~')[0]} {c.partition(' ')[2]}"
        if len(ks) > 1:
            subject += f" 외 {len(ks) - 1}건"
        lines = "\n".join(
            slot_line(k.replace(f"{fac} ", "", 1), new_tags[k]) for k in ks)
        body = (f"{fac} 테니스장에 예약 가능한 자리가 새로 생겼어요.\n\n"
                f"{lines}\n\n지금 예약하기: {URLS.get(fac, '')}\n\n"
                "다른 사람이 먼저 예약할 수 있으니 서둘러 주세요.")
        cur = {k.replace(f"{fac} ", "", 1) for k in avail if k.startswith(fac + " ")}
        body += (f"\n\n──────────\n현재 비어 있는 자리 전체 ({fac}, 총 {len(cur)}개)\n\n"
                 + (fmt(cur) or "없음"))
        send_mail(subject, body)


def fmt(keys):
    """날짜별로 묶어서 보여주기 (수동 실행 메일용)"""
    by_date = {}
    for k in sort_keys(keys):
        c, d, t = k.split("|")
        by_date.setdefault(d, {}).setdefault(c, []).append(t)
    out = []
    for d, courts in sorted(by_date.items()):
        out.append(f"■ {date_label(d)}")
        for c in sorted(courts, key=court_sort):
            out.append(f"   {c}: {', '.join(courts[c])}")
        out.append("")
    return "\n".join(out).strip()


# ---------- 사이트 읽기 ----------
def settle(page, ms=8000):
    """로딩이 끝날 때까지 최대 ms만 기다리고, 안 끝나도 그냥 진행한다."""
    try:
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass



def login(page, diag):
    msgs = []

    def on_dialog(d):
        msgs.append(d.message)
        d.accept()

    page.on("dialog", on_dialog)
    goto_retry(page, LOGIN_URL, 3)
    settle(page)
    pw = page.locator("input[type=password]:visible").first
    form = page.locator("form:has(input[type=password]:visible)").first
    idbox = form.locator(
        "input:visible:not([type=password]):not([type=hidden])"
        ":not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button])"
    ).first
    idbox.fill(os.environ["SITE_ID"])
    pw.fill(os.environ["SITE_PW"])
    pw.press("Enter")
    page.wait_for_timeout(3000)
    settle(page)
    ok = page.locator("input[type=password]:visible").count() == 0
    diag.append(f"로그인: {'성공' if ok else '실패(비밀번호 입력창이 아직 보임)'} / 주소 {page.url}")
    if msgs:
        diag.append("사이트 안내창 메시지: " + " / ".join(msgs))
    return ok


def find_courts(page, diag):
    goto_retry(page, COURT_URL, 3)
    settle(page)
    body = page.inner_text("body")
    nums = sorted(set(re.findall(r"(\d+)\s*번\s*코트", body)), key=int)
    courts = {f"{n}번 코트": "" for n in nums}
    diag.append(f"코트 {len(courts)}개: {list(courts)}")
    return courts


def open_court(page, cname):
    """코트 화면을 열고 (사용할 page, 새 창 여부)를 돌려준다."""
    n = cname[0]
    goto_retry(page, COURT_URL, 3)
    settle(page)
    link = page.locator("a", has_text=re.compile(rf"{n}\s*번\s*코트")).first
    try:
        with page.context.expect_page(timeout=4000) as pi:
            link.click()
        newp = pi.value
        newp.on("dialog", lambda d: d.accept())
        settle(newp)
        return newp, True
    except PWTimeout:
        settle(page)
        return page, False


def day_links(page):
    loc = page.locator("td a").filter(has_text=re.compile(r"^\s*\d{1,2}\s*$"))
    if loc.count() == 0:
        loc = page.locator("td").filter(has_text=re.compile(r"^\s*\d{1,2}\s*$"))
    return loc


def cal_month(page):
    m = re.search(r"(\d{4})\s*\.\s*(\d{1,2})", page.inner_text("body"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def go_next_month(cp):
    before = cal_month(cp)
    try:
        cp.get_by_text(">", exact=True).first.click(timeout=3000)
        cp.wait_for_timeout(800)
        settle(cp, 3000)
    except Exception:
        return False
    return cal_month(cp) != before


def goto_current_month(cp):
    """코트 화면은 과거 달에서 시작할 수 있어서 이번 달까지 넘긴다."""
    now = datetime.now(KST)
    for _ in range(6):
        ym = cal_month(cp)
        if ym is None or ym >= (now.year, now.month):
            return
        if not go_next_month(cp):
            return


def read_slots(page):
    """(날짜, [빈 시간], [찬 시간])  시간은 '06:00~08:00' 형태로 통일"""
    body = page.inner_text("body")
    m = re.search(r"\((\d{4}-\d{2}-\d{2})\)", body)
    date = m.group(1) if m else "날짜미상"
    free, full = [], []
    for txt in page.locator("select option").all_inner_texts():
        t = re.sub(r"\s+", " ", txt).strip()
        if "~" not in t:
            continue
        (full if any(w in t for w in FULL_WORDS) else free).append(norm_slot(t))
    return date, free, full


def scan_month(cp, cname, label, avail, observed, diag):
    now = datetime.now(KST)
    ym = cal_month(cp)
    n = day_links(cp).count()
    seen = miss = 0
    try:
        last_date = read_slots(cp)[0]
    except Exception:
        last_date = ""
    for i in range(n):
        loc = day_links(cp).nth(i)
        try:
            num = int(re.sub(r"\D", "", loc.inner_text()))
            if ym == (now.year, now.month) and num < now.day:
                continue
            loc.click(timeout=3000)
            cp.wait_for_timeout(600)
            settle(cp, 3000)
            date, free, full = read_slots(cp)
        except Exception:
            continue
        if (not free and not full) or date == last_date:   # 열리지 않은 날(화면이 안 바뀜)
            miss += 1
            if seen and miss >= 4:
                break
            continue
        last_date = date
        if not in_window(date):
            diag.append(f"[{cname}] {date}부터는 예약 가능 범위(오늘+{WINDOW_DAYS}일) 밖이라 건너뜀 "
                        f"(그 날 시간표: 빈 {len(free)}개 / 찬 {len(full)}개)")
            break
        miss = 0
        seen += 1
        for t in free:
            if is_upcoming(date, t):
                avail.add(f"{label}|{date}|{t}")
                observed.add(f"{label}|{date}|{t}")
        for t in full:
            if is_upcoming(date, t):
                observed.add(f"{label}|{date}|{t}")
    return seen


def scan_eulsukdo(page, diag):
    if not login(page, diag):
        return set(), set(), 0
    courts = find_courts(page, diag)
    avail, observed, total = set(), set(), 0
    for cname in courts:
        cp, popped = page, False
        try:
            cp, popped = open_court(page, cname)
            goto_current_month(cp)
            for step in range(2):             # 이번 달 + 다음 달
                ym = cal_month(cp)
                seen = scan_month(cp, cname, f"을숙도 {cname}", avail, observed, diag)
                total += seen
                diag.append(f"[{cname}] {ym} : 시간표가 나온 날짜 {seen}개")
                if step == 0 and not go_next_month(cp):
                    diag.append(f"[{cname}] 다음 달로 넘어가지 못함")
                    break
        except Exception as e:
            diag.append(f"[{cname}] 실패: {str(e)[:200]}")
        finally:
            if popped:
                cp.close()
    return avail, observed, total


# ---------- 사직 실내테니스장 ----------
def goto_retry(page, url, tries=2):
    last = None
    for _ in range(tries):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            return
        except Exception as e:
            last = e
            page.wait_for_timeout(3000)
    raise last



def parse_grid(tables):
    """표 목록(행→칸 글자)에서 '코트 번호 머리글'이 있는 표를 찾아 (코트번호, 시간, 칸글자) 목록을 만든다."""
    for trs in tables:
        for hi, row in enumerate(trs):
            cols = {}
            for i, c in enumerate(row):
                m = re.search(r"(\d+)\s*번\s*코트", c)
                if m and len(c) < 20:
                    cols[i] = m.group(1)
            if len(cols) < 2:
                continue
            res = []
            for r in trs[hi + 1:]:
                if not r:
                    continue
                m = re.search(r"\d{1,2}:\d{2}\s*~\s*\d{1,2}:\d{2}", r[0])
                if not m:
                    continue
                slot = norm_slot(m.group(0))
                for i, n in cols.items():
                    res.append((n, slot, r[i] if i < len(r) else ""))
            return res
    return []


def scan_sajik(page, diag):
    avail, observed, total = set(), set(), 0
    goto_retry(page, SAJIK_URL)
    if page.locator("input[type=password]:visible").count() > 0:
        diag.append("[사직] 로그인이 필요한 페이지로 보입니다. (비밀번호 입력창이 보임)")
        return avail, observed, 0
    tab = re.compile(r"^\s*\d{1,2}월\s*\d{1,2}일\s*\(.\)\s*$")
    def tabs():
        return page.locator("a, button, li, span").filter(has_text=tab)
    n = tabs().count()
    diag.append(f"[사직] 날짜 탭 {n}개")
    done = set()
    for i in range(n):
        try:
            tabs().nth(i).click(timeout=3000)
            page.wait_for_timeout(800)
            settle(page, 3000)
            body = page.inner_text("body")
            dm = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", body)
            if not dm:
                continue
            date = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
            if date in done:
                continue
            tables = page.evaluate("""() => [...document.querySelectorAll('table')].map(t =>
                [...t.querySelectorAll('tr')].map(tr => [...tr.children].map(c => (c.innerText || '').trim())))""")
            cells = parse_grid(tables)
        except Exception as e:
            diag.append(f"[사직] 탭 {i + 1} 읽기 실패: {str(e)[:120]}")
            continue
        if not cells:
            continue
        done.add(date)
        total += 1
        for n_, slot, txt in cells:
            if not is_upcoming(date, slot):
                continue
            key = f"사직 {n_}번 코트|{date}|{slot}"
            t = re.sub(r"\s+", "", txt)
            if any(w in t for w in FULL_WORDS):
                observed.add(key)
            elif t == "" or "가능" in t:
                observed.add(key)
                avail.add(key)
        if len(done) == 1:
            sample = [f"{n_} {sl} '{tx}'" for n_, sl, tx in cells[:6]]
            diag.append(f"[사직] {date} 표 읽기 예시: " + " / ".join(sample))
    diag.append(f"[사직] 시간표를 읽은 날짜 {len(done)}개: {sorted(done)}")
    return avail, observed, total


# ---------- 상태 저장 / 현황 페이지 ----------
def load_state():
    if not os.path.exists(STATE):
        return None
    try:
        d = json.load(open(STATE))
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("ver") != STATE_VER:
        return None          # 옛 형식이면 새로 기준을 잡는다
    return d


def build_page(avail, events, updated, warn=""):
    e = html.escape
    by_date = {}
    for k in sort_keys(avail):
        c, d, t = k.split("|")
        by_date.setdefault(d, {}).setdefault(c, []).append(t)
    cards = []
    for d, courts in sorted(by_date.items()):
        rows = "".join(
            f'<div class="row"><b>{e(c)}</b>'
            + "".join(f'<span class="chip">{e(t)}</span>' for t in courts[c]) + "</div>"
            for c in sorted(courts, key=court_sort))
        cards.append(f'<section class="card"><h3>{e(date_label(d))}</h3>{rows}</section>')
    body_cards = "".join(cards) or '<p class="empty">지금 빈 자리가 없어요. 취소가 나오면 아래 &lsquo;최근 취소&rsquo;에 표시돼요.</p>'
    ev = []
    for x in events[:30]:
        on = x["k"] in avail
        c, d, t = x["k"].split("|")
        tag = x.get("tag", "")
        ev.append(f'<li><span class="t">{e(x["t"])}</span> {e(date_label(d))} {e(t)} · {e(c)}'
                  f'{" (" + e(tag) + ")" if tag else ""}'
                  f'<span class="tag {"on" if on else "off"}">{"아직 가능" if on else "마감됨"}</span></li>')
    ev_html = "<ul>" + "".join(ev) + "</ul>" if ev else '<p class="empty">아직 기록이 없어요.</p>'
    warn_html = f'<div class="warn">{e(warn)}</div>' if warn else ""
    page = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>부산 테니스장 빈 자리 현황</title>
<style>
:root{{--bg:#f5f6f8;--fg:#1c1f26;--card:#fff;--mut:#6b7280;--line:#e5e7eb;--acc:#16794c;--chip:#e7f6ee;--warn:#fff4d6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111418;--fg:#eceff4;--card:#1b2027;--mut:#9aa3af;--line:#2a313b;--acc:#5fd39a;--chip:#173326;--warn:#3a3014}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}}
main{{max-width:720px;margin:0 auto;padding:20px 16px 48px}}
h1{{font-size:22px;margin:8px 0 2px}}.sub{{color:var(--mut);font-size:14px;margin:0 0 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:10px 0}}
.card h3{{margin:0 0 6px;font-size:16px}}.row{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}}
.row b{{min-width:62px}}.chip{{background:var(--chip);color:var(--acc);border-radius:999px;padding:2px 10px;font-size:14px}}
h2{{font-size:17px;margin:28px 0 6px}}ul{{list-style:none;padding:0;margin:0}}
li{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin:6px 0;font-size:14px}}
.t{{color:var(--mut);margin-right:6px}}.tag{{float:right;font-size:12px;border-radius:999px;padding:1px 8px}}
.tag.on{{background:var(--chip);color:var(--acc)}}.tag.off{{background:var(--line);color:var(--mut)}}
.empty{{color:var(--mut)}}.warn{{background:var(--warn);border-radius:10px;padding:8px 12px;margin:10px 0;font-size:14px}}
a.btn{{display:inline-block;background:var(--acc);color:#fff;text-decoration:none;border-radius:10px;padding:10px 16px;margin:10px 8px 0 0}}
@media (prefers-color-scheme:dark){{a.btn{{color:#07140d}}}}
</style></head><body><main>
<h1>테니스장 빈 자리 (을숙도 · 사직)</h1>
<p class="sub">오늘 이후 · 열려 있는 날짜 · 마지막 확인 {e(updated or '아직 없음')} (한국시간) · 2분마다 자동 새로고침</p>
{warn_html}
{body_cards}
<h2>최근 취소 / 새로 열린 자리</h2>
{ev_html}
<a class="btn" href="{COURT_URL}" target="_blank" rel="noopener">을숙도 예약</a>
<a class="btn" href="{SAJIK_URL}" target="_blank" rel="noopener">사직 예약</a>
</main></body></html>"""
    os.makedirs(SITE_DIR, exist_ok=True)
    open(os.path.join(SITE_DIR, "index.html"), "w", encoding="utf-8").write(page)


# ---------- 실행 ----------
FACILITIES = {"을숙도": scan_eulsukdo, "사직": scan_sajik}


def main():
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    st = load_state()
    prev = set(st["avail"]) if st else None
    prev_all = set(st["all"]) if st else set()
    prev_facs = set(st.get("facs", [])) if st else set()
    events = list(st["events"]) if st else []
    updated = st["updated"] if st else ""
    diag, avail, observed, ok_facs, failed = [], set(), set(), set(), []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for fac, fn in FACILITIES.items():
                a = o = set()
                t = 0
                try:
                    page = browser.new_page()
                    a, o, t = fn(page, diag)
                    page.close()
                except Exception as e:
                    diag.append(f"[{fac}] 오류: {str(e)[:200]}")
                if t > 0:
                    avail |= a
                    observed |= o
                    ok_facs.add(fac)
                else:
                    failed.append(fac)
                    diag.append(f"[{fac}] 시간표를 읽지 못해 이번 결과는 저장하지 않고 이전 정보를 유지합니다.")
                    # 읽기 실패한 시설은 이전 정보를 그대로 유지(가짜 신규 알림 방지)
                    avail |= {k for k in (prev or set()) if k.startswith(fac + " ")}
                    observed |= {k for k in prev_all if k.startswith(fac + " ")}
    except Exception as e:
        diag.append(f"오류: {e}")
        print("오류 발생:", type(e).__name__)

    ok = bool(ok_facs)
    now_s = datetime.now(KST).strftime("%m/%d %H:%M")
    new_tags = {}
    if ok and prev is not None:
        for k in avail - prev:
            fac = k.split("|")[0].split(" ")[0]
            if fac not in ok_facs or fac not in prev_facs:
                continue            # 이번에 못 읽었거나 처음 보는 시설은 기준만 잡는다
            # 전에 시간표에 있었는데(찬 자리였는데) 비었다 = 취소, 처음 보는 시간 = 신규 오픈
            new_tags[k] = "취소" if k in prev_all else "신규 오픈"
        if new_tags:
            alert_mail(new_tags, avail)
            print("새 빈 자리 메일 발송:", len(new_tags))
        else:
            print("새 빈 자리 없음")
    elif ok:
        print("첫 실행: 현재 상태를 기준으로 저장")

    if manual:
        body = ("현재 빈 자리(오늘 이후):\n" + (fmt(avail) or "없음")
                + "\n\n--- 진단 ---\n" + "\n\n".join(diag))
        send_mail("[빈코트 감시] 현재 빈 자리 + 진단", body)
        print("진단 메일 발송")

    if ok:
        for k in reversed(sort_keys(new_tags)):
            events.insert(0, {"t": now_s, "k": k, "tag": new_tags[k]})
        events = events[:60]
        updated = now_s
        json.dump({"ver": STATE_VER, "avail": sorted(avail), "all": sorted(observed),
                   "facs": sorted(prev_facs | ok_facs), "events": events, "updated": updated},
                  open(STATE, "w"), ensure_ascii=False)
        warn = ("일부 시설(" + ", ".join(failed) + ")은 이번 확인에 실패해 이전 정보를 보여주고 있어요.") if failed else ""
        build_page(avail, events, updated, warn)
    else:
        build_page(prev or set(), events, updated,
                   warn="이번 확인에 실패해서 마지막으로 성공한 시점의 정보를 보여주고 있어요.")


if __name__ == "__main__":
    main()
