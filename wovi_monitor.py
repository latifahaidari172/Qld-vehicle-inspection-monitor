#!/usr/bin/env python3
"""
WOVI Booking Monitor — Queensland Inspection Services
Monitors Brisbane, Burleigh Heads, Narangba, Yatala for 2 vehicles.
Auto-books earliest slot before each vehicle's cutoff date via 2captcha.
"""

import csv, json, os, re, smtplib, sys, time
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ── Constants ─────────────────────────────────────────────────────────────────

BOOKING_URL        = "https://wovi.com.au/bookings/"
CSV_FILE           = Path(__file__).parent / "wovi_results.csv"
STATE_FILE         = Path(__file__).parent / "wovi_state.json"
LOCATIONS          = ["Brisbane", "Burleigh Heads", "Narangba", "Yatala"]
RECAPTCHA_SITE_KEY = "6LfAG_0pAAAAAFQzCmk7OQ4roYKXfgYFAPwsVo-5"

# ── Timezone ──────────────────────────────────────────────────────────────────

def now_adelaide() -> datetime:
    import zoneinfo
    try:
        return datetime.now(zoneinfo.ZoneInfo("Australia/Adelaide")).replace(tzinfo=None)
    except Exception:
        return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=9, minutes=30)

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    ts = now_adelaide().strftime("%d/%m/%Y %I:%M:%S %p")
    print(f"[{ts}]{' ['+level+']' if level != 'INFO' else ''} {msg}", flush=True)

