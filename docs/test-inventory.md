# 主测试套件清单（230 条）

> 本文件列出 `agent_eval/tests/` 下框架自身单元测试的完整清单。
> 数据取自 `pytest --collect-only` 实测结果；每条说明依据对应测试函数的实际断言撰写。
> 采集时间：2026-11-21　｜　测试函数 198 个（其中 8 个参数化，展开 40 条），合计 **230 条**。

## 口径说明

- **主测试套件**仅指 `agent_eval/tests/`。另有两套独立套件**不计入**本表：demo 门禁专项（`agent_eval/examples/demo_project/test_gate.py`，10 条）与通用案例库自检（`agent_eval/examples/universal_cases/test_universal_cases.py`，7 条）。
- 8 个函数带 `parametrize`，展开为 40 条用例（相对按函数计数净增 32 条）；表中「条数」列标出展开数量，并在说明后附参数取值。
- 说明中的「通过 / 失败」均指被测框架行为，不是测试自身的结果。本表全部用例当前状态为通过。

复现命令：

```bash
python3 -m pytest agent_eval/tests/ -q                     # 运行全部 230 条
python3 -m pytest agent_eval/tests/ --collect-only -q      # 列出全部 nodeid
python3 -m pytest agent_eval/tests/test_harness.py -v      # 单文件逐条查看
```

## 概览

| 文件 | 测试函数 | 参数化用例 | 用例数 | 主要覆盖 |
|---:|---:|---:|---:|---|
| `test_harness.py` | 30 | 21 | **48** | 断言引擎、批量执行、复跑、多轮会话、归因与门禁 |
| `test_judge.py` | 40 | 4 | **43** | LLM 语义判分：解析容错、三态、κ 校准、缓存与重试 |
| `test_safeguards.py` | 27 | 15 | **38** | 门禁可靠性回归：undetermined 三态、异常隔离、快照完整性 |
| `test_flywheel.py` | 38 | 0 | **38** | 数据飞轮：证据接地、指纹、失败回流、逐题 diff、性能 delta |
| `test_webui.py` | 23 | 0 | **23** | Web 控制台 API：序列化保真、CRUD、路径白名单、并发隔离 |
| `test_report.py` | 10 | 0 | **10** | 覆盖审计报告：账本构建、文本/HTML 渲染与转义 |
| `test_replay.py` | 9 | 0 | **9** | 快照派生与四档回放判定 |
| `test_evalset.py` | 9 | 0 | **9** | 用例集加载与字段校验 |
| `test_attribution.py` | 7 | 0 | **7** | 首错定位与分层归因优先级 |
| `test_coverage.py` | 5 | 0 | **5** | 覆盖率账本统计与审核规则 |
| **合计** | **198** | **40** | **230** | — |

## 明细

表中「条数」为该函数展开后的用例数；标注参数取值的行即参数化函数。

### `test_harness.py`（48 条）

| 测试函数 | 条数 | 验证内容 |
|---|---:|---|
| `test_must_call_pass_and_fail` | 1 | 验证 must_call 命中指定工具则通过、未命中则失败且失败详情提示缺少工具调用 |
| `test_must_call_required_args_missing` | 1 | 验证工具虽被调用但 required_args 指定参数缺失时判失败并提示缺少参数 |
| `test_agent_error_short_circuits` | 1 | 验证 Agent 抛异常时只产生 no_error 一条失败断言、其余断言短路且归因 system 层 |
| `test_attribution_priority_over_first_error` | 1 | 验证 must_call 与 state_after 同时挂时链路首错记 must_call、主归因仍按优先级取 system |
| `test_attribution_single_layer_no_promotion` | 1 | 验证只有 trajectory 层断言失败时主归因即为 trajectory、不误抬升到 system 层 |
| `test_attribution_semantic_fail_is_component` | 1 | 验证语义裁判判 fail 时该失败归因到 component 层、判分对象是提示词与回答行为 |
| `test_must_not_call_and_contains` | 1 | 验证 response_contains 通过而 must_not_call 命中禁用工具时恰有该一条断言失败 |
| `test_state_after_value_and_min_length` | 1 | 验证 state_after 按 min_length 判列表长度达标通过、按 value 校验状态值不符则失败 |
| `test_repeat_all_pass` | 1 | 验证复跑三次全部通过时 pass_runs 累计为 3 且该用例不标记 flaky |
| `test_repeat_flaky_detected` | 1 | 验证复跑一过一挂时按 pass^k 判失败、标记 flaky 并进入 flaky_cases 与报告文本 |
| `test_repeat_baseline_not_diluted` | 1 | 验证成功率按用例数计而不被复跑次数稀释，四条中一条挂一次仍得 75% |
| `test_concurrent_order_preserved` | 1 | 验证八用例四线程并发跑后结果仍按原用例顺序排列且八条全部通过 |
| `test_invalid_params_rejected` | 1 | 验证 repeat 设为 0 时会抛 ValueError 拒绝非法复跑参数、不会静默通过 |
| `test_gate_blocker_one_vote_veto` | 1 | 验证 blocker 级用例失败即一票否决门禁、即使总体成功率仍高达放行线 |
| `test_gate_baseline_drop` | 1 | 验证成功率较基线回落超阈值被门禁拦截、持平 75% 基线时则放行 |
| `test_multiturn_session_shared` | 1 | 验证多轮用例各轮共享同一 session_id、turn_index 从 0 递增且末轮 is_last_turn 为真 |
| `test_multiturn_turn_level_contains` | 1 | 验证轮次级 response_contains 断言落在对应轮输出上、第二轮命中关键词即通过 |
| `test_multiturn_turn_level_fail_message` | 1 | 验证轮次级断言失败时失败详情明确标注第 2 轮以便定位 |
| `test_required_args_must_be_present_on_one_call` | 6 | 验证 required_args 必须由同一次工具调用凑齐、不跨调用拼接，覆盖单轮/会话/轮次级与参数完整与否<br>参数取值：`single-False`、`single-True`、`session-False`、`session-True`、`turn-False`、`turn-True` |
| `test_multiturn_tool_assertions_only_inspect_their_own_turn` | 1 | 验证轮次级 must_call 与 must_not_call 只检查本轮工具、两条失败详情均标注第 2 轮 |
| `test_multiturn_missing_run_fails_without_falling_back` | 3 | 验证缺轮时直接判 no_error 失败并标注所缺轮次、绝不回退末轮，覆盖缺 0/1/2 轮<br>参数取值：`0`、`1`、`2` |
| `test_evalset_validates_assertions_at_both_scopes` | 12 | 验证非法断言字段加载报错须含精确字段路径，覆盖案例级/轮次级与多类坏字段<br>参数取值：`spec0-must_call-False`、`spec0-must_call-True`、`spec1-must_call[0]-False`、`spec1-must_call[0]-True`、`spec2-must_not_call[0]-False`、`spec2-must_not_call[0]-True`、`spec3-must_call[0].required_args-False`、`spec3-must_call[0].required_args-True`、`spec4-must_call[0].required_args-False`、`spec4-must_call[0].required_args-True`、`spec5-response_contains[0]-False`、`spec5-response_contains[0]-True` |
| `test_multiturn_must_call_union` | 1 | 验证案例级 must_call 看全程工具并集、工具仅在首轮调用仍判通过 |
| `test_multiturn_must_not_call_union_catches` | 1 | 验证案例级 must_not_call 看全程并集、任一轮出现禁用工具即失败且首错指向它 |
| `test_multiturn_response_contains_on_final_turn` | 1 | 验证案例级 response_contains 只检查最后一轮输出、首轮无关键词不影响通过 |
| `test_multiturn_error_in_later_turn` | 1 | 验证第 2 轮抛异常时 no_error 失败、详情标注轮次且归因 system 层 |
| `test_write_snapshots_jsonl` | 1 | 验证快照每轮一条含轮次/工具/查询等字段，重复写入以追加方式累计行数 |
| `test_compare_reports_and_format` | 1 | 验证报告对比算出新增失败/修复与 flaky 变化、blocker 新增失败即判 investigate |
| `test_compare_reports_noise_band` | 1 | 验证成功率差值落在 3pp 噪声带内时对比判 invalid 作废、不误判提升 |
| `test_compare_reports_promote` | 1 | 验证成功率提升 15pp 且无 blocker 失败时判 promote、文本含回放判定 |

