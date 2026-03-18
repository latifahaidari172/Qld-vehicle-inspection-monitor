#!/usr/bin/env python3
"""
WOVI Booking Monitor — Queensland Inspection Services
Monitors Brisbane, Burleigh Heads, Narangba, Yatala for 2 vehicles.
Auto-books earliest slot before each vehicle's cutoff date.
Emails confirmation when booked.
"""

import csv
import os
import re
import smtplib
import sys
import time
import requests
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException
)

# ─────────────────────────────────────────────────────────────────────────────

BOOKING_URL = "https://wovi.com.au/bookings/"
CSV_FILE   = Path(__file__).parent / "wovi_results.csv"
STATE_FILE = Path(__file__).parent / "wovi_state.json"

LOCATIONS = [
    "Brisbane",
    "Burleigh Heads",
    "Narangba",
    "Yatala",
]

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def get_env(key: str, required: bool = True) -> str:
    val = os.environ.get(key, "").strip()
    if required and not val:
        log(f"Missing required secret: {key}", "ERROR")
        sys.exit(1)
    return val


def parse_cutoff(date_str: str) -> datetime:
    for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            pass
    log(f"Could not parse cutoff date: {date_str}", "ERROR")
    sys.exit(1)

# ── CSV ───────────────────────────────────────────────────────────────────────

def write_csv(now: datetime, vehicle: str, location: str,
              result: str, detail: str = ""):
    exists = CSV_FILE.exists()
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["Date", "Time", "Vehicle", "Location", "Result", "Detail"])
        w.writerow([
            now.strftime("%d/%m/%Y"),
            now.strftime("%H:%M:%S"),
            vehicle,
            location,
            result,
            detail
        ])

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str, gmail_address: str,
               app_password: str, notify_email: str):
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = gmail_address
    msg["To"]      = notify_email
    try:
        log(f"Sending email: {subject}")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, app_password)
            server.sendmail(gmail_address, notify_email, msg.as_string())
        log("Email sent.")
    except Exception as e:
        log(f"Email failed: {e}", "ERROR")

# ── WebDriver ─────────────────────────────────────────────────────────────────

def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--log-level=3")
    opts.binary_location = "/usr/bin/google-chrome"
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)
    return driver

# ── 2captcha ──────────────────────────────────────────────────────────────────

def solve_recaptcha(api_key: str, site_key: str, page_url: str) -> str | None:
    log("Submitting CAPTCHA to 2captcha...")
    try:
        resp = requests.post("http://2captcha.com/in.php", data={
            "key":       api_key,
            "method":    "userrecaptcha",
            "googlekey": site_key,
            "pageurl":   page_url,
            "json":      1,
        }, timeout=30)
        result = resp.json()
        if result.get("status") != 1:
            log(f"2captcha submission failed: {result}", "ERROR")
            return None

        captcha_id = result["request"]
        log(f"CAPTCHA submitted (id={captcha_id}). Waiting for solution...")

        for attempt in range(24):
            time.sleep(5)
            poll = requests.get("http://2captcha.com/res.php", params={
                "key":    api_key,
                "action": "get",
                "id":     captcha_id,
                "json":   1,
            }, timeout=10)
            pr = poll.json()
            if pr.get("status") == 1:
                log("CAPTCHA solved!")
                return pr["request"]
            elif pr.get("request") == "CAPCHA_NOT_READY":
                log(f"  Waiting... (attempt {attempt+1}/24)")
                continue
            else:
                log(f"2captcha error: {pr}", "ERROR")
                return None

        log("CAPTCHA solving timed out.", "ERROR")
        return None
    except Exception as e:
        log(f"2captcha error: {e}", "ERROR")
        return None

# ── Date parser ───────────────────────────────────────────────────────────────

