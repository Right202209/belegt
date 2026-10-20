# Spec: 04 §2.4（evidence-miner 输入：datapoint_ids + fact_keys，由工作流按 Mapping 与 02 evidence_requirements 展开）、
#       §2.5（calc-service：method_id/methodology_version/factor_set_version/inputs/params）、05 §2–§3、05 §8.1
"""活动数据 ``fact_key`` 目录与计算配方（DataPoint → belegt-calc 方法与输入绑定）。

只描述"需要什么"，不含任何数值。配方中的 ``decisions`` 是方法论参数中必须由人决定的项（CALC_PARAM_REQUIRES_DECISION
→ ClosedQuestion，05 CQ-METH-*）；``params`` 中的 ``{consolidation}``/``{grid_region}`` 从组织配置取值。
"""
from __future__ import annotations

from dataclasses import dataclass, field

FACTS: dict[str, dict] = {
    "activity.electricity.purchased": {"labels": ["Wirkarbeit", "Stromverbrauch", "Strombezug", "electricity consumption"],
                                       "unit": "MWh", "label": {"de": "Strombezug (Wirkarbeit)", "en": "Purchased electricity"}},
    "activity.electricity.eac": {"labels": ["Herkunftsnachweis", "HKN", "entwertet", "energy attribute"], "unit": "MWh",
                                 "label": {"de": "Entwertete Herkunftsnachweise", "en": "Cancelled energy attribute certificates"}},
    "activity.natural_gas.hi": {"labels": ["Erdgas"], "unit": "MWh",
                                "label": {"de": "Erdgasverbrauch (Heizwert Hi)", "en": "Natural gas consumption (net calorific value)"}},
    "activity.diesel.volume": {"labels": ["Diesel"], "unit": "L", "label": {"de": "Dieselverbrauch", "en": "Diesel consumption"}},
}


@dataclass(frozen=True)
class InputBinding:
    name: str
    fact_key: str
    unit: str
    optional: bool = False


@dataclass(frozen=True)
class Recipe:
    method_id: str
    methodology_version: str
    factor_set: str
    inputs: tuple[InputBinding, ...]
    params: dict[str, str] = field(default_factory=dict)
    decisions: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)  # param → (CQ id, options)


FS = "fs:belegt-example@2026.10"
CALC_RECIPES: dict[str, Recipe] = {
    "dp:env.ghg.scope1_total": Recipe(
        "ghg.scope1.total", "ghg-scope1@1.0.0", FS,
        (InputBinding("natural_gas_hi", "activity.natural_gas.hi", "MWh"), InputBinding("diesel_volume", "activity.diesel.volume", "L")),
        params={"consolidation": "{consolidation}"}, decisions={"gwp_set": ("CQ-METH-02", ("ar6", "ar5"))}),
    "dp:env.ghg.scope2_location_based": Recipe(
        "ghg.scope2.location_based", "ghg-scope2-location@1.0.0", FS,
        (InputBinding("electricity_purchased", "activity.electricity.purchased", "MWh"),),
        params={"consolidation": "{consolidation}", "grid_region": "{grid_region}"}),
    "dp:env.ghg.scope2_market_based": Recipe(
        "ghg.scope2.market_based", "ghg-scope2-market@1.0.0", FS,
        (InputBinding("electricity_purchased", "activity.electricity.purchased", "MWh"),
         InputBinding("electricity_contractual_renewable", "activity.electricity.eac", "MWh", optional=True)),
        params={"consolidation": "{consolidation}", "grid_region": "{grid_region}"}),
}
DECISION_LABELS = {
    "CQ-METH-02": {"de": "Welcher GWP-Satz soll für Scope 1 verwendet werden?", "en": "Which GWP set should Scope 1 use?"},
    "CQ-METH-03": {"de": "Welcher Konsolidierungsansatz gilt für diese Berechnung?",
                   "en": "Which consolidation approach applies to this calculation?"},
}
# belegt-calc 的 CALC_PARAM_REQUIRES_DECISION（``CalcError.decision``）→ 可选项（05 CQ-METH-*；与 belegt_calc 方法的 choices 一致）。
# 未登记的决策（如因子地理歧义，无 decision 号）不能生成合法的 single_choice（options 2–4 项），作为缺口转人工。
DECISION_OPTIONS: dict[str, tuple[str, ...]] = {
    "CQ-METH-02": ("ar6", "ar5"),
    "CQ-METH-03": ("operational_control", "financial_control", "equity_share"),
}
OPTION_LABELS = {"ar6": {"de": "IPCC AR6", "en": "IPCC AR6"}, "ar5": {"de": "IPCC AR5", "en": "IPCC AR5"},
                 "operational_control": {"de": "Operative Kontrolle", "en": "Operational control"},
                 "financial_control": {"de": "Finanzielle Kontrolle", "en": "Financial control"},
                 "equity_share": {"de": "Equity-Share-Ansatz", "en": "Equity share"}}


def fact_keys_for(dp_base: str) -> list[str]:
    r = CALC_RECIPES.get(dp_base)
    return [b.fact_key for b in r.inputs] if r else []
