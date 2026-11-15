"""
manufacturer_split.py  (version 2)
Splits a CDSCO 'str_manufactured_by' string into (company_name, address_part, status).

status values:
  ok_full_address   -> name found by a company word (Pharma, Labs, Ltd...), real address follows
  ok_place_only     -> name found, only a city / state follows
  ok_name_only      -> name found, no address at all in the string
  ok_guess_name     -> no company word found; name GUESSED from the first comma / first number
  no_manufacturer   -> placeholder such as "Under Investigation", "Not applicable", "Spurious"
  review_contract   -> two companies in one string ("At M/s."), or "Repacked by"
  review_unclear    -> could not split with confidence (goes to the review file)
"""
import re

PLACEHOLDERS = {"under investigation", "not applicable", "na", "n/a", "-", "nil",
                "not available", "spurious", "not known", "unknown", ""}

TYPE_WORDS = [
    "pharmaceuticals", "pharmaceutical", "pharma", "pharm", "pharmaceutica", "pharmacare",
    "laboratories", "laboratory", "labs", "lab", "lifesciences", "life sciences",
    "life science", "lifescience", "sciences", "science", "lifecare", "life care",
    "healthcare", "health care", "care", "remedies", "drugs", "drug", "antibiotics",
    "biotech", "biotechnology", "biologicals", "biological", "biogenetic", "biogenetics",
    "vaccines", "vaccine", "parenterals", "parenteral", "surgicals", "surgical",
    "medicines", "medicine", "medico", "medicare", "formulations", "organics",
    "chemicals", "chemical", "industries", "enterprises", "exports", "overseas",
    "therapeutics", "biosciences", "bioscience", "genetics", "cosmetics", "ayurveda",
    "herbals", "systems", "international", "products", "works", "sons",
    "private", "limited", "pvt", "ltd", "llp", "inc", "corporation", "company", "co",
]

JOINERS = r"(?:&|and|pvt\.?|private|ltd\.?|limited|\(p\)|llp|inc\.?|co\.?|company)"
UNIT_PAREN = r"\((?:a\s+)?(?:unit|division|branch)\b[^)]*\)"
ANY_PAREN = r"\([^)]{1,25}\)"

_TYPE_ALT = "|".join(sorted(map(re.escape, TYPE_WORDS), key=len, reverse=True))
TYPE_RE = re.compile(r"(?<![A-Za-z])(" + _TYPE_ALT + r")(?![A-Za-z])", re.I)
EXTEND_RE = re.compile("|".join([
    r"\s*,?\s*" + UNIT_PAREN,                                                   # , (A Unit of ...)
    r"\s*\.?\s*,?\s*(?:pvt|private|ltd|limited|llp|inc)\.?(?![A-Za-z])",       # Pvt. Ltd. / pvt.Ltd
    r"\s+" + JOINERS + r"(?![A-Za-z])",                                         # & / and / Pvt / Ltd
    r"\s+" + ANY_PAREN,                                                         # (India) (P)
    r"\s+(?:" + _TYPE_ALT + r")(?![A-Za-z])",                                   # another company word
]), re.I)

ADDRESS_START = re.compile(
    r"^(?:plot|survey|village|vill|gat|khasra|khasara|sector|unit|block|shed|door|h\.?\s?no|house|industrial|"
    r"near|opp|p\.?o\.?|nh|n\.h|s\.?f\.?|gala|building|floor|road|mouza|sy\.?|s\.?no|no\.?|at|"
    r"works|factory|godown|[a-z]{0,2}\s?-?\s?\d)", re.I)
ADDR_KW = re.compile(
    r"(?<![A-Za-z])(?:plot|survey|village|vill|khasra|khasara|sector|industrial|estate|road|distt|dist|"
    r"district|tehsil|teh|taluk|block|phase|gidc|midc|sidco|epip|marg|street|lane|floor|mauza|hobli|"
    r"k\.?\s?no|p\.o)(?![A-Za-z])", re.I)
PIN_RE = re.compile(r"(?<!\d)\d{3}\s?\d{3}(?!\d)")
STATES = (r"andhra|arunachal|assam|bihar|chhattisgarh|goa|gujarat|haryana|himachal|jharkhand|karnataka|kerala|"
          r"madhya|maharashtra|manipur|meghalaya|mizoram|nagaland|odisha|orissa|punjab|rajasthan|sikkim|"
          r"tamil|telangana|tripura|uttar|uttarakhand|bengal|delhi|puducherry|pondicherry|chandigarh|"
          r"jammu|kashmir|ladakh|daman|diu|dadra|nagar haveli|"
          r"h\.?p\.?|u\.?p\.?|m\.?p\.?|t\.?s\.?|a\.?p\.?|w\.?b\.?|j\.?&\.?k")