def parse_date(text: str) -> datetime | None:
    text = text.strip()
    for fmt in [
        "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y",
        "%B %d, %Y", "%d %B %Y", "%b %d, %Y", "%d %b %Y",
    ]:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    m = re.search(r'(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})', text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r'(\d{4})[\/\-](\d{2})[\/\-](\d{2})', text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None

# ── Helper: fill input field ──────────────────────────────────────────────────

def fill_field(driver, value: str, *names):
    for name in names:
        for attr in ["name", "id", "ng-model"]:
            try:
                el = driver.find_element(By.XPATH, f"//input[@{attr}='{name}']")
                el.clear()
                el.send_keys(value)
                return True
            except NoSuchElementException:
                pass
    log(f"Field not found for: {names}", "WARN")
    return False

# ── Helper: click next/submit ─────────────────────────────────────────────────

def click_next(driver, wait):
    try:
        btn = wait.until(EC.element_to_be_clickable((By.XPATH,
            "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            "'abcdefghijklmnopqrstuvwxyz'),'next')] | "
            "//input[@type='submit'] | "
            "//button[@type='submit'] | "
            "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            "'abcdefghijklmnopqrstuvwxyz'),'submit')]"
        )))
        btn.click()
        time.sleep(2)
        return True
    except TimeoutException:
        log("Next/Submit button not found", "WARN")
        return False

# ── Step 1: Check all locations for a vehicle ─────────────────────────────────

def find_earliest_slot(cutoff: datetime, vehicle_label: str) -> tuple | None:
    """
    Opens WOVI bookings page, checks all 4 locations for dates before cutoff.
    Returns (datetime, date_text, location) for the earliest slot found,
    or None if nothing available.
    """
    driver = make_driver()
    wait   = WebDriverWait(driver, 20)
    all_slots = []

    try:
        log(f"[{vehicle_label}] Loading WOVI bookings page...")
        driver.get(BOOKING_URL)
        time.sleep(4)
        log(f"[{vehicle_label}] Page title: {driver.title}")

        for location in LOCATIONS:
            log(f"[{vehicle_label}] Checking: {location}")
            try:
                # Find location dropdown
                loc_sel = wait.until(EC.presence_of_element_located((By.XPATH,
                    "//select[.//option[contains(text(),'Brisbane') or "
                    "contains(text(),'Eagle Farm') or "
                    "contains(text(),'Yatala') or "
                    "contains(text(),'Narangba') or "
                    "contains(text(),'Burleigh')]]"
                )))
                select = Select(loc_sel)

                # Find and select matching location
                matched = False
                for opt in select.options:
                    if location.lower() in opt.text.lower():
                        select.select_by_visible_text(opt.text)
                        log(f"  Selected: {opt.text}")
                        matched = True
                        break

                if not matched:
                    log(f"  Location '{location}' not in dropdown — available: "
                        f"{[o.text for o in select.options]}", "WARN")
                    continue

                time.sleep(5)  # wait for calendar to load

                # Wait until at least one td.day appears (up to 10s)
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.XPATH,
                            "//td[contains(@class,'day')]"
                        ))
                    )
                except TimeoutException:
                    log(f"  Calendar did not load for {location}", "WARN")

                # Log ALL day cells including disabled so we can see what's there
                all_cells = driver.find_elements(By.XPATH, "//td[contains(@class,'day')]")
                log(f"  Total day cells (incl disabled): {len(all_cells)}")
                if all_cells:
                    sample = all_cells[0]
                    log(f"  Sample cell class='{sample.get_attribute('class')}' text='{sample.text}'")
                    log(f"  Sample cell data-date='{sample.get_attribute('data-date')}'")

                # Read available (non-disabled) calendar dates
                date_cells = driver.find_elements(By.XPATH,
                    "//td[contains(@class,'day') "
                    "and not(contains(@class,'disabled')) "
                    "and not(contains(@class,'old')) "
                    "and not(contains(@class,'new')) "
                    "and not(contains(@class,'unavailable'))] | "
                    "//div[contains(@class,'day') and contains(@class,'available')]"
                )
                log(f"  Available date cells: {len(date_cells)}")

                for cell in date_cells:
                    date_text = (
                        cell.get_attribute("data-date") or
                        cell.get_attribute("data-day") or
                        cell.get_attribute("title") or
                        cell.text.strip()
                    )
                    if not date_text:
                        continue
                    dt = parse_date(date_text)
                    if dt and dt < cutoff:
                        log(f"  ✅ Earlier slot at {location}: {date_text}")
                        all_slots.append((dt, date_text, location))

            except TimeoutException:
                log(f"  Timed out for {location}", "WARN")
            except Exception as e:
                log(f"  Error for {location}: {e}", "WARN")

    except Exception as e:
        log(f"[{vehicle_label}] Page load error: {e}", "ERROR")
    finally:
        driver.quit()

    if not all_slots:
        return None

    # Return the earliest slot across all locations
    all_slots.sort(key=lambda x: x[0])
    return all_slots[0]

