from datetime import date
from pathlib import Path

from jobhunt import db, salary, sources


def test_ashby_structured_salary_keeps_currency_and_period():
    extracted = sources.ashby_salary({"scrapeableCompensationSalarySummary": "£115K - £130K",
        "summaryComponents": [{"compensationType": "Salary", "interval": "1 YEAR",
                               "currencyCode": "GBP", "minValue": 115000, "maxValue": 130000}]})
    assert extracted == "GBP 115,000 - GBP 130,000 annually"
    assert salary.applicable_band({"salary": extracted}, "GBP", "GB")["low_annual"] == 115000


def test_ashby_primary_country_salary_is_not_confused_with_other_tiers():
    argentina = {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "ARS",
                 "minValue": 152000000, "maxValue": 160000000}
    brazil = {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "BRL",
              "minValue": 538000, "maxValue": 669000}
    extracted = sources.ashby_salary({"summaryComponents": [argentina],
        "compensationTiers": [{"components": [argentina]}, {"components": [brazil]}]})
    assert extracted == "ARS 152,000,000 - ARS 160,000,000 annually"
    assert salary.applicable_band({"salary": extracted}, "ARS", "AR")["low_annual"] == 152000000


CANDIDATE = {"screening": {"salary_expectation": {
    "strategy": "local_role_median", "basis": "gross", "period": "month"}}}

BENCHMARK = {"country_code": "AR", "role_key": "ai_engineer", "location": "national",
             "currency": "USD", "median_annual_gross": 78000,
             "source_url": "https://example.org/salary-survey", "observed_at": "2026-06-01"}


def test_no_market_benchmark_means_no_numeric_salary_answer():
    result = salary.recommendation({"id": 1, "description": "Salary: $84,000-$120,000 USD annually"},
                                   CANDIDATE, today=date(2026, 11, 18))
    assert result["status"] == "market_data_required"
    assert result["suggested_monthly_gross"] is None


def test_annual_band_uses_low_end_above_local_median():
    result = salary.recommendation({"id": 1, "description":
        "Compensation: $84,000–$120,000 USD annually."},
        CANDIDATE, BENCHMARK, today=date(2026, 11, 18))
    assert result["status"] == "within_band"
    assert result["market_median_monthly_gross"] == 6500
    assert result["suggested_monthly_gross"] == 7000


def test_market_median_applies_within_or_above_a_band():
    overlap = salary.recommendation({"description": "Salary: $50,000-$80,000 USD annually"},
        CANDIDATE, BENCHMARK, today=date(2026, 11, 18))
    below = salary.recommendation({"description": "Salary: $40,000-$50,000 USD annually"},
        CANDIDATE, BENCHMARK, today=date(2026, 11, 18))
    assert overlap["suggested_monthly_gross"] == 6500
    assert below["status"] == "below_market_median"
    assert below["suggested_monthly_gross"] is None


def test_us_only_band_is_not_applied_to_latam():
    result = salary.recommendation({"description":
        "US compensation: $170,000–$230,000 annual base. "
        "LATAM compensation: A separate, lower regional range applies."},
        CANDIDATE, BENCHMARK, today=date(2026, 11, 18))
    assert result["status"] == "band_requires_review"
    assert result["suggested_monthly_gross"] is None


def test_local_currency_band_uses_market_median_and_old_data_is_rejected():
    euro = {**BENCHMARK, "country_code": "DE", "currency": "EUR"}
    band = salary.recommendation({"salary": "€70,000–€90,000 annually"},
        CANDIDATE, euro, today=date(2026, 11, 18))
    stale = salary.recommendation({}, CANDIDATE,
        {**euro, "observed_at": "2022-01-01"}, today=date(2026, 11, 18))
    assert band["status"] == "within_band"
    assert band["suggested_monthly_gross"] == 6500
    assert stale["status"] == "stale_market_data"


def test_swiss_and_danish_formats_are_parsed_when_explicit():
    ch = {**BENCHMARK, "country_code": "CH", "currency": "CHF", "median_annual_gross": 120000}
    dk = {**BENCHMARK, "country_code": "DK", "currency": "DKK", "median_annual_gross": 600000}
    swiss = salary.recommendation({"salary": "CHF 130'000–150'000 gross per year"},
        CANDIDATE, ch, today=date(2026, 11, 18))
    danish = salary.recommendation({"salary": "DKK 650.000–850.000 per year"},
        CANDIDATE, dk, today=date(2026, 11, 18))
    assert swiss["status"] == "within_band"
    assert swiss["suggested_annual_gross"] == 130000
    assert danish["status"] == "within_band"
    assert danish["suggested_monthly_gross"] == round(650000 / 12, 2)


def test_berlin_employer_band_above_local_median():
    berlin = {**BENCHMARK, "country_code": "DE", "currency": "EUR",
              "location": "berlin", "median_annual_gross": 83340}
    result = salary.recommendation({"description":
        "Compensation & Benefits: €85,000 to €160,000 annually, plus equity."},
        CANDIDATE, berlin, today=date(2026, 11, 18))
    assert result["status"] == "within_band"
    assert result["suggested_annual_gross"] == 85000


def test_benchmark_persists_by_country_role_and_location(tmp_path: Path):
    database = tmp_path / "jobs.sqlite3"
    db.add_salary_benchmark("DE", "software_engineer", "EUR", 70000,
        "https://example.org/2026", "2026-01-01", db_path=database)
    saved = db.get_salary_benchmark("DE", "software_engineer", db_path=database)
    assert saved["median_annual_gross"] == 70000
    assert saved["currency"] == "EUR"
