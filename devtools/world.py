"""The fictional world of the synthetic test dataset: a European logistics department ("Corp Europe B.V.").
Europe only in the main dataset; a separate Korean slice (testdata/ko) tests Korean-language support."""
from __future__ import annotations

DOMAIN = "corp-europe.example"
START, END = "2026-09-21", "2026-10-30"

PEOPLE = {
    # key: (name, role, domain)
    "mdeboer": ("Mark de Boer", "Head of Logistics Europe", DOMAIN),
    "rjansen": ("Ruud Jansen", "Logistics manager Rotterdam, owner of the customs migration project", DOMAIN),
    "mkowalska": ("Marta Kowalska", "Customs coordinator", DOMAIN),
    "jdevries": ("Jeroen de Vries", "Outbound team lead Venlo", DOMAIN),
    "anovak": ("Ana Novak", "Accounts payable analyst (Finance)", DOMAIN),
    "lbecker": ("Lukas Becker", "SAP key user SD/LE", DOMAIN),
    "speeters": ("Sofie Peeters", "Warehouse supervisor Antwerp", DOMAIN),
    "thendriks": ("Tom Hendriks", "Transport planner", DOMAIN),
    "erossi": ("Elena Rossi", "Procurement buyer", DOMAIN),
    "pbakker": ("Pieter Bakker", "RPA and IT developer", DOMAIN),
    "hschmidt": ("Hanna Schmidt", "EWM project lead (Rotterdam and Venlo rollouts)", DOMAIN),
    "owright": ("Oliver Wright", "Import coordinator Felixstowe (UK)", DOMAIN),
    "kzielinska": ("Katarzyna Zielinska", "Site coordinator Gdansk", DOMAIN),
    "dvisser": ("Daan Visser", "Master data specialist", DOMAIN),
    "elaurent": ("Emma Laurent", "Customer service lead", DOMAIN),
    "nmeijer": ("Nina Meijer", "AI lead, Logistics Europe", DOMAIN),
    "skovac": ("Stefan Kovac", "Automation engineer (n8n, Python, LLM tooling)", DOMAIN),
    # partners
    "plindqvist": ("Pia Lindqvist", "Account manager at Nordline (freight forwarder)", "nordline.example"),
    "eholm": ("Erik Holm", "IT integration lead at Nordline", "nordline.example"),
    "vbilling": ("Veltrans Billing", "Billing team of Veltrans (road carrier)", "veltrans.example"),
    "jnovotny": ("Jan Novotny", "Key account manager at Veltrans", "veltrans.example"),
    "customstest": ("Customs test team", "Dutch customs authority, test coordination", "customs-authority.example"),
    "palletpro": ("PalletPro Sales", "Pallet supplier", "palletpro.example"),
    "balticship": ("Baltic Shipping Claims", "Short-sea carrier Gdansk", "balticship.example"),
    "routewise": ("RouteWise Support", "TMS software vendor", "routewise.example"),
    "hht": ("HHT Terminal Operations", "Container terminal Hamburg", "hht-terminal.example"),
    "brightwater": ("Brightwater Retail Logistics", "New customer (retail)", "brightwater.example"),
    "harbour": ("Harbour Agency UK", "Customs agent Felixstowe", "harbour-agency.example"),
}
DEPTS = {"dl-ct": "Control tower team mailbox", "dl-cus": "Customs team mailbox", "dl-fin": "Finance logistics mailbox",
         "dl-ai": "AI and automation team mailbox"}
# the team (department members) for the report filter; Mark de Boer (head) and Daan Visser (master data) are outside it
TEAM = [k for k, v in PEOPLE.items() if v[2] == DOMAIN and k not in ("mdeboer", "dvisser")]
# which department's report each storyline belongs in (n8n_eta_flow is mailed to Control Tower but is AI work)
REPORT_DEPT = {"customs": "dl-cus", "aeo": "dl-cus", "reefer_holds": "dl-cus", "adr": "dl-cus", "veltrans_invoices": "dl-fin", "accrual": "dl-fin",
               "baltic_claim": "dl-fin", "detention": "dl-fin", "rpa_bot": "dl-fin", "ai_mail_classifier": "dl-ai", "n8n_eta_flow": "dl-ai"}