# ── Step 2: Book the slot ─────────────────────────────────────────────────────

def book_slot(location: str, date_dt: datetime, date_text: str,
              owner: dict, vehicle: dict, api_key: str,
              vehicle_label: str) -> bool:
    """
    Complete the full WOVI booking form for the given slot.
    Returns True if booking confirmed.
    """
    driver = make_driver()
    wait   = WebDriverWait(driver, 20)

    try:
        log(f"[{vehicle_label}] Starting booking: {location} on {date_text}")
        driver.get(BOOKING_URL)
        time.sleep(4)

        # ── Location ──────────────────────────────────────────────────────────
        loc_sel = wait.until(EC.presence_of_element_located((By.XPATH,
            "//select[.//option[contains(text(),'Brisbane') or "
            "contains(text(),'Yatala') or contains(text(),'Narangba') or "
            "contains(text(),'Burleigh')]]"
        )))
        select = Select(loc_sel)
        for opt in select.options:
            if location.lower() in opt.text.lower():
                select.select_by_visible_text(opt.text)
                break
        time.sleep(3)

        # ── Click target date on calendar ─────────────────────────────────────
        target_day = str(date_dt.day)
        date_cells = driver.find_elements(By.XPATH,
            f"//td[contains(@class,'day') "
            f"and not(contains(@class,'disabled')) "
            f"and normalize-space(text())='{target_day}']"
        )
        clicked = False
        for cell in date_cells:
            try:
                cell.click()
                log(f"[{vehicle_label}] Clicked date: {target_day}")
                clicked = True
                time.sleep(2)
                break
            except Exception:
                continue

        if not clicked:
            log(f"[{vehicle_label}] Could not click date!", "ERROR")
            return False

        # ── Select earliest time ──────────────────────────────────────────────
        try:
            time_sel = driver.find_element(By.XPATH,
                "//select[contains(@ng-model,'time') or "
                "contains(@ng-change,'time') or contains(@id,'time')]"
            )
            time_select = Select(time_sel)
            real_times = [o for o in time_select.options
                          if o.get_attribute("value") not in
                          ("", "null", "undefined", "0")]
            if real_times:
                # Pick earliest time (6:30am first if available)
                real_times.sort(key=lambda o: o.text)
                time_select.select_by_visible_text(real_times[0].text)
                log(f"[{vehicle_label}] Selected time: {real_times[0].text}")
        except NoSuchElementException:
            log(f"[{vehicle_label}] Time select not found", "WARN")

        time.sleep(1)
        click_next(driver, wait)
        time.sleep(2)

        # ── Vehicle details ───────────────────────────────────────────────────
        log(f"[{vehicle_label}] Filling vehicle details...")

        # Vehicle type radio
        vtype = vehicle["type"].lower()
        try:
            radio = driver.find_element(By.XPATH,
                f"//input[@type='radio'][@value='{vehicle['type']}' or "
                f"@id='{vtype}' or following-sibling::*"
                f"[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                f"'abcdefghijklmnopqrstuvwxyz'),'{vtype}')]]"
            )
            radio.click()
        except NoSuchElementException:
            log(f"[{vehicle_label}] Vehicle type radio not found", "WARN")

        fill_field(driver, vehicle["vin"],
                   "vin", "chassis", "VIN", "vinChassis")
        fill_field(driver, vehicle["make"],
                   "make", "vehicleMake")
        fill_field(driver, vehicle["model"],
                   "model", "vehicleModel")
        fill_field(driver, vehicle["year"],
                   "year", "buildYear", "vehicleYear")
        fill_field(driver, vehicle["colour"],
                   "colour", "color", "vehicleColour")
        fill_field(driver, vehicle["purchased_from"],
                   "purchasedFrom", "purchased_from", "sellerName", "purchasedFromName")

        # Damage dropdown
        try:
            dmg = driver.find_element(By.XPATH,
                "//select[contains(@ng-model,'damage') or "
                "contains(@id,'damage') or contains(@name,'damage')]"
            )
            Select(dmg).select_by_visible_text(vehicle["damage"])
        except Exception as e:
            log(f"[{vehicle_label}] Damage field: {e}", "WARN")

        # Purchase method dropdown
        try:
            pm = driver.find_element(By.XPATH,
                "//select[contains(@ng-model,'purchase') or "
                "contains(@id,'purchase') or contains(@name,'purchase')]"
            )
            Select(pm).select_by_visible_text(vehicle["purchase_method"])
        except Exception as e:
            log(f"[{vehicle_label}] Purchase method: {e}", "WARN")

        click_next(driver, wait)
        time.sleep(2)

        # ── Customer details ──────────────────────────────────────────────────
        log(f"[{vehicle_label}] Filling customer details...")
        fill_field(driver, owner["crn"],
                   "crn", "CRN", "licenceNumber", "licence", "crnLicence")
        fill_field(driver, owner["first_name"],
                   "firstName", "first_name", "firstname", "fname")
        fill_field(driver, owner["last_name"],
                   "lastName", "last_name", "lastname", "surname", "lname")
        fill_field(driver, owner["address"],
                   "address", "streetAddress", "street", "streetAddress1")
        fill_field(driver, owner["suburb"],
                   "suburb", "city", "town")
        fill_field(driver, owner["postcode"],
                   "postcode", "zipCode", "zip", "postalCode")
        fill_field(driver, owner["email"],
                   "email", "emailAddress", "Email")
        fill_field(driver, owner["phone"],
                   "phone", "mobile", "mobileNumber", "phoneNumber")

        click_next(driver, wait)
        time.sleep(2)

        # ── Solve CAPTCHA ─────────────────────────────────────────────────────
        log(f"[{vehicle_label}] Looking for CAPTCHA...")
        site_key = None
        try:
            iframe = driver.find_element(By.XPATH,
                "//iframe[contains(@src,'recaptcha')]"
            )
            src = iframe.get_attribute("src") or ""
            m   = re.search(r'[?&]k=([^&]+)', src)
            if m:
                site_key = m.group(1)
        except NoSuchElementException:
            pass

        if not site_key:
            m = re.search(r'data-sitekey=["\']([^"\']+)["\']',
                          driver.page_source)
            if m:
                site_key = m.group(1)

        if not site_key:
            log(f"[{vehicle_label}] reCAPTCHA site key not found!", "ERROR")
            return False

        log(f"[{vehicle_label}] Site key: {site_key[:20]}...")
        token = solve_recaptcha(api_key, site_key, BOOKING_URL)

        if not token:
            log(f"[{vehicle_label}] CAPTCHA solving failed!", "ERROR")
            return False

        # Inject token
        driver.execute_script(
            'var el = document.getElementById("g-recaptcha-response");'
            'if(el) el.innerHTML = arguments[0];', token
        )
        driver.execute_script(
            'var el = document.querySelector("[name=\'g-recaptcha-response\']");'
            'if(el) el.value = arguments[0];', token
        )
        log(f"[{vehicle_label}] CAPTCHA token injected.")
        time.sleep(1)

        # ── Submit ────────────────────────────────────────────────────────────
        log(f"[{vehicle_label}] Submitting...")
        click_next(driver, wait)
        time.sleep(4)

        # ── Handle "update booking" popup ─────────────────────────────────────
        try:
            update_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH,
                    "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    "'abcdefghijklmnopqrstuvwxyz'),'update booking') or "
                    "contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    "'abcdefghijklmnopqrstuvwxyz'),'update your booking')]"
                ))
            )
            log(f"[{vehicle_label}] Clicking 'Update Booking'...")
            update_btn.click()
            time.sleep(3)
        except TimeoutException:
            log(f"[{vehicle_label}] No update popup — may be new booking.", "WARN")

        # ── Check confirmation ─────────────────────────────────────────────────
        page = driver.page_source.lower()
        confirmed = any(w in page for w in [
            "booking has been secured", "booking number",
            "confirmed", "success", "thank you", "submitted"
        ])

        if confirmed:
            log(f"[{vehicle_label}] BOOKING CONFIRMED at {location} on {date_text}!")
            return True
        else:
            log(f"[{vehicle_label}] Submitted but confirmation unclear.", "WARN")
            return False

    except Exception as e:
        log(f"[{vehicle_label}] Booking error: {e}", "ERROR")
        import traceback
        log(traceback.format_exc(), "DEBUG")
        return False
    finally:
        driver.quit()