### `test_judge.py`（43 条）

| 测试函数 | 条数 | 验证内容 |
|---|---:|---|
| `test_cohen_kappa_perfect_and_inverse` | 1 | 验证完全一致时科恩卡帕等于一，逐项完全反向时等于负一 |
| `test_cohen_kappa_corrects_chance_agreement` | 1 | 验证全判通过时一致率零点九但卡帕近零，纠正二值斯皮尔曼的假象 |
| `test_cohen_kappa_both_constant_identical` | 1 | 验证双方边际同为全通过标签时完全一致，卡帕取一而非除零异常 |
| `test_agreement_rate` | 1 | 验证四组标签中三组一致时，观测一致率按零点七五计算 |
| `test_parse_plain_json` | 1 | 验证不含噪声的纯 JSON 可解析出通过判定与证据数组内容 |
| `test_parse_with_code_fence_and_noise` | 1 | 验证带前缀说明与 json 代码围栏的输出仍能解析出 fail 判定 |
| `test_parse_invalid_verdict_becomes_need_review` | 1 | 验证判定词为非法值 maybe 时降级为 need_review 待人工复核，不硬判失败 |
| `test_parse_garbage_becomes_need_review` | 1 | 验证完全非 JSON 的乱码输出也降级为 need_review，绝不抛解析异常 |
| `test_judge_failure_degrades_to_need_review` | 1 | 验证判分器调用抛错时降级 need_review、证据标注调用降级且错误留档一条 |
| `test_judge_prompt_contains_criteria_query_output` | 1 | 验证判分提示词含全部标准、问题、回答，且系统提示使用固定常量 |
| `test_judge_renders_dialogue_separately_from_truncated_execution_evidence` | 1 | 验证末轮问答单列、历史对话按轮完整呈现，工具证据截断带标记且不含 turns 键 |
| `test_calibrate_passes_on_perfect_labels` | 1 | 验证机器标签与金标完全相同时一致率与卡帕均为一、通过校准且待复核计数为零 |
| `test_calibrate_chance_agreement_fails_kappa_gate` | 1 | 验证全判通过一致率零点九过粗筛，但卡帕为零被拦下不予放行 |
| `test_calibrate_below_agreement_gate_not_qualified` | 1 | 验证一致率仅零点八低于门槛时不算合格，与卡帕无关 |
| `test_calibrate_need_review_is_a_separate_category` | 1 | 验证 need_review 独立计入分母且不与人工失败算一致，卡帕降为零点七五不放行 |
| `test_calibrate_all_need_review_stays_in_denominator` | 1 | 验证全为 need_review 时仍计入分母，样本数二百、一致率与卡帕皆零且不合格 |
| `test_calibrate_rejects_invalid_labels` | 4 | 覆盖机器侧未知空值、人工侧待复核与大写标签，均抛值错误并定位到首条<br>参数取值：`unknown-fail-machine`、`None-fail-machine`、`pass-need_review-human`、`pass-FAIL-human` |
| `test_calibrate_rejects_small_goldset` | 1 | 验证金标集仅一百九十九条不足二百时抛值错误，拒绝小样本校准 |
| `test_calibrate_rejects_mismatched_lengths` | 1 | 验证机器与人工标签数组长度不等时抛值错误，拒绝错位校准 |
| `test_l0_never_runs_judge` | 1 | 验证 L0 层级零次大模型调用，语义断言直接跳过且不影响用例通过 |
| `test_uncalibrated_judge_is_skipped_at_l1` | 1 | 验证未经金标校准的判分器在 L1 被跳过，断言详情注明未通过校准 |
| `test_calibrated_judge_fail_fails_case_at_l1` | 1 | 验证已校准判分器判失败时使用例不通过、语义断言不跳过且归因组件层 |
| `test_calibrated_judge_pass_passes_case_at_l1` | 1 | 验证已校准判分器判通过时用例顺利通过，语义断言正常生效 |
| `test_need_review_does_not_hard_fail` | 1 | 验证判分器返回待复核时只转人工不硬判失败，用例仍算通过 |
| `test_no_judge_criteria_no_semantic_assertion` | 1 | 验证用例未配置判分标准时不生成任何语义断言 |
| `test_run_all_level_flows_to_semantic_and_report_stats` | 1 | 验证批量运行按判定分流统计，失败计入语义统计且报告含语义判分小节 |
| `test_semantic_fail_in_blocker_blocks_gate` | 1 | 验证阻断级用例语义判分失败时门禁拒绝放行，理由点明阻断级 |
| `test_load_judge_from_env_missing` | 1 | 验证三个模型环境变量全部缺失时加载函数返回空而不构造判分器 |
| `test_load_judge_from_env_full` | 1 | 验证三个环境变量齐全时可构造判分器，模型名与密钥正确透传客户端 |
| `test_openai_client_direct_opener` | 1 | 验证兼容客户端直接使用完整接口地址，末尾保留会话补全路径 |
| `test_judge_cache_hits_on_identical_input` | 1 | 验证输入完全相同时命中缓存仅调用一次，两次判定结果一致 |
| `test_judge_cache_does_not_hide_different_output` | 1 | 验证仅回答内容不同时不复用缓存，模型被实际调用两次 |
| `test_judge_cache_distinguishes_earlier_turns_with_identical_final_answer` | 1 | 验证末轮回答相同但首轮不同时缓存仍区分，提示词各含对应首轮回答 |
| `test_judge_need_review_not_cached` | 1 | 验证待复核结果不入缓存，证据恢复后允许重新判分并通过 |
| `test_judge_retry_then_succeeds` | 1 | 验证前两次超时后第三次重试成功返回通过，累计调用三次 |
| `test_judge_all_retries_fail_degrades` | 1 | 验证重试全部耗尽后降级待复核、证据注明重试，调用次数为重试上限加一 |
| `test_result_status_is_explicit_not_parsed_from_detail` | 1 | 验证断言状态取显式字段与跳过标志，不靠解析详情文案猜测 |
| `test_semantic_stats_and_review_cases_use_status` | 1 | 验证待复核计入语义统计与待复核用例清单，报告文本标注待复核 |
| `test_repeat_keeps_multi_turn_trajectories` | 1 | 验证重复运行三轮的多轮用例仍保留两轮轨迹，快照写出两条记录 |
| `test_snapshot_records_semantic_evidence` | 1 | 验证快照写入语义断言失败状态与判分不达标详情，可供复核追溯 |

