from datetime import datetime, timezone, timedelta
import pytest
from news.filters import is_retrospective_headline, is_routine_news, compute_news_age_hours


# --- is_retrospective_headline ---

def test_retro_why_surging():
    assert is_retrospective_headline("Why Is UONE Stock Surging Friday?") is True

def test_retro_why_skyrocketing():
    assert is_retrospective_headline("Why Urban One Stock Is Skyrocketing Friday") is True

def test_retro_why_jumping():
    assert is_retrospective_headline("Why Is MX Stock Jumping Today?") is True

def test_retro_why_rising():
    assert is_retrospective_headline("Why Is NWL Stock Rising?") is True

def test_retro_why_gaining():
    assert is_retrospective_headline("Why Is Newell Brands Stock Gaining Friday?") is True

def test_retro_trading_higher_after():
    assert is_retrospective_headline("Southwest Airlines shares are trading higher after Spirit Airlines reports") is True

def test_retro_trading_lower_after():
    assert is_retrospective_headline("XYZ shares are trading lower after the company reported a loss") is True

def test_retro_shares_surging():
    assert is_retrospective_headline("XYZ shares surging on earnings beat") is True

def test_not_retro_fda_approval():
    assert is_retrospective_headline("Arvinas Receives FDA Approval For VEPPANU") is False

def test_not_retro_earnings_beat():
    assert is_retrospective_headline("Moderna Reports Better-Than-Expected Q1 Results") is False

def test_not_retro_acquisition():
    assert is_retrospective_headline("Inseego to Acquire Nokia FWA CPE Business") is False

def test_not_retro_phase_3():
    assert is_retrospective_headline("Rezolute Presents Expanded Phase 3 Data For Ersodetug") is False


# --- is_routine_news ---

def test_routine_monthly_auto_sales():
    assert is_routine_news("American Honda Reports April Sales Of 137,405 Units") is True

def test_routine_monthly_sales_generic():
    assert is_routine_news("Toyota Motor Reports Monthly US Sales") is True

def test_routine_shareholder_letter():
    assert is_routine_news("IonQ CEO Shareholder Letter Dated April 30") is True

def test_routine_ceo_letter():
    assert is_routine_news("CEO Letter To Shareholders") is True

def test_routine_annual_report():
    assert is_routine_news("Company Files Annual Report With SEC") is True

def test_not_routine_earnings_beat():
    assert is_routine_news("Moderna Beats Q1 Earnings Estimates, Raises Guidance") is False

def test_not_routine_fda():
    assert is_routine_news("FDA Grants Approval For New Drug") is False

def test_not_routine_acquisition():
    assert is_routine_news("Company Announces Acquisition Of Rival For $2B") is False


# --- compute_news_age_hours ---

def test_age_zero_for_just_published():
    now = datetime.now(timezone.utc)
    assert compute_news_age_hours(now) < 0.1

def test_age_two_hours():
    two_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    age = compute_news_age_hours(two_ago)
    assert 1.9 < age < 2.1

def test_age_prior_day():
    yesterday = datetime.now(timezone.utc) - timedelta(hours=20)
    assert compute_news_age_hours(yesterday) > 18.0

def test_age_naive_datetime_raises():
    naive = datetime(2026, 5, 1, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_news_age_hours(naive)