# ── Process one vehicle ───────────────────────────────────────────────────────

def process_vehicle(vehicle_label: str, vehicle: dict, owner: dict,
                    cutoff: datetime, api_key: str,
                    gmail_addr: str, gmail_pass: str, notify_addr: str):
    log(f"{'='*55}")
    log(f"Processing {vehicle_label} — cutoff: {cutoff.strftime('%d/%m/%Y')}")
    log(f"{'='*55}")
    now = datetime.now()

    # Step 1: Find earliest slot
    result = find_earliest_slot(cutoff, vehicle_label)

    if result is None:
        log(f"[{vehicle_label}] No earlier slots found.")
        for location in LOCATIONS:
            write_csv(now, vehicle_label, location, "No earlier slots", "")
        return

    earliest_dt, earliest_text, earliest_location = result
    log(f"[{vehicle_label}] Best slot: {earliest_text} at {earliest_location}")
    write_csv(now, vehicle_label, earliest_location,
              "Earlier slot found", earliest_text)

    # Step 2: Book it
    confirmed = book_slot(
        earliest_location, earliest_dt, earliest_text,
        owner, vehicle, api_key, vehicle_label
    )

    if confirmed:
        write_csv(now, vehicle_label, earliest_location, "BOOKED", earliest_text)
        send_email(
            f"✅ WOVI BOOKED — {vehicle_label} — {earliest_location} {earliest_text}",
            f"Your WOVI inspection has been automatically rescheduled!\n\n"
            f"Vehicle:   {vehicle_label} ({vehicle['make']} {vehicle['model']})\n"
            f"Location:  {earliest_location}\n"
            f"Date/Time: {earliest_text}\n\n"
            f"Please check your WOVI confirmation email.\n\n"
            f"If anything looks wrong contact WOVI immediately:\n"
            f"Ph: 1300 722 411\n"
            f"Email: adminqis@wovi.com.au\n"
            f"Site: {BOOKING_URL}",
            gmail_addr, gmail_pass, notify_addr
        )
    else:
        write_csv(now, vehicle_label, earliest_location,
                  "BOOKING FAILED", earliest_text)
        send_email(
            f"⚠️ WOVI slot found but booking failed — {vehicle_label} — act now!",
            f"An earlier slot was found but auto-booking failed.\n\n"
            f"Vehicle:  {vehicle_label} ({vehicle['make']} {vehicle['model']})\n"
            f"Location: {earliest_location}\n"
            f"Date:     {earliest_text}\n\n"
            f"Please book manually NOW:\n{BOOKING_URL}\n\n"
            f"Slot may still be available — act quickly!",
            gmail_addr, gmail_pass, notify_addr
        )

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    log("=" * 55)
    log("WOVI Booking Monitor — checking both vehicles")
    log("=" * 55)

    # Shared credentials
    gmail_addr  = get_env("GMAIL_ADDRESS")
    gmail_pass  = get_env("GMAIL_APP_PASSWORD")
    notify_addr = get_env("NOTIFY_EMAIL")
    api_key     = get_env("TWOCAPTCHA_API_KEY")

    # Shared owner details
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

    # Vehicle 1
    v1 = {
        "type":            get_env("WOVI_V1_VEHICLE_TYPE"),
        "vin":             get_env("WOVI_V1_VIN"),
        "make":            get_env("WOVI_V1_MAKE"),
        "model":           get_env("WOVI_V1_MODEL"),
        "year":            get_env("WOVI_V1_YEAR"),
        "colour":          get_env("WOVI_V1_COLOUR"),
        "damage":          get_env("WOVI_V1_DAMAGE"),
        "purchase_method": get_env("WOVI_V1_PURCHASE_METHOD"),
        "purchased_from":  get_env("WOVI_V1_PURCHASED_FROM"),
    }
    cutoff1 = parse_cutoff(get_env("WOVI_V1_CUTOFF_DATE"))

    # Vehicle 2
    v2 = {
        "type":            get_env("WOVI_V2_VEHICLE_TYPE"),
        "vin":             get_env("WOVI_V2_VIN"),
        "make":            get_env("WOVI_V2_MAKE"),
        "model":           get_env("WOVI_V2_MODEL"),
        "year":            get_env("WOVI_V2_YEAR"),
        "colour":          get_env("WOVI_V2_COLOUR"),
        "damage":          get_env("WOVI_V2_DAMAGE"),
        "purchase_method": get_env("WOVI_V2_PURCHASE_METHOD"),
        "purchased_from":  get_env("WOVI_V2_PURCHASED_FROM"),
    }
    cutoff2 = parse_cutoff(get_env("WOVI_V2_CUTOFF_DATE"))

    # Run both vehicles
    process_vehicle("Vehicle 1", v1, owner, cutoff1,
                    api_key, gmail_addr, gmail_pass, notify_addr)

    process_vehicle("Vehicle 2", v2, owner, cutoff2,
                    api_key, gmail_addr, gmail_pass, notify_addr)

    log("All done.")


