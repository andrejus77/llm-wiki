"""Reference numbers (by regex, from settings) and SAP t-code discovery. Facts by script, never by the model."""
from __future__ import annotations

import re

# Common SD / MM / LE / WM / EWM / FI / basis transactions. New ones are discovered and promoted automatically.
TCODE_SEED = set("""
VA01 VA02 VA03 VA05 VA11 VA21 VA22 VF01 VF02 VF03 VF04 VF11 VF31 VL01N VL02N VL03N VL06O VL06G VL06I VL10A VL10B VL31N VL32N VL33N
VT01N VT02N VT03N VI01 VI02 VKM1 VKM3 VKM4 FD32 FD33 UKM_BP ME21N ME22N ME23N ME28 ME29N ME2M ME2N ME51N ME52N ME53N ME55 ME5A ME57
MIGO MIRO MIR4 MIR7 MB51 MB52 MMBE MM01 MM02 MM03 MM60 MD04 MD01 MD02 CO01 CO02 CO03 COOIS MB1A MB1B MB1C MB31 LT01 LT03 LT12 LT10
LT22 LT23 LT24 LS01N LS02N LS03N LX02 LX03 /SCWM/MON /SCWM/PRDO /SCWM/PRDI /SCWM/WAVE /SCWM/TO_CONF /SCWM/ADGI /SCWM/RFUI
FB01 FB02 FB03 FBL1N FBL3N FBL5N F-02 F-28 F-53 FK01 FK02 FK03 XK01 XK02 XK03 XD01 XD02 XD03 BP SE16N SE16 SE37 SE38 SM30 SM35 SM37
SU01 SQ01 ST22 SM21 SM12 SPRO VK11 VK12 VK13 V/LD VD52 VBO2 KE30 KSB1 CJ20N CJ03 J1BTAX VT11 VT12 VTTK EWM WE02 WE05 WE19 BD87
""".split())
TCODE_SHAPE = re.compile(r"(?<![\w/])(/[A-Z0-9]{2,8}/[A-Z0-9_]{2,20}|[A-Z]{2,4}\d{1,3}[A-Z]?|[A-Z]{2,4}_[A-Z]{2,4})(?![\w/])")
TCODE_CONTEXT = re.compile(r"(?:t-?code|tcode|transaction|transactie|transaktion|트랜잭션|/n)", re.I)


def find_identifiers(text: str, patterns: dict[str, str]) -> list[tuple[str, str]]:
    out, seen = [], set()
    for typ, pat in patterns.items():
        try:
            for m in re.finditer(pat, text or ""):
                v = m.group(0)
                if (typ, v) not in seen:
                    seen.add((typ, v))
                    out.append((typ, v))
        except re.error:
            continue
    return out


def find_tcodes(text: str, stoplist: list[str], known: set[str] | None = None) -> tuple[set[str], dict[str, str]]:
    """Return (known codes found, {candidate: context}) — candidates are t-code-shaped tokens near t-code words."""
    known = (known or set()) | TCODE_SEED
    stop = {s.upper() for s in stoplist}
    found, cands = set(), {}
    for tok in re.findall(r"(?<![\w/])(/?[A-Z][A-Z0-9_/\-]{1,20})(?![\w])", text or ""):
        t = tok.upper().rstrip("/")
        if t in known and t not in stop and t not in ("EWM", "BP") or (t in ("BP",) and TCODE_CONTEXT.search(text or "")):
            found.add(t)
    for m in TCODE_SHAPE.finditer(text or ""):
        code = m.group(1).upper()
        if code in stop:
            continue
        if code in known:
            found.add(code)
            continue
        ctx = (text or "")[max(0, m.start() - 60): m.end() + 40]
        if TCODE_CONTEXT.search(ctx) or (text or "")[max(0, m.start() - 2):m.start()].lower() == "/n":
            cands[code] = " ".join(ctx.split())
    return found, cands