### `test_safeguards.py`（38 条）

| 测试函数 | 条数 | 验证内容 |
|---|---:|---|
| `test_all_skipped_blocker_is_undetermined` | 1 | 验证全跳过的阻断级用例判未定，不计通过率、确定样本归零并标记阻断级未定 |
| `test_gate_rejects_blocker_undetermined` | 1 | 验证阻断级用例未有效测试时门禁拒绝，理由必须点明未有效测试 |
| `test_non_blocker_undetermined_only_annotated` | 1 | 验证非阻断级未定只标注不阻断，通过率分母仅算有效判定仍得满分 |
| `test_need_review_case_excluded_from_rate` | 1 | 验证待复核属弃判，不计已判数与通过率分子分母，仅入待复核清单 |
| `test_mixed_skipped_with_effective_assertion_still_passed` | 1 | 验证存在有效断言时被跳过的语义断言只作标注，用例仍判通过 |
| `test_case_status_three_states` | 1 | 验证三态口径：有失败断言判失败、全跳过判未定、有效通过判通过 |
| `test_empty_report_gate_rejects` | 1 | 验证手工构造的空报告直接送门禁也必须拒绝，理由含空报告 |
| `test_run_all_rejects_empty_cases` | 1 | 验证空用例列表触发空跑批值错误，不允许静默产出空报告 |
| `test_run_all_rejects_bad_dataset_type` | 4 | 覆盖整数、字典与混合数组等非法数据集类型，均抛数据集值错误<br>参数取值：`3`、`bad1`、`bad2`、`bad3` |
| `test_tool_result_used_explicit_values_dedup` | 1 | 验证显式取值重复去重后凑不满最小条数，工具结果断言判失败 |
| `test_output_grounded_in_dedup_and_shared_helper` | 1 | 验证去重口径统一，归一化后逗号与全角同值算重复，去重后仅剩一组 |
| `test_custom_judge_bad_verdict_degrades_need_review` | 5 | 覆盖空值、字符串、非法判定、证据非数组与整数，一律降级待复核不误拦截阻断级<br>参数取值：`None`、`maybe`、`raw2`、`raw3`、`42` |
| `test_custom_judge_exception_does_not_abort_batch` | 1 | 验证判分器抛异常不中断整批，该用例转未定而后续用例照常通过 |
| `test_judge_accepts_trace_keyword_only_signature` | 1 | 验证仅 trace 形参才收含工具调用的轨迹，ctx 与三参签名不误判且均通过 |
| `test_normalize_verdict_whitelist` | 1 | 验证白名单仅认小写通过，大写判定与证据非数组一律降级待复核 |
| `test_adapter_exception_isolated_single_and_concurrent` | 2 | 覆盖单线程与四并发，坏适配器异常转为系统层失败且好用例仍通过<br>参数取值：`1`、`4` |
| `test_compare_reports_both_legacy_without_hash_uncomparable` | 1 | 验证两旧报告都缺用例指纹时判无效，标记缺指纹且不认可提升可合并 |
| `test_evalset_id_non_string_rejected` | 4 | 覆盖列表、字典、整数与布尔等非字符串编号，给清晰值错误而非不可哈希报错<br>参数取值：`bad_id0`、`bad_id1`、`123`、`True` |
| `test_evalset_required_query_type_rejected` | 1 | 验证问题字段传数字等错误类型时抛必填字段或类型错误的值错误 |
| `test_multiturn_last_turn_versions_change_config_sig` | 1 | 验证末轮模型版本变化必改配置签名，顶层与逐题版本口径保持一致 |
| `test_perf_summary_uses_last_run` | 1 | 验证性能汇总与逐题均取末轮，延迟九十与令牌五十一致不矛盾 |
| `test_snapshot_nonserializable_fields` | 1 | 验证日期、集合、自定义对象与非法字典键均可落盘且能被反序列化 |
| `test_snapshot_atomic_write_no_partial_file` | 1 | 验证序列化先于写盘完成，失败抛错时不留下半批残文件 |
| `test_snapshot_session_id_is_effective_value` | 1 | 验证自动生成的会话号写进快照，以固定前缀开头且等于用例结果值 |
| `test_empty_runs_via_batch_runner` | 1 | 验证多轮返回空轨迹不崩溃，用例判失败归因系统层且快照写零条 |
| `test_client_runtime_error_degrades_need_review` | 1 | 验证客户端抛运行时异常时降级待复核，错误明细留档含异常类型 |
| `test_report_to_dict_exposes_undetermined_fields` | 1 | 验证报告字典保留全部旧键并新增未定系列字段，逐题状态同步为未定 |

### `test_flywheel.py`（38 条）

