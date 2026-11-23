"""
을숙도 테니스장 취소표 감시 -> Gmail 알림
설치:  pip install playwright && playwright install chromium
실행:  환경변수를 설정한 뒤  python eulsukdo_watcher.py
  SITE_ID, SITE_PW         예약 사이트 아이디/비밀번호
  GMAIL_USER               보내는 Gmail 주소
  GMAIL_APP_PW             Gmail 앱 비밀번호(16자리)
  MAIL_TO                  받는 주소 (기본: GMAIL_USER)
  INTERVAL_SEC             조회 간격 초 (기본 120, 너무 짧게 하지 마세요)
"""
import os, re, time, smtplib, hashlib
from email.message import EmailMessage
from playwright.sync_api import sync_playwright

BASE = "https://saha-eulsukdogym.kr"
LOGIN_URL = f"{BASE}/login"
COURT_URL = f"{BASE}/html/?pCode=7"
INTERVAL = int(os.environ.get("INTERVAL_SEC", "120"))
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


def scan(page):
    """코트 목록 -> 각 코트의 예약 화면에서 '가능' 표시를 찾는다."""
    page.goto(COURT_URL)
    page.wait_for_load_state("networkidle")
    names = [t.strip() for t in page.locator("text=/을숙도 테니스장 - \\d번 코트/").all_inner_texts()]
    found = {}
    for i, name in enumerate(dict.fromkeys(names)):
        page.goto(COURT_URL)
        page.wait_for_load_state("networkidle")
        page.locator(f"text={name}").first.click()
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        hits = [ln.strip() for ln in text.splitlines()
                if any(w in ln for w in OPEN_WORDS)]
        if hits:
            found[name] = hits
    return found


def main():
    """GitHub Actions용: 1회만 조회하고 종료. 중복 알림 방지용 상태는 seen.txt에 저장."""
    state = "seen.txt"
    prev = open(state).read().strip() if os.path.exists(state) else ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        login(page)
        found = scan(page)
    lines = sorted(f"[{c}] {h}" for c, hs in found.items() for h in hs)
    key = hashlib.md5("\n".join(lines).encode()).hexdigest() if lines else ""
    if lines and key != prev:
        send_mail("[을숙도 테니스장] 빈 자리 발견!",
                  "\n".join(lines) + f"\n\n바로 예약: {COURT_URL}")
        print("메일 발송:", lines)
    else:
        print("변화 없음")
    open(state, "w").write(key)


if __name__ == "__main__":
    main()
