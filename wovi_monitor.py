#!/usr/bin/env python3
"""
WOVI Booking Monitor — Queensland Inspection Services
Checks Brisbane, Burleigh Heads, Narangba, Yatala for 2 vehicles.
Auto-books earliest slot before each vehicle's cutoff date via 2captcha.
Emails confirmation when booked. Logs every check to wovi_results.csv.
"""

import csv
import json
import os
import re
import smtplib
import sys
import time
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

# ─────────────────────────────────────────────────────────────────────────────

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

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    ts = now_adelaide().strftime("%d/%m/%Y %I:%M:%S %p")
    prefix = f"[{level}] " if level != "INFO" else ""
    print(f"[{ts}] {prefix}{msg}", flush=True)

# ── Env ───────────────────────────────────────────────────────────────────────

def get_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        log(f"Missing secret: {key}", "ERROR")
        sys.exit(1)
    return val

def parse_cutoff(s: str) -> datetime:
    for fmt in ["%d/%m/%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    log(f"Cannot parse date: {s}", "ERROR")
    sys.exit(1)

def format_dob(dob: str) -> str:
    return "".join(c for c in dob if c.isdigit())

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── CSV ───────────────────────────────────────────────────────────────────────

def write_csv(t: datetime, vehicle: str, location: str, result: str, detail: str = ""):
    exists = CSV_FILE.exists()
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["Date", "Time", "Vehicle", "Location", "Result", "Detail"])
        w.writerow([t.strftime("%d/%m/%Y"), t.strftime("%I:%M:%S %p"),
                    vehicle, location, result, detail])

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str, gmail: str, password: str, to: str):
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = gmail
    msg["To"]      = to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(gmail, password)
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
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)
    return driver

# ── 2captcha ──────────────────────────────────────────────────────────────────

def solve_captcha(api_key: str) -> str | None:
    log("Submitting CAPTCHA to 2captcha...")
    try:
        r = requests.post("http://2captcha.com/in.php", data={
            "key": api_key, "method": "userrecaptcha",
            "googlekey": RECAPTCHA_SITE_KEY,
            "pageurl": BOOKING_URL, "json": 1,
        }, timeout=30).json()
        if r.get("status") != 1:
            log(f"2captcha error: {r}", "ERROR")
            return None
        cid = r["request"]
        log(f"CAPTCHA submitted (id={cid}), waiting...")
        for _ in range(24):
            time.sleep(5)
            try:
                resp = requests.get("http://2captcha.com/res.php", params={
                    "key": api_key, "action": "get", "id": cid, "json": 1
                }, timeout=10)
                # Handle both JSON and plain text responses
                try:
                    p = resp.json()
                    if p.get("status") == 1:
                        log("CAPTCHA solved!")
                        return p["request"]
                    if p.get("request") != "CAPCHA_NOT_READY":
                        log(f"2captcha error: {p}", "ERROR")
                        return None
                except Exception:
                    # Plain text response e.g. "OK|token" or "CAPCHA_NOT_READY"
                    text = resp.text.strip()
                    if text.startswith("OK|"):
                        log("CAPTCHA solved!")
                        return text[3:]
                    if text != "CAPCHA_NOT_READY":
                        log(f"2captcha response: {text}", "ERROR")
                        return None
            except Exception as ex:
                log(f"Poll error: {ex}", "WARN")
                continue
        log("CAPTCHA timed out", "ERROR")
        return None
    except Exception as e:
        log(f"2captcha exception: {e}", "ERROR")
        return None

# ── Date parser ───────────────────────────────────────────────────────────────

def parse_date(s: str) -> datetime | None:
    for pat, build in [
        (r'(\d{4})-(\d{2})-(\d{2})', lambda m: datetime(int(m[1]), int(m[2]), int(m[3]))),
        (r'(\d{1,2})/(\d{1,2})/(\d{4})', lambda m: datetime(int(m[3]), int(m[2]), int(m[1]))),
    ]:
        m = re.search(pat, s)
        if m:
            try:
                return build(m)
            except ValueError:
                pass
    return None

# ── Helpers ───────────────────────────────────────────────────────────────────

def fill(driver, value: str, *names):
    for name in names:
        for attr in ["name", "id", "ng-model"]:
            try:
                el = driver.find_element(By.XPATH, f"//input[@{attr}='{name}']")
                # Scroll into view first
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.3)
                # Clear and fill via JS to bypass interactability issues
                driver.execute_script("arguments[0].value='';", el)
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

def click_next(driver, wait):
    try:
        btn = wait.until(EC.presence_of_element_located((By.XPATH,
            "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'next')] | "
            "//input[@type='submit'] | //button[@type='submit'] | "
            "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]"
        )))
        # Scroll into view and click via JavaScript to bypass any overlays
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(2)
        return True
    except TimeoutException:
        return False