STATE_RE = re.compile(r"(?<![A-Za-z])(?:" + STATES + r")(?![A-Za-z])", re.I)
PREFIX_RE = re.compile(r"^(?:(?:mfg\.?|mfd\.?|manufactured|manufacturing)\s*by\s*[:\-]?|m\s*/\s*s\.?|messrs\.?|[:;,.\-–\s])+", re.I)
MOJIBAKE = {"â€“": "-", "â€”": "-", "â€™": "'", "â€˜": "'", "â€œ": '"', "â€\x9d": '"', "Â": ""}


def _address_ok(rest):
    return bool(PIN_RE.search(rest) or ADDRESS_START.match(rest) or ADDR_KW.search(rest))


def split_manufacturer(raw):
    if raw is None:
        return "", "", "no_manufacturer"
    s = str(raw)
    for bad, good in MOJIBAKE.items():                  # fixes broken dash characters from the source
        s = s.replace(bad, good)
    s = re.sub(r"\s+", " ", s).strip()                  # fixes line breaks inside the text
    if s.lower().strip(" .:") in PLACEHOLDERS:
        return "", "", "no_manufacturer"
    if re.match(r"\s*re-?pack", s, re.I):               # a repacker is not the manufacturer
        return s, "", "review_contract"
    s = PREFIX_RE.sub("", s, count=1)                   # remove leading M/s. , Mfg By: , stray colons
    if s.lower().strip(" .:") in PLACEHOLDERS:
        return "", "", "no_manufacturer"

    # two companies in one string (contract manufacturing) -> human review
    if re.search(r"(?<![A-Za-z])m\s*/\s*s\b|loan licen|on behalf of", s, re.I):
        return s, "", "review_contract"

    m = TYPE_RE.search(s)
    if m:
        end = m.end()
        while True:                                     # keep absorbing "& Pharmaceuticals Pvt. Ltd." etc.
            e = EXTEND_RE.match(s, end)
            if not e or e.end() == end:
                break
            end = e.end()
        name = s[:end].strip(" ,.-–;")
        rest = s[end:].strip(" ,.;-–")
        if len(name) >= 3:
            if not rest:
                return name, "", "ok_name_only"
            if _address_ok(rest):
                return name, rest, "ok_full_address"
            words = rest.split()
            if len(words) <= 6 and not TYPE_RE.search(rest) and (STATE_RE.search(rest) or len(words) <= 4):
                return name, rest, "ok_place_only"

    # no company word found: guess the name from the first comma or first number
    cuts = [x.start() for x in (re.search(r",", s), re.search(r"\s(?=[A-Za-z]{0,2}-?\d)", s)) if x]
    if cuts:
        cut = min(cuts)
        name = s[:cut].strip(" ,.-–;")
        rest = s[cut:].strip(" ,.;-–")
        if 3 <= len(name) and 1 <= len(name.split()) <= 7 and _address_ok(rest):
            return name, rest, "ok_guess_name"
    return s, "", "review_unclear"


