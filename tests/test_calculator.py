"""
test_calculator.py
Unit tests for deterministic SR 11-7 financial ratio calculation functions.
"""
import pytest
from loan_agent.calculator import (
    get_fico_score,
    has_active_bankruptcy,
    get_total_delinquencies_12m,
    count_nsf_overdraft_events,
    calculate_verified_monthly_income,
    calculate_stated_monthly_income,
    calculate_income_variance_pct,
    calculate_monthly_installment,
    calculate_bureau_monthly_debt,
    calculate_undisclosed_monthly_debt,
    calculate_total_post_loan_debt,
    calculate_post_loan_dti,
    calculate_avg_ending_balance,
    calculate_payment_cushion_ratio,
    compute_financial_ratios,
)
from loan_agent.mock_apis import (
    fetch_experian_credit_report,
    fetch_plaid_bank_statement,
    prefilter_underwriting_transactions,
)


class TestFicoAndCreditBureau:
    def test_get_fico_score_valid(self, clean_credit_report):
        assert get_fico_score(clean_credit_report) == 750

    def test_get_fico_score_missing(self):
        assert get_fico_score({}) == 0

    def test_has_active_bankruptcy_discharged_is_false(self, clean_credit_report):
        # A discharged bankruptcy should not count as active
        assert has_active_bankruptcy(clean_credit_report) is False

    def test_has_active_bankruptcy_active_is_true(self, subprime_credit_report):
        assert has_active_bankruptcy(subprime_credit_report) is True

    def test_has_active_bankruptcy_empty(self):
        assert has_active_bankruptcy({}) is False

    def test_get_total_delinquencies_clean(self, clean_credit_report):
        assert get_total_delinquencies_12m(clean_credit_report) == 0

    def test_get_total_delinquencies_subprime(self, subprime_credit_report):
        # 2 (30d) + 1 (60d) + 1 (90+d) = 4 total
        assert get_total_delinquencies_12m(subprime_credit_report) == 4


class TestLedgerCashFlowMetrics:
    def test_count_nsf_overdraft_events(self, categorized_transactions_clean, categorized_transactions_distressed):
        assert count_nsf_overdraft_events(categorized_transactions_clean) == 0
        assert count_nsf_overdraft_events(categorized_transactions_distressed) == 3

    def test_calculate_verified_monthly_income(self, categorized_transactions_clean):
        # Total payroll: 6 x 4000 = 24,000 across 3 months -> 8,000 / month
        verified = calculate_verified_monthly_income(categorized_transactions_clean, statement_months=3.0)
        assert verified == 8000.00

    def test_calculate_verified_monthly_income_ignores_gifts_and_outflows(self):
        txs = [
            {"category": "VERIFIED_PAYROLL_INCOME", "amount": 3000.00},
            {"category": "ONE_OFF_GIFT_OR_TRANSFER_IGNORE", "amount": 5000.00},
            {"category": "VERIFIED_PAYROLL_INCOME", "amount": -100.00},  # negative amount ignored
        ]
        assert calculate_verified_monthly_income(txs, statement_months=1.0) == 3000.00

    def test_calculate_stated_monthly_income(self):
        assert calculate_stated_monthly_income(120000.0) == 10000.00
        assert calculate_stated_monthly_income(0.0) == 0.0
        assert calculate_stated_monthly_income(-5000.0) == 0.0

    def test_calculate_income_variance_pct(self):
        # Exactly matching income -> 0% variance
        assert calculate_income_variance_pct(120000.0, 10000.0) == 0.0
        # Stated $10,000/mo, verified $8,000/mo -> 20% variance
        assert calculate_income_variance_pct(120000.0, 8000.0) == 20.0
        # Zero stated income edge case
        assert calculate_income_variance_pct(0.0, 5000.0) == 0.0
        assert calculate_income_variance_pct(0.0, 0.0) == 100.0