def get_env(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        log(f"Missing secret: {key}", "ERROR"); sys.exit(1)
    return v

def parse_cutoff(s: str) -> datetime:
    for fmt in ["%d/%m/%Y", "%Y-%m-%d"]:
        try: return datetime.strptime(s, fmt)
        except ValueError: pass
    log(f"Cannot parse date: {s}", "ERROR"); sys.exit(1)

def parse_date(s: str) -> datetime | None:
    for pat, fn in [
        (r'(\d{4})-(\d{2})-(\d{2})', lambda m: datetime(int(m[1]), int(m[2]), int(m[3]))),
        (r'(\d{1,2})/(\d{1,2})/(\d{4})', lambda m: datetime(int(m[3]), int(m[2]), int(m[1]))),
    ]:
        m = re.search(pat, s)
        if m:
            try: return fn(m)
            except ValueError: pass
    return None

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    except Exception:
        return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── CSV ───────────────────────────────────────────────────────────────────────

def write_csv(t: datetime, vehicle: str, location: str, result: str, detail: str = "", keep: int = 60):
    header = ["Date", "Time", "Vehicle", "Location", "Result", "Detail"]
    new_row = [t.strftime("%d/%m/%Y"), t.strftime("%I:%M:%S %p"), vehicle, location, result, detail]
    rows = []
    if CSV_FILE.exists():
        with open(CSV_FILE, newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)
        # Strip header if present
        if rows and rows[0] == header:
            rows = rows[1:]
    rows.append(new_row)
    # Keep only last `keep` rows
    rows = rows[-keep:]
    with open(CSV_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str, gmail: str, pw: str, to: str):
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = gmail
    msg["To"]      = to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(gmail, pw)
            s.sendmail(gmail, to, msg.as_string())
        log(f"Email sent: {subject}")
    except Exception as e:
        log(f"Email failed: {e}", "ERROR")

# ── Chrome ────────────────────────────────────────────────────────────────────

def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--log-level=3")
    d = webdriver.Chrome(options=opts)
    d.set_page_load_timeout(30)
    return d

# ── 2captcha ──────────────────────────────────────────────────────────────────

def solve_captcha(api_key: str) -> str | None:
    log("Solving CAPTCHA via 2captcha...")
    try:
        r = requests.post("http://2captcha.com/in.php", data={
            "key": api_key, "method": "userrecaptcha",
            "googlekey": RECAPTCHA_SITE_KEY,
            "pageurl": BOOKING_URL, "json": 1,
        }, timeout=30).json()
        if r.get("status") != 1:
            log(f"2captcha submit error: {r}", "ERROR"); return None
        cid = r["request"]
        log(f"CAPTCHA submitted (id={cid})")
        for _ in range(24):
            time.sleep(5)
            try:
                resp = requests.get("http://2captcha.com/res.php", params={
                    "key": api_key, "action": "get", "id": cid, "json": 1
                }, timeout=10)
                try:
                    p = resp.json()
                    if p.get("status") == 1:
                        log("CAPTCHA solved ✅"); return p["request"]
                    if p.get("request") != "CAPCHA_NOT_READY":
                        log(f"2captcha error: {p}", "ERROR"); return None
                except Exception:
                    t = resp.text.strip()
                    if t.startswith("OK|"):
                        log("CAPTCHA solved ✅"); return t[3:]
                    if t != "CAPCHA_NOT_READY":
                        log(f"2captcha: {t}", "ERROR"); return None
            except Exception as ex:
                log(f"Poll error: {ex}", "WARN")
        log("CAPTCHA timed out", "ERROR"); return None
    except Exception as e:
        log(f"2captcha exception: {e}", "ERROR"); return None

# ── Form helpers ──────────────────────────────────────────────────────────────

def fill(driver, value: str, *names):
    for name in names:
        for attr in ["name", "id", "ng-model"]:
            try:
                el = driver.find_element(By.XPATH, f"//input[@{attr}='{name}']")
                driver.execute_script(
                    "arguments[0].value=arguments[1];"
                    "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                    el, value
                )
                return True
            except NoSuchElementException:
                pass
    return False

def select_by(driver, value: str, *xpaths):
    for xpath in xpaths:
        try:
            sel = driver.find_element(By.XPATH, xpath)
            try: Select(sel).select_by_value(value)
            except Exception: Select(sel).select_by_visible_text(value)
            return True
        except Exception:
            pass
    return False

def click_next(driver, wait):
    try:
        btn = wait.until(EC.presence_of_element_located((By.XPATH,
            "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'next')] | "
            "//button[@type='submit'] | //input[@type='submit']"
        )))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(2)
        return True
    except TimeoutException:
        return False

def select_location(driver, wait, location: str) -> bool:
    try:
        sel = wait.until(EC.presence_of_element_located((By.XPATH,
            "//select[.//option[contains(text(),'Brisbane')]]"
        )))
        for opt in Select(sel).options:
            if location.lower() in opt.text.lower():
                Select(sel).select_by_visible_text(opt.text)
                return True
    except Exception:
        pass
    return False

def wait_for_calendar(driver, timeout=8) -> bool:
    try:
        WebDriverWait(driver, timeout).until(lambda d: len(
            d.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']")
        ) > 0)
        return True
    except TimeoutException:
        return False

def get_day_value(driver, item) -> str | None:
    return driver.execute_script(
        "try{var s=angular.element(arguments[0]).scope();"
        "if(!s||!s.day) return null;return s.day.value;}catch(e){return null;}", item
    )

# ── Step 1: Find available slots ──────────────────────────────────────────────

def find_slots(driver, cutoff: datetime, label: str) -> list:
    wait = WebDriverWait(driver, 20)
    slots = []
    for location in LOCATIONS:
        try:
            if not select_location(driver, wait, location):
                continue
            if not wait_for_calendar(driver):
                log(f"  {location}: calendar timeout")
                continue
            found = 0
            for item in driver.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']"):
                try:
                    d = driver.execute_script(
                        "try{var s=angular.element(arguments[0]).scope();"
                        "if(!s||!s.day||!s.day.available||!s.day.thisMonth) return null;"
                        "return s.day.value;}catch(e){return null;}", item
                    )
                    if not d: continue
                    dt = parse_date(d)
                    if dt and dt < cutoff:
                        slots.append((dt, d, location))
                        found += 1
                except Exception:
                    continue
            log(f"  {location}: {found} slot(s) before cutoff")
        except Exception as e:
            log(f"  {location}: error — {e}", "WARN")
    slots.sort(key=lambda x: x[0])
    return slots

# ── Step 2: Book the slot ─────────────────────────────────────────────────────

def book_slot(location: str, date_str: str, owner: dict, vehicle: dict,
              api_key: str, label: str) -> bool:

    driver = make_driver()
    wait   = WebDriverWait(driver, 20)

    try:
        log(f"[{label}] Booking {location} on {date_str}")
        driver.get(BOOKING_URL)
        time.sleep(3)

        # Select location and wait for calendar
        if not select_location(driver, wait, location):
            log(f"[{label}] Could not select location", "ERROR"); return False
        time.sleep(3)
        if not wait_for_calendar(driver):
            log(f"[{label}] Calendar timeout", "ERROR"); return False

        # Click the target date
        clicked = False
        for item in driver.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']"):
            if get_day_value(driver, item) == date_str:
                driver.execute_script("arguments[0].click();", item)
                clicked = True
                log(f"[{label}] Date clicked: {date_str}")
                time.sleep(2)
                break

        if not clicked:
            log(f"[{label}] Could not find date {date_str}", "ERROR"); return False

        # Select earliest available time slot
        try:
            time_sel = driver.find_element(By.XPATH,
                "//select[contains(@ng-model,'time') or contains(@ng-change,'time')]"
            )
            opts = sorted(
                [o for o in Select(time_sel).options
                 if o.get_attribute("value") not in ("", "null", "undefined", "0")],
                key=lambda o: o.text
            )
            if opts:
                Select(time_sel).select_by_visible_text(opts[0].text)
                log(f"[{label}] Time: {opts[0].text}")
        except NoSuchElementException:
            pass

        time.sleep(1)
        click_next(driver, wait)
        time.sleep(3)

        # ── Vehicle details ───────────────────────────────────────────────────
        log(f"[{label}] Filling vehicle details...")

        # Vehicle type — click label text
        try:
            label_el = driver.find_element(By.XPATH,
                f"//label[contains(normalize-space(.),'{vehicle['type']}')]"
            )
            driver.execute_script("arguments[0].click();", label_el)
        except NoSuchElementException:
            pass

        fill(driver, vehicle["vin"],            "vin", "chassis", "VIN", "vinChassis")
        fill(driver, vehicle["make"],           "make", "vehicleMake")
        fill(driver, vehicle["model"],          "model", "vehicleModel")
        fill(driver, vehicle["year"],           "year", "buildYear", "buildDateYear")
        fill(driver, vehicle["colour"],         "colour", "color", "vehicleColour")
        fill(driver, vehicle["purchased_from"], "purchasedFrom", "sellerName")

        select_by(driver, vehicle["build_month"],
            "//select[contains(@name,'buildDateMonth') or contains(@ng-model,'buildDateMonth')]"
        )
        select_by(driver, vehicle["damage"],
            "//select[contains(@ng-model,'damage') or contains(@name,'damage') or contains(@ng-model,'Damage')]"
        )
        select_by(driver, vehicle["purchase_method"],
            "//select[contains(@ng-model,'purchase') or contains(@name,'purchase') or contains(@ng-model,'Purchase')]"
        )

        click_next(driver, wait)
        time.sleep(2)

        # ── Customer details ──────────────────────────────────────────────────
        log(f"[{label}] Filling customer details...")
        fill(driver, owner["crn"],        "crn", "CRN", "licenceNumber", "crnLicence")
        fill(driver, owner["first_name"], "firstName", "first_name", "fname")
        fill(driver, owner["last_name"],  "lastName", "last_name", "surname")
        fill(driver, owner["address"],    "address", "streetAddress", "street")
        fill(driver, owner["suburb"],     "suburb", "city")
        fill(driver, owner["postcode"],   "postcode", "zipCode")
        fill(driver, owner["email"],      "email", "emailAddress")
        fill(driver, owner["phone"],      "phone", "mobile", "mobileNumber")

        click_next(driver, wait)
        time.sleep(2)

        # ── CAPTCHA ───────────────────────────────────────────────────────────
        log(f"[{label}] Solving CAPTCHA...")
        token = solve_captcha(api_key)
        if not token:
            log(f"[{label}] CAPTCHA failed", "ERROR"); return False

        driver.execute_script(
            "var el=document.getElementById('g-recaptcha-response');"
            "if(el) el.innerHTML=arguments[0];", token
        )
        driver.execute_script(
            "var el=document.querySelector('[name=\"g-recaptcha-response\"]');"
            "if(el) el.value=arguments[0];", token
        )
        time.sleep(1)

        # ── Submit ────────────────────────────────────────────────────────────
        log(f"[{label}] Submitting...")
        click_next(driver, wait)
        time.sleep(4)

        # Handle "Update Booking" popup
        try:
            popup = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH,
                "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'update booking')]"
            )))
            driver.execute_script("arguments[0].click();", popup)
            log(f"[{label}] Confirmed 'Update Booking'")
            time.sleep(3)
        except TimeoutException:
            pass

        confirmed = any(w in driver.page_source.lower() for w in
                        ["booking has been secured", "booking number", "confirmed",
                         "success", "thank you", "submitted"])
        log(f"[{label}] {'BOOKING CONFIRMED ✅' if confirmed else 'Submitted — verify manually'}")
        return confirmed

    except Exception as e:
        log(f"[{label}] Error: {e}", "ERROR")
        import traceback; log(traceback.format_exc(), "DEBUG")
        return False
    finally:
        driver.quit()