if __name__ == "__main__":
    tests = [
     "M/s. Unicure (India) Ltd., Roorkee, Uttarakhand",
     "M/s. Sridhara Life Science Pvt. Ltd., Haridwar (Uttarakhand)",
     "M/s. Omega Biotech Ltd., Roorkee, Uttarakhand-247667",
     "M/s. Vikram Laboratories (P) Ltd., Meerut Road, Muzaffarnagar-251002",
     "M/s. Redico Remedies, Solan (H.P.)",
     "M/s. Goodwill Lifesciences, Surat - 394510",
     "M/s. Sai Parenterals Pvt. Ltd., Hyderabad - 500055",
     "M/s. Jackson Laboratories Pvt. Ltd., Amritsar -143001",
     "M/s. Stadmed Private Limited, Lucknow – 226008",
     "M/s. Sterlife Pharma Pvt Ltd., Rajkot, Gujarat",
     "M/s. Medico Remedies Ltd., Mumbai-400049",
     "M/s. M Sea Pharmaceuticals Pvt. Ltd., Paonta Sahib, H.P.",
     "M/s. Vee Laboratories, Solan (H.P.)",
     "M/s. Zee Laboratories Ltd., Paonta Sahib",
     "M/s. SKOHIND LABS, Baddi (H.P.)",
     "M/s. Ciron Drugs & Pharmaceuticals Pvt Ltd., Palghar - 401506",
     "M/s DANO Vaccines & Biologicals Pvt\nLtd., Factory-575, Sivaredyguda,\nGhatkesar-501301, (T.S.)",
     "M/s. Akums Drugs & Pharmaceuticals Ltd., Haridwar – 249403",
     "M/s. Medishines Laboratories (P) Ltd., 156/6, Vinayagar Koil St, Thirubhuvanai Palayam, Puducherry 605107",
     "M/s. ISCON SURGICALS LTD., B-70, M.I.A., Phase II, Basni, Jodhpur-342005, Rajasthan",
     "M/s. Ornate Labs Pvt. Ltd., Bela Industrial Estate, Muzaffarpur - 842 005.",
     "Rachil Pharma,At M/s. Qurx Pharmaceuticals, 191-191A, Industrial Area, Phase III, Sansarpur Terrace, Distt. Kangra (H.P.)-176501",
     "M/s. Wings Biotech 43 & 44 HPSIDC, Industrial Area, Baddi-173205 (H.P.)",
     "M/s. Paschim Banga Pharmaceuticals, (A Unit of Farista Vanijya Pvt. Ltd), NH- 31, TIN MILE HAT, Sonapur\nHAT, PS. Chopra Dist. Uttar Dinajpur-733207",
     "M/s. Bal Pharma Limited, Plot No. 1, 2, 3 & 69, Sector 4 IIE, SIDCUL, Pantnagar, Rudrapur Distt.-Udham Singh Nagar, Uttarakhand",
     "M/s. Swaroop Pharmaceuticals Pvt. Ltd., A-10, Tala Nagari Industrial Area, Aligarh – 202001, Uttar Pradesh",
     "M/s. Health Biotech Ltd., Village Sandoli, Nalagarh Road, Baddi, Distt.Solan (H.P.) India-173205",
     "Asuwaldi Pharmaceuticals, D-9, Phase II, MIDC, Manpada Road, Dombivali (E), Thane421 204",
     "Under Investigation",
     # ---- the 25 real 'unclear' strings from the first run ----
     "M/s. Martin & Brown Bio Sciences, K.No. 918/419, Malkumajra, Nalagarh Road, Baddi, Dist. Solan (H.P.)- 173205",
     "Star Filteration Systems.,Rampur Jattan Trilokpur Road Kala Amb Tehsil Nahan Distt. Sirmour 173030(HP)",
     "M/s. Modi Antibiotics, Plot No. 28, G.I.D.C., Ahmedabad 382 415, Gujarat",
     "M/s. K. Ramakrishnan & Sons, Chatrapatti-626 102 Tamil Nadu",
     "Marc Lifesciences Sikkim, Khasara No. 35, 36, 409, 410, Vill, Julanglay, Block L, Maneydara Namthang, Distt. Namchi, South Sikkim.",
     "Repacked by Ganesh Eucalyptus Oil Disitillery, 101, Ganesh Hall, Walsham Road, Ootacamund 643001, The Nilgris",
     "M/s. Martin & Brown Bio- Sciences K. No. 918/419, Malkumajra, Nalagarh Road, Baddi, Dist- Solan (H.P) 173205",
     "M/s. NEELAM CHEMICAL WORKS, B.O.M.G. Road, Bada Bazar, Kolkata - 7",
     "Cartel Laboratories, An ISO 9001: 2015 & GMP Certified Company Plot No.10/A & 11/A, Chengicherla, Medchal-Malkajgiri Dist. Hyderabad-92",
     "M/s. P.C. Cosmetics",
     "TORQUE PHARMACEUTICALS PVT. LTD.",
     "Titanes Pharm, Village Bathu, Tehsil Haroli, Distt. Una (H.P) 174507",
     "Spurious",
     "Kanha Biogenetic, Plot No. 1B, EPIP, Phase 1, Jharmajri, Baddi, Dist. Solan, (H.P.).",
     "Seldom Pharma Pvt. Ltd.",
     "Elvia Care Pvt. Ltd., Vill Gullarwala, Near Sai Road, Baddi, Distt. Solan (H.P.)",
     "M/s.Mestra Pharma Pvt. Ltd., M/s. Jasvic Laboratories Kh no. 376, Vill. Latifpur Khubbanpur, Dehradun, Roorkee-",
     "Gurumangalam International, Rancharda-382115, Ta: Kalol, Dist: Gandhinagar, Gujarat",
     "Mfg By: M/s. Samkem 15-B & 10-B, A.K.V.N. Industrial Area, Rangwasa, Rao, Indore - 453331. (M.P). Madhya Pradesh.",
     "M/s. Gujarat Chempac Pvt. Ltd., Valsad, Gujarat",
     "Steadfast Medishield pvt.Ltd, At khasra,No.322, Nanhera , Anantpur, Bhgwanpur, Roorkee, Dist. Haridwar",
     "M/s Digital Vision Pvt. Ltd. 176, Mauza Ogli, Nahan road, Kala Amb, Distt. Sirmour (H.P)",
     "M/s. Spas Remedise, Vill Bersan, P.O. Lodhi Majra, Teh Baddi, Distt. Solan Himachal Pradesh â€“ 173 205.",
     ": Mandkind Pharma Ltd, 393/394, Melli Jorethang Road, Gom Block, Sikkim 737 121",
    ]
    for t in tests:
        n, a, st = split_manufacturer(t)
        print(f"[{st:16}] NAME={n!r}\n{'':19} REST={a[:55]!r}")