| 测试函数 | 条数 | 验证内容 |
|---|---:|---|
| `test_judge_prompt_contains_trace_evidence` | 1 | 验证裁判 user 提示含执行证据块、工具名与返回值，系统提示强调别只看状态 |
| `test_judge_cache_key_includes_trace` | 1 | 验证回答相同但工具返回值不同时缓存键随轨迹变、触发重判而非命中旧缓存 |
| `test_judge_cache_still_hits_without_trace` | 1 | 验证不传轨迹时相同请求仍命中缓存、裁判仅被调用一次 |
| `test_judge_backward_compatible_with_3arg_impl` | 1 | 验证三参数旧版自定义 judge 不被强传 trace 参数且用例仍通过 |
| `test_merged_trace_collects_all_turns` | 1 | 验证多轮轨迹合并会把前几轮工具结果与召回文档一并纳入证据 |
| `test_harness_passes_trace_to_judge` | 1 | 验证 harness 向支持 trace 的裁判传入含工具返回值的执行轨迹 |
| `test_norm_value_normalizes_formats` | 1 | 验证 _norm_value 去除逗号与货币符、全角数字转半角并统一小写 |
| `test_tool_result_used_passes_with_value_normalization` | 1 | 验证回答数值经格式归一后与工具返回值匹配即通过 tool_result_used 断言 |
| `test_tool_result_used_catches_case_b` | 1 | 验证工具成功返回但回答未采用结果时仅 tool_result_used 失败、must_call 仍通过 |
| `test_tool_result_used_skipped_when_adapter_reports_nothing` | 1 | 验证适配器未上报工具返回值时接地断言标 skipped 而非误判 Agent 出错 |
| `test_tool_result_used_fails_when_tool_not_called` | 1 | 验证断言指定工具根本未被调用时判失败并提示未被调用 |
| `test_output_grounded_in_pass_and_fail` | 1 | 验证回答含足量工具返回值则接地通过、缺支撑则报缺乏执行证据支撑 |
| `test_output_grounded_in_supports_retrieved_and_explicit_values` | 1 | 验证显式 values 与 retrieved 召回均可作证据、召回有据却不被回答采用则失败 |
| `test_output_grounded_in_skipped_without_any_evidence` | 1 | 验证完全无执行证据时接地断言标 skipped 且用例判通过 |
| `test_grounding_assertion_layer_is_trajectory` | 1 | 验证接地断言通过时归因留空、失败时才归到 trajectory 层 |
| `test_load_evalset_dataset_filter` | 1 | 验证按 dataset 筛选加载用例、缺省归入 regression、筛选无匹配时抛没有 dataset 错 |
| `test_dataset_counts` | 1 | 验证 dataset_counts 按分层统计用例条数、未标注 dataset 的用例默认计入 regression 层 |
| `test_run_all_dataset_filter_and_empty_guard` | 1 | 验证 run_all 按 dataset 过滤并回显该值、筛出空集时抛拒绝空跑批错 |
| `test_fingerprint_env_and_config_sig` | 1 | 验证每次 run_id 唯一、相同配置 config_sig 可比、换 env 后配置签名必变 |
| `test_fingerprint_reads_eval_env` | 1 | 验证 EVAL_ENV 环境变量设为 preprod 时报告 env 随之取该值、无需显式传参 |
| `test_perf_summary` | 1 | 验证性能汇总算出平均延迟 100ms、总 token 15 与有延迟用例数 |
| `test_report_text_shows_fingerprint_and_perf` | 1 | 验证报告文本同时含运行指纹与性能指标两个段落 |
| `test_snapshot_records_new_fields` | 1 | 验证快照落盘含 run_id、env、dataset、工具返回值及延迟与 token 用量等新字段 |
| `test_case_to_dict_roundtrip` | 1 | 验证用例经 case_to_dict 导出再加载后 dataset、judge_criteria 与接地断言字段均不丢失 |
| `test_collect_failures_content` | 1 | 验证失败回流含 env、失败断言、工具实际值与 REG 前缀建议用例及 review_hint |
| `test_export_and_promote_failures` | 1 | 验证失败样本导出后 promote 另存 e.promoted.json、原集不变且追加 REG 用例 |
| `test_promote_avoids_id_collision_and_validates` | 1 | 验证 promote 遇同名 id 自动加后缀 -2 避撞、非法整集拒绝落盘且不留临时文件 |
| `test_promote_rejects_empty_selection` | 1 | 验证按不存在的 id 筛选回流时抛 ValueError 且提示没有选中 |
| `test_report_to_dict_full_fields` | 1 | 验证报告字典含回答、工具返回值、延迟、dataset 及指纹环境与性能汇总 |
| `test_case_diff_detects_output_and_assertion_changes` | 1 | 验证 _case_diff 记录回答与断言状态变更、工具调用未变则不计入 diff |
| `test_compare_reports_case_diffs_and_perf` | 1 | 验证报告对比输出逐题 diff 与延迟/token 增量并判 promote、文本含相应段落 |
| `test_compare_reports_env_mismatch_invalidates` | 1 | 验证新旧报告 env 不一致时判 invalid、判定与文本均标明环境不一致 |
| `test_compare_reports_legacy_format_still_works` | 1 | 验证缺指纹与性能字段的旧报告仍可对比、diff 与性能增量为空且照常 promote |
| `test_compare_rejects_removed_failed_cases` | 1 | 验证删掉失败用例凑高成功率时列出 removed_cases、判数据集不一致并作废 |
| `test_compare_rejects_changed_expectation_with_same_id` | 1 | 验证同 id 用例期望被改动致成功率变化时标 changed_cases 并判 invalid |
| `test_repeat_exports_first_failed_trajectory` | 1 | 验证 flaky 复跑仅回流首个失败轨迹与断言、runs 只两条且归因 trajectory |
| `test_evalset_rejects_bad_new_fields` | 1 | 验证 dataset 类型错、接地字段缺 tool、min_values 为零或坏来源等非法值均被拒 |
| `test_evalset_accepts_valid_new_fields` | 1 | 验证合法 dataset 与两类接地断言新字段可正常加载、min_values 等取值不丢 |

### `test_webui.py`（23 条）

| 测试函数 | 条数 | 验证内容 |
|---|---:|---|
| `test_serialize_report_exposes_status_and_skipped` | 1 | 报告序列化时无裁判的语义断言状态为 skipped 且跳过统计计为一 |
| `test_serialize_report_failed_semantic_status` | 1 | 语义裁判判定不达标时断言 ok 为假且状态被标为 failed |
| `test_save_evalset_keeps_unknown_fields` | 1 | 编辑保存用例时评判标准、上下文覆盖与 persona 等新字段均不丢失 |
| `test_save_evalset_cleans_tmp_on_validation_failure` | 1 | 用例缺 severity 校验失败时抛异常且不留临时文件、原文件不被破坏 |
| `test_validate_case_normalizes_judge_criteria_string` | 1 | 校验用例时多行评判标准字符串按行去空白与空行归一为列表 |
| `test_validate_case_rejects_duplicate_id` | 1 | 校验用例遇到与已有用例重复的编号时抛出含已存在的异常 |
| `test_resolve_evalset_path_rejects_unregistered` | 1 | 解析评估集路径时未注册的白名单外文件被拒绝并抛异常 |
| `test_generic_http_adapter_uses_configured_method` | 1 | 通用 HTTP 适配器采用配置中指定的 PUT 方法而非默认值 |
| `test_generic_http_adapter_default_method_post` | 1 | 构造通用 HTTP 适配器时未配置请求方法则默认采用 POST 提交 |
| `test_generic_http_adapter_failure_becomes_agent_error` | 1 | HTTP 请求失败被转成 AgentRun 的错误字段并含调用失败提示 |
| `test_build_custom_adapter_rejects_empty_file` | 1 | 构建 python 自定义适配器时脚本文件为空被拒绝并抛异常 |
| `test_build_custom_adapter_rejects_non_py` | 1 | 构建自定义适配器时传入非 .py 后缀文件被拒绝并抛异常 |
| `test_ledger_units_is_public_readonly_view` | 1 | 覆盖账本 units 返回副本，外部清空列表不会改动账本内容 |
| `test_build_adapter_does_not_touch_globals` | 1 | 构建红绿两版 demo 适配器不再改动 BUGS 与通知日志全局状态 |
| `test_scoped_adapters_isolated_in_concurrent_batches` | 1 | 红绿批次并发跑互不串扰，修复版成功率满而缺陷版显著更低 |
| `test_demo_health_returns_true` | 1 | 进程内 demo 适配器探活方法恒返回真而不再抛属性错误 |
| `test_serialize_report_exposes_fingerprint_and_perf` | 1 | 报告暴露环境数据集运行号指纹、令牌用量、工具结果与用例数据集 |
| `test_serialize_failures_are_candidates_not_answers` | 1 | 失败回流生成 REG 前缀候选用例，错误输出仅作证据不进预期答案 |
| `test_safe_datasets_handles_bad_path` | 1 | 读取不存在的数据集文件路径时安全返回空字典而不抛异常 |
| `test_promote_endpoint_rejects_unregistered_path` | 1 | 回灌端点按白名单拒绝未注册路径，库函数仍产出 e.promoted.json 文件 |
| `test_validate_case_keeps_flywheel_fields` | 1 | 用例编辑表单保留数据集、工具结果引用与输出接地等飞轮新字段 |
| `test_serialize_report_exposes_undetermined` | 1 | 全跳过时报告暴露未定计数、阻断级未定清单、用例未定状态并保留旧键 |
| `test_demo_evalset_label_count` | 1 | 注册表 demo 标签标注九条用例须与评估集实际条数一致 |