def addr(key: str) -> str:
    if key in DEPTS:
        return f"{key}@{DOMAIN}"
    name, _, dom = PEOPLE[key]
    if dom == DOMAIN:
        first, last = name.split(" ", 1)
        return f"{first[0].lower()}.{last.lower().replace(' ', '')}@{dom}"
    return f"{key}@{dom}"


# storyline briefs: (key, title, level, people, partners, (start, end), n_emails, brief, extras)
STORYLINES = [
    ("customs", "Q4 customs migration to the new declaration system (DMS)", "project", ["rjansen", "mkowalska", "hschmidt", "mdeboer"], ["plindqvist", "eholm", "customstest"],
     ("2026-09-21", "2026-10-30"), 22,
     "The Dutch customs authority replaces the old declaration system with DMS. Corp Europe sends declarations through its customs gateway from SAP and from forwarder Nordline. "
     "Go-live was planned for 3 Nov 2026. On 20 Oct Nordline says its DMS release slips to 10 Nov, so Ruud moves the go-live to 17 Nov (no parallel formats) and keeps the EWM change freeze in Rotterdam until then. "
     "End-to-end test with customs on 4–6 Nov with at least 20 declarations; go/no-go call 12 Nov. Nordline test file due 31 Oct. Export mapping: 103 of 105 fields mapped, two goods-item fields wait for Finance.",
     "late_cc: the customs mailbox dl-cus is only CC'd from the 6th email on; one email has an xlsx mapping attachment; one table with test cases"),
    ("veltrans_invoices", "Veltrans invoice mismatches (2025 rates still used)", "topic", ["anovak", "thendriks", "mdeboer"], ["vbilling", "jnovotny"],
     ("2026-10-05", "2026-10-29"), 12,
     "Ana finds invoice INV-88213 lines 1-2 are €4,140 above contract VT-2026-03, plus fuel surcharge charged on top of an all-in rate. Veltrans admits its system still used the 2025 rate card and issues a credit note of €4,657.50. "
     "Demurrage for two reefers held at Felixstowe stays with Corp Europe because the hold was caused by our customs paperwork. Finance wants a monthly spot check until the rate card is fixed.",
     "an HTML table comparing invoiced vs contract rates per line; a PDF credit note attachment"),
    ("z1_block", "Delivery block Z1 after a credit segment change in Venlo", "topic", ["jdevries", "lbecker", "anovak", "dvisser"], [],
     ("2026-10-21", "2026-10-23"), 9,
     "On 21 Oct master data moved Venlo customers to credit segment 0002 with a lower limit without telling logistics; 14 outbound deliveries got delivery block Z1 (credit check) in VL02N. "
     "Lukas releases 9 in VKM3; Finance (UKM_BP) raises the limit for the remaining 5 on 23 Oct, all ship. Explain how to recognise a segment change in VL02N and who can approve a temporary limit. Delivery numbers like 80012345.",
     "a screenshot of VL02N with the block; a table of the 14 deliveries"),
    ("rpa_bot", "Invoice bot errors after Veltrans PDF layout change", "topic", ["pbakker", "anovak"], ["vbilling"],
     ("2026-10-20", "2026-10-26"), 8,
     "Since 20 Oct the RPA invoice bot failed on all 12 Veltrans invoices because the invoice total moved from page 1 to page 2. Pieter adds a fallback that searches all pages; rerun in the night of 23 Oct; Ana checks the results on 26 Oct, 11 of 12 OK, one has a missing PO reference.",
     "a log excerpt table of failed runs"),
    ("ewm_rotterdam", "Rotterdam DC SAP EWM rollout", "project", ["hschmidt", "rjansen", "speeters", "lbecker"], [],
     ("2026-09-21", "2026-10-28"), 16,
     "Rotterdam goes live on SAP EWM on 6 Oct 2026. Week 1 picking rate reaches 96% of baseline. Wave planning is frozen until 17 Nov (customs go-live); the broken zone B wave template is fixed the week after. Hypercare until 30 Oct. Monitor /SCWM/MON used for open warehouse tasks.",
     "a KPI table with pick rates per day; long thread with many short replies"),
    ("venlo_cutover", "Venlo EWM cut-over planning", "project", ["hschmidt", "jdevries", "mdeboer"], [],
     ("2026-10-12", "2026-10-30"), 10,
     "Venlo moves to the new EWM release in Q1 2027 after Rotterdam. Options for the cut-over date: 16 Jan, 30 Jan or 13 Feb 2027; peak season argues against January. Workshop on 5 Nov at 09:00 decides. Four phases: data migration, training, dress rehearsal, cut-over weekend.",
     "one email forwards the Rotterdam lessons learned (forward)"),
    ("po_approval", "PO 4500123 waiting for approval (ME21N workflow)", "task", ["erossi", "jdevries", "pbakker"], ["palletpro"],
     ("2026-10-14", "2026-10-28"), 8,
     "Purchase order 4500123456 for new EUR pallets from PalletPro (€12,480) waits for approval since 14 Oct because the ME21N release workflow still routes to a buyer who moved to Antwerp. Venlo runs out of pallets in about a week. IT changes the workflow owner on 27 Oct; PO approved 28 Oct.",
     ""),
    ("rates_2027", "Carrier rate renewal 2027 (Nordline offer)", "project", ["thendriks", "mdeboer", "erossi"], ["plindqvist"],
     ("2026-10-06", "2026-10-30"), 10,
     "Nordline opens the 2027 rate round with +6% on all 19 ocean lanes; the offer expires 30 Nov. Tom benchmarks: market is +2 to +4%. Corp Europe counters with +3% and a volume commitment of 1,800 TEU. Answer to Nordline due 18 Nov.",
     "xlsx attachment with the 19 lanes and offered rates"),
    ("reefer_holds", "Reefer holds on Felixstowe imports", "topic", ["owright", "mkowalska", "anovak"], ["harbour"],
     ("2026-09-28", "2026-10-16"), 11,
     "Since Brexit reefer containers from Felixstowe need a full import declaration before release. Two reefers (MSKU4021553, TGHU7781204) were held 4 days because the commodity code on the declaration was wrong. Demurrage of €2,350 charged. New checklist: commodity code check by Harbour Agency before vessel arrival.",
     ""),
    ("kpi_co2", "KPI dashboard: CO2 per shipment column", "task", ["thendriks", "pbakker"], ["routewise"],
     ("2026-10-14", "2026-10-22"), 6,
     "From week 44 the TMS export gets a co2_kg_per_shipment column (road legs only, well-to-wheel, GLEC framework). Dashboard mapping updated by Pieter; numbers are estimates until carriers deliver actual fuel data.",
     ""),
    ("hamburg_congestion", "Hamburg terminal congestion and vessel delays", "topic", ["thendriks", "hschmidt"], ["hht"],
     ("2026-09-22", "2026-10-09"), 9,
     "HHT terminal in Hamburg has yard utilisation above 92%; vessel arrivals delayed 3-5 days. Tom rebooks 6 containers via Bremerhaven, extra cost €1,900 per container trucking. Situation normalises from 8 Oct.",
     "a table of affected containers and new ETAs"),
    ("gdansk_lease", "New warehouse lease in Gdansk", "project", ["kzielinska", "mdeboer", "erossi"], [],
     ("2026-09-21", "2026-10-30"), 12,
     "Corp Europe leases a 12,000 m2 warehouse in Gdansk from 1 Feb 2027 to serve Baltic customers. Two sites compared (Port Logistics Park vs Kowale). Kowale chosen for lower rent (€4.10/m2 vs €4.80) despite longer drive. Lease signing planned 20 Nov after legal review.",
     ""),
    ("accrual", "Weekly freight cost accrual (month-end)", "task", ["anovak", "thendriks"], [],
     ("2026-09-25", "2026-10-30"), 8,
     "Every Friday Ana accrues open freight costs: shipments delivered but not invoiced. Steps: report from TMS, match in FBL1N, post accrual with FB50 to account 4711000, reverse on the 1st. September accrual €184,300; October €201,950.",
     "recurring task: same subject pattern each week"),
    ("baltic_claim", "Damaged goods claim with Baltic Shipping", "topic", ["kzielinska", "anovak"], ["balticship"],
     ("2026-09-29", "2026-10-27"), 10,
     "Claim CLM-2026-0912: 14 pallets of electronics damaged by water on the Gdynia-Rotterdam sailing. Value €8,200. Baltic Shipping first rejects (packaging), then accepts 60% after photos and the surveyor report. Settlement €4,920 on 27 Oct.",
     "images: photos of damaged pallets (as screenshots); pdf surveyor report"),
    ("edi_asn", "EDI ASN failures for customer Brightwater", "topic", ["lbecker", "pbakker", "elaurent"], ["brightwater"],
     ("2026-10-07", "2026-10-21"), 9,
     "Advance shipping notices (DESADV) to Brightwater fail since 7 Oct: IDoc status 51 because of a mapping error in the pallet SSCC segment. Checked in WE02. Fixed on 14 Oct; 37 IDocs reprocessed with BD87.",
     "table of failed IDocs"),
    ("onboarding", "Onboarding new customer Brightwater Retail", "project", ["elaurent", "dvisser", "lbecker", "thendriks"], ["brightwater"],
     ("2026-09-21", "2026-10-16"), 11,
     "Brightwater Retail starts shipping on 12 Oct: 3 delivery points in Belgium and the Netherlands. Master data: sold-to 100845 created in XD01, shipping condition 02 (standard), unloading time windows 06:00-14:00. First week: 2 late deliveries because of the time windows.",
     ""),
    ("pallets", "Pallet shortage in Venlo", "topic", ["jdevries", "erossi"], ["palletpro"],
     ("2026-10-12", "2026-10-29"), 8,
     "Venlo has fewer than 300 EUR pallets left; consumption 120 per day. PalletPro can deliver 1,000 on 30 Oct if PO 4500123456 is approved. Short-term: borrow 400 from Antwerp. Safety stock raised to 800.",
     "related to the PO approval storyline"),
    ("adr", "Dangerous goods (ADR) document checks", "task", ["speeters", "mkowalska"], [],
     ("2026-10-01", "2026-10-21"), 7,
     "Antwerp found two shipments with missing ADR transport documents (UN 1263 paint). New rule: dispatcher checks the dangerous goods declaration before loading; checklist in the WMS. Refresher training for 18 drivers on 20 Oct.",
     ""),
    ("tms_upgrade", "RouteWise TMS upgrade", "project", ["thendriks", "pbakker"], ["routewise"],
     ("2026-09-23", "2026-10-26"), 10,
     "TMS RouteWise upgrade to version 9.2 on the weekend of 24-25 Oct. Test plan with 40 test cases; two defects found (rate calculation for multi-stop, CO2 field). Rollback plan agreed. Upgrade went live, one hotfix on 26 Oct.",
     ""),
    ("peak", "Year-end peak season planning", "project", ["mdeboer", "jdevries", "speeters", "thendriks"], [],
     ("2026-10-01", "2026-10-30"), 9,
     "Peak volume in Nov-Dec expected +35% vs average. Plan: 25 temp workers in Venlo from 9 Nov, Saturday shifts, extra trailer capacity of 12 trucks per day from Veltrans. Budget €310,000 approved by Mark on 22 Oct.",
     ""),
    ("detention", "Container detention dispute in Antwerp", "topic", ["speeters", "anovak"], ["plindqvist"],
     ("2026-10-08", "2026-10-28"), 7,
     "Nordline charges detention of €1,650 for container MSKU1234567 kept 11 days at the Antwerp DC. Sofie shows 5 of those days were caused by Nordline's late pickup booking. Agreed: Corp Europe pays 6 days (€900).",
     ""),
    ("qm_block", "Inbound quality block for supplier stock", "task", ["speeters", "lbecker"], [],
     ("2026-10-05", "2026-10-15"), 6,
     "How to release stock in quality inspection (stock type Q) after a supplier delivers without certificates: QA checks, usage decision in QA11, then the stock is available. Took 3 days the first time; now a how-to with t-codes.",
     "a screenshot of the QA11 usage decision screen"),
    ("authorizations", "SAP access for new colleague", "task", ["lbecker", "thendriks"], [],
     ("2026-10-19", "2026-10-23"), 5,
     "New transport planner needs SAP roles: VT01N/VT02N for shipments, VL06O for delivery monitoring. Request via the IT portal, approval by the role owner (Lukas), done in 2 days.",
     ""),
    ("aeo", "AEO customs audit preparation", "project", ["mkowalska", "rjansen", "mdeboer"], ["customstest"],
     ("2026-10-05", "2026-10-30"), 8,
     "The customs authority audits Corp Europe's AEO certificate on 9 Dec. Preparation: update procedures, security checklist for the Rotterdam yard, training records. Gap found: visitor registration in Venlo. Owner Marta; mock audit 24 Nov.",
     ""),
    ("packaging", "New box sizes in packaging master data", "task", ["dvisser", "speeters", "lbecker"], [],
     ("2026-10-13", "2026-10-27"), 6,
     "Three new carton sizes (S2, M3, L4) replace the old ones from 2 Nov. Master data updated with MM02 for 312 materials; packaging instructions in EWM adjusted. Old cartons used up first.",
     ""),
]

