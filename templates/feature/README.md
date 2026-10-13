# Feature template

Copy this directory when you add a genuinely new data integration. It compiles, it is
tested by `tests/contracts/test_extension_smoke.py`, and it exercises the whole v1
contract: a typed settings model, an immutable payload, one bounded provider, one pure
presenter, `FEATURE` registration and offline tests.

The template is rendered with three placeholders:

| Placeholder | Meaning | Example |
|---|---|---|
| `{{FEATURE_ID}}` | feature id, lower snake case | `ticker` |
| `{{NAMESPACE}}` | type-id namespace before the dot | `demo` |
| `{{TITLE}}` | human label used in the widget title | `Demo` |

## Where to put it

```
src/sideboard/features/<feature_id>/
  __init__.py
  feature.py        # FEATURE = FeatureSpec(...)
  provider.py       # async fetch(context) -> Payload  +  aclose()
tests/features/<feature_id>/
  test_feature.py
```

Then add **one** line to `src/sideboard/features/registry.py`:

```python
from .<feature_id> import FEATURE as <CONSTANT>
FEATURES = (..., <CONSTANT>, ...)
```

Nothing else in core needs to change. If you find yourself editing `app.py`, the
scheduler, `core/storage.py` or the navigation keybindings, stop: an ordinary
integration must not require it.

## Checklist before you are done

- [ ] `uv run pytest tests/features/<feature_id> -q`
- [ ] `uv run pytest tests/contracts -q` (the shared contract suite picks your feature up)
- [ ] `uv run sideboard config validate`
- [ ] `uv run sideboard features list` shows your types
- [ ] compact, detail, privacy and offline states all render something honest
- [ ] setup, limits and blocked modes are documented in `docs/features.md`
- [ ] no import-time I/O, no credentials in config, no shell strings
