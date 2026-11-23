"""
을숙도 테니스장 취소표 감시 -> Gmail 알림
GitHub Actions에서 5분마다 1회 실행됩니다.
수동 실행(Run workflow)하면 진단 보고서 메일도 보냅니다.
"""
import os, re, time, smtplib, hashlib
from email.message import EmailMessage
from playwright.sync_api import sync_playwright

BASE = "https://saha-eulsukdogym.kr"
LOGIN_URL = f"{BASE}/login"
COURT_URL = f"{BASE}/html/?pCode=7"
# 빈 자리를 뜻하는 단어들 (사이트 실제 표기에 맞게 수정)
OPEN_WORDS = ["예약가능", "신청가능", "접수가능", "예약하기", "신청하기"]


def send_mail(subject, body):
    user = os.environ["GMAIL_USER"]
    msg = EmailMessage()
    msg["From"], msg["To"] = user, os.environ.get("MAIL_TO", user)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, os.environ["GMAIL_APP_PW"])
        s.send_message(msg)


def login(page):
    page.goto(LOGIN_URL)
    page.fill("input[type=text], input[name*=id i]", os.environ["SITE_ID"])
    page.fill("input[type=password]", os.environ["SITE_PW"])
    page.click("button:has-text('로그인'), input[value*='로그인'], a:has-text('로그인')")
    page.wait_for_load_state("networkidle")


def scan(page, diag):
    """코트 목록 -> 각 코트의 예약 화면에서 '가능' 표시를 찾는다."""
    page.goto(COURT_URL)
    page.wait_for_load_state("networkidle")
    diag.append(f"현재 주소: {page.url}")
    body = page.inner_text("body")
    diag.append("예약 목록 화면 글자(앞 800자):\n" + body[:800])
    names = [t.strip() for t in page.locator("text=/\\d번 코트/").all_inner_texts()]
    names = list(dict.fromkeys(names))
    diag.append(f"찾은 코트 수: {len(names)} -> {names}")
    found = {}
    for name in names:
        page.goto(COURT_URL)
        page.wait_for_load_state("networkidle")
        page.locator(f"text={name}").first.click()
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        diag.append(f"[{name}] 화면 글자(앞 500자):\n" + text[:500])
        hits = [ln.strip() for ln in text.splitlines()
                if any(w in ln for w in OPEN_WORDS)]
        if hits:
            found[name] = hits
    return found


def main():
    """1회 조회 후 종료. 수동 실행이면 진단 메일을 보낸다."""
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    state = "seen.txt"
    prev = open(state).read().strip() if os.path.exists(state) else ""
    diag = []
    found = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            login(page)
            diag.append(f"로그인 후 주소: {page.url}")
            found = scan(page, diag)
    except Exception as e:
        diag.append(f"오류: {e}")
        print("오류 발생:", type(e).__name__)
    lines = sorted(f"[{c}] {h}" for c, hs in found.items() for h in hs)
    key = hashlib.md5("\n".join(lines).encode()).hexdigest() if lines else ""
    if lines and key != prev:
        send_mail("[을숙도 테니스장] 빈 자리 발견!",
                  "\n".join(lines) + f"\n\n바로 예약: {COURT_URL}")
        print("메일 발송:", lines)
    else:
        print("변화 없음")
    if manual:
        send_mail("[을숙도 감시] 진단 보고서", "\n\n".join(diag) or "정보 없음")
        print("진단 메일 발송")
    open(state, "w").write(key)


if __name__ == "__main__":
    main()
