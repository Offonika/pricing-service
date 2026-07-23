"""Allowlisted metadata for neutral cross-project data contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeVar

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ContractPolicy:
    contract_version: str
    source_project: str
    schema: str
    schema_sha256: str
    max_age: timedelta


class ContractPolicyRegistry(dict[str, ContractPolicy]):
    _retail_director_monthly_pattern = re.compile(
        r"retail-director-monthly/(?P<month>\d{4}-(?:0[1-9]|1[0-2]))/"
        r"retail-director-summary-(?P=month)\.json"
    )
    _retail_director_monthly_policy = ContractPolicy(
        "retail-director-monthly-snapshot.v2",
        "mm-compensation",
        "retail-director-monthly-snapshot.schema.json",
        "7306d10df7a489b985216ea856f1bbd7518a421bfd5deaeadd552e637cd70154",
        timedelta(days=45),
    )
    _bp_tax_accrual_monthly_pattern = re.compile(
        r"executive-dashboard/bp-tax-accruals/(?P<month>\d{4}-(?:0[1-9]|1[0-2]))/"
        r"bp-tax-accruals-(?P=month)\.json"
    )
    _bp_tax_accrual_monthly_policy = ContractPolicy(
        "executive-bp-tax-accrual-snapshot.v1",
        "mm-compensation",
        "executive-bp-tax-accrual-snapshot.schema.json",
        "12e2bb409c7aa468da086b2bf3a884633425cafae54b162f7734a22efe3188bf",
        timedelta(days=45),
    )

    def get(self, key: str, default: _T | None = None) -> ContractPolicy | _T | None:
        exact = super().get(key)
        if exact is not None:
            return exact
        if self._retail_director_monthly_pattern.fullmatch(key):
            return self._retail_director_monthly_policy
        if self._bp_tax_accrual_monthly_pattern.fullmatch(key):
            return self._bp_tax_accrual_monthly_policy
        return default


CONTRACT_POLICIES: ContractPolicyRegistry = ContractPolicyRegistry(
    {
        "executive-dashboard/bp_tax_snapshot.json": ContractPolicy(
            "executive-bp-tax-snapshot.v1",
            "mm-compensation",
            "executive-bp-tax-snapshot.schema.json",
            "9cc59b8044ef0771668da57ad6f97db81454cb81b8b3a8492d8e3a8075f6d330",
            timedelta(hours=48),
        ),
        "executive-dashboard/bp_balance_snapshot.json": ContractPolicy(
            "executive-bp-balance-snapshot.v1",
            "mm-compensation",
            "executive-bp-balance-snapshot.schema.json",
            "5284b96a1002cfc0c4d6639e780fe7e469f950b4a69aefad0e3639aeadfb0e10",
            timedelta(hours=48),
        ),
        ("executive-dashboard/management-opening-equity/2026-06-30/current.json"): ContractPolicy(
            "management-opening-equity-snapshot.v1",
            "mm-compensation",
            "management-opening-equity-snapshot.schema.json",
            "b2e546a4b7aa25bbc12983d4b77c84311944b96ee22a3cf3cffbd9fd680d57e3",
            timedelta(days=3650),
        ),
        "executive-dashboard/cashflow_period_cache.json": ContractPolicy(
            "executive-cashflow-period-cache.v1",
            "mm-compensation",
            "executive-cashflow-period-cache.schema.json",
            "1d1d26b6c85ad9978a909853dab7561fd6fb413832a612948c087fcfa52a8abf",
            timedelta(hours=48),
        ),
        "executive-dashboard/employee_payroll_balance_snapshot.json": ContractPolicy(
            "employee-payroll-balance-snapshot.v1",
            "mm-compensation",
            "employee-payroll-balance-snapshot.schema.json",
            "286781133fdffec5faeded1cf8502c28dcc2553c41dc7c4930821144cb106a00",
            timedelta(hours=48),
        ),
        "executive-dashboard/finance_snapshot.json": ContractPolicy(
            "executive-finance-snapshot.v1",
            "mm-compensation",
            "executive-finance-snapshot.schema.json",
            "310a8146c91d8430443ec1e6977f298ae040feb8e8f0df04280e9dace2360f41",
            timedelta(hours=48),
        ),
        "executive-dashboard/owner_cash_transit_snapshot.json": ContractPolicy(
            "executive-owner-cash-control.v1",
            "mm-compensation",
            "executive-owner-cash-control.schema.json",
            "2dcde2dedeb17bdcbf0e0a438c33e8922bbe4c7282d307c9977eb44bfd31b49f",
            timedelta(hours=48),
        ),
        "executive-dashboard/sales_plan_monthly_snapshot.json": ContractPolicy(
            "executive-sales-plan-snapshot.v1",
            "mm-compensation",
            "executive-sales-plan-snapshot.schema.json",
            "3198513de622239e1c93219b0eeed6a62a97a9d36c422c7612249463f6b552a0",
            timedelta(days=45),
        ),
        "executive-dashboard/service_accrual_source_snapshot.json": ContractPolicy(
            "executive-service-accrual-source.v1",
            "mm-compensation",
            "executive-service-accrual-source.schema.json",
            "b6fecdeea9bb956cbd7c7227d638cc47a85144eb2ecb999004331d3541f6b534",
            timedelta(days=14),
        ),
        "executive-dashboard/warehouse_snapshot.json": ContractPolicy(
            "executive-warehouse-snapshot.v1",
            "mm-compensation",
            "executive-warehouse-snapshot.schema.json",
            "a7e67c2b40d883d79ae9c2a54678adcb2f1ad2583df0d75777cd48477a24a194",
            timedelta(hours=48),
        ),
        "procurement/procurement_open_orders_snapshot.json": ContractPolicy(
            "executive-procurement-snapshot.v2",
            "pricing-service",
            "executive-procurement-snapshot.schema.json",
            "c6c5e7b6db3430808c6e524a8e2bc0f597a3b07e76f4a9b417033327e0b1b5b6",
            timedelta(hours=48),
        ),
    }
)