# ── Step 1: Find slots ────────────────────────────────────────────────────────

def find_slots(driver, cutoff: datetime, vehicle_label: str) -> list:
    wait      = WebDriverWait(driver, 20)
    all_slots = []

    for location in LOCATIONS:
        try:
            loc_sel = wait.until(EC.presence_of_element_located((By.XPATH,
                "//select[.//option[contains(text(),'Brisbane')]]"
            )))
            for opt in Select(loc_sel).options:
                if location.lower() in opt.text.lower():
                    Select(loc_sel).select_by_visible_text(opt.text)
                    break
            else:
                continue

            try:
                WebDriverWait(driver, 8).until(lambda d: len(
                    d.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']")
                ) > 0)
            except TimeoutException:
                log(f"  {location}: calendar timeout")
                continue

            items = driver.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']")
            found = 0
            for item in items:
                try:
                    d = driver.execute_script(
                        "try{var s=angular.element(arguments[0]).scope();"
                        "if(!s||!s.day||!s.day.available||!s.day.thisMonth) return null;"
                        "return s.day.value;}catch(e){return null;}", item
                    )
                    if not d:
                        continue
                    dt = parse_date(d)
                    if dt and dt < cutoff:
                        all_slots.append((dt, d, location))
                        found += 1
                except Exception:
                    continue

            log(f"  {location}: {found} slot(s) before cutoff")

        except Exception as e:
            log(f"  {location}: error — {e}", "WARN")

    all_slots.sort(key=lambda x: x[0])
    return all_slots

# ── Step 2: Book ──────────────────────────────────────────────────────────────

def book_slot(location: str, date_dt: datetime, date_str: str,
              owner: dict, vehicle: dict, api_key: str,
              vehicle_label: str, test_mode: bool = False) -> bool:

    driver = make_driver()
    wait   = WebDriverWait(driver, 20)

    try:
        log(f"[{vehicle_label}] Booking: {location} on {date_str}")
        driver.get(BOOKING_URL)
        time.sleep(3)

        # Select location
        loc_sel = wait.until(EC.presence_of_element_located((By.XPATH,
            "//select[.//option[contains(text(),'Brisbane')]]"
        )))
        for opt in Select(loc_sel).options:
            if location.lower() in opt.text.lower():
                Select(loc_sel).select_by_visible_text(opt.text)
                break
        time.sleep(3)

        # Click target date using Angular scope value
        try:
            WebDriverWait(driver, 8).until(lambda d: len(
                d.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']")
            ) > 0)
        except TimeoutException:
            log(f"[{vehicle_label}] Calendar timeout during booking", "ERROR")
            return False

        clicked = False
        for item in driver.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']"):
            try:
                d = driver.execute_script(
                    "try{var s=angular.element(arguments[0]).scope();"
                    "if(!s||!s.day) return null;return s.day.value;}catch(e){return null;}", item
                )
                if d == date_str:
                    item.click()
                    clicked = True
                    log(f"[{vehicle_label}] Clicked date: {date_str}")
                    time.sleep(2)
                    break
            except Exception:
                continue

        if not clicked:
            log(f"[{vehicle_label}] Could not click date {date_str}", "ERROR")
            return False

        # Select earliest time
        try:
            time_sel = driver.find_element(By.XPATH,
                "//select[contains(@ng-model,'time') or contains(@ng-change,'time')]"
            )
            opts = [o for o in Select(time_sel).options
                    if o.get_attribute("value") not in ("", "null", "undefined", "0")]
            if opts:
                opts.sort(key=lambda o: o.text)
                Select(time_sel).select_by_visible_text(opts[0].text)
                log(f"[{vehicle_label}] Time: {opts[0].text}")
        except NoSuchElementException:
            pass

        time.sleep(1)
        click_next(driver, wait)
        time.sleep(2)

        # Vehicle details — wait for page to be ready
        time.sleep(3)
        log(f"[{vehicle_label}] Filling vehicle details...")
        vtype = vehicle["type"].lower()
        try:
            driver.find_element(By.XPATH,
                f"//input[@type='radio'][@value='{vehicle['type']}' or @id='{vtype}']"
            ).click()
        except NoSuchElementException:
            pass

        fill(driver, vehicle["vin"],            "vin", "chassis", "VIN", "vinChassis")
        fill(driver, vehicle["make"],           "make", "vehicleMake")
        fill(driver, vehicle["model"],          "model", "vehicleModel")
        fill(driver, vehicle["year"],           "year", "buildYear")
        fill(driver, vehicle["colour"],         "colour", "color", "vehicleColour")
        fill(driver, vehicle["purchased_from"], "purchasedFrom", "sellerName")

        try:
            Select(driver.find_element(By.XPATH,
                "//select[contains(@ng-model,'damage') or contains(@name,'damage')]"
            )).select_by_visible_text(vehicle["damage"])
        except Exception:
            pass

        try:
            Select(driver.find_element(By.XPATH,
                "//select[contains(@ng-model,'purchase') or contains(@name,'purchase')]"
            )).select_by_visible_text(vehicle["purchase_method"])
        except Exception:
            pass

        click_next(driver, wait)
        time.sleep(2)

        # Customer details
        log(f"[{vehicle_label}] Filling customer details...")
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

        # Solve CAPTCHA
        log(f"[{vehicle_label}] Solving CAPTCHA...")
        token = solve_captcha(api_key)
        if not token:
            log(f"[{vehicle_label}] CAPTCHA failed", "ERROR")
            return False

        driver.execute_script(
            "var el=document.getElementById('g-recaptcha-response');"
            "if(el) el.innerHTML=arguments[0];", token
        )
        driver.execute_script(
            "var el=document.querySelector('[name=\"g-recaptcha-response\"]');"
            "if(el) el.value=arguments[0];", token
        )
        log(f"[{vehicle_label}] CAPTCHA token injected ✅")
        time.sleep(1)

        # TEST MODE: screenshot and stop
        if test_mode:
            path = Path(__file__).parent / "test_screenshot.png"
            driver.save_screenshot(str(path))
            log(f"[{vehicle_label}] TEST MODE — screenshot saved: test_screenshot.png ✅")
            log(f"[{vehicle_label}] All steps passed! ✅")
            return False

        # Submit
        log(f"[{vehicle_label}] Submitting...")
        click_next(driver, wait)
        time.sleep(4)

        # Handle update popup
        try:
            btn = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH,
                "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'update booking')]"
            )))
            btn.click()
            log(f"[{vehicle_label}] Clicked 'Update Booking'")
            time.sleep(3)
        except TimeoutException:
            log(f"[{vehicle_label}] No update popup", "WARN")

        confirmed = any(w in driver.page_source.lower() for w in
                        ["booking has been secured", "booking number", "confirmed",
                         "success", "thank you", "submitted"])
        if confirmed:
            log(f"[{vehicle_label}] BOOKING CONFIRMED ✅")
        else:
            log(f"[{vehicle_label}] Submitted — confirmation unclear", "WARN")
        return confirmed

    except Exception as e:
        log(f"[{vehicle_label}] Booking error: {e}", "ERROR")
        import traceback
        log(traceback.format_exc(), "DEBUG")
        return False
    finally:
        driver.quit()

