# Atlas world bible (canonical facts shared by Jira, Notion and email)

TODAY = 2026-06-30 (Tuesday, frozen). Company: Northwind. Project: Atlas (checkout and billing platform).
If any source disagrees with this page, the page is right unless the disagreement is listed under "Planted conflicts".

## People
Priya Nair (product lead) | Marcus Chen (backend, payments lead) | Sofia Lindqvist (frontend, design) | Wei Zhang (backend: tax, billing)
Elena Rossi (QA) | Tomas Alvarez (backend: inventory, refunds, migration) | Daniel Okafor (infra, security) | Aisha Khan (marketing, ops)
Owen Pierce (Finance, vendor management) | Nadia Haddad (Legal) | Rafael Duarte (VP Product, steering chair)
External: Lukas Brandt (PayGate, payments vendor) | Mira Castellan (Halberd Security, pen test vendor)

## Timeline
- 2026-04-12 ATL-112 opened, due 2026-06-10. Plan: PayGate v2 sandbox by 2026-06-10.
- 2026-04-30 Decision: hosted card fields (NTN-106).
- 2026-05-18 PayGate sends v2 amendment: fee 2.9% -> 3.1% (+0.30), liability cap 12 months (EML-101).
- 2026-05-20 Launch spec v1 approved, GA 2026-09-15 (NTN-102).
- 2026-05-28 Steering descopes subscription billing to Atlas 1.1, Q4 2026 (NTN-105).
- 2026-06-04 Steering meeting; ATL-103 and Priya say it confirmed GA 2026-09-15.
- 2026-06-04 PayGate best offer: 3.0% for 12 months, 18-month cap. Finance accepts 06-09, Legal 06-10.
- 2026-06-10 ATL-112 due date passes. Sandbox NOT delivered.
- 2026-06-12 Owen countersigns the amendment (25 days after it was sent). Marketing tells ATL-139 the launch is 2026-10-06.
- 2026-06-15 PayGate: 20 business days after signature plus a security review; target 2026-07-15 (EML-111).
- 2026-06-18 Jira and Notion record the new ETA as mid-July.
- 2026-06-22 Priya escalates to vendor management; Owen asks PayGate to expedite 06-23; PayGate declines 06-26 (EML-117).
- 2026-06-22 Launch spec v2 (marketing, In Review) says launch 2026-10-06 (NTN-103).
- 2026-06-24..26 Email chain: marketing says 10-06, product says 09-15. Unresolved. Rafael defers to the 2026-07-02 steering meeting.
- 2026-06-29 Priya moves beta kickoff 2026-08-03 -> 2026-08-17 (EML-124).
- 2026-06-30 Threat model review (ATL-116, due today, NOT overdue).
- Upcoming: 07-01 staging hardware arrives | 07-02 steering | 07-03 office closed | 07-15 staging upgrade and PayGate sandbox target |
  07-20 earliest pen test window (Halberd) | 07-22 load test due (ATL-124, moved from 07-08) | 07-31 beta onboarding list (ATL-140).

## The payments delay story
Why ATL-112 is late: PayGate only issues v2 sandbox credentials after Northwind countersigns the v2 amendment. Signature was held
from 05-18 to 06-12 by a fee and liability-cap negotiation (Finance vs PayGate's 3.1%, Legal vs a 12-month cap). Then PayGate needs
20 business days plus a security review, so the target is 2026-07-15. Four tickets wait on it: ATL-118, ATL-121, ATL-127, ATL-133.
Jira shows the symptom only (credentials missing, amendment pending, ETA mid-July). The cause is in email only.
ATL-109 (privacy copy) and ATL-136 (pen test) are NOT part of this chain. Nadia is on both the PayGate thread and ATL-109, which is a trap.

## Planted conflicts (intentionally unresolved)
1. Launch date: ATL-103 and NTN-102 say 2026-09-15; ATL-139 and NTN-103 say 2026-10-06; EML-119..123 argue it. No source decides.
2. Refund workflow: NTN-107 says Complete (06-19); ATL-121 is Blocked (comment 06-23).
3. Staging: NTN-109 says upgraded and ready (06-02); ATL-138 is In Progress, due 07-15; ATL-124 moved for that reason.
4. Beta kickoff: NTN-111 says 2026-08-03; EML-124 (later) says 2026-08-17. Here the email is simply newer.

## Rules for adding content
Atlas only. Distractors are other topics (HR, IT, holidays, design guidelines), never other projects.
No-data topics, absent from every source: salesforce, kubernetes, zendesk, loyalty.
