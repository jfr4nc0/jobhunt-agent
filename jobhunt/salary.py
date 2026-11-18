"""Market-relative salary suggestions backed by dated local role benchmarks."""

from __future__ import annotations

import re
from datetime import date

from . import db, profile

CURRENCY_MARKERS = {
    "USD": r"(?:US\$|USD|\$)", "EUR": r"(?:EUR|€)", "GBP": r"(?:GBP|£)",
    "CHF": r"CHF", "AUD": r"(?:AUD|A\$)", "NZD": r"(?:NZD|NZ\$)",
    "SGD": r"(?:SGD|S\$)", "DKK": r"DKK", "NOK": r"NOK",
    "PLN": r"(?:PLN|zł)", "CNY": r"(?:CNY|CN¥|RMB)",
    "HKD": r"(?:HKD|HK\$)", "SEK": r"SEK",
    "ARS": r"ARS", "BRL": r"(?:BRL|R\$)", "MXN": r"(?:MXN|MX\$)",
}
PERIOD = r"annually|annual(?: base)?|per year|/year|yearly|monthly|per month|/month"


def _amount(number: str, unit: str | None) -> float:
    multiplier = {"K": 1_000, "M": 1_000_000}.get((unit or "").upper(), 1)
    number = number.replace("'", "")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", number):
        number = number.replace(".", "")
    return float(number.replace(",", "")) * multiplier


def applicable_band(job: dict, currency: str = "USD", country_code: str = "") -> dict | None:
    """Read one unambiguous range in the benchmark's currency and time unit."""
    marker = CURRENCY_MARKERS.get(currency.upper())
    if not marker:
        return None
    number = r"\d+(?:[.,']\d+)*"
    pattern = re.compile(
        rf"(?P<m1>{marker})\s*(?P<low>{number})(?P<low_unit>[KM])?\s*(?:[–—-]|\bto\b)\s*"
        rf"(?P<m2>{marker})?\s*(?P<high>{number})(?P<high_unit>[KM])?\+?\s*"
        rf"(?P<m3>{marker})?\s*(?:gross\s*|base\s*)?(?P<period>{PERIOD})", re.I)
    for text, salary_field in ((job.get("salary") or "", True), (job.get("description") or "", False)):
        found = []
        for match in pattern.finditer(text):
            before = text[max(0, match.start() - 100):match.start()].lower()
            after = text[match.end():match.end() + 110].lower()
            if not salary_field and not re.search(r"compensation|salary|pay range|base pay|base salary|remuneration", before):
                continue
            if re.search(r"\b(us|usa|united states) compensation\s*:\s*$", before):
                continue
            if "latam compensation" in after and "separate" in after:
                continue
            if currency.upper() == "USD" and country_code.upper() != "US" and \
                    not re.search(r"USD|US\$", match.group(0), re.I):
                continue
            low = _amount(match.group("low"), match.group("low_unit"))
            high = _amount(match.group("high"), match.group("high_unit"))
            if high < low:
                continue
            period = match.group("period").lower()
            if "month" in period:
                low_month, high_month = low, high
                low_annual, high_annual = low * 12, high * 12
            else:
                low_month, high_month = low / 12, high / 12
                low_annual, high_annual = low, high
            found.append({"low_monthly": round(low_month, 2), "high_monthly": round(high_month, 2),
                          "low_annual": round(low_annual, 2), "high_annual": round(high_annual, 2),
                          "currency": currency.upper(), "published_text": match.group(0),
                          "period": "monthly" if "month" in period else "annual"})
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return None
    return None


def recommendation(job: dict, candidate: dict | None = None,
                   benchmark: dict | None = None, today: date | None = None) -> dict:
    candidate = candidate or profile.load_profile()
    preference = candidate.get("screening", {}).get("salary_expectation") or {}
    if preference.get("strategy") != "local_role_median" or preference.get("basis") != "gross":
        raise ValueError("Candidate salary strategy must be gross local role median")
    base = {"job_id": job.get("id"), "suggested_monthly_gross": None,
            "suggested_annual_gross": None}
    if not benchmark:
        return {**base, "status": "market_data_required",
                "reason": "A dated local salary median for this role and country is required before answering."}
    currency = benchmark["currency"].upper()
    median_annual = float(benchmark["median_annual_gross"])
    observed = date.fromisoformat(benchmark["observed_at"])
    today = today or date.today()
    context = {"country_code": benchmark["country_code"], "role_key": benchmark["role_key"],
               "location": benchmark["location"], "currency": currency,
               "market_median_monthly_gross": round(median_annual / 12, 2),
               "market_median_annual_gross": round(median_annual, 2),
               "source_url": benchmark["source_url"], "observed_at": benchmark["observed_at"]}
    if observed > today or (today - observed).days > 730:
        return {**base, **context, "status": "stale_market_data",
                "reason": "The salary benchmark is future-dated or more than two years old; refresh it before answering."}
    median_monthly = round(median_annual / 12, 2)
    band = applicable_band(job, currency, benchmark["country_code"])
    if band:
        if band["high_annual"] < median_annual:
            return {**base, **context, "status": "below_market_median", "band": band,
                    "reason": "The advertised maximum is below the local role median; review before applying."}
        suggested_annual = max(median_annual, band["low_annual"])
        return {**context, "status": "within_band", "band": band,
                "suggested_monthly_gross": round(suggested_annual / 12, 2),
                "suggested_annual_gross": round(suggested_annual, 2),
                "reason": "Use the lower edge of the applicable band without undercutting the local role median."}
    if job.get("salary") or re.search(
        r"(?:salary|compensation|remuneration).{0,80}(?:[$€£]|\b[A-Z]{3}\b)\s*\d", job.get("description") or "", re.I):
        return {**base, **context, "status": "band_requires_review",
                "reason": "A published salary exists but cannot be safely compared with this market benchmark."}
    return {**context, "status": "market_median",
            "suggested_monthly_gross": median_monthly,
            "suggested_annual_gross": round(median_annual, 2),
            "reason": "No applicable published band; use the sourced local role median."}


def for_job(job_id: int, country_code: str = "", role_key: str = "",
            location: str = "national") -> dict:
    job = db.get_job(job_id)
    if not country_code or not role_key:
        return {"job_id": job_id, "status": "market_context_required",
                "suggested_monthly_gross": None,
                "reason": "Specify the job's country_code and role_key to use a comparable local market median."}
    benchmark = db.get_salary_benchmark(country_code, role_key, location)
    return recommendation(job, benchmark=benchmark)
