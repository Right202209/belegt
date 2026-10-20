# belegt-calc

Belegt 的**确定性计算库**（Apache-2.0）。规格：`docs/05-计算方法论.md`（本库即其参考实现），哈希规则见
`docs/03-数据模型与证据图.md` §3.6、§4.5、§5.4、§5.6。

- 纯函数：不调用 LLM、网络、系统时钟、随机数或环境变量（05 §1.1）。
- 运行期只依赖 Python 3.12 标准库 + PyYAML（读因子库文件）。
- 数字一律 DEC-STR（十进制字符串），`decimal` 上下文 `prec=34`、`ROUND_HALF_EVEN`；结果只在写出时舍入一次。
- 每个结果都带可复现四元组 `(input_hash, factor_set_version, methodology_version, engine_version)`。

```bash
pip install -e packages/belegt-calc
pytest packages/belegt-calc            # 全部测试（含 TV-01…TV-11 逐位复现）
belegt-calc vectors verify packages/belegt-calc/tests/vectors
```

## 方法与 family（05 §1.5）

| family | method_id | 结果 | 默认位数 |
|---|---|---|---|
| `ghg-scope1@1.0.0` | `ghg.scope1.stationary_combustion` / `mobile_combustion` / `process` / `fugitive` / `total` | tCO2e | 3 |
| `ghg-scope2-location@1.0.0` | `ghg.scope2.location_based` | tCO2e | 3 |
| `ghg-scope2-market@1.0.0` | `ghg.scope2.market_based`（合同工具层级 1–5） | tCO2e | 3 |
| `pcf-iso14067-pact@1.0.0` | `pcf.cradle_to_gate`、`pcf.allocation`、`pcf.data_quality` | kgCO2e/声明单位、tCO2e、% | 4 / 3 / 2 |
| `pcf-catenax@0.1.0` | 预留（V2），调用即 `CALC_METHODOLOGY_UNSUPPORTED` | — | — |
| `cbam-see@1.0.0` | `cbam.see_simple`、`cbam.see_complex`、`cbam.see_default`、`cbam.sefa_default` | tCO2e/t | 6 |
| `cbam-cost-gap@1.0.0` | `cbam.cost_gap`（05 §6.6 修正公式） | EUR | 2 |
| `cbam-threshold@1.0.0` | `cbam.threshold_check`（50 t 门槛） | t | 3 |
| `period-align@1.0.0` | `period.aggregate`、`period.time_proration` | 同输入 | 3（EUR 为 2） |

`belegt-calc methods` 或 `list_methods()` 给出每个方法的必填输入、参数和默认位数；各方法模块文件头的
docstring 写明输入命名约定（如 `<fuel>_hs`、`precursor_<x>_mass`、`lot_<id>`）。

要点：

- **Scope 1**：AR5/AR6 由 `params.gwp_set` 显式给出（缺失 → `CALC_PARAM_REQUIRES_DECISION`，CQ-METH-02）；燃烧源 CH4 取
  AR6 non-fossil，逸散/过程取 fossil；混合工质按组分质量分数加权（R-410A：1923.5 / 2255.5）；Hs 计量的燃气账单必须有
  `fuel.<fuel>.hi_hs_ratio` 因子行，缺失报 `CALC_FACTOR_MISSING`，绝不默认 1；生物源 CO2 单列 `biogenic_co2`。
  `params.ef_basis`（`per_mwh_hi` 默认 / `per_gj_hi`）选择因子分母，`params.gases` 可限定计入的气体（默认 co2,ch4,n2o，
  某气体因子缺失且未被排除时报错，不会静默略去）。
- **Scope 2 market-based**：EAC（HKN）→ PPA → 供应商特定 → 残余组合 → 无残余组合时回退电网平均并标
  `residual_mix_unavailable=1`；合同电量超过消耗报 `CALC_ALLOCATION_IMBALANCE`。是否满足 8 项 Quality Criteria 是人工判断
  （CQ-METH-04），只有判定满足的工具才作为输入。
- **PCF**：cut-off（`params.recycling=cut_off`，回收废料零负荷）；分摊键由 `params.allocation` 显式给出（CQ-METH-05）；
  `pcf.allocation` 守恒逐位相等，尾差加到份额最大的产品并记 `rounding_residual`；`pcf.data_quality` 按 |E_i| 加权，
  未知按最坏情况 PDS 0%、DQR 5；同时输出含/不含生物源的 PCF；排除比例 > 3% 拒绝。
  `methods.pcf.decide_allocation()` 实现 05 §5.4 决策树，`methods.pcf.check_supplier_pcf()` 实现 §4.5 的 5 条供应商 PCF 校验。