KOREAN_STORYLINES = [
    ("ko_hq_forecast", "Korea HQ: Q4 shipment forecast alignment (Korean-language mail)", "topic", ["mdeboer", "thendriks"], [],
     ("2026-10-05", "2026-10-28"), 12,
     "Headquarters colleagues in Seoul write in Korean about the Q4 inbound shipment forecast from the Busan/Incheon plants to Rotterdam: 1,240 TEU planned for November, a 9% increase; vessel capacity with Nordline secured for 1,100 TEU; the remaining 140 TEU via rail. "
     "European colleagues answer in English. Deadline for the final forecast 25 Oct.",
     "KOREAN: emails from HQ are written fully in Korean (Hangul), with Korean Outlook quote headers (보낸 사람/보낸 날짜/받는 사람/제목) and subject prefixes 회신:/전달:"),
    ("ko_customs_docs", "Korea plant: missing certificates of origin (Korean-language mail)", "topic", ["mkowalska"], [],
     ("2026-10-12", "2026-10-27"), 10,
     "The Pyeongtaek plant sends shipments without the EU-Korea FTA certificate of origin statement on the invoice; customs in Rotterdam charges full duty (€18,400 on 3 shipments). The plant export team (writing in Korean) adds the origin statement from 20 Oct; refund request filed for the 3 shipments.",
     "KOREAN: plant emails in Korean; Marta writes English"),
]

KOREAN_PEOPLE = {
    "kimhq": ("Kim Min-jun", "Logistics planner at headquarters Seoul", "hq-korea.example"),
    "leeplant": ("Lee Seo-yeon", "Export manager at the Pyeongtaek plant", "plant-korea.example"),
    "parkhq": ("Park Ji-ho", "Global SCM manager at headquarters", "hq-korea.example"),
}
