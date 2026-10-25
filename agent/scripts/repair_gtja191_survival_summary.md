# GTJA191 CSI300 2023-2026 失效因子修复汇总

存活门槛：`ic_mean > 0.02` 且 `ic_positive_ratio >= 0.55` 且 `|t| > 2`。

## 全量 rebench 结果

- status: `ok`
- **alive: 43** (基线 11 → +32)
- reversed: 6
- dead: 140
- tested: 189 / skipped: 2
- wall_seconds: 22.1

## 修复动作

- 成功写回：**32** 个（原公式保留在 `_compute_raw`）
- 尝试后放弃：**146** 个

## 已修复明细

| id | phase | attempt | ic_mean | ic+ | ir |
|---|---|---|---:|---:|---:|
| gtja191_007 | dead | smooth5_rank | 0.0322 | 0.550 | 0.113 |
| gtja191_010 | reversed | sign_flip | 0.0419 | 0.584 | 0.146 |
| gtja191_013 | reversed | sign_flip | 0.0364 | 0.572 | 0.121 |
| gtja191_030 | reversed | sign_flip | 0.0422 | 0.565 | 0.147 |
| gtja191_041 | near_miss | decay8_rank | 0.0309 | 0.551 | 0.114 |
| gtja191_070 | reversed | sign_flip | 0.0267 | 0.550 | 0.107 |
| gtja191_075 | dead | decay8_rank | 0.0248 | 0.552 | 0.083 |
| gtja191_077 | reversed | sign_flip | 0.0295 | 0.567 | 0.115 |
| gtja191_093 | reversed | sign_flip | 0.0476 | 0.579 | 0.149 |
| gtja191_095 | reversed | sign_flip | 0.0247 | 0.551 | 0.094 |
| gtja191_097 | near_miss | decay8_rank | 0.0272 | 0.554 | 0.113 |
| gtja191_100 | near_miss | smooth5_rank | 0.0278 | 0.556 | 0.115 |
| gtja191_124 | reversed | sign_flip | 0.0442 | 0.575 | 0.157 |
| gtja191_126 | reversed | sign_flip | 0.0364 | 0.570 | 0.121 |
| gtja191_127 | reversed | sign_flip | 0.0437 | 0.559 | 0.147 |
| gtja191_129 | reversed | sign_flip | 0.0518 | 0.587 | 0.160 |
| gtja191_143 | reversed | sign_flip | 0.0390 | 0.565 | 0.135 |
| gtja191_149 | reversed | sign_flip | 0.0377 | 0.553 | 0.121 |
| gtja191_153 | reversed | sign_flip | 0.0383 | 0.571 | 0.127 |
| gtja191_156 | dead | decay8_rank | 0.0304 | 0.582 | 0.126 |
| gtja191_158 | reversed | sign_flip | 0.0417 | 0.582 | 0.167 |
| gtja191_159 | reversed | sign_flip | 0.0573 | 0.582 | 0.203 |
| gtja191_160 | reversed | sign_flip | 0.0479 | 0.580 | 0.145 |
| gtja191_161 | reversed | sign_flip | 0.0473 | 0.584 | 0.151 |
| gtja191_167 | reversed | sign_flip | 0.0476 | 0.583 | 0.159 |
| gtja191_173 | reversed | sign_flip | 0.0361 | 0.568 | 0.120 |
| gtja191_174 | reversed | sign_flip | 0.0476 | 0.570 | 0.152 |
| gtja191_175 | reversed | sign_flip | 0.0478 | 0.591 | 0.153 |
| gtja191_178 | reversed | sign_flip | 0.0352 | 0.559 | 0.126 |
| gtja191_185 | near_miss | smooth5_rank | 0.0494 | 0.580 | 0.202 |
| gtja191_187 | reversed | sign_flip | 0.0438 | 0.582 | 0.144 |
| gtja191_189 | reversed | sign_flip | 0.0468 | 0.582 | 0.152 |

## 当前全部存活 ID

gtja191_002, gtja191_007, gtja191_010, gtja191_012, gtja191_013, gtja191_017, gtja191_030, gtja191_041, gtja191_054, gtja191_070, gtja191_075, gtja191_077, gtja191_081, gtja191_093, gtja191_095, gtja191_097, gtja191_100, gtja191_111, gtja191_114, gtja191_120, gtja191_124, gtja191_126, gtja191_127, gtja191_129, gtja191_143, gtja191_149, gtja191_153, gtja191_156, gtja191_158, gtja191_159, gtja191_160, gtja191_161, gtja191_163, gtja191_164, gtja191_167, gtja191_171, gtja191_173, gtja191_174, gtja191_175, gtja191_178, gtja191_185, gtja191_187, gtja191_189

## 放弃（|IC| 较高的前 20）

| id | phase | last_attempt | ic_mean | ic+ |
|---|---|---|---:|---:|
| gtja191_016 | dead | decay8_rank | 0.5000 | 1.000 |
| gtja191_036 | near_miss | decay8_rank | 0.0441 | 0.475 |
| gtja191_042 | near_miss | decay8_rank | 0.0347 | 0.540 |
| gtja191_182 | reversed | sign_flip_rank | 0.0262 | 0.548 |
| gtja191_170 | dead | sign_flip_smooth5_rank | 0.0251 | 0.537 |
| gtja191_088 | dead | decay8_rank | 0.0251 | 0.522 |
| gtja191_071 | dead | decay8_rank | 0.0249 | 0.527 |
| gtja191_137 | reversed | sign_flip_rank | 0.0246 | 0.538 |
| gtja191_180 | dead | sign_flip_smooth5_rank | 0.0242 | 0.542 |
| gtja191_027 | dead | decay8_rank | 0.0240 | 0.527 |
| gtja191_150 | reversed | sign_flip_rank | 0.0234 | 0.544 |
| gtja191_067 | dead | decay8_rank | 0.0233 | 0.546 |
| gtja191_082 | dead | sign_flip_smooth5_rank | 0.0219 | 0.525 |
| gtja191_125 | reversed | sign_flip_rank | 0.0219 | 0.534 |
| gtja191_045 | reversed | sign_flip_rank | 0.0212 | 0.529 |
| gtja191_132 | reversed | sign_flip_rank | 0.0212 | 0.534 |
| gtja191_072 | dead | sign_flip_smooth5_rank | 0.0212 | 0.529 |
| gtja191_116 | dead | decay8_rank | 0.0210 | 0.537 |
| gtja191_025 | dead | sign_flip_smooth5_rank | 0.0208 | 0.529 |
| gtja191_073 | dead | sign_flip_smooth5_rank | 0.0208 | 0.531 |

机器可读：`repair_gtja191_survival_log.json`、`repair_gtja191_rebench_result.json`。