class TestDebtAndInstallmentCalculations:
    def test_calculate_monthly_installment_standard(self):
        # $15,000 at 12% APR for 36 months is approx $498.21
        pmt = calculate_monthly_installment(15000.0, annual_rate=0.12, term_months=36)
        assert pmt == 498.21

    def test_calculate_monthly_installment_zero_or_none(self):
        assert calculate_monthly_installment(0.0) == 0.0
        assert calculate_monthly_installment(None) == 0.0
        assert calculate_monthly_installment(-1000.0) == 0.0

    def test_calculate_bureau_monthly_debt(self, clean_credit_report):
        # 450 + 950 = 1400.00
        assert calculate_bureau_monthly_debt(clean_credit_report) == 1400.00

    def test_calculate_undisclosed_monthly_debt(self, categorized_transactions_distressed):
        # One BNPL transaction of $450 over 3 months -> $150/month
        debt = calculate_undisclosed_monthly_debt(categorized_transactions_distressed, statement_months=3.0)
        assert debt == 150.00

    def test_calculate_total_post_loan_debt(self):
        # 1400 bureau + 150 undisclosed + 498.21 new loan = 2048.21
        total = calculate_total_post_loan_debt(1400.00, 150.00, 498.21)
        assert total == 2048.21

    def test_calculate_post_loan_dti(self):
        # 2000 debt / 8000 income * 100 = 25.0%
        assert calculate_post_loan_dti(2000.00, 8000.00) == 25.0
        # Zero income protection
        assert calculate_post_loan_dti(2000.00, 0.0) == 999.0

    def test_calculate_avg_ending_balance(self, clean_bank_statement):
        # (8500 + 9200 + 9000) / 3 = 8900.00
        assert calculate_avg_ending_balance(clean_bank_statement) == 8900.00

    def test_calculate_avg_ending_balance_empty(self):
        assert calculate_avg_ending_balance({}) == 0.0

    def test_calculate_payment_cushion_ratio(self):
        # $8,900 reserve / $498.21 payment = 17.86x
        assert calculate_payment_cushion_ratio(8900.00, 498.21) == 17.86
        assert calculate_payment_cushion_ratio(5000.00, 0.0) == 0.0


class TestMasterRatioEngineIntegration:
    def test_compute_financial_ratios_sarah_persona(self):
        """Sarah Jenkins: prime credit score, clean tradelines, healthy auto-approval DTI."""
        credit = fetch_experian_credit_report("4401")
        bank = fetch_plaid_bank_statement("4401")
        # Sarah's prefiltered transactions categorized
        categorized = [
            {"category": "VERIFIED_PAYROLL_INCOME", "amount": 3800.00},
            {"category": "MATCHED_BUREAU_DEBT", "amount": -950.00},
            {"category": "MATCHED_BUREAU_DEBT", "amount": -450.00},
            {"category": "VERIFIED_PAYROLL_INCOME", "amount": 3800.00},
            {"category": "VERIFIED_PAYROLL_INCOME", "amount": 3800.00},
            {"category": "MATCHED_BUREAU_DEBT", "amount": -950.00},
            {"category": "MATCHED_BUREAU_DEBT", "amount": -450.00},
            {"category": "VERIFIED_PAYROLL_INCOME", "amount": 3800.00},
            {"category": "VERIFIED_PAYROLL_INCOME", "amount": 3800.00},
            {"category": "MATCHED_BUREAU_DEBT", "amount": -950.00},
            {"category": "MATCHED_BUREAU_DEBT", "amount": -450.00},
            {"category": "VERIFIED_PAYROLL_INCOME", "amount": 3800.00},
        ]
        ratios = compute_financial_ratios(
            credit_report=credit,
            bank_raw=bank,
            categorized_txs=categorized,
            stated_annual_income=96000.0,
            loan_amount_requested=15000.0,
        )

        assert ratios["fico_score"] == 745
        assert ratios["has_active_bankruptcy"] is False
        assert ratios["total_delinquencies_12m"] == 0
        assert ratios["nsf_count_90d"] == 0
        assert ratios["verified_monthly_income"] == 7600.00
        assert ratios["stated_monthly_income"] == 8000.00
        assert ratios["income_variance_pct"] == 5.0
        assert ratios["bureau_monthly_debt"] == 1400.00
        assert ratios["undisclosed_monthly_debt"] == 0.0
        assert ratios["estimated_new_loan_payment"] == 498.21
        assert ratios["post_loan_dti"] == 24.98
        assert ratios["avg_ending_balance"] == 6500.00
        assert ratios["payment_cushion_ratio"] == 13.05