- **CBAM**：钢铝氢只计直接排放进入证书数（Annex II）；默认值一律取 total 列再加价，加价按进口年度查
  `cbam.markup.<sector>.<y>`（无当年键时取不晚于 y 的最近年份键，体现 2028 年起 30%）；未列国家用 `OTHER` 行；
  `cbam.cost_gap` 严格按 §6.6：
  `N_def = max(0, M×(SEE_def,y − SEFA_def))`，`N_act = max(0, M×(SEE_act − SEFA_act) − CPP/P)`，`cost_gap = (N_def − N_act)×P`；
  价格季度/周必须与计算期间一致（否则 `CALC_PERIOD_MISMATCH`），未公布期间的价格不存在于因子库即 `CALC_FACTOR_MISSING`。
- **门槛**：每个日历年累计净质量，排除 CN 2716（电力）与 2804 10 00（氢）；恰好 50 t 仍豁免；门槛值为因子行。

## 精度与舍入（05 §1.2）

中间量以有理数 `Rat` 精确承载：加、减、乘精确；若一个有限小数中间量需要超过 34 位有效数字（即 `decimal` 中会触发
`Inexact`）报 `CALC_PRECISION_OVERFLOW`。所有除法保留在分母中，量化输出时才执行**唯一一次** `Decimal` 除法
（prec=34、HALF_EVEN），再量化到目标位数——这等价于把每个输出量的除法放在最后一步，避免"截断后的值再参与乘法"。
`breakdown` 按 10 位小数量化（例外：`pcf.data_quality` 的 `dqr_*` 是对外报告值，按 RND-1 的 2 位写出，另有
`dqr_*_unrounded`）。`params.rounding_digits` 可覆盖结果位数，覆盖值进入 `input_hash`。

单位换算只用无参数线性换算，系数为有理数：`GJ→MWh` 是 `÷3.6`（4444.4412 GJ → 1234.567 MWh 精确），不是 `×0.277778`。

## Python API（05 §8.1）

```python
from belegt_calc import calculate, input_hash, output_hash, replay, list_methods, load_factor_set, CalcError, InputValue, Period

fs = load_factor_set("fs:belegt-example@2026.10")      # 内置 ID 或 YAML 路径；加载时校验 rows_hash
res = calculate(
    "ghg.scope2.market_based", "ghg-scope2-market@1.0.0", fs,
    inputs=[
        InputValue("electricity_purchased", "ev_01JD8Z1A2B3C4D5E6F7G8H9J0K#v2", "4820", "MWh"),
        InputValue("electricity_contractual_renewable", "ev_01JE0TV03G0000000000000001#v1", "1200", "MWh"),
        InputValue("electricity_supplier_specific", "ev_01JE0TV03SP000000000000001#v1", "1000", "MWh"),
    ],
    params={"consolidation": "operational_control", "grid_region": "DE"},
    boundary="organization",
    period=Period("2025-01-01", "2025-12-31"),
)
res.result        # {'value': '1717.2', 'unit': 'tCO2e', 'rounding': {'mode': 'half_even', 'digits': 3}}
res.input_hash    # 'sha256:a4d9e6946ad1f718452a12076d29d27b44c615a77bcb247f363096a1106e236e'（= TV-05）
res.to_dict()     # Calculation 主体字段（不含 id/version/信封），可直接交给 belegt-core 写入
res.annotations   # 不属于 Calculation：is_estimate、gas_scope_co2_only、factor_year_gap、法条依据等，供定级参考

try:
    calculate("ghg.scope1.total", "ghg-scope1@1.0.0", fs, [...], {"consolidation": "operational_control"}, "organization", Period("2025-01-01", "2025-12-31"))
except CalcError as e:
    e.code, e.decision      # 'CALC_PARAM_REQUIRES_DECISION', 'CQ-METH-02' → calc-service 生成 ClosedQuestion

input_hash(m)                       # 03 §4.5：只取 M 的 8 个键；键序、inputs 顺序、"4820.0" 与 "4820" 无关
output_hash(res.result, res.breakdown)
rep = replay(res.to_dict(), fs)     # ReplayReport：ok / drift / input_hash_match / output_hash_match / diffs
```

错误码（05 文末）：`CALC_UNKNOWN_METHOD`、`CALC_METHODOLOGY_UNSUPPORTED`、`CALC_INPUT_MISSING`、
`CALC_PARAM_REQUIRES_DECISION`、`CALC_FACTOR_MISSING`、`CALC_FACTOR_SET_DEPRECATED`、`CALC_UNIT_INCOMPATIBLE`、
`CALC_DEC_INVALID`、`CALC_PRECISION_OVERFLOW`、`CALC_ALLOCATION_IMBALANCE`、`CALC_PERIOD_MISMATCH`，
以及本实现补充的 `CALC_FACTOR_SET_INTEGRITY`（rows_hash 与行不一致、使用未冻结草稿、同键多行无法唯一选择）。

