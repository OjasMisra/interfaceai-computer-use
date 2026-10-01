"""A scripted stand-in for a human operator.

It does exactly what a person at the console would do, through the same
interfaces: claims the intervention over the console API, attaches to the
*same* live browser over CDP, acts in the page, and resolves. Nothing here
talks to the automation process directly.

Playbooks:
    verify_identity  click "Identity Verified" on the security screen, then resume
    approve          approve the pending irreversible action (no page interaction)
    perform_post     the human clicks "Post" themselves, then resumes
    abort            decline / abort the run
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

from playwright.sync_api import sync_playwright

OPERATOR = "sim-operator (scripted)"


def api(console: str, path: str, body: dict | None = None):
    req = urllib.request.Request(console + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def in_live_page(cdp: str, fn) -> None:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp)
        page = browser.contexts[0].pages[0]
        fn(page)
        page.wait_for_timeout(1200)  # let the navigation land before handing back


def click_in_work(label: str):
    def run(page):
        work = page.frame(name="work") or page.main_frame
        work.get_by_role("button", name=label).click()
    return run


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--console", required=True)
    ap.add_argument("--intervention", required=True)
    ap.add_argument("--playbook", required=True)
    a = ap.parse_args()

    time.sleep(1.5)  # a human takes a moment to pick it up
    iv = api(a.console, f"/api/interventions/{a.intervention}/claim", {"operator": OPERATOR})
    print("claimed", iv["id"], iv["kind"], iv["reason"])

    if a.playbook == "verify_identity":
        in_live_page(iv["cdp_endpoint"], click_in_work("Identity Verified"))
        decision, note = "resume", "Verified member identity by phone; continued past security screen."
    elif a.playbook == "perform_post":
        in_live_page(iv["cdp_endpoint"], click_in_work("Post"))
        decision, note = "resume", "Reviewed the new share details and posted it myself."
    elif a.playbook == "approve":
        decision, note = "approve", "Reviewed member, share type and deposit on the verify screen. Approved."
    else:
        decision, note = "abort", "Declined."

    out = api(a.console, f"/api/interventions/{a.intervention}/resolve",
              {"operator": OPERATOR, "decision": decision, "note": note})
    print("resolved", out["id"], out["decision"], "human_actions:", len(out["human_actions"]))


if __name__ == "__main__":
    main()