# ── Daily summary ─────────────────────────────────────────────────────────────

def send_daily_summary(gmail: str, pw: str, to: str):
    today = now_adelaide().strftime("%d/%m/%Y")
    v1, v2, total = [], [], 0
    if CSV_FILE.exists():
        with open(CSV_FILE, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("Date") != today: continue
                total += 1
                if row.get("Result") in ("No earlier slots", ""): continue
                line = f"  {row['Time']} — {row['Location']}: {row['Result']} {row['Detail']}".strip()
                (v1 if "1" in row.get("Vehicle","") else v2).append(line)

    def sec(lbl, rows):
        return f"{lbl}:\n" + ("\n".join(rows) if rows else "No earlier slots today.")

    body = (
        f"WOVI Daily Summary — {today}\n{'='*40}\n\n"
        f"Total checks: {total}\n\n"
        f"{sec('Vehicle 1 (BYD)', v1)}\n\n"
        f"{sec('Vehicle 2 (Kia)', v2)}\n\n"
        f"Checks every 1 min (6:30–10am Brisbane) and 5 mins otherwise.\n"
        f"Full log: wovi_results.csv in your GitHub repository."
    )
    send_email(f"WOVI Daily Summary — {today}", body, gmail, pw, to)

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    log("=" * 55)
    log("WOVI Booking Monitor — checking both vehicles")
    log("=" * 55)

    gmail   = get_env("GMAIL_ADDRESS")
    pw      = get_env("GMAIL_APP_PASSWORD")
    notify  = get_env("NOTIFY_EMAIL")
    api_key = get_env("TWOCAPTCHA_API_KEY")
    daily   = os.environ.get("DAILY_SUMMARY", "false").lower() == "true"

    owner = {k: get_env(v) for k, v in {
        "crn":        "WOVI_CRN",
        "first_name": "WOVI_FIRST_NAME",
        "last_name":  "WOVI_LAST_NAME",
        "address":    "WOVI_ADDRESS",
        "suburb":     "WOVI_SUBURB",
        "postcode":   "WOVI_POSTCODE",
        "email":      "WOVI_EMAIL",
        "phone":      "WOVI_PHONE",
    }.items()}

    def veh(n):
        return {k: get_env(f"WOVI_V{n}_{v}") for k, v in {
            "type": "VEHICLE_TYPE", "vin": "VIN", "make": "MAKE",
            "model": "MODEL", "year": "YEAR", "colour": "COLOUR",
            "build_month": "BUILD_MONTH", "damage": "DAMAGE",
            "purchase_method": "PURCHASE_METHOD", "purchased_from": "PURCHASED_FROM",
        }.items()}

    vehicles = [
        ("Vehicle 1", veh(1), parse_cutoff(get_env("WOVI_V1_CUTOFF_DATE"))),
        ("Vehicle 2", veh(2), parse_cutoff(get_env("WOVI_V2_CUTOFF_DATE"))),
    ]

    state = load_state()

    # Check all locations with single browser session
    driver      = make_driver()
    booking_jobs = []
    try:
        driver.get(BOOKING_URL)
        time.sleep(3)
        for label, vehicle, cutoff in vehicles:
            # Use earlier date if we already have a booking
            key = label.lower().replace(" ", "_") + "_booked_date"
            if state.get(key):
                try:
                    bd = datetime.strptime(state[key], "%Y-%m-%d")
                    if bd < cutoff: cutoff = bd
                except ValueError:
                    pass

            log(f"Checking {label} — cutoff {cutoff.strftime('%d/%m/%Y')}")
            slots = find_slots(driver, cutoff, label)
            now   = now_adelaide()

            if not slots:
                log(f"{label}: no earlier slots.")
                write_csv(now, label, "All locations", "No earlier slots")
            else:
                dt, ds, loc = slots[0]
                log(f"{label}: earliest slot {ds} at {loc}")
                write_csv(now, label, loc, "Earlier slot found", ds)
                booking_jobs.append((label, vehicle, dt, ds, loc))
    finally:
        try: driver.quit()
        except Exception: pass

    # Book any earlier slots found
    for label, vehicle, dt, ds, loc in booking_jobs:
        now = now_adelaide()
        key = label.lower().replace(" ", "_") + "_booked_date"
        confirmed = book_slot(loc, ds, owner, vehicle, api_key, label)
        if confirmed:
            state[key] = dt.strftime("%Y-%m-%d")
            save_state(state)
            write_csv(now, label, loc, "BOOKED", ds)
            send_email(
                f"WOVI BOOKED — {label} — {loc} {ds}",
                f"Rescheduled successfully!\n\n"
                f"Vehicle:  {label} ({vehicle['make']} {vehicle['model']})\n"
                f"Location: {loc}\nDate:     {ds}\n\n"
                f"Verify at wovi.com.au or call 1300 722 411",
                gmail, pw, notify
            )
        else:
            write_csv(now, label, loc, "BOOKING FAILED", ds)
            log(f"{label}: booking failed — logged.")

    if daily:
        send_daily_summary(gmail, pw, notify)

    log("All done.")


if __name__ == "__main__":
    run()