if __name__ == "__main__":
    run()


# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    import json
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    import json
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log(f"State saved: {state}")


# ── Patched process_vehicle with state tracking ───────────────────────────────

_original_process = process_vehicle


def process_vehicle(vehicle_label: str, vehicle: dict, owner: dict,
                    cutoff: datetime, api_key: str,
                    gmail_addr: str, gmail_pass: str, notify_addr: str):

    state     = load_state()
    state_key = vehicle_label.lower().replace(" ", "_") + "_booked_date"

    # If we already booked an earlier date, use that as the new cutoff
    if state.get(state_key):
        try:
            booked_dt = datetime.strptime(state[state_key], "%Y-%m-%d")
            if booked_dt < cutoff:
                log(f"[{vehicle_label}] Already booked {state[state_key]} — "
                    f"using as new cutoff instead of {cutoff.strftime('%d/%m/%Y')}")
                cutoff = booked_dt
        except ValueError:
            pass

    log(f"{'='*55}")
    log(f"Processing {vehicle_label} — looking for slots before "
        f"{cutoff.strftime('%d/%m/%Y')}")
    log(f"{'='*55}")
    now = datetime.now()

    # Find earliest slot
    result = find_earliest_slot(cutoff, vehicle_label)

    if result is None:
        log(f"[{vehicle_label}] No earlier slots found.")
        write_csv(now, vehicle_label, "All locations", "No earlier slots", "")
        return

    earliest_dt, earliest_text, earliest_location = result
    log(f"[{vehicle_label}] Best slot: {earliest_text} at {earliest_location}")
    write_csv(now, vehicle_label, earliest_location,
              "Earlier slot found", earliest_text)

    # Book it
    confirmed = book_slot(
        earliest_location, earliest_dt, earliest_text,
        owner, vehicle, api_key, vehicle_label
    )

    if confirmed:
        # Save new booked date as the updated cutoff for future runs
        state[state_key] = earliest_dt.strftime("%Y-%m-%d")
        save_state(state)

        write_csv(now, vehicle_label, earliest_location, "BOOKED", earliest_text)
        send_email(
            f"✅ WOVI BOOKED — {vehicle_label} — {earliest_location} {earliest_text}",
            f"Your WOVI inspection has been automatically rescheduled!\n\n"
            f"Vehicle:   {vehicle_label} ({vehicle['make']} {vehicle['model']})\n"
            f"Location:  {earliest_location}\n"
            f"Date/Time: {earliest_text}\n\n"
            f"Next run will now look for slots even earlier than {earliest_text}.\n\n"
            f"Please check your WOVI confirmation email.\n\n"
            f"If anything looks wrong contact WOVI immediately:\n"
            f"Ph: 1300 722 411\n"
            f"Email: adminqis@wovi.com.au\n"
            f"Site: {BOOKING_URL}",
            gmail_addr, gmail_pass, notify_addr
        )
    else:
        write_csv(now, vehicle_label, earliest_location,
                  "BOOKING FAILED", earliest_text)
        send_email(
            f"⚠️ WOVI slot found but booking failed — {vehicle_label} — act now!",
            f"An earlier slot was found but auto-booking failed.\n\n"
            f"Vehicle:  {vehicle_label} ({vehicle['make']} {vehicle['model']})\n"
            f"Location: {earliest_location}\n"
            f"Date:     {earliest_text}\n\n"
            f"Please book manually NOW:\n{BOOKING_URL}\n\n"
            f"Slot may still be available — act quickly!",
            gmail_addr, gmail_pass, notify_addr
        )


