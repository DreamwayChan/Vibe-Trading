## Summary

<!-- What does this PR do? 1-3 bullet points. -->

-

## Why

<!-- What problem does it solve? Link related issues with "Closes #123". -->

## Changes

<!-- List key changes. For new skills/presets, describe what they cover. -->

-

## Test Plan

- [ ] Offline CI pytest is green (`pip install -e ".[dev,ashare]"` then `pytest --ignore=agent/tests/e2e_backtest --ignore=agent/tests/test_e2e_harness_v2.py --disable-socket --allow-hosts=localhost,127.0.0.1,::1 --cov=agent --cov-report=term-missing --tb=short -q`)
- [ ] New tests added (if applicable)
- [ ] Tested manually (describe below)

## Checklist

- [ ] No changes to protected areas (`src/agent/`, `src/session/`, `src/providers/`) without prior discussion
- [ ] No hardcoded values (API keys, file paths, magic numbers)
- [ ] Code follows [CONTRIBUTING.md](../CONTRIBUTING.md) guidelines
- [ ] Documentation updated (if user-facing change)