### `test_report.py`（10 条）

| 测试函数 | 条数 | 验证内容 |
|---|---:|---|
| `test_build_ledger_from_spec` | 1 | 从规格加载账本统计三单元一覆盖两未覆盖并标出高风险豁免单元 |
| `test_build_ledger_rejects_bad_layer` | 1 | 从规格构建账本时非法评估层级取值被拒绝并抛出 layer 异常 |
| `test_build_ledger_rejects_bad_reason` | 1 | 从规格构建账本时未知的未覆盖原因被拒绝并抛校验异常 |
| `test_build_ledger_requires_unit_id` | 1 | 规格条目缺少单元编号 unit_id 时构建账本抛出键错误 |
| `test_text_report_shows_rate_and_high_risk` | 1 | 文本覆盖报告显示 33.3% 覆盖率并列出高风险未覆盖单元告警 |
| `test_text_report_clean_when_no_high_risk` | 1 | 无高风险未覆盖单元时文本报告显示高风险单元零豁免 |
| `test_html_report_written_and_complete` | 1 | HTML 报告落盘且含覆盖率、审计不通过横幅、三层原因明细与单文件无外链 |
| `test_html_report_audit_pass_banner` | 1 | 单元全部已覆盖且无高风险时 HTML 报告显示审计通过横幅 |
| `test_html_escaping` | 1 | HTML 报告对单元编号与描述中的脚本标签做转义避免注入 |
| `test_build_ledger_matches_manual` | 1 | 规格构建的账本与手工账本在总数覆盖数及原因分布上完全一致 |

### `test_replay.py`（9 条）

| 测试函数 | 条数 | 验证内容 |
|---|---:|---|
| `test_derive_single_variable_ok` | 1 | 派生快照仅改提示词版本时成功且知识库等其他变量保持锁定 |
| `test_derive_rejects_multi_variable` | 1 | 派生快照同时改动提示词与模型两个变量时抛出回放错误 |
| `test_derive_rejects_zero_variable` | 1 | 派生快照未指定任何变量改动时抛出回放错误 |
| `test_snapshot_not_overwritable` | 1 | 同一追踪编号的快照重复保存被拒绝以防覆盖既有快照 |
| `test_gate_promote` | 1 | 成功率提升十五个百分点时门禁判定为可提升 PROMOTE |
| `test_gate_investigate` | 1 | 成功率下降二十五个百分点时门禁判定为 INVESTIGATE 需排查 |
| `test_gate_hold_fills_the_missing_band` | 1 | 成功率下降十个百分点落在补齐的缺档区间时判为保持 |
| `test_gate_invalid_in_noise_band` | 1 | 成功率仅降一个百分点处于噪声带时门禁判为无效 |
| `test_replay_runs_only_derived_snapshot` | 1 | 回放仅执行派生变体快照，基线分数由外部传入而不重跑 |

### `test_evalset.py`（9 条）

| 测试函数 | 条数 | 验证内容 |
|---|---:|---|
| `test_load_valid` | 1 | 合法评估集加载后严重级别与必调工具等字段被正确解析 |
| `test_empty_array_rejected` | 1 | 评估集为空数组时加载被拒绝并提示须为非空数组 |
| `test_missing_required_field` | 1 | 评估集用例缺少 severity 必填字段时加载被拒并提示缺少必填字段 |
| `test_invalid_severity_rejected` | 1 | 评估集用例严重级别取非法值 fatal 时加载被拒绝并提示级别非法 |
| `test_duplicate_id_rejected` | 1 | 评估集内出现重复用例编号时加载被拒绝并提示重复 |
| `test_list_field_wrong_type_rejected` | 1 | 响应包含字段传入字符串而非数组时加载被拒绝并提示应为数组 |
| `test_must_call_entry_rejected` | 1 | 必调工具条目写成字符串缺少工具键时加载被拒绝 |
| `test_response_contains_non_string_rejected` | 1 | 响应包含列表混入数字等非字符串元素时加载被拒绝 |
| `test_non_dict_item_rejected` | 1 | 评估集数组元素为字符串而非对象时加载被拒绝 |

### `test_attribution.py`（7 条）

| 测试函数 | 条数 | 验证内容 |
|---|---:|---|
| `test_first_error_picks_earliest` | 1 | 链路归因取最早失败步骤 s2 而非其后同样失败的步骤 |
| `test_first_error_none_when_all_pass` | 1 | 链路所有步骤均通过时首个错误查找器返回空结果 |
| `test_inconclusive_steps_collected` | 1 | 结论未定 ok 为空的步骤被单独收集，通过步骤不计入其中 |
| `test_single_attribution_prefers_system` | 1 | 三层同时命中归因时按优先级取系统层作为唯一归因 |
| `test_single_attribution_falls_through` | 1 | 仅组件层命中时单一归因回退取组件层而不报错 |
| `test_single_attribution_rejects_empty` | 1 | 归因输入为空字典时单一归因被拒绝并抛取值异常 |
| `test_blame_once_keeps_one_per_layer` | 1 | 同层多条错误经去重后每层仅保留首条归因记录 |

### `test_coverage.py`（5 条）

| 测试函数 | 条数 | 验证内容 |
|---|---:|---|
| `test_report_counts_reasons` | 1 | 账本统计三项全未覆盖并按原因给出中文分布计数 |
| `test_covered_unit_cannot_be_waived` | 1 | 已覆盖单元被标记风险豁免时抛异常，禁止既覆盖又豁免 |
| `test_uncovered_without_reason_fails_audit` | 1 | 未覆盖单元未标注原因时生成报告被拒绝，审计无法通过 |
| `test_high_risk_uncovered_fails_audit` | 1 | 高风险单元以风险豁免未覆盖时审核结论为不通过 |
| `test_layer_breakdown` | 1 | 账本按系统层与轨迹层分别拆解覆盖与未覆盖数量 |