# ── Daily 5pm summary ─────────────────────────────────────────────────────────

def send_daily_summary(gmail_addr: str, gmail_pass: str, notify_addr: str):
    import json
    now       = datetime.now()
    today_str = now.strftime("%d/%m/%Y")

    # Count today's checks and any slots found from CSV
    today_checks     = 0
    slots_found      = []
    bookings_made    = []

    if CSV_FILE.exists():
        with open(CSV_FILE, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("Date") == today_str:
                    today_checks += 1
                    result = row.get("Result", "")
                    if result == "Earlier slot found":
                        slots_found.append(
                            f"  {row['Time']} — {row['Vehicle']} at "
                            f"{row['Location']}: {row['Detail']}"
                        )
                    elif result == "BOOKED":
                        bookings_made.append(
                            f"  {row['Time']} — {row['Vehicle']} at "
                            f"{row['Location']}: {row['Detail']}"
                        )

    # Load current cutoffs from state
    state = load_state()
    v1_current = state.get("vehicle_1_booked_date", "Not yet rescheduled")
    v2_current = state.get("vehicle_2_booked_date", "Not yet rescheduled")

    # Build email body
    slots_section = (
        "\n".join(slots_found)
        if slots_found
        else "  No earlier slots found at any location today."
    )
    bookings_section = (
        "\n".join(bookings_made)
        if bookings_made
        else "  No bookings made today."
    )

    body = (
        f"WOVI Daily Summary — {today_str}\n"
        f"{'=' * 45}\n\n"
        f"Total checks run today: {today_checks}\n\n"
        f"Current booking status:\n"
        f"  BYD  (Vehicle 1): {v1_current}\n"
        f"  Kia  (Vehicle 2): {v2_current}\n\n"
        f"Earlier slots seen today:\n{slots_section}\n\n"
        f"Bookings made today:\n{bookings_section}\n\n"
        f"Locations monitored: {', '.join(LOCATIONS)}\n\n"
        f"The monitor checks every 1 min (6:30–10am) and every\n"
        f"5 min (rest of day) and will auto-reschedule when an\n"
        f"earlier slot is found.\n\n"
        f"View full history: open wovi_results.csv in your\n"
        f"GitHub repository."
    )

    send_email(
        f"📋 WOVI Daily Summary — {today_str}",
        body,
        gmail_addr, gmail_pass, notify_addr
    )
    log("Daily summary email sent.")


# ── Patch run() to include daily summary ──────────────────────────────────────

_original_run = run


def run():
    daily_summary = os.environ.get("DAILY_SUMMARY", "false").lower() == "true"

    if not daily_summary:
        # Normal check run
        _original_run()
    else:
        # Daily summary run — still do a check AND send summary
        _original_run()
        gmail_addr  = get_env("GMAIL_ADDRESS")
        gmail_pass  = get_env("GMAIL_APP_PASSWORD")
        notify_addr = get_env("NOTIFY_EMAIL")
        send_daily_summary(gmail_addr, gmail_pass, notify_addr)

# ── Daily summary ─────────────────────────────────────────────────────────────

def send_daily_summary(gmail_addr: str, gmail_pass: str, notify_addr: str):
    now       = datetime.now()
    today_str = now.strftime("%d/%m/%Y")

    v1_slots  = []
    v2_slots  = []
    v1_checks = 0
    v2_checks = 0

    if CSV_FILE.exists():
        with open(CSV_FILE, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("Date") != today_str:
                    continue
                vehicle = row.get("Vehicle", "")
                result  = row.get("Result", "")
                detail  = row.get("Detail", "")
                loc     = row.get("Location", "")

                if "1" in vehicle:
                    v1_checks += 1
                    if result not in ("No earlier slots", "Error", ""):
                        v1_slots.append(f"  {row['Time']} — {loc}: {result} {detail}")
                elif "2" in vehicle:
                    v2_checks += 1
                    if result not in ("No earlier slots", "Error", ""):
                        v2_slots.append(f"  {row['Time']} — {loc}: {result} {detail}")

    def section(label, checks, slots):
        if slots:
            return f"{label}:\nChecks today: {checks}\nSlots seen:\n" + "\n".join(slots)
        return f"{label}:\nChecks today: {checks}\nNo earlier slots seen today."

    body = (
        f"WOVI Daily Summary — {today_str}\n"
        f"{'=' * 40}\n\n"
        f"{section('Vehicle 1 (BYD)', v1_checks, v1_slots)}\n\n"
        f"{section('Vehicle 2 (Kia)', v2_checks, v2_slots)}\n\n"
        f"Monitor checks every 1 min (6:30–10am) and 5 mins (rest of day).\n"
        f"Will auto-book the earliest slot found before each cutoff date.\n\n"
        f"View full history: open wovi_results.csv in your GitHub repository."
    )

    send_email(
        f"📋 WOVI Daily Summary — {today_str}",
        body,
        gmail_addr, gmail_pass, notify_addr
    )
    log("Daily summary email sent.")
