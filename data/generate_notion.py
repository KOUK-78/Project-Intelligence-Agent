#!/usr/bin/env python3
"""Generate the synthetic Notion dataset for Project Atlas (fictional company: Northwind).

Run:  python data/generate_notion.py
Writes:
  data/synthetic/notion_pages.json       (20 pages, NTN-101..NTN-120)
  data/synthetic/notion_pages_stub.json  (3 pages, for building against before integration)

Hand-written, no randomness: output is byte-for-byte reproducible. Every planted scenario is
validated and the script refuses to write if one is missing. Facts must agree with
data/WORLD.md and the Jira data. See data/PRIVATE_NOTES.md for the map.
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

TODAY = "2026-06-30"  # mirror of app/config.py TODAY; self-checks only
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "synthetic"

STATUSES = {"Draft", "In Review", "Approved", "Superseded", "Complete", "Ready to run", "Published"}
PAGE_TYPES = {"spec", "decision_log", "status_report", "planning", "process", "runbook",
              "onboarding", "guideline", "calendar", "support_notes"}
KEYS = ["id", "title", "page_type", "owner", "status", "created_date", "last_edited", "sections"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def p(n, title, ptype, owner, status, created, edited, sections):
    return {"id": f"NTN-{n}", "title": title, "page_type": ptype, "owner": owner, "status": status,
            "created_date": created, "last_edited": edited,
            "sections": [{"heading": h, "text": x} for h, x in sections]}


PRIYA, MARCUS, SOFIA, WEI = "Priya Nair", "Marcus Chen", "Sofia Lindqvist", "Wei Zhang"
ELENA, TOMAS, AISHA, DANIEL, OWEN = "Elena Rossi", "Tomas Alvarez", "Aisha Khan", "Daniel Okafor", "Owen Pierce"

PAGES = [
    p(101, "Atlas Project Overview and Roster", "planning", PRIYA, "Approved", "2026-04-01", "2026-06-01", [
        ("Purpose", "Atlas is Northwind's new checkout and billing platform: catalog, cart, card payments, "
                    "refunds, invoices and a customer account dashboard."),
        ("Team roster", "Priya Nair: product lead. Marcus Chen: backend and payments lead. Sofia Lindqvist: "
                        "frontend and design. Wei Zhang: backend (tax, billing). Elena Rossi: QA. Tomas Alvarez: "
                        "backend (inventory, refunds, data migration). Daniel Okafor: infrastructure and security. "
                        "Aisha Khan: marketing and operations (launch communications, support). Owen Pierce: "
                        "Finance and vendor management. Nadia Haddad: Legal counsel. Rafael Duarte: VP Product, "
                        "chair of the steering committee."),
        ("External vendors", "PayGate provides the payments API. Halberd Security provides the external "
                             "penetration test."),
        ("Where things live", "Tickets are tracked in Jira (ATL project). Specs, plans and decision logs live "
                              "in Notion. Vendor and cross-team discussion happens by email."),
    ]),
    # --- Spec version 1 (disagrees with v2 on the launch date) ---
    p(102, "Atlas Launch Spec v1", "spec", PRIYA, "Approved", "2026-04-20", "2026-05-20", [
        ("Summary", "Version 1 of the Atlas launch specification, approved by product and engineering on 2026-05-20."),
        ("Launch date", "Target public GA date: 2026-09-15. The launch announcement follows GA in the same week."),
        ("Scope at GA", "Catalog and cart, card payments, refunds, invoice generation, customer account "
                        "dashboard, and recurring (subscription) billing."),
        ("Dependencies", "Payments API v2 from PayGate and the external security review must both land before GA."),
    ]),
    # --- Spec version 2 (disagrees with v1 on the launch date) ---
    p(103, "Atlas Launch Spec v2 (marketing revision)", "spec", AISHA, "In Review", "2026-06-12", "2026-06-22", [
        ("Summary", "Version 2 of the launch specification, revised by marketing. It replaces version 1 "
                    "once it is approved. Approval is pending."),
        ("Launch date", "Public launch: 2026-10-06, aligned with the press agency embargo. The press "
                        "release, customer emails and in-app announcements are scheduled around this date."),
        ("Scope at launch", "Catalog and cart, card payments, refunds, invoice generation and the customer "
                            "account dashboard. Recurring billing is not part of launch scope."),
        ("Open items", "Final sign-off from product and the steering committee."),
    ]),
    p(104, "Payments Integration Plan", "planning", MARCUS, "Approved", "2026-04-12", "2026-06-18", [
        ("Scope", "Integrate PayGate payments API v2: authorize, capture, refund and webhooks. Tracked in ATL-112."),
        ("Original schedule", "Sandbox delivery from PayGate by 2026-06-10, then integration work by the "
                              "checkout, refund, billing and test teams."),
        ("Downstream work", "Four workstreams depend on the sandbox: the card payment screen (ATL-118), "
                            "refund workflow (ATL-121), end-to-end payment tests (ATL-127) and billing "
                            "invoice generation (ATL-133)."),
        ("Revision log", "2026-06-18: sandbox delivery moved from 2026-06-10 to mid-July. The vendor has "
                         "not yet provisioned the sandbox. Status is tracked in ATL-112."),
    ]),
    p(105, "Decision Log: Subscription billing descoped from Atlas GA", "decision_log", PRIYA, "Approved",
      "2026-05-28", "2026-05-28", [
        ("Decision", "Recurring (subscription) billing is removed from the Atlas GA scope and moved to "
                     "Atlas 1.1, planned for Q4 2026."),
        ("Reason", "The payments API v2 launch scope covers one-off charges, refunds and webhooks only. "
                   "The basic fraud rules (ATL-130) are not designed for recurring charges. Keeping "
                   "subscriptions in scope would put the launch date at risk."),
        ("Decided by", "The steering committee, on 2026-05-28."),
        ("Impact", "The scope list in the launch specification no longer includes recurring billing. "
                   "Existing one-off invoicing work continues."),
    ]),
    p(106, "Decision Log: Hosted card fields for checkout", "decision_log", MARCUS, "Approved",
      "2026-04-30", "2026-04-30", [
        ("Decision", "Card entry uses PayGate hosted card fields, so card numbers never touch Northwind servers."),
        ("Reason", "It keeps Atlas out of the heaviest PCI compliance scope and shortens the security review."),
        ("Decided by", "Marcus Chen and Daniel Okafor (security), on 2026-04-30."),
        ("Consequence", "The card payment screen (ATL-118) is built around the hosted fields. Real card "
                        "submission cannot be tested until the PayGate sandbox exists."),
    ]),
    # --- Status contradicts Jira (ATL-121 is Blocked) ---
    p(107, "Refund Workflow Status", "status_report", TOMAS, "Complete", "2026-06-02", "2026-06-19", [
        ("Status", "Complete. Refund workflow implementation signed off on 2026-06-19 and ready for release."),
        ("What it covers", "Customer refund requests, approvals and payouts."),
        ("Linked ticket", "ATL-121."),
    ]),
    p(108, "Weekly Status Report 2026-06-19", "status_report", PRIYA, "Published", "2026-06-19", "2026-06-19", [
        ("Overall", "Amber. One critical dependency is outside the team's control."),
        ("Payments", "Payments API v2 is blocked on the vendor sandbox. New ETA is mid-July. Payment-dependent "
                     "work is on hold."),
        ("Coming up", "Threat model review on 2026-06-30 (ATL-116). Staging environment upgrade due "
                      "2026-07-15 (ATL-138)."),
        ("Risks", "Any further slip in the sandbox delivery moves the checkout, refund and billing work."),
    ]),
    # --- Stale: says staging is upgraded, Jira says it is not (ATL-138 in progress) ---
    p(109, "Checkout Load Test Plan", "planning", ELENA, "Ready to run", "2026-05-30", "2026-06-02", [
        ("Environment", "Staging has been upgraded to production capacity and is ready for load testing."),
        ("Scenarios", "Peak checkout traffic, sustained browse load, and a coupon-heavy cart mix."),
        ("Schedule", "Run window 2026-06-08 to 2026-07-08, reported in ATL-124."),
        ("Reporting", "Bottlenecks and latency percentiles go into the test report."),
    ]),
    p(110, "Security Review and Pen Test Plan", "planning", DANIEL, "Approved", "2026-05-25", "2026-06-15", [
        ("Threat model", "A checkout threat model is reviewed with the security team (ATL-116, review meeting "
                         "on 2026-06-30)."),
        ("Penetration test", "An external vendor, Halberd Security, tests the checkout and billing endpoints "
                             "before launch. Scheduling is tracked in ATL-136."),
        ("Timing", "The test window depends on the vendor's availability."),
    ]),
    p(111, "Beta Program Plan", "planning", PRIYA, "Approved", "2026-06-14", "2026-06-14", [
        ("Goal", "Onboard the first 20 beta customers and collect checkout feedback before launch."),
        ("Kickoff", "Beta kickoff is 2026-08-03."),
        ("Onboarding", "The customer list is selected and onboarding scheduled under ATL-140, due 2026-07-31."),
        ("Support", "Beta customers use the standard support escalation process."),
    ]),
    p(112, "Engineering Onboarding Guide", "onboarding", DANIEL, "Approved", "2026-04-02", "2026-05-12", [
        ("Repository", "Atlas lives in a monorepo. Request access from infrastructure and clone it on day one."),
        ("Continuous integration", "Every pull request runs build and tests. Main is protected; CI must be green to merge."),
        ("Code review", "Every change needs one reviewer from another sub-team."),
        ("Local setup", "Use the provided container image so local and CI environments match."),
    ]),
    p(113, "Incident Response Runbook", "runbook", DANIEL, "Approved", "2026-05-15", "2026-06-07", [
        ("Severity levels", "Sev 1: checkout down. Sev 2: degraded performance. Sev 3: minor defect."),
        ("On-call", "Infrastructure runs a weekly on-call rotation. The primary acknowledges pages within 10 minutes."),
        ("Communication", "Post updates in the incident channel every 30 minutes until resolved."),
        ("After the incident", "Write a short review within five working days."),
    ]),
    p(114, "Design System Usage Guidelines", "guideline", SOFIA, "Approved", "2026-04-30", "2026-05-20", [
        ("Tokens", "Use the published color, spacing and typography tokens (ATL-104). Do not hard-code values."),
        ("Components", "Prefer existing components. Request new ones through the design channel."),
        ("Accessibility", "All components target WCAG 2.1 AA contrast and keyboard support."),
    ]),
    p(115, "Company Holiday Calendar 2026", "calendar", AISHA, "Published", "2026-01-05", "2026-05-02", [
        ("US holidays", "Independence Day observed on 2026-07-03, Labor Day on 2026-09-07, Thanksgiving "
                        "on 2026-11-26 and 2026-11-27."),
        ("Year end", "The office is closed from 2026-12-24 to 2026-12-31."),
        ("Requesting leave", "Submit leave requests at least two weeks ahead."),
    ]),
    p(116, "Customer Support Escalation Process", "process", AISHA, "Approved", "2026-06-10", "2026-06-17", [
        ("Tiers", "Tier 1 handles general questions. Tier 2 handles account and billing issues. Tier 3 is "
                  "engineering."),
        ("Response targets", "Critical issues get a first reply within one hour, others within one working day."),
        ("Handoff", "Escalate with the customer's ticket reference and a short summary of steps already tried."),
    ]),
    p(117, "Data Migration Approach", "planning", TOMAS, "Approved", "2026-05-14", "2026-06-24", [
        ("Approach", "Migrate legacy billing data in phases by customer cohort, with a full dry run in the "
                     "test environment before each phase."),
        ("Invoice history", "Invoice history is mapped last, after customer records are verified. "
                            "Mapping is in progress (ATL-134)."),
        ("Cutover", "Final cutover happens after the beta program, with a rollback plan for each cohort."),
        ("Owner", "Tomas Alvarez owns the migration. Target completion is 2026-08-14."),
    ]),
    p(118, "Analytics Tracking Plan Summary", "planning", ELENA, "Approved", "2026-04-15", "2026-05-19", [
        ("Events", "Track page views, add-to-cart, checkout start, payment submitted and order confirmed."),
        ("Governance", "The data team approves any new event before it ships."),
    ]),
    p(119, "Vendor Management Playbook", "process", OWEN, "Approved", "2026-04-20", "2026-05-02", [
        ("Fee and pricing changes", "Any change to a vendor fee schedule needs approval from Finance. "
                                    "Owen Pierce signs for Finance."),
        ("Liability and legal terms", "Legal reviews liability and indemnity terms before signature."),
        ("Signature", "Finance countersigns vendor contracts after Finance and Legal approve."),
        ("Escalation", "If a vendor misses a date, vendor management escalates to the vendor's account lead."),
    ]),
    # --- RESERVED: fake PII, waits for Phase 5. Not in any key. ---
    p(120, "Beta Partner Contact Notes", "support_notes", AISHA, "Approved", "2026-06-16", "2026-06-16", [
        ("Primary contact", "Riley Hart, pilot partner. Phone +1 (555) 010-9921, email riley.hart@example.com."),
        ("Preferences", "Prefers email for scheduling and a phone call for urgent issues."),
    ]),
]

NO_DATA_TOPICS = ("salesforce", "kubernetes", "zendesk", "loyalty")
DISTRACTOR_IDS = {"NTN-112", "NTN-113", "NTN-114", "NTN-115", "NTN-116", "NTN-118"}


def validate(pages):
    today = date.fromisoformat(TODAY)
    assert 18 <= len(pages) <= 22, f"need about 20 pages, got {len(pages)}"
    ids = [x["id"] for x in pages]
    assert ids == [f"NTN-{101 + i}" for i in range(len(ids))], "IDs must be contiguous from NTN-101"
    for x in pages:
        assert list(x.keys()) == KEYS, f"{x['id']}: schema keys differ from the contract"
        assert x["status"] in STATUSES, (x["id"], x["status"])
        assert x["page_type"] in PAGE_TYPES, (x["id"], x["page_type"])
        for k in ("created_date", "last_edited"):
            assert DATE_RE.match(x[k]), (x["id"], k)
            assert date.fromisoformat(x[k]) <= today, f"{x['id']}: {k} is after TODAY"
        assert x["created_date"] <= x["last_edited"], f"{x['id']}: edited before created"
        assert x["sections"] and all(list(s.keys()) == ["heading", "text"] and s["heading"] and s["text"]
                                     for s in x["sections"]), x["id"]
    by = {x["id"]: x for x in pages}

    def text(i):
        return " ".join(s["text"] for s in by[i]["sections"])

    # two spec versions disagree on the launch date
    assert "2026-09-15" in text("NTN-102") and "2026-10-06" not in text("NTN-102")
    assert "2026-10-06" in text("NTN-103") and "2026-09-15" not in text("NTN-103")
    assert by["NTN-102"]["last_edited"] < by["NTN-103"]["last_edited"]
    # decision log explains a descoped feature
    assert by["NTN-105"]["page_type"] == "decision_log"
    assert "subscription" in text("NTN-105").lower() and "Reason" in [s["heading"] for s in by["NTN-105"]["sections"]]
    # status contradicts Jira (ATL-121 is Blocked), and a stale page contradicts ATL-138
    assert by["NTN-107"]["status"] == "Complete" and "ATL-121" in text("NTN-107")
    assert by["NTN-109"]["status"] == "Ready to run" and "upgraded" in text("NTN-109")
    # original payments plan date, revision log
    assert "2026-06-10" in text("NTN-104") and "mid-July" in text("NTN-104")
    # beta kickoff date that an email later revises (see generate_email.py)
    assert "2026-08-03" in text("NTN-111")
    # distractors
    assert len(DISTRACTOR_IDS) >= 3 and DISTRACTOR_IDS <= set(ids)
    # reserved PII page: exactly one
    pii = [x["id"] for x in pages if re.search(r"\(555\)", text(x["id"])) and "@example.com" in text(x["id"])]
    assert pii == ["NTN-120"], pii
    # no-data topics appear nowhere
    blob = json.dumps(pages).lower()
    for topic in NO_DATA_TOPICS:
        assert topic not in blob, f"'{topic}' must not appear: a no-data question depends on it"
    # cross-source: every ATL reference exists in the Jira data
    jira = OUT_DIR / "jira_tickets.json"
    if jira.exists():
        jids = {t["id"] for t in json.loads(jira.read_text(encoding="utf-8"))}
        refs = set(re.findall(r"ATL-\d{3}", json.dumps(pages)))
        assert refs <= jids, f"unknown Jira IDs referenced: {sorted(refs - jids)}"
        jt = {t["id"]: t for t in json.loads(jira.read_text(encoding="utf-8"))}
        assert jt["ATL-121"]["status"] == "Blocked" and jt["ATL-138"]["status"] == "In Progress"


def write(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    validate(PAGES)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write(OUT_DIR / "notion_pages.json", PAGES)
    stub = [x for x in PAGES if x["id"] in ("NTN-104", "NTN-105", "NTN-112")]
    write(OUT_DIR / "notion_pages_stub.json", stub)
    print(f"wrote {len(PAGES)} pages and {len(stub)} stub pages to {OUT_DIR}", file=sys.stderr)


if __name__ == "__main__":
    main()