## CLI（05 §8.2）

```bash
belegt-calc run --method ghg.scope1.total --mv ghg-scope1@1.0.0 \
    --factors fs/belegt-example@2026.10.yaml --input tv01.json > calc.json
belegt-calc hash --input tv01.json --factors fs/belegt-example@2026.10.yaml
belegt-calc replay calc.json --factors fs/belegt-example@2026.10.yaml    # 0=一致 3=漂移 4=input_hash 不一致 1=错误
belegt-calc vectors verify tests/vectors/                                  # FR-209 一致性套件
belegt-calc factors import --id fs:uba-strommix@2026.03 --csv rows.csv --license "[需法务确认]" \
    --published-at 2026-03-23 --valid-from 2023-01-01 --out uba.yaml       # → status: draft
belegt-calc factors freeze uba.yaml                                        # 计算 rows_hash，status: active
belegt-calc factors diff old.yaml new.yaml                                 # 新增/删除/变化的行
belegt-calc factors deprecate old.yaml --reason "上游撤回" --date 2026-11-01   # 不可逆；仍可重放
belegt-calc factors list | show <file>
belegt-calc verify-pack belegt-auditpack.zip [--factors-dir DIR] [--public-key key.hex]
belegt-calc methods
```

`tv01.json` 就是 `calculate` 的参数（05 §9 TV-01 的 JSON 块原样可用）。`--factors` 也接受内置 ID（`fs:belegt-example@2026.10`）。

`verify-pack`（03 §7 核验步骤 1–2）：校验 `manifest.json` 中每个文件的 sha256/bytes 与 `pack_hash`；由
`calculations/replay/*.json` 的原文 M 重算 `input_hash`；对 `calculations/calculations.jsonl` 中每个 Calculation 用包内
`calculations/factor_rows/<fs_id>.csv`（rows_hash 必须吻合）或 `--factors-dir`/内置因子库执行重放。`manifest.sig`
（Ed25519）仅在安装了可选依赖 `cryptography` 并给出公钥时校验，否则记为 skipped。

## 因子库（05 §7）

YAML 冻结格式（`BaseLoader` 读取，标量全为字符串，杜绝浮点）：

```yaml
id: fs:belegt-example@2026.10
source: belegt-example
license: Apache-2.0
published_at: '2026-10-18'
valid_from: '2025-01-01'
rows:
- {factor_key: fuel.natural_gas.co2.per_mwh_hi, geography: DE, year: '2025', value: '0.2016', unit: tCO2e/MWh, source_row_id: EX-NG-CO2}
row_count: 21
rows_hash: sha256:25cd7633e6907491a142f0c6cf90224f4403b25fb49957bacb67987bb71faaf9
status: active
```

`rows_hash` = JCS（按 `source_row_id` 升序的完整行对象数组）的 SHA-256；05 §7.1 的 UBA 示例（含 `quality`）按此复现
`sha256:12af011e…`（`tests/fixtures/uba-strommix@2026.03.yaml`）。选择规则（§7.4）：地理 精确 → EU → GLO；年份
同年 → 不晚于期间的最近年份（breakdown 标 `factor_year_gap`）→ 无年份行，绝不使用晚于期间的年份；状态 final >
preliminary > estimated；无匹配 `CALC_FACTOR_MISSING`，不跨库查找。

随包分发（`src/belegt_calc/fs/`，顶层 `fs/` 为其符号链接）：

| 因子库 | 用途 | rows_hash |
|---|---|---|
| `fs:belegt-example@2026.10`（21 行） | TV-01…06、09、10 | `sha256:25cd7633…faaf9` |
| `fs:belegt-example-cbam@2026.10`（7 行） | TV-07、08、11 | `sha256:27358e85…916e5` |
| `fs:belegt-example-cbam-price@2026Q3`（1 行，82.32 € 实价） | TV-08 成本差 | `sha256:03cd93e5…90191` |
| `fs:aib-residual-mix@2025.1`（2 行） | TV-04 / 03 §5.6 | 声明值 `sha256:35f78fd0…b460`（占位，见下） |
| `fs:belegt-null@1`（0 行） | 汇总、期间对齐 | `sha256:4f53cda1…` |

**示例库中的数值均为示例值（EX-），非官方因子，不得当作真实因子使用**；GWP 取 GHG Protocol GWP 表（2024-08），CBAM
加价与 CBAM factor、50 t 门槛为法定值，2026Q3 价格为 DG TAXUD 公布值。UBA/AIB/IPCC 数据的再分发许可待法务确认
（05 §7.2、待决 6），因此不随包分发真实 UBA/AIB 数据（UBA 示例只作测试夹具）。

## 测试向量（05 §9）