## 维护约定

1. 新增或删除测试后，用 `python3 -m pytest agent_eval/tests/ --collect-only -q` 核对总数，并同步更新本表的概览数字、明细行与下方索引。
2. README 与 `agent_eval/README.md` 中的测试条数以 `--collect-only` 实测值为准；历史漂移（158 → 190 → 230）已修正，勿再写死数字而不复核。
3. 参数化函数新增参数时，只需更新该行的条数与参数取值，不必新增行。

## 附录：完整 nodeid 索引（230 条）

<details>
<summary>展开查看全部用例 ID</summary>

**`test_harness.py`（48 条）**

```text
agent_eval/tests/test_harness.py::test_must_call_pass_and_fail
agent_eval/tests/test_harness.py::test_must_call_required_args_missing
agent_eval/tests/test_harness.py::test_agent_error_short_circuits
agent_eval/tests/test_harness.py::test_attribution_priority_over_first_error
agent_eval/tests/test_harness.py::test_attribution_single_layer_no_promotion
agent_eval/tests/test_harness.py::test_attribution_semantic_fail_is_component
agent_eval/tests/test_harness.py::test_must_not_call_and_contains
agent_eval/tests/test_harness.py::test_state_after_value_and_min_length
agent_eval/tests/test_harness.py::test_repeat_all_pass
agent_eval/tests/test_harness.py::test_repeat_flaky_detected
agent_eval/tests/test_harness.py::test_repeat_baseline_not_diluted
agent_eval/tests/test_harness.py::test_concurrent_order_preserved
agent_eval/tests/test_harness.py::test_invalid_params_rejected
agent_eval/tests/test_harness.py::test_gate_blocker_one_vote_veto
agent_eval/tests/test_harness.py::test_gate_baseline_drop
agent_eval/tests/test_harness.py::test_multiturn_session_shared
agent_eval/tests/test_harness.py::test_multiturn_turn_level_contains
agent_eval/tests/test_harness.py::test_multiturn_turn_level_fail_message
agent_eval/tests/test_harness.py::test_required_args_must_be_present_on_one_call[single-False]
agent_eval/tests/test_harness.py::test_required_args_must_be_present_on_one_call[single-True]
agent_eval/tests/test_harness.py::test_required_args_must_be_present_on_one_call[session-False]
agent_eval/tests/test_harness.py::test_required_args_must_be_present_on_one_call[session-True]
agent_eval/tests/test_harness.py::test_required_args_must_be_present_on_one_call[turn-False]
agent_eval/tests/test_harness.py::test_required_args_must_be_present_on_one_call[turn-True]
agent_eval/tests/test_harness.py::test_multiturn_tool_assertions_only_inspect_their_own_turn
agent_eval/tests/test_harness.py::test_multiturn_missing_run_fails_without_falling_back[0]
agent_eval/tests/test_harness.py::test_multiturn_missing_run_fails_without_falling_back[1]
agent_eval/tests/test_harness.py::test_multiturn_missing_run_fails_without_falling_back[2]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec0-must_call-False]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec0-must_call-True]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec1-must_call[0]-False]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec1-must_call[0]-True]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec2-must_not_call[0]-False]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec2-must_not_call[0]-True]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec3-must_call[0].required_args-False]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec3-must_call[0].required_args-True]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec4-must_call[0].required_args-False]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec4-must_call[0].required_args-True]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec5-response_contains[0]-False]
agent_eval/tests/test_harness.py::test_evalset_validates_assertions_at_both_scopes[spec5-response_contains[0]-True]
agent_eval/tests/test_harness.py::test_multiturn_must_call_union
agent_eval/tests/test_harness.py::test_multiturn_must_not_call_union_catches
agent_eval/tests/test_harness.py::test_multiturn_response_contains_on_final_turn
agent_eval/tests/test_harness.py::test_multiturn_error_in_later_turn
agent_eval/tests/test_harness.py::test_write_snapshots_jsonl
agent_eval/tests/test_harness.py::test_compare_reports_and_format
agent_eval/tests/test_harness.py::test_compare_reports_noise_band
agent_eval/tests/test_harness.py::test_compare_reports_promote
```

**`test_judge.py`（43 条）**

```text
agent_eval/tests/test_judge.py::test_cohen_kappa_perfect_and_inverse
agent_eval/tests/test_judge.py::test_cohen_kappa_corrects_chance_agreement
agent_eval/tests/test_judge.py::test_cohen_kappa_both_constant_identical
agent_eval/tests/test_judge.py::test_agreement_rate
agent_eval/tests/test_judge.py::test_parse_plain_json
agent_eval/tests/test_judge.py::test_parse_with_code_fence_and_noise
agent_eval/tests/test_judge.py::test_parse_invalid_verdict_becomes_need_review
agent_eval/tests/test_judge.py::test_parse_garbage_becomes_need_review
agent_eval/tests/test_judge.py::test_judge_failure_degrades_to_need_review
agent_eval/tests/test_judge.py::test_judge_prompt_contains_criteria_query_output
agent_eval/tests/test_judge.py::test_judge_renders_dialogue_separately_from_truncated_execution_evidence
agent_eval/tests/test_judge.py::test_calibrate_passes_on_perfect_labels
agent_eval/tests/test_judge.py::test_calibrate_chance_agreement_fails_kappa_gate
agent_eval/tests/test_judge.py::test_calibrate_below_agreement_gate_not_qualified
agent_eval/tests/test_judge.py::test_calibrate_need_review_is_a_separate_category
agent_eval/tests/test_judge.py::test_calibrate_all_need_review_stays_in_denominator
agent_eval/tests/test_judge.py::test_calibrate_rejects_invalid_labels[unknown-fail-machine]
agent_eval/tests/test_judge.py::test_calibrate_rejects_invalid_labels[None-fail-machine]
agent_eval/tests/test_judge.py::test_calibrate_rejects_invalid_labels[pass-need_review-human]
agent_eval/tests/test_judge.py::test_calibrate_rejects_invalid_labels[pass-FAIL-human]
agent_eval/tests/test_judge.py::test_calibrate_rejects_small_goldset
agent_eval/tests/test_judge.py::test_calibrate_rejects_mismatched_lengths
agent_eval/tests/test_judge.py::test_l0_never_runs_judge
agent_eval/tests/test_judge.py::test_uncalibrated_judge_is_skipped_at_l1
agent_eval/tests/test_judge.py::test_calibrated_judge_fail_fails_case_at_l1
agent_eval/tests/test_judge.py::test_calibrated_judge_pass_passes_case_at_l1
agent_eval/tests/test_judge.py::test_need_review_does_not_hard_fail
agent_eval/tests/test_judge.py::test_no_judge_criteria_no_semantic_assertion
agent_eval/tests/test_judge.py::test_run_all_level_flows_to_semantic_and_report_stats
agent_eval/tests/test_judge.py::test_semantic_fail_in_blocker_blocks_gate
agent_eval/tests/test_judge.py::test_load_judge_from_env_missing
agent_eval/tests/test_judge.py::test_load_judge_from_env_full
agent_eval/tests/test_judge.py::test_openai_client_direct_opener
agent_eval/tests/test_judge.py::test_judge_cache_hits_on_identical_input
agent_eval/tests/test_judge.py::test_judge_cache_does_not_hide_different_output
agent_eval/tests/test_judge.py::test_judge_cache_distinguishes_earlier_turns_with_identical_final_answer
agent_eval/tests/test_judge.py::test_judge_need_review_not_cached
agent_eval/tests/test_judge.py::test_judge_retry_then_succeeds
agent_eval/tests/test_judge.py::test_judge_all_retries_fail_degrades
agent_eval/tests/test_judge.py::test_result_status_is_explicit_not_parsed_from_detail
agent_eval/tests/test_judge.py::test_semantic_stats_and_review_cases_use_status
agent_eval/tests/test_judge.py::test_repeat_keeps_multi_turn_trajectories
agent_eval/tests/test_judge.py::test_snapshot_records_semantic_evidence
```