# ── Daily summary ─────────────────────────────────────────────────────────────

def send_daily_summary(gmail: str, password: str, to: str):
    now       = now_adelaide()
    today_str = now.strftime("%d/%m/%Y")
    v1_rows, v2_rows = [], []
    total = 0

    if CSV_FILE.exists():
        with open(CSV_FILE, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("Date") != today_str:
                    continue
                total += 1
                if row.get("Result") in ("No earlier slots", ""):
                    continue
                line = f"  {row['Time']} — {row['Location']}: {row['Result']} {row['Detail']}".strip()
                if "1" in row.get("Vehicle", ""):
                    v1_rows.append(line)
                else:
                    v2_rows.append(line)

    def section(label, rows):
        return f"{label}:\n" + ("\n".join(rows) if rows else "No earlier slots seen today.")

    body = (
        f"WOVI Daily Summary — {today_str}\n{'='*40}\n\n"
        f"Total checks: {total}\n\n"
        f"{section('Vehicle 1 (BYD)', v1_rows)}\n\n"
        f"{section('Vehicle 2 (Kia)', v2_rows)}\n\n"
        f"Monitor checks every 1 min (6:30–10am) and 5 mins (rest of day).\n"
        f"Full history: open wovi_results.csv in your GitHub repository."
    )
    send_email(f"WOVI Daily Summary — {today_str}", body, gmail, password, to)

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    log("=" * 55)
    log("WOVI Booking Monitor — checking both vehicles")
    log("=" * 55)

    gmail_addr  = get_env("GMAIL_ADDRESS")
    gmail_pass  = get_env("GMAIL_APP_PASSWORD")
    notify_addr = get_env("NOTIFY_EMAIL")
    api_key     = get_env("TWOCAPTCHA_API_KEY")
    test_mode   = os.environ.get("TEST_MODE", "false").lower() == "true"
    daily_sum   = os.environ.get("DAILY_SUMMARY", "false").lower() == "true"

    owner = {
        "crn":        get_env("WOVI_CRN"),
        "first_name": get_env("WOVI_FIRST_NAME"),
        "last_name":  get_env("WOVI_LAST_NAME"),
        "address":    get_env("WOVI_ADDRESS"),
        "suburb":     get_env("WOVI_SUBURB"),
        "postcode":   get_env("WOVI_POSTCODE"),
        "email":      get_env("WOVI_EMAIL"),
        "phone":      get_env("WOVI_PHONE"),
    }

    vehicles = [
        ("Vehicle 1", {
            "type":            get_env("WOVI_V1_VEHICLE_TYPE"),
            "vin":             get_env("WOVI_V1_VIN"),
            "make":            get_env("WOVI_V1_MAKE"),
            "model":           get_env("WOVI_V1_MODEL"),
            "year":            get_env("WOVI_V1_YEAR"),
            "colour":          get_env("WOVI_V1_COLOUR"),
            "damage":          get_env("WOVI_V1_DAMAGE"),
            "purchase_method": get_env("WOVI_V1_PURCHASE_METHOD"),
            "purchased_from":  get_env("WOVI_V1_PURCHASED_FROM"),
        }, parse_cutoff(get_env("WOVI_V1_CUTOFF_DATE"))),
        ("Vehicle 2", {
            "type":            get_env("WOVI_V2_VEHICLE_TYPE"),
            "vin":             get_env("WOVI_V2_VIN"),
            "make":            get_env("WOVI_V2_MAKE"),
            "model":           get_env("WOVI_V2_MODEL"),
            "year":            get_env("WOVI_V2_YEAR"),
            "colour":          get_env("WOVI_V2_COLOUR"),
            "damage":          get_env("WOVI_V2_DAMAGE"),
            "purchase_method": get_env("WOVI_V2_PURCHASE_METHOD"),
            "purchased_from":  get_env("WOVI_V2_PURCHASED_FROM"),
        }, parse_cutoff(get_env("WOVI_V2_CUTOFF_DATE"))),
    ]

    state = load_state()

    # Single browser for slot checking
    driver = make_driver()
    booking_jobs = []  # collect (vehicle_label, vehicle, slot) to book after checking

    try:
        driver.get(BOOKING_URL)
        time.sleep(3)

        for vehicle_label, vehicle, cutoff in vehicles:
            # Update cutoff from state
            state_key = vehicle_label.lower().replace(" ", "_") + "_booked_date"
            if state.get(state_key):
                try:
                    booked_dt = datetime.strptime(state[state_key], "%Y-%m-%d")
                    if booked_dt < cutoff:
                        cutoff = booked_dt
                except ValueError:
                    pass

            log(f"Checking {vehicle_label} — cutoff {cutoff.strftime('%d/%m/%Y')}")
            slots = find_slots(driver, cutoff, vehicle_label)
            now   = now_adelaide()

            if not slots:
                log(f"{vehicle_label}: no earlier slots.")
                write_csv(now, vehicle_label, "All locations", "No earlier slots", "")
            else:
                earliest_dt, earliest_str, earliest_loc = slots[0]
                log(f"{vehicle_label}: earliest slot {earliest_str} at {earliest_loc}")
                write_csv(now, vehicle_label, earliest_loc, "Earlier slot found", earliest_str)
                booking_jobs.append((vehicle_label, vehicle, earliest_dt, earliest_str, earliest_loc, cutoff))

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # Book each vehicle that found a slot
    for vehicle_label, vehicle, earliest_dt, earliest_str, earliest_loc, cutoff in booking_jobs:
        now       = now_adelaide()
        state_key = vehicle_label.lower().replace(" ", "_") + "_booked_date"

        confirmed = book_slot(
            earliest_loc, earliest_dt, earliest_str,
            owner, vehicle, api_key, vehicle_label, test_mode
        )

        if confirmed:
            state[state_key] = earliest_dt.strftime("%Y-%m-%d")
            save_state(state)
            write_csv(now, vehicle_label, earliest_loc, "BOOKED", earliest_str)
            send_email(
                f"WOVI BOOKED — {vehicle_label} — {earliest_loc} {earliest_str}",
                f"Your WOVI inspection has been rescheduled!\n\n"
                f"Vehicle:  {vehicle_label} ({vehicle['make']} {vehicle['model']})\n"
                f"Location: {earliest_loc}\nDate: {earliest_str}\n\n"
                f"Verify at: {BOOKING_URL}\n"
                f"Contact: 1300 722 411 / adminqis@wovi.com.au",
                gmail_addr, gmail_pass, notify_addr
            )
        elif not test_mode:
            write_csv(now, vehicle_label, earliest_loc, "BOOKING FAILED", earliest_str)
            log(f"{vehicle_label}: booking failed — logged to CSV.")

    if daily_sum:
        send_daily_summary(gmail_addr, gmail_pass, notify_addr)

    log("All done.")


if __name__ == "__main__":
    run()
