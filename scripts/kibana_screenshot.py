"""Save a screenshot of the dashboard. Usage: kibana_screenshot.py KIBANA_URL OUTPUT"""

import sys

from playwright.sync_api import sync_playwright

URL = "/app/dashboards#/view/guardian-overview?_g=(time:(from:now-1h,to:now))"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.goto(sys.argv[1].rstrip("/") + URL, timeout=120_000)
    page.wait_for_selector("[data-test-subj='dashboardPanel'], .dshDashboardGrid__item")
    page.wait_for_load_state("networkidle", timeout=120_000)
    page.wait_for_timeout(15_000)
    page.screenshot(path=sys.argv[2])
    browser.close()
