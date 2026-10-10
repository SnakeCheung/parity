# 离线候选详情核验

`parity.candidate_validation` 只读写 JSON，不请求京东、不启动浏览器、不改变 E2B 生命周期。

## 生成计划

```bash
PYTHONPATH=src python -m parity.candidate_validation plan \
  --candidates artifacts/bridge/inbox/latest.json \
  --expected-spec 50ml --output artifacts/validation/plan.json
```

支持桥接服务器的 `items` 和搜索采集器的 `candidates`。规格由调用方明确传入，不从搜索卡片猜测。省略规格表示没有具体规格约束。

输出 `source` 保留采集元数据；`candidates` 复用 SearchCandidate 字段，`sources` 保存每条原始记录和输入索引；`tasks` 按 SKU 稳定去重，包含 `task_id`、`candidate_id` 和可直接传给 `CrawlRequest(**task['request'])` 的 `request`。

网页/移动版 URL 与查询参数差异不产生重复任务。SKU 与 URL 冲突、域名错误或缺少详情 URL 的记录保留为 `sku_mismatch`，不生成任务。

任务只是待执行数据，可交给现有详情采集流程逐条处理。本模块不实现自动批量访问或重试；遇到登录、验证码、403 或频控应停止并等待人工处理。

## 合并结果

```bash
PYTHONPATH=src python -m parity.candidate_validation merge \
  --plan artifacts/validation/plan.json \
  --details artifacts/item-a/result.json artifacts/item-b/result.json \
  --output artifacts/validation/merged.json
```

每个详情文件是现有 `CrawlResult.to_dict()` 的完整 JSON，兼容 E2B 导出的 `result.json`。按 `requested_url` SKU 关联，再检查最终页面 SKU，避免把重定向后的错误商品关联到其他候选。重复详情不自动择优：显式选择一个采集结果再合并。无法关联的结果保留在 `unmatched_details`。

| status | 含义 |
|---|---|
| unverified | 缺少详情、采集失败、多个结果歧义或店铺/HTTP 成功证据不足 |
| verified | 采集成功、HTTP 成功、SKU 一致、指定规格匹配、店铺确认且详情价格有效 |
| sku_mismatch | 候选身份冲突或详情 SKU 不一致 |
| spec_mismatch | 详情未能确认指定规格，包括不匹配和证据缺失 |
| price_evidence_insufficient | 无有效 DOM 价格或唯一且单 SKU 绑定的网络价格 |
| access_restricted | 403、429、登录、验证码或频控；优先于其他状态 |

规格匹配忽略空白和大小写，使用精确短语与数字边界：`50ml` 不匹配 `150ml`。不支持单位换算、套装语义或型号推理。

仅 `verified` 输出 `verified_price`。搜索价格始终作为 `candidate.page_price` 提示，不充当详情证据。未绑定 SKU 的 `network_price_candidates` 不能单独证明价格；网络价格要求成功响应、唯一目标 SKU 和唯一有效价格。`detail_results` 保留原始详情以供审计。核验不保证优惠券、地区、会员或最终结算价格。

## 测试

```bash
PYTHONPATH=src python -m pytest -q
```

包含两种候选格式、去重溯源、身份冲突、规格边界、价格不足、访问受限、结果关联、缺失详情、旧成功清除和 CLI 往返测试，无需真实京东访问。
