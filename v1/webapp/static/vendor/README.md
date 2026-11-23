# Vendored third-party assets

Bootstrap 5.3.2, served from this machine rather than a CDN.

## Why these are in the repository

A lab PC is routinely offline. The page previously loaded both the Bootstrap
stylesheet *and the Bootstrap JavaScript bundle* from `cdn.jsdelivr.net`, with
a local fallback that covered only the CSS. With no internet `window.bootstrap`
was therefore undefined, and every dialog is a Bootstrap component — including
the one behind the **Emergency Stop** button, whose handler calls
`new bootstrap.Modal(...)`. The operator could not open the emergency stop
dialog on exactly the machine most likely to be disconnected.

Serving these two files locally removes that failure mode. They are pinned, not
tracked: upgrading is a deliberate act.

## Files

| File | Source |
|---|---|
| `bootstrap.min.css` | `bootstrap@5.3.2/dist/css/bootstrap.min.css` |
| `bootstrap.bundle.min.js` | `bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js` |

## What is deliberately *not* vendored

**Bootstrap Icons** still come from the CDN. Vendoring them costs roughly
300 KB of font files, and every control that moves hardware — STOP, STOP NOW,
Pause, Resume, Cancel — carries a text label next to its glyph, so a missing
icon is cosmetic rather than a safety problem. If that ever stops being true,
vendor `bootstrap-icons` here too.

## Upgrading

```
npm pack bootstrap@<version>
tar xzf bootstrap-<version>.tgz
cp package/dist/css/bootstrap.min.css package/dist/js/bootstrap.bundle.min.js \
   v1/webapp/static/vendor/
```

Then update the version in this file and re-run the webapp test suite:
`python -m pytest tests/unit/test_static_assets.py -q`
