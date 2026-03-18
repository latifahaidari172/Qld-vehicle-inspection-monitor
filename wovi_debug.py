#!/usr/bin/env python3
"""
WOVI Debug Script
Loads the booking page, selects Brisbane, waits 15 seconds,
then dumps the full page source and screenshots.
Run this once to figure out the calendar HTML structure.
"""

import time
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException

BOOKING_URL = "https://wovi.com.au/bookings/"

def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.binary_location = "/usr/bin/google-chrome"
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)
    return driver

driver = make_driver()
wait   = WebDriverWait(driver, 20)

print("Loading page...")
driver.get(BOOKING_URL)
time.sleep(5)

print(f"Title: {driver.title}")
print(f"URL: {driver.current_url}")

# Take screenshot of initial page
driver.save_screenshot("debug_01_initial.png")
print("Screenshot saved: debug_01_initial.png")

# Find and select Brisbane
print("\nLooking for location dropdown...")
selects = driver.find_elements(By.TAG_NAME, "select")
print(f"Found {len(selects)} select elements")
for i, sel in enumerate(selects):
    opts = [o.text for o in Select(sel).options]
    print(f"  Select {i}: name='{sel.get_attribute('name')}' options={opts}")

# Try to select Brisbane
for sel in selects:
    for opt in Select(sel).options:
        if "brisbane" in opt.text.lower():
            print(f"\nSelecting Brisbane from dropdown...")
            Select(sel).select_by_visible_text(opt.text)
            break

print("Waiting 15 seconds for calendar to load...")
time.sleep(15)

# Scroll down to calendar area
driver.execute_script("window.scrollTo(0, 600);")
time.sleep(1)
driver.save_screenshot("debug_02_calendar_top.png")
print("Screenshot saved: debug_02_calendar_top.png")

driver.execute_script("window.scrollTo(0, 1200);")
time.sleep(1)
driver.save_screenshot("debug_03_calendar_mid.png")
print("Screenshot saved: debug_03_calendar_mid.png")

driver.execute_script("window.scrollTo(0, 1800);")
time.sleep(1)
driver.save_screenshot("debug_04_calendar_bottom.png")
print("Screenshot saved: debug_04_calendar_bottom.png")

# Also set window very tall to capture everything in one shot
driver.set_window_size(1280, 3000)
time.sleep(1)
driver.execute_script("window.scrollTo(0, 0);")
time.sleep(1)
driver.save_screenshot("debug_05_full_page.png")
print("Screenshot saved: debug_05_full_page.png")

# Save full page source
src = driver.page_source
with open("debug_page_source.html", "w", encoding="utf-8") as f:
    f.write(src)
print(f"Page source saved: {len(src)} chars")

# Look for ANY elements that could be calendar dates
print("\nSearching for calendar elements...")
test_xpaths = [
    ("td.day",           "//td[contains(@class,'day')]"),
    ("div.day",          "//div[contains(@class,'day')]"),
    ("button.day",       "//button[contains(@class,'day')]"),
    ("fc-day",           "//*[contains(@class,'fc-day')]"),
    ("available",        "//*[contains(@class,'available')]"),
    ("ng-click date",    "//*[@ng-click and contains(@ng-click,'date')]"),
    ("ng-click day",     "//*[@ng-click and contains(@ng-click,'day')]"),
    ("data-date",        "//*[@data-date]"),
    ("data-day",         "//*[@data-day]"),
    ("calendar",         "//*[contains(@class,'calendar')]"),
    ("datepicker",       "//*[contains(@class,'datepicker')]"),
    ("picker",           "//*[contains(@class,'picker')]"),
    ("date-picker",      "//*[contains(@class,'date-picker')]"),
    ("flatpickr",        "//*[contains(@class,'flatpickr')]"),
    ("react-datepicker", "//*[contains(@class,'react-datepicker')]"),
]

for label, xpath in test_xpaths:
    elements = driver.find_elements(By.XPATH, xpath)
    if elements:
        print(f"\n  ✅ FOUND {len(elements)} elements for: {label}")
        for el in elements[:3]:
            print(f"     tag={el.tag_name} "
                  f"class='{el.get_attribute('class')}' "
                  f"text='{el.text[:50]}' "
                  f"ng-click='{el.get_attribute('ng-click')}' "
                  f"data-date='{el.get_attribute('data-date')}'")

# Log key parts of page source
print("\n--- PAGE SOURCE SNIPPETS ---")
keywords = ["calendar", "datepicker", "fc-day", "available", "ng-click", "flatpickr", "picker"]
for kw in keywords:
    idx = src.lower().find(kw)
    if idx >= 0:
        print(f"\nKeyword '{kw}' found at position {idx}:")
        print(f"  ...{src[max(0,idx-100):idx+200]}...")

driver.quit()
print("\nDone!")
