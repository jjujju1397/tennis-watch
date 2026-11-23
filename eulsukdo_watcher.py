"""
을숙도 테니스장 빈 자리/취소표 감시 -> Gmail 알림
- 자동 실행(5분마다): 새로 생긴 빈 자리(취소 포함)가 있을 때만 메일
- 수동 실행(Run workflow): 현재 빈 자리 전체 + 진단 정보를 메일로 보냄
"""
import os, re, json, smtplib
from email.message import EmailMessage
from playwright.sync_api import sync_playwright

BASE = "https://saha-eulsukdogym.kr"
LOGIN_URL = f"{BASE}/login"
COURT_URL = f"{BASE}/html/?pCode=7"
FULL_WORDS = ["예약완료", "마감", "불가"]   # 이 단어가 붙은 시간은 '찬 자리'
STATE = "seen.json"


def send_mail(subject, body):
    user = os.environ["GMAIL_USER"]
    msg = EmailMessage()
    msg["From"], msg["To"] = user, os.environ.get("MAIL_TO", user)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, os.environ["GMAIL_APP_PW"])
        s.send_message(msg)


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
    """접수하기 화면에서 1~6번 코트 이름과(있다면) 링크를 찾는다."""
    page.goto(COURT_URL)
    page.wait_for_load_state("networkidle")
    body = page.inner_text("body")
    nums = sorted(set(re.findall(r"(\d+)\s*번\s*코트", body)), key=int)
    items = page.eval_on_selector_all(
        "a", "els => els.map(e => [(e.innerText||'').trim(), e.href])")
    courts = {}
    for n in nums:
        href = ""
        for text, h in items:
            if re.search(rf"\b{n}\s*번\s*코트", text) and h and not h.endswith("#"):
                href = h
                break
        courts[f"{n}번 코트"] = href
    diag.append(f"코트 {len(courts)}개: {courts}")
    if not courts:
        diag.append("접수 화면 글자(앞 800자):\n" + body[:800])
    return courts


def open_court(page, cname, href):
    if href:
        page.goto(href)
    else:
        page.goto(COURT_URL)
        page.wait_for_load_state("networkidle")
        page.locator(f"text=/{cname[0]}\\s*번\\s*코트/").first.click()
    page.wait_for_load_state("networkidle")


def day_links(page):
    loc = page.locator("td a").filter(has_text=re.compile(r"^\s*\d{1,2}\s*$"))
    if loc.count() == 0:
        loc = page.locator("td").filter(has_text=re.compile(r"^\s*\d{1,2}\s*$"))
    return loc


def read_slots(page):
    """현재 선택된 날짜의 (날짜문자열, [빈 시간들], [찬 시간들])"""
    body = page.inner_text("body")
    m = re.search(r"\((\d{4}-\d{2}-\d{2})\)", body)
    date = m.group(1) if m else "날짜미상"
    free, full = [], []
    for txt in page.locator("select option").all_inner_texts():
        t = re.sub(r"\s+", " ", txt).strip()
        if "~" not in t:
            continue
        (full if any(w in t for w in FULL_WORDS) else free).append(t)
    return date, free, full


def scan(page, diag):
    courts = find_courts(page, diag)
    available = {}   # "코트|날짜|시간" -> True
    for cname, url in courts.items():
        try:
            open_court(page, cname, url)
        except Exception as e:
            diag.append(f"[{cname}] 열기 실패: {e}")
            continue
        diag.append(f"[{cname}] 열린 주소: {page.url}")
        n = day_links(page).count()
        seen_dates = 0
        for i in range(n):
            try:
                day_links(page).nth(i).click(timeout=3000)
                page.wait_for_timeout(800)
                page.wait_for_load_state("domcontentloaded")
                date, free, full = read_slots(page)
            except Exception:
                continue
            if not free and not full:
                continue
            seen_dates += 1
            for t in free:
                available[f"{cname}|{date}|{t}"] = True
        diag.append(f"[{cname}] 날짜칸 {n}개, 시간표가 나온 날짜 {seen_dates}개")
    return available


def fmt(keys):
    rows = {}
    for k in sorted(keys):
        c, d, t = k.split("|")
        rows.setdefault((c, d), []).append(t)
    return "\n".join(f"{c}  {d}  : {', '.join(ts)}" for (c, d), ts in rows.items())


def main():
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    prev = None
    if os.path.exists(STATE):
        prev = set(json.load(open(STATE)))
    diag, cur, ok = [], {}, False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            if login(page, diag):
                cur = scan(page, diag)
                ok = True
    except Exception as e:
        diag.append(f"오류: {e}")
        print("오류 발생:", type(e).__name__)

    cur_keys = set(cur)
    if ok and prev is not None:
        new = cur_keys - prev
        if new:
            send_mail("[을숙도 테니스장] 새로 빈 자리가 생겼어요!",
                      fmt(new) + f"\n\n바로 예약: {COURT_URL}")
            print("새 빈 자리 메일 발송:", len(new))
        else:
            print("새 빈 자리 없음")
    elif ok:
        print("첫 실행: 현재 상태를 기준으로 저장")

    if manual:
        body = ("현재 빈 자리(전체):\n" + (fmt(cur_keys) or "없음")
                + "\n\n--- 진단 ---\n" + "\n\n".join(diag))
        send_mail("[을숙도 감시] 현재 빈 자리 + 진단", body)
        print("진단 메일 발송")

    if ok:
        json.dump(sorted(cur_keys), open(STATE, "w"), ensure_ascii=False)


if __name__ == "__main__":
    main()
