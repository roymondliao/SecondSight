# Acceptance Criteria — phase1-adapters (GUR-97)

This file is the binding acceptance contract for GUR-97. Lifted verbatim from `2-plan.md` §6 with verification commands. Each criterion has an explicit pass/fail check.

| ID | Criterion | Verification |
|----|-----------|--------------|
| AC-1 | `from secondsight.api.normalizer import` raises ImportError | `python -c "from secondsight.api.normalizer import Normalizer"` exits non-zero AND error contains `ModuleNotFoundError: No module named 'secondsight.api.normalizer'` |
| AC-2 | `AgentAdapter` is an ABC; missing-method subclass fails to instantiate | `pytest tests/adapters/test_base.py::test_abc_cannot_instantiate` green |
| AC-3 | `IdentityAdapter` passes all prior `IdentityNormalizer` tests + ABC structural tests | `pytest tests/adapters/test_identity.py` green; test count ≥ prior IdentityNormalizer test count |
| AC-4 | `ClaudeCodeAdapter().supported_event_types()` is superset of P1 floor | `pytest tests/adapters/test_claude_code.py::test_supported_event_types_floor` green |
| AC-5 | Every fixture round-trips with mapped fields populated and drop-listed fields absent | `pytest tests/adapters/test_claude_code.py -k fixture` green for every `tests/fixtures/claude_code/*.json` |
| AC-6 | Privacy canary absent from every produced `Event.data` | `pytest tests/adapters/test_claude_code.py::test_privacy_canary` green |
| AC-7 | `inject_hint` + `inject_convention` raise `NotImplementedError` with required messages | `pytest tests/adapters/test_base.py::test_inject_hint_loud` AND `::test_inject_convention_loud` green |
| AC-8 | `mypy` clean | `mypy src/secondsight/adapters tests/adapters` exit 0 |
| AC-9 | Full test suite passes | `pytest` exit 0; total test count ≥ 380 (baseline 351 + ≥ 29 new) |
| AC-10 | No production import of old normalizer path | `! grep -r "from secondsight.api.normalizer" src/` exit 0 (zero matches in src/) |

## North-star metric verification

The `claude_code_event_normalization_fidelity` metric (defined in kickoff §North Star) is verified by:

```bash
pytest tests/adapters/test_integration_claude_code.py::test_fidelity_against_fixtures -v
```

Pass criterion: for every fixture, every non-`_source`, non-`_meta` field either:
- is mapped to a `PartialEvent` field with the correct value, OR
- is explicitly listed in `ClaudeCodeAdapter.DROP_LIST` with rationale, OR
- appears in `Event.data._unmapped` for traceability (rare; flagged in scar).

The integration test prints a fidelity ratio per fixture; ratio < 1.0 fails the test with the unmapped field names.

## Bundle-level gate

GUR-97 ships only when ALL of the following are simultaneously true:

1. AC-1 through AC-10 green
2. North-star fidelity = 1.0 across all P1-floor fixtures
3. Yin reviewer (samsara:code-reviewer) verdict: PASS or PASS_WITH_CONCERNS where all CRITICALs are resolved
4. Quality reviewer (samsara:code-quality-reviewer) verdict: PASS or PASS_WITH_CONCERNS where all IMPORTANTs are resolved
5. Scar report complete; every item has resolution: resolved-in-task | deferred-to-iteration | deferred-to-phase-2 | accepted-as-documented
