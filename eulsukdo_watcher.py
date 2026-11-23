"""
을숙도 테니스장 빈 자리/취소표 감시 -> Gmail 알림 + 현황 페이지
- 이번 달 + 다음 달(열려 있는 날짜까지), 오늘 이후 일정만 확인
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
FULL_WORDS = ["예약완료", "마감", "불가"]   # 이 단어가 붙은 시간은 '찬 자리'
STATE = "seen.json"
STATE_VER = 2
SITE_DIR = "site"
KST = ZoneInfo("Asia/Seoul")
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


def sort_keys(keys):
    # 키 = "코트|날짜|시간" -> 날짜, 시간, 코트 순
    return sorted(keys, key=lambda k: (k.split("|")[1], k.split("|")[2], k.split("|")[0]))


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


def alert_mail(new_tags):
    """new_tags: {키: '취소' 또는 '신규 오픈'}"""
    ks = sort_keys(new_tags)
    c, d, t = ks[0].split("|")
    subject = f"[을숙도 빈코트] {date_label(d)} {t.split('~')[0]} {c}"
    if len(ks) > 1:
        subject += f" 외 {len(ks) - 1}건"
    lines = "\n".join(slot_line(k, new_tags[k]) for k in ks)
    body = ("을숙도 다목적 실내 테니스장에 예약 가능한 자리가 새로 생겼어요.\n\n"
            f"{lines}\n\n지금 예약하기: {COURT_URL}\n\n"
            "다른 사람이 먼저 예약할 수 있으니 서둘러 주세요.")
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
        for c in sorted(courts, key=lambda x: int(re.sub(r"\D", "", x) or 0)):
            out.append(f"   {c}: {', '.join(courts[c])}")
        out.append("")
    return "\n".join(out).strip()


# ---------- 사이트 읽기 ----------
def login(page, diag):
    msgs = []

    def on_dialog(d):
        msgs.append(d.message)
        d.accept()

    page.on("dialog", on_dialog)
    page.goto(LOGIN_URL)
    page.wait_for_load_state("networkidle")
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
    page.wait_for_load_state("networkidle")
    ok = page.locator("input[type=password]:visible").count() == 0
    diag.append(f"로그인: {'성공' if ok else '실패(비밀번호 입력창이 아직 보임)'} / 주소 {page.url}")
    if msgs:
        diag.append("사이트 안내창 메시지: " + " / ".join(msgs))
    return ok


def find_courts(page, diag):
    page.goto(COURT_URL)
    page.wait_for_load_state("networkidle")
    body = page.inner_text("body")
    nums = sorted(set(re.findall(r"(\d+)\s*번\s*코트", body)), key=int)
    courts = {f"{n}번 코트": "" for n in nums}
    diag.append(f"코트 {len(courts)}개: {list(courts)}")
    return courts


def open_court(page, cname):
    """코트 화면을 열고 (사용할 page, 새 창 여부)를 돌려준다."""
    n = cname[0]
    page.goto(COURT_URL)
    page.wait_for_load_state("networkidle")
    link = page.locator("a", has_text=re.compile(rf"{n}\s*번\s*코트")).first
    try:
        with page.context.expect_page(timeout=4000) as pi:
            link.click()
        newp = pi.value
        newp.on("dialog", lambda d: d.accept())
        newp.wait_for_load_state("networkidle")
        return newp, True
    except PWTimeout:
        page.wait_for_load_state("networkidle")
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
        cp.wait_for_load_state("domcontentloaded")
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


def scan_month(cp, cname, avail, observed):
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
            cp.wait_for_load_state("domcontentloaded")
            date, free, full = read_slots(cp)
        except Exception:
            continue
        if (not free and not full) or date == last_date:   # 열리지 않은 날(화면이 안 바뀜)
            miss += 1
            if seen and miss >= 4:
                break
            continue
        last_date = date
        miss = 0
        seen += 1
        for t in free:
            if is_upcoming(date, t):
                avail.add(f"{cname}|{date}|{t}")
                observed.add(f"{cname}|{date}|{t}")
        for t in full:
            if is_upcoming(date, t):
                observed.add(f"{cname}|{date}|{t}")
    return seen


def scan(page, diag):
    courts = find_courts(page, diag)
    avail, observed, total = set(), set(), 0
    for cname in courts:
        cp, popped = page, False
        try:
            cp, popped = open_court(page, cname)
            goto_current_month(cp)
            for step in range(2):             # 이번 달 + 다음 달
                label = cal_month(cp)
                seen = scan_month(cp, cname, avail, observed)
                total += seen
                diag.append(f"[{cname}] {label} : 시간표가 나온 날짜 {seen}개")
                if step == 0 and not go_next_month(cp):
                    diag.append(f"[{cname}] 다음 달로 넘어가지 못함")
                    break
        except Exception as e:
            diag.append(f"[{cname}] 실패: {str(e)[:200]}")
        finally:
            if popped:
                cp.close()
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
            for c in sorted(courts, key=lambda x: int(re.sub(r"\D", "", x) or 0)))
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
<title>을숙도 테니스장 빈 자리 현황</title>
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
a.btn{{display:inline-block;background:var(--acc);color:#fff;text-decoration:none;border-radius:10px;padding:10px 16px;margin-top:10px}}
@media (prefers-color-scheme:dark){{a.btn{{color:#07140d}}}}
</style></head><body><main>
<h1>을숙도 테니스장 빈 자리</h1>
<p class="sub">오늘 이후 · 열려 있는 날짜 · 마지막 확인 {e(updated or '아직 없음')} (한국시간) · 2분마다 자동 새로고침</p>
{warn_html}
{body_cards}
<h2>최근 취소 / 새로 열린 자리</h2>
{ev_html}
<a class="btn" href="{COURT_URL}" target="_blank" rel="noopener">예약하러 가기</a>
</main></body></html>"""
    os.makedirs(SITE_DIR, exist_ok=True)
    open(os.path.join(SITE_DIR, "index.html"), "w", encoding="utf-8").write(page)