`tests/vectors/TV-01.json … TV-11.json`，每个文件含主情形与补充/错误情形（共 46 个），容差 0。主情形逐位复现文档：

| TV | 结果 | input_hash |
|---|---|---|
| TV-01 | 560.563 tCO2e（output_hash `0213d8ab…` 与 05 §8.3 一致） | `0952b505…` |
| TV-02 | 556.415 | `9e7df2a5…` |
| TV-03 | 1658.08 | `75bcf6a8…` |
| TV-04 | 2027.2 | `fa52b131cff377ebdaec89df24a2a00382ad4837e8dc577c3989a7a302ede446`（= 03 §5.6.8） |
| TV-05 | 1717.2 | `a4d9e694…` |
| TV-06 | A 2.91 / B 3.91；PDS 68.25 % / 76.37 %；DQR 1.79 / 1.79 / 1 | A `01b59ef8…` |
| TV-07 | SEE_dir 2.2025、SEE_ind 0.2422、SEC 0.2 | 文档未给 |
| TV-08 | SEE_def 2.849（2027：3.108）、SEFA_def 1.33575、cost_gap 99 315.29 € | `65b9f405…`、`8ee5ebc0…` |
| TV-09 | 225.148；缺 Hi/Hs 行 → `CALC_FACTOR_MISSING`；GJ↔MWh 精确 | 文档未给 |
| TV-10 | 2.012 / 2.174 / "0" / −2.012；先求和后舍入 4.025 | 文档未给 |
| TV-11 | 50 t → exempt；50.001 t → 不豁免 | 文档未给 |

`tools/build_vectors.py` 重新生成向量：它先用**从文档抄录**的期望值断言引擎输出，一致才写文件。修改已有向量的期望值
等同于方法论升级（05 §9），必须同时提升 `methodology_version`。

## 与文档的差异 / 文档问题

1. **03 §5.6 / TV-04 的 `rows_hash sha256:35f78fd0…b460` 是占位值**：参考脚本 `work/gen_example.py` 中为
   `sha256(b"example-aib-residual-mix-2025.1-rows")`，不是任何行集合的 JCS 哈希，不符合 05 §1.4/§7.1 的定义。为复现
   03/05 的 `input_hash`，`fs/aib-residual-mix@2025.1.yaml` 保留该声明值并标 `rows_hash_placeholder: "true"`，加载时给出警告；
   其他因子库 rows_hash 不符一律拒绝。建议下一版 03/05 改用真实 rows_hash（本库行的 JCS 哈希为 `sha256:c1bda2be…`），
   并同步更新 TV-04 的 input_hash。
2. **03 §5.6.8 的 `rounding.digits: 1` 与 05 §1.2 矛盾**：05 规定位数覆盖必须经 `params.rounding_digits` 进入
   `input_hash`，但该示例 params 中没有它（否则 input_hash 不是 `fa52b131…`）。本库按默认 3 位：`result.value` 同为 `2027.2`，
   `output_hash` 与 03 的 `12a0337f…` 不同（03 的值可由其文中 result/breakdown 复现，已在测试中验证 output_hash 实现）。
3. **05 §2.3 与 TV-01/§8.3**：§2.3 要求过程排放为零时也必须有人工确认证据，否则总量报 `CALC_INPUT_MISSING`；而 TV-01
   没有过程输入却期望 `process = 0`（且 output_hash 固定）。本库：汇总模式（子计算链）强制四项齐全；TV-01 的活动数据
   展示模式在无过程输入时记 0，并在 annotations 中注明。
4. **TV-07 的 `EF_el 0.581 tCO2/MWh`** 与 §1.3"tCO2 与 tCO2e 不混用"冲突；向量按 `tCO2e/MWh` 输入。
5. 05 §1.2 的 breakdown "10 位小数"与 TV-06 的 DQR 期望 `1.79`（RND-1 2 位）、TV-08 表中 `609.446064`/€ 2 位的展示值不同层：
   本库 breakdown 存 10 位（`609.4460641399`、`149484.888`、`50169.6`），测试另断言其舍入后等于文档表值；DQR 以 2 位写入。
6. 05 未定义 rows_hash 不一致时的错误码，本库补充 `CALC_FACTOR_SET_INTEGRITY`。
7. 05 §8.1 注释写"无运行期依赖除标准库"，但冻结因子库是 YAML；按 CONTRACTS 允许 PyYAML。
8. TV-09 所用示例库同时有 `per_mwh_hi`（含 CH4/N2O）与 `per_gj_hi`（仅 CO2）两组天然气因子，文档未说明如何选择；
   本库默认 `per_mwh_hi`（规范能量单位，TV-01 依赖此默认），TV-09 显式给 `ef_basis=per_gj_hi`、`gases=co2`。

## 许可

Apache-2.0，见 `LICENSE`。