**`test_safeguards.py`（38 条）**

```text
agent_eval/tests/test_safeguards.py::test_all_skipped_blocker_is_undetermined
agent_eval/tests/test_safeguards.py::test_gate_rejects_blocker_undetermined
agent_eval/tests/test_safeguards.py::test_non_blocker_undetermined_only_annotated
agent_eval/tests/test_safeguards.py::test_need_review_case_excluded_from_rate
agent_eval/tests/test_safeguards.py::test_mixed_skipped_with_effective_assertion_still_passed
agent_eval/tests/test_safeguards.py::test_case_status_three_states
agent_eval/tests/test_safeguards.py::test_empty_report_gate_rejects
agent_eval/tests/test_safeguards.py::test_run_all_rejects_empty_cases
agent_eval/tests/test_safeguards.py::test_run_all_rejects_bad_dataset_type[3]
agent_eval/tests/test_safeguards.py::test_run_all_rejects_bad_dataset_type[bad1]
agent_eval/tests/test_safeguards.py::test_run_all_rejects_bad_dataset_type[bad2]
agent_eval/tests/test_safeguards.py::test_run_all_rejects_bad_dataset_type[bad3]
agent_eval/tests/test_safeguards.py::test_tool_result_used_explicit_values_dedup
agent_eval/tests/test_safeguards.py::test_output_grounded_in_dedup_and_shared_helper
agent_eval/tests/test_safeguards.py::test_custom_judge_bad_verdict_degrades_need_review[None]
agent_eval/tests/test_safeguards.py::test_custom_judge_bad_verdict_degrades_need_review[maybe]
agent_eval/tests/test_safeguards.py::test_custom_judge_bad_verdict_degrades_need_review[raw2]
agent_eval/tests/test_safeguards.py::test_custom_judge_bad_verdict_degrades_need_review[raw3]
agent_eval/tests/test_safeguards.py::test_custom_judge_bad_verdict_degrades_need_review[42]
agent_eval/tests/test_safeguards.py::test_custom_judge_exception_does_not_abort_batch
agent_eval/tests/test_safeguards.py::test_judge_accepts_trace_keyword_only_signature
agent_eval/tests/test_safeguards.py::test_normalize_verdict_whitelist
agent_eval/tests/test_safeguards.py::test_adapter_exception_isolated_single_and_concurrent[1]
agent_eval/tests/test_safeguards.py::test_adapter_exception_isolated_single_and_concurrent[4]
agent_eval/tests/test_safeguards.py::test_compare_reports_both_legacy_without_hash_uncomparable
agent_eval/tests/test_safeguards.py::test_evalset_id_non_string_rejected[bad_id0]
agent_eval/tests/test_safeguards.py::test_evalset_id_non_string_rejected[bad_id1]
agent_eval/tests/test_safeguards.py::test_evalset_id_non_string_rejected[123]
agent_eval/tests/test_safeguards.py::test_evalset_id_non_string_rejected[True]
agent_eval/tests/test_safeguards.py::test_evalset_required_query_type_rejected
agent_eval/tests/test_safeguards.py::test_multiturn_last_turn_versions_change_config_sig
agent_eval/tests/test_safeguards.py::test_perf_summary_uses_last_run
agent_eval/tests/test_safeguards.py::test_snapshot_nonserializable_fields
agent_eval/tests/test_safeguards.py::test_snapshot_atomic_write_no_partial_file
agent_eval/tests/test_safeguards.py::test_snapshot_session_id_is_effective_value
agent_eval/tests/test_safeguards.py::test_empty_runs_via_batch_runner
agent_eval/tests/test_safeguards.py::test_client_runtime_error_degrades_need_review
agent_eval/tests/test_safeguards.py::test_report_to_dict_exposes_undetermined_fields
```

**`test_flywheel.py`（38 条）**

```text
agent_eval/tests/test_flywheel.py::test_judge_prompt_contains_trace_evidence
agent_eval/tests/test_flywheel.py::test_judge_cache_key_includes_trace
agent_eval/tests/test_flywheel.py::test_judge_cache_still_hits_without_trace
agent_eval/tests/test_flywheel.py::test_judge_backward_compatible_with_3arg_impl
agent_eval/tests/test_flywheel.py::test_merged_trace_collects_all_turns
agent_eval/tests/test_flywheel.py::test_harness_passes_trace_to_judge
agent_eval/tests/test_flywheel.py::test_norm_value_normalizes_formats
agent_eval/tests/test_flywheel.py::test_tool_result_used_passes_with_value_normalization
agent_eval/tests/test_flywheel.py::test_tool_result_used_catches_case_b
agent_eval/tests/test_flywheel.py::test_tool_result_used_skipped_when_adapter_reports_nothing
agent_eval/tests/test_flywheel.py::test_tool_result_used_fails_when_tool_not_called
agent_eval/tests/test_flywheel.py::test_output_grounded_in_pass_and_fail
agent_eval/tests/test_flywheel.py::test_output_grounded_in_supports_retrieved_and_explicit_values
agent_eval/tests/test_flywheel.py::test_output_grounded_in_skipped_without_any_evidence
agent_eval/tests/test_flywheel.py::test_grounding_assertion_layer_is_trajectory
agent_eval/tests/test_flywheel.py::test_load_evalset_dataset_filter
agent_eval/tests/test_flywheel.py::test_dataset_counts
agent_eval/tests/test_flywheel.py::test_run_all_dataset_filter_and_empty_guard
agent_eval/tests/test_flywheel.py::test_fingerprint_env_and_config_sig
agent_eval/tests/test_flywheel.py::test_fingerprint_reads_eval_env
agent_eval/tests/test_flywheel.py::test_perf_summary
agent_eval/tests/test_flywheel.py::test_report_text_shows_fingerprint_and_perf
agent_eval/tests/test_flywheel.py::test_snapshot_records_new_fields
agent_eval/tests/test_flywheel.py::test_case_to_dict_roundtrip
agent_eval/tests/test_flywheel.py::test_collect_failures_content
agent_eval/tests/test_flywheel.py::test_export_and_promote_failures
agent_eval/tests/test_flywheel.py::test_promote_avoids_id_collision_and_validates
agent_eval/tests/test_flywheel.py::test_promote_rejects_empty_selection
agent_eval/tests/test_flywheel.py::test_report_to_dict_full_fields
agent_eval/tests/test_flywheel.py::test_case_diff_detects_output_and_assertion_changes
agent_eval/tests/test_flywheel.py::test_compare_reports_case_diffs_and_perf
agent_eval/tests/test_flywheel.py::test_compare_reports_env_mismatch_invalidates
agent_eval/tests/test_flywheel.py::test_compare_reports_legacy_format_still_works
agent_eval/tests/test_flywheel.py::test_compare_rejects_removed_failed_cases
agent_eval/tests/test_flywheel.py::test_compare_rejects_changed_expectation_with_same_id
agent_eval/tests/test_flywheel.py::test_repeat_exports_first_failed_trajectory
agent_eval/tests/test_flywheel.py::test_evalset_rejects_bad_new_fields
agent_eval/tests/test_flywheel.py::test_evalset_accepts_valid_new_fields
```