# ---------- 실행 ----------
def main():
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    st = load_state()
    prev = set(st["avail"]) if st else None
    prev_all = set(st["all"]) if st else set()
    events = list(st["events"]) if st else []
    updated = st["updated"] if st else ""
    diag, avail, observed, ok = [], set(), set(), False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            if login(page, diag):
                avail, observed, total = scan(page, diag)
                ok = total > 0   # 시간표를 한 번이라도 읽었을 때만 성공으로 본다
                if not ok:
                    diag.append("시간표를 하나도 읽지 못해 이번 결과는 저장하지 않습니다.")
    except Exception as e:
        diag.append(f"오류: {e}")
        print("오류 발생:", type(e).__name__)

    now_s = datetime.now(KST).strftime("%m/%d %H:%M")
    new_tags = {}
    if ok and prev is not None:
        for k in avail - prev:
            # 전에 시간표에 있었는데(찬 자리였는데) 비었다 = 취소, 처음 보는 시간 = 신규 오픈
            new_tags[k] = "취소" if k in prev_all else "신규 오픈"
        if new_tags:
            alert_mail(new_tags)
            print("새 빈 자리 메일 발송:", len(new_tags))
        else:
            print("새 빈 자리 없음")
    elif ok:
        print("첫 실행: 현재 상태를 기준으로 저장")

    if manual:
        body = ("현재 빈 자리(오늘 이후):\n" + (fmt(avail) or "없음")
                + "\n\n--- 진단 ---\n" + "\n\n".join(diag))
        send_mail("[을숙도 감시] 현재 빈 자리 + 진단", body)
        print("진단 메일 발송")

    if ok:
        for k in reversed(sort_keys(new_tags)):
            events.insert(0, {"t": now_s, "k": k, "tag": new_tags[k]})
        events = events[:60]
        updated = now_s
        json.dump({"ver": STATE_VER, "avail": sorted(avail), "all": sorted(observed),
                   "events": events, "updated": updated},
                  open(STATE, "w"), ensure_ascii=False)
        build_page(avail, events, updated)
    else:
        build_page(prev or set(), events, updated,
                   warn="이번 확인에 실패해서 마지막으로 성공한 시점의 정보를 보여주고 있어요.")


if __name__ == "__main__":
    main()
