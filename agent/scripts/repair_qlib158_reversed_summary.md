# Qlib158 CSI300 2023-2026 反转因子修复汇总

存活门槛：`ic_mean > 0.02` 且 `ic_positive_ratio >= 0.55` 且 `|t| > 2`。

基线测评：alive **1** / reversed **22** / dead **131**（`cntd60` 原本存活）。

## 修复动作

- 成功写回：**22** 个反转因子（原公式保留在 `_compute_raw`）
- 放弃：0
- 修复后存活库：alive **23** / reversed **0** / dead **131**

## 已修复明细

| id | attempt | ic_mean | ic+ | ir |
|---|---|---:|---:|---:|
| qlib158_cord60 | sign_flip | 0.0443 | 0.567 | 0.171 |
| qlib158_std30 | sign_flip | 0.0442 | 0.561 | 0.162 |
| qlib158_ksft2 | sign_flip | 0.0427 | 0.565 | 0.152 |
| qlib158_std60 | sign_flip | 0.0425 | 0.554 | 0.147 |
| qlib158_klen | sign_flip | 0.0420 | 0.576 | 0.169 |
| qlib158_std10 | sign_flip | 0.0413 | 0.569 | 0.155 |
| qlib158_std20 | sign_flip | 0.0402 | 0.573 | 0.152 |
| qlib158_std5 | sign_flip | 0.0395 | 0.560 | 0.151 |
| qlib158_ksft | sign_flip | 0.0387 | 0.582 | 0.132 |
| qlib158_cntn60 | sign_flip | 0.0383 | 0.557 | 0.119 |
| qlib158_klow | sign_flip | 0.0381 | 0.552 | 0.157 |
| qlib158_max60 | sign_flip | 0.0374 | 0.556 | 0.117 |
| qlib158_cord30 | sign_flip | 0.0320 | 0.550 | 0.128 |
| qlib158_kmid | sign_flip | 0.0307 | 0.559 | 0.107 |
| qlib158_max20 | sign_flip_smooth10 | 0.0414 | 0.563 | 0.134 |
| qlib158_cntn30 | sign_flip_smooth10 | 0.0351 | 0.553 | 0.116 |
| qlib158_max10 | sign_flip_smooth5 | 0.0384 | 0.558 | 0.126 |
| qlib158_cntn20 | sign_flip_smooth5 | 0.0325 | 0.554 | 0.113 |
| qlib158_max30 | sign_flip_smooth30 | 0.0411 | 0.578 | 0.130 |
| qlib158_cord20 | sign_flip_smooth30 | 0.0369 | 0.578 | 0.147 |
| qlib158_kmid2 | sign_flip_tsrank4 | 0.0253 | 0.560 | 0.095 |
| qlib158_qtlu60 | sign_flip_tsmin11 | 0.0348 | 0.570 | 0.108 |

## 当前全部存活 ID

qlib158_cntd60, qlib158_cntn20, qlib158_cntn30, qlib158_cntn60, qlib158_cord20, qlib158_cord30, qlib158_cord60, qlib158_klen, qlib158_klow, qlib158_kmid, qlib158_kmid2, qlib158_ksft, qlib158_ksft2, qlib158_max10, qlib158_max20, qlib158_max30, qlib158_max60, qlib158_qtlu60, qlib158_std10, qlib158_std20, qlib158_std30, qlib158_std5, qlib158_std60

机器可读：`repair_qlib158_reversed_log.json`。