**`test_webui.py`（23 条）**

```text
agent_eval/tests/test_webui.py::test_serialize_report_exposes_status_and_skipped
agent_eval/tests/test_webui.py::test_serialize_report_failed_semantic_status
agent_eval/tests/test_webui.py::test_save_evalset_keeps_unknown_fields
agent_eval/tests/test_webui.py::test_save_evalset_cleans_tmp_on_validation_failure
agent_eval/tests/test_webui.py::test_validate_case_normalizes_judge_criteria_string
agent_eval/tests/test_webui.py::test_validate_case_rejects_duplicate_id
agent_eval/tests/test_webui.py::test_resolve_evalset_path_rejects_unregistered
agent_eval/tests/test_webui.py::test_generic_http_adapter_uses_configured_method
agent_eval/tests/test_webui.py::test_generic_http_adapter_default_method_post
agent_eval/tests/test_webui.py::test_generic_http_adapter_failure_becomes_agent_error
agent_eval/tests/test_webui.py::test_build_custom_adapter_rejects_empty_file
agent_eval/tests/test_webui.py::test_build_custom_adapter_rejects_non_py
agent_eval/tests/test_webui.py::test_ledger_units_is_public_readonly_view
agent_eval/tests/test_webui.py::test_build_adapter_does_not_touch_globals
agent_eval/tests/test_webui.py::test_scoped_adapters_isolated_in_concurrent_batches
agent_eval/tests/test_webui.py::test_demo_health_returns_true
agent_eval/tests/test_webui.py::test_serialize_report_exposes_fingerprint_and_perf
agent_eval/tests/test_webui.py::test_serialize_failures_are_candidates_not_answers
agent_eval/tests/test_webui.py::test_safe_datasets_handles_bad_path
agent_eval/tests/test_webui.py::test_promote_endpoint_rejects_unregistered_path
agent_eval/tests/test_webui.py::test_validate_case_keeps_flywheel_fields
agent_eval/tests/test_webui.py::test_serialize_report_exposes_undetermined
agent_eval/tests/test_webui.py::test_demo_evalset_label_count
```

**`test_report.py`（10 条）**

```text
agent_eval/tests/test_report.py::test_build_ledger_from_spec
agent_eval/tests/test_report.py::test_build_ledger_rejects_bad_layer
agent_eval/tests/test_report.py::test_build_ledger_rejects_bad_reason
agent_eval/tests/test_report.py::test_build_ledger_requires_unit_id
agent_eval/tests/test_report.py::test_text_report_shows_rate_and_high_risk
agent_eval/tests/test_report.py::test_text_report_clean_when_no_high_risk
agent_eval/tests/test_report.py::test_html_report_written_and_complete
agent_eval/tests/test_report.py::test_html_report_audit_pass_banner
agent_eval/tests/test_report.py::test_html_escaping
agent_eval/tests/test_report.py::test_build_ledger_matches_manual
```

**`test_replay.py`（9 条）**

```text
agent_eval/tests/test_replay.py::test_derive_single_variable_ok
agent_eval/tests/test_replay.py::test_derive_rejects_multi_variable
agent_eval/tests/test_replay.py::test_derive_rejects_zero_variable
agent_eval/tests/test_replay.py::test_snapshot_not_overwritable
agent_eval/tests/test_replay.py::test_gate_promote
agent_eval/tests/test_replay.py::test_gate_investigate
agent_eval/tests/test_replay.py::test_gate_hold_fills_the_missing_band
agent_eval/tests/test_replay.py::test_gate_invalid_in_noise_band
agent_eval/tests/test_replay.py::test_replay_runs_only_derived_snapshot
```

**`test_evalset.py`（9 条）**

```text
agent_eval/tests/test_evalset.py::test_load_valid
agent_eval/tests/test_evalset.py::test_empty_array_rejected
agent_eval/tests/test_evalset.py::test_missing_required_field
agent_eval/tests/test_evalset.py::test_invalid_severity_rejected
agent_eval/tests/test_evalset.py::test_duplicate_id_rejected
agent_eval/tests/test_evalset.py::test_list_field_wrong_type_rejected
agent_eval/tests/test_evalset.py::test_must_call_entry_rejected
agent_eval/tests/test_evalset.py::test_response_contains_non_string_rejected
agent_eval/tests/test_evalset.py::test_non_dict_item_rejected
```

**`test_attribution.py`（7 条）**

```text
agent_eval/tests/test_attribution.py::test_first_error_picks_earliest
agent_eval/tests/test_attribution.py::test_first_error_none_when_all_pass
agent_eval/tests/test_attribution.py::test_inconclusive_steps_collected
agent_eval/tests/test_attribution.py::test_single_attribution_prefers_system
agent_eval/tests/test_attribution.py::test_single_attribution_falls_through
agent_eval/tests/test_attribution.py::test_single_attribution_rejects_empty
agent_eval/tests/test_attribution.py::test_blame_once_keeps_one_per_layer
```

**`test_coverage.py`（5 条）**

```text
agent_eval/tests/test_coverage.py::test_report_counts_reasons
agent_eval/tests/test_coverage.py::test_covered_unit_cannot_be_waived
agent_eval/tests/test_coverage.py::test_uncovered_without_reason_fails_audit
agent_eval/tests/test_coverage.py::test_high_risk_uncovered_fails_audit
agent_eval/tests/test_coverage.py::test_layer_breakdown
```

</details>
